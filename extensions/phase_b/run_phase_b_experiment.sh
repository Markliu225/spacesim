#!/usr/bin/env bash
# Phase B — orchestrate the LLM workload experiment.
#
# Reuses Phase A's Starlink-550 state and its augmented fstate_0
# (SAT-dst=894 already added). Materialises a run dir with the Phase B
# config + schedule + satellite_roles.txt (symlinked), then runs
# main_satnet via waf.

set -euo pipefail

PHASE_B=/home/mark/spacesim/hypatia/extensions/phase_b
PHASE_A=/home/mark/spacesim/hypatia/extensions/phase_a
SIMULATOR=/home/mark/spacesim/hypatia/ns3-sat-sim/simulator
VENV_BIN=/home/mark/spacesim/venv/bin

NETWORK_NAME=starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls
STATE_DIR=/home/mark/spacesim/hypatia/paper/satellite_networks_state/gen_data/$NETWORK_NAME
DYN_DIR=$STATE_DIR/dynamic_state_100ms_for_10s

RUN_NAME=${1:-llm_run}
RUN_DIR=$PHASE_B/runs/$RUN_NAME

echo "== Phase B run orchestrator =="
echo "  phase_b dir : $PHASE_B"
echo "  state dir   : $STATE_DIR"
echo "  run dir     : $RUN_DIR"
echo

# Prereqs
echo "-- prereq checks --"
test -d "$STATE_DIR"                                || { echo "FATAL: state missing"; exit 1; }
test -f "$DYN_DIR/fstate_0.txt"                     || { echo "FATAL: fstate_0 missing"; exit 1; }
test -f "$PHASE_A/satellite_roles.txt"              || { echo "FATAL: Phase A roles missing"; exit 1; }
test -f "$PHASE_B/config_ns3_phase_b.properties"    || { echo "FATAL: config missing"; exit 1; }
test -f "$PHASE_B/llm_workload_schedule.csv"        || { echo "FATAL: schedule missing"; exit 1; }
# fstate_0 must contain rows with dst=894 (our schedule's dst).
DST=$(grep -v '^#' "$PHASE_B/llm_workload_schedule.csv" | head -1 | cut -d, -f2)
echo "  schedule dst node = $DST"
awk -F, -v d="$DST" '$2==d {found=1; exit} END {exit !found}' "$DYN_DIR/fstate_0.txt" \
    || { echo "FATAL: fstate_0 has no row with dst=$DST. Run augment_fstate.py first."; exit 1; }
if grep -q '^#' "$DYN_DIR/fstate_0.txt"; then
    echo "FATAL: fstate_0.txt has a '#' comment line (ns-3 parser will abort)"; exit 1
fi
echo "  ok."
echo

# Materialise run dir.
echo "-- materialising run dir --"
mkdir -p "$RUN_DIR/logs_ns3"
ln -sf "$PHASE_B/config_ns3_phase_b.properties" "$RUN_DIR/config_ns3.properties"
ln -sf "$PHASE_B/llm_workload_schedule.csv"     "$RUN_DIR/llm_workload_schedule.csv"
ln -sf "$PHASE_A/satellite_roles.txt"           "$RUN_DIR/satellite_roles.txt"
echo "  $RUN_DIR/config_ns3.properties        -> phase_b/config_ns3_phase_b.properties"
echo "  $RUN_DIR/llm_workload_schedule.csv    -> phase_b/llm_workload_schedule.csv"
echo "  $RUN_DIR/satellite_roles.txt          -> phase_a/satellite_roles.txt"
echo

# Run ns-3.
echo "-- running main_satnet --"
echo "  starting at $(date +%H:%M:%S)"
cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo "  finished at $(date +%H:%M:%S), waf exit=$EXIT"
echo

echo "-- artefacts --"
ls -lh "$RUN_DIR/logs_ns3/" 2>/dev/null | head -20
exit $EXIT
