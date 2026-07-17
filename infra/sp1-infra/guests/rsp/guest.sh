# guests/rsp/guest.sh — guest contract for RSP (EVM block execution).
#
# RSP stays a SEPARATE local checkout; this is a thin adapter. The remote prover
# never needs RSP — only the ELF this produces.
#
# Sourced by ./run. Available globals: ROOT, ELF, INPUT.
# Params via env:
#   RSP_DIR       Path to the RSP checkout (required for build-elf / gen-input)
#   BLOCK         Block number (required)
#   CHAIN_ID      Chain id (default: 1 = Ethereum mainnet)
#   RPC_URL       Archive-node RPC (else RSP falls back to RPC_<chain_id>)
#   RSP_HOST_BIN  Host binary name (default: rsp)
#   RSP_CLIENT_DIR Guest crate dir (default: $RSP_DIR/bin/client)

# Canonical id: <chain>-<block> (matches RSP's own cache layout).
guest_tag() { echo "${CHAIN_ID:-1}-${BLOCK:?set BLOCK=<block number>}"; }

# Build the RSP guest ELF and place it at $ELF. Records the RSP commit so the
# ELF and inputs can be pinned to the same version.
guest_build_elf() {
  local rsp="${RSP_DIR:?set RSP_DIR to your RSP checkout}"
  local client="${RSP_CLIENT_DIR:-$rsp/bin/client}"
  ( cd "$client" && cargo prove build )
  local p
  p="$(find "$rsp" -path '*/elf-compilation/*/release/rsp-client' -type f 2>/dev/null | head -n1)"
  [[ -n "$p" ]] || { echo "ERROR: built rsp-client ELF not found under $rsp" >&2; return 1; }
  cp "$p" "$ELF"
  git -C "$rsp" rev-parse HEAD > "${ELF%.elf}.commit" 2>/dev/null || echo unknown > "${ELF%.elf}.commit"
  echo "RSP commit: $(cat "${ELF%.elf}.commit")"
}

# Generate the block witness LOCALLY (execute-only) and copy it to $INPUT.
# This is the byte-identical bincode(EthClientExecutorInput) the guest reads.
guest_gen_input() {
  # Resolve RSP_DIR to an ABSOLUTE path: gen-input runs RSP after `cd "$rsp"`, so a
  # relative --cache-dir (e.g. "vendor/rsp/cache") would double to "$rsp/$rsp/cache"
  # — RSP writes the input there while the cache_file check below looks at the
  # (repo-root-relative) path and misses it. Absolute keeps both sides in sync.
  local rsp; rsp="$(cd "${RSP_DIR:?set RSP_DIR to your RSP checkout}" && pwd)"
  local chain="${CHAIN_ID:-1}"
  local cache="$rsp/cache"
  local args=(--block-number "${BLOCK:?set BLOCK}" --chain-id "$chain" --cache-dir "$cache")
  [[ -n "${RPC_URL:-}" ]] && args+=(--rpc-url "$RPC_URL")
  ( cd "$rsp" && cargo run --release --bin "${RSP_HOST_BIN:-rsp}" -- "${args[@]}" )
  local cache_file="$cache/input/$chain/${BLOCK}.bin"
  [[ -f "$cache_file" ]] || { echo "ERROR: RSP cache file not found: $cache_file" >&2; return 1; }
  cp "$cache_file" "$INPUT"
}

# Public values are bincode(CommittedHeader). A typed decoder can live under
# rsp-tools/ (depends on rsp-primitives); until then, hex dump for manual
# comparison of the committed block hash against a trusted source.
guest_decode_pv() {
  echo "CommittedHeader (bincode). For a real block proof, compare its block hash"
  echo "to a trusted source. Hex dump (first 256 bytes):"
  xxd -l 256 "$1" || true
}
