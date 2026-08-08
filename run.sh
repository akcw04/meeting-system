#!/usr/bin/env bash
# Kairos - start both servers (macOS / Linux)
# ===================================================
#   bash run.sh              start, and open the browser when ready
#   bash run.sh --no-browser start without opening a browser
#
# Starts the backend (API :8000) and the frontend (web UI :5173), waits until
# both are actually serving, then opens Kairos in your browser. Press Ctrl+C to
# stop both.
#
# Server output goes to logs/ rather than being interleaved on one terminal, so
# a failure can still be read afterwards.
set -euo pipefail

NO_BROWSER=0
[ "${1:-}" = "--no-browser" ] && NO_BROWSER=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"

FRONTEND_URL="http://localhost:5173"   # localhost, not 127.0.0.1 - Vite serves on IPv6

if [ ! -x "$BACKEND/.venv/bin/python" ]; then
  echo "Backend not set up yet. Run: bash setup.sh"; exit 1
fi

# -u keeps Python unbuffered, so the startup introduction reaches the log
# immediately instead of sitting in a buffer.
echo "  Starting the AI pipeline service (takes a minute or two)..."
( cd "$BACKEND" && exec .venv/bin/python -u -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 ) > "$LOGDIR/backend.out.log" 2> "$LOGDIR/backend.err.log" &
BACK_PID=$!

( cd "$FRONTEND" && exec npm run dev ) > "$LOGDIR/frontend.out.log" 2> "$LOGDIR/frontend.err.log" &
FRONT_PID=$!

cleanup() {
  echo; echo "  Stopping Kairos..."
  kill "$BACK_PID" "$FRONT_PID" 2>/dev/null || true
}
# HUP included so closing the terminal window stops the servers too, not just
# Ctrl+C - otherwise they would keep running with no window to stop them from.
trap cleanup EXIT INT TERM HUP

wait_for() {  # wait_for <url> <label> <seconds>
  local url="$1" label="$2" limit="$3" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if curl -s -o /dev/null -m 1 "$url"; then return 0; fi
    sleep 1; waited=$((waited + 1))
    printf '.'
  done
  echo
  echo "  Kairos could not start: the $label did not come up in time." >&2
  tail -n 15 "$LOGDIR/$label.err.log" 2>/dev/null >&2 || true
  return 1
}

wait_for "http://127.0.0.1:8000/health" "backend" 300 || exit 1
echo " ready"
printf '  Starting the web interface...'
wait_for "$FRONTEND_URL" "frontend" 120 || exit 1
echo " ready"

# The startup introduction the backend printed - shown here because the server
# now runs in the background rather than on this terminal.
sed -n '/^=\+$/,/^=\+$/p' "$LOGDIR/backend.out.log" 2>/dev/null | head -n 14 || true

if [ "$NO_BROWSER" -eq 0 ]; then
  if command -v open >/dev/null 2>&1; then open "$FRONTEND_URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$FRONTEND_URL"
  fi
fi

echo
echo "  Kairos is running at $FRONTEND_URL"
echo "  Everything runs on this computer. Nothing is uploaded."
echo "  Press Ctrl+C to stop."
wait "$BACK_PID"
