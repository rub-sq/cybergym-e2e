"""Keeps waits for a model key off an agent's clock.

kiconnect_proxy.py holds a request while every key is busy or quota-locked, and the agent sits in that wait.
A run's requests carry its id in the URL (run_url), the proxy adds up the time at least one of them waited
(GET /_waits/<run id>), and AgentClock lets the agent run until its working time -- time elapsed minus those
waits -- reaches its allowance. Then it times the agent out exactly as the in-container `timeout` wrapper
would, by sending that wrapper SIGALRM (GNU timeout treats it as its own timer firing).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
import uuid

POLL_S = 5.0
# How long beyond its allowance a run may spend waiting for keys: one quota lock (kiconnect_proxy --lock-seconds).
MAX_KEY_WAIT_S = 7200

# The oldest `timeout` in the container is the wrapper exec_run put around the agent; the agent's own commands
# start later. /proc is read directly: procps (pkill, pgrep) is not in every task image.
_ALARM_AGENT_WRAPPER = (
    'pid=$(for d in /proc/[0-9]*; do [ "$(cat "$d/comm" 2>/dev/null)" = timeout ] && echo "${d#/proc/}"; done'
    ' | sort -n | head -1); [ -n "$pid" ] && kill -ALRM "$pid"'
)


def new_run_id(label: str) -> str:
    """A run id the proxy accepts: letters, digits, '_', '.', '-'."""
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in label)[:80]
    return f"{safe}-{uuid.uuid4().hex[:8]}"


def run_url(base_url: str, run_id: str) -> str:
    """The proxy URL that tags every request with run_id (a /run/<id> prefix the proxy strips)."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base[:-3]}/run/{run_id}/v1"
    return f"{base}/run/{run_id}"


def _host_root(container_url: str) -> str:
    """The proxy's root as reached from the host: host.docker.internal only resolves inside containers."""
    base = container_url.rstrip("/").replace("host.docker.internal", "127.0.0.1")
    return base[:-3] if base.endswith("/v1") else base


def is_proxy(container_url: str | None) -> bool:
    """Whether the URL points at kiconnect_proxy.py (it answers /_pool), not straight at a provider."""
    if not container_url:
        return False
    try:
        with urllib.request.urlopen(_host_root(container_url) + "/_pool", timeout=5) as resp:
            return isinstance(json.loads(resp.read().decode("utf-8", "replace")), dict)
    except Exception:  # noqa: BLE001 -- not the proxy, or not reachable from the host
        return False


def waited_seconds(container_url: str, run_id: str) -> float | None:
    """The run's key-wait seconds so far, or None when the proxy cannot be asked."""
    try:
        with urllib.request.urlopen(f"{_host_root(container_url)}/_waits/{run_id}", timeout=5) as resp:
            return float(json.loads(resp.read().decode("utf-8", "replace"))["seconds"])
    except Exception:  # noqa: BLE001
        return None


class AgentClock:
    """Times out the agent in container_id once its working time reaches allowance_s; key waits do not count.

    The proxy's total only grows, so the largest answer seen is kept: a query that fails never takes waits back.
    """

    def __init__(self, container_id: str, container_url: str, run_id: str, allowance_s: float,
                 poll_s: float = POLL_S) -> None:
        self.container_id = container_id
        self.container_url = container_url
        self.run_id = run_id
        self.allowance_s = allowance_s
        self.poll_s = poll_s
        self.waited = 0.0
        self.fired = False
        self._t0 = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "AgentClock":
        self._t0 = time.monotonic()
        self._thread.start()
        return self

    def _refresh(self) -> None:
        seen = waited_seconds(self.container_url, self.run_id)
        if seen is not None and seen > self.waited:
            self.waited = seen

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            self._refresh()
            if time.monotonic() - self._t0 - self.waited >= self.allowance_s:
                self.fired = True
                try:
                    subprocess.run(["docker", "exec", self.container_id, "bash", "-c", _ALARM_AGENT_WRAPPER],
                                   capture_output=True, timeout=30)
                except Exception:  # noqa: BLE001 -- the wrapper's own outer limit still ends the agent
                    pass
                return

    def stop(self) -> float:
        """Stop watching; returns the run's key-wait seconds."""
        self._stop.set()
        self._thread.join(timeout=self.poll_s + 35)
        self._refresh()
        return self.waited
