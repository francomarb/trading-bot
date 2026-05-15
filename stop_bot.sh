#!/usr/bin/env bash
# Gracefully stops the forward-test bot running in tmux.
# Usage: ./stop_bot.sh

set -euo pipefail

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

bot_pids() {
    bot_processes | awk '{print $1}'
}

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Sending Ctrl+C to tmux session '$SESSION'..."
    tmux send-keys -t "$SESSION" C-c

    # Wait for the tmux session to exit (up to 10 seconds).
    for i in $(seq 1 10); do
        if ! tmux has-session -t "$SESSION" 2>/dev/null; then
            echo "Session '$SESSION' ended."
            break
        fi
        sleep 1
    done

    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "Session still alive after 10s — killing."
        tmux kill-session -t "$SESSION"
        echo "Session '$SESSION' killed."
    fi
else
    echo "No tmux session '$SESSION' found."
fi

REMAINING_BOTS="$(bot_processes || true)"
if [[ -z "$REMAINING_BOTS" ]]; then
    echo "No bot processes detected."
    exit 0
fi

echo "Bot process(es) still running after tmux stop:"
echo "$REMAINING_BOTS"
echo "Sending TERM to remaining bot process(es)..."
for pid in $(bot_pids); do
    kill -TERM "$pid" 2>/dev/null || true
done

for i in $(seq 1 10); do
    if [[ -z "$(bot_processes || true)" ]]; then
        echo "All bot processes stopped."
        exit 0
    fi
    sleep 1
done

echo "ERROR: bot process(es) still running after TERM:"
bot_processes || true
echo "Not starting a replacement bot until these are cleared."
exit 1
