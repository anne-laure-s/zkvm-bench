#!/usr/bin/env bash
# bench-l2.sh — how long a proof of one L2 block takes, by block size. RUNS ON THE BOX, against a
# cluster up.sh has brought up; run.sh drives it, and it can be run on its own.
#
#   OUT=<dir> bash bench-l2.sh               # a STARK proof of every input, PASSES times
#
# Env: PASSES=2 · WARMUPS=1 · ONLY=<regex on the input id> · GPUS=<label for this worker config>
#      PROVE_TIMEOUT=1800 · COORD=http://127.0.0.1:7000 · METRICS_PORT=9090
#
# What is timed is the client's wall clock around `cargo-zisk remote prove`: submission to proof
# on disk, which is what a sequencer waiting on a proof sees. Setup runs before every prove and
# outside the clock, as tests/paired does: the coordinator's cache has surprised this repo before,
# and two seconds of setup are cheaper than a poisoned run.
#
# Every proof is kept (the last pass's) so run.sh can check its public values against what the
# block must publish once nothing is being timed. Each prove's slice of worker.log is carved out
# and its `<<< PHASE (N ms)` spans written to phases.csv, whatever the phases are called in this
# release.
set -u
export PATH="$HOME/.zisk/bin:$PATH"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="$(cd "$HERE/../.." && pwd)"
IN="$HERE/inputs"
OUT="${OUT:?set OUT=<results dir>}"; mkdir -p "$OUT/logs" "$OUT/proofs" "$OUT/metrics"
COORD="${COORD:-http://127.0.0.1:${API_PORT:-7000}}"
METRICS_PORT="${METRICS_PORT:-9090}"
PASSES="${PASSES:-2}"; WARMUPS="${WARMUPS:-1}"
PROVE_TIMEOUT="${PROVE_TIMEOUT:-1800}"
GPUS="${GPUS:-all}"
ONLY="${ONLY:-.}"
WLOG="$CLUSTER/logs/worker.log"

[ -f "$IN/inputs.csv" ] || { echo "ERROR: no $IN/inputs.csv — stage the inputs (prepare-inputs.py)" >&2; exit 1; }
command -v cargo-zisk >/dev/null 2>&1 || { echo "ERROR: cargo-zisk not on PATH" >&2; exit 1; }

# id,arm,pair,elf,bin of every input this run takes, in inputs.csv order.
mapfile -t ROWS < <(python3 - "$IN/inputs.csv" "$ONLY" <<'EOF'
import csv, re, sys
for r in csv.DictReader(open(sys.argv[1])):
    if re.search(sys.argv[2], r['id']):
        print(','.join((r['id'], r['arm'], r['pair'], r['elf'], r['bin'])))
EOF
)
[ "${#ROWS[@]}" -ge 1 ] || { echo "ERROR: no input matches ONLY=$ONLY" >&2; exit 1; }

wlines() { if [ -f "$WLOG" ]; then wc -l < "$WLOG" | tr -d ' '; else echo 0; fi; }
# The worker.log lines a prove wrote, reduced to its phase spans.
phases() { # phases <first line> <last line> <pass> <id> <kind>
  sed -n "${1},${2}p" "$WLOG" 2>/dev/null \
    | grep -aoE '<<< [A-Za-z0-9_]+ \([0-9]+ms\)' \
    | sed -E "s/<<< ([A-Za-z0-9_]+) \\(([0-9]+)ms\\)/$3,$GPUS,$4,$5,\\1,\\2/" >> "$OUT/phases.csv"
}
metrics() { curl -s --max-time 5 "http://127.0.0.1:$METRICS_PORT/metrics" > "$OUT/metrics/$1.prom" 2>/dev/null || true; }
setup() { # setup <elf> <tag>
  cargo-zisk remote setup --coordinator "$COORD" -e "$IN/$1" > "$OUT/logs/setup.$2.log" 2>&1 \
    || { echo "  SETUP FAILED for $1 — $OUT/logs/setup.$2.log:"; tail -6 "$OUT/logs/setup.$2.log" | sed 's/^/    /'; }
}
# Sets SECS and RC in this shell: called through $(…) it would run in a subshell, and RC would
# never reach the caller -- every failed prove would read as rc=0.
timed() { # timed <log> <cmd…>
  local log="$1"; shift
  local t0 t1
  t0=$(date +%s.%N)
  timeout "$PROVE_TIMEOUT" "$@" > "$log" 2>&1
  RC=$?
  t1=$(date +%s.%N)
  SECS=$(awk "BEGIN{printf \"%.3f\", $t1-$t0}")
}

gpu_sampler() {
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader,nounits -lms 500 \
    > "$OUT/gpu.csv" 2>/dev/null &
  SAMPLER=$!
}
trap '[ -n "${SAMPLER:-}" ] && kill "$SAMPLER" 2>/dev/null' EXIT

{ echo "date_utc     $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "run          gpus=$GPUS  passes=$PASSES  warmups=$WARMUPS  only=$ONLY"
  echo "cargo_zisk   $(cargo-zisk --version 2>/dev/null)"
  echo "worker       $(grep -ao 'starting worker: .*' "$HOME/start.out" 2>/dev/null | tail -1)"
  echo "CUDA_VISIBLE_DEVICES ${CUDA_VISIBLE_DEVICES:-<all>}"
  for e in "$IN"/*.elf; do echo "elf          $(basename "$e") $(sha256sum "$e" | cut -c1-16)"; done
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/gpu          /'
  echo "cpu          $(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | sed 's/^ //')  ($(nproc) threads)"
  echo "ram          $(awk '/MemTotal/{printf "%.0f GB", $2/1048576}' /proc/meminfo)"
} > "$OUT/env.txt"
sed 's/^/  /' "$OUT/env.txt"

[ -f "$OUT/timings.csv" ] || echo "pass,gpus,id,arm,pair,secs,rc" > "$OUT/timings.csv"
[ -f "$OUT/phases.csv" ]  || echo "pass,gpus,id,kind,phase,ms" > "$OUT/phases.csv"

one() { # one <pass> <row>
  local p="$1" id arm pair elf bin secs a b
  IFS=, read -r id arm pair elf bin <<<"$2"
  setup "$elf" "$id.p$p"
  a=$(( $(wlines) + 1 ))
  timed "$OUT/logs/$id.p$p.log" cargo-zisk remote prove --coordinator "$COORD" \
           -e "$IN/$elf" -i "$IN/$bin" -o "$OUT/proofs/$id.proof"
  secs=$SECS
  b=$(wlines)
  echo "$p,$GPUS,$id,$arm,$pair,$secs,$RC" >> "$OUT/timings.csv"
  phases "$a" "$b" "$p" "$id" stark
  metrics "$id.p$p"
  printf '  p%-2s %-36s %8ss  rc=%s\n' "$p" "$id" "$secs" "$RC"
}

echo "== warm-up x$WARMUPS per ELF (discarded) =="
for elf in $(printf '%s\n' "${ROWS[@]}" | cut -d, -f4 | sort -u); do
  row="$(printf '%s\n' "${ROWS[@]}" | awk -F, -v e="$elf" '$4==e' | head -1)"
  for i in $(seq 1 "$WARMUPS"); do
    IFS=, read -r id arm pair e bin <<<"$row"
    setup "$e" "warm.$id.$i"
    timed "$OUT/logs/warm.$id.$i.log" cargo-zisk remote prove --coordinator "$COORD" \
             -e "$IN/$e" -i "$IN/$bin" -o "$OUT/proofs/warm.proof"
    secs=$SECS
    printf '  warm %-31s %8ss  rc=%s\n' "$id" "$secs" "$RC"
  done
done

gpu_sampler
# Order alternates within a pair, as in tests/paired: whichever arm goes first can pay for a
# cache the second finds warm, and a fixed order turns that into a difference between the arms.
for p in $(seq 1 "$PASSES"); do
  echo "== pass $p/$PASSES =="
  k=0
  for pair in $(printf '%s\n' "${ROWS[@]}" | cut -d, -f3 | awk '!seen[$0]++'); do
    mapfile -t PR < <(printf '%s\n' "${ROWS[@]}" | awk -F, -v q="$pair" '$3==q')
    k=$((k + 1))
    if [ $(( (k + p) % 2 )) -eq 0 ]; then
      for (( j=0; j<${#PR[@]}; j++ )); do one "$p" "${PR[$j]}"; done
    else
      for (( j=${#PR[@]}-1; j>=0; j-- )); do one "$p" "${PR[$j]}"; done
    fi
  done
done
kill "$SAMPLER" 2>/dev/null; SAMPLER=""

tot=$(awk -F, 'NR>1' "$OUT/timings.csv" | wc -l | tr -d ' ')
bad=$(awk -F, 'NR>1 && $7!=0' "$OUT/timings.csv" | wc -l | tr -d ' ')
echo "== STARK done: $tot proves, $bad failed — $OUT/timings.csv =="
[ "$bad" -gt 0 ] && echo "!!! read $OUT/logs/<id>.p<n>.log for the failures" >&2
exit 0
