#!/usr/bin/env bash
# 00 — ONE-TIME, fully NON-INTERACTIVE install ON THE BOX (the GPU prover).
#
# Installs: system deps (+open-mpi) → rustup (ziskup's toolchain step needs it) →
# ziskup (cargo-zisk GPU + ziskemu + coordinator + worker) + the STARK proving key.
# Hard-fails if cargo-zisk isn't [gpu] or the proving key is missing.
#
# ── DISK vs RAM ──────────────────────────────────────────────────────────────
# The proving key extracts to ~30 GB+ (const-trees for all precompiles+recursion).
#   * Default: everything on disk under ~/.zisk → needs a >=64 GB disk.
#   * High-RAM / tiny-disk box (e.g. 1 TB RAM + 32 GB disk): put the KEY in RAM:
#         ZISK_KEY_DIR=/dev/shm/zisk ./00-install-once.sh
#     Binaries + toolchain + the ROM-asm cache stay on DISK (they must be
#     executable; /dev/shm is usually mounted noexec). Only the proving key
#     (read-only DATA, ~30 GB) is symlinked into RAM. RAM-backed = NOT persistent
#     across an instance STOP → just re-run this script after each start.
set -uo pipefail

# Resolve where the repo-committed helpers (nolock.c, zisk-worker.patched) live, so they
# are found whether shipped inside cluster/ or at the zisk-infra repo root, falling back
# to $HOME. `find_asset <name>` echoes the first existing path.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
find_asset() {
  local n="$1" p
  for p in "$SCRIPT_DIR/$n" "$SCRIPT_DIR/../$n" "$HOME/$n"; do
    [[ -f "$p" ]] && { echo "$p"; return 0; }
  done
  return 1
}

# ~/.zisk holds executable binaries → must be a REAL dir on an exec filesystem (disk).
# A prior attempt may have left it as a symlink into noexec /dev/shm — undo that.
[[ -L "$HOME/.zisk" ]] && rm -f "$HOME/.zisk"
mkdir -p "$HOME/.zisk"

RAM_KEY="${ZISK_KEY_DIR:-}"
disk_avail_gb() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }

# ── space pre-flight (fail fast — a full disk mid-const-tree-gen corrupts the key) ──
da="$(disk_avail_gb "$HOME/.zisk")"
if [[ -n "$RAM_KEY" ]]; then
  mkdir -p "$RAM_KEY"
  # The proving key contains dlopen'd .so files (per-AIR compressor.so, …) → the target
  # MUST be on an EXECUTABLE filesystem. /dev/shm is usually noexec, which breaks
  # `cargo-zisk setup/prove` with "failed to map segment from shared object". Test now,
  # before the 3 GB download + long const-tree gen, and fail fast if noexec.
  _t="$RAM_KEY/.exectest.$$"; printf '#!/bin/sh\nexit 0\n' > "$_t" 2>/dev/null; chmod +x "$_t" 2>/dev/null
  if ! "$_t" 2>/dev/null; then
    rm -f "$_t"
    echo "ERROR: $RAM_KEY is NOEXEC — ZisK's proving key has executable .so files (dlopen) that can't run there." >&2
    echo "       /dev/shm can't be made exec without privileges. Use a bigger DISK (>=64 GB) and run" >&2
    echo "       ./00-install-once.sh WITHOUT ZISK_KEY_DIR (key on the executable disk)." >&2
    exit 1
  fi
  rm -f "$_t"
  ka="$(disk_avail_gb "$RAM_KEY")"
  [[ -n "$da" && "$da" -ge 12 ]] || { echo "ERROR: need >=12 GB free on disk (~/.zisk) for binaries+asm-cache; have ${da:-?} GB." >&2; exit 1; }
  [[ -n "$ka" && "$ka" -ge 40 ]] || { echo "ERROR: need >=40 GB in $RAM_KEY for the proving key; have ${ka:-?} GB." >&2; exit 1; }
  echo "RAM-key mode: binaries on disk (${da} GB free), proving key -> $RAM_KEY (${ka} GB free, RAM)"
else
  MIN_DISK_GB="${MIN_DISK_GB:-64}"
  [[ -n "$da" && "$da" -ge "$MIN_DISK_GB" ]] || {
    echo "ERROR: only ${da} GB free on ~/.zisk — ZisK's key needs ~30 GB+." >&2
    echo "       Use a bigger disk (>=64 GB), OR put the key in RAM: ZISK_KEY_DIR=/dev/shm/zisk ./00-install-once.sh" >&2
    echo "       (override with MIN_DISK_GB=<n>.)" >&2
    exit 1
  }
  echo "disk-key mode: ${da:-?} GB free on ~/.zisk"
fi

echo "== system deps =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
# Dep list mirrors ZisK's official tools/test-env/install_deps.sh (minus qemu-system
# and the CUDA toolkit — the driver is already on the box and we prove with the
# prebuilt GPU binaries). libgmp-dev + libomp-dev are REQUIRED: `cargo-zisk setup
# --asm` compiles the ROM via emulator-asm/Makefile whose final link is
# `gcc ... -lgmp -lgmpxx -lstdc++` → without libgmp-dev the ROM_SETUP step fails
# with "make failed exit 2". nasm/clang/llvm/grpc/secp256k1/sodium/pqxx round out
# the official toolchain (coordinator/worker + native crates).
apt-get install -y --no-install-recommends \
  jq git curl ca-certificates build-essential pkg-config libssl-dev xz-utils \
  protobuf-compiler libprotobuf-dev zstd numactl \
  libgmp-dev libomp-dev nlohmann-json3-dev uuid-dev libgrpc++-dev \
  libsecp256k1-dev libsodium-dev libpqxx-dev nasm \
  libclang-dev clang gcc-riscv64-unknown-elf llvm \
  openmpi-bin openmpi-common libopenmpi-dev
ulimit -l unlimited 2>/dev/null || echo "WARN: could not raise memlock (set ulimit -l unlimited on the host/container)"

echo "== rustup (dependency for ziskup's toolchain step) =="
if ! command -v rustup >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
fi
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.cargo/bin:$HOME/.zisk/bin:$PATH"
command -v rustup >/dev/null 2>&1 || { echo "ERROR: rustup install failed" >&2; exit 1; }

# In RAM-key mode, ziskup installs binaries only (--nokey); we fetch the key to RAM below.
ZK="${ZISK_KEY:-}"
[[ -z "$ZK" ]] && { [[ -n "$RAM_KEY" ]] && ZK=--nokey || ZK=--provingkey; }
echo "== ziskup ($ZK), non-interactive =="
curl -fsSL https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/ziskup/install.sh \
  | bash -s -- "$ZK" -y
export PATH="$HOME/.zisk/bin:$PATH"
command -v cargo-zisk >/dev/null 2>&1 || { echo "ERROR: cargo-zisk not installed (ziskup failed)" >&2; exit 1; }
ver="$(cargo-zisk --version 2>/dev/null)"; echo "cargo-zisk: $ver"
case "$ver" in *'[gpu]'*) GPUFLAG=--gpu ;; *) echo "ERROR: cargo-zisk is NOT the GPU build ([gpu] expected) — is the NVIDIA driver present?" >&2; exit 1 ;; esac

# ── RAM-key mode: download+extract the proving key to RAM, symlink, gen const-trees ──
if [[ -n "$RAM_KEY" ]]; then
  v="$(printf '%s' "$ver" | awk '{print $2}')"          # e.g. 1.0.0-alpha
  F="zisk-provingkey-${v}.tar.gz"; BUCKET="https://storage.googleapis.com/zisk-setup"
  echo "== fetching proving key $v into $RAM_KEY (RAM, ~3 GB download) =="
  ( cd "$RAM_KEY" \
      && curl -fL -O "$BUCKET/$F" && curl -fL -O "$BUCKET/$F.md5" \
      && md5sum -c "$F.md5" \
      && tar --no-same-owner --overwrite -xf "$F" \
      && rm -f "$F" "$F.md5" ) \
    || { echo "ERROR: proving key download/extract into $RAM_KEY failed" >&2; exit 1; }
  ln -sfn "$RAM_KEY/provingKey" "$HOME/.zisk/provingKey"
  echo "== generating constant tree files into RAM (one-time, can take 10-40 min) =="
  CHECK_BIN="$(command -v cargo-zisk-dev || command -v cargo-zisk)"
  "$CHECK_BIN" check-setup --proving-key "$HOME/.zisk/provingKey" -a "$GPUFLAG" \
    || { echo "ERROR: const-tree generation (check-setup) failed" >&2; exit 1; }
fi

echo "== patch memlock (unprivileged Docker / vast.ai: memlock hard-capped ~64 KB) =="
# ZisK's ASM emulator mmaps the ROM (and RAM/input) with MAP_LOCKED by default. On an
# unprivileged container memlock can't be raised, so the locked mmap fails at
# STARTING_ASM_MICROSERVICES with "mmap(rom) errno=11 / Shmem creation for mo failed"
# — killing BOTH `cargo-zisk setup --asm` and `prove --asm`. The `-u/--unlock-mapped-memory`
# flag that would fix it does NOT propagate through the SDK to the spawned microservice
# (verified on v1.0.0-alpha), so we patch the C default map_locked_flag = 0. Pages are
# never actually swapped on a big-RAM box → zero perf cost. Idempotent. See
# fix-memlock-patch.sh for the standalone version (+ cache purge when re-patching).
GLB="$HOME/.zisk/zisk/emulator-asm/src/globals.c"
if [ -f "$GLB" ] && grep -q '^int map_locked_flag = MAP_LOCKED;' "$GLB"; then
  sed -i 's|^int map_locked_flag = MAP_LOCKED;|int map_locked_flag = 0; /* PATCH: unprivileged-Docker memlock cap, unlock by default */|' "$GLB"
  echo "  patched map_locked_flag -> 0 in $GLB"
elif [ -f "$GLB" ]; then
  echo "  (map_locked_flag already patched or default changed — ok)"
else
  echo "  WARN: $GLB not found — asm-emulator source layout may have changed upstream." >&2
fi

echo "== nolock.so shim (memlock: strips MAP_LOCKED for the ASM backend) =="
NOLOCK_C="$(find_asset nolock.c || true)"
if [ -n "$NOLOCK_C" ]; then
  gcc -shared -fPIC -O2 -o "$HOME/nolock.so" "$NOLOCK_C" -ldl \
    && echo "  built ~/nolock.so (from $NOLOCK_C)" || echo "  WARN: nolock.so build failed"
else
  echo "  WARN: nolock.c missing (looked in cluster/, repo root, \$HOME) — LD_PRELOAD required on a memlock-capped box."
fi

echo "== patched zisk-worker drop-in (count_and_plan.cu multi-GPU fix) — LOAD-BEARING =="
# The stock ziskup worker CRASHES at count_and_plan.cu:1586 on multi-GPU. This patched
# binary (built off-box for sm_120; BuildID 4da61fa8135d1d9ad46d76d6ce260b073a0c45f0) is
# what lets a worker use ALL GPUs — required for BOTH the MPI path and the NO_MPI
# single-process path that the benchmark actually used. It is committed in the repo
# (see .gitignore); rebuild recipe in docs/zisk-bringup-report.md.
WORKER_PATCHED="$(find_asset zisk-worker.patched || true)"
if [ -n "$WORKER_PATCHED" ]; then
  cp "$HOME/.zisk/bin/zisk-worker" "$HOME/.zisk/bin/zisk-worker.stock" 2>/dev/null || true
  cp "$WORKER_PATCHED" "$HOME/.zisk/bin/zisk-worker"
  chmod +x "$HOME/.zisk/bin/zisk-worker"
  bid=$(readelf -n "$HOME/.zisk/bin/zisk-worker" 2>/dev/null | awk '/Build ID/{print $NF}')
  echo "  installed patched worker from $WORKER_PATCHED — BuildID $bid"
  echo "  (expected: 4da61fa8135d1d9ad46d76d6ce260b073a0c45f0)"
  if [ "$bid" != "4da61fa8135d1d9ad46d76d6ce260b073a0c45f0" ]; then
    echo "  !!! WARNING: zisk-worker BuildID mismatch (got ${bid:-?}, expected 4da61fa8135d1d9ad46d76d6ce260b073a0c45f0) — wrong/corrupt binary?" >&2
  fi
else
  echo "  !!! zisk-worker.patched NOT FOUND (looked in cluster/, repo root, \$HOME) !!!" >&2
  echo "  !!! The STOCK worker WILL crash in count_and_plan on multi-GPU. Ship the binary" >&2
  echo "  !!! (it is committed in the repo) or rebuild per docs/zisk-bringup-report.md." >&2
fi

echo "== clone zisk repo (mpi_params.sh / official deploy scripts) =="
[ -d "$HOME/zisk/.git" ] || git clone --depth 1 https://github.com/0xPolygonHermez/zisk "$HOME/zisk"

echo "== sanity check =="
command -v mpirun >/dev/null 2>&1 && mpirun --version | head -1 || echo "WARN: mpirun missing (openmpi-bin)"
nvidia-smi -L 2>/dev/null | head -1 || echo "(no nvidia-smi)"
if [ -e "$HOME/.zisk/provingKey" ]; then
  du -shL "$HOME/.zisk/provingKey" 2>/dev/null | sed 's/^/provingKey: /'
else
  echo "ERROR: provingKey missing at ~/.zisk/provingKey after install." >&2
  exit 1
fi

echo "== done. next: ./start.sh, then per-ELF ONCE:  cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000  (01-setup-elf.sh is only for ZISK_PROVE_BACKEND=local) =="
