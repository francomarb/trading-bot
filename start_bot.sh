#!/usr/bin/env bash
# Re-launches the forward-test bot in a tmux session if not already running.
# Usage: ./start_bot.sh

set -euo pipefail
cd "$(dirname "$0")"

SESSION="bot"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
    exit 0
fi

tmux new-session -d -s "$SESSION" "source venv/bin/activate && python forward_test.py"
echo "Started forward test in tmux session '$SESSION'."
echo "  Attach:  tmux attach -t $SESSION"
echo "  Stop:    tmux send-keys -t $SESSION C-c"
