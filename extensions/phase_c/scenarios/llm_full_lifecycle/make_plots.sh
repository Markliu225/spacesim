#!/usr/bin/env bash
# Render all Phase C full-lifecycle figures.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/mark/spacesim/venv/bin/python
cd "$HERE"

echo "== analyze =="
"$PY" analyze.py --scenario-dir .

echo "== latency CDF (4 panel) =="
"$PY" plot_latency_cdf.py

echo "== topology + lifecycle paths (1 frame) =="
"$PY" plot_topology_lifecycle.py

echo "== topology animation (~30 s render) =="
"$PY" plot_topology_anim.py

echo
ls -lh plots/
