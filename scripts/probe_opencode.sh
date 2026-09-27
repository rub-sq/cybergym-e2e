#!/bin/bash
# Smoke test for the opencode lane: verifies the EXACT invocation
# opencode_host_runner.py builds (per-run config, env, flags, cwd, proxy
# round-trip, model) on a throwaway git repo. ~2-4 minutes.
#
# Usage: bash scripts/probe_opencode.sh
# Reuses a proxy already running on $PROXY_PORT (e.g. an active bench);
# starts and stops its own otherwise.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f .env ]] && { set -a; source .env; set +a; }
[[ -f .venv_runner/bin/activate ]] && source .venv_runner/bin/activate

MODEL="${OPENCODE_MODEL:-qwen-qwen3-8-27b}"
PORT="${PROXY_PORT:-8817}"
WORK=$(mktemp -d /tmp/opencode_probe.XXXXXX)
ORIG_PWD=$PWD
PROXY_PID=""
cleanup() {
    [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null || true
    # An older probe bug ran opencode in the caller's cwd and made it
    # 'create' hello.py there. Sweep that garbage if it appeared.
    if [[ -n "${REPO:-}" && "$ORIG_PWD" != "$REPO" && -f "$ORIG_PWD/hello.py" ]]; then
        rm -f "$ORIG_PWD/hello.py"
        echo "removed stray $ORIG_PWD/hello.py left by a broken probe run"
    fi
}
trap cleanup EXIT

# A proxy left behind by a killed bench or earlier probe would silently
# serve this test with STALE code - kill our own leftovers first (never
# while a live orchestrator is running: that proxy belongs to the bench).
if pgrep -f dual_agent_orchestrator > /dev/null; then
    echo "WARNING: a bench orchestrator is running - its proxy on $PORT is in use; the test talks to THAT code, not the checked-out one."
else
    pkill -f "kiconnect_proxy.py --port $PORT" 2>/dev/null && sleep 1 || true
fi

if ! python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT/_pool',timeout=3)" 2>/dev/null; then
    echo "starting proxy on $PORT ..."
    python3 scripts/kiconnect_proxy.py --port "$PORT" --state-file "$WORK/pool.json" \
        --key "$KICONNECT_KEY1" ${KICONNECT_KEY2:+--key "$KICONNECT_KEY2"} > "$WORK/proxy.log" 2>&1 &
    PROXY_PID=$!
    sleep 2
else
    echo "WARNING: something else is already serving on $PORT - a manual proxy is fine, but the test below talks to THAT code, not the checked-out one."
fi

REPO="$WORK/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q .
git -C "$REPO" config user.email probe@localhost
git -C "$REPO" config user.name probe
echo "x = 1" > "$REPO/hello.py"
git -C "$REPO" add -A && git -C "$REPO" commit -qm baseline

# Per-run config, identical in shape to opencode_host_runner._write_config
cat > "$WORK/opencode.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "kiconnect/$MODEL",
  "autoupdate": false,
  "snapshot": false,
  "share": "disabled",
  "enabled_providers": ["kiconnect"],
  "tools": {"webfetch": false, "websearch": false},
  "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
  "provider": {
    "kiconnect": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "KI:connect - Inferenz NRW",
      "options": {
        "baseURL": "http://127.0.0.1:$PORT/v1",
        "apiKey": "{env:KICONNECT_API_KEY}"
      },
      "models": {
        "$MODEL": {"name": "$MODEL", "limit": {"context": 262144, "output": 131072}}
      }
    }
  }
}
EOF

# Exact env + command opencode_host_runner.build uses (cwd = the repo).
# Mirrors the runner's fallback: with the proxy, KICONNECT_API_KEY is a real
# pooled key (the client must present a key that LOOKS valid even though the
# proxy rewrites Authorization).
export OPENCODE_CONFIG="$WORK/opencode.json"
export KICONNECT_API_KEY="${KICONNECT_API_KEY:-${KICONNECT_KEY1:-proxy-managed}}"
export OPENCODE_DISABLE_AUTOUPDATE=1
export OPENCODE_DISABLE_LSP_DOWNLOAD=1
export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_DISABLE_CLAUDE_CODE=1
echo "opencode $(opencode --version 2>&1 | head -1) | kiconnect/$MODEL via 127.0.0.1:$PORT"
echo "workdir: $WORK"
# v2 has no --dir: the process CWD is the project. The runner uses
# Popen(cwd=repo_dir); this probe must do the same or the agent works in
# the wrong tree (observed: it 'created' hello.py in the caller's cwd).
( cd "$REPO" && timeout 600 opencode run --standalone --model "kiconnect/$MODEL" --auto --format default \
    "Edit hello.py so that the variable x equals 42. Do not create any other files." \
    > "$WORK/oc.log" 2>&1 )
RC=$?

echo "--- opencode exit: $RC"
tail -8 "$WORK/oc.log"
echo "--- git diff (expect x = 42):"
git -C "$REPO" diff
if git -C "$REPO" diff | grep -q "x = 42"; then
    echo "PROBE PASS: opencode + proxy + model working end-to-end"
    exit 0
fi
echo "PROBE FAIL: expected edit missing. Logs: $WORK/oc.log, $WORK/proxy.log"
exit 1
