#!/bin/bash
# Provide /scripts/.venv with tomli, which is all validate.py needs beyond stdlib.
#
# The original version installed uv from astral.sh plus a standalone Python 3.13
# in every container. run_agent.py uses a fresh container per validation stage,
# so a full benchmark run performs thousands of those setups, each pulling
# ~60MB from astral.sh. In the Aug 2026 run that produced 1054 "Connection
# reset by peer" failures against astral.sh (throttling from a single IP) and
# broke roughly 385 tasks at the validation step.
#
# validate.py imports only argparse/json/os/subprocess/sys/pathlib + tomli and
# uses no 3.9+ syntax, so the container's own python3 is sufficient. The venv
# path is unchanged, so run_agent.py needs no modification.

apt-get install -y -qq python3 python3-venv >/dev/null 2>&1

VENV="/scripts/.venv"
mkdir -p /scripts
python3 -m venv "$VENV"

# Python 3.11+ ships tomllib in the stdlib; alias it rather than reaching out
# to PyPI at all. Older interpreters (Ubuntu focal has 3.8) still need the
# wheel, but it is a few KB rather than a 60MB toolchain.
if "$VENV/bin/python" -c "import tomllib" >/dev/null 2>&1; then
    SITE_PACKAGES="$("$VENV/bin/python" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
    printf 'from tomllib import *\nfrom tomllib import load, loads\n' > "$SITE_PACKAGES/tomli.py"
else
    "$VENV/bin/pip" install --quiet --retries 5 tomli
fi
