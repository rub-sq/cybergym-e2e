#!/bin/bash
# Install the opencode CLI inside a task container (see container_agent_runner.py).
#
# The release is OPENCODE_VERSION, set by run_agent.py. OpenCode 2 has its own installer
# (opencode.ai/v2/install); the old opencode.ai/install only brings 1.x. The binary lands in root's
# home and is copied onto the system PATH for the unprivileged agent user. ripgrep is installed up
# front because opencode's grep and glob tools run it and would otherwise download it on first use,
# which the closed network forbids (scripts/sandbox.py).

set -eux

apt-get update
apt-get install -y curl git ca-certificates
apt-get install -y ripgrep || {
    # Older task images have no ripgrep package: take the static release.
    curl -fsSL https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz \
        | tar -xz -C /tmp
    install -m 755 /tmp/ripgrep-14.1.1-x86_64-unknown-linux-musl/rg /usr/local/bin/rg
}

# Retried: a single dropped connection to opencode.ai (curl 35) cost two attempts once. pipefail in
# the subshell so a failed download is not masked by bash exiting 0 on empty input; a bash loop
# instead of curl's --retry-all-errors, which older task images (curl < 7.71) do not know.
for i in 1 2 3 4 5; do
    ( set -o pipefail; curl -fsSL https://opencode.ai/v2/install | VERSION="${OPENCODE_VERSION:?}" bash ) && break
    echo "opencode installer failed (try $i/5); retrying in 10s" >&2
    sleep 10
done

install -m 755 "$HOME/.opencode/bin/opencode" /usr/local/bin/opencode
opencode --version
rg --version | head -1
