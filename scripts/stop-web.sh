#!/usr/bin/env bash
set -euo pipefail
PIDFILE="/home/runner/workspace/thmedia-data/thmedia-web.pid"
if [ ! -f "$PIDFILE" ]; then echo "TH Media web is not running"; exit 0; fi
PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
fi
rm -f "$PIDFILE"
echo "TH Media web stopped"
