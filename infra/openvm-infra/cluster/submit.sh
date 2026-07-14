#!/usr/bin/env bash
# submit.sh — path ①: prove ONE block using ALL GPUs, WITHOUT the in-process VPMM crash.
# (Parity twin of sp1-infra/zisk-infra submit.sh; the earlier in-process path ② driver — one process
# driving N GPUs — was removed because it crashes at runtime with VPMM cudaErrorInvalidResourceHandle.)
# N worker processes (one per GPU, CUDA_VISIBLE_DEVICES pinned) each prove their shard of the
# block's continuation segments (seg_idx % N == worker_id); then a single `aggregate` step builds
# the final STARK from the union of segment proofs. Each process sees exactly ONE GPU → one CUDA
# context → no cross-device "invalid resource handle". This is the real multi-GPU single-block
# latency (the ethproofs metric).
#
#   NUM_GPUS=8 ./submit.sh <block>
# Timing (timing.txt): workers_secs (the fan-out) + aggregate_secs + total_secs (the latency).
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
[[ -f box-env.sh ]] && { . ./box-env.sh; } || echo "WARN: cluster/box-env.sh missing (run 00-install-once.sh)" >&2

BLOCK="${1:?usage: ./submit.sh <block>}"
CHAIN_ID="${CHAIN_ID:-1}"
N="${NUM_GPUS:-$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')}"
[[ "$N" -ge 1 ]] 2>/dev/null || N=1
BIN="${OPENVM_BIN:-openvm-reth-benchmark}"
CACHE="${OPENVM_CACHE_DIR:-$ROOT/inputs/openvm-reth/rpc-cache}"
KEYS="${OPENVM_KEYS_DIR:-$ROOT/keys}"
RPC="${RPC_URL:-${RPC_1:-}}"

# common args for every invocation. --skip-comparison so no worker/aggregate re-executes the block
# on CPU (that runs before the mode branch and would 8x-inflate everything).
common=(--block-number "$BLOCK" --chain-id "$CHAIN_ID" --cache-dir "$CACHE" --skip-comparison)
[[ -n "$RPC" ]] && common+=(--rpc-url "$RPC")
if [[ -f "$KEYS/app_pk.bitcode" && -f "$KEYS/agg_pk.bitcode" ]]; then
  common+=(--app-pk-path "$KEYS/app_pk.bitcode" --agg-pk-path "$KEYS/agg_pk.bitcode"); keygen_excluded=1
else
  echo "WARN: no keys in $KEYS — keygen runs in-band (per worker!). Run cluster/01-keygen.sh first." >&2
  keygen_excluded=0
fi
# canonical blowups (must match keygen) — word-split intentionally
extra="${OPENVM_PROVE_EXTRA_FLAGS:-}"

tag="${CHAIN_ID}-${BLOCK}"
run="runs/mg-${tag}-$(date -u +%Y%m%d-%H%M%SZ)"
segdir="$run/segments"
mkdir -p "$segdir"
{ echo "block=$BLOCK chain=$CHAIN_ID num_gpus=$N date=$(date -u)"; echo "bin=$BIN"
  echo "openvm=$($BIN --version 2>/dev/null || echo '?')"; echo "--- gpus ---"; nvidia-smi -L 2>/dev/null || echo "(none)"
} > "$run/env.txt"

# background GPU sampler (all GPUs should be busy during the workers phase)
if command -v nvidia-smi >/dev/null 2>&1; then
  ( while :; do nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used --format=csv,noheader,nounits; sleep 2; done ) > "$run/gpu-util.csv" 2>/dev/null &
  SAMPLER=$!
  trap '[[ -n "${SAMPLER:-}" ]] && kill "$SAMPLER" 2>/dev/null || true' EXIT
fi

echo "== multi-GPU prove block $BLOCK across $N GPUs (path ①) -> $run =="
t0=$(date +%s)
pids=()
for g in $(seq 0 $((N - 1))); do
  # shellcheck disable=SC2086
  ( CUDA_VISIBLE_DEVICES="$g" RUST_LOG="${RUST_LOG:-info}" "$BIN" --mode prove-segments \
      --worker-id "$g" --num-workers "$N" --segments-out "$segdir" \
      "${common[@]}" $extra > "$run/worker-$g.log" 2>&1 ) &
  pids+=($!)
done
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done
t1=$(date +%s)
if [[ "$fail" != 0 ]]; then echo ">>> a worker FAILED — see $run/worker-*.log" >&2; exit 1; fi
nseg=$(ls "$segdir"/seg-*.bitcode 2>/dev/null | wc -l | tr -d ' ')
echo "workers done: $nseg segment proofs in $((t1 - t0))s"

ta=$(date +%s)
# shellcheck disable=SC2086
RUST_LOG="${RUST_LOG:-info}" "$BIN" --mode aggregate --segments-out "$segdir" --output-dir "$run" \
  "${common[@]}" $extra > "$run/aggregate.log" 2>&1 \
  || { echo ">>> aggregate FAILED — see $run/aggregate.log" >&2; exit 1; }
t2=$(date +%s)
[[ -n "${SAMPLER:-}" ]] && { kill "$SAMPLER" 2>/dev/null || true; }

{ echo "workers_secs=$((t1 - t0))"; echo "aggregate_secs=$((t2 - ta))"; echo "total_secs=$((t2 - t0))"
  echo "num_segments=$nseg"; } | tee "$run/timing.txt"
[[ -f "$run/gpu-util.csv" ]] && awk -F', *' '{if($4+0>p)p=$4+0} END{if(p)print "peak_vram_mib_per_gpu="p}' "$run/gpu-util.csv" | tee -a "$run/timing.txt"
echo "block_hash (aggregate): $(grep -oE 'block_hash \(aggregate\): [0-9a-f]+' "$run/aggregate.log" | grep -oE '[0-9a-f]{64}' | head -1)"

# ---- report.json — the shared cross-zkVM prove contract (see cli/report-schema.md), so
# profiling/results.py reads OpenVM runs like sp1/zisk. Backend = "multi-process-multi-gpu" (path ①).
proof_bytes=""; [[ -f "$run/proof.json" ]] && proof_bytes="$(wc -c < "$run/proof.json" | tr -d ' ')"
# cycles (OpenVM work-unit): best-effort from the block's execute report if it was minted.
cycles=""; exec_report="$ROOT/inputs/openvm-reth/${tag}.exec-report.json"
[[ -f "$exec_report" ]] && cycles="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cycles",""))' "$exec_report" 2>/dev/null || echo '')"
python3 - "$run/report.json" "$BLOCK" "$N" "$((t2 - t0))" "$((t1 - t0))" "$((t2 - ta))" "$nseg" "$proof_bytes" "$cycles" "${keygen_excluded:-0}" <<'PY'
import json, sys
p, block, n, total, workers, agg, nseg, pbytes, cycles, keyex = sys.argv[1:11]
def num(x):
    x = str(x).strip()
    if not x: return None
    try: return int(x)
    except ValueError:
        try: return float(x)
        except ValueError: return None
n_i = num(n)
json.dump({
    "mode": "prove-stark",
    "zkvm": "OpenVM",
    "block": num(block),
    "num_gpus": n_i,
    "multi_gpu": bool(n_i and n_i > 1),
    "cycles": num(cycles),
    "prove_secs": num(total),
    "total_secs": num(total),
    "proof_bytes": num(pbytes),
    "keygen_excluded": keyex == "1",
    "comparison_skipped": True,
    "verified": None,
    "backend": "multi-process-multi-gpu",
    "workers_secs": num(workers),
    "aggregate_secs": num(agg),
    "num_segments": num(nseg),
}, open(p, "w"), indent=2)
PY
echo "OK — run record: $(cd "$run" && pwd)/"
echo "  report.json · proof.json · worker-*.log · aggregate.log · gpu-util.csv · timing.txt · env.txt"
