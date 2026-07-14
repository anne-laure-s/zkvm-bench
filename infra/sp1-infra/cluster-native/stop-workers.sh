#!/usr/bin/env bash
# stop-workers.sh — kill ONLY the cpu/gpu worker processes, leaving the control plane
# (api / coordinator / network-gateway) and infra (redis / postgres) running. This is the
# fast-iteration companion to `WORKERS_ONLY=1 ./03-start.sh` for tuning sweeps: between two
# configs you only bounce the workers, not the whole stack.
set -uo pipefail
cd "$(dirname "$0")"
n=0
for p in run/cpu-node-*.pid run/gpu*.pid; do
  [[ -f "$p" ]] || continue
  pid="$(cat "$p")"; name="$(basename "$p" .pid)"
  if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; n=$((n+1)); fi
  rm -f "$p"
done
# give the GPUs a moment to release VRAM / sockets before the next start binds them
sleep 2
echo "done — $n worker(s) stopped; control plane + redis/postgres left running."
