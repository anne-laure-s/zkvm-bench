#!/usr/bin/env bash
# prove-all.sh — prove EVERY cached block in sequence (multi-GPU, path ①), one full run record each,
# ON THE BOX. Resume-able (skips blocks that already have a successful proof), continues on
# per-block failure, prints a summary. Each block goes through submit.sh (path ①), so every run
# gets its timing.txt · worker-*.log · aggregate.log · gpu-util.csv · proof.json · env.txt.
#
#   NUM_GPUS=8 ./prove-all.sh                 # all cached blocks, multi-GPU (path ①)
#   BLOCKS="20000000 20500000" ./prove-all.sh # a subset
#
# 💸 Anti-waste: if the FIRST attempted block fails, the batch ABORTS (a first-block failure is
# almost always setup — keys/cache/build, not a per-block issue — no point burning GPU on the rest).
# Later per-block failures are logged and skipped. Ctrl-C is safe (resume picks up where it stopped).
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
[[ -f box-env.sh ]] && { . ./box-env.sh; } || echo "WARN: cluster/box-env.sh missing (run 00-install-once.sh)" >&2

CHAIN_ID="${CHAIN_ID:-1}"
CACHE="${OPENVM_CACHE_DIR:-$ROOT/inputs/openvm-reth/rpc-cache}"

# Blocks: explicit BLOCKS, else every cached witness (what we minted).
if [[ -z "${BLOCKS:-}" ]]; then
  BLOCKS="$(ls "$CACHE/input/$CHAIN_ID/"*.bin 2>/dev/null | sed 's#.*/##; s#\.bin$##' | grep -E '^[0-9]+$' | sort -n | tr '\n' ' ')"
fi
[[ -n "${BLOCKS// }" ]] || { echo "ERROR: no cached blocks under $CACHE/input/$CHAIN_ID/ (rsync the witnesses first)" >&2; exit 1; }

total="$(echo $BLOCKS | wc -w | tr -d ' ')"
echo "== proving $total blocks (multi-GPU path ①, NUM_GPUS=${NUM_GPUS:-auto}) =="
ok=0 skip=0 fail=0 failed="" attempted=0
i=0
for b in $BLOCKS; do
  i=$((i+1))
  # Resume: skip if a successful proof already exists for this block (path ① run record layout).
  if ls "runs/mg-${CHAIN_ID}-${b}-"*/proof.json >/dev/null 2>&1; then
    echo "[$i/$total] block $b — already proven, skip"; skip=$((skip+1)); continue
  fi
  echo ""; echo "===== [$i/$total] block $b ====="
  if ./submit.sh "$b"; then
    ok=$((ok+1)); attempted=$((attempted+1))
  else
    fail=$((fail+1)); failed="$failed $b"
    if [[ "$attempted" == 0 ]]; then
      echo ""; echo ">>> FIRST prove FAILED (block $b) — almost always setup (keys/cache/build), not this block." >&2
      echo ">>> Batch ABORTED to save GPU. Inspect the worker/aggregate logs:" >&2
      echo ">>>   runs/mg-${CHAIN_ID}-${b}-*/worker-*.log  and  .../aggregate.log" >&2
      exit 1
    fi
    attempted=$((attempted+1))
    echo ">>> block $b failed — continuing (see runs/mg-${CHAIN_ID}-${b}-*/worker-*.log)" >&2
  fi
done

echo ""
echo "== summary =="
echo "  proven now     : $ok"
echo "  already done   : $skip"
echo "  failed         : $fail${failed:+ ->$failed}"
echo "  run records    : $(cd runs 2>/dev/null && pwd || echo runs/)"
echo "Fetch to the Mac (compressed):"
echo "  ssh -p \$PORT \$HOST 'tar czf - -C $(cd .. && pwd)/cluster runs 2>/dev/null' | tar xzf - -C infra/openvm-infra/cluster/"
