#!/bin/bash
# Install Claude Code inside a task container so the claude lane can run
# in-container (see container_agent_runner.py) instead of on the host.
#
# Authentication is handled by the runner, not here: the host's subscription
# login (~/.claude) is streamed into the container per-run, or
# ANTHROPIC_API_KEY is injected when no subscription is logged in.

set -eux

apt-get update
apt-get install -y curl ca-certificates

curl -fsSL https://deb.nodesource.com/setup_20.x 2>/dev/null | bash - >/dev/null 2>&1 || true
apt-get install -y nodejs sudo >/dev/null 2>&1

npm install -g @anthropic-ai/claude-code@2.1.91

# The runner invokes the CLI as the unprivileged `agent` user (created by
# run_agent.install); make sure it exists so a fresh container without the
# claude-code host-lane user can still run the in-container agent.
useradd -m -s /bin/bash agent 2>/dev/null || true
mkdir -p /work /output
chown -R agent:agent /src /output /work 2>/dev/null || true

command -v claude
claude --version 2>&1 | head -1 || true
