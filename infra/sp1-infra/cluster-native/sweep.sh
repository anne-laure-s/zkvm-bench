#!/usr/bin/env bash
# sweep.sh — ADAPTIVE tuning sweep with minimum paid machine time.
#
# Coordinate descent that carries the winner forward between stages, so it converges in one run
# (no manual 2nd pass). Stages run in priority order; EACH is skippable (empty TRY / DO_*=0):
#   baseline    — the reference (cluster defaults), seeded with any locked-in BASE_ENV
#   A core (#6) — NUM_CORE_WORKERS: overlap per-shard trace-gen with GPU proving. THE lever. OOM-stop.
#   B cpu-trim  — USE_FIXED_PK=1 + VERIFY_INTERMEDIATES=0: drop per-shard setup + intermediate verify
#   C shard     — ascending LOG2_SHARD_SIZE, STOP at first CUDA-OOM   (skip with SHARD_TRY= once locked)
#   D NUMA      — NUMA_PIN interleave/bind (now container-safe: 03-start.sh falls back to taskset)
#   E splice(#4)— larger SP1_WORKER_NUM_SPLICING_WORKERS
# Only workers are bounced between configs (WORKERS_ONLY, fast), each proof is timeout-bounded
# (a bad config is abandoned, never hangs for the 14400s network timeout), and everything is
# logged to a CSV. Runs unattended — you don't pay for think-time.
#
#   ./sweep.sh <elf> <representative-witness.bin>
#
# To sweep ONLY the CPU levers with a prior shard winner locked (no re-paying for shard sizes):
#   BASE_ENV="LOG2_SHARD_SIZE=21 GPU_MAX_WEIGHT=32" SHARD_TRY= SPLICE_TRY= DO_NUMA=0 ./sweep.sh elf wit
#   (set GPU_MAX_WEIGHT ≥ 4×max(CORE_TRY) so the admission budget allows the core workers; PROVE_SHARD
#    has weight 4, so 32 admits 8 concurrent shards.)
#
# Env knobs:
#   RUNS          proofs per config, MIN kept (discards a cold first run). default 2
#   PROOF_TIMEOUT hard cap per proof (s). default 300
#   READY_WAIT    s to let fresh workers register before submitting. default 8
#   MODE          prove-compressed | prove-core | prove-groth16. default prove-compressed
#   CORE_TRY      ascending NUM_CORE_WORKERS to probe (#6). default "6 8" (baseline already = default 4)
#   DO_CPU_TRIM   1=try fixed-pk + verify-off (stage B). default 1
#   SHARD_TRY     ascending log2 shard sizes. default "21 22 23"; set empty to SKIP (lock via BASE_ENV)
#   DO_NUMA       1=run NUMA/taskset stage. default 1
#   SPLICE_TRY    splice-worker counts. default "32 48 64"; set empty to SKIP
#   BASE_ENV      fixed env prefix applied to EVERY config (lock a prior winner, e.g. shard size)
set -uo pipefail
cd "$(dirname "$0")"

ELF="${1:?usage: ./sweep.sh <elf> <witness.bin>}"
WIT="${2:?usage: ./sweep.sh <elf> <witness.bin>}"
MODE="${MODE:-prove-compressed}"
RUNS="${RUNS:-2}"
PROOF_TIMEOUT="${PROOF_TIMEOUT:-300}"
READY_WAIT="${READY_WAIT:-8}"
NGPU="${NUM_GPUS:-16}"
# NB: no-colon ${VAR-default} so an explicit empty (VAR=) is honored as "skip", not reset to default.
CORE_TRY="${CORE_TRY-6 8}"           # NUM_CORE_WORKERS values (#6); empty skips stage A
DO_CPU_TRIM="${DO_CPU_TRIM:-1}"      # 0 skips stage B (fixed-pk + verify-off)
SHARD_TRY="${SHARD_TRY-21 22 23}"    # empty skips stage C (e.g. when locked via BASE_ENV)
DO_NUMA="${DO_NUMA:-1}"              # 0 skips stage D
SPLICE_TRY="${SPLICE_TRY-32 48 64}"  # empty skips stage E
BASE_ENV="${BASE_ENV:-}"             # fixed env prefix applied to every config
tag="$(basename "${WIT%.bin}")"
CSV="sweep-$(date -u +%Y%m%d-%H%M%SZ).csv"
[[ -f "$ELF" && -f "$WIT" ]] || { echo "ERROR: elf/witness not found" >&2; exit 1; }

lt() { awk "BEGIN{exit !($1 < $2)}"; }   # float less-than

# measure <label> <env-string> → sets RESULT_SECS (empty on crash/fail), logs a CSV row + a line.
RESULT_SECS=""
measure() {
  local label="$1" envs="$2" r secs rundir cfg_best="" alive=0 p
  RESULT_SECS=""
  ./stop-workers.sh >/dev/null
  # shellcheck disable=SC2086
  env $envs WORKERS_ONLY=1 NUM_GPUS="$NGPU" ./03-start.sh >/dev/null 2>&1
  sleep "$READY_WAIT"
  for p in run/gpu*.pid; do [[ -f "$p" ]] && kill -0 "$(cat "$p")" 2>/dev/null && alive=$((alive+1)); done
  if (( alive < NGPU )); then
    printf '  %-30s %10s\n' "$label" "CRASH($alive/$NGPU)"; echo "$label,-,CRASH" >> "$CSV"; return
  fi
  for r in $(seq 1 "$RUNS"); do
    BUNDLE_LOGS=0 timeout "$PROOF_TIMEOUT" ./submit.sh "$ELF" "$WIT" "$MODE" >/dev/null 2>&1 \
      || { echo "$label,$r,TIMEOUT_OR_FAIL" >> "$CSV"; continue; }
    rundir="$(ls -dt runs/${tag}-* 2>/dev/null | head -1)"
    secs="$(jq -r '.prove_secs // empty' "$rundir/report.json" 2>/dev/null)"
    [[ -n "$secs" ]] || continue
    echo "$label,$r,$secs" >> "$CSV"
    { [[ -z "$cfg_best" ]] || lt "$secs" "$cfg_best"; } && cfg_best="$secs"
  done
  if [[ -n "$cfg_best" ]]; then printf '  %-30s %10.2f\n' "$label" "$cfg_best"; RESULT_SECS="$cfg_best"
  else printf '  %-30s %10s\n' "$label" "FAIL"; fi
}

# Incumbent (best-so-far) env + time. consider() keeps a candidate if it beats the incumbent.
INC_ENV=""; INC_SECS=""
consider() {  # <label> <env-string>
  measure "$1" "$2"
  [[ -n "$RESULT_SECS" ]] || return 1
  { [[ -z "$INC_SECS" ]] || lt "$RESULT_SECS" "$INC_SECS"; } && { INC_SECS="$RESULT_SECS"; INC_ENV="$2"; }
  return 0
}

# Bring up the control plane ONCE if it's down (then we only bounce workers per config).
if ! kill -0 "$(cat run/coordinator.pid 2>/dev/null)" 2>/dev/null; then
  echo "== control plane down → one full start, then per-config worker restarts =="
  ./stop.sh >/dev/null 2>&1 || true
  NUM_GPUS="$NGPU" ./03-start.sh >/dev/null 2>&1 || { echo "full start failed" >&2; exit 1; }
  ./stop-workers.sh >/dev/null
fi

echo "config,run,prove_secs" > "$CSV"
echo "Adaptive sweep → $CSV   (block $tag · mode $MODE · RUNS=$RUNS · timeout ${PROOF_TIMEOUT}s)"

# Baseline — the reference, seeded with any locked-in BASE_ENV so every later config inherits it.
# (This row is also the NUM_CORE_WORKERS=cluster-default-4 control, so CORE_TRY starts at 6.)
consider "baseline${BASE_ENV:+ +base}" "$BASE_ENV" || true

if [[ -n "$CORE_TRY" ]]; then
  echo "Stage A — core workers / #6 (overlap trace-gen w/ proving; ascending, stop at first OOM):"
  BASE="$INC_ENV"
  for w in $CORE_TRY; do
    consider "core=$w" "${BASE:+$BASE }NUM_CORE_WORKERS=$w CORE_BUFFER_SIZE=$w" \
      || { echo "  (crash/fail at core=$w → stop climbing)"; break; }
  done
  echo "  → best after stage A: [${INC_ENV:-default}] ${INC_SECS}s"
fi

if [[ "$DO_CPU_TRIM" != 0 ]]; then
  echo "Stage B — CPU trim (fixed-pk + verify-off, on the core winner):"
  BASE="$INC_ENV"
  consider "trim:fixedpk+noverify" "${BASE:+$BASE }USE_FIXED_PK=1 VERIFY_INTERMEDIATES=0" || true
  echo "  → best after stage B: [${INC_ENV:-default}] ${INC_SECS}s"
fi

if [[ -n "$SHARD_TRY" ]]; then
  echo "Stage C — shard size (ascending, stop at first OOM):"
  BASE="$INC_ENV"
  for sz in $SHARD_TRY; do
    consider "shard=$sz w=30" "${BASE:+$BASE }LOG2_SHARD_SIZE=$sz GPU_MAX_WEIGHT=30" \
      || { echo "  (crash/fail at log2=$sz → stop climbing)"; break; }
  done
  echo "  → best after stage C: [${INC_ENV:-default}] ${INC_SECS}s"
fi

if [[ "$DO_NUMA" != 0 ]]; then
  echo "Stage D — NUMA / CPU pinning (container-safe via taskset fallback):"
  BASE="$INC_ENV"
  for np in interleave bind; do consider "numa=$np" "${BASE:+$BASE }NUMA_PIN=$np" || true; done
  echo "  → best after stage D: [${INC_ENV:-default}] ${INC_SECS}s"
fi

if [[ -n "$SPLICE_TRY" ]]; then
  echo "Stage E — splice workers / #4:"
  BASE="$INC_ENV"
  for w in $SPLICE_TRY; do consider "splice=$w" "${BASE:+$BASE }SPLICING_WORKERS=$w" || true; done
fi

echo
echo "WINNER  : ${INC_SECS:-?}s  (block $tag)   full data: $CSV"
echo "  params : ${INC_ENV:-<cluster defaults — no override won>}"
echo "  lock in: ./stop-workers.sh && ${INC_ENV:+$INC_ENV }WORKERS_ONLY=1 ./03-start.sh"
echo "  confirm: re-run the winner on a 2nd block size to check the optimum holds."
