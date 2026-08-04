#!/usr/bin/env bash
# aggregate.sh — finish a run that stopped after the app-proof phase (submit.sh SKIP_AGGREGATE=1).
# Reads the segment proofs kept in <run>/segments/ and builds the final STARK, updating the SAME
# run record (timing.txt · report.json · proof.json · aggregate.log).
#
#   ./aggregate.sh runs/mg-1-20000000-<ts>
#
# Aggregation is single-GPU by construction (path ① only shards the app phase), so this costs the
# same whether the app phase ran on 1 or 8 GPUs. Splitting it out is the point: you get the app
# proofs first, then decide whether to pay the tail.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
[[ -f box-env.sh ]] && { . ./box-env.sh; } || echo "WARN: cluster/box-env.sh missing (run 00-install-once.sh)" >&2

RUN="${1:?usage: ./aggregate.sh <run-dir>   (e.g. runs/mg-1-20000000-20260731-101500Z)}"
RUN="${RUN%/}"
segdir="$RUN/segments"
[[ -d "$segdir" ]] || { echo "ERROR: no $segdir — nothing to aggregate (was SKIP_AGGREGATE used?)" >&2; exit 1; }
[[ -f "$RUN/proof.json" ]] && { echo "$RUN already has proof.json — delete it to re-aggregate. Done."; exit 0; }

# Block/chain come from the run's own env.txt ("block=N chain=C num_gpus=K date=…"), so the
# aggregate is provably about the same block as the workers.
BLOCK="$(sed -n 's/^block=\([0-9]*\).*/\1/p' "$RUN/env.txt" 2>/dev/null | head -1)"
CHAIN_ID="$(sed -n 's/^block=[0-9]* chain=\([0-9]*\).*/\1/p' "$RUN/env.txt" 2>/dev/null | head -1)"
[[ -n "$BLOCK" ]] || { echo "ERROR: cannot read block= from $RUN/env.txt" >&2; exit 1; }
CHAIN_ID="${CHAIN_ID:-1}"

BIN="${OPENVM_BIN:-openvm-reth-benchmark}"
CACHE="${OPENVM_CACHE_DIR:-$ROOT/inputs/openvm-reth/rpc-cache}"
KEYS="${OPENVM_KEYS_DIR:-$ROOT/keys}"
RPC="${RPC_URL:-${RPC_1:-}}"
extra="${OPENVM_PROVE_EXTRA_FLAGS:-}"

common=(--block-number "$BLOCK" --chain-id "$CHAIN_ID" --cache-dir "$CACHE" --skip-comparison)
[[ -n "$RPC" ]] && common+=(--rpc-url "$RPC")
# MUST match the flags/keys the workers used, or the binary rejects the loaded keys.
if [[ -f "$KEYS/app_pk.bitcode" && -f "$KEYS/agg_pk.bitcode" ]]; then
  common+=(--app-pk-path "$KEYS/app_pk.bitcode" --agg-pk-path "$KEYS/agg_pk.bitcode")
else
  echo "WARN: no keys in $KEYS — keygen runs in-band and inflates aggregate_secs." >&2
fi

nseg=$(ls "$segdir"/seg-*.bitcode 2>/dev/null | wc -l | tr -d ' ')
echo "== aggregate block $BLOCK ($nseg segment proofs) -> $RUN =="

if command -v nvidia-smi >/dev/null 2>&1; then
  ( while :; do nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used --format=csv,noheader,nounits; sleep 2; done ) >> "$RUN/gpu-util.csv" 2>/dev/null &
  SAMPLER=$!
  trap '[[ -n "${SAMPLER:-}" ]] && kill "$SAMPLER" 2>/dev/null || true' EXIT
fi

ta=$(date +%s)
# shellcheck disable=SC2086
RUST_LOG="${RUST_LOG:-info}" "$BIN" --mode aggregate --segments-out "$segdir" --output-dir "$RUN" \
  "${common[@]}" $extra > "$RUN/aggregate.log" 2>&1 \
  || { echo ">>> aggregate FAILED — see $RUN/aggregate.log" >&2; exit 1; }
t2=$(date +%s)
agg_secs=$((t2 - ta))
[[ -n "${SAMPLER:-}" ]] && { kill "$SAMPLER" 2>/dev/null || true; }

echo "block_hash (aggregate): $(grep -oE 'block_hash \(aggregate\): [0-9a-f]+' "$RUN/aggregate.log" | grep -oE '[0-9a-f]{64}' | head -1)"

# Update the run record in place: timing.txt (aggregate_secs / total_secs) + report.json.
proof_bytes=""; [[ -f "$RUN/proof.json" ]] && proof_bytes="$(wc -c < "$RUN/proof.json" | tr -d ' ')"
python3 - "$RUN/timing.txt" "$RUN/report.json" "$agg_secs" "$proof_bytes" <<'PY'
import json, os, sys
timing, report, agg, pbytes = sys.argv[1:5]
agg = int(agg); pbytes = int(pbytes) if pbytes else None

workers = None
lines = []
if os.path.exists(timing):
    for line in open(timing):
        k, _, v = line.strip().partition("=")
        if k == "workers_secs":
            workers = int(v)
        if k in ("aggregate_secs", "total_secs"):
            continue
        lines.append(line.rstrip("\n"))
total = (workers + agg) if workers is not None else agg
# workers_secs first, then the two we just recomputed, then whatever else was there.
out = [l for l in lines if l.startswith("workers_secs=")]
out += [f"aggregate_secs={agg}", f"total_secs={total}"]
out += [l for l in lines if not l.startswith("workers_secs=")]
open(timing, "w").write("\n".join(out) + "\n")
print("\n".join(out))

if os.path.exists(report):
    d = json.load(open(report))
    d.update({"mode": "prove-stark", "aggregate_secs": agg,
              "prove_secs": total, "total_secs": total, "proof_bytes": pbytes})
    json.dump(d, open(report, "w"), indent=2)
PY
echo "OK — $RUN/proof.json"
