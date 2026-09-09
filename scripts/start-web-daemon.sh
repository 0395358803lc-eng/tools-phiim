#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/runner/workspace/tools-phiim"
RUNTIME="/home/runner/workspace/thmedia-data"
PIDFILE="$RUNTIME/thmedia-web.pid"
LOGFILE="$RUNTIME/logs/web-server.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "TH Media web already running pid=$(cat "$PIDFILE")"
  exit 0
fi
nohup "$ROOT/scripts/start-web.sh" >>"$LOGFILE" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -fsS --max-time 2 http://127.0.0.1:8010/api/health >/dev/null 2>&1; then
    echo "TH Media web started pid=$(cat "$PIDFILE")"
    exit 0
  fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "TH Media web failed to start; inspect $LOGFILE" >&2
    exit 1
  fi
  sleep 0.5
done

echo "TH Media web did not become healthy; inspect $LOGFILE" >&2
exit 1
