#!/bin/bash
# Phase A — orchestrate the GS -> compute-SAT TCP flow experiment.
#
# Prereqs (verified up-front):
#   - Starlink-550 state dir generated under
#     paper/satellite_networks_state/gen_data/starlink_550_.../dynamic_state_100ms_for_10s/
#   - satellite_roles.txt present in this directory
#   - augment_fstate.py already run (fstate_*.txt contains PHASE_A_AUGMENT rows)
#   - config_ns3_phase_a.properties + schedule_gs_to_compute.csv present
#
# Steps:
#   1. Sanity-check the four prereqs above.
#   2. Materialise the run dir under runs/<run_name>/ with canonical filenames
#      (`config_ns3.properties`, `schedule.csv`) the simulator expects.
#   3. Invoke ns-3 via waf with --run_dir pointing at it.
#   4. Show a short summary of where logs landed.

set -euo pipefail

PHASE_A=/home/mark/spacesim/hypatia/extensions/phase_a
SIMULATOR=/home/mark/spacesim/hypatia/ns3-sat-sim/simulator
VENV_BIN=/home/mark/spacesim/venv/bin
NETWORK_NAME=starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls
STATE_DIR=/home/mark/spacesim/hypatia/paper/satellite_networks_state/gen_data/$NETWORK_NAME
DYN_DIR=$STATE_DIR/dynamic_state_100ms_for_10s

RUN_NAME=${1:-gs0_to_compute_sat}
RUN_DIR=$PHASE_A/runs/$RUN_NAME

echo "== Phase A run orchestrator =="
echo "  phase_a dir : $PHASE_A"
echo "  state dir   : $STATE_DIR"
echo "  run dir     : $RUN_DIR"
echo

# 1. Sanity checks
echo "-- prereq checks --"
test -d "$STATE_DIR" || { echo "FATAL: state dir missing: $STATE_DIR"; exit 1; }
test -d "$DYN_DIR" || { echo "FATAL: dynamic_state dir missing: $DYN_DIR"; exit 1; }
test -f "$PHASE_A/satellite_roles.txt" || { echo "FATAL: satellite_roles.txt missing"; exit 1; }
test -f "$PHASE_A/config_ns3_phase_a.properties" || { echo "FATAL: config missing"; exit 1; }
test -f "$PHASE_A/schedule_gs_to_compute.csv" || { echo "FATAL: schedule missing"; exit 1; }

# Sanity: every fstate file has been augmented.
NUM_FSTATE=$(ls "$DYN_DIR"/fstate_*.txt | wc -l)
NUM_AUGMENTED=$(grep -l 'PHASE_A_AUGMENT' "$DYN_DIR"/fstate_*.txt 2>/dev/null | wc -l)
echo "  fstate files       : $NUM_FSTATE"
echo "  augmented fstate   : $NUM_AUGMENTED"
if [ "$NUM_AUGMENTED" -ne "$NUM_FSTATE" ]; then
    echo "FATAL: $((NUM_FSTATE - NUM_AUGMENTED)) fstate files are not augmented."
    echo "       Run augment_fstate.py first."
    exit 1
fi
echo "  ok."
echo

# 2. Materialise run dir
echo "-- materialising run dir --"
mkdir -p "$RUN_DIR"
mkdir -p "$RUN_DIR/logs_ns3"
# Use symlinks so updates to the canonical files in phase_a/ flow through
# without copy drift.
ln -sf "$PHASE_A/config_ns3_phase_a.properties" "$RUN_DIR/config_ns3.properties"
ln -sf "$PHASE_A/schedule_gs_to_compute.csv"    "$RUN_DIR/schedule.csv"
echo "  $RUN_DIR/config_ns3.properties -> phase_a/config_ns3_phase_a.properties"
echo "  $RUN_DIR/schedule.csv          -> phase_a/schedule_gs_to_compute.csv"
echo

# 3. Run ns-3
echo "-- running main_satnet --"
echo "  starting at $(date +%H:%M:%S)"
cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo "  finished at $(date +%H:%M:%S), waf exit=$EXIT"
echo

# 4. Summary
echo "-- artefacts --"
echo "  console      : $RUN_DIR/logs_ns3/console.txt"
ls "$RUN_DIR/logs_ns3" 2>/dev/null | sed "s|^|  $RUN_DIR/logs_ns3/|"
exit $EXIT
