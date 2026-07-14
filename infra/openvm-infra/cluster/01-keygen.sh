#!/usr/bin/env bash
# 01-keygen.sh — ONE-TIME: generate + save the app/agg proving keys so that per-block prove
# timing EXCLUDES keygen. This is the OpenVM analog of SP1's setup / ZisK's `cargo-zisk setup`.
#
# Mechanism: run the prover binary in `generate-fixtures` mode, which writes app_pk.bitcode +
# agg_pk.bitcode (bitcode == the exact format --app-pk-path/--agg-pk-path load) into a dir.
# One-time cost = a single-GPU prove of one block (side-effect: also drops proof fixtures).
#
# ⚠️ The keys are tied to the proving config: keygen MUST use the SAME blowup flags as prove,
# else the binary rejects them at load time ("vm_config mismatch"). Both read the canonical
# $OPENVM_PROVE_EXTRA_FLAGS from cluster/box-env.sh — do not override one without the other.
#
#   RPC_1=<archive-rpc> ./01-keygen.sh [block]      # default block 20000000; needs RPC or warm cache
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
[[ -f box-env.sh ]] && . ./box-env.sh || echo "WARN: cluster/box-env.sh missing (run 00-install-once.sh)" >&2

OPENVM_BIN="${OPENVM_BIN:-openvm-reth-benchmark}"
# The keys are config-derived (block-independent), so use the SMALLEST cached block to keygen
# fastest — generate-fixtures does a full single-GPU prove of it (which also smoke-tests 1-GPU proving).
BLOCK="${1:-${KEYGEN_BLOCK:-20000000}}"
CHAIN_ID="${CHAIN_ID:-1}"
RPC="${RPC_URL:-${RPC_1:-}}"
CACHE="${OPENVM_CACHE_DIR:-$ROOT/inputs/openvm-reth/rpc-cache}"
KEYS="${OPENVM_KEYS_DIR:-$ROOT/keys}"
mkdir -p "$KEYS" "$CACHE"

if [[ -f "$KEYS/app_pk.bitcode" && -f "$KEYS/agg_pk.bitcode" ]]; then
  echo "keys already present in $KEYS — delete them to regenerate. Done."
  exit 0
fi
[[ -n "$RPC" ]] || echo "WARN: no RPC_1/RPC_URL — only works if block $BLOCK is already cached in $CACHE" >&2

echo "== keygen (one-time, generate-fixtures) block $BLOCK -> $KEYS =="
echo "flags: ${OPENVM_PROVE_EXTRA_FLAGS:-<none>}"
# shellcheck disable=SC2086
OUTPUT_PATH=/dev/null RUST_LOG="${RUST_LOG:-info}" \
  "$OPENVM_BIN" --mode generate-fixtures --block-number "$BLOCK" --chain-id "$CHAIN_ID" \
    ${RPC:+--rpc-url "$RPC"} --cache-dir "$CACHE" --fixtures-path "$KEYS" \
    ${OPENVM_PROVE_EXTRA_FLAGS:-}

[[ -f "$KEYS/app_pk.bitcode" && -f "$KEYS/agg_pk.bitcode" ]] || {
  echo "ERROR: app_pk.bitcode / agg_pk.bitcode not produced in $KEYS" >&2
  echo "       (check the generate-fixtures output above; the fixtures file names may have" >&2
  echo "        drifted — set OPENVM_APP_PK/OPENVM_AGG_PK manually to whatever it wrote)" >&2
  exit 1
}
echo "== keys ready =="
echo "  $KEYS/app_pk.bitcode"
echo "  $KEYS/agg_pk.bitcode"
echo "submit.sh auto-detects \$OPENVM_KEYS_DIR ($KEYS). Per-block prove now excludes keygen."
