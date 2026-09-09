#!/usr/bin/env bash
set -euo pipefail
URL="${TH_MEDIA_HEALTH_URL:-http://127.0.0.1:8010/api/health}"
body="$(curl -fsS --max-time 5 "$URL")"
printf '%s\n' "$body"
printf '%s' "$body" | grep -q '"ok":true'
