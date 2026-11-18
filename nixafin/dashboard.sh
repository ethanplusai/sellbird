#!/usr/bin/env bash
# Start or stop the dashboard for this cloud session's preview pane.
#   nixafin/dashboard.sh         start (in the background, survives this shell)
#   nixafin/dashboard.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE=data/nixafin-dashboard.pid

if [ "${1:-}" = "stop" ]; then
  if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then echo "Dashboard stopped."; else echo "Dashboard was not running."; fi
  rm -f "$PIDFILE"
  exit 0
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Dashboard already running."
  exit 0
fi

source .venv/bin/activate
export SELLBIRD_WEBSITE_PROXY="${SELLBIRD_WEBSITE_PROXY:-session}"
# Output goes nowhere: nothing here prints the token, and nothing should keep it.
setsid nohup python nixafin/dashboard_preview.py >/dev/null 2>&1 < /dev/null &
echo $! > "$PIDFILE"
echo "Dashboard starting on port ${NIXAFIN_DASHBOARD_PORT:-5555}."
