#!/usr/bin/env bash
# Phase C — full request lifecycle:
#   GS LLMRequestApplication  → UDP request burst
#     → SAT GatherApplication → ComputeApplication (FIFO queue)
#       → UDP response burst
#         → GS LLMResponseSinkApplication
#
# Reuses Phase A's Starlink-550 state + augmented fstate_0.

set -euo pipefail
PHASE_A=/home/mark/spacesim/hypatia/extensions/phase_a
PHASE_C=/home/mark/spacesim/hypatia/extensions/phase_c
SIMULATOR=/home/mark/spacesim/hypatia/ns3-sat-sim/simulator
VENV_BIN=/home/mark/spacesim/venv/bin
NETWORK=starlink_550_isls_plus_grid_ground_stations_top_100_algorithm_free_one_only_over_isls
STATE_DIR=/home/mark/spacesim/hypatia/paper/satellite_networks_state/gen_data/$NETWORK
DYN_DIR=$STATE_DIR/dynamic_state_100ms_for_10s

RUN_NAME=${1:-llm_run}
RUN_DIR=$PHASE_C/runs/$RUN_NAME

echo "== Phase C orchestrator =="
echo "  state dir : $STATE_DIR"
echo "  run dir   : $RUN_DIR"
echo

test -d "$STATE_DIR"                                || { echo "FATAL: state dir missing"; exit 1; }
test -f "$DYN_DIR/fstate_0.txt"                     || { echo "FATAL: fstate_0 missing"; exit 1; }
test -f "$PHASE_A/satellite_roles.txt"              || { echo "FATAL: Phase A roles missing"; exit 1; }
test -f "$PHASE_C/config_ns3_phase_c.properties"    || { echo "FATAL: config missing"; exit 1; }
test -f "$PHASE_C/llm_workload_schedule.csv"        || { echo "FATAL: schedule missing"; exit 1; }
DST=$(grep -v '^#' "$PHASE_C/llm_workload_schedule.csv" | head -1 | cut -d, -f2)
awk -F, -v d="$DST" '$2==d {found=1; exit} END {exit !found}' "$DYN_DIR/fstate_0.txt" \
    || { echo "FATAL: fstate_0 missing dst=$DST routes (run augment_fstate.py)"; exit 1; }
echo "  ok: state + augment ($DST)"
echo

mkdir -p "$RUN_DIR/logs_ns3"
ln -sf "$PHASE_C/config_ns3_phase_c.properties"   "$RUN_DIR/config_ns3.properties"
ln -sf "$PHASE_C/llm_workload_schedule.csv"       "$RUN_DIR/llm_workload_schedule.csv"
ln -sf "$PHASE_A/satellite_roles.txt"             "$RUN_DIR/satellite_roles.txt"

cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo
ls -lh "$RUN_DIR/logs_ns3"
exit $EXIT
