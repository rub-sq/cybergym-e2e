#!/usr/bin/env python3
"""Run two agent lanes concurrently for weeks, each blocking independently.

Lanes:
  openhands  - KIConnect models, reached through scripts/kiconnect_proxy.py so
               several keys can be pooled and rotated on quota.
  claude     - the host's logged-in Claude Code CLI (subscription auth).

The lanes are deliberately decoupled. When OpenHands has burned every KIConnect
key it waits on the proxy; that must not stall Claude. When Claude hits its
5-hour or weekly session limit it sleeps until the stated reset; that must not
stall OpenHands. Each lane runs exactly one task at a time.

The one thing they DO share is the disk. Before starting any task a lane asks
the DiskGate for a slot. If free space has fallen below the threshold the gate
stops handing out slots, waits for whatever is already running to finish, runs
the same scoped Docker cleanup the checkpoint script uses, and only then lets
work resume. That replaces fixed checkpoints with a condition that reflects
what is actually happening to the disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.absolute()
REPO_ROOT = SCRIPTS_DIR.parent
IMAGE_PREFIXES = r"^(cybergym/|n132/arvo|gcr\.io/oss-fuzz-base)"
STOP = threading.Event()


def log(lane, msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{lane:<9}] {msg}", flush=True)


def free_gb(path="/"):
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


# --- docker cleanup (scoped: this is a shared machine) ----------------------
def docker_cleanup():
    """Remove our orphaned containers and idle images. Never a blanket prune."""
    try:
        listing = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}}"],
            capture_output=True, text=True, timeout=120,
        ).stdout.splitlines()
        ours = [
            line.split()[0]
            for line in listing
            if len(line.split()) > 1 and re.match(IMAGE_PREFIXES, line.split()[1])
        ]
        if ours:
            log("cleanup", f"removing {len(ours)} container(s)")
            subprocess.run(["docker", "rm", "-f", *ours],
                           capture_output=True, timeout=600)
        images = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=120,
        ).stdout.splitlines()
        targets = [i for i in images if re.match(IMAGE_PREFIXES, i) and "<none>" not in i]
        if targets:
            subprocess.run(["docker", "rmi", *targets], capture_output=True, timeout=1800)
        log("cleanup", f"done; free disk {free_gb():.0f}G")
    except Exception as exc:
        log("cleanup", f"cleanup error (continuing): {exc}")


class DiskGate:
    """Admission control on free disk, shared by every lane."""

    def __init__(self, threshold_gb, cleanup=docker_cleanup):
        self._cv = threading.Condition()
        self._active = 0
        self._draining = False
        self._threshold = threshold_gb
        self._cleanup = cleanup

    def acquire(self, lane):
        while not STOP.is_set():
            with self._cv:
                if self._draining:
                    self._cv.wait(timeout=5)
                    continue
                available = free_gb()
                if available >= self._threshold:
                    self._active += 1
                    return True
                # Whoever notices first becomes the drainer.
                self._draining = True
                log(lane, f"free disk {available:.0f}G < {self._threshold}G - draining")
                while self._active > 0 and not STOP.is_set():
                    self._cv.wait(timeout=10)
            try:
                self._cleanup()
            finally:
                with self._cv:
                    self._draining = False
                    self._cv.notify_all()
            if free_gb() < self._threshold:
                log(lane, f"still only {free_gb():.0f}G after cleanup - waiting 10min")
                STOP.wait(600)
        return False

    def release(self):
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()


# --- resume -----------------------------------------------------------------
def latest_summary(task, output_dir):
    tdir = Path(output_dir) / task.replace("/", "_")
    if not tdir.is_dir():
        return None
    runs = sorted(p for p in tdir.iterdir() if p.is_dir())
    for run in reversed(runs):
        summary = run / "summary.json"
        if summary.is_file():
            try:
                return json.JSONDecoder().raw_decode(
                    summary.read_text(encoding="utf-8", errors="replace")
                )[0]
            except Exception:
                continue
    return None


def already_done(task, output_dir):
    """A task counts as done on success, or on a failure that got a fair run."""
    data = latest_summary(task, output_dir)
    if not data:
        return False
    if str(data.get("status", "")).upper() == "SUCCESS":
        return True
    return data.get("duration_seconds", 0) >= 60


# --- lanes ------------------------------------------------------------------
QUOTA_MARKER = re.compile(r"CLAUDE_QUOTA_EXHAUSTED wake_at=(\S+)")


class Lane(threading.Thread):
    def __init__(self, name, tasks, output_dir, build_cmd, gate, env=None,
                 log_path=None, handles_claude_quota=False):
        super().__init__(name=name, daemon=True)
        self.lane = name
        self.tasks = tasks
        self.output_dir = output_dir
        self.build_cmd = build_cmd
        self.gate = gate
        self.env = env or {}
        self.log_path = log_path
        self.handles_claude_quota = handles_claude_quota
        self.done = 0
        self.skipped = 0
        self.blocked_until = None
        self.crashed = False
        self.pre_done = 0

    def run(self):
        try:
            self._run_lane()
        except Exception:
            import traceback
            log(self.lane, "LANE CRASHED - see traceback below")
            traceback.print_exc()
            self.crashed = True

    def _run_lane(self):
        total = len(self.tasks)
        # Count what is already complete up front. Walking the list and
        # counting skips as you go only tells you how far the cursor has
        # moved, which on a resumed run reads as though nothing was done.
        self.pre_done = sum(1 for t in self.tasks if already_done(t, self.output_dir))
        log(self.lane,
            f"lane started: {self.pre_done}/{total} already complete, "
            f"{total - self.pre_done} to run")
        index = 0
        while index < len(self.tasks) and not STOP.is_set():
            task = self.tasks[index]
            if already_done(task, self.output_dir):
                self.skipped += 1
                index += 1
                continue
            if not self.gate.acquire(self.lane):
                break
            try:
                wake_at = self._run_task(task, index + 1, total)
            finally:
                # Release BEFORE any quota sleep. Holding a slot through a
                # multi-hour wait would stop the DiskGate from ever draining
                # (it waits for active==0) and deadlock the other lane.
                self.gate.release()
            if wake_at:
                self._sleep_until(wake_at, task)
                continue          # same task again once the window reopens
            self.done += 1
            index += 1
        log(self.lane,
            f"lane finished: {self.done} run this session, "
            f"{self.pre_done + self.done}/{len(self.tasks)} complete overall")

    def _run_task(self, task, position, total):
        """Run one task. Returns a wake-up time if the lane must wait, else None."""
        cmd = self.build_cmd(task)
        env = {**os.environ, **self.env}
        # `position` is the slot in the full task list, not a progress count -
        # a resumed lane starts near the top while most tasks are already done.
        # Report both so the log cannot be misread.
        completed = self.pre_done + self.done
        log(self.lane,
            f"[slot {position}/{total} | done {completed}] {task}  "
            f"(free {free_gb():.0f}G)")
        started = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(REPO_ROOT), env=env, capture_output=True,
                text=True, timeout=None,
            )
        except Exception as exc:
            log(self.lane, f"launch failed for {task}: {exc}")
            return None
        output = (proc.stdout or "") + (proc.stderr or "")
        if self.log_path:
            # A full disk must not kill the lane. This exact write raised
            # ENOSPC once and silently took the OpenHands lane down for 35
            # hours; losing a log line is always preferable to losing a lane.
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n===== {task} =====\n{output}\n")
            except OSError as exc:
                log(self.lane, f"could not write task log ({exc}); continuing")

        if self.handles_claude_quota:
            match = QUOTA_MARKER.search(output)
            if match:
                return match.group(1)
            if "CLAUDE_AUTH_ERROR" in output:
                log(self.lane, "claude is not logged in - stopping this lane")
                STOP.set()
                return None

        mins = (time.time() - started) / 60
        verdict = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        log(self.lane, f"    {task} -> {verdict} in {mins:.1f}m")
        return None

    def _sleep_until(self, iso_time, task):
        try:
            wake = datetime.fromisoformat(iso_time)
        except ValueError:
            wake = datetime.now(timezone.utc)
        if wake.tzinfo is None:
            wake = wake.replace(tzinfo=timezone.utc)
        self.blocked_until = wake
        while not STOP.is_set():
            remaining = (wake - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            log(self.lane,
                f"quota exhausted on {task}; sleeping {remaining/60:.0f}m "
                f"until {wake.astimezone().strftime('%H:%M')} (other lane keeps running)")
            STOP.wait(min(remaining, 600))
        self.blocked_until = None



# --- command builders -------------------------------------------------------
def openhands_cmd_factory(model_id, output_dir, attempts, timeout):
    def build(task):
        return [
            sys.executable, str(SCRIPTS_DIR / "run_agent.py"), task,
            "--agent", "openhands",
            "--prompt-style", "no-test",
            "--mode", "patch-only",
            "--max-attempts", str(attempts),
            "--timeout", str(timeout),
            "--model-provider", "kiconnect",
            "--kiconnect-model-id", model_id,
            "--agent-output", output_dir,
        ]
    return build


def claude_cmd_factory(model, output_dir, attempts, timeout):
    def build(task):
        return [
            sys.executable, str(SCRIPTS_DIR / "run_agent.py"), task,
            "--agent", "claude-code-host",
            "--prompt-style", "no-test",
            "--mode", "patch-only",
            "--max-attempts", str(attempts),
            "--timeout", str(timeout),
            "--claude-host-model", model,
            "--agent-output", output_dir,
        ]
    return build


def read_tasks(path):
    return [ln.strip() for ln in Path(path).read_text().splitlines() if ln.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default="tasks_920.txt")
    ap.add_argument("--min-free-gb", type=float, default=50.0)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--log-dir", default="parallel_logs_dual")

    ap.add_argument("--openhands", action="store_true", help="enable the OpenHands lane")
    ap.add_argument("--openhands-model", default="openai-gpt-oss-120b",
                    help="KIConnect model id (discover with the /models endpoint)")
    ap.add_argument("--openhands-output", default="agent_output_openhands_codex")
    ap.add_argument("--proxy-url", default="http://host.docker.internal:8817/v1",
                    help="URL of kiconnect_proxy.py AS SEEN FROM INSIDE THE CONTAINER")

    ap.add_argument("--claude", action="store_true", help="enable the Claude Code lane")
    ap.add_argument("--claude-model", default="sonnet")
    ap.add_argument("--claude-output", default="agent_output_claude_host")
    args = ap.parse_args()

    if not args.openhands and not args.claude:
        ap.error("enable at least one lane: --openhands and/or --claude")

    tasks = read_tasks(args.tasks)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    gate = DiskGate(args.min_free_gb)

    def handle_signal(signum, frame):
        log("main", "stop requested; finishing current tasks then exiting")
        STOP.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("=" * 68)
    print(f"  tasks           : {len(tasks)} from {args.tasks}")
    print(f"  disk threshold  : {args.min_free_gb:.0f}G (currently {free_gb():.0f}G free)")
    print(f"  lanes           : "
          f"{'openhands ' if args.openhands else ''}{'claude' if args.claude else ''}")
    print("=" * 68)

    lanes = []
    if args.openhands:
        Path(args.openhands_output).mkdir(parents=True, exist_ok=True)
        lanes.append(Lane(
            "openhands", tasks, args.openhands_output,
            openhands_cmd_factory(args.openhands_model, args.openhands_output,
                                  args.max_attempts, args.timeout),
            gate,
            env={"KICONNECT_BASE_URL": args.proxy_url,
                 "KICONNECT_API_KEY": os.getenv("KICONNECT_API_KEY", "proxy-managed")},
            log_path=str(log_dir / "openhands.log"),
        ))
    if args.claude:
        Path(args.claude_output).mkdir(parents=True, exist_ok=True)
        lanes.append(Lane(
            "claude", tasks, args.claude_output,
            claude_cmd_factory(args.claude_model, args.claude_output,
                               args.max_attempts, args.timeout),
            gate,
            log_path=str(log_dir / "claude.log"),
            handles_claude_quota=True,
        ))

    for lane in lanes:
        lane.start()
    try:
        reported = set()
        while any(lane.is_alive() for lane in lanes):
            for lane in lanes:
                if not lane.is_alive() and lane.lane not in reported:
                    reported.add(lane.lane)
                    state = "crashed" if lane.crashed else "finished"
                    log("main", f"*** lane '{lane.lane}' {state} "
                                f"({lane.done} done) - other lanes continue ***")
            time.sleep(5)
    except KeyboardInterrupt:
        STOP.set()
    for lane in lanes:
        lane.join(timeout=30)

    print("=" * 68)
    for lane in lanes:
        print(f"  {lane.lane:<10} {lane.done} run, {lane.skipped} skipped")
    print(f"  free disk       : {free_gb():.0f}G")
    print("=" * 68)


if __name__ == "__main__":
    main()
