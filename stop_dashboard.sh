#!/usr/bin/env bash
# Gracefully stops the dashboard running in tmux.
# Usage: ./stop_dashboard.sh

set -euo pipefail

SESSION="dashboard"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "No tmux session '$SESSION' found — nothing to stop."
    exit 0
fi

echo "Sending Ctrl+C to tmux session '$SESSION'..."
tmux send-keys -t "$SESSION" C-c

for i in $(seq 1 10); do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "Session '$SESSION' ended."
        exit 0
    fi
    sleep 1
done

echo "Session still alive after 10s — killing."
tmux kill-session -t "$SESSION"
echo "Session '$SESSION' killed."
