#!/usr/bin/env python3
"""Run a CLI agent (opencode, claude) INSIDE the task container.

The CLI is driven with `docker exec` against the already-set-up task container (/src holds the
source, /output the results dir), as the unprivileged agent user, so the agent never sees anything
outside its container: a CLI on the host could walk up from its workspace into this repository and
read projects/<task>/patch.diff, the ground-truth patch, and one did. By the time a runner starts,
run_agent.py has closed the container's network to the lane's model endpoint (scripts/sandbox.py).
The container's baseline git commit (made by setup_workspace) means `git diff` in the repository
yields the patch.

opencode talks to the KIConnect proxy through the address the OpenHands lane uses: start_container()
maps host.docker.internal to host-gateway, and on hosts where host-gateway is refused (the tower) the
operator overrides it with OPENHANDS_PROXY_URL (the host's LAN IP); this module reuses that variable,
so there is one knob per host.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import exec_run
from cli_failures import (
    ClaudeAuthError,
    ClaudeQuotaExhausted,
    OpencodeAuthError,
    OpencodeQuotaExhausted,
    classify_claude_output,
    classify_opencode_output,
    compute_wake_at,
    pool_lock_seconds,
)

AGENT_USER = "agent"          # created by run_agent._restrict_files
WORKDIR = "/work"
# `su` runs a login shell that DROPS the environment docker exec passes in,
# so per-run variables (OPENCODE_CONFIG, the claude login token) are written
# to a file the agent user reads, and `source`d inside the su'd command.
ENV_FILE = f"{WORKDIR}/agent.env"
# What a client in the container sends as its KIConnect key. The proxy owns the real keys and sets
# Authorization itself; the placeholder only has to look like a key (id:secret), because opencode 2
# rejects a value of another shape before sending anything.
PROXY_KEY_PLACEHOLDER = "proxy-managed:not-a-real-key"


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
        "export PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH",
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
    # exec_run is root, but the tree is chowned to the agent user; without
    # safe.directory git refuses ("dubious ownership"), the redirected stderr
    # hides it and every run would look like no_patch.
    exec_run(
        container_id,
        f"cd {repo_dir_q} && git -c safe.directory='*' "
        f"-c user.email=cybergym@localhost "
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
def _host_claude_credentials() -> str | None:
    """The subscription login of the host's Claude Code, as the JSON its credentials file holds.

    Linux keeps it in ~/.claude/.credentials.json, macOS in the user's login Keychain.
    """
    credentials = Path(os.path.expanduser("~/.claude/.credentials.json"))
    if credentials.is_file():
        return credentials.read_text(encoding="utf-8")
    if sys.platform == "darwin":
        found = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                               capture_output=True, text=True)
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    return None


def _copy_claude_credentials(container_id) -> bool:
    """Give the agent user the host's subscription login, and nothing else of ~/.claude.

    The rest of ~/.claude is the host user's own: prompt history of every project, plans, settings,
    memory files. The login goes through stdin, so it is never written to a file on the host.
    """
    credentials = _host_claude_credentials()
    if not credentials:
        return False
    home = f"/home/{AGENT_USER}/.claude"
    written = subprocess.run(
        ["docker", "exec", "-i", container_id, "sh", "-c",
         f"mkdir -p {home} && umask 077 && cat > {home}/.credentials.json && "
         f"chown -R {AGENT_USER}:{AGENT_USER} {home}"],
        input=credentials, text=True, capture_output=True,
    )
    return written.returncode == 0


def run_claude_in_container(container_id, repo_to_patch, prompt, model,
                            timeout=3600, wait_cycle=1, log_path=None):
    """Run the claude CLI inside the task container.

    Returns (exit_code, None, log_text, 0); the patch is left at /output/fix.patch.
    Raises ClaudeQuotaExhausted / ClaudeAuthError, which the orchestrator turns
    into a quota sleep or a stopped lane.

    Authentication, first match wins: CLAUDE_CODE_OAUTH_TOKEN (a long-lived subscription token from
    `claude setup-token`, nothing to refresh), the host's subscription login (its credentials file, or
    the Keychain on macOS), ANTHROPIC_API_KEY (bills the API instead of the subscription).
    """
    repo_dir = f"/src/{repo_to_patch}" if repo_to_patch else "/src"

    # 1. Provision auth. The container reaches only the Anthropic API (and its login host, for a
    #    token refresh), so the CLI's update checks, telemetry and error reports are off.
    claude_env = {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        claude_env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    elif _copy_claude_credentials(container_id):
        pass
    elif os.environ.get("ANTHROPIC_API_KEY"):
        claude_env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    else:
        raise ClaudeAuthError(
            "no claude login for the container: set CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token` "
            "once on this host), or log in to claude on this host, or set ANTHROPIC_API_KEY"
        )

    # 2. Prompt and config as files - never through a shell at exec time.
    _write_container_file(container_id, f"{WORKDIR}/.prompt.txt", prompt)
    _write_container_file(container_id, f"{WORKDIR}/empty_mcp.json",
                          json.dumps({"mcpServers": {}}))

    # 3. Run as the unprivileged agent user. bypassPermissions is required for unattended runs;
    #    the locked container is the sandbox. WebSearch runs on Anthropic's servers, beyond the
    #    network lock, so it is the --disallowedTools that keeps it out.
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

    # 4. Hard failures first: auth and quota raise; anything else falls through to best-effort
    #    patch extraction (the CLI sometimes exits non-zero after making edits).
    if code != 0:
        verdict = classify_claude_output(output)
        if verdict == "auth":
            raise ClaudeAuthError(
                "claude is not authenticated in the container - refresh CLAUDE_CODE_OAUTH_TOKEN, "
                "re-run `claude` on the host, or set ANTHROPIC_API_KEY\n" + output[-800:]
            )
        if verdict == "quota":
            raise ClaudeQuotaExhausted(
                compute_wake_at(output, wait_cycle=wait_cycle), output[-800:]
            )

    _extract_patch(container_id, repo_dir)
    return code, None, output, 0


# --- opencode-in-container ---------------------------------------------------
def _opencode_config(model: str, base_url: str) -> str:
    """Per-run opencode config: one provider (KIConnect through the proxy), no web tools.

    apiKey is resolved from the child's environment (KICONNECT_API_KEY), where
    it is only the proxy placeholder.
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


def _opencode_run_flags(container_id) -> list[str]:
    """--auto and --standalone when this opencode release knows them (1.x rejects --standalone).

    --standalone gives the run a private server instead of opencode 2's shared background service.
    """
    _, help_text, _ = exec_run(container_id, "opencode run --help 2>&1", verbose=False)
    return [flag for flag in ("--auto", "--standalone") if flag in help_text]


def run_opencode_in_container(container_id, repo_to_patch, prompt, model,
                              timeout=3600, wait_cycle=1, log_path=None, base_url=None):
    """Run the opencode CLI inside the task container. base_url overrides the proxy URL (a run-scoped one
    carries the run's id, see key_wait_clock).

    Returns (exit_code, None, log_text, 0); the patch is left at /output/fix.patch.
    Raises OpencodeQuotaExhausted / OpencodeAuthError, which the orchestrator
    turns into a quota sleep or a stopped lane.
    """
    repo_dir = f"/src/{repo_to_patch}" if repo_to_patch else "/src"

    # 1. Config (base URL = proxy as seen from inside the container) and
    #    prompt as files - nothing shell-interpolated at exec time.
    base_url = base_url or container_proxy_url()
    _write_container_file(container_id, f"{WORKDIR}/opencode.json",
                          _opencode_config(model, base_url))
    _write_container_file(container_id, f"{WORKDIR}/.prompt.txt", prompt)

    # 2. Run opencode in the repo dir as the unprivileged agent user.
    #    (opencode v2 has no --dir: the process CWD is the project.)
    env = {
        "OPENCODE_CONFIG": f"{WORKDIR}/opencode.json",
        "KICONNECT_API_KEY": PROXY_KEY_PLACEHOLDER,
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    }
    _write_env_file(container_id, env)
    run_cmd = (
        f"cd {shlex.quote(repo_dir)} && opencode run "
        f"--model {shlex.quote('kiconnect/' + model)} "
        f"{' '.join(_opencode_run_flags(container_id))} --format default "
        f"\"$(cat {shlex.quote(WORKDIR + '/.prompt.txt')})\""
    )
    code, stdout, stderr = _run_as_agent(container_id, run_cmd,
                                         "Running opencode in container", timeout)

    output = (stdout or "") + (("\n--- stderr ---\n" + stderr) if stderr else "")
    if log_path:
        Path(log_path).write_text(output, encoding="utf-8")

    # 3. Hard failures first: auth and quota raise.
    if code != 0:
        verdict = classify_opencode_output(output)
        if verdict == "auth":
            raise OpencodeAuthError(
                "opencode/kiconnect authentication failed inside the container "
                f"- check the proxy is reachable from the container ({base_url})\n"
                + output[-800:]
            )
        if verdict == "quota":
            lock = pool_lock_seconds(base_url, model)
            if lock > 0:
                wake = datetime.now(timezone.utc) + timedelta(seconds=lock + 120)
            else:
                wake = compute_wake_at(output, wait_cycle=wait_cycle)
            raise OpencodeQuotaExhausted(wake, output[-800:])

    # 4. Best-effort patch extraction (git diff vs baseline commit).
    _extract_patch(container_id, repo_dir)
    return code, None, output, 0
