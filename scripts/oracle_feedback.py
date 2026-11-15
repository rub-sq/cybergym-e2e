"""What a failed validation tells the agent in patch-only mode: the same information agentic-redblue's patch oracle
gives its Blue Team (src/oracle/patch_oracle.py there), with the same extraction rules and limits.

Upstream told the agent only `TEST VALIDATION: PASSED/FAILED/ERROR`. A patch that passed the tests but left the crash
in place read as PASSED, and a patch that did not compile read as ERROR without the compiler's message, so a second
attempt had nothing to work from. The verdict follows the oracle's order (apply, build, PoC, tests) and reports the
first failure: the patch did not apply, the build failed (its error lines), the PoC still crashes or hangs (the end of
its output), or the suite failed (the lines that name the failure, then the end of its output). The PoC is the one the
agent already has at /src/poc.bin, so the verdict reveals nothing beyond what the task hands the agent.
"""

from __future__ import annotations

import re

FAILURE_DETAIL_CHARS = 4000

# A build log is mostly warnings: hand the agent the error lines and the failed make target.
_ERROR_LINE_RE = re.compile(
    r".*:\s*error:|error:.*"
    r"|\bundefined reference\b|\bmultiple definition of\b"
    r"|\*\*\* \[.*Error\b"
    r"|\bld: error\b|\bclang\+\+: error\b|\bcollect2: error\b"
    r"|\bno such file or directory\b|\buse of undeclared identifier\b"
    r"|\bcannot find -l\w+",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")
# A test-output line naming what failed, across harnesses (meson, ctest, make check, pytest, ...).
_TEST_FAILURE_LINE_RE = re.compile(
    r"\bFAIL(?:ED|URE|S)?\b|\bERROR\b|\berror:|\bfailed\b|Assertion|assert(?:ion)? failed|\bAbort(?:ed)?\b"
    r"|\bpanic(?:ked)?\b|Traceback|^\s*\w*(?:Error|Exception): |Segmentation fault|core dumped|\bTIMEOUT\b"
    r"|timed out|[Tt]est timeout|exit status [1-9]|\b(?:Fail|FAIL|Failures?|Errors?|failures?|errors?)\s*:\s*[1-9]"
)
# Build and configure lines inside a suite's log: progress counters, warnings, meson and CMake chatter.
_BUILD_NOISE_RE = re.compile(
    r"^\s*\[\d+/\d+\]|warning:|^\s*[\w.+-]+\| |^\s*-- |^make(?:\[\d+\])?: (?:Entering|Leaving)"
    r"|^\s*(?:Compiling|Linking|Building|Configuring|Generating|Installing|Scanning) "
)
# validate.py's headers in front of a captured output; the agent gets the output itself.
_HEADER_RE = re.compile(r"^(?:Compile failed:|Tests failed:|Ground truth PoC still crashes - found different bug:)\n")


def _tail(text: str | None) -> str:
    return (text or "")[-FAILURE_DETAIL_CHARS:]


def test_failure_summary(output: str | None) -> str:
    """The lines naming the failure, then the end of the output, without shell trace, terminal escapes and
    carriage returns."""
    if not output:
        return ""
    lines = [_ANSI_RE.sub("", line).rstrip("\r").rstrip() for line in output.replace("\r\n", "\n").split("\n")]
    body = [line for line in lines
            if line.strip() and not line.startswith("+") and line.strip() not in ("STDOUT:", "STDERR:")]
    hits: list[str] = []
    for line in body:
        text = line.strip()
        if _TEST_FAILURE_LINE_RE.search(line) and not _BUILD_NOISE_RE.search(line) and text[:240] not in hits:
            hits.append(text[:240])
    parts: list[str] = []
    if hits:
        # the last matches: harnesses print failures and the summary at the end
        parts.append(("TEST FAILURE (the lines that name what failed):\n" + "\n".join(hits[-20:]))[-2400:])
    tail = "\n".join(line[:240] for line in body[-30:])
    parts.append("End of the suite's output (shell trace removed):\n" + tail[-1500:])
    return "\n\n".join(parts)


def build_failure_summary(output: str | None) -> str:
    """The error lines (and the failed make target), deduplicated and bounded; the plain tail when none match."""
    if not output:
        return ""
    errors: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if _ERROR_LINE_RE.search(line):
            key = line.strip()[:120]
            if key and key not in seen:
                seen.add(key)
                errors.append(line.strip())
    if not errors:
        return _tail(output)
    head = "BUILD ERROR (the lines that matter; the full log was mostly warnings):\n"
    return (head + "\n".join(errors[:40]))[-FAILURE_DETAIL_CHARS:]


def verdict(results: dict) -> str | None:
    """The first failure of a patch-only validation, as the agent's next attempt reads it; None when it passed.

    `results` is run_final_validation's: stage3 (apply, build, tests) and stage4 (build, PoC) statuses, and under
    "details" the failing stage's step, exit code and output."""
    s3, s4 = results.get("stage3"), results.get("stage4")
    if s3 == "passed" and s4 == "passed":
        return None
    details = results.get("details") or {}
    d3, d4 = details.get("stage3") or {}, details.get("stage4") or {}

    def output(d: dict) -> str:
        return _HEADER_RE.sub("", d.get("output") or "", count=1)

    timed_out = {"stage3": (d3.get("output") or "") == "Command timed out",
                 "stage4": (d4.get("output") or "") == "Command timed out"}
    # 1. apply and 2. build: stage 3 does both first; stage 4 repeats them on a fresh container.
    for name, d, status in (("stage3", d3, s3), ("stage4", d4, s4)):
        if status == "error" and d.get("step") == "apply":
            return f"patch did not apply cleanly: {_tail(output(d))}"
        if status == "error" and d.get("step") == "build":
            if timed_out[name]:
                return "build failed:\nthe build was killed at its time limit"
            return f"build failed:\n{build_failure_summary(output(d))}"
    # 3. the PoC, before the suite: a patch that leaves the crash is reported as such whatever the tests say.
    if s4 == "failed":
        return f"the PoC (/src/poc.bin) still crashes (exit {d4.get('exit_code')}):\n{_tail(output(d4))}"
    if s4 == "error" and d4.get("step") == "poc" and timed_out["stage4"]:
        return ("the PoC (/src/poc.bin) hangs -- the run was killed at its timeout "
                "(a patch that makes the target hang does not negate the PoC)")
    # 4. the suite.
    if s3 == "failed":
        return f"regression suite failed (exit {d3.get('exit_code')}):\n{test_failure_summary(output(d3))}"
    if s3 == "error" and d3.get("step") == "tests" and timed_out["stage3"]:
        return "regression suite failed (killed at its time limit)"
    # Anything else is the harness's own failure (a container that did not start, a missing result file).
    detail = output(d3) or output(d4)
    return f"validation error (stage 3: {s3}, stage 4: {s4})" + (f":\n{_tail(detail)}" if detail else "")
