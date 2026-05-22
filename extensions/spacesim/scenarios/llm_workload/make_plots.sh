#!/usr/bin/env bash
# Render all three figures for the Phase B LLM workload scenario.
#
#   plots/latency_cdf.png      — 2-panel CDF (per-packet, per-request gather)
#   plots/request_timeline.png — 5-row Gantt-like request lifelines
#   plots/topology_llm.png     — world map with 5 LLM flows, line ∝ pkts

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=/home/mark/spacesim/venv/bin/python
cd "$HERE"

# Patch for cartopy 0.18 + matplotlib 3.7 — same shim used by Phase A.
# (Only plot_topology_llm.py uses cartopy shapereader; the latency / timeline
# plots are pure matplotlib.)

echo "== analyze =="
"$PY" analyze.py --scenario-dir .

echo "== latency CDF =="
"$PY" plot_latency_cdf.py

echo "== request timeline =="
"$PY" plot_request_timeline.py

echo "== topology + LLM flows =="
"$PY" plot_topology_llm.py

echo "== topology animation (~30 s render) =="
"$PY" plot_topology_anim.py

echo
echo "outputs:"
ls -lh plots/
