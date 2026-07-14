#!/usr/bin/env bash
# stop.sh — kill any stray openvm-reth-benchmark proving process on the box.
# OpenVM has no coordinator/worker daemons (multi-GPU = one transient process), so
# unlike the ZisK cluster there is nothing to "bring down" — this is just a safety net
# to free GPUs if a prove run was interrupted and left a process behind.
set -uo pipefail
pids="$(pgrep -f 'openvm-reth-benchmark' 2>/dev/null || true)"
if [[ -z "$pids" ]]; then
  echo "no openvm-reth-benchmark process running."
  exit 0
fi
echo "killing openvm-reth-benchmark: $pids"
# shellcheck disable=SC2086
kill $pids 2>/dev/null || true
sleep 2
pids="$(pgrep -f 'openvm-reth-benchmark' 2>/dev/null || true)"
[[ -n "$pids" ]] && { echo "force-killing: $pids"; kill -9 $pids 2>/dev/null || true; }
nvidia-smi --query-gpu=index,memory.used --format=csv 2>/dev/null || true
