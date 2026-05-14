#!/usr/bin/env bash
# Render all result figures for the mixed-topology smoke test.
#
#   plots/flow_dynamics.png    — 5-flow RTT / cwnd / progress / summary
#   plots/topology_paths.png   — world map at t=200ms with 5 flow paths
#   plots/topology_grid.png    — 6-panel time-series snapshot (0.2..2.5 s)
#   plots/topology_anim.gif    — 50-frame animation, real-time playback

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/mark/spacesim/venv/bin/python
cd "$HERE"

echo "== plotting flow dynamics =="
"$PY" plot_flow_dynamics.py

echo "== plotting topology + paths (single snapshot) =="
"$PY" plot_topology_paths.py

echo "== plotting topology snapshot grid =="
"$PY" plot_topology_grid.py

echo "== rendering topology animation =="
"$PY" plot_topology_anim.py

echo
echo "done. figures:"
ls -lh plots/*