# guests/zisk-reth/guest.sh — guest contract for ZisK's reth EVM block prover.
#
# Wraps the official zisk-eth-client checkout (the RSP analog), specifically its
# `stateless-validator-reth` guest. The checkout stays SEPARATE; this is a thin
# adapter run LOCALLY (on the Mac). The remote box never needs zisk-eth-client —
# only the ELF + witness (input + hints) this produces.
#
# Sourced by ./run. Available globals: ROOT, ELF, INPUT, HINTS.
# Params via env:
#   ZISK_ETH_DIR   Path to the zisk-eth-client checkout (required for build-elf / gen-input)
#   BLOCK          Block number (required)
#   CHAIN_ID       Chain id (default: 1 = Ethereum mainnet)
#   RPC_URL        Archive-node RPC (eth_getProof capable; required for gen-input)
#   GUEST_CRATE_DIR  Guest crate dir (default: $ZISK_ETH_DIR/bin/guests/stateless-validator-reth)
#   ZISK_CLIENT_KIND Client kind passed to the host tools (default: reth)
#
# ⚠️ zisk-eth-client + ZisK are alpha; subcommand/flag names drift. The exact
# input-gen/hints-gen invocations below are the documented shape — confirm with
# `--help` in your checkout and adjust (or override via the env knobs).

# Canonical id: <chain>-<block> (mirrors RSP's cache layout for cross-comparison).
# In SAMPLE mode the block is parsed from the committed file name
# (<chain>_<block>_<txs>_<mgas>_zec_<client>.bin) when BLOCK isn't given.
guest_tag() {
  local blk="${BLOCK:-}"
  if [[ -z "$blk" && -n "${SAMPLE:-}" ]]; then blk="$(basename "$SAMPLE" | awk -F_ '{print $2}')"; fi
  : "${blk:?set BLOCK=<block number> (or SAMPLE=<path to a committed sample .bin>)}"
  echo "${CHAIN_ID:-1}-${blk}"
}

# Build the ZisK reth guest ELF -> $ELF. Records the zisk-eth-client commit so the
# ELF and inputs can be pinned to the same version (mirrors RSP's .commit file).
guest_build_elf() {
  local dir="${ZISK_ETH_DIR:?set ZISK_ETH_DIR to your zisk-eth-client checkout}"
  local crate="${GUEST_CRATE_DIR:-$dir/bin/guests/stateless-validator-reth}"
  [[ -d "$crate" ]] || { echo "ERROR: guest crate not found: $crate" >&2; return 1; }
  ( cd "$crate" && cargo-zisk build --release )
  # Locate the built guest ELF. It is the lone EXECUTABLE file at the top of the
  # release dir (deps, .d and lock files are not executable / live in subdirs).
  # Search under $crate (not $dir) so we never pick a sibling guest's ELF
  # (e.g. stateless-validator-ethrex). For stateless-validator-reth this is `zec-reth`.
  local reldir p=""
  reldir="$(find "$crate" -type d -path '*/elf/riscv64ima-zisk-zkvm-elf/release' 2>/dev/null | head -n1)"
  [[ -n "$reldir" ]] || { echo "ERROR: ZisK release dir not found under $crate (build failed?)" >&2; return 1; }
  if [[ -n "${ZISK_ELF_NAME:-}" ]]; then
    p="$reldir/$ZISK_ELF_NAME"
  else
    for cand in "$reldir"/*; do [[ -f "$cand" && -x "$cand" ]] && { p="$cand"; break; }; done
  fi
  [[ -n "$p" && -f "$p" ]] || { echo "ERROR: built ZisK ELF not found in $reldir (set ZISK_ELF_NAME)" >&2; return 1; }
  cp "$p" "$ELF"
  git -C "$dir" rev-parse HEAD > "${ELF%.elf}.commit" 2>/dev/null || echo unknown > "${ELF%.elf}.commit"
  echo "zisk-eth-client commit: $(cat "${ELF%.elf}.commit")"
  echo "ELF built from: $p"
  echo "REMINDER: after shipping this ELF to the box (~/$(basename "$ELF")), run the per-ELF setup there —"
  echo "  distributed (benchmark): cargo-zisk remote setup -e ~/$(basename "$ELF") --hints --coordinator http://127.0.0.1:7000"
  echo "  local backend          : cluster/01-setup-elf.sh ~/$(basename "$ELF")"
}

# Generate the block witness LOCALLY: (1) input-gen pulls the stateless block input
# from an archive RPC, (2) hints-gen pre-computes the ZisK hints. Both are copied to
# $INPUT and its sibling $HINTS so the file-based consumers can ship them as a pair.
guest_gen_input() {
  local dir="${ZISK_ETH_DIR:?set ZISK_ETH_DIR to your zisk-eth-client checkout}"
  local kind="${ZISK_CLIENT_KIND:-reth}"
  local crate="${GUEST_CRATE_DIR:-$dir/bin/guests/stateless-validator-reth}"

  # Deduce a committed offline SAMPLE from BLOCK when neither SAMPLE nor RPC_URL is given.
  # The committed inputs are named <chain>_<block>_<txs>_<mgas>_zec_<client>.bin, so the block
  # number selects one. Only the committed blocks work offline; anything else needs the RPC path.
  if [[ -z "${SAMPLE:-}" && -z "${RPC_URL:-}" && -n "${BLOCK:-}" ]]; then
    local sdir="$crate/inputs"
    SAMPLE="$(find "$sdir" -type f -name "*_${BLOCK}_*_zec_${kind}.bin" 2>/dev/null | head -n1)"
    if [[ -z "$SAMPLE" ]]; then
      echo "ERROR: no committed sample for block $BLOCK in $sdir." >&2
      echo "       Available offline blocks:" >&2
      find "$sdir" -type f -name "*_zec_${kind}.bin" 2>/dev/null | awk -F/ '{print $NF}' | awk -F_ '{print "         "$2}' | sort -un >&2
      echo "       For any other block, generate via RPC: add RPC_URL=<node with debug_executionWitness>." >&2
      return 1
    fi
    echo "== deduced sample for block $BLOCK: $SAMPLE =="
  fi

  # ── SAMPLE mode (offline, NO RPC) ──────────────────────────────────────────
  # Use a pre-generated committed input (.bin) and only generate hints natively.
  # Lets you benchmark without a debug_executionWitness node:
  #   ./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client \
  #       SAMPLE=../../vendor/zisk-eth-client/bin/guests/stateless-validator-reth/inputs/mainnet_24626900_221_16_zec_reth.bin
  if [[ -n "${SAMPLE:-}" ]]; then
    local sample_abs; case "$SAMPLE" in /*) sample_abs="$SAMPLE";; *) sample_abs="$PWD/$SAMPLE";; esac
    [[ -f "$sample_abs" ]] || { echo "ERROR: SAMPLE not found: $sample_abs" >&2; return 1; }
    echo "== offline sample (no RPC): $sample_abs =="
    cp "$sample_abs" "$INPUT"
    local hg="$dir/target/release/hints-gen"
    [[ -x "$hg" ]] || ( cd "$dir" && RUSTFLAGS="--cfg zisk_hints" cargo build --release -p hints-gen )
    local hout; hout="$(mktemp -d)"
    echo "== hints-gen (native, no RPC) =="
    "$hg" -c "$kind" -o "$hout" "$sample_abs"
    local src_hints; src_hints="$(find "$hout" -name '*.hints' 2>/dev/null | head -n1)"
    [[ -n "$src_hints" && -e "$src_hints" ]] || { echo "ERROR: hints-gen produced no .hints in $hout" >&2; rm -rf "$hout"; return 1; }
    cp -R "$src_hints" "${HINTS:-${INPUT%.bin}.hints}"
    echo "Input from : $sample_abs"
    echo "Hints from : $src_hints"
    rm -rf "$hout"
    return 0
  fi

  # ── RPC mode (input-gen + hints-gen) ───────────────────────────────────────
  : "${BLOCK:?set BLOCK}" "${RPC_URL:?set RPC_URL=<RPC with debug_executionWitness>}"
  # input-gen writes to <client>-inputs/, hints-gen writes to <client>-hints/ (separate dirs).
  local indir="$dir/${kind}-inputs"
  local hintsdir="$dir/${kind}-hints"

  # ⚠️ The reth input-gen fetches state via `debug_executionWitness` (reth/geth debug RPC),
  # NOT eth_getProof — many hosted providers (Alchemy/Infura) don't expose it. Use a reth
  # node with the debug namespace enabled, or one of the committed sample inputs to test.
  echo "== input-gen (block $BLOCK, client $kind) =="
  ( cd "$dir" && cargo run --release --bin input-gen -- -c "$kind" rpc -u "$RPC_URL" -b "$BLOCK" )

  echo "== hints-gen =="
  ( cd "$dir" && RUSTFLAGS="--cfg zisk_hints" cargo build --release -p hints-gen \
      && ./target/release/hints-gen -f "$indir" )

  # Locate the per-block input + hints. Files are named
  # <chain>_<block>_<txs>_<mgas>_zec_<client>.bin (+ matching .hints in <client>-hints/).
  # Override with ZISK_INPUT_SRC / ZISK_HINTS_SRC if your version names them differently.
  local src_in="${ZISK_INPUT_SRC:-}"
  if [[ -z "$src_in" ]]; then
    src_in="$(find "$indir" -type f -name "*_${BLOCK}_*zec_${kind}.bin" 2>/dev/null | head -n1)"
    [[ -z "$src_in" ]] && src_in="$(find "$indir" -type f -name "*${BLOCK}*.bin" 2>/dev/null | head -n1)"
  fi
  [[ -n "$src_in" && -f "$src_in" ]] || { echo "ERROR: could not find generated input for block $BLOCK under $indir" >&2; return 1; }
  cp "$src_in" "$INPUT"

  local src_hints="${ZISK_HINTS_SRC:-}"
  if [[ -z "$src_hints" ]]; then
    src_hints="$(find "$hintsdir" -type f -name "*${BLOCK}*.hints" 2>/dev/null | head -n1)"
    [[ -z "$src_hints" ]] && src_hints="$(find "$hintsdir" -type f -name "*.hints" -newer "$src_in" 2>/dev/null | head -n1)"
  fi
  if [[ -n "$src_hints" && -e "$src_hints" ]]; then
    cp -R "$src_hints" "${HINTS:-${INPUT%.bin}.hints}"
  else
    echo "WARN: no hints artifact matched for block $BLOCK under $hintsdir — set ZISK_HINTS_SRC" >&2
  fi
  echo "Input from : $src_in"
  echo "Hints from : ${src_hints:-<none>}  (client kind: $kind)"
}

# Public values: the guest commits the validated block header / outputs. Until a
# typed decoder exists, hex-dump for manual comparison of the committed block hash
# against a trusted source (same posture as the RSP adapter).
guest_decode_pv() {
  echo "ZisK reth public outputs. For a real block proof, compare the committed"
  echo "block hash to a trusted source. Hex dump (first 256 bytes):"
  xxd -l 256 "$1" || true
}
