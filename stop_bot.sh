#!/usr/bin/env bash
# Gracefully stops the forward-test bot running in tmux.
# Usage: ./stop_bot.sh

set -euo pipefail

SESSION="bot"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "No tmux session '$SESSION' found — nothing to stop."
    exit 0
fi

echo "Sending Ctrl+C to tmux session '$SESSION'..."
tmux send-keys -t "$SESSION" C-c

# Wait for the process to exit (up to 10 seconds).
for i in $(seq 1 10); do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "Session '$SESSION' ended."
        exit 0
    fi
    sleep 1
done

# Session still alive — kill it.
echo "Session still alive after 10s — killing."
tmux kill-session -t "$SESSION"
echo "Session '$SESSION' killed."
