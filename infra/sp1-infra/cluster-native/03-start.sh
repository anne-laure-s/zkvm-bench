#!/usr/bin/env bash
# 03 — launch the cluster as native processes (no Docker).
# Each binary runs via the IMAGE's loader + libs (robust vs host glibc skew);
# `nohup` so they survive an SSH disconnect. Env mirrors the compose, on
# localhost with de-conflicted ports.
#
#   NUM_GPUS=1  ./03-start.sh     # validate wiring on 1 GPU first
#   NUM_GPUS=16 ./03-start.sh     # full run
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
NUM_GPUS="${NUM_GPUS:-16}"
NUM_CPU_NODES="${NUM_CPU_NODES:-2}"
# WORKERS_ONLY=1 → (re)start ONLY the cpu/gpu workers, reusing the already-running control plane
# (api/coordinator/gateway/redis/postgres). This is the fast path for tuning sweeps: it skips the
# ~10s of control-plane sleeps + DB migration, so each config costs ~one proof, not a full boot.
# Pair with ./stop-workers.sh (not ./stop.sh) between configs.
WORKERS_ONLY="${WORKERS_ONLY:-}"
# CPU-node feed/emit tuning (cause #4). The trace-chunk emission is CO-LOCATED with the
# executing node, so for SINGLE-BLOCK latency the lever is splice workers PER node (not the
# node count). On a 96c/192t EPYC the default 12 underuses the box — sweep upward (e.g. 24,
# then 32) and keep the value that LOWERS prove_secs (too high → oversubscribes the 16 GPU
# workers' own CPU-side trace-gen and regresses). NUM_CPU_NODES mostly helps concurrent
# throughput, not one block's latency.
SPLICING_WORKERS="${SPLICING_WORKERS:-12}"
SPLICING_BUFFER="${SPLICING_BUFFER:-$SPLICING_WORKERS}"
# ── Core workers (cause #6 — THE lever for single-block latency) ──────────────────────────────
# A PROVE_SHARD task on a GPU node has TWO phases on the SAME worker: (1) `into_record`/`trace_chunk`
# = re-expanding the controller's compact checkpoint into the full ExecutionRecord (~2s, CPU, RISC-V
# re-exec), then (2) the GPU proof. Diag (block 25367437): ~22s of that CPU trace-gen per GPU vs only
# ~4s of GPU compute over a 26s proof → the GPU idles ~80% waiting on its OWN CPU. In v2.4.3 this
# expansion CANNOT be offloaded to the CPU nodes (PROVE_SHARD is hard-routed to WORKER_TYPE=GPU; the
# trace+prove split is intentionally one worker). The ONLY way to hide it is intra-node pipelining:
# run N "core workers" per GPU node so worker A's GPU proves shard K while worker B's CPU expands
# shard K+1. Cluster default is 4 but in practice we saw it serialize to ~1 (likely /dev/shm or the
# weight-admission budget gating concurrency — see notes below). EMPTY = keep cluster default.
#   ⚠️ Each in-flight shard holds a full ExecutionRecord (hundreds of MB–GB). Raising this needs
#   (a) WORKER_MAX_WEIGHT_OVERRIDE high enough to admit them (PROVE_SHARD weight=4, so 24→admits 6),
#   and (b) a BIG /dev/shm (containers default to 64 MB → the limiter then serializes to 1, defeating
#   it; check `df -h /dev/shm`). Sweep 6→8 and watch prove_secs, host RAM, and CUDA OOM.
NUM_CORE_WORKERS="${NUM_CORE_WORKERS:-}"          # empty = cluster default (4); try 6–8 to overlap trace-gen
CORE_BUFFER_SIZE="${CORE_BUFFER_SIZE:-}"          # empty = cluster default (4); lookahead depth feeding the workers
# Trim the per-shard CPU work that surrounds the GPU prove (both reduce the non-GPU tax per shard):
USE_FIXED_PK="${USE_FIXED_PK:-}"                  # =1 → SP1_WORKER_USE_FIXED_PK=true  (skip per-shard setup)
VERIFY_INTERMEDIATES="${VERIFY_INTERMEDIATES:-}"  # =0 → SP1_WORKER_VERIFY_INTERMEDIATES=false (skip per-shard re-verify)
# GPU shard sizing (cause #5). LOG2_SHARD_SIZE = log2(rows per shard). The cluster's built-in
# default is tuned for 24 GB cards; the 5090 has 32 GB. Bigger shards => fewer shards => less
# recursion/normalize overhead AND fewer host<->GPU (PCIe 5.0 x8) + Redis transfers. EMPTY keeps
# the cluster default (safe no-op). To exploit the 32 GB, bump ONE notch at a time (e.g. if the
# default is 21, try 22), watch prove_secs AND `nvidia-smi` VRAM — too high => CUDA OOM.
# GPU_MAX_WEIGHT ~ the VRAM budget (GB) a GPU worker advertises; raise toward ~30 if bigger
# shards spill. Default 24 = today's behavior.
LOG2_SHARD_SIZE="${LOG2_SHARD_SIZE:-}"
GPU_MAX_WEIGHT="${GPU_MAX_WEIGHT:-24}"
# NUMA pinning for the dual-socket EPYC 9654 (192c/384t, 2 NUMA nodes). Cross-socket trace/Redis
# traffic is the subtle latency tax on a bi-socket 16-GPU box. Values:
#   (empty)    = off (today's behavior)
#   interleave = wrap workers in `numactl --interleave=all` (safe, no topology needed) — try first
#   bind       = pin gpu[0..N/2-1] + odd cpu-nodes to socket 0, the rest to socket 1.
#                ⚠️ VERIFY the real GPU<->socket map with `nvidia-smi topo -m` first; the
#                contiguous-half split below is the common layout but not guaranteed.
# CONTAINER FALLBACK: `numactl` sets a MEMORY policy (set_mempolicy/mbind), which needs
# CAP_SYS_NICE + the NUMA sysfs — both usually ABSENT in a Vast container, where even
# `numactl --interleave=all` aborts the worker (this is why earlier NUMA sweeps crashed 0/N).
# When numactl is unusable, we fall back to `taskset -c` (sched_setaffinity only → container-safe):
# pinning a worker to one socket's CPUs makes Linux first-touch land its memory on that socket,
# i.e. ~the locality win without the failing mempolicy syscall. taskset can only 'bind' (no
# interleave equivalent). Pin method is auto-detected below (PIN_METHOD).
NUMA_PIN="${NUMA_PIN:-}"
RUST_LOG="${RUST_LOG:-info}"

# ── Multi-machine (2×8 GPU) ──────────────────────────────────────────────────
# ROLE controls the topology:
#   all    (default) = single box: control plane (api/coordinator/gateway) + cpu + gpu nodes.
#   head            = same as `all`; the name just documents that remote workers will join.
#   worker          = GPU nodes ONLY, joining a remote head's coordinator + redis.
# For 2×8: run ROLE=head NUM_GPUS=8 on box A, ROLE=worker NUM_GPUS=8 HEAD_ADDR=… on box B.
#
# Networking (worker → head): the worker's gpu-nodes must reach the head's coordinator (50052)
# and redis (6379). Two ways:
#   • SSH tunnel (recommended, simple+secure): on the worker run, in the background,
#       ssh -N -L 6379:localhost:6379 -L 50052:localhost:50052 <head-ssh>
#     then keep HEAD_ADDR=localhost (the default) — redis stays 127.0.0.1 on the head.
#   • Direct: expose the head's 6379+50052 publicly, set HEAD_ADDR=<head-ip>, the matching
#     COORD_PORT/REDIS_PORT, and REDIS_PASSWORD (boot.sh must start redis with the same).
ROLE="${ROLE:-all}"
HEAD_ADDR="${HEAD_ADDR:-localhost}"
COORD_PORT="${COORD_PORT:-50052}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
# Where the cpu/gpu nodes find the control plane: localhost for head/all, the head for workers.
if [[ "$ROLE" == worker ]]; then CTRL_HOST="$HEAD_ADDR"; else CTRL_HOST="localhost"; fi
COORD_RPC="http://$CTRL_HOST:$COORD_PORT"
REDIS_URL="redis://${REDIS_PASSWORD:+:$REDIS_PASSWORD@}$CTRL_HOST:$REDIS_PORT/0"

mkdir -p logs run "$HOME/.sp1/circuits"

# refuse to double-start (would collide on ports). In WORKERS_ONLY mode the control plane is
# expected to stay up, so only the worker pids must be clear (use ./stop-workers.sh between configs).
for p in run/*.pid; do
  [[ -f "$p" ]] || continue
  name="$(basename "$p" .pid)"
  if [[ -n "$WORKERS_ONLY" ]]; then case "$name" in cpu-node-*|gpu*) ;; *) continue ;; esac; fi
  if kill -0 "$(cat "$p")" 2>/dev/null; then
    echo "ERROR: already running — $name (pid $(cat "$p")). Run ./stop.sh (or ./stop-workers.sh) first." >&2
    exit 1
  fi
done

# shellcheck source=_paths.sh
source "$ROOT/_paths.sh"
require_cuda13 || exit 1   # host driver must natively support CUDA >= 13.0 (v2.4.3 GPU binaries)
paths_check || exit 1
echo "loader(base)=$LD_BASE"
echo "loader(gpu) =$LD_GPU"

# Decide HOW to pin (numactl vs taskset vs off). numactl is preferred when it can actually apply
# a policy here; probe it with a no-op (`numactl --interleave=all true`) — in a container without
# CAP_SYS_NICE this exits non-zero, which is exactly the case that used to crash the workers.
PIN_METHOD=""   # "" = off | numactl | taskset
if [[ -n "$NUMA_PIN" ]]; then
  numa_nodes="$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l | tr -d ' ')"
  if command -v numactl >/dev/null 2>&1 && numactl --interleave=all true >/dev/null 2>&1; then
    PIN_METHOD=numactl
    echo "pin: numactl usable → NUMA_PIN=$NUMA_PIN"
  elif command -v taskset >/dev/null 2>&1 && [[ "${numa_nodes:-0}" -gt 1 ]]; then
    PIN_METHOD=taskset
    [[ "$NUMA_PIN" == interleave ]] && \
      echo "WARN: interleave needs numactl (unusable here); taskset can only bind → using bind-style CPU pinning." >&2
    echo "pin: numactl unusable (likely a container w/o CAP_SYS_NICE) → taskset CPU-pinning + first-touch ($numa_nodes NUMA nodes)"
  else
    echo "WARN: NUMA_PIN=$NUMA_PIN requested but no working numactl and not (taskset + >1 NUMA node) → pinning OFF." >&2
    NUMA_PIN=""
  fi
fi

WRAP=()   # optional per-process command prefix (numactl ...), set by numa_wrap below
start() {  # <name> <ld> <libs> <binary> -- VAR=val ...
  local name="$1" ld="$2" libs="$3" bin="$4"; shift 4; [[ "${1:-}" == "--" ]] && shift
  [[ -f "$bin" ]] || { echo "MISSING binary for $name: $bin" >&2; return 1; }
  echo "  start $name ${WRAP[*]:+[${WRAP[*]}]}"
  nohup ${WRAP[@]+"${WRAP[@]}"} env "$@" "$ld" --library-path "$libs" "$bin" > "logs/$name.log" 2>&1 < /dev/null &
  echo $! > "run/$name.pid"
}

# Set WRAP to pin the next-started worker onto NUMA socket $1, honoring NUMA_PIN + PIN_METHOD.
numa_wrap() {  # <socket>
  WRAP=()
  case "$PIN_METHOD" in
    numactl)
      case "$NUMA_PIN" in
        interleave) WRAP=(numactl --interleave=all) ;;
        bind)       WRAP=(numactl --cpunodebind="$1" --membind="$1") ;;
      esac ;;
    taskset)
      # container-safe: pin to this socket's CPU list (from sysfs); first-touch handles memory.
      local cpulist; cpulist="$(cat /sys/devices/system/node/node"$1"/cpulist 2>/dev/null)"
      [[ -n "$cpulist" ]] && WRAP=(taskset -c "$cpulist")
      ;;
  esac
}

if [[ "$ROLE" == worker ]]; then
  echo "== ROLE=worker: GPU nodes only, joining head at $CTRL_HOST (coord :$COORD_PORT, redis :$REDIS_PORT) =="
  # Fail fast with a clear message if the head isn't reachable (saves a confusing crash loop).
  for hp in "$CTRL_HOST:$COORD_PORT" "$CTRL_HOST:$REDIS_PORT"; do
    h="${hp%:*}"; p="${hp##*:}"
    # /dev/tcp has no connect timeout → bound it (5s) so an unreachable head fails fast, not in ~2 min.
    if command -v timeout >/dev/null 2>&1; then
      reachable=$(timeout 5 bash -c "exec 3<>/dev/tcp/$h/$p" 2>/dev/null && echo y)
    else
      (exec 3<>"/dev/tcp/$h/$p") 2>/dev/null && reachable=y || reachable=
    fi
    if [[ -z "$reachable" ]]; then
      echo "ERROR: cannot reach head at $hp." >&2
      echo "  • SSH tunnel? run:  ssh -fN -L 6379:localhost:6379 -L 50052:localhost:50052 <head-ssh>  (keep HEAD_ADDR=localhost)" >&2
      echo "  • Direct?     set HEAD_ADDR/COORD_PORT/REDIS_PORT to the head's reachable address+ports." >&2
      exit 1
    fi
  done
  echo "  head reachable ✓"
elif [[ -z "$WORKERS_ONLY" ]]; then
echo "== api =="
start api "$LD_BASE" "$LIBS_BASE" "$ROOT/rootfs-base/api" -- \
  API_GRPC_ADDR=0.0.0.0:50051 API_HTTP_ADDR=0.0.0.0:3000 API_AUTO_MIGRATE=true \
  API_DATABASE_URL=postgresql://postgres:postgrespassword@localhost:5432/postgres \
  RUST_LOG="$RUST_LOG"
sleep 5

echo "== coordinator =="
start coordinator "$LD_BASE" "$LIBS_BASE" "$ROOT/rootfs-base/coordinator" -- \
  COORDINATOR_CLUSTER_RPC=http://localhost:50051 \
  COORDINATOR_ADDR=0.0.0.0:50052 \
  COORDINATOR_METRICS_ADDR=0.0.0.0:9090 \
  RUST_LOG="$RUST_LOG"
sleep 3

echo "== network-gateway (SDK-compatible endpoint, lets us retrieve proofs) =="
start network-gateway "$LD_BASE" "$LIBS_BASE" "$ROOT/rootfs-base/network-gateway" -- \
  GATEWAY_GRPC_ADDR=0.0.0.0:50061 GATEWAY_HTTP_ADDR=0.0.0.0:8081 \
  GATEWAY_PUBLIC_HTTP_URL=http://localhost:8081 \
  GATEWAY_CLUSTER_RPC=http://localhost:50051 \
  GATEWAY_ARTIFACT_STORE=redis GATEWAY_REDIS_NODES="$REDIS_URL" \
  GATEWAY_PROGRAM_STORE=memory GATEWAY_AUTH_MODE=none RUST_LOG="$RUST_LOG"
sleep 2
else
  echo "== WORKERS_ONLY: reusing the running control plane (api/coordinator/gateway) =="
  kill -0 "$(cat run/coordinator.pid 2>/dev/null)" 2>/dev/null || \
    echo "WARN: coordinator not alive — run a full ./03-start.sh once before WORKERS_ONLY restarts." >&2
fi

# cpu-nodes live with the control plane (splicing/trace-chunk emission). On a worker box we run
# GPU nodes only — the head's cpu-nodes feed the whole cluster.
if [[ "$ROLE" != worker ]]; then
echo "== cpu nodes ($NUM_CPU_NODES) =="
for ((c=1; c<=NUM_CPU_NODES; c++)); do
  numa_wrap $(( (c - 1) % 2 ))   # spread cpu-nodes across the 2 sockets
  start "cpu-node-$c" "$LD_BASE" "$LIBS_BASE" "$ROOT/rootfs-base/node" -- \
    NODE_COORDINATOR_RPC="$COORD_RPC" \
    NODE_ARTIFACT_STORE=redis NODE_REDIS_NODES="$REDIS_URL" \
    WORKER_TYPE=CPU WORKER_MAX_WEIGHT_OVERRIDE=48 \
    WORKER_METRICS_ADDR="127.0.0.1:$((9100 + c))" \
    SP1_WORKER_NUM_SPLICING_WORKERS="$SPLICING_WORKERS" SP1_WORKER_SPLICING_BUFFER_SIZE="$SPLICING_BUFFER" \
    SP1_WORKER_NUMBER_OF_SEND_SPLICE_WORKERS_PER_SPLICE=4 \
    SP1_WORKER_SEND_SPLICE_INPUT_BUFFER_SIZE_PER_SPLICE=4 \
    MINIMAL_TRACE_CHUNK_THRESHOLD=33554432 RUST_LOG="$RUST_LOG"
done
fi

# /dev/shm gates how many shards a GPU node can expand+hold concurrently (full ExecutionRecords go
# there). A tiny container default (64 MB) silently serializes core workers to 1 → GPU waits ~2s/shard
# on trace-gen no matter how high NUM_CORE_WORKERS is. Warn loudly so pipelining isn't a no-op.
shm_mb=$(df -m /dev/shm 2>/dev/null | awk 'NR==2{print $2}')
if [[ -n "$shm_mb" && "$shm_mb" -lt 16384 ]]; then
  echo "WARN: /dev/shm is only ${shm_mb} MB — too small to expand multiple shards concurrently." >&2
  echo "      core-worker pipelining (cause #6) will serialize. Remount bigger, e.g.:" >&2
  echo "        mount -o remount,size=128g /dev/shm   (or set --shm-size at container create)" >&2
fi

echo "== gpu nodes ($NUM_GPUS) =="
for ((g=0; g<NUM_GPUS; g++)); do
  numa_wrap $(( NUM_GPUS <= 1 ? 0 : (g < NUM_GPUS / 2 ? 0 : 1) ))   # first half -> socket 0, second half -> socket 1
  gpu_env=(
    NODE_COORDINATOR_RPC="$COORD_RPC"
    NODE_ARTIFACT_STORE=redis NODE_REDIS_NODES="$REDIS_URL"
    WORKER_TYPE=GPU WORKER_MAX_WEIGHT_OVERRIDE="$GPU_MAX_WEIGHT"
    WORKER_METRICS_ADDR="127.0.0.1:$((9200 + g))"
    CUDA_VISIBLE_DEVICES="$g" RUST_LOG="$RUST_LOG"
  )
  # cause #5: only override the shard size when the operator opts in (else keep cluster default).
  [[ -n "$LOG2_SHARD_SIZE" ]] && gpu_env+=( SP1_CLUSTER_LOG2_SHARD_SIZE="$LOG2_SHARD_SIZE" )
  # cause #6: pipeline trace-gen behind GPU proving (only set when the operator opts in).
  [[ -n "$NUM_CORE_WORKERS" ]] && gpu_env+=( SP1_WORKER_NUM_CORE_WORKERS="$NUM_CORE_WORKERS" )
  [[ -n "$CORE_BUFFER_SIZE" ]] && gpu_env+=( SP1_WORKER_CORE_BUFFER_SIZE="$CORE_BUFFER_SIZE" )
  [[ "$USE_FIXED_PK" == 1 ]]   && gpu_env+=( SP1_WORKER_USE_FIXED_PK=true )
  [[ "$VERIFY_INTERMEDIATES" == 0 ]] && gpu_env+=( SP1_WORKER_VERIFY_INTERMEDIATES=false )
  start "gpu$g" "$LD_GPU" "$LIBS_GPU" "$ROOT/rootfs-gpu/app/sp1-cluster-node" -- "${gpu_env[@]}"
done

sleep 3
echo
echo "== started ($ROLE): $(ls run/*.pid 2>/dev/null | wc -l | tr -d ' ') local processes =="
if [[ "$ROLE" == worker ]]; then
  echo "Workers point at head $CTRL_HOST. Verify on the HEAD: tail -f logs/coordinator.log"
  echo "(the head's GetStats should now show 16 gpu_workers total)."
  echo "Stop:   ./stop.sh"
else
  echo "Watch:  tail -f logs/coordinator.log     (GPU workers should register — local + remote)"
  echo "        tail -f logs/api.log logs/gpu0.log"
  echo "Submit: ./submit.sh ~/rsp.elf ~/<witness>.bin   (RAW .bin, not .stdin)   [run on the HEAD]"
  echo "Stop:   ./stop.sh"
fi
