#!/bin/bash
# Launch the long-running multi-agent benchmark.
#
#   lanes   OpenHands   -> KIConnect, through a key-rotating proxy
#           Claude Code -> Claude Code CLI (subscription auth)
#           opencode    -> opencode CLI, through the KIConnect proxy, or through
#                          a proxy of its own in front of OpenRouter
#                          (OPENCODE_PROVIDER=openrouter)
#
# Every agent CLI runs INSIDE its task container (anti-cheat: the agent can
# never see the benchmark repo on the host), and the container is locked to
# its model endpoint before the agent starts (scripts/sandbox.py): no GitHub,
# no package registry, no DNS. The only host-side state is the proxies and,
# for claude, the subscription login: CLAUDE_CODE_OAUTH_TOKEN
# (`claude setup-token`), else the host's own login (the credentials file
# on Linux, the Keychain on macOS), else ANTHROPIC_API_KEY.
#
# Every lane runs one task at a time and blocks independently: OpenHands or
# opencode waiting on quota does not stall Claude, and Claude waiting out a
# 5-hour or weekly limit does not stall the rest. Disk is the only shared
# resource, and the orchestrator drains + cleans whenever free space drops
# below the floor.
#
# Usage:
#   ./run_dual_agents.sh                          openhands and claude
#   LANES=claude ./run_dual_agents.sh             one lane only
#   LANES=claude,opencode ./run_dual_agents.sh    any comma-separated set of
#                                                 openhands, claude, opencode
#   (the older names both, openhands-opencode still work)
#
# Config via environment or .env:
#   KICONNECT_KEY1 / KICONNECT_KEY2   keys pooled by the KIConnect proxy
#   OPENHANDS_MODEL                   KIConnect model id (see below)
#   CLAUDE_MODEL                      default: sonnet
#   CLAUDE_OUTPUT                     default: agent_output_claude_host
#   OPENCODE_MODELS                   comma-separated model ids for opencode
#                                     lanes (default: qwen-qwen3-8-27b)
#   OPENCODE_OUTPUT                   output dir (one opencode model only)
#   OPENCODE_PROVIDER                 kiconnect (default) or openrouter
#   OPENROUTER_API_KEY                the key, with OPENCODE_PROVIDER=openrouter
#   OPENROUTER_PROVIDER_ORDER         comma-separated OpenRouter endpoints that
#                                     alone may serve opencode's requests
#   OPENCODE_PROXY_PORT               default: 8818 (OpenRouter proxy)
#   OPENCODE_PROXY_URL                container-side URL of that proxy (default:
#                                     OPENHANDS_PROXY_URL with its port, else
#                                     host.docker.internal)
#   MIN_FREE_GB                       default: 50
#   TASKS                             default: tasks_920.txt
#
# Discover KIConnect model ids with:
#   curl -s https://chat.kiconnect.nrw/api/v1/models \
#     -H "Authorization: Bearer $KICONNECT_KEY1" | python3 -m json.tool | grep '"id"'

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# .env supplies defaults: a variable the caller already set keeps its value.
if [[ -f .env ]]; then
    while IFS= read -r _line || [[ -n "$_line" ]]; do
        [[ "$_line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
        _name="${BASH_REMATCH[2]}"
        [[ -n "${!_name+x}" ]] && continue
        eval "$_line"
        export "$_name"
    done < .env
fi
[[ -f .venv_runner/bin/activate ]] && source .venv_runner/bin/activate

# A system-wide http_proxy must never intercept the loopback proxy hop;
# export it for the orchestrator and every agent it spawns.
export no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost"

KICONNECT_KEY1="${1:-${KICONNECT_KEY1:-}}"
KICONNECT_KEY2="${2:-${KICONNECT_KEY2:-}}"
# The proxy rotates across every pooled key, but a model id is only valid on
# the account it belongs to (mistral's ki.inferenz.nrw-... id lives on KEY2).
# KICONNECT_KEY2_ONLY=1 pools KEY2 only, otherwise requests landing on KEY1
# get "model does not exist".
if [[ "${KICONNECT_KEY2_ONLY:-0}" == "1" ]]; then
    KICONNECT_KEY1="$KICONNECT_KEY2"; KICONNECT_KEY2=""
fi
# Comma-separated: one lane per model. KIConnect meters limits per model,
# so e.g. a slow codex lane and a fast nano lane do not block each other.
OPENHANDS_MODELS="${OPENHANDS_MODELS:-${OPENHANDS_MODEL:-openai-gpt-oss-120b}}"
CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
CLAUDE_OUTPUT="${CLAUDE_OUTPUT:-}"
OPENCODE_MODELS="${OPENCODE_MODELS:-qwen-qwen3-8-27b}"
OPENCODE_OUTPUT="${OPENCODE_OUTPUT:-}"
OPENCODE_PROVIDER="${OPENCODE_PROVIDER:-kiconnect}"
OPENROUTER_PROVIDER_ORDER="${OPENROUTER_PROVIDER_ORDER:-}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
TASKS="${TASKS:-tasks_920.txt}"
PROXY_PORT="${PROXY_PORT:-8817}"
OPENCODE_PROXY_PORT="${OPENCODE_PROXY_PORT:-8818}"
LOCK_SECONDS="${LOCK_SECONDS:-7200}"
LANES="${LANES:-both}"
LOG_DIR="${LOG_DIR:-parallel_logs_dual}"

mkdir -p "$LOG_DIR"

want_openhands=0; want_claude=0; want_opencode=0
case "$LANES" in
    both)               LANES="openhands,claude" ;;
    openhands-opencode) LANES="openhands,opencode" ;;
esac
IFS=',' read -ra _lanes <<< "$LANES"
for _lane in "${_lanes[@]}"; do
    case "$(echo "$_lane" | xargs)" in
        openhands) want_openhands=1 ;;
        claude)    want_claude=1 ;;
        opencode)  want_opencode=1 ;;
        *) echo "LANES must be a comma-separated set of openhands, claude, opencode (or both, openhands-opencode)"; exit 1 ;;
    esac
done
case "$OPENCODE_PROVIDER" in
    kiconnect|openrouter) ;;
    *) echo "OPENCODE_PROVIDER must be kiconnect or openrouter"; exit 1 ;;
esac

PROXY_PIDS=()
cleanup() {
    echo ""
    echo "shutting down..."
    for _pid in "${PROXY_PIDS[@]:-}"; do
        [[ -n "$_pid" ]] && kill "$_pid" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

# start_proxy NAME PORT LOG ARGS...: a proxy in the background, checked to answer.
start_proxy() {
    local name="$1" port="$2" logfile="$3"; shift 3
    echo "starting $name proxy on port $port ..."
    python3 scripts/kiconnect_proxy.py --port "$port" "$@" >> "$logfile" 2>&1 &
    local pid=$!
    PROXY_PIDS+=("$pid")
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ERROR: the $name proxy failed to start - see $logfile"; tail -20 "$logfile"; exit 1
    fi
    # curl may be absent on minimal servers, and a system http_proxy env var
    # would reroute even loopback requests; python3 + no_proxy covers both.
    local pool_get="import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:$port/_pool', timeout=5).read().decode(), end='')"
    local status
    if ! status=$(env no_proxy="127.0.0.1,localhost" NO_PROXY="127.0.0.1,localhost" \
        http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= python3 -c "$pool_get" 2>/dev/null); then
        echo "ERROR: the $name proxy is not answering on port $port"; exit 1
    fi
    echo "$name proxy up (pid $pid); pool: $(echo "$status" | tr -d '\n ')"
}

# The KIConnect proxy serves the OpenHands lanes, and opencode unless it has
# its own (OpenRouter) proxy.
want_proxy=0
[[ "$want_openhands" == "1" ]] && want_proxy=1
[[ "$want_opencode" == "1" && "$OPENCODE_PROVIDER" == "kiconnect" ]] && want_proxy=1
want_openrouter_proxy=0
[[ "$want_opencode" == "1" && "$OPENCODE_PROVIDER" == "openrouter" ]] && want_openrouter_proxy=1

CONTAINER_PROXY_URL="${OPENHANDS_PROXY_URL:-http://host.docker.internal:$PROXY_PORT/v1}"
if [[ -z "${OPENCODE_PROXY_URL:-}" ]]; then
    if [[ -n "${OPENHANDS_PROXY_URL:-}" ]]; then
        OPENCODE_PROXY_URL="${OPENHANDS_PROXY_URL/:$PROXY_PORT/:$OPENCODE_PROXY_PORT}"
    else
        OPENCODE_PROXY_URL="http://host.docker.internal:$OPENCODE_PROXY_PORT/v1"
    fi
fi

if [[ "$want_proxy" == "1" ]]; then
    if [[ -z "$KICONNECT_KEY1" ]]; then
        echo "ERROR: KIConnect lanes need KICONNECT_KEY1 (and ideally KICONNECT_KEY2)"
        echo "Pass them as arguments or set them in .env"
        exit 1
    fi
    PROXY_ARGS=(--lock-seconds "$LOCK_SECONDS"
                --state-file "$LOG_DIR/kiconnect_pool_state.json"
                --key "$KICONNECT_KEY1")
    [[ -n "$KICONNECT_KEY2" ]] && PROXY_ARGS+=(--key "$KICONNECT_KEY2")
    start_proxy KIConnect "$PROXY_PORT" "$LOG_DIR/proxy.log" "${PROXY_ARGS[@]}"
fi

if [[ "$want_openrouter_proxy" == "1" ]]; then
    if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
        echo "ERROR: OPENCODE_PROVIDER=openrouter needs OPENROUTER_API_KEY"; exit 1
    fi
    # The key goes through the environment, not the command line, so it stays out of `ps`.
    OR_ARGS=(--provider openrouter --lock-seconds "$LOCK_SECONDS" --max-concurrent 8
             --state-file "$LOG_DIR/openrouter_pool_state.json")
    [[ -n "$OPENROUTER_PROVIDER_ORDER" ]] && OR_ARGS+=(--provider-order "$OPENROUTER_PROVIDER_ORDER")
    IFS=',' read -ra _models <<< "$OPENCODE_MODELS"
    for _m in "${_models[@]}"; do
        OR_ARGS+=(--allow-model "$(echo "$_m" | xargs)")
    done
    start_proxy OpenRouter "$OPENCODE_PROXY_PORT" "$LOG_DIR/proxy_openrouter.log" "${OR_ARGS[@]}"
    # The proxy answers only once a whole reply is in, and waits out rate
    # limits first (up to 30 min): opencode must wait longer than that.
    export OPENCODE_CHUNK_TIMEOUT_MS="${OPENCODE_CHUNK_TIMEOUT_MS:-3000000}"
fi

ORCH_ARGS=(--tasks "$TASKS" --min-free-gb "$MIN_FREE_GB" --log-dir "$LOG_DIR")
if [[ "$want_openhands" == "1" ]]; then
    # Container-side view of the proxy. On Linux, host.docker.internal maps to
    # host-gateway, which may not be the interface the proxy is reachable on;
    # OPENHANDS_PROXY_URL overrides it (e.g. http://10.150.0.212:8817/v1 on
    # the tower, where host-gateway traffic is refused).
    ORCH_ARGS+=(--openhands)
    IFS=',' read -ra _models <<< "$OPENHANDS_MODELS"
    for _m in "${_models[@]}"; do
        ORCH_ARGS+=(--openhands-model "$(echo "$_m" | xargs)")
    done
fi
[[ "$want_proxy" == "1" ]] && ORCH_ARGS+=(--proxy-url "$CONTAINER_PROXY_URL")
# claude and opencode run INSIDE their task containers:
#  - claude logs in with CLAUDE_CODE_OAUTH_TOKEN, the host's own login
#    (~/.claude/.credentials.json on Linux, the Keychain on macOS; only
#    the login is copied in) or ANTHROPIC_API_KEY.
#  - opencode reaches its proxy through the container-side URL: the
#    KIConnect proxy's (--proxy-url above) or the OpenRouter proxy's.
if [[ "$want_claude" == "1" ]]; then
    ORCH_ARGS+=(--claude --claude-model "$CLAUDE_MODEL")
    [[ -n "$CLAUDE_OUTPUT" ]] && ORCH_ARGS+=(--claude-output "$CLAUDE_OUTPUT")
fi
if [[ "$want_opencode" == "1" ]]; then
    ORCH_ARGS+=(--opencode)
    IFS=',' read -ra _models <<< "$OPENCODE_MODELS"
    for _m in "${_models[@]}"; do
        ORCH_ARGS+=(--opencode-model "$(echo "$_m" | xargs)")
    done
    [[ -n "$OPENCODE_OUTPUT" ]] && ORCH_ARGS+=(--opencode-output "$OPENCODE_OUTPUT")
    [[ "$want_openrouter_proxy" == "1" ]] && ORCH_ARGS+=(--opencode-proxy-url "$OPENCODE_PROXY_URL")
fi

# Preflight: lock a scratch container exactly as every agent's container is
# locked, and verify that nothing but the lanes' model endpoints answers
# (scripts/sandbox.py). The bench does not start otherwise.
CHECK_ARGS=()
[[ "$want_proxy" == "1" ]] && CHECK_ARGS+=(--allow "$CONTAINER_PROXY_URL")
[[ "$want_openrouter_proxy" == "1" ]] && CHECK_ARGS+=(--allow "$OPENCODE_PROXY_URL")
[[ "$want_claude" == "1" ]] && CHECK_ARGS+=(--allow "${ANTHROPIC_BASE_URL:-https://api.anthropic.com}")
echo "network check (a scratch container, locked like an agent's) ..."
if ! python3 scripts/sandbox.py --check "${CHECK_ARGS[@]}" > "$LOG_DIR/network_check.log" 2>&1; then
    echo "ERROR: the network check failed - see $LOG_DIR/network_check.log"
    tail -20 "$LOG_DIR/network_check.log"
    exit 1
fi
echo "network check passed ($(grep -c '^ok:' "$LOG_DIR/network_check.log") checks)"

echo ""
python3 scripts/dual_agent_orchestrator.py "${ORCH_ARGS[@]}"
