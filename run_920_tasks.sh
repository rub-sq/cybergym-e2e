#!/bin/bash
# ============================================================================
# CyberGym-E2E: Download 920 tasks and run with dual KIConnect API keys
# ============================================================================
# Usage:
#   ./run_920_tasks.sh <KEY1> <KEY2>
#
# Arguments:
#   KEY1                        KIConnect API key for GPT-OSS batch (format: ID:SECRET)
#   KEY2                        KIConnect API key for Mistral batch (format: ID:SECRET)
#
# Environment Variables (from .env):
#   MISTRAL_MODEL_ID            Mistral model ID (default: ki.inferenz.nrw-mistralai-mistral-small-4-119b-2603)
#   HF_TOKEN                    HuggingFace token for downloading data
#   MAX_PARALLEL_PER_KEY        Tasks to run in parallel per key (default: 3)
#   TIMEOUT                     Task timeout in seconds (default: 3600)
#   MAX_ATTEMPTS                Max retry attempts (default: 2)
#   CHECKPOINT_COUNT            Number of checkpoints to split remaining
#                               work into (default: 10)
#
# NOTE on resume:
#   Before running, each model's OWN output dir is scanned and any task with
#   a genuine completed run (SUCCESS, or a real >=60s duration) is skipped.
#   Only what's left is re-run - safe to re-invoke after an interrupted run
#   without redoing already-finished work.
#
# NOTE on checkpoints:
#   The remaining tasks (per model) are split into CHECKPOINT_COUNT roughly
#   equal chunks. Each checkpoint runs BOTH models' chunk i concurrently
#   (same MAX_PARALLEL_PER_KEY-per-model procedure), waits for BOTH to fully
#   finish, then runs a Docker cleanup (stopped containers + cybergym/n132/
#   gcr.io-oss-fuzz-base images) before moving to chunk i+1. This gives a
#   real reset point every 1/CHECKPOINT_COUNT of the work instead of running
#   continuously for days with no boundary.
#
# NOTE on concurrency:
#   Within one checkpoint, both batches (GPT-OSS with KEY1, Mistral with
#   KEY2) run SIMULTANEOUSLY, each capped at MAX_PARALLEL_PER_KEY concurrent
#   tasks, so peak concurrency can reach 2 * MAX_PARALLEL_PER_KEY at once.
#   Tune MAX_PARALLEL_PER_KEY down if this trips API rate limits or strains
#   shared-machine resources.
#
# NOTE on credentials:
#   Passing API keys as CLI arguments means they are visible to anyone who
#   can run `ps aux` on this host, and they will land in shell history.
#   Prefer sourcing them from .env (KICONNECT_KEY1/KICONNECT_KEY2) instead
#   of the command line where possible. CLI args are still supported here
#   for backwards compatibility but a warning is printed.
# ============================================================================

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data/projects"
SCRIPTS_DIR="${SCRIPT_DIR}/scripts"
LOG_DIR="${SCRIPT_DIR}/parallel_logs_920"

# Activate venv if it exists
VENV_DIR="${SCRIPT_DIR}/.venv_runner"
if [[ -f "${VENV_DIR}/bin/activate" ]]; then
    source "${VENV_DIR}/bin/activate"
fi

# Load .env if exists
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
fi

# Allow keys to come from .env if not passed on the CLI
KICONNECT_KEY1="${1:-${KICONNECT_KEY1:-}}"
KICONNECT_KEY2="${2:-${KICONNECT_KEY2:-}}"

if [[ $# -ge 2 ]]; then
    echo "WARNING: API keys were passed as command-line arguments. This exposes" >&2
    echo "them via 'ps aux' and shell history. Prefer setting KICONNECT_KEY1 /" >&2
    echo "KICONNECT_KEY2 in .env instead." >&2
fi

if [[ -z "$KICONNECT_KEY1" ]] || [[ -z "$KICONNECT_KEY2" ]]; then
    echo "ERROR: Missing required API keys"
    echo ""
    echo "Usage: $0 <KEY1> <KEY2>"
    echo ""
    echo "Arguments:"
    echo "  KEY1  KIConnect API key for GPT-OSS batch (format: ID:SECRET)"
    echo "  KEY2  KIConnect API key for Mistral batch (format: ID:SECRET)"
    echo ""
    echo "Keys may also be provided via KICONNECT_KEY1 / KICONNECT_KEY2 in .env"
    exit 1
fi

# Environment defaults
MISTRAL_MODEL_ID="${MISTRAL_MODEL_ID:-ki.inferenz.nrw-mistralai-mistral-small-4-119b-2603}"
HF_TOKEN="${HF_TOKEN:-}"
MAX_PARALLEL_PER_KEY="${MAX_PARALLEL_PER_KEY:-3}"
TIMEOUT="${TIMEOUT:-3600}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-2}"
CHECKPOINT_COUNT="${CHECKPOINT_COUNT:-10}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Track background batch PIDs so we can clean up on interrupt
declare -a BATCH_PIDS=()

# ============================================================================
# Helper Functions
# ============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

cleanup_on_interrupt() {
    log_warning "Interrupted - terminating background batches and their children..."
    for pid in "${BATCH_PIDS[@]:-}"; do
        [[ -z "$pid" ]] && continue
        # Kill the whole process group so orphaned run_agent.py children die too
        kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    exit 130
}
trap cleanup_on_interrupt INT TERM

check_dependencies() {
    log_info "Checking dependencies..."

    local missing=0

    if ! command -v python3 &> /dev/null; then
        log_error "python3 not found"
        ((missing++))
    fi

    if ! command -v jq &> /dev/null; then
        log_error "jq not found"
        ((missing++))
    fi

    if ! command -v hf &> /dev/null; then
        log_warning "huggingface-cli (hf) not found - checking for huggingface_hub python package instead"
        if ! python3 -c "import huggingface_hub" &> /dev/null; then
            log_error "huggingface_hub python package not found either (pip install huggingface_hub)"
            ((missing++))
        fi
    fi

    if [[ ! -x "${SCRIPTS_DIR}/run_agent.py" ]] && [[ ! -f "${SCRIPTS_DIR}/run_agent.py" ]]; then
        log_error "run_agent.py not found at ${SCRIPTS_DIR}/run_agent.py"
        ((missing++))
    fi

    if [[ $missing -gt 0 ]]; then
        log_error "Missing $missing dependencies"
        exit 1
    fi

    log_success "All dependencies found"
}

validate_api_keys() {
    log_info "Validating API keys..."

    if [[ -z "$KICONNECT_KEY1" ]] || [[ ! "$KICONNECT_KEY1" =~ : ]]; then
        log_error "Invalid KICONNECT_KEY1 format. Expected: ID:SECRET"
        exit 1
    fi

    if [[ -z "$KICONNECT_KEY2" ]] || [[ ! "$KICONNECT_KEY2" =~ : ]]; then
        log_error "Invalid KICONNECT_KEY2 format. Expected: ID:SECRET"
        exit 1
    fi

    log_success "API keys configured"
    log_info "Key1: ${KICONNECT_KEY1%%:*}:***"
    log_info "Key2: ${KICONNECT_KEY2%%:*}:***"
}

validate_numeric_config() {
    local name val
    for name in MAX_PARALLEL_PER_KEY TIMEOUT MAX_ATTEMPTS CHECKPOINT_COUNT; do
        val="${!name}"
        if ! [[ "$val" =~ ^[0-9]+$ ]]; then
            log_error "$name must be a positive integer, got: '$val'"
            exit 1
        fi
    done
    if [[ "$MAX_PARALLEL_PER_KEY" -lt 1 ]]; then
        log_error "MAX_PARALLEL_PER_KEY must be >= 1"
        exit 1
    fi
    if [[ "$CHECKPOINT_COUNT" -lt 1 ]]; then
        log_error "CHECKPOINT_COUNT must be >= 1"
        exit 1
    fi
}

download_tasks() {
    log_info "Downloading 920 tasks from HuggingFace..."

    if [[ -z "$HF_TOKEN" ]]; then
        log_warning "HF_TOKEN not set - download may fail"
        log_info "Set HF_TOKEN to download data"
    fi

    if [[ ! -d "$DATA_DIR" ]]; then
        log_info "Data directory not found, creating: $DATA_DIR"
        mkdir -p "$DATA_DIR"
    fi

    # Check if data already exists AND looks complete (marker file written
    # only after a fully successful snapshot_download call below).
    local complete_marker="${DATA_DIR}/.download_complete"
    if [[ -f "$complete_marker" ]] && \
       [[ $(find "$DATA_DIR" -mindepth 1 -type d | wc -l) -gt 100 ]]; then
        log_success "Data already downloaded ($(find "$DATA_DIR" -mindepth 1 -type d | wc -l) projects found)"
        return 0
    fi

    if [[ -d "$DATA_DIR" ]] && [[ $(find "$DATA_DIR" -mindepth 1 -type d | wc -l) -gt 100 ]] && [[ ! -f "$complete_marker" ]]; then
        log_warning "Found an existing data directory without a completion marker - it may be from an interrupted download. Re-downloading to be safe."
    fi

    log_info "Downloading benchmark data..."
    if [[ -n "$HF_TOKEN" ]]; then
        # Download to SCRIPT_DIR/data (the dataset repo already contains its
        # own "projects/" folder at its root), so it lands at DATA_DIR
        # (SCRIPT_DIR/data/projects). Downloading directly into DATA_DIR
        # would double-nest it as data/projects/projects/...
        HF_TOKEN="$HF_TOKEN" HF_LOCAL_DIR="${SCRIPT_DIR}/data" python3 << 'EOF'
import os
from huggingface_hub import snapshot_download

hf_token = os.getenv("HF_TOKEN")
local_dir = os.getenv("HF_LOCAL_DIR")

print(f"Downloading to: {local_dir}")
snapshot_download(
    repo_id="sunblaze-ucb/cybergym-e2e",
    repo_type="dataset",
    local_dir=local_dir,
    token=hf_token if hf_token else None
)
print("Download complete!")
EOF
        touch "${complete_marker}"
    else
        log_error "HF_TOKEN required for download. Set it and try again."
        exit 1
    fi

    log_success "Data downloaded"
}

generate_task_list() {
    log_info "Generating task list..." >&2

    local task_list="${SCRIPT_DIR}/tasks_920.txt"

    # Find all task directories (e.g., binutils/arvo_61822, arrow/arvo_24101)
    find "$DATA_DIR" -maxdepth 2 -type d \( -name "arvo_*" -o -name "oss-fuzz_*" \) | \
        sed "s|$DATA_DIR/||" | \
        sed 's|/$||' | \
        grep -v '^$' | \
        sort | \
        uniq > "$task_list"

    local count
    count=$(wc -l < "$task_list")
    log_success "Generated task list with $count tasks: $task_list" >&2

    # Only the path goes to stdout, so callers using $(...) get a clean value.
    echo "$task_list"
}

prepare_tasks_for_both_models() {
    local task_list="$1"

    log_info "Preparing all tasks for both models..." >&2

    local total
    total=$(wc -l < "$task_list")

    log_success "Both models will process all $total tasks" >&2
    log_success "  GPT-OSS (Key1):  $total tasks" >&2
    log_success "  Mistral (Key2):  $total tasks" >&2

    echo "$task_list"
}

# Split $1 into $2 roughly-equal chunk files named ${3}_chunk_01, _02, ...
# (2-digit, zero-padded, 1-indexed). Pure awk arithmetic instead of GNU
# split's `-n l/N` (not portable - unsupported on BSD/macOS split, and this
# was verified locally rather than assumed). Always creates exactly
# num_chunks files, even ones that end up empty (fewer tasks than chunks),
# so callers never need to special-case a missing chunk file. Remainder
# lines are distributed across the leading chunks so every task is
# accounted for exactly once - verified against the input with a diff.
split_into_chunks() {
    local input_list="$1"
    local num_chunks="$2"
    local prefix="$3"

    rm -f "${prefix}_chunk_"*

    local total
    total=$(wc -l < "$input_list")

    for ((i = 1; i <= num_chunks; i++)); do
        : > "$(printf '%s_chunk_%02d' "$prefix" "$i")"
    done

    [[ "$total" -eq 0 ]] && return 0

    awk -v total="$total" -v n="$num_chunks" -v prefix="$prefix" '
        BEGIN {
            base = int(total / n)
            rem = total % n
            chunk = 1
            count = 0
            limit = base + (chunk <= rem ? 1 : 0)
        }
        {
            while (count >= limit && chunk < n) {
                chunk++
                count = 0
                limit = base + (chunk <= rem ? 1 : 0)
            }
            outfile = sprintf("%s_chunk_%02d", prefix, chunk)
            print $0 >> outfile
            count++
        }
    ' "$input_list"
}

# Broader cleanup run once per checkpoint (between chunks), when nothing of
# ours should still be running: remove stopped containers, then the same
# scoped image cleanup used per-task, but with a moment to actually catch up
# now that both batches' current chunk is fully done.
cleanup_docker_checkpoint() {
    log_info "Checkpoint cleanup: removing orphaned containers and idle images..."

    # This runs only at a checkpoint boundary (both batches already waited
    # on) or before checkpoint 1 - nothing of ours should legitimately still
    # be running at these points. So any container whose image matches our
    # own known prefixes is, by definition, an orphan: e.g. left running
    # because its parent `run_agent.py` process was killed (OOM, crash,
    # etc.) without ever issuing its own `docker stop`/`rm` - killing the
    # client does NOT stop the container itself, Docker manages it
    # independently. `docker container prune` alone only removes containers
    # already `exited`, so it never catches this case; force-removing any
    # match here (running or not) is what actually does.
    # The `|| true` is load-bearing: with `set -euo pipefail`, a grep that
    # matches nothing exits 1, pipefail propagates it, and the assignment's
    # non-zero status kills the whole script. That's the NORMAL healthy case
    # (no orphans), so without this the run would die at the first cleanup.
    local orphans
    orphans=$(docker ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null \
        | grep -E ' (cybergym/|n132/arvo|gcr\.io/oss-fuzz-base)' \
        | awk '{print $1}' || true)
    if [[ -n "$orphans" ]]; then
        local orphan_count
        orphan_count=$(echo "$orphans" | wc -l)
        log_warning "Found $orphan_count orphaned container(s) still referencing our images - force removing"
        echo "$orphans" | xargs -r docker rm -f > /dev/null 2>&1 || true
    fi

    docker container prune -f > /dev/null 2>&1 || true
    docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep -E '^(cybergym/|n132/arvo|gcr\.io/oss-fuzz-base)' \
        | xargs -r docker rmi > /dev/null 2>&1 || true

    local free_space
    free_space=$(df -h / | awk 'NR==2 {print $4}')
    log_success "Checkpoint cleanup done. Free disk: $free_space"
}

# Build a resume list for one batch: tasks from $1 that do NOT already have a
# genuine completed run under $2 (output_dir). "Genuine" means status==SUCCESS,
# or a real duration (>=60s) - short of that, the only existing record is an
# environmental crash (e.g. the network-outage run's ~32s instant failures, or
# the disk-full run's tasks that never even started), so it's re-run instead
# of skipped. This is evaluated per-batch since gpt-oss and mistral have
# different, independent completion states.
filter_resume_tasks() {
    local full_task_list="$1"
    local output_dir="$2"
    local resume_list="$3"

    python3 -c "
import json, os, sys

full_task_list = '$full_task_list'
output_dir = '$output_dir'
resume_list = '$resume_list'

def already_done(task):
    task_safe = task.replace('/', '_')
    task_dir = os.path.join(output_dir, task_safe)
    if not os.path.isdir(task_dir):
        return False
    runs = sorted(os.listdir(task_dir))
    if not runs:
        return False
    summary_path = os.path.join(task_dir, runs[-1], 'summary.json')
    if not os.path.exists(summary_path):
        return False
    try:
        with open(summary_path) as f:
            content = f.read()
        data, _ = json.JSONDecoder().raw_decode(content)
    except Exception:
        return False
    if str(data.get('status', '')).upper() == 'SUCCESS':
        return True
    return data.get('duration_seconds', 0) >= 60

with open(full_task_list) as f:
    tasks = [line.strip() for line in f if line.strip()]

skipped = 0
with open(resume_list, 'w') as out:
    for task in tasks:
        if already_done(task):
            skipped += 1
            continue
        out.write(task + '\n')

print(skipped)
"
}

run_batch_with_key() {
    local task_list="$1"
    local api_key="$2"
    local model_id="$3"
    local output_dir="$4"
    local batch_name="$5"
    local chunk_label="${6:-}"

    log_info "Starting batch: $batch_name${chunk_label:+ ($chunk_label)}"
    log_info "  Model: $model_id"
    log_info "  Tasks: $(wc -l < "$task_list")"
    log_info "  Output: $output_dir"

    local log_file="$LOG_DIR/${batch_name}.log"
    mkdir -p "$LOG_DIR"
    mkdir -p "$output_dir"
    echo "===== ${chunk_label:-run} starting: $(wc -l < "$task_list") tasks =====" >> "$log_file"

    # Each task's build_image (n132/arvo:*-fix, gcr.io/oss-fuzz-base/*) is a
    # multi-GB PER-PROJECT image reused across every task of that project -
    # that's what filled the disk last time (~150+ distinct projects x
    # several GB each), not per-task bloat. After each task we prune images
    # matching only our own known prefixes (cybergym/, n132/arvo, gcr.io/
    # oss-fuzz-base) - NEVER a blanket prune, this is a shared machine.
    # Plain `docker rmi` (no -f) silently no-ops on any image still
    # referenced by another concurrently-running task for the same project,
    # so this can't break a sibling task or touch anything unrelated
    # (colleagues' images are never in these prefixes).
    #
    # xargs -P invokes this via bash -c with an inlined command string
    # (relying only on exported plain variables, not an exported function -
    # exported bash functions rely on BASH_FUNC_* which many hardened bash
    # builds break, causing xargs to see "command not found" and abort all
    # further processing; plain variables don't have that problem).
    (
        export KICONNECT_API_KEY="$api_key"
        export SCRIPTS_DIR MAX_ATTEMPTS TIMEOUT
        export batch_model_id="$model_id"
        export batch_output_dir="$output_dir"

        cat "$task_list" | xargs -P "$MAX_PARALLEL_PER_KEY" -I {} bash -c '
            task="$1"
            python3 "$SCRIPTS_DIR/run_agent.py" "$task" \
                --agent openhands \
                --prompt-style no-test \
                --mode patch-only \
                --max-attempts "$MAX_ATTEMPTS" \
                --timeout "$TIMEOUT" \
                --model-provider kiconnect \
                --kiconnect-model-id "$batch_model_id" \
                --agent-output "$batch_output_dir"

            docker images --format "{{.Repository}}:{{.Tag}}" 2>/dev/null \
                | grep -E "^(cybergym/|n132/arvo|gcr\.io/oss-fuzz-base)" \
                | xargs -r docker rmi 2>/dev/null || true
        ' _ {} \
            || true

        echo "[$batch_name] ${chunk_label:-run} completed"
    ) < /dev/null >> "$log_file" 2>&1 &

    # Set a global instead of `echo $!` + command substitution. Capturing
    # this function's output via `pid1=$(run_batch_with_key ...)` would run
    # the WHOLE function in its own subshell (that's what command
    # substitution does), making the backgrounded job above a GRANDCHILD of
    # the main script instead of a direct child. `wait` can only wait on
    # direct children - on anything else it fails INSTANTLY with "not a
    # child of this shell" instead of actually waiting. That's exactly what
    # was happening: every run "failed" in under a second because wait was
    # erroring out immediately, not because the task failed - the real task
    # (which takes 5+ minutes) was still running, orphaned, when we checked
    # the log seconds later and found it empty.
    LAST_BATCH_PID=$!
}

wait_for_batch() {
    local pid="$1"
    local batch_name="$2"

    if wait "$pid" 2>/dev/null; then
        log_success "$batch_name completed (PID $pid)"
        return 0
    else
        log_error "$batch_name failed (PID $pid) - see logs for details"
        return 1
    fi
}

# ============================================================================
# Main
# ============================================================================

main() {
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║  CyberGym-E2E: 920 Tasks - Dual KIConnect API Keys            ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
    echo ""

    check_dependencies
    validate_api_keys
    validate_numeric_config
    echo ""

    log_info "Configuration:"
    log_info "  HF_TOKEN: ${HF_TOKEN:+configured}"
    log_info "  Max parallel per key: $MAX_PARALLEL_PER_KEY"
    log_info "  Timeout per task: ${TIMEOUT}s"
    log_info "  Max attempts: $MAX_ATTEMPTS"
    log_info "  GPT-OSS model: openai-gpt-oss-120b"
    log_info "  Mistral model: $MISTRAL_MODEL_ID"
    log_info "  Data dir: $DATA_DIR"
    log_info "  Logs dir: $LOG_DIR"
    echo ""

    # Download tasks
    download_tasks
    echo ""

    # Generate task list
    task_list=$(generate_task_list)
    echo ""

    # Prepare tasks for both models (all 920 tasks for each)
    prepare_tasks_for_both_models "$task_list" > /dev/null
    echo ""

    local overall_status=0

    # Build a separate resume list per batch: skip any task that already has
    # a genuine completed run (SUCCESS, or a real >=60s duration) in that
    # batch's own output dir. gpt-oss and mistral have independent
    # completion states, so this is NOT a shared list.
    local gpt_oss_output="agent_output_openhands_gpt_oss"
    local mistral_output="agent_output_openhands_mistral"
    local gpt_oss_resume="${SCRIPT_DIR}/tasks_gpt_oss_resume.txt"
    local mistral_resume="${SCRIPT_DIR}/tasks_mistral_resume.txt"

    log_info "Filtering already-completed tasks (resume mode)..."
    mkdir -p "$gpt_oss_output" "$mistral_output"

    local gpt_oss_skipped mistral_skipped
    gpt_oss_skipped=$(filter_resume_tasks "$task_list" "$gpt_oss_output" "$gpt_oss_resume")
    mistral_skipped=$(filter_resume_tasks "$task_list" "$mistral_output" "$mistral_resume")

    log_success "  GPT-OSS: skipping $gpt_oss_skipped already-completed, $(wc -l < "$gpt_oss_resume") to run"
    log_success "  Mistral: skipping $mistral_skipped already-completed, $(wc -l < "$mistral_resume") to run"
    echo ""

    # Split each batch's OWN resume list into CHECKPOINT_COUNT chunks. Each
    # checkpoint runs BOTH models' chunk i concurrently (same 3-parallel
    # procedure as before), waits for BOTH to fully finish, runs a Docker
    # cleanup while nothing is running, then moves to chunk i+1. This gives
    # the system a real reset point every 1/CHECKPOINT_COUNT of the work,
    # instead of 6 workers running continuously for days with no boundary -
    # which is what let Docker state and memory pressure snowball last time.
    local gpt_oss_prefix="${SCRIPT_DIR}/tasks_gpt_oss"
    local mistral_prefix="${SCRIPT_DIR}/tasks_mistral"

    log_info "Splitting resume lists into $CHECKPOINT_COUNT checkpoints each..."
    split_into_chunks "$gpt_oss_resume" "$CHECKPOINT_COUNT" "$gpt_oss_prefix"
    split_into_chunks "$mistral_resume" "$CHECKPOINT_COUNT" "$mistral_prefix"
    echo ""

    # Clean up BEFORE checkpoint 1 too, not just between checkpoints - if
    # disk is already tight when the script starts (e.g. resuming after a
    # previous run), checkpoint 1 could hit disk-full before ever reaching
    # its first post-checkpoint cleanup.
    log_info "Pre-run cleanup (before checkpoint 1)..."
    cleanup_docker_checkpoint
    echo ""

    for ((checkpoint = 1; checkpoint <= CHECKPOINT_COUNT; checkpoint++)); do
        local gpt_oss_chunk mistral_chunk
        gpt_oss_chunk=$(printf '%s_chunk_%02d' "$gpt_oss_prefix" "$checkpoint")
        mistral_chunk=$(printf '%s_chunk_%02d' "$mistral_prefix" "$checkpoint")

        local gpt_oss_count mistral_count
        gpt_oss_count=$(wc -l < "$gpt_oss_chunk")
        mistral_count=$(wc -l < "$mistral_chunk")

        if [[ "$gpt_oss_count" -eq 0 && "$mistral_count" -eq 0 ]]; then
            log_info "Checkpoint $checkpoint/$CHECKPOINT_COUNT: nothing to do for either model, skipping"
            continue
        fi

        log_info "═══════════════════════════════════════════════════════════════"
        log_info "CHECKPOINT $checkpoint/$CHECKPOINT_COUNT"
        log_info "  GPT-OSS: $gpt_oss_count tasks"
        log_info "  Mistral: $mistral_count tasks"
        log_info "═══════════════════════════════════════════════════════════════"

        local pid1="" pid2=""

        if [[ "$gpt_oss_count" -gt 0 ]]; then
            run_batch_with_key \
                "$gpt_oss_chunk" \
                "$KICONNECT_KEY1" \
                "openai-gpt-oss-120b" \
                "$gpt_oss_output" \
                "gpt-oss-batch" \
                "checkpoint $checkpoint/$CHECKPOINT_COUNT"
            pid1=$LAST_BATCH_PID
            BATCH_PIDS+=("$pid1")
            log_info "GPT-OSS checkpoint $checkpoint started, PID: $pid1"
        else
            log_info "GPT-OSS: nothing left in this checkpoint, skipping"
        fi

        if [[ "$mistral_count" -gt 0 ]]; then
            run_batch_with_key \
                "$mistral_chunk" \
                "$KICONNECT_KEY2" \
                "$MISTRAL_MODEL_ID" \
                "$mistral_output" \
                "mistral-batch" \
                "checkpoint $checkpoint/$CHECKPOINT_COUNT"
            pid2=$LAST_BATCH_PID
            BATCH_PIDS+=("$pid2")
            log_info "Mistral checkpoint $checkpoint started, PID: $pid2"
        else
            log_info "Mistral: nothing left in this checkpoint, skipping"
        fi

        log_info "Waiting for checkpoint $checkpoint/$CHECKPOINT_COUNT to finish (both models)..."

        [[ -n "$pid1" ]] && { wait_for_batch "$pid1" "GPT-OSS checkpoint $checkpoint" || overall_status=1; }
        [[ -n "$pid2" ]] && { wait_for_batch "$pid2" "Mistral checkpoint $checkpoint" || overall_status=1; }

        cleanup_docker_checkpoint
        echo ""
    done

    # Summary
    if [[ $overall_status -eq 0 ]]; then
        log_success "═══════════════════════════════════════════════════════════════"
        log_success "ALL CHECKPOINTS COMPLETED"
        log_success "═══════════════════════════════════════════════════════════════"
    else
        log_error "═══════════════════════════════════════════════════════════════"
        log_error "ONE OR MORE CHECKPOINTS REPORTED FAILURES - CHECK LOGS"
        log_error "═══════════════════════════════════════════════════════════════"
    fi
    log_info "Results:"
    log_info "  GPT-OSS:  $gpt_oss_output/"
    log_info "  Mistral:  $mistral_output/"
    log_info "Logs: $LOG_DIR/"

    exit $overall_status
}

# Run main function
main "$@"
