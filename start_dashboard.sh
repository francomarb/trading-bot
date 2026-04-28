#!/usr/bin/env bash
# Start the Streamlit analytics dashboard (Phase 11.14).
# Usage: bash start_dashboard.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
# Find venv: check this dir first, then walk up (handles git worktrees).
VENV=""
D="$SCRIPT_DIR"
for _ in 1 2 3 4; do
    if [ -f "$D/venv/bin/activate" ]; then VENV="$D/venv"; break; fi
    D="$(dirname "$D")"
done
if [ -z "$VENV" ]; then echo "ERROR: venv not found"; exit 1; fi
source "$VENV/bin/activate"
PORT="${DASHBOARD_PORT:-8501}"
exec streamlit run dashboard.py \
    --server.port "$PORT" \
    --server.headless true \
    --server.runOnSave false
