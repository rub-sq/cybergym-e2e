#!/usr/bin/env python3
"""Run a host-CLI agent (opencode, claude) INSIDE the task container.

The host-based runners (claude_host_runner / opencode_host_runner) export the
source tree out of the container, let a host-side CLI edit it, and diff it.
That works, but a host agent can walk up from its workspace into the benchmark
repo and read projects/<task>/patch.diff (ground-truth patch) - observed in
practice (mongoose/arvo_51757, 2026-10).

These runners instead drive the same CLI with `docker exec` against the
already-set-up task container (/src holds the source, /output the results
dir), so the agent never sees anything outside its container. The container's
baseline git commit (made by setup_workspace) means `git diff` in the repo
directory yields the patch, exactly like the host runners.

opencode additionally talks to the KIConnect proxy through the same address
the OpenHands lane already uses: start_container() maps host.docker.internal
to host-gateway, and on hosts where host-gateway is refused (the tower) the
operator overrides with OPENHANDS_PROXY_URL (host LAN IP) - this module
reuses that variable so there is one knob per host.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import exec_run
from claude_host_runner import (
    ClaudeQuotaExhausted,
    ClaudeAuthError,
    compute_wake_at,
    classify_output as classify_claude_output,
)
from opencode_host_runner import (
    OpencodeQuotaExhausted,
    OpencodeAuthError,
    _pool_lock_seconds,
    classify_output as classify_opencode_output,
)

AGENT_USER = "agent"          # created by install() in run_agent.py
WORKDIR = "/work"
# `su` runs a login shell that DROPS the environment docker exec passes in,
# so per-run variables (API key, OPENCODE_CONFIG) are written to a file the
# agent user reads, and `source`d inside the su'd command.
ENV_FILE = f"{WORKDIR}/agent.env"


def container_proxy_url() -> str:
    """Proxy URL as seen from INSIDE the container.

    KICONNECT_BASE_URL is set per-lane by the orchestrator to the
    container-side URL (host.docker.internal, or the OPENHANDS_PROXY_URL
    override where host-gateway is refused); OPENHANDS_PROXY_URL wins when
    the operator passed it explicitly.
    """
    url = os.getenv("OPENHANDS_PROXY_URL") or os.getenv("KICONNECT_BASE_URL")
    return url or "http://host.docker.internal:8817/v1"


def _write_container_file(container_id, path, content):
    exec_run(
        container_id,
        f"cat > {path} << 'EOF_CYBERGYM'\n{content}\nEOF_CYBERGYM",
        verbose=False,
    )


def _write_env_file(container_id, env):
    """Write per-run variables the su'd agent command must see.

    `su agent -c` starts a login shell that resets the environment (and the
    agent user's PATH misses /usr/local/bin where the installers put the CLIs),
    so the command sources this file first. Values are shlex-quoted; the
    file is owned by the agent user and only holds what that agent needs.
    """
    lines = [
        "export HOME=/home/agent",
        "export PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:"
        "/root/.opencode/bin:$PATH",
    ]
    lines += [f"export {k}={shlex.quote(str(v))}" for k, v in env.items()]
    _write_container_file(container_id, ENV_FILE, "\n".join(lines))
    exec_run(container_id,
             f"chown {AGENT_USER}:{AGENT_USER} {ENV_FILE} && "
             f"chmod 600 {ENV_FILE}",
             verbose=False)


def _run_as_agent(container_id, run_cmd, description, timeout):
    """Run a command as the agent user with ENV_FILE sourced first."""
    return exec_run(
        container_id,
        f"su {AGENT_USER} -c {shlex.quote(f'. {ENV_FILE} && ' + run_cmd)}",
        description,
        timeout=timeout,
        verbose=False,
    )


def _extract_patch(container_id, repo_dir):
    """Write `git diff` vs the baseline commit to /output/fix.patch INSIDE the
    container (where run_agent.py's normal copy-out step already looks).

    Returns True if a non-empty diff was written, else False. The patch lives
    in the container so the agent never produces a host-side artifact and the
    copy-out path is identical for every agent backend.
    """
    repo_dir_q = shlex.quote(repo_dir)
    # The diff is written to /output/fix.patch entirely inside the container
    # (temp + move so a partial write never leaves a half-empty file). No
    # round-trip through the host: patches can be large and the container
    # already owns the only copy of the source tree.
    exec_run(
        container_id,
        f"cd {repo_dir_q} && git -c user.email=cybergym@localhost "
        f"-c user.name=cybergym diff > /output/.fix.patch.tmp 2>/dev/null; "
        f"if [ -s /output/.fix.patch.tmp ]; then "
        f"mv /output/.fix.patch.tmp /output/fix.patch; "
        f"else rm -f /output/.fix.patch.tmp; fi",
        "git diff -> /output/fix.patch (in-container)",
        verbose=False,
        workdir="/",
    )
    code, _, _ = exec_run(container_id,
                          "test -s /output/fix.patch",
                          verbose=False, workdir="/")
    return code == 0


# --- claude-in-container -----------------------------------------------------
def _claude_home() -> Path:
    """Host-side Claude Code state (subscription OAuth credentials live here)."""
    return Path(os.path.expanduser("~/.claude"))


def _copy_claude_home(container_id) -> bool:
    """Stream the host's ~/.claude into the container for the agent user.

    Session history (projects/, shell snapshots) is excluded: it is bulky,
    machine-specific and useless inside a fresh container. The agent user's
    HOME is /home/agent (created by useradd -m in install), and the run
    command sets HOME to match so the CLI finds its state.
    """
    home = _claude_home()
    code, _, stderr = exec_run(container_id,
                               f"mkdir -p /home/{AGENT_USER}/.claude",
                               "mkdir .claude in container", verbose=False)
    if code != 0:
        raise ClaudeAuthError(f"could not prepare .claude dir: {stderr[-200:]}")
    tar = subprocess.Popen(
        ["tar", "-C", str(home),
         "--exclude=projects", "--exclude=shell-snapshots", "--exclude=todos",
         "-cf", "-", "."],
        stdout=subprocess.PIPE,
    )
    docker = subprocess.Popen(
        ["docker", "exec", "-i", container_id,
         "tar", "-xf", "-", "-C", f"/home/{AGENT_USER}/.claude"],
        stdin=tar.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tar.stdout.close()
    ok = docker.wait() == 0 and tar.wait() == 0
    if ok:
        exec_run(container_id,
                 f"chown -R {AGENT_USER}:{AGENT_USER} /home/{AGENT_USER}/.claude",
                 "chown .claude in container", verbose=False)
    return ok


def run_claude_in_container(container_id, repo_to_patch, prompt, model,
                            timeout=3600, wait_cycle=1, log_path=None):
    """Run the claude CLI inside the task container.

    Returns (exit_code, patch_host_path_or_None, log_text, elapsed).
    Raises ClaudeQuotaExhausted / ClaudeAuthError exactly like
    claude_host_runner.run_claude_on_host, so the orchestrator's existing
    lane logic (quota sleep, auth stop) handles both identically.

    Authentication: the subscription login on this host (~/.claude) is
    streamed into the container. ANTHROPIC_API_KEY in the runner environment
    is the fallback when no host login exists (bills the API instead of the
    subscription; the host runner deliberately avoids that, but in-container
    there is no other way to authenticate).
    """
    repo_dir = f"/src/{repo_to_patch}" if repo_to_patch else "/src"

    # 1. Provision auth: copy the host subscription, else inject an API key.
    claude_env = {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    home = _claude_home()
    if home.exists():
        if not _copy_claude_home(container_id):
            raise ClaudeAuthError(
                "could not copy the host's ~/.claude into the container "
                "(set ANTHROPIC_API_KEY to fall back to API billing)\n"
            )
    elif os.environ.get("ANTHROPIC_API_KEY"):
        claude_env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    else:
        raise ClaudeAuthError(
            "no claude subscription on this host (~/.claude missing) and "
            "ANTHROPIC_API_KEY not set; cannot authenticate in-container "
            "claude code"
        )

    # 2. Prompt and config as files - never through a shell at exec time.
    _write_container_file(container_id, f"{WORKDIR}/.prompt.txt", prompt)
    _write_container_file(container_id, f"{WORKDIR}/empty_mcp.json",
                          json.dumps({"mcpServers": {}}))

    # 3. Run as the unprivileged agent user that install() creates.
    #    bypassPermissions is required for unattended runs; the container
    #    itself is the sandbox (same posture as the host runner, which runs
    #    with bypassPermissions on the host workspace).
    run_cmd = (
        f"cd {shlex.quote(repo_dir)} && claude -p \"$(cat {shlex.quote(WORKDIR + '/.prompt.txt')})\" "
        f"--model {shlex.quote(model)} "
        "--permission-mode bypassPermissions "
        "--output-format text "
        "--no-session-persistence "
        "--setting-sources project "
        f"--mcp-config {shlex.quote(WORKDIR + '/empty_mcp.json')} "
        "--strict-mcp-config "
        "--disable-slash-commands "
        "--disallowedTools WebSearch,WebFetch,mcp__*"
    )
    _write_env_file(container_id, claude_env)
    code, stdout, stderr = _run_as_agent(container_id, run_cmd,
                                         "Running claude in container", timeout)

    output = (stdout or "") + (("\n--- stderr ---\n" + stderr) if stderr else "")
    if log_path:
        Path(log_path).write_text(output, encoding="utf-8")

    # 4. Hard failures first, with the host runner's semantics: auth and
    #    quota raise; anything else falls through to best-effort patch
    #    extraction (the CLI sometimes exits non-zero after making edits).
    if code != 0:
        verdict = classify_claude_output(output)
        if verdict == "auth":
            raise ClaudeAuthError(
                "claude is not authenticated in the container - re-run "
                "`claude` interactively on the host (or set ANTHROPIC_API_KEY)\n"
                + output[-800:]
            )
        if verdict == "quota":
            raise ClaudeQuotaExhausted(
                compute_wake_at(output, wait_cycle=wait_cycle), output[-800:]
            )

    _extract_patch(container_id, repo_dir)
    # The patch lives at /output/fix.patch in the container; run_agent.py
    # copies it out with the standard docker cp path (host_patch is None).
    return code, None, output, 0


# --- opencode-in-container ---------------------------------------------------
def _opencode_config(model: str, base_url: str) -> str:
    """Per-run opencode config, same shape as opencode_host_runner._write_config.

    apiKey is resolved from the child's environment (KICONNECT_API_KEY) so
    the real key never lands in a file; the value only needs to exist and
    look valid because the proxy rewrites Authorization.
    """
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
    return json.dumps(config, indent=2)


def run_opencode_in_container(container_id, repo_to_patch, prompt, model,
                              timeout=3600, wait_cycle=1, log_path=None):
    """Run the opencode CLI inside the task container.

    Returns (exit_code, patch_host_path_or_None, log_text, elapsed).
    Raises OpencodeQuotaExhausted / OpencodeAuthError exactly like
    opencode_host_runner.run_opencode_on_host, so the orchestrator's
    existing lane logic handles both identically.
    """
    repo_dir = f"/src/{repo_to_patch}" if repo_to_patch else "/src"

    # 1. Config (base URL = proxy as seen from inside the container) and
    #    prompt as files - nothing shell-interpolated at exec time.
    base_url = container_proxy_url()
    _write_container_file(container_id, f"{WORKDIR}/opencode.json",
                          _opencode_config(model, base_url))
    _write_container_file(container_id, f"{WORKDIR}/.prompt.txt", prompt)

    # 2. Run opencode in the repo dir as the unprivileged agent user.
    #    (opencode v2 has no --dir: the process CWD is the project.)
    env = {
        "OPENCODE_CONFIG": f"{WORKDIR}/opencode.json",
        "KICONNECT_API_KEY": os.getenv("KICONNECT_API_KEY")
                             or os.getenv("KICONNECT_KEY1") or "proxy-managed",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    }
    _write_env_file(container_id, env)
    run_cmd = (
        f"cd {shlex.quote(repo_dir)} && opencode run "
        "--standalone "
        f"--model {shlex.quote('kiconnect/' + model)} "
        "--auto --format default "
        f"\"$(cat {shlex.quote(WORKDIR + '/.prompt.txt')})\""
    )
    code, stdout, stderr = _run_as_agent(container_id, run_cmd,
                                         "Running opencode in container", timeout)

    output = (stdout or "") + (("\n--- stderr ---\n" + stderr) if stderr else "")
    if log_path:
        Path(log_path).write_text(output, encoding="utf-8")

    # 3. Hard failures first, with the host runner's semantics.
    if code != 0:
        verdict = classify_opencode_output(output)
        if verdict == "auth":
            raise OpencodeAuthError(
                "opencode/kiconnect authentication failed inside the container "
                f"- check the proxy is reachable from the container ({base_url})\n"
                + output[-800:]
            )
        if verdict == "quota":
            lock = _pool_lock_seconds(base_url, model)
            if lock > 0:
                wake = datetime.now(timezone.utc) + timedelta(seconds=lock + 120)
            else:
                wake = compute_wake_at(output, wait_cycle=wait_cycle)
            raise OpencodeQuotaExhausted(wake, output[-800:])

    # 4. Best-effort patch extraction (git diff vs baseline commit).
    _extract_patch(container_id, repo_dir)
    # The patch lives at /output/fix.patch in the container; run_agent.py
    # copies it out with the standard docker cp path (host_patch is None).
    return code, None, output, 0
