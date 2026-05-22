#!/usr/bin/env bash
# Install the llm-workload module into the ns-3 source tree, then run
# waf configure + build to make sure the module compiles into Hypatia.
#
# The canonical source lives in extensions/phase_b/llm_workload/. This
# script syncs it into ns3-sat-sim/simulator/src/llm-workload/ (using
# rsync --delete so removed files at the source are also removed at
# the destination), then triggers a waf rebuild.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM="$HERE/../../../ns3-sat-sim/simulator"
SRC="$HERE/ns3_module"
DST="$SIM/src/llm-workload"
VENV_BIN=/home/mark/spacesim/venv/bin

if [ ! -d "$SRC" ]; then
    echo "FATAL: source dir missing: $SRC"; exit 1
fi
mkdir -p "$DST"

echo "== Syncing llm-workload module =="
echo "  src: $SRC"
echo "  dst: $DST"
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' "$SRC/" "$DST/"

# Mark example wscript as a top-level wscript that waf can recurse into.
echo "  installed:"
find "$DST" -maxdepth 2 -type f | sed "s|^$DST/|    |"

echo
echo "== waf configure =="
cd "$SIM"
PATH="$VENV_BIN:$PATH" ./waf configure \
    --build-profile=debug --enable-mpi --enable-examples --enable-tests \
    --enable-gcov --out=build/debug_all 2>&1 | tail -5

echo
echo "== waf build =="
PATH="$VENV_BIN:$PATH" ./waf 2>&1 | tail -10

echo
echo "== verify llm-workload module is built =="
if grep -q "llm-workload" build/debug_all/c4che/_cache.py 2>/dev/null; then
    echo "  -> llm-workload listed in build cache"
else
    echo "  -> WARNING: could not find llm-workload in build cache"
fi

# Verify the example binary exists
EX="$SIM/build/debug_all/src/llm-workload/examples/ns3.31-llm-workload-example-debug"
if [ -f "$EX" ]; then
    echo "  -> example binary built: $EX"
else
    echo "  -> WARNING: example binary not found at $EX"
fi