# guests/openvm-reth/guest.sh — guest contract for OpenVM's reth EVM block prover.
#
# Wraps the official openvm-eth checkout (axiom-crypto/openvm-eth), specifically its
# `bin/reth-benchmark` (= openvm-reth-benchmark). The checkout stays SEPARATE; this is a
# thin adapter.
#
# OpenVM specifics vs the SP1/ZisK guests:
#   * The guest ELF is `include_bytes!`'d into the prover binary — there is no separate
#     ELF to build/ship. "Building the guest" == building the openvm-reth-benchmark binary.
#   * The block witness is minted from an archive RPC by block number (cached under
#     rpc-cache/). There is no separate witness file to ship — only the block number is
#     the cross-zkVM interface.
#
# Sourced by ./run. Available globals: ROOT, BLOCK, CHAIN_ID, RPC_URL, IN_DIR, RUNNER.
# Params via env:
#   OPENVM_ETH_DIR  Path to the openvm-eth checkout (default: ../../vendor/openvm-eth)
#   CUDA=1          Build the CUDA binary (box only; default off = CPU build for execute)
#   MULTI_GPU_PATCH=1  Apply the vendored multi-GPU patch before building (see docs/)
#
# ⚠️ openvm-eth + openvm SDK move fast; flags/paths drift. Confirm with
# `openvm-reth-benchmark --help` and adjust (or override via env knobs).

# guest_build_elf — build the RISC-V guest ELF (`cargo openvm build`) and copy it to $ELF
# (guests/openvm-reth/openvm-reth.elf) for visibility, PLUS into the spot the
# prover binary `include_bytes!`s it. Cross-compiles to riscv32im-risc0-zkvm-elf — NO GPU needed.
# Requires cargo-openvm (see the ERROR hint below for install).
guest_build_elf() {
  local dir="${OPENVM_ETH_DIR:-$ROOT/../../vendor/openvm-eth}"
  local crate="${GUEST_CRATE_DIR:-$dir/bin/stateless-guest}"
  [[ -d "$crate" ]] || { echo "ERROR: guest crate not found: $crate" >&2; return 1; }
  command -v cargo-openvm >/dev/null 2>&1 || { echo "ERROR: cargo-openvm not installed.
  Install (rev-matched, reuses the vendored checkout):
    cargo install --locked --path $dir/.vendor/openvm/crates/cli
  or from git:
    cargo install --locked --git https://github.com/openvm-org/openvm.git cargo-openvm" >&2; return 1; }
  echo "== cargo openvm build (guest -> riscv32im-risc0-zkvm-elf) =="
  ( cd "$crate" && OPENVM_RUST_TOOLCHAIN="${OPENVM_RUST_TOOLCHAIN:-nightly-2026-01-01}" cargo openvm build )
  local src="$crate/target/riscv32im-risc0-zkvm-elf/release/openvm-stateless-guest"
  [[ -f "$src" ]] || { echo "ERROR: guest ELF not produced at $src" >&2; return 1; }
  cp "$src" "$ELF"                                                       # guests/openvm-reth/openvm-reth.elf (visible artifact)
  mkdir -p "$dir/bin/reth-benchmark/elf"
  cp "$src" "$dir/bin/reth-benchmark/elf/openvm-stateless-guest"         # where the host binary embeds it
  git -C "$dir" rev-parse HEAD > "${ELF%.elf}.commit" 2>/dev/null || echo unknown > "${ELF%.elf}.commit"
  echo "openvm-eth commit: $(cat "${ELF%.elf}.commit")"
  echo "Guest ELF: $ELF ($(wc -c < "$ELF" | tr -d ' ') bytes, RISC-V)"
}

# guest_build_bin — build openvm-reth-benchmark. CPU build by default (enough for
# `execute`/cycle count on the Mac). The CUDA + multi-GPU build for the box is done by
# cluster/00-install-once.sh (which also vendors + patches openvm/stark-backend).
guest_build_bin() {
  local dir="${OPENVM_ETH_DIR:-$ROOT/../../vendor/openvm-eth}"
  [[ -d "$dir" ]] || { echo "ERROR: openvm-eth checkout not found: $dir
  Clone it:  git clone https://github.com/axiom-crypto/openvm-eth $dir" >&2; return 1; }
  local feats=()
  [[ "${CUDA:-0}" == 1 ]] && feats+=(cuda)
  if [[ "${MULTI_GPU_PATCH:-0}" == 1 ]]; then
    echo "== applying multi-GPU patch (see docs/openvm-multigpu.md) =="
    "$ROOT/patches/apply.sh" "$dir"
  fi
  echo "== building openvm-reth-benchmark (features: ${feats[*]:-none/CPU}) =="
  ( cd "$dir" && cargo build --release --bin openvm-reth-benchmark \
      ${feats:+--features "$(IFS=,; echo "${feats[*]}")"} )
  local bin="$dir/target/release/openvm-reth-benchmark"
  [[ -x "$bin" ]] || { echo "ERROR: build produced no binary at $bin" >&2; return 1; }
  git -C "$dir" rev-parse HEAD > "$GUESTS_DIR/$GUEST/$GUEST.commit" 2>/dev/null || echo unknown > "$GUESTS_DIR/$GUEST/$GUEST.commit"
  echo "openvm-eth commit: $(cat "$GUESTS_DIR/$GUEST/$GUEST.commit")"
  echo "Binary: $bin"
  echo "Use it:  OPENVM_BIN=$bin ./run execute BLOCK=$BLOCK RPC_URL=\$RPC_1"
}

# guest_gen_input — mint the OpenVM block witness via `--mode make-input`: the host fetches the
# stateless input from a standard archive RPC (native reth execution + the rpc-proxy that
# synthesizes debug_executionWitness — Alchemy/Infura OK, NO debug node needed) and, thanks to
# --cache-dir, writes it to <cache>/input/<chain>/<block>.bin — the exact file the prove step
# reads. make-input STOPS right after (no zkVM execution, no proving), so it is FAST even for
# 500M-cycle blocks. Cycle counts come later from the prove report.json (or `./run execute`).
# With RSYNC_CACHE=1 the prove step pushes this cache to the box to avoid a refetch.
guest_gen_input() {
  : "${BLOCK:?set BLOCK}" "${RPC_URL:?set RPC_URL=<archive RPC; put it in openvm-eth/.env as RPC_1>}"
  local dir="${OPENVM_ETH_DIR:-$ROOT/../../vendor/openvm-eth}"
  local bin="${OPENVM_BIN:-$dir/target/release/openvm-reth-benchmark}"
  [[ -x "$bin" ]] || { echo "ERROR: binary not found: $bin (build it: ./run build-bin GUEST=$GUEST)" >&2; return 1; }
  local cache="${OPENVM_CACHE_DIR:-$IN_DIR/rpc-cache}"
  local tag="${CHAIN_ID}-${BLOCK}"
  local cbin="$cache/input/$CHAIN_ID/$BLOCK.bin"
  local json="$IN_DIR/$tag.input.json"
  mkdir -p "$cache" "$IN_DIR"
  if [[ -f "$cbin" && -z "${FORCE:-}" ]]; then
    echo "cached ✓ $cbin ($(wc -c < "$cbin" | tr -d ' ') bytes) — set FORCE=1 to refetch"; return 0
  fi
  echo "== make-input (fetch witness via standard RPC, native reth, no zkVM) block $BLOCK =="
  ( cd "$dir" && "$bin" --mode make-input --block-number "$BLOCK" --chain-id "$CHAIN_ID" \
      --rpc-url "$RPC_URL" --cache-dir "$cache" --generated-input-path "$json" \
      ${OPENVM_MAKEINPUT_EXTRA_FLAGS:-} )
  [[ -f "$cbin" ]] || { echo "ERROR: no witness cached at $cbin (fetch failed?)" >&2; return 1; }
  echo "Witness: $cbin ($(wc -c < "$cbin" | tr -d ' ') bytes)"
  echo "Cache : $cache  (push to box with: ./run prove … RSYNC_CACHE=1)"
}

# guest_decode_pv — OpenVM commits the validated block outputs as public values. Until a
# typed decoder exists, hex-dump for manual comparison of the committed block hash against
# a trusted source (same posture as the RSP / ZisK adapters).
guest_decode_pv() {
  echo "OpenVM reth public outputs. For a real block proof, compare the committed block"
  echo "hash to a trusted source. Hex dump (first 256 bytes):"
  xxd -l 256 "$1" 2>/dev/null || od -A x -t x1z "$1" | head -16 || true
}
