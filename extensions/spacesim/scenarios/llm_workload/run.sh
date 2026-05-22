#!/usr/bin/env bash
# Phase B LLM workload — multi-flow E2E scenario orchestrator.
#
# Reuses the constellation state built by Phase A's
# extensions/phase_a/scenarios/mixed_topology/ via a symlink in this
# scenario's gen_data/ directory. Phase A's augment_fstate.py has
# already added SAT-dst routes for all 6 compute SATs.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIMULATOR=/home/mark/spacesim/hypatia/ns3-sat-sim/simulator
PHASE_A_MIXED=/home/mark/spacesim/hypatia/extensions/phase_a/scenarios/mixed_topology
VENV_BIN=/home/mark/spacesim/venv/bin
NETWORK=tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls

# Materialise gen_data via symlink if missing.
mkdir -p "$HERE/gen_data"
if [ ! -e "$HERE/gen_data/$NETWORK" ]; then
    ln -s "$PHASE_A_MIXED/gen_data/$NETWORK" "$HERE/gen_data/$NETWORK"
fi

DYN_DIR=$HERE/gen_data/$NETWORK/dynamic_state_100ms_for_5s
SAT_ROLES=$PHASE_A_MIXED/satellite_roles.txt

echo "== Phase B LLM workload scenario =="
echo "  scenario dir : $HERE"
echo "  state dir    : $HERE/gen_data/$NETWORK"
echo

# Prereqs.
test -d "$DYN_DIR"                                        || { echo "FATAL: dyn-state missing: $DYN_DIR"; exit 1; }
test -f "$SAT_ROLES"                                      || { echo "FATAL: roles missing: $SAT_ROLES"; exit 1; }
test -f "$HERE/config_ns3.properties"                     || { echo "FATAL: config missing"; exit 1; }
test -f "$HERE/llm_workload_schedule.csv"                 || { echo "FATAL: schedule missing"; exit 1; }
# Every compute SAT referenced in the schedule must have augment rows.
MANIFEST=$DYN_DIR/.phase_a_augment.json
test -f "$MANIFEST"                                       || { echo "FATAL: augment manifest missing"; exit 1; }
PY=$VENV_BIN/python
DSTS=$(grep -v '^#' "$HERE/llm_workload_schedule.csv" | awk -F, '{print $2}' | sort -u)
for D in $DSTS; do
    if ! "$PY" -c "import json,sys; m=json.load(open('$MANIFEST')); sys.exit(0 if '$D' in m else 1)"; then
        echo "FATAL: fstate not augmented for dst=$D. Re-run augment_fstate.py with --dst-sats=all-compute."
        exit 1
    fi
done
echo "  ok: state + roles + augment for dsts: $DSTS"
echo

# Materialise run dir.
RUN_DIR=$HERE/run
mkdir -p "$RUN_DIR/logs_ns3"
ln -sf "$HERE/config_ns3.properties"      "$RUN_DIR/config_ns3.properties"
ln -sf "$HERE/llm_workload_schedule.csv"  "$RUN_DIR/llm_workload_schedule.csv"
ln -sf "$SAT_ROLES"                       "$RUN_DIR/satellite_roles.txt"
echo "  run dir      : $RUN_DIR"
echo

cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo
ls -lh "$RUN_DIR/logs_ns3" | head -20
exit $EXIT
