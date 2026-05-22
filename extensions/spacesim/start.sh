#!/usr/bin/env bash
# One-click launcher for the LLM-on-satellite dashboard.
#
# Usage:
#   ./start.sh                    # default: 0.0.0.0:8501, headless
#   ./start.sh --port 8888
#   ./start.sh --port 8888 --open # try to xdg-open after the server is up
#   ./start.sh --help
#
# What it does:
#   1. Locate the venv (`/home/mark/spacesim/venv`) and the spacesim dir
#      (this script's own dir).
#   2. If `streamlit` isn't importable in the venv, run
#      `pip install -r requirements.txt`.
#   3. `streamlit run app.py` with `--server.headless true` and a chosen port.
#   4. Wait for the health endpoint to report `200 ok` before printing the URL.
#   5. Trap SIGINT so Ctrl+C cleanly stops the streamlit subprocess.
#
# Exits with the streamlit process's exit code.

set -euo pipefail

# ---------- argv parsing ---------------------------------------------------

DEFAULT_PORT=8501
PORT="$DEFAULT_PORT"
ADDR="0.0.0.0"
OPEN_BROWSER=0
EXTRA_ARGS=()

usage() {
    cat <<EOF
Usage: $0 [--port N] [--addr HOST] [--open] [--help] [-- ...extra]

  --port N      Bind on port N (default ${DEFAULT_PORT}).
  --addr HOST   Bind address (default 0.0.0.0; use 127.0.0.1 for local-only).
  --open        After health-check passes, try \`xdg-open\` / \`open\` the URL.
  --help        This help.
  --            Anything after -- is appended to the streamlit command line.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            [[ $# -ge 2 ]] || { echo "FATAL: --port needs a value"; exit 2; }
            PORT="$2"; shift 2 ;;
        --addr)
            [[ $# -ge 2 ]] || { echo "FATAL: --addr needs a value"; exit 2; }
            ADDR="$2"; shift 2 ;;
        --open)
            OPEN_BROWSER=1; shift ;;
        --help|-h)
            usage; exit 0 ;;
        --)
            shift; EXTRA_ARGS=("$@"); break ;;
        *)
            echo "FATAL: unknown flag: $1"; usage; exit 2 ;;
    esac
done

# ---------- locate things --------------------------------------------------

SPACESIM_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="/home/mark/spacesim/venv"
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_STREAMLIT="$VENV_DIR/bin/streamlit"

if [[ ! -x "$VENV_PY" ]]; then
    cat >&2 <<EOF
FATAL: Python venv not found at $VENV_DIR.

This launcher assumes the project's shared venv. If you've put yours
elsewhere, set VENV_DIR explicitly:
    VENV_DIR=/path/to/venv ./start.sh
EOF
    exit 1
fi

cd "$SPACESIM_DIR"

echo "[spacesim] working dir : $SPACESIM_DIR"
echo "[spacesim] venv        : $VENV_DIR"
echo "[spacesim] python      : $("$VENV_PY" --version 2>&1)"

# ---------- ensure deps are installed --------------------------------------

if ! "$VENV_PY" -c "import streamlit, plotly, pandas, numpy, yaml" 2>/dev/null; then
    echo "[spacesim] streamlit / deps not found in venv — installing..."
    "$VENV_PIP" install -r "$SPACESIM_DIR/requirements.txt"
else
    echo "[spacesim] deps OK"
fi

# ---------- ensure port is free --------------------------------------------

if "$VENV_PY" - <<EOF 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", $PORT))
except OSError:
    sys.exit(1)
s.close()
EOF
then
    :
else
    echo >&2 "FATAL: port $PORT is already in use. Pick another with --port."
    exit 1
fi

# ---------- launch streamlit ----------------------------------------------

URL="http://${ADDR/0.0.0.0/127.0.0.1}:${PORT}"
echo "[spacesim] launching streamlit on $URL"

# Streamlit writes logs to stderr; we keep them visible.
"$VENV_STREAMLIT" run "$SPACESIM_DIR/dashboard/app.py" \
    --server.headless true \
    --server.address "$ADDR" \
    --server.port "$PORT" \
    --browser.gatherUsageStats false \
    "${EXTRA_ARGS[@]}" &

STREAMLIT_PID=$!

cleanup() {
    if kill -0 "$STREAMLIT_PID" 2>/dev/null; then
        echo
        echo "[spacesim] stopping streamlit (pid $STREAMLIT_PID)..."
        kill "$STREAMLIT_PID" 2>/dev/null || true
        wait "$STREAMLIT_PID" 2>/dev/null || true
    fi
}
trap cleanup INT TERM

# Health-check loop: wait up to 15 s.
HEALTH_URL="${URL}/_stcore/health"
for i in {1..15}; do
    if "$VENV_PY" -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('$HEALTH_URL', timeout=1)
    sys.exit(0 if r.read() == b'ok' else 2)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        echo "[spacesim] health: 200 ok  (took ${i}s)"
        echo
        echo "    Open: $URL"
        echo
        if [[ $OPEN_BROWSER -eq 1 ]]; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "$URL" >/dev/null 2>&1 || true
            elif command -v open >/dev/null 2>&1; then
                open "$URL" >/dev/null 2>&1 || true
            fi
        fi
        break
    fi
    sleep 1
done

# Block on streamlit until it exits or we get a signal.
wait "$STREAMLIT_PID"
EXIT_CODE=$?
exit $EXIT_CODE
