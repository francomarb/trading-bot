#!/usr/bin/env bash
# Re-launches the forward-test bot in a tmux session if not already running.
# Usage: ./start_bot.sh

set -euo pipefail
cd "$(dirname "$0")"

SESSION="bot"

bot_processes() {
    ps -axo pid=,ppid=,stat=,lstart=,command= | awk '
        /[[:space:]]tmux[[:space:]]/ { next }
        /[Pp]ython[0-9.]* .*forward_test\.py/ ||
        /[Pp]ython[0-9.]* .*main\.py/ ||
        /[Pp]ython[0-9.]* .* -m[[:space:]]+engine\.trader/ {
            print
        }
    '
}

RUNNING_BOTS="$(bot_processes || true)"
if [[ -n "$RUNNING_BOTS" ]]; then
    echo "Refusing to start: bot process already running:"
    echo "$RUNNING_BOTS"
    echo ""
    echo "Use ./stop_bot.sh first, then rerun ./start_bot.sh."
    exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' exists, but no bot process was detected."
    echo "Refusing to start into a possibly stale session."
    echo "Run ./stop_bot.sh first, then rerun ./start_bot.sh."
    exit 1
fi

CMD="source venv/bin/activate && python forward_test.py"
if [[ "$(uname)" == "Darwin" ]]; then
    CMD="source venv/bin/activate && caffeinate -s python forward_test.py"
    echo "  caffeinate -s prevents idle sleep while the bot runs (macOS)."
fi

tmux new-session -d -s "$SESSION" "$CMD"
echo "Started forward test in tmux session '$SESSION'."
echo "  Attach:  tmux attach -t $SESSION"
echo "  Stop:    tmux send-keys -t $SESSION C-c"
