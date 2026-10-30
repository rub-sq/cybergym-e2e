#!/usr/bin/env python3
"""Lock an agent's task container down to its model endpoint before the agent starts.

Coding agents asked to fix a bug have gone looking for the maintainers' fix instead: on GitHub, in
the project's upstream history, in patch files next to their workspace. Turning off an agent's web
tools does not stop `curl` or `git clone` from its shell, so the container itself is closed once the
agent is installed:

* egress: the container's network namespace accepts only TCP to the lane's model endpoint (the
  KIConnect proxy on the host, or the Anthropic API) and rejects everything else, IPv6 included;
* names: the allowed hosts are pinned in /etc/hosts and resolv.conf is emptied, so nothing else
  resolves;
* the firewall is written by a short-lived helper container that joins only the network namespace
  and holds NET_ADMIN. The task container never has that capability, so nothing in it, root
  included (OpenHands runs as root), can change the rules.

verify() then tries, inside the locked container, what an agent must not be able to do, and the run
does not start unless every attempt fails and the endpoint answers.

    python3 scripts/sandbox.py --check                  # preflight on a scratch container, no model call
    python3 scripts/sandbox.py --check --image IMAGE --allow http://host.docker.internal:8817/v1
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import socket
import subprocess
import sys
import uuid
from urllib.parse import urlparse

from utils import exec_run, start_container, cleanup_container

HELPER_IMAGE = "cybergym-netlock:1"
_HELPER_DOCKERFILE = "FROM alpine:3.20\nRUN apk add --no-cache iptables ip6tables\n"
AGENT_USER = "agent"

# Where an agent would look for the fix, tried by name (needs DNS) and, in verify(), also by address.
BLOCKED_URLS = (
    "https://github.com", "https://raw.githubusercontent.com", "https://codeload.github.com",
    "https://api.github.com", "https://gitlab.com", "https://pypi.org", "https://registry.npmjs.org",
    "https://1.1.1.1", "http://8.8.8.8",
)
_ADDRESS_PROBES = ("github.com", "raw.githubusercontent.com")  # resolved on the host, tried by IP inside

# Run in the helper container (alpine, iptables), inside the task container's network namespace.
_FIREWALL = r"""
set -e
ipt=iptables
$ipt -S >/dev/null 2>&1 || ipt=iptables-legacy
$ipt -S >/dev/null 2>&1 || { echo "iptables is not usable in this network namespace" >&2; exit 97; }
ip6=ip6tables; [ "$ipt" = iptables-legacy ] && ip6=ip6tables-legacy
$ipt -F OUTPUT
$ipt -A OUTPUT -o lo -j ACCEPT
$ipt -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for target in $ALLOW; do
  $ipt -A OUTPUT -p tcp -d "${target%:*}" --dport "${target##*:}" -j ACCEPT
done
$ipt -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset
$ipt -A OUTPUT -j REJECT
$ipt -P OUTPUT DROP
$ip6 -F OUTPUT 2>/dev/null || true
$ip6 -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
$ip6 -P OUTPUT DROP 2>/dev/null || true
$ipt -S OUTPUT
"""

# Run inside the locked task container, as root and as the agent user. One line per check; exit 1 on any failure.
_VERIFY = r"""
fail=0
ok() { echo "ok:   $*"; }
bad() { echo "FAIL: $*"; fail=1; }
http() { curl "$@" -sk -m 8 -o /dev/null -w '%{http_code}' 2>/dev/null || true; }

bnd=$(awk '/^CapBnd/ {print $2}' /proc/self/status)
if [ $(( 0x$bnd & 0x1000 )) -ne 0 ]; then bad "NET_ADMIN is in the bounding set: the firewall could be changed"
else ok "no process in this container can get NET_ADMIN"; fi
if [ "$(id -u)" != 0 ]; then
  eff=$(awk '/^CapEff/ {print $2}' /proc/self/status)
  [ "$eff" = 0000000000000000 ] && ok "agent has no capabilities" || bad "agent keeps capabilities ($eff)"
  if sudo -n true >/dev/null 2>&1; then bad "agent can become root with sudo"; else ok "agent cannot become root (sudo)"; fi
  for f in /src/*.sh /src/crash.log /src/poc.bin /config/config.toml /scripts/validate.py; do
    [ -e "$f" ] || continue
    if [ -w "$f" ]; then bad "agent can write $f"; else ok "read-only for the agent: $f"; fi
  done
  if [ -n "$REPO" ]; then
    if [ -w "$REPO" ]; then ok "agent can write its repository $REPO"; else bad "agent cannot write its repository $REPO"; fi
  fi
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is missing, nothing could be checked"; exit 1; }

if getent hosts github.com >/dev/null 2>&1; then bad "github.com resolves"; else ok "no DNS: github.com does not resolve"; fi
for url in $BLOCKED; do
  code=$(http -4 "$url")
  if [ -n "$code" ] && [ "$code" != 000 ]; then bad "reachable: $url (HTTP $code)"; else ok "blocked: $url"; fi
done
for address in $BLOCKED_ADDRESSES; do
  for port in 443 22 9418; do
    if timeout 5 bash -c "exec 3<>/dev/tcp/$address/$port" 2>/dev/null; then bad "TCP open: $address:$port"
    else ok "blocked: $address:$port"; fi
  done
done
if command -v python3 >/dev/null 2>&1; then
  if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
q = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06github\x03com\x00\x00\x01\x00\x01'
s.sendto(q, ('8.8.8.8', 53)); s.recvfrom(512)" 2>/dev/null; then bad "a DNS server answers over UDP (8.8.8.8:53)"
  else ok "blocked: DNS over UDP (8.8.8.8:53)"; fi
fi
code=$(http -6 https://github.com)
if [ -n "$code" ] && [ "$code" != 000 ]; then bad "IPv6 egress is open"; else ok "blocked: IPv6"; fi

for url in $ALLOWED; do
  code=$(curl -sk -m 15 -o /dev/null -w '%{http_code}' "$url" 2>/tmp/.endpoint.err || true)
  if [ -z "$code" ] || [ "$code" = 000 ]; then bad "model endpoint not reachable: $url ($(tr '\n' ' ' < /tmp/.endpoint.err))"
  else ok "model endpoint reachable: $url (HTTP $code)"; fi
done
rm -f /tmp/.endpoint.err
exit $fail
"""


class SandboxError(RuntimeError):
    """The container could not be locked down, or the lock did not hold."""


def endpoint(url: str) -> tuple[str, int]:
    """(host, port) of a URL."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise SandboxError(f"cannot read a host from the endpoint URL {url!r}")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _is_ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def ensure_helper_image() -> None:
    """Build the firewall helper image once per host."""
    if subprocess.run(["docker", "image", "inspect", HELPER_IMAGE], capture_output=True).returncode == 0:
        return
    result = subprocess.run(["docker", "build", "-t", HELPER_IMAGE, "-"], input=_HELPER_DOCKERFILE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SandboxError(f"could not build {HELPER_IMAGE}: {result.stderr[-500:]}")


def lock(container_id: str, urls: list[str]) -> dict:
    """Close the container's network to the hosts of `urls`; returns what was allowed, for the run record.

    Names are resolved inside the container while it still has DNS (host.docker.internal comes from its
    /etc/hosts), then pinned there before resolv.conf is emptied.
    """
    allowed, rules, pins = [], [], []
    for host, port in dict.fromkeys(endpoint(url) for url in urls):
        if _is_ipv4(host):
            addresses = [host]
        else:
            _, out, _ = exec_run(container_id, f"getent ahostsv4 {shlex.quote(host)} | awk '{{print $1}}' | sort -u",
                                 verbose=False, workdir="/")
            addresses = out.split()
            if not addresses:
                raise SandboxError(f"cannot resolve {host} inside the container")
            pins += [f"{address} {host}" for address in addresses]
        rules += [f"{address}:{port}" for address in addresses]
        allowed.append({"host": host, "port": port, "addresses": addresses})

    code, _, err = exec_run(
        container_id,
        (f"printf '%s\\n' {' '.join(shlex.quote(p) for p in pins)} >> /etc/hosts && " if pins else "")
        + ": > /etc/resolv.conf",
        verbose=False, workdir="/",
    )
    if code != 0:
        raise SandboxError(f"could not pin the allowed hosts: {err[-300:]}")

    ensure_helper_image()
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", f"container:{container_id}", "--cap-add", "NET_ADMIN",
         "-e", f"ALLOW={' '.join(rules)}", HELPER_IMAGE, "sh", "-c", _FIREWALL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise SandboxError(f"could not write the firewall: {(result.stderr or result.stdout)[-500:]}")
    return {
        "allowed": allowed,
        "rules": result.stdout.strip().splitlines(),
        "enforced_by": "iptables in the container's network namespace, written by a helper container; "
                       "the task container has no NET_ADMIN (scripts/sandbox.py)",
    }


def _host_addresses() -> list[str]:
    """IPv4 addresses of hosts that hold upstream sources, resolved here (the locked container has no DNS)."""
    addresses = []
    for name in _ADDRESS_PROBES:
        try:
            addresses += sorted({info[4][0] for info in socket.getaddrinfo(name, 443, socket.AF_INET)})[:1]
        except OSError:
            pass
    return addresses


def verify(container_id: str, urls: list[str], users: tuple[str, ...] = ("root", AGENT_USER),
           repo: str | None = None) -> tuple[bool, str]:
    """Inside the locked container, as each of `users`, try what an agent must not be able to do.

    Passes only if every blocked destination fails and every model endpoint answers, and, for the agent
    user, if the task's inputs are read-only and `repo` (the repository it patches) is writable. A user
    that does not exist in the container (an image without the agent user) is skipped.
    """
    env = {
        "BLOCKED": " ".join(BLOCKED_URLS),
        "BLOCKED_ADDRESSES": " ".join(_host_addresses()),
        "ALLOWED": " ".join(urls),
        "REPO": repo or "",
    }
    passed, report = True, []
    for user in users:
        if user != "root" and exec_run(container_id, f"id -u {user}", verbose=False)[0] != 0:
            continue
        cmd = ["docker", "exec", "-u", user, "-w", "/"]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        result = subprocess.run(cmd + [container_id, "bash", "-c", _VERIFY], capture_output=True, text=True,
                                timeout=600, errors="replace")
        passed = passed and result.returncode == 0
        report.append(f"--- as {user} ---\n{(result.stdout + result.stderr).strip()}")
    return passed, "\n".join(report)


def lock_and_verify(container_id: str, urls: list[str], repo: str | None = None) -> dict:
    """lock() then verify(); raises SandboxError, with the report, unless the lock holds."""
    record = lock(container_id, urls)
    passed, report = verify(container_id, urls, repo=repo)
    record["verified"] = passed
    if not passed:
        raise SandboxError("the network lock did not hold:\n" + report)
    return record


def default_endpoints() -> list[str]:
    """What the lanes run against: the KIConnect proxy as containers see it, and the Anthropic API."""
    proxy = os.getenv("OPENHANDS_PROXY_URL") or os.getenv("KICONNECT_BASE_URL") or "http://host.docker.internal:8817/v1"
    return [proxy, os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"]


def check(image: str, urls: list[str]) -> bool:
    """Preflight: lock a scratch container exactly as an agent's is locked and verify it (no model call)."""
    container_id = start_container(image, container_name=f"netcheck-{uuid.uuid4().hex[:8]}", workdir="/",
                                   hardened=True)
    try:
        exec_run(container_id, "command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl)",
                 "Installing curl", verbose=False, workdir="/", timeout=600)
        exec_run(container_id, f"id -u {AGENT_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {AGENT_USER}",
                 verbose=False, workdir="/")
        record = lock(container_id, urls)
        passed, report = verify(container_id, urls)
        print("allowed:", ", ".join(f"{a['host']}:{a['port']} -> {' '.join(a['addresses'])}" for a in record["allowed"]))
        print(report)
        print("NETWORK CHECK PASS" if passed else "NETWORK CHECK FAIL")
        return passed
    except SandboxError as exc:
        print(f"NETWORK CHECK FAIL: {exc}")
        return False
    finally:
        cleanup_container(container_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="lock a scratch container and verify it")
    parser.add_argument("--image", default="ubuntu:24.04", help="image of the scratch container")
    parser.add_argument("--allow", action="append", default=[], help="endpoint URL to allow (repeatable; "
                        "default: the KIConnect proxy as containers see it, and the Anthropic API)")
    args = parser.parse_args()
    if not args.check:
        parser.error("nothing to do (use --check)")
    return 0 if check(args.image, args.allow or default_endpoints()) else 1


if __name__ == "__main__":
    sys.exit(main())
