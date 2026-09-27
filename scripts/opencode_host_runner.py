#!/usr/bin/env python3
"""Run the host's opencode CLI against a task exported from its container.

Same shape as claude_host_runner: the LLM is a host-side CLI (here opencode,
talking to KIConnect either directly or through scripts/kiconnect_proxy.py),
so the source tree is exported from the task container, opencode edits the
export, and the resulting git diff is fed back into the normal validation
pipeline.

The provider is configured with a per-run opencode.json (written into the
work dir and pointed at via OPENCODE_CONFIG) so the model id, base URL and
API key all come from the environment instead of any stored credentials.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_host_runner import (
    _terminate,
    compute_wake_at,
    export_repo,
    localize_prompt,
    write_patch,
)
from kiconnect_proxy import QUOTA_RE

UPSTREAM_DEFAULT = "https://chat.kiconnect.nrw/api/v1"

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


def classify_output(text: str) -> str:
    """Return 'quota', 'auth' or 'other' for opencode + kiconnect output."""
    if POOL_EXHAUSTED_RE.search(text) or QUOTA_RE.search(text):
        return "quota"
    if AUTH_RE.search(text):
        return "auth"
    return "other"


def _pool_lock_seconds(base_url: str, model: str) -> int:
    """Longest remaining key lock for `model` in the local proxy pool, else 0.

    When the proxy reports a lock we can sleep exactly until it frees instead
    of guessing with the generic backoff.
    """
    base = base_url
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


def _wake_at(text: str, wait_cycle: int, base_url: str, model: str) -> datetime:
    lock = _pool_lock_seconds(base_url, model)
    if lock > 0:
        return datetime.now(timezone.utc) + timedelta(seconds=lock + 120)
    return compute_wake_at(text, wait_cycle=wait_cycle)


def _write_config(work_dir: Path, model: str, base_url: str) -> Path:
    """Write the per-run opencode config; apiKey is resolved from the child's
    environment (KICONNECT_API_KEY) so the real key never lands in the file."""
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"kiconnect/{model}",
        "autoupdate": False,
        "snapshot": False,
        "share": "disabled",
        "enabled_providers": ["kiconnect"],
        "tools": {"webfetch": False, "websearch": False},
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
        "provider": {
            "kiconnect": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "KI:connect - Inferenz NRW",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:KICONNECT_API_KEY}",
                    # Slow 27B inference behind a queue: no request deadline,
                    # but a dead connection (no chunk for 10 min) still aborts.
                    "timeout": False,
                    "headerTimeout": False,
                    "chunkTimeout": 600000,
                },
                "models": {
                    model: {
                        "name": model,
                        "limit": {"context": 262144, "output": 131072},
                    }
                },
            }
        },
    }
    path = work_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def run_opencode_on_host(container_id, repo_to_patch, prompt, work_dir, model,
                         timeout=3600, wait_cycle=1, log_path=None,
                         keep_workspace=False):
    """Execute opencode on the host and return (exit_code, patch_path, log_text, elapsed).

    Raises OpencodeQuotaExhausted (with a wake_at) or OpencodeAuthError so the
    caller can distinguish "come back later" from "stop, this needs a human".
    """
    work_dir = Path(work_dir).resolve()
    repo_dir = export_repo(container_id, repo_to_patch, work_dir)
    host_src_root = work_dir / "src"
    host_output = work_dir / "output"
    local_prompt = localize_prompt(prompt, host_src_root, host_output)
    (work_dir / "prompt.txt").write_text(local_prompt, encoding="utf-8")

    base_url = os.getenv("KICONNECT_BASE_URL") or UPSTREAM_DEFAULT
    proxied = base_url != UPSTREAM_DEFAULT
    api_key = os.getenv("KICONNECT_API_KEY", "")
    if not api_key:
        if not proxied:
            raise OpencodeAuthError(
                "KICONNECT_API_KEY env var not set; required for direct kiconnect access "
                "(or point KICONNECT_BASE_URL at the key-rotating proxy)"
            )
        # Proxied: the proxy rewrites Authorization, but the client must still
        # present a key that LOOKS valid - some CLI versions validate the key
        # format before sending, and a bare placeholder would be rejected or
        # sent upstream verbatim. A real pooled key is safe either way.
        api_key = os.getenv("KICONNECT_KEY1") or "proxy-managed"

    config_path = _write_config(work_dir, model, base_url)
    # opencode v2 dropped --dir (it runs in the process CWD, set via cwd=
    # below) and gained --standalone, which keeps each run on its own
    # private server instead of a shared background daemon - sequential
    # benchmark tasks must not leak state across each other.
    command = [
        "opencode", "run",
        "--standalone",
        "--model", f"kiconnect/{model}",
        "--auto",
        "--format", "default",
        local_prompt,
    ]
    (work_dir / "command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(config_path)
    env["KICONNECT_API_KEY"] = api_key
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    # Only kiconnect is enabled in the config; drop stray keys so a built-in
    # provider can never be billed by accident.
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(var, None)

    log_path = Path(log_path).resolve() if log_path else (work_dir / "opencode_output.txt")
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

    # The exported source tree is hundreds of MB per task, so it must be
    # removed on EVERY exit path (same lesson as claude_host_runner).
    try:
        if returncode != 0:
            verdict = classify_output(output)
            if verdict == "auth":
                raise OpencodeAuthError(
                    "opencode/kiconnect authentication failed - check KICONNECT_API_KEY.\n"
                    + output[-800:]
                )
            if verdict == "quota":
                raise OpencodeQuotaExhausted(
                    _wake_at(output, wait_cycle, base_url, model), output[-800:]
                )

        patch_path = write_patch(repo_dir, host_output)
        return returncode, patch_path, output, elapsed
    finally:
        if not keep_workspace:
            shutil.rmtree(host_src_root, ignore_errors=True)
