#!/usr/bin/env bash
# spacesim test runner. Uses pytest in /home/mark/spacesim/venv.
#
# Usage:
#   ./run_tests.sh                 — full suite (unit + integration + regression)
#   ./run_tests.sh -m "not slow"   — exclude tests marked @pytest.mark.slow
#   ./run_tests.sh -k augment      — only tests whose name matches "augment"
#   ./run_tests.sh -v              — verbose
#
# Exits non-zero on any failure. Skipped tests are not failures.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY=/home/mark/spacesim/venv/bin/python

cd "$HERE"

# Make sure the venv has pytest. Cheap inline guard so users don't have
# to remember to install it.
if ! "$VENV_PY" -c "import pytest" 2>/dev/null; then
    echo "pytest not in venv; installing..."
    /home/mark/spacesim/venv/bin/pip install pytest
fi

# Run. -ra prints a short summary of skipped/failed tests at the end.
exec "$VENV_PY" -m pytest -ra "$@" tests/