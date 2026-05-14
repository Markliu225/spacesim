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

# Sanity: every fstate file ns-3 will *actually read* must contain a
# forwarding entry for our compute-SAT dst. ns-3 reads at t = 0,
# INTERVAL, 2*INTERVAL, ... while t < SIM_END_NS. We rely on
# augment_fstate.py's .phase_a_augment.json manifest as the authoritative
# "(dst, t) augmented" record, with a CSV-probe fallback for runs that
# pre-date the manifest. Comment-line check is a hard guard because ns-3's
# parser SIGIOTs on any line whose comma-split != 5.
INTERVAL_NS=$(grep '^dynamic_state_update_interval_ns=' "$PHASE_A/config_ns3_phase_a.properties" | cut -d= -f2 | tr -d '"' | tr -d ' ')
SIM_END_NS=$(grep '^simulation_end_time_ns=' "$PHASE_A/config_ns3_phase_a.properties" | cut -d= -f2 | tr -d '"' | tr -d ' ')
DST_NODE=$(head -1 "$PHASE_A/schedule_gs_to_compute.csv" | cut -d, -f3)
MANIFEST="$DYN_DIR/.phase_a_augment.json"
echo "  dynamic_state_update_interval_ns = $INTERVAL_NS"
echo "  simulation_end_time_ns           = $SIM_END_NS"
echo "  schedule dst node                = $DST_NODE"
echo "  augment manifest                 : $(test -f "$MANIFEST" && echo present || echo absent)"
REQUIRED_TS=$(seq 0 "$INTERVAL_NS" $((SIM_END_NS - 1)))
echo "  fstate timesteps ns-3 will read  : $(echo $REQUIRED_TS | wc -w)"
PY=/home/mark/spacesim/venv/bin/python
for t in $REQUIRED_TS; do
    f="$DYN_DIR/fstate_${t}.txt"
    test -f "$f" || { echo "FATAL: missing $f"; exit 1; }
    gf="$DYN_DIR/gsl_if_bandwidth_${t}.txt"
    test -f "$gf" || { echo "FATAL: missing $gf"; exit 1; }
    # Manifest preferred; else CSV probe.
    if [ -f "$MANIFEST" ] && "$PY" -c "
import json, sys
m = json.load(open('$MANIFEST'))
sys.exit(0 if $t in m.get('$DST_NODE', []) else 1)
" 2>/dev/null; then
        :  # manifest says (dst=$DST_NODE, t=$t) is augmented
    else
        awk -F, -v dst="$DST_NODE" '$2==dst {found=1; exit} END {exit !found}' "$f" \
            || { echo "FATAL: $f has no row with dst=$DST_NODE (run augment_fstate.py)"; exit 1; }
    fi
    if grep -q '^#' "$f"; then
        echo "FATAL: $f has a '#' comment line (ns-3 parser will abort)."
        echo "       Run: augment_fstate.py --rewrite --dst-sats $DST_NODE"
        exit 1
    fi
done
echo "  ok: all required fstate files present, dst=$DST_NODE rows found, no comment lines."
echo

# 2. Materialise run dir
echo "-- materialising run dir --"
mkdir -p "$RUN_DIR"
mkdir -p "$RUN_DIR/logs_ns3"
# Use symlinks so updates to the canonical files in phase_a/ flow through
# without copy drift. satellite_roles.txt is required by the patched
# TopologySatelliteNetwork (Phase A extension) -- it reads it from the
# run dir to add compute SATs to m_endpoints.
ln -sf "$PHASE_A/config_ns3_phase_a.properties" "$RUN_DIR/config_ns3.properties"
ln -sf "$PHASE_A/schedule_gs_to_compute.csv"    "$RUN_DIR/schedule.csv"
ln -sf "$PHASE_A/satellite_roles.txt"           "$RUN_DIR/satellite_roles.txt"
echo "  $RUN_DIR/config_ns3.properties -> phase_a/config_ns3_phase_a.properties"
echo "  $RUN_DIR/schedule.csv          -> phase_a/schedule_gs_to_compute.csv"
echo "  $RUN_DIR/satellite_roles.txt   -> phase_a/satellite_roles.txt"
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
