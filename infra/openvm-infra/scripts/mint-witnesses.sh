#!/usr/bin/env bash
# mint-witnesses.sh — batch-mint OpenVM block witnesses (--mode make-input) into the local
# rpc-cache, ready to rsync to the box. Runs on the Mac / any CPU host (NO GPU). Resume-able
# (skips blocks already cached), continues on per-block failure, prints a summary.
#
# Blocks: by default a built-in benchmark set (self-contained — this stack reads no other
# stack's inputs). Override with BLOCKS="a b c".
# RPC: RPC_URL if set, else RPC_1 from openvm-eth/.env (put your Alchemy archive URL there).
#
#   ./scripts/mint-witnesses.sh                      # the built-in benchmark set
#   BLOCKS="20000000 24626900" ./scripts/mint-witnesses.sh
#   MINT_DELAY=3 ./scripts/mint-witnesses.sh         # throttle between blocks (Alchemy CU)
#
# ⚠️ DO NOT raise --preimage-cache-nibbles above the default 7 (via OPENVM_MAKEINPUT_EXTRA_FLAGS):
# the rpc-proxy brute-forces keccak256 to fill 16^N buckets, REBUILT PER BLOCK. 7 -> 8 is ~16x
# more hashing (16^8 ≈ 4.3B buckets) and pegs the CPU for hours PER block. If a block fails with
# "provider error" at the default, it's a proxy limitation — retry as-is (transient), else skip it.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ETH="${OPENVM_ETH_DIR:-$ROOT/../../vendor/openvm-eth}"
CHAIN_ID="${CHAIN_ID:-1}"
CACHE="${OPENVM_CACHE_DIR:-$ROOT/../../guests/openvm-reth/inputs/rpc-cache}"

# RPC: explicit RPC_URL wins, else load RPC_1 from openvm-eth/.env.
if [[ -z "${RPC_URL:-}" ]]; then
  [[ -f "$ETH/.env" ]] && { set -a; . "$ETH/.env"; set +a; }
  RPC_URL="${RPC_1:-}"
fi
[[ -n "${RPC_URL:-}" ]] || { echo "ERROR: no RPC. Put your Alchemy archive URL in $ETH/.env as RPC_1=… or export RPC_URL." >&2; exit 1; }

# Block list: explicit BLOCKS, else the built-in benchmark set — self-contained, this stack
# does not read any other stack's inputs. Edit this list to change the default set.
if [[ -z "${BLOCKS:-}" ]]; then
  BLOCKS="5000000 10000000 13000000 15537394 17000000 18000000 18884864 19000000 19426587 \
20000000 20250000 20500000 20750000 21000000 21250000 21500000 21750000 22000000 22200000 \
22300000 24626900 24628590 24628595 24628607 24628608 24628611 24647140 24697070 24697073 \
25229951 25229957 25367437 25367820 19500000"
fi
[[ -n "${BLOCKS// }" ]] || { echo "ERROR: no blocks to mint (set BLOCKS=…)" >&2; exit 1; }

total="$(echo $BLOCKS | wc -w | tr -d ' ')"
echo "== minting $total witnesses -> $CACHE =="
echo "RPC: $(echo "$RPC_URL" | sed -E 's#(https?://[^/]+/v2/).*#\1***#')"
have=0; minted=0; fail=0; failed=""
i=0
for b in $BLOCKS; do
  i=$((i+1))
  cbin="$CACHE/input/$CHAIN_ID/$b.bin"
  printf '\n[%d/%d] block %s\n' "$i" "$total" "$b"
  if [[ -f "$cbin" ]]; then
    echo "  cached ✓ ($(wc -c < "$cbin" | tr -d ' ') bytes) — skip"; have=$((have+1)); continue
  fi
  if RPC_URL="$RPC_URL" OPENVM_CACHE_DIR="$CACHE" CHAIN_ID="$CHAIN_ID" \
       ./run gen-input GUEST=openvm-reth BLOCK="$b"; then
    if [[ -f "$cbin" ]]; then minted=$((minted+1)); else fail=$((fail+1)); failed="$failed $b"; fi
  else
    fail=$((fail+1)); failed="$failed $b"; echo "  !! failed block $b (continuing)"
  fi
  sleep "${MINT_DELAY:-1}"
done

echo ""
echo "== summary =="
echo "  already cached : $have"
echo "  newly minted   : $minted"
echo "  failed         : $fail${failed:+ ->$failed}"
echo "  cache dir      : $CACHE"
echo "Ship to the box:  rsync -az -e ssh $CACHE/ user@host:/workspace/openvm/openvm-infra/inputs/openvm-reth/rpc-cache/"
echo "   (or per-prove: ./run prove BLOCK=<n> REMOTE=user@host RSYNC_CACHE=1)"
[[ "$fail" == 0 ]]
