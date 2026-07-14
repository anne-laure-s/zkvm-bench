#!/usr/bin/env bash
#
# mint-inputs.sh — batch-mint RSP block witnesses into guests/rsp/inputs/ (default: 10 pre-Pectra blocks).
#
# Pre-Pectra blocks (< ~22.4M) have no EIP-7702 txs, so they avoid the RSP gas
# mismatch bug (issue #181). Each block is minted via ./run gen-input (writes the
# witness + an exec-report into guests/rsp/inputs/).
#
# Usage:
#   RSP_DIR=/path/to/rsp RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<KEY> \
#     ./scripts/mint-inputs.sh [BLOCK ...]
#
# Without explicit BLOCKs, uses the curated pre-Pectra list below. Failing
# blocks are skipped (the batch keeps going) and listed in the summary.
set -uo pipefail   # deliberately NOT -e: continue past a failing block

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RSP_DIR="${RSP_DIR:?set RSP_DIR to your RSP checkout}"
CHAIN_ID="${CHAIN_ID:-1}"

# RPC_URL is optional: if unset, RSP falls back to RPC_<chain_id> from its own
# environment / $RSP_DIR/.env. Either way the endpoint must serve eth_getProof.
if [[ -z "${RPC_URL:-}" && ! -f "$RSP_DIR/.env" ]]; then
  echo "ERROR: set RPC_URL=... or put RPC_${CHAIN_ID}=... in $RSP_DIR/.env" >&2
  exit 1
fi

# 10 pre-Pectra mainnet blocks, spread across 2023–2025 for cycle-count variety.
DEFAULT_BLOCKS=(18884864 19000000 19500000 20000000 20250000
                20500000 21000000 21250000 21500000 22000000)
BLOCKS=("$@"); [[ ${#BLOCKS[@]} -eq 0 ]] && BLOCKS=("${DEFAULT_BLOCKS[@]}")

ok=(); fail=()
for B in "${BLOCKS[@]}"; do
  echo "==================== block $B ===================="
  # Build a non-empty arg array (safe under `set -u`, incl. bash 3.2). Pass
  # RPC_URL only when set; otherwise RSP uses its RPC_<chain_id>/.env fallback.
  run_args=(gen-input GUEST=rsp BLOCK="$B")
  [[ -n "${RPC_URL:-}" ]] && run_args+=(RPC_URL="$RPC_URL")
  if RSP_DIR="$RSP_DIR" CHAIN_ID="$CHAIN_ID" "$ROOT/run" "${run_args[@]}"; then
    ok+=("$B")
  else
    echo "WARN: block $B failed — skipping" >&2
    fail+=("$B")
  fi
done

echo "==================== summary ===================="
echo "OK   (${#ok[@]}): ${ok[*]:-—}"
echo "FAIL (${#fail[@]}): ${fail[*]:-—}"
echo "Inputs in: guests/rsp/inputs/"
