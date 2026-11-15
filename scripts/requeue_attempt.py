#!/usr/bin/env python3
"""Rerun a patch-only task from a later attempt, keeping the attempts before it.

When the feedback between attempts changes, the attempts that came after a failure were run on the old feedback.
The first attempt saw no feedback at all, so it stays as recorded. This script re-validates the recorded patch of
the attempt before --from-attempt with the current validation, writes the feedback that patch now gets, and sets up
a new run directory (status `requeued`, with a resume.json) that run_agent.py continues at --from-attempt. The lanes
treat a requeued task as not done, so the next run of the lane picks it up. The old run directory is left as it was;
the new one names it in `requeued_from`.

    python3 scripts/requeue_attempt.py --agent-output agent_output_claude_sonnet55 fmt/arvo_25884 htslib/arvo_24097
    python3 scripts/requeue_attempt.py ... --dry-run     # re-validate and show the feedback, write nothing
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import tomli

import run_agent


def latest_run(task_dir: Path) -> Path | None:
    runs = sorted(p for p in task_dir.iterdir() if p.is_dir()) if task_dir.is_dir() else []
    return runs[-1] if runs else None


def requeue(task: str, args) -> str:
    """Requeue one task; returns a one-line outcome."""
    task_dir = Path(args.agent_output) / task.replace("/", "_")
    old = latest_run(task_dir)
    if old is None or not (old / "summary.json").is_file():
        return "skipped: no run"
    summary = json.loads((old / "summary.json").read_text())
    if summary.get("status") == run_agent.REQUEUED:
        return f"skipped: {old.name} is already requeued"
    if summary.get("mode") != "patch-only":
        return f"skipped: mode {summary.get('mode')} (only patch-only is supported)"
    keep = args.from_attempt - 1
    attempts = summary.get("attempts") or []
    if len(attempts) < keep or any(a.get("success") for a in attempts[:keep]):
        return f"skipped: the run has no failed attempt {keep}"
    patch = old / "output" / f"fix_attempt_{keep}.patch"
    if not patch.is_file():
        # no patch: its feedback ("No fix.patch was generated") is the same under every validation
        return f"skipped: attempt {keep} left no patch, its feedback does not change"

    script_path = Path(args.script_dir) / task
    config = tomli.loads((script_path / "../project.toml").read_text())
    config.update(tomli.loads((script_path / "config.toml").read_text()))
    build_image = config.get("build_image", args.default_build_image)
    print(f"[{task}] re-validating attempt {keep}'s patch from {old.name} on {build_image} ...", flush=True)
    started = time.time()
    results = run_agent.run_final_validation(None, patch, Path(args.data_dir) / task, script_path, build_image,
                                             "patch-only")
    seconds = time.time() - started
    record = dict(attempts[keep - 1])
    before = (record.get("stage3"), record.get("stage4"))
    now = (results.get("stage3"), results.get("stage4"))
    print(f"[{task}] attempt {keep}: recorded stages {before}, now {now} ({seconds:.0f}s)", flush=True)
    if now == ("passed", "passed"):
        return (f"NOT requeued: attempt {keep}'s patch passes now (recorded {before}); "
                "look at it by hand, it may be a flaky suite")
    feedback = run_agent.format_feedback(results, keep, "patch-only", patch_file=patch)
    if args.dry_run:
        print(feedback)
        return f"dry run: would requeue at attempt {args.from_attempt}"

    new = task_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{old.name.split('_', 2)[2]}"
    if new.exists() or new.name <= old.name:
        return f"skipped: the new run directory {new.name} would not sort after {old.name}"
    (new / "output").mkdir(parents=True)
    (new / "trajectory").mkdir()
    for n in range(1, keep + 1):
        for rel in (f"output/fix_attempt_{n}.patch", f"trajectory/attempt_{n}.log"):
            if (old / rel).is_file():
                shutil.copy2(old / rel, new / rel)
        if (old / f"workspace_attempt_{n}").is_dir():
            shutil.copytree(old / f"workspace_attempt_{n}", new / f"workspace_attempt_{n}")
    for n in range(1, keep):
        if (old / f"output/feedback_attempt_{n}.txt").is_file():
            shutil.copy2(old / f"output/feedback_attempt_{n}.txt", new / f"output/feedback_attempt_{n}.txt")
    (new / "output" / f"feedback_attempt_{keep}.txt").write_text(feedback)

    record.update({
        "stage3": now[0], "stage4": now[1], "agent_success": False, "gt_success": now[1] == "passed",
        "success": False, "recorded_stages": list(before), "revalidation_seconds": round(seconds, 1),
    })
    kept = [*attempts[:keep - 1], record]
    charged = float(record.get("budget_charged_seconds") or 0.0)
    run_key = "|".join(str(summary.get(k)) for k in ("agent", "model", "mode", "prompt_style", "max_attempts", "timeout"))
    (new / run_agent.RESUME_FILE).write_text(json.dumps({
        "run_key": run_key, "attempt": args.from_attempt, "feedback": feedback, "attempts": kept,
        "charged": charged, "interruptions": 0, "endpoint_waits": 0, "requeued_from": old.name,
    }, indent=2))
    # the duration a resumed run adds its own to: what the kept attempts spent working
    spent = sum(float(a.get("agent_exec_seconds") or 0) + float(a.get("validation_seconds") or 0) for a in kept)
    (new / "summary.json").write_text(json.dumps({
        **{k: summary.get(k) for k in ("task", "agent", "prompt_style", "mode", "max_attempts", "timeout", "model")},
        "status": run_agent.REQUEUED, "attempts": kept, "budget_charged_seconds": charged,
        "duration_seconds": spent, "duration_minutes": round(spent / 60, 2), "output_dir": str(new.absolute()),
        "quota_interruptions": 0, "endpoint_interruptions": 0, "requeued_from": old.name,
    }, indent=2))
    return f"requeued at attempt {args.from_attempt} in {new.name} (from {old.name})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tasks", nargs="+", help="project/task_id")
    parser.add_argument("--agent-output", required=True, help="the lane's output folder")
    parser.add_argument("--from-attempt", type=int, default=2)
    parser.add_argument("--data-dir", default="./data/projects")
    parser.add_argument("--script-dir", default="./projects")
    parser.add_argument("--default-build-image",
                        default="gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.from_attempt < 2:
        parser.error("--from-attempt must be 2 or later: attempt 1 has no feedback to change")
    outcomes = {task: requeue(task, args) for task in args.tasks}
    print(f"\n{args.agent_output}:")
    for task, outcome in outcomes.items():
        print(f"  {task:34} {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
