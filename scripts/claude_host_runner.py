#!/usr/bin/env python3
"""Run Claude Code on the HOST against a task exported from its container.

Why not in the container: a Claude Pro/Max subscription authenticates through an
interactive OAuth login whose credentials live in the user's home directory. They
cannot be forwarded into a fresh container, and ANTHROPIC_API_KEY is deliberately
not an option here. So the source tree is exported to a host workspace, the
already-logged-in `claude` binary runs there, and the resulting diff is fed back
into the normal validation pipeline.

ANTHROPIC_API_KEY is explicitly removed from the child environment: if it were
present the CLI would bill the API instead of using the subscription.

Quota handling (5-hour and weekly session limits) is detected from the CLI's own
output and surfaced as ClaudeQuotaExhausted carrying a wake-up time, so a caller
can sleep exactly as long as needed instead of polling blindly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- failure classification -------------------------------------------------
QUOTA_PATTERNS = (
    r"\b(?:usage|weekly|session|rate|5[-\s]hour|five[-\s]hour)\s*limit\b",
    r"you(?:['’]ve| have)\s+(?:hit|reached)\s+(?:your|the)\b[^\n]{0,40}\blimit\b",
    r"\blimit\s+will\s+reset\b",
    r"\bout of\b[^\n]{0,40}\busage\b",
    r"\brate[-\s]limited\b",
)
AUTH_PATTERNS = (
    r"not logged in",
    r"authentication (?:failed|required)",
    r"please (?:log|sign) in",
    r"invalid (?:oauth|credentials|token)",
    r"unauthorized",
)
TRANSIENT_PATTERNS = (
    r"internal server error",
    r"service unavailable",
    r"bad gateway",
    r"gateway timeout",
    r"(?:http|status|error)[^\n]{0,20}\b(?:500|502|503|504)\b",
    r"connection (?:reset|timed out|refused)",
    r"network error",
    r"temporarily unavailable",
    r"overloaded",
)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}
CLOCK_PATTERN = (
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<timezone>[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+)\))?"
)


class ClaudeQuotaExhausted(Exception):
    """Raised when the subscription's session or weekly limit is spent."""

    def __init__(self, wake_at: datetime, evidence: str):
        super().__init__(f"Claude quota exhausted; resume at {wake_at.isoformat()}")
        self.wake_at = wake_at
        self.evidence = evidence


class ClaudeAuthError(Exception):
    """Raised when the CLI is not logged in - waiting will not help."""


@dataclass
class QuotaReset:
    reset_at: datetime
    source: str


def _local_timezone():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _resolve_clock(match):
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or "0")
    ampm = (match.group("ampm") or "").lower()
    if not 0 <= minute <= 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return hour, minute
    if match.group("minute") is None or not 0 <= hour <= 23:
        return None
    return hour, minute


def _resolve_timezone(match):
    name = match.group("timezone")
    if not name:
        return _local_timezone()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def parse_quota_reset(text, now=None):
    """Extract an explicit reset time from CLI output, in UTC, if one is stated."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    lower = text.lower()

    retry_after = re.search(r"retry[- ]after\s*[:=]?\s*(\d+)", lower)
    if retry_after:
        return QuotaReset(
            now + timedelta(seconds=int(retry_after.group(1))), retry_after.group(0)
        )

    iso = re.search(
        r"(?P<stamp>20\d{2}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
        r"(?:z|[+-]\d{2}:?\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if iso:
        stamp = iso.group("stamp").replace(" ", "T")
        if stamp.lower().endswith("z"):
            stamp = stamp[:-1] + "+00:00"
        if re.search(r"[+-]\d{4}$", stamp):
            stamp = stamp[:-5] + stamp[-5:-2] + ":" + stamp[-2:]
        try:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_local_timezone())
            return QuotaReset(parsed.astimezone(timezone.utc), iso.group(0))
        except ValueError:
            pass

    weekday = re.search(
        r"reset(?:s)?(?:\s+(?:at|on))?\s+"
        r"\b(?P<weekday>mon|tue|wed|thu|fri|sat|sun)[a-z]*,?(?:\s+at)?\s+" + CLOCK_PATTERN,
        text,
        re.IGNORECASE,
    )
    if weekday:
        clock = _resolve_clock(weekday)
        tz = _resolve_timezone(weekday)
        if clock and tz:
            hour, minute = clock
            local_now = now.astimezone(tz)
            target = WEEKDAYS[weekday.group("weekday").lower()]
            cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            cand += timedelta(days=(target - local_now.weekday()) % 7)
            if cand <= local_now:
                cand += timedelta(days=7)
            return QuotaReset(cand.astimezone(timezone.utc), weekday.group(0))

    clock_only = re.search(
        r"(?:reset(?:s)?|try again)(?:\s+(?:at|after|on))?\s+" + CLOCK_PATTERN
        + r"(?=\s|$|[.,;])",
        text,
        re.IGNORECASE,
    )
    if clock_only:
        clock = _resolve_clock(clock_only)
        tz = _resolve_timezone(clock_only)
        if clock and tz:
            hour, minute = clock
            local_now = now.astimezone(tz)
            cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand <= local_now:
                cand += timedelta(days=1)
            return QuotaReset(cand.astimezone(timezone.utc), clock_only.group(0))
    return None


def classify_output(text):
    """Return 'quota', 'auth', 'transient' or 'other' for Claude CLI output."""
    for pattern in AUTH_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "auth"
    for pattern in QUOTA_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "quota"
    for pattern in TRANSIENT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "transient"
    return "other"


def compute_wake_at(text, wait_cycle=1, fallback_seconds=1800,
                    max_fallback_seconds=18000, buffer_seconds=120):
    """When does quota likely return? Prefer a stated reset, else backoff."""
    now = datetime.now(timezone.utc)
    parsed = parse_quota_reset(text, now)
    if parsed:
        reset_at = parsed.reset_at
    else:
        progressive = fallback_seconds * (2 ** max(wait_cycle - 1, 0))
        reset_at = now + timedelta(seconds=min(progressive, max_fallback_seconds))
    return reset_at + timedelta(seconds=buffer_seconds)


# --- execution --------------------------------------------------------------
def _terminate(proc, grace=5):
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()


def export_repo(container_id, repo_to_patch, dest_dir):
    """Copy /src out of the container so the host agent has something to edit."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src_root = dest_dir / "src"
    if src_root.exists():
        shutil.rmtree(src_root)
    src_root.mkdir(parents=True)

    subprocess.run(
        ["docker", "cp", f"{container_id}:/src/{repo_to_patch}", str(src_root / repo_to_patch)],
        check=True, capture_output=True,
    )
    # Context files the prompt refers to; absent in some tasks, so best effort.
    for name in ("crash.log", "poc.bin"):
        subprocess.run(
            ["docker", "cp", f"{container_id}:/src/{name}", str(src_root / name)],
            check=False, capture_output=True,
        )
    (dest_dir / "output").mkdir(exist_ok=True)
    return src_root / repo_to_patch


def localize_prompt(prompt, host_src_root, host_output_dir):
    """Repoint container paths at the host workspace, keeping prompt wording intact."""
    return prompt.replace("/src/", f"{host_src_root}/").replace(
        "/output/", f"{host_output_dir}/"
    )


def run_claude_on_host(container_id, repo_to_patch, prompt, work_dir, model,
                       timeout=3600, wait_cycle=1, log_path=None):
    """Execute Claude Code on the host and return (exit_code, patch_path, log_text).

    Raises ClaudeQuotaExhausted (with a wake_at) or ClaudeAuthError so the caller
    can distinguish "come back later" from "stop, this needs a human".
    """
    work_dir = Path(work_dir)
    repo_dir = export_repo(container_id, repo_to_patch, work_dir)
    host_src_root = work_dir / "src"
    host_output = work_dir / "output"
    local_prompt = localize_prompt(prompt, host_src_root, host_output)
    (work_dir / "prompt.txt").write_text(local_prompt, encoding="utf-8")

    empty_mcp = work_dir / "empty_mcp.json"
    empty_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    command = [
        "claude", "-p", local_prompt,
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--output-format", "text",
        "--no-session-persistence",
        "--setting-sources", "project",
        "--mcp-config", str(empty_mcp),
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--disallowedTools", "WebSearch,WebFetch,mcp__*",
    ]
    (work_dir / "command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")

    env = os.environ.copy()
    # Force the subscription path; with a key present the CLI would bill the API.
    env.pop("ANTHROPIC_API_KEY", None)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"

    log_path = Path(log_path) if log_path else (work_dir / "claude_output.txt")
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            command, cwd=str(repo_dir), env=env,
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            text=True, start_new_session=(os.name != "nt"),
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            returncode = 124
    elapsed = time.time() - started
    output = log_path.read_text(encoding="utf-8", errors="replace")

    if returncode != 0:
        verdict = classify_output(output)
        if verdict == "auth":
            raise ClaudeAuthError(
                "claude is not authenticated on this host - run `claude` once "
                "interactively and log in, then retry.\n" + output[-800:]
            )
        if verdict == "quota":
            raise ClaudeQuotaExhausted(
                compute_wake_at(output, wait_cycle=wait_cycle), output[-800:]
            )

    patch_path = write_patch(repo_dir, host_output)
    return returncode, patch_path, output, elapsed


def write_patch(repo_dir, host_output):
    """Produce fix.patch from the repo's own git state.

    Deriving the diff ourselves rather than trusting a hand-written file is
    deliberate: agents fabricate hunk headers and line numbers that then fail to
    apply. If git yields nothing, fall back to whatever the agent wrote.
    """
    host_output = Path(host_output)
    host_output.mkdir(parents=True, exist_ok=True)
    patch_path = host_output / "fix.patch"

    result = subprocess.run(
        ["git", "diff"], cwd=str(repo_dir), capture_output=True, text=True, check=False
    )
    if result.returncode == 0 and result.stdout.strip():
        patch_path.write_text(result.stdout, encoding="utf-8")
        return patch_path
    if patch_path.is_file() and patch_path.read_text(encoding="utf-8").strip():
        return patch_path
    return None
