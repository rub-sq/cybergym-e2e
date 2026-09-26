#!/bin/bash
# Pre-download everything a task file needs, idempotently:
#   1. task data (src.tgz / poc.bin / crash.log) from HuggingFace
#   2. build images from the registries
# Safe to re-run: anything already present is skipped.
# Usage: bash scripts/prep_tasks.sh [tasks_file]     (default: tasks_20.txt)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
TASKS_FILE="${1:-tasks_20.txt}"
[[ -f .env ]] && { set -a; source .env; set +a; }

if [[ -f .venv_runner/bin/python ]]; then PY=".venv_runner/bin/python"; else PY=python3; fi
"$PY" -c "import huggingface_hub, tomli" 2>/dev/null || "$PY" -m pip install -q huggingface_hub tomli

echo "=== [1/2] task data ==="
TASKS_FILE="$TASKS_FILE" HF_TOKEN="${HF_TOKEN:-}" "$PY" - <<'PYEOF'
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

tasks = [l.strip() for l in Path(os.environ["TASKS_FILE"]).read_text().splitlines() if l.strip()]
ok = skipped = failed = 0
for t in tasks:
    for f in ("src.tgz", "poc.bin", "crash.log"):
        local = Path("data/projects") / t / f
        if local.exists() and local.stat().st_size > 0:
            skipped += 1
            continue
        try:
            hf_hub_download(
                "sunblaze-ucb/cybergym-e2e", repo_type="dataset",
                filename=f"projects/{t}/{f}", local_dir="data",
                token=os.environ.get("HF_TOKEN") or None)
            ok += 1
            print(f"got {t}/{f}", flush=True)
        except Exception as e:
            failed += 1
            print(f"FAIL {t}/{f}: {e}", flush=True)
print(f"data: {ok} downloaded, {skipped} present, {failed} failed", flush=True)
if failed:
    raise SystemExit(1)
PYEOF

echo "=== [2/2] build images ==="
TASKS_FILE="$TASKS_FILE" "$PY" - <<'PYEOF'
import os, subprocess
from pathlib import Path
import tomli

tasks = [l.strip() for l in Path(os.environ["TASKS_FILE"]).read_text().splitlines() if l.strip()]
imgs = []
for t in tasks:
    proj, tag = t.split("/")
    pcfg = tomli.loads(Path(f"projects/{proj}/project.toml").read_text())
    tp = Path(f"projects/{proj}/{tag}/config.toml")
    tcfg = tomli.loads(tp.read_text()) if tp.exists() else {}
    img = tcfg.get("build_image") or pcfg.get("build_image")
    if img and img not in imgs:
        imgs.append(img)

out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                     capture_output=True, text=True).stdout.split()
present = set(out)

def have(img):
    if img in present:
        return True
    if "@" in img:  # digest-pinned: the local copy may show as <none>:<none>
        repo = img.split("@")[0]
        return any(p == repo or p.startswith(repo + ":") for p in present)
    return False

todo = [i for i in imgs if not have(i)]
print(f"{len(imgs)} unique images, {len(todo)} to pull", flush=True)
for img in todo:
    print(f"pulling {img} ...", flush=True)
    r = subprocess.run(["docker", "pull", img])
    if r.returncode != 0:
        raise SystemExit(f"pull failed for {img}")
print("images: all present", flush=True)
PYEOF

echo "prep complete"
