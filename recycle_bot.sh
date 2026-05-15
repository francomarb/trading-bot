#!/usr/bin/env bash
# Stops the forward-test bot (if running) and starts a fresh instance.
# Usage: ./recycle_bot.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "── Stopping ──────────────────────────────"
./stop_bot.sh

echo ""
echo "── Starting ──────────────────────────────"
# start_bot.sh independently refuses to launch if any bot process is still
# present, so recycle fails closed instead of creating a duplicate runtime.
./start_bot.sh
