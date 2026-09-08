#!/usr/bin/env python3
"""Split failed tasks into GENUINE (the agent had a fair run) vs ENVIRONMENTAL.

  python3 classify.py                      report on every agent_output_* dir
  python3 classify.py <dir> [<dir> ...]    report on specific dirs
  python3 classify.py --write              also write rerun_<label>.txt
  python3 classify.py --write --all-failed write EVERY non-success task

Environmental failures never reached the model in a usable state, so counting
them as failures understates the model. They are listed separately and can be
re-run after the underlying cause is fixed.
"""
import json, os, sys, glob

WRITE = "--write" in sys.argv
ALL_FAILED = "--all-failed" in sys.argv
MIN_AGENT_SECONDS = 30

dirs = [a for a in sys.argv[1:] if not a.startswith("--")]
if not dirs:
    dirs = sorted(d for d in glob.glob("agent_output_*") if os.path.isdir(d))

safe2task = {}
if os.path.exists("tasks_920.txt"):
    with open("tasks_920.txt") as f:
        for line in f:
            t = line.strip()
            if t:
                safe2task[t.replace("/", "_")] = t


def load(p):
    try:
        with open(p) as f:
            return json.JSONDecoder().raw_decode(f.read())[0]
    except Exception:
        return None


SAFEGUARD_MARKERS = ("safeguards flagged this message", "Details: `[cyber]`",
                     "Cyber Verification Program")


def safeguard_flagged(run_dir):
    """True when the CLI was stopped by Claude's real-time cyber safeguards.

    These never reach the model, are not quota, and do not recover on retry -
    the same task content is flagged every time. They belong in the results as
    an explicit exclusion, not as an agent failure.
    """
    traj = os.path.join(run_dir, "trajectory")
    if not os.path.isdir(traj):
        return False
    for name in os.listdir(traj):
        if not name.endswith(".log"):
            continue
        try:
            with open(os.path.join(traj, name), errors="replace") as fh:
                text = fh.read(4000)
        except OSError:
            continue
        if any(marker in text for marker in SAFEGUARD_MARKERS):
            return True
    return False


def classify(s):
    atts = s.get("attempts") or [{}]
    last = atts[-1]
    if s.get("duration_seconds", 0) < 60:
        return "instant_death(<60s)"
    if str(s.get("status", "")).lower() == "error":
        return "status_error"
    if all(a.get("agent_exec_seconds", 0) == 0 for a in atts):
        return "agent_never_ran"
    if all(0 <= a.get("agent_exec_seconds", 0) < MIN_AGENT_SECONDS for a in atts):
        return f"agent_died_early(<{MIN_AGENT_SECONDS}s)"
    for st in ("stage1", "stage2", "stage3", "stage4"):
        if str(last.get(st, "")) == "error":
            return f"harness_error({st})"
    return None


for base in dirs:
    if not os.path.isdir(base):
        print(f"=== {base} === (missing)")
        continue
    label = base.replace("agent_output_", "")
    genuine, env, success, reasons = [], [], 0, {}
    models = set()

    for safe in sorted(os.listdir(base)):
        tdir = os.path.join(base, safe)
        if not os.path.isdir(tdir):
            continue
        runs = sorted(os.listdir(tdir))
        summaries = [x for x in (load(os.path.join(tdir, r, "summary.json")) for r in runs) if x]
        task = safe2task.get(safe) or (summaries[0].get("task") if summaries else safe)
        for s in summaries:
            if s.get("model"):
                models.add(s["model"])

        if any(str(s.get("status", "")).upper() == "SUCCESS" for s in summaries):
            success += 1
            continue
        if not summaries:
            env.append(task); reasons["killed_no_summary"] = reasons.get("killed_no_summary", 0) + 1
            continue
        latest_run = os.path.join(tdir, runs[-1]) if runs else None
        if latest_run and safeguard_flagged(latest_run):
            why = "safeguard_flagged(cyber)"
        else:
            why = classify(summaries[-1])
        if why:
            env.append(task); reasons[why] = reasons.get(why, 0) + 1
        else:
            genuine.append(task)

    evaluated = success + len(genuine)
    rate = f"{100.0 * success / evaluated:.1f}%" if evaluated else "n/a"
    print(f"=== {label} ===")
    if models:
        print(f"  model(s)             : {', '.join(sorted(models))}")
    print(f"  success              : {success}")
    print(f"  genuine failures     : {len(genuine)}")
    print(f"  success rate         : {rate}  ({success}/{evaluated} evaluated)")
    print(f"  environmental (rerun): {len(env)}")
    for k, v in sorted(reasons.items()):
        print(f"      {k:<26} {v}")
    if WRITE:
        out_list = sorted(env + genuine) if ALL_FAILED else sorted(env)
        out = f"rerun_{label}.txt"
        with open(out, "w") as f:
            f.write("\n".join(out_list) + ("\n" if out_list else ""))
        print(f"  -> wrote {out} ({len(out_list)} tasks)")
    print()
