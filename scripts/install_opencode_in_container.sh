#!/bin/bash
# Install the opencode CLI inside a task container so the opencode lane can
# run in-container (see container_agent_runner.py) instead of on the host.
#
# The in-container build is a single static binary (no npm), so there is no
# bun-runtime outbound-HTTPS quirk that forced the host lane onto loopback.

set -eux

apt-get update
apt-get install -y curl git ca-certificates

# Static single-file binary. The installer targets the calling user's home
# ($HOME/.opencode/bin), which here is /root; the agent runs as the
# unprivileged `agent` user, so expose the binary system-wide on PATH.
# Retry: a single dropped connection to opencode.ai (curl 35) cost two attempts
# in the 2026-10 mistral run. pipefail in the subshell so a failed download is
# not masked by bash exiting 0 on empty input; a bash loop instead of curl's
# --retry-all-errors, which older task images (curl < 7.71) do not know.
for i in 1 2 3 4 5; do
    ( set -o pipefail; curl -fsSL https://opencode.ai/install | bash ) && break
    echo "opencode installer failed (try $i/5); retrying in 10s" >&2
    sleep 10
done

OC_BIN="$(command -v opencode || true)"
if [[ -z "$OC_BIN" && -x "$HOME/.opencode/bin/opencode" ]]; then
    OC_BIN="$HOME/.opencode/bin/opencode"
fi
if [[ -z "$OC_BIN" ]]; then
    echo "ERROR: opencode binary not found after install" >&2
    exit 1
fi
cp -f "$OC_BIN" /usr/local/bin/opencode
chmod 755 /usr/local/bin/opencode

# The runner invokes the CLI as the unprivileged `agent` user (created by
# run_agent.install for other lanes); make sure it exists here too.
useradd -m -s /bin/bash agent 2>/dev/null || true
mkdir -p /work /output
chown -R agent:agent /src /output /work 2>/dev/null || true

# Verify it lands on PATH for the unprivileged user.
command -v opencode
opencode --version
