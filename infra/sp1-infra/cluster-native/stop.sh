#!/usr/bin/env bash
# stop.sh — kill all cluster processes started by 03-start.sh (redis/postgres stay up).
set -uo pipefail
cd "$(dirname "$0")"
for p in run/*.pid; do
  [[ -f "$p" ]] || continue
  pid="$(cat "$p")"; name="$(basename "$p" .pid)"
  if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; fi
  rm -f "$p"
done
echo "done (redis & postgres left running — stop with: redis-cli shutdown nosave ; pg_ctlcluster <ver> main stop)"
