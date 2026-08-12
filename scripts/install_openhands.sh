#!/bin/bash

# adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/openhands/openhands-setup.sh.j2

# Update package manager
apt-get update

apt-get install -y curl git build-essential tmux

curl -LsSf https://astral.sh/uv/install.sh | sh

# Add uv to PATH for current session
source $HOME/.local/bin/env

# Install Python 3.13 using uv
uv python install 3.13

# Create a dedicated virtual environment for OpenHands
OPENHANDS_VENV="/opt/openhands-venv"
mkdir -p /opt
uv venv $OPENHANDS_VENV --python 3.13

# Activate the virtual environment and install OpenHands
source $OPENHANDS_VENV/bin/activate

# Set SKIP_VSCODE_BUILD to true to skip VSCode extension build for OpenHands
export SKIP_VSCODE_BUILD=true

# Use 1.0.0 which has Claude Opus 4.5 fix
# Staggered starts in batch_run.sh should avoid runtime contention issues
uv pip install --prerelease=allow openhands-ai==1.0.0

# Fix Python 3.13 compatibility in binaryornot
# The library has Python 2 code (unicode() builtin) that breaks on Python 3
python3 << 'EOF'
import site
import os

# Find binaryornot installation
binaryornot_path = None
for path in site.getsitepackages() + [site.getusersitepackages()]:
    candidate = os.path.join(path, 'binaryornot', 'helpers.py')
    if os.path.exists(candidate):
        binaryornot_path = candidate
        break

if not binaryornot_path:
    print("ERROR: Could not find binaryornot/helpers.py")
    exit(1)

# Read the file
with open(binaryornot_path, 'r') as f:
    content = f.read()

# Fix the Python 2 unicode() call to work on Python 3
# Replace: unicode(bytes_to_check, encoding=detected_encoding['encoding'])
# With: bytes_to_check.decode(encoding=detected_encoding['encoding'] or 'utf-8')
original_code = """unicode(bytes_to_check, encoding=detected_encoding['encoding'])  # noqa"""
fixed_code = """bytes_to_check.decode(encoding=detected_encoding.get('encoding') or 'utf-8')  # noqa"""

if original_code not in content:
    print("WARNING: Expected code pattern not found in binaryornot/helpers.py")
    print("The file may have changed; check if the unicode() issue still exists")
else:
    content = content.replace(original_code, fixed_code)
    with open(binaryornot_path, 'w') as f:
        f.write(content)
    print(f"✓ Fixed Python 3 compatibility in {binaryornot_path}")
EOF

