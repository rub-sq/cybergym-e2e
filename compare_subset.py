#!/usr/bin/env python3
"""Score every model over one common set of tasks.

Models that ran the full benchmark cannot be compared directly against a model
whose runs were partly blocked: the blocked tasks are not a random sample, so
the surviving ones are a different, likely easier, distribution. This restricts
every model to the tasks the reference model actually got to attempt.

  python3 compare_subset.py                      reference = claude_host
  python3 compare_subset.py --reference <dir>
"""
import json, glob, os, sys
from collections import Counter

REF = "agent_output_claude_host"
if "--reference" in sys.argv:
    REF = sys.argv[sys.argv.index("--reference") + 1]

SAFEGUARD = ("safeguards flagged this message", "Details: `[cyber]`")


def latest(task_dir):
    runs = sorted(p for p in glob.glob(os.path.join(task_dir, "*")) if os.path.isdir(p))
    for run in reversed(runs):
        s = os.path.join(run, "summary.json")
        if os.path.isfile(s):
            try:
                return run, json.JSONDecoder().raw_decode(open(s).read())[0]
            except Exception:
                continue
    return None, None


def blocked(run_dir):
    traj = os.path.join(run_dir, "trajectory")
    if not os.path.isdir(traj):
        return False
    for n in os.listdir(traj):
        if n.endswith(".log"):
            t = open(os.path.join(traj, n), errors="replace").read(3000)
            if any(m in t for m in SAFEGUARD):
                return True
    return False


def outcome(base, safe):
    """success / fail / None (never fairly evaluated)."""
    tdir = os.path.join(base, safe)
    if not os.path.isdir(tdir):
        return None
    run, data = latest(tdir)
    if not data:
        return None
    if str(data.get("status", "")).upper() == "SUCCESS":
        return "success"
    if run and blocked(run):
        return None
    if data.get("duration_seconds", 0) < 60:
        return None
    atts = data.get("attempts") or [{}]
    if all(a.get("agent_exec_seconds", 0) == 0 for a in atts):
        return None
    last = atts[-1]
    if any(str(last.get(s, "")) == "error" for s in ("stage1", "stage2", "stage3", "stage4")):
        return None
    return "fail"


# the comparison set: tasks the reference model fairly evaluated
subset = sorted(s for s in os.listdir(REF)
                if os.path.isdir(os.path.join(REF, s)) and outcome(REF, s) is not None)
print(f"reference       : {REF}")
print(f"comparison set  : {len(subset)} tasks\n")

dirs = sorted(d for d in glob.glob("agent_output_*") if os.path.isdir(d))
print(f"{'model':<26}{'success':>8}{'fail':>7}{'n/a':>6}{'rate':>9}")
print("-" * 56)
for base in dirs:
    c = Counter(outcome(base, s) for s in subset)
    ok, bad, na = c["success"], c["fail"], c[None]
    ev = ok + bad
    rate = f"{100*ok/ev:.1f}%" if ev else "n/a"
    print(f"{base.replace('agent_output_',''):<26}{ok:>8}{bad:>7}{na:>6}{rate:>9}")

print(f"\nproject spread of the comparison set (top 12):")
for proj, n in Counter(s.rsplit('_arvo_', 1)[0].rsplit('_oss-fuzz_', 1)[0]
                       for s in subset).most_common(12):
    print(f"  {n:>4}  {proj}")
