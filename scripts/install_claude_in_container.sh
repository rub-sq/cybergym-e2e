#!/bin/bash
# Install Claude Code inside a task container (see container_agent_runner.py).
#
# The release is CLAUDE_CODE_VERSION, set by run_agent.py. The official native installer brings a
# single binary, so task images of any age work without a recent Node.js (the npm package of
# current releases demands Node 22). It installs into root's home; the binary is copied onto the
# system PATH for the unprivileged agent user. Authentication is the runner's job, and run_agent.py
# closes the container's network before the CLI starts (scripts/sandbox.py).

set -eux

apt-get update
apt-get install -y curl ca-certificates

# Retried: one dropped connection would otherwise cost the whole attempt. A bash loop instead of
# curl's --retry-all-errors, which older task images (curl < 7.71) do not know.
for i in 1 2 3 4 5; do
    ( set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION:?}" ) && break
    echo "claude installer failed (try $i/5); retrying in 10s" >&2
    sleep 10
done

install -m 755 "$(readlink -f "$HOME/.local/bin/claude")" /usr/local/bin/claude
claude --version
