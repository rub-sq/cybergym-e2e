#!/bin/bash
# Decide whether the cyber-safeguard flagging is driven by prompt CONTENT or by
# ACCOUNT state, by replaying prompts whose past outcome we already know.
cd "$(dirname "${BASH_SOURCE[0]}")"
M="${CLAUDE_MODEL:-claude-sonnet-5}"
run() { claude -p "$1" --model "$M" --permission-mode bypassPermissions \
        --output-format text 2>&1 | head -4; }

echo "═══ A. baseline: is the account working at all ═══"
run "Reply with exactly: ok"

echo ""
echo "═══ B. a prompt that SUCCEEDED before 2026-09-11 03:00 ═══"
B=$(python3 - <<'PY'
import json,glob,os,re
best=None
for s in glob.glob("agent_output_claude_host/*/*/summary.json"):
    run=os.path.basename(os.path.dirname(s))
    m=re.match(r"(\d{8}_\d{6})",run)
    if not m or m.group(1) >= "20260911_030000": continue
    try: d=json.JSONDecoder().raw_decode(open(s).read())[0]
    except Exception: continue
    if str(d.get("status","")).upper()!="SUCCESS": continue
    p=glob.glob(os.path.join(os.path.dirname(s),"workspace_attempt_*","host_workspace","prompt.txt"))
    if p: best=p[0]; print(p[0]); break
PY
)
if [ -n "$B" ]; then echo "   replaying: $B"; run "$(cat "$B")"; else echo "   (none found)"; fi

echo ""
echo "═══ C. a prompt that was FLAGGED after the cutover ═══"
C=$(python3 - <<'PY'
import glob,os
for t in sorted(glob.glob("agent_output_claude_host/*/*/trajectory/attempt_1.log")):
    if "safeguards flagged" in open(t,errors="replace").read(3000):
        p=glob.glob(os.path.join(os.path.dirname(os.path.dirname(t)),
                    "workspace_attempt_*","host_workspace","prompt.txt"))
        if p: print(p[0]); break
PY
)
if [ -n "$C" ]; then echo "   replaying: $C"; run "$(cat "$C")"; else echo "   (none found)"; fi

echo ""
echo "═══ D. same task, defanged wording ═══"
run "Read the C source file at /etc/hostname (just read it) and reply with exactly: read-ok"
