#!/usr/bin/env bash
# 00-install-once.sh — one-time setup ON THE BOX for self-hosted multi-GPU OpenVM.
#
# For OpenVM the "install" is:
#   1. toolchain (nightly-2026-01-01) + CUDA prerequisites,
#   2. clone openvm-eth (the block prover) + its pinned openvm / stark-backend revs,
#   3. apply the vendored MULTI-GPU patch (docs/openvm-multigpu.md) via cargo [patch],
#   4. build the CUDA binary,
#   5. (optional) keygen the app/agg proving keys.
#
# There is NO coordinator/worker to install: OpenVM multi-GPU here is ONE process driving
# all the box's GPUs (the patch). Re-run after a rev drift to re-vendor + rebuild.
#
#   CUDA_ARCH=120 ./00-install-once.sh           # RTX 5090 = Blackwell (sm_120, NOT 89)
#   MULTI_GPU_PATCH=0 ./00-install-once.sh        # build the STOCK single-GPU binary only
#   BUILD_FEATURES= BUILD_GUEST=0 ./00-install-once.sh   # FREE compile-check (no CUDA/GPU; placeholder guest)
#
# 💸 SAVE GPU-BOX COST: the patch's CUDA code is all #[cfg(feature="cuda")] and async is enabled on
# the openvm-sdk dependency directly, so a plain (no-feature) build already compiles the multi-GPU
# path. Run this once with BUILD_FEATURES= on your Mac / a cheap CPU box FIRST to shake out [patch]
# + trait-bound errors for free. Only rent the GPU box once that build is green. See docs/.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

OPENVM_ETH_DIR="${OPENVM_ETH_DIR:-$HOME/openvm-eth}"
# PIN openvm-eth to the EXACT commit validated on the Mac (witnesses were minted + the binary/ELF
# built against it). A newer HEAD can change StatelessExecutorInput → cached witnesses fail to
# deserialize, or drift the patch's exact-string anchors. Only bump this if you re-mint witnesses.
OPENVM_ETH_REV="${OPENVM_ETH_REV:-0373b3b9d67056e30c99810bc868e433647a03a7}"
TOOLCHAIN="${TOOLCHAIN:-nightly-2026-01-01}"
CUDA_ARCH="${CUDA_ARCH:-120}"                 # Blackwell (RTX 5090). 4090=89, L40S=89, H100=90.
MULTI_GPU_PATCH="${MULTI_GPU_PATCH:-1}"
BUILD_FEATURES="${BUILD_FEATURES:-cuda}"

echo "== 1. toolchain =="
if ! command -v rustup >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  # shellcheck source=/dev/null
  source "$HOME/.cargo/env"
fi
rustup toolchain install "$TOOLCHAIN" || true
rustup component add rust-src clippy rustfmt --toolchain "$TOOLCHAIN" || true

# 1.5 CUDA preflight — fail fast BEFORE the ~3min guest + cargo-openvm builds if the toolkit can't
# target this GPU (Blackwell sm_120 needs CUDA toolkit >= 12.8).
if [[ "$BUILD_FEATURES" == *cuda* ]]; then
  echo "== 1.5 CUDA preflight (sm_$CUDA_ARCH needs toolkit >= 12.8) =="
  if command -v nvcc >/dev/null 2>&1; then
    nv="$(nvcc --version | grep -oE 'release [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)"
    echo "  nvcc release: ${nv:-?}"
    awk -v v="${nv:-0}" 'BEGIN{split(v,a,".");ok=(a[1]>12)||(a[1]==12&&a[2]>=8);exit !ok}' \
      || { echo "ERROR: CUDA toolkit ${nv:-?} < 12.8 — sm_$CUDA_ARCH (Blackwell) will fail to build. Install CUDA toolkit >= 12.8, or use BUILD_FEATURES= for the CPU build." >&2; exit 1; }
  else
    echo "ERROR: nvcc NOT found — the CUDA build WILL fail. Install CUDA toolkit >= 12.8, or use BUILD_FEATURES= for the CPU build." >&2
    exit 1
  fi
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/  gpu: /' || echo "  ⚠️ nvidia-smi not found" >&2
fi

echo "== 2. clone openvm-eth @ $OPENVM_ETH_REV =="
if [[ ! -d "$OPENVM_ETH_DIR/.git" ]]; then
  git clone https://github.com/axiom-crypto/openvm-eth "$OPENVM_ETH_DIR"
fi
git -C "$OPENVM_ETH_DIR" fetch --depth 1 origin "$OPENVM_ETH_REV" 2>/dev/null || git -C "$OPENVM_ETH_DIR" fetch origin
git -C "$OPENVM_ETH_DIR" checkout -q "$OPENVM_ETH_REV" \
  || { echo "ERROR: cannot checkout openvm-eth $OPENVM_ETH_REV" >&2; exit 1; }
mkdir -p "$ROOT/elfs"
git -C "$OPENVM_ETH_DIR" rev-parse HEAD | tee "$ROOT/elfs/openvm-reth.commit"

echo "== 3. multi-GPU patch (vendoring openvm + stark-backend, cargo [patch]) =="
if [[ "$MULTI_GPU_PATCH" == 1 ]]; then
  "$ROOT/patches/apply.sh" "$OPENVM_ETH_DIR"
else
  echo "  MULTI_GPU_PATCH=0 -> building the stock single-GPU binary (no patch)."
fi

echo "== 3.5 guest ELF (cargo openvm build) =="
# The prover binary `include_bytes!`s bin/reth-benchmark/elf/openvm-stateless-guest. It is NOT
# committed — built from bin/stateless-guest with the openvm CLI (riscv32im-risc0 target), exactly
# as openvm-eth's run.sh / CI do. BUILD_GUEST=0 writes a placeholder (compile-check only; NOT runnable).
GUEST_DST="$OPENVM_ETH_DIR/bin/reth-benchmark/elf/openvm-stateless-guest"
mkdir -p "$(dirname "$GUEST_DST")"
if [[ "${BUILD_GUEST:-1}" == 1 ]]; then
  # Install cargo-openvm at the SAME rev as the host openvm SDK (from Cargo.lock), NOT main HEAD —
  # a drifted guest toolchain can emit an ELF/exe the pinned host SDK can't interpret.
  OPENVM_REV="$(grep -oE 'openvm-org/openvm\.git[^\"]*#[0-9a-f]{40}' "$OPENVM_ETH_DIR/Cargo.lock" 2>/dev/null | grep -oE '[0-9a-f]{40}' | head -1)"
  if ! command -v cargo-openvm >/dev/null 2>&1; then
    vendored_cli="$OPENVM_ETH_DIR/.vendor/openvm/crates/cli"
    if [[ -d "$vendored_cli" ]]; then
      echo "installing cargo-openvm from the vendored checkout (rev-matched, no re-clone)..."
      cargo install --locked --path "$vendored_cli"
    else
      echo "installing cargo-openvm at rev ${OPENVM_REV:-main} (matches host SDK)..."
      cargo install --locked --git https://github.com/openvm-org/openvm.git \
        ${OPENVM_REV:+--rev "$OPENVM_REV"} cargo-openvm
    fi
  else
    echo "cargo-openvm already installed: $(cargo-openvm --version 2>/dev/null || echo '?') (ensure it matches rev ${OPENVM_REV:-?})"
  fi
  ( cd "$OPENVM_ETH_DIR/bin/stateless-guest" && OPENVM_RUST_TOOLCHAIN="$TOOLCHAIN" cargo openvm build )
  GUEST_SRC="$OPENVM_ETH_DIR/bin/stateless-guest/target/riscv32im-risc0-zkvm-elf/release/openvm-stateless-guest"
  [[ -f "$GUEST_SRC" ]] && cp "$GUEST_SRC" "$GUEST_DST" \
    || { echo "ERROR: guest ELF not produced at $GUEST_SRC" >&2; exit 1; }
  # Also surface a copy + commit pin under the box's elfs/.
  mkdir -p "$ROOT/elfs" && cp "$GUEST_SRC" "$ROOT/elfs/openvm-reth.elf"
  git -C "$OPENVM_ETH_DIR" rev-parse HEAD > "$ROOT/elfs/openvm-reth.commit" 2>/dev/null || echo unknown > "$ROOT/elfs/openvm-reth.commit"
  echo "guest ELF -> $GUEST_DST  (+ copy in $ROOT/elfs/openvm-reth.elf)"
else
  echo "BUILD_GUEST=0 -> placeholder ELF (compile-check only, NOT runnable)"; touch "$GUEST_DST"
fi

echo "== 4. build (features: ${BUILD_FEATURES:-<none/CPU>}, CUDA_ARCH=$CUDA_ARCH, toolchain $TOOLCHAIN) =="
# Blackwell needs a recent CUDA toolkit (>=12.8). stark-backend's cuda-builder reads $CUDA_ARCH
# (verified: crates/cuda-builder get_cuda_arch) and emits `-gencode arch=compute_$A,code=sm_$A`
# plus PTX for forward-compat. Do NOT also set NVCC_PREPEND_FLAGS=-arch=... — it double-specifies
# the arch and conflicts with cuda-builder's own -gencode. CUDA_ARCH alone is correct (and it
# auto-detects from the GPU if unset).
export CUDA_ARCH
feat_args=()
[[ -n "$BUILD_FEATURES" ]] && feat_args=(--features "$BUILD_FEATURES")
( cd "$OPENVM_ETH_DIR" && \
    RUSTFLAGS="${RUSTFLAGS:--Ctarget-cpu=native}" \
    rustup run "$TOOLCHAIN" cargo build --release --bin openvm-reth-benchmark "${feat_args[@]}" )

BIN="$OPENVM_ETH_DIR/target/release/openvm-reth-benchmark"
[[ -x "$BIN" ]] || { echo "ERROR: build produced no binary at $BIN" >&2; exit 1; }

# Expose the binary + runner to the per-block scripts via a small env file.
cat > "$ROOT/cluster/box-env.sh" <<EOF
# generated by 00-install-once.sh — sourced by submit.sh / 01-keygen.sh
export OPENVM_BIN="$BIN"
export OPENVM_ETH_DIR="$OPENVM_ETH_DIR"
export RUNNER="$ROOT/openvm-runner"
export CUDA_ARCH="$CUDA_ARCH"
export OPENVM_KEYS_DIR="$ROOT/keys"
# CANONICAL prove/keygen flags — keygen MUST use the SAME blowups as prove or the binary
# rejects the loaded keys (vm_config mismatch assert). Single source of truth for both.
export OPENVM_PROVE_EXTRA_FLAGS="--app-log-blowup 1 --leaf-log-blowup 1 --internal-log-blowup 2 --root-log-blowup 3 --max-segment-length 4194304 --segment-max-cells 1200000000"
EOF
echo "wrote cluster/box-env.sh"

echo "== 5. keygen (one-time) =="
echo "IMPORTANT for benchmarking: generate the proving keys ONCE so per-block prove timing"
echo "EXCLUDES keygen (the OpenVM analog of SP1 setup / cargo-zisk setup). Otherwise keygen"
echo "runs in-band and inflates prove_secs."
echo "  RPC_1=<archive-rpc> ./cluster/01-keygen.sh 20000000"
echo "submit.sh then auto-detects \$OPENVM_KEYS_DIR and passes --app-pk-path/--agg-pk-path."

echo ""
echo "== done =="
echo "Binary : $BIN"
echo "Smoke  : OPENVM_BIN=$BIN $ROOT/openvm-runner --mode execute --block 20000000 --rpc \$RPC_1"
echo "Prove  : NUM_GPUS=\$(nvidia-smi -L | wc -l) ./cluster/submit.sh 20000000"
