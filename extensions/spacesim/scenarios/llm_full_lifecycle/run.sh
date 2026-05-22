#!/usr/bin/env bash
# Phase C full lifecycle scenario: 5 GS → 5 compute SAT, real-dynamic state.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHASE_A_MIXED=/home/mark/spacesim/hypatia/extensions/phase_a/scenarios/mixed_topology
SIMULATOR=/home/mark/spacesim/hypatia/ns3-sat-sim/simulator
VENV_BIN=/home/mark/spacesim/venv/bin
NETWORK=tiny_walker_1500_isls_plus_grid_5cities_algorithm_free_one_only_over_isls

mkdir -p "$HERE/gen_data"
if [ ! -e "$HERE/gen_data/$NETWORK" ]; then
    ln -s "$PHASE_A_MIXED/gen_data/$NETWORK" "$HERE/gen_data/$NETWORK"
fi
SAT_ROLES=$PHASE_A_MIXED/satellite_roles.txt

echo "== Phase C full-lifecycle scenario =="
echo "  scenario dir : $HERE"
test -d "$HERE/gen_data/$NETWORK/dynamic_state_100ms_for_5s" || { echo "FATAL: dyn state missing"; exit 1; }
test -f "$SAT_ROLES" || { echo "FATAL: roles missing"; exit 1; }

# Make sure manifest covers every dst SAT.
PY=$VENV_BIN/python
MANIFEST=$HERE/gen_data/$NETWORK/dynamic_state_100ms_for_5s/.phase_a_augment.json
test -f "$MANIFEST" || { echo "FATAL: augment manifest missing"; exit 1; }
DSTS=$(grep -v '^#' "$HERE/llm_workload_schedule.csv" | awk -F, '{print $2}' | sort -u)
for D in $DSTS; do
    "$PY" -c "import json,sys; m=json.load(open('$MANIFEST')); sys.exit(0 if '$D' in m else 1)" \
        || { echo "FATAL: fstate not augmented for dst=$D"; exit 1; }
done
echo "  augmented dsts: $DSTS"

RUN_DIR=$HERE/run
mkdir -p "$RUN_DIR/logs_ns3"
ln -sf "$HERE/config_ns3.properties"        "$RUN_DIR/config_ns3.properties"
ln -sf "$HERE/llm_workload_schedule.csv"    "$RUN_DIR/llm_workload_schedule.csv"
ln -sf "$SAT_ROLES"                         "$RUN_DIR/satellite_roles.txt"

cd "$SIMULATOR"
PATH="$VENV_BIN:$PATH" ./waf --run "main_satnet --run_dir='$RUN_DIR'" \
    2>&1 | tee "$RUN_DIR/logs_ns3/console.txt"
EXIT=${PIPESTATUS[0]}
echo
ls -lh "$RUN_DIR/logs_ns3"
exit $EXIT
