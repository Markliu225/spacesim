#!/usr/bin/env bash
# Phase A — mixed-topology E2E smoke test orchestrator.
#
# Prereqs:
#   - build_state.py has been run (gen_data/.../ populated)
#   - satellite_roles.txt is in this scenario dir
#   - augment_fstate.py has been run for all type=C sats
#   - The patched ns-3 binary at
#     /home/mark/spacesim/hypatia/ns3-sat-sim/simulator/build/debug_all/scratch/main_satnet/main_satnet
#     is up to date with the topology-satellite-network.cc patch.
#
# Steps:
#   1. Prereq checks (state, roles, manifest, schedule).
#   2. Materialise run dir with symlinks for config / schedule / roles
#      into a sibling dir at the same level as gen_data/, so the relative
#      paths in config_ns3.properties resolve.
#   3. Invoke ns-3.
#   4. Print where logs landed.

set -euo pipefail

SCENARIO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHASE_A_DIR="$(cd "$SCENARIO_DIR/../.." && pwd)"
SIMULATOR="/home/mark/spacesim/hypatia/ns3-sat-sim/simulator"
VENV_BIN=/home/mark/spacesim/venv/bin
NETWORK_NAME=tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls
STATE_DIR="$SCENARIO_DIR/gen_data/$NETWORK_NAME"
DYN_DIR="$STATE_DIR/dynamic_state_100ms_for_5s"
RUN_DIR="$SCENARIO_DIR/run"

echo "== mixed-topology E2E smoke test =="
echo "  scenario   : $SCENARIO_DIR"
echo "  state      : $STATE_DIR"
echo "  run dir    : $RUN_DIR"
echo

# 1. Prereq checks
echo "-- prereq checks --"
test -d "$STATE_DIR"                        || { echo "FATAL: missing state dir. Run: python build_state.py"; exit 1; }
test -d "$DYN_DIR"                          || { echo "FATAL: missing dynamic-state dir"; exit 1; }
test -f "$SCENARIO_DIR/satellite_roles.txt" || { echo "FATAL: missing satellite_roles.txt"; exit 1; }
test -f "$SCENARIO_DIR/schedule.csv"        || { echo "FATAL: missing schedule.csv"; exit 1; }
test -f "$SCENARIO_DIR/config_ns3.properties" || { echo "FATAL: missing config_ns3.properties"; exit 1; }
test -f "$DYN_DIR/.phase_a_augment.json"    || { echo "FATAL: fstate not augmented. Run augment_fstate.py"; exit 1; }
NUM_FSTATE=$(ls "$DYN_DIR"/fstate_*.txt | wc -l)
COMPUTE_SATS=$(awk -F, '$2=="C"{print $1}' "$SCENARIO_DIR/satellite_roles.txt" | wc -l)
echo "  fstate count        : $NUM_FSTATE  (expect 50)"
echo "  compute SAT count   : $COMPUTE_SATS  (expect 6)"
echo "  schedule rows       : $(grep -cv '^[[:space:]]*\(#\|$\)' "$SCENARIO_DIR/schedule.csv")"
test "$NUM_FSTATE" -eq 50  || { echo "FATAL: fstate count != 50 -- rerun build_state.py"; exit 1; }
test "$COMPUTE_SATS" -eq 6 || { echo "FATAL: compute SAT count != 6 -- rewrite satellite_roles.txt"; exit 1; }

# Hard sanity: ns-3 fstate parser will SIGIOT on any '#' comment line.
if grep -q '^#' "$DYN_DIR"/fstate_*.txt 2>/dev/null; then
    echo "FATAL: at least one fstate has a '#' line. Re-run augment with --rewrite."
    exit 1
fi
echo "  ok."
echo

# 2. Materialise run dir
echo "-- materialising run dir --"
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR/logs_ns3"
ln -sf "$SCENARIO_DIR/config_ns3.properties" "$RUN_DIR/config_ns3.properties"
ln -sf "$SCENARIO_DIR/schedule.csv"          "$RUN_DIR/schedule.csv"
ln -sf "$SCENARIO_DIR/satellite_roles.txt"   "$RUN_DIR/satellite_roles.txt"
echo "  $RUN_DIR/{config_ns3.properties,schedule.csv,satellite_roles.txt}"
echo

# 3. Run ns-3
echo "-- running ns-3 (debug build) --"
echo "  starting at $(date +%H:%M:%S)"
cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo "  finished at $(date +%H:%M:%S), waf exit=$EXIT"
echo

# 4. Summary
echo "-- artefacts --"
echo "  tcp_flows.csv   : $RUN_DIR/logs_ns3/tcp_flows.csv"
echo "  per-flow logs   : $RUN_DIR/logs_ns3/tcp_flow_<id>_*.csv"
ls "$RUN_DIR/logs_ns3" 2>/dev/null | sed "s|^|  |"
exit $EXIT