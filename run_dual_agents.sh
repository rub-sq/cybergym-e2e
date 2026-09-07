#!/bin/bash
# Launch the long-running dual-agent benchmark.
#
#   lane 1  OpenHands -> KIConnect, through a key-rotating proxy
#   lane 2  Claude Code -> the host's logged-in CLI (subscription auth)
#
# Both lanes run one task at a time and block independently: OpenHands waiting
# on KIConnect quota does not stall Claude, and Claude waiting out a 5-hour or
# weekly limit does not stall OpenHands. Disk is the only shared resource, and
# the orchestrator drains + cleans whenever free space drops below the floor.
#
# Usage:
#   ./run_dual_agents.sh                 both lanes
#   LANES=claude ./run_dual_agents.sh    one lane only
#
# Config via environment or .env:
#   KICONNECT_KEY1 / KICONNECT_KEY2   keys pooled by the proxy
#   OPENHANDS_MODEL                   KIConnect model id (see below)
#   CLAUDE_MODEL                      default: sonnet
#   MIN_FREE_GB                       default: 50
#   TASKS                             default: tasks_920.txt
#
# Discover KIConnect model ids with:
#   curl -s https://chat.kiconnect.nrw/api/v1/models \
#     -H "Authorization: Bearer $KICONNECT_KEY1" | python3 -m json.tool | grep '"id"'

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

[[ -f .env ]] && { set -a; source .env; set +a; }
[[ -f .venv_runner/bin/activate ]] && source .venv_runner/bin/activate

KICONNECT_KEY1="${1:-${KICONNECT_KEY1:-}}"
KICONNECT_KEY2="${2:-${KICONNECT_KEY2:-}}"
OPENHANDS_MODEL="${OPENHANDS_MODEL:-openai-gpt-oss-120b}"
CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
TASKS="${TASKS:-tasks_920.txt}"
PROXY_PORT="${PROXY_PORT:-8817}"
LOCK_SECONDS="${LOCK_SECONDS:-7200}"
LANES="${LANES:-both}"
LOG_DIR="${LOG_DIR:-parallel_logs_dual}"

mkdir -p "$LOG_DIR"

want_openhands=0; want_claude=0
case "$LANES" in
    both)      want_openhands=1; want_claude=1 ;;
    openhands) want_openhands=1 ;;
    claude)    want_claude=1 ;;
    *) echo "LANES must be both|openhands|claude"; exit 1 ;;
esac

PROXY_PID=""
cleanup() {
    echo ""
    echo "shutting down..."
    [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ "$want_openhands" == "1" ]]; then
    if [[ -z "$KICONNECT_KEY1" ]]; then
        echo "ERROR: OpenHands lane needs KICONNECT_KEY1 (and ideally KICONNECT_KEY2)"
        echo "Pass them as arguments or set them in .env"
        exit 1
    fi
    PROXY_ARGS=(--port "$PROXY_PORT" --lock-seconds "$LOCK_SECONDS"
                --state-file "$LOG_DIR/kiconnect_pool_state.json"
                --key "$KICONNECT_KEY1")
    [[ -n "$KICONNECT_KEY2" ]] && PROXY_ARGS+=(--key "$KICONNECT_KEY2")

    echo "starting KIConnect proxy on port $PROXY_PORT ..."
    python3 scripts/kiconnect_proxy.py "${PROXY_ARGS[@]}" >> "$LOG_DIR/proxy.log" 2>&1 &
    PROXY_PID=$!
    sleep 2
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        echo "ERROR: proxy failed to start - see $LOG_DIR/proxy.log"; tail -20 "$LOG_DIR/proxy.log"; exit 1
    fi
    if ! curl -sf "http://127.0.0.1:$PROXY_PORT/_pool" > /dev/null; then
        echo "ERROR: proxy is not answering on port $PROXY_PORT"; exit 1
    fi
    echo "proxy up (pid $PROXY_PID); pool: $(curl -s http://127.0.0.1:$PROXY_PORT/_pool | tr -d '\n ')"
fi

if [[ "$want_claude" == "1" ]]; then
    if ! command -v claude > /dev/null; then
        echo "ERROR: claude CLI not found on PATH"; exit 1
    fi
    echo "claude CLI: $(claude --version 2>&1 | head -1)"
fi

ORCH_ARGS=(--tasks "$TASKS" --min-free-gb "$MIN_FREE_GB" --log-dir "$LOG_DIR")
[[ "$want_openhands" == "1" ]] && ORCH_ARGS+=(--openhands
    --openhands-model "$OPENHANDS_MODEL"
    --proxy-url "http://host.docker.internal:$PROXY_PORT/v1")
[[ "$want_claude" == "1" ]] && ORCH_ARGS+=(--claude --claude-model "$CLAUDE_MODEL")

echo ""
python3 scripts/dual_agent_orchestrator.py "${ORCH_ARGS[@]}"
