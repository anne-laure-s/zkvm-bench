#!/usr/bin/env bash
# start.sh — bring up the ZisK multi-GPU prover ON THE BOX (coordinator + worker).
#
# Multi-GPU: by DEFAULT a single worker process drives ALL GPUs (needs the patched
# worker from 00-install-once.sh) — the config the 16×5090 benchmark ran. ZisK's official
# path is MPI (`mpirun` MPI_NP ranks, NUMA-bound ~2 GPUs/rank, mirroring
# `distributed/deploy/scripts/worker/install.sh --no-service --gpu`); opt in with USE_MPI=1.
# MPI multi-rank segfaults on unprivileged vast.ai containers (NUMA membind), hence the default.
#
# ── CANONICAL ALTERNATIVE (use this if anything here misbehaves) ───────────────
#   bash $ZISK_SRC/distributed/deploy/scripts/coordinator/install.sh --no-service --api-port 7000
#   bash $ZISK_SRC/distributed/deploy/scripts/worker/install.sh      --no-service --gpu \
#        --coordinator-url http://127.0.0.1:50051
#   # each prints the exact foreground command to run.
#
#   NUM_GPUS auto-detected.  Env knobs:
#     (default)                    single-process worker = ALL GPUs (needs patched worker) — benchmark config
#     USE_MPI=1                     opt into the MPI multi-rank path (ZISK_SRC needed; segfaults on vast.ai)
#     NO_MPI=1                      force single-process (redundant now; overrides USE_MPI)
#     WORKER_BACKEND=asm|emulator   witness backend (default asm; reth needs asm)
#     PROVING_KEY=<folder>          default ~/.zisk/provingKey
#     COMPUTE_CAPACITY / MAX_STREAMS / API_PORT / CLUSTER_PORT / METRICS_PORT
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p run logs
export PATH="$HOME/.zisk/bin:$PATH"

API_PORT="${API_PORT:-7000}"            # client-facing (ProverClient::remote / submit.sh)
CLUSTER_PORT="${CLUSTER_PORT:-50051}"   # worker-facing (ZisK default)
METRICS_PORT="${METRICS_PORT:-9090}"
COORD_BIN="${COORD_BIN:-zisk-coordinator}"
WORKER_BIN="${WORKER_BIN:-zisk-worker}"
ZISK_SRC="${ZISK_SRC:-$HOME/zisk}"
PROVING_KEY="${PROVING_KEY:-$HOME/.zisk/provingKey}"
# Default backend is ASM, not emulator: the only guest here is reth, and --emulator
# returns "register_hints_stream not supported" for it (hints REQUIRE asm). emulator
# is fine only for hint-less guests — override with WORKER_BACKEND=emulator if so.
WORKER_BACKEND="${WORKER_BACKEND:-asm}"

command -v "$COORD_BIN"  >/dev/null 2>&1 || { echo "ERROR: $COORD_BIN not on PATH (run 00-install-once.sh)" >&2; exit 1; }
command -v "$WORKER_BIN" >/dev/null 2>&1 || { echo "ERROR: $WORKER_BIN not on PATH" >&2; exit 1; }

launch() { # name cmd...
  local name="$1"; shift
  echo "starting $name: $*"
  "$@" > "logs/$name.log" 2>&1 &
  echo $! > "run/$name.pid"
}

# ── coordinator ───────────────────────────────────────────────────────────────
if [[ "${WORKERS_ONLY:-}" != 1 ]]; then
  launch coordinator "$COORD_BIN" \
    --api-port "$API_PORT" --cluster-port "$CLUSTER_PORT" --metrics-port "$METRICS_PORT"
  for _ in $(seq 1 30); do
    grep -qiE 'listen|ready|serving|started|registered' logs/coordinator.log 2>/dev/null && break
    sleep 0.5
  done
fi

# ── worker args (shared by MPI and single-process paths) ──────────────────────
wargs=(--coordinator-url "http://localhost:$CLUSTER_PORT" --proving-key "$PROVING_KEY" --gpu)
if [[ "$WORKER_BACKEND" == asm ]]; then
  # asm is the DEFAULT backend (selected simply by NOT passing --emulator; see
  # worker cli/main.rs). `--asm <path>` is OPTIONAL and is NOT used to locate the ROM
  # asm — the worker auto-finds it by program hash in ~/.zisk/cache once `cargo-zisk
  # remote setup -e <elf> --hints` has generated it. So pass --asm only if a path is
  # explicitly given (ASM_FILE); otherwise omit it. The asm backend is REQUIRED for
  # guests that use precompile hints (reth): --emulator returns
  # "register_hints_stream not supported by this backend".
  [[ -n "${ASM_FILE:-}" ]] && wargs+=(--asm "$ASM_FILE")
  # ASM ROM is mmap'd with MAP_LOCKED by default; vast.ai / unprivileged Docker cap
  # memlock at 64 KB → locked mmap fails (errno 11). --unlock-mapped-memory + the
  # nolock.so LD_PRELOAD shim (see start.sh mpirun -x LD_PRELOAD) both disable locking.
  [[ "${ZISK_LOCK_MEM:-0}" == 1 ]] || wargs+=(--unlock-mapped-memory)
  # Auto-load the nolock.so shim (strips MAP_LOCKED) if present and not already set.
  # On memlock-capped boxes the asm backend needs it; harmless elsewhere. This makes
  # `./start.sh` work out of the box without the caller remembering LD_PRELOAD.
  if [[ -z "${LD_PRELOAD:-}" && -f "$HOME/nolock.so" ]]; then
    export LD_PRELOAD="$HOME/nolock.so"
    echo "auto LD_PRELOAD=$LD_PRELOAD (asm backend memlock shim)"
  fi
else
  wargs+=(--emulator)
fi
[[ -n "${MAX_STREAMS:-}" ]]      && wargs+=(--max-streams "$MAX_STREAMS")
[[ -n "${COMPUTE_CAPACITY:-}" ]] && wargs+=(--compute-capacity "$COMPUTE_CAPACITY")

# ── worker launch: single-process (ALL GPUs) by DEFAULT; MPI only if USE_MPI=1 ─
# Single-process is the config the 16×5090 benchmark ran: with the PATCHED worker
# (count_and_plan.cu fix, installed by 00-install-once.sh) one process drives ALL GPUs
# (proofman assigns every GPU to the single rank, node_size=1). MPI multi-rank segfaults
# on unprivileged vast.ai containers (NUMA membind), so it's opt-in. With the STOCK
# worker, single-process falls back to ~1 GPU.
if [[ "${USE_MPI:-}" != 1 || -n "${NO_MPI:-}" ]]; then
  echo "single-process worker (all GPUs via patched worker; set USE_MPI=1 for the MPI path)."
  # The worker links OpenMPI; launched standalone its singleton MPI_Init otherwise stalls ~4 min
  # waiting for a daemon (see docs/zisk-bringup-report.md). This skips it; harmless if not MPI-linked.
  export OMPI_MCA_ess_singleton_isolated=1
  launch worker "$WORKER_BIN" "${wargs[@]}"
else
  command -v mpirun >/dev/null 2>&1 || { echo "ERROR: mpirun not found (apt install openmpi-bin) — or use NO_MPI=1" >&2; exit 1; }
  mp="$ZISK_SRC/distributed/deploy/scripts/common/mpi_params.sh"
  [[ -f "$mp" ]] || { echo "ERROR: mpi_params.sh not found at $mp — set ZISK_SRC=<cloned zisk repo> (or NO_MPI=1)" >&2; exit 1; }
  # mpi_params.sh prints MPI_NP / MPI_PPR / MPI_RAYON_NUM_THREADS / MPI_NUM_GPUS from this host.
  # It needs lscpu (util-linux) / numactl / nproc / nvidia-smi; it EXITS nonzero if it can't
  # detect the socket count — so guard MPI_NP rather than feeding mpirun an empty -np.
  eval "$(bash "$mp" --quiet)" || true
  [[ "${MPI_NP:-}" =~ ^[0-9]+$ && "${MPI_NP:-0}" -ge 1 ]] || {
    echo "ERROR: mpi_params.sh did not produce a valid MPI_NP (socket detection failed?)." >&2
    echo "       Check 'lscpu' / 'numactl --hardware', or run with NO_MPI=1 (single process)." >&2
    exit 1
  }
  # Overridable layout. On unprivileged containers `--bind-to numa` + `-map-by ppr:N:numa`
  # fails: socket-1 ranks can't bind memory ("failed to bind memory") → segfault. (The
  # rank→GPU assignment itself is collision-free — proofman pops distinct GPUs per rank;
  # the crash is the NUMA membind, not the GPU mapping.) MPI_NP_OVERRIDE=<n_gpus>
  # MPI_MAPBY=slot MPI_BIND=none → 1 rank per GPU, no NUMA binding = deterministic 1:1.
  MPI_NP="${MPI_NP_OVERRIDE:-$MPI_NP}"
  MPI_MAPBY="${MPI_MAPBY:-ppr:${MPI_PPR}:numa}"
  MPI_BIND="${MPI_BIND:-numa}"
  MPI_RAYON="${MPI_RAYON_OVERRIDE:-$MPI_RAYON_NUM_THREADS}"
  echo "MPI layout: np=$MPI_NP map-by=$MPI_MAPBY bind=$MPI_BIND rayon=$MPI_RAYON over ${MPI_NUM_GPUS:-?} GPU(s)"
  [[ "${MPI_NUM_GPUS:-0}" -ge 1 ]] || echo "WARN: mpi_params saw 0 GPUs — is nvidia-smi present?" >&2
  # -x LD_PRELOAD: propagate the nolock.so shim to every MPI rank (strips MAP_LOCKED
  # from the ASM backend's shm mmaps — see nolock.c; needed on memlock-capped boxes).
  launch worker mpirun --report-bindings --allow-run-as-root \
    -np "$MPI_NP" -map-by "$MPI_MAPBY" --bind-to "$MPI_BIND" --rank-by slot \
    -x "RAYON_NUM_THREADS=$MPI_RAYON" -x HOME ${LD_PRELOAD:+-x LD_PRELOAD} \
    "$WORKER_BIN" "${wargs[@]}"
fi

echo "== up =="
echo "  coordinator api=$API_PORT cluster=$CLUSTER_PORT metrics=$METRICS_PORT"
echo "  worker backend=$WORKER_BACKEND  (single-process all-GPU by default; USE_MPI=1 for the MPI path)"
echo "  tail -f logs/coordinator.log logs/worker.log"
echo "  remote setup (once):  cargo-zisk remote setup -e <elf> --hints --coordinator http://127.0.0.1:$API_PORT"
echo "  submit:  ./submit.sh <elf> <input.bin> <hints>"
