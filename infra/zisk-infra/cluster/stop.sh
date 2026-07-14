#!/usr/bin/env bash
# stop.sh — kill the ZisK cluster processes started by start.sh (by pid file).
#   ./stop.sh              # stop coordinator + worker
#   WORKERS_ONLY=1 ./stop.sh   # stop only the worker (keep the coordinator up)
set -uo pipefail
cd "$(dirname "$0")"
for p in run/*.pid; do
  [[ -f "$p" ]] || continue
  name="$(basename "$p" .pid)"
  [[ "${WORKERS_ONLY:-}" == 1 && "$name" != worker* ]] && continue
  pid="$(cat "$p")"
  if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; fi
  rm -f "$p"
done
echo "done."
