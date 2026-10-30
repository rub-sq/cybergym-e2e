#!/usr/bin/env python3
"""How the in-container agent CLIs fail, and when a spent quota comes back.

claude (subscription) and opencode (KIConnect, through scripts/kiconnect_proxy.py) report a spent
quota or a rejected login only in their output. The runners classify that output and raise the
exceptions below; run_agent.py turns them into the markers the orchestrator acts on (sleep until the
wake-up time, or stop the lane).
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kiconnect_proxy import QUOTA_RE

# --- claude -------------------------------------------------------------------
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


def classify_claude_output(text):
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


# --- opencode + KIConnect -------------------------------------------------------
AUTH_RE = re.compile(
    r"\b401\b|\b403\b|unauthorized|invalid (?:api )?key|"
    r"api key (?:is )?(?:invalid|missing|required)|no api key|authentication (?:failed|required)",
    re.IGNORECASE,
)
# The proxy's own exhaustion errors mean "every pooled key is locked for this
# model right now" - a quota condition, not an auth one.
POOL_EXHAUSTED_RE = re.compile(
    r"no key available|all \d+ keys locked|exhausted \d+ attempts across key pool",
    re.IGNORECASE,
)


class OpencodeQuotaExhausted(Exception):
    """Raised when the key pool is locked or KIConnect reports quota exhaustion."""

    def __init__(self, wake_at: datetime, evidence: str = ""):
        super().__init__(f"opencode quota exhausted; resume at {wake_at.isoformat()}")
        self.wake_at = wake_at
        self.evidence = evidence


class OpencodeAuthError(Exception):
    """Raised when the kiconnect key is missing or rejected - waiting won't help."""


def classify_opencode_output(text: str) -> str:
    """Return 'quota', 'auth' or 'other' for opencode + kiconnect output."""
    if POOL_EXHAUSTED_RE.search(text) or QUOTA_RE.search(text):
        return "quota"
    if AUTH_RE.search(text):
        return "auth"
    return "other"


def pool_lock_seconds(base_url: str, model: str) -> int:
    """Longest remaining key lock for `model` in the local proxy pool, else 0.

    When the proxy reports a lock we can sleep exactly until it frees instead
    of guessing with the generic backoff. base_url is the proxy as the container
    sees it; asked from the host, host.docker.internal is this machine.
    """
    base = base_url.replace("host.docker.internal", "127.0.0.1")
    if base.endswith("/v1"):
        base = base[:-3]
    url = base.rstrip("/") + "/_pool"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            pool = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return 0
    longest = 0
    for info in pool.values():
        for lock_model, remaining in (info.get("locked") or {}).items():
            if lock_model in (model, "") and remaining > longest:
                longest = int(remaining)
    return longest
