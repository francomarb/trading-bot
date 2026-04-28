#!/usr/bin/env bash
# Stops the dashboard (if running) and starts a fresh instance.
# Usage: ./recycle_dashboard.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "── Stopping ──────────────────────────────"
./stop_dashboard.sh

echo ""
echo "── Starting ──────────────────────────────"
./start_dashboard.sh
