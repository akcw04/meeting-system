#!/usr/bin/env bash
# Meeting System - start both servers (macOS / Linux)
# ===================================================
#   bash run.sh
# Starts the backend (API :8000) and frontend (web UI :5173).
# Press Ctrl+C to stop both. Then open http://localhost:5173
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"

if [ ! -x "$BACKEND/.venv/bin/python" ]; then
  echo "Backend not set up yet. Run: bash setup.sh"; exit 1
fi

echo "Starting backend (http://127.0.0.1:8000) ..."
( cd "$BACKEND" && . .venv/bin/activate && exec uvicorn app.main:app --host 127.0.0.1 --port 8000 ) &
BACK_PID=$!

# stop the backend when this script exits (Ctrl+C)
trap 'echo; echo "Stopping..."; kill "$BACK_PID" 2>/dev/null || true' EXIT INT TERM

sleep 2
echo "Starting frontend (http://localhost:5173) ..."
echo "(Use localhost, not 127.0.0.1 - Vite serves on IPv6 localhost.)"
cd "$FRONTEND" && npm run dev
