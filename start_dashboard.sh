#!/usr/bin/env bash
# Launches the analytics dashboard in a tmux session if not already running.
# Usage: ./start_dashboard.sh

set -euo pipefail
cd "$(dirname "$0")"

SESSION="dashboard"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
    exit 0
fi

PORT="${DASHBOARD_PORT:-8501}"

tmux new-session -d -s "$SESSION" \
    "source venv/bin/activate && streamlit run dashboard.py --server.port $PORT --server.headless true"

echo "Started dashboard in tmux session '$SESSION' on port $PORT."
echo "  Open:    http://localhost:$PORT"
echo "  Attach:  tmux attach -t $SESSION"
echo "  Stop:    ./stop_dashboard.sh"
