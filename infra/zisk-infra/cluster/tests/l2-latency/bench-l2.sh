#!/usr/bin/env bash
# bench-l2.sh — how long a proof of one L2 block takes, by block size. RUNS ON THE BOX, against a
# cluster up.sh has brought up; run.sh drives it, and it can be run on its own.
#
#   OUT=<dir> bash bench-l2.sh               # a STARK proof of every input, PASSES times
#
# Env: PASSES=1 · WARMUPS=1 · ORDER=elf|pair · RECHECK=3 · ONLY=<regex on the input id>
#      GPUS=<label for this worker config> · PROVE_TIMEOUT=1800 · COORD=http://127.0.0.1:7000
#      METRICS_PORT=9090
#
# ORDER=elf (the default) proves every input of one ELF in a row, right after that ELF's
# warm-up, the way a prover on one chain proves one ELF: a worker that changes ELF between two
# proofs could pay for the change inside the clock. The price is that the arms are timed in
# different stretches of the run, so a box that drifted would put its drift in their ratios:
# RECHECK proves the first ELF's first inputs once more at the end, after a warm-up of their
# own, into recheck.csv, and their times against the first ones bound the drift. ORDER=pair is
# the order tests/paired uses: every arm of one block in a row, alternating which goes first.
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
PASSES="${PASSES:-1}"; WARMUPS="${WARMUPS:-1}"; ORDER="${ORDER:-elf}"; RECHECK="${RECHECK:-3}"
case "$ORDER" in elf|pair) ;; *) echo "ERROR: ORDER is elf or pair, not $ORDER" >&2; exit 1 ;; esac
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
  echo "run          gpus=$GPUS  passes=$PASSES  warmups=$WARMUPS  order=$ORDER  recheck=$RECHECK  only=$ONLY"
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

warm() { # warm <elf>: WARMUPS discarded proves of that ELF's first input
  local row id arm pair e bin i
  row="$(printf '%s\n' "${ROWS[@]}" | awk -F, -v e="$1" '$4==e' | head -1)"
  IFS=, read -r id arm pair e bin <<<"$row"
  for i in $(seq 1 "$WARMUPS"); do
    setup "$e" "warm.$id.$i"
    timed "$OUT/logs/warm.$id.$i.log" cargo-zisk remote prove --coordinator "$COORD" \
             -e "$IN/$e" -i "$IN/$bin" -o "$OUT/proofs/warm.proof"
    printf '  warm %-31s %8ss  rc=%s\n' "$id" "$SECS" "$RC"
  done
}
# The ELFs in inputs.csv order, so the reference arm's comes first.
mapfile -t ELFS < <(printf '%s\n' "${ROWS[@]}" | cut -d, -f4 | awk '!seen[$0]++')

gpu_sampler
if [ "$ORDER" = elf ]; then
  for elf in "${ELFS[@]}"; do
    echo "== $elf: warm-up x$WARMUPS (discarded), then its inputs x$PASSES =="
    warm "$elf"
    for p in $(seq 1 "$PASSES"); do
      for row in "${ROWS[@]}"; do
        [ "$(cut -d, -f4 <<<"$row")" = "$elf" ] && one "$p" "$row"
      done
    done
  done
else
  echo "== warm-up x$WARMUPS per ELF (discarded) =="
  for elf in "${ELFS[@]}"; do warm "$elf"; done
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
fi

# The drift check: the first ELF's first RECHECK inputs again, after a warm-up, into their own
# csv and proof directory -- timings.csv and the kept proofs stay one per input and pass.
if [ "$RECHECK" -gt 0 ]; then
  first="${ELFS[0]}"
  echo "== recheck: $first, its first $RECHECK inputs once more =="
  warm "$first"
  [ -f "$OUT/recheck.csv" ] || echo "pass,gpus,id,arm,pair,secs,rc" > "$OUT/recheck.csv"
  mkdir -p "$OUT/recheck"
  n=0
  for row in "${ROWS[@]}"; do
    IFS=, read -r id arm pair e bin <<<"$row"
    [ "$e" = "$first" ] || continue
    setup "$e" "$id.recheck"
    timed "$OUT/logs/$id.recheck.log" cargo-zisk remote prove --coordinator "$COORD" \
             -e "$IN/$e" -i "$IN/$bin" -o "$OUT/recheck/$id.proof"
    echo "r,$GPUS,$id,$arm,$pair,$SECS,$RC" >> "$OUT/recheck.csv"
    printf '  r   %-36s %8ss  rc=%s\n' "$id" "$SECS" "$RC"
    n=$((n + 1))
    [ "$n" -ge "$RECHECK" ] && break
  done
fi
kill "$SAMPLER" 2>/dev/null; SAMPLER=""

tot=$(awk -F, 'NR>1' "$OUT/timings.csv" | wc -l | tr -d ' ')
bad=$(awk -F, 'NR>1 && $7!=0' "$OUT/timings.csv" | wc -l | tr -d ' ')
echo "== STARK done: $tot proves, $bad failed — $OUT/timings.csv =="
[ "$bad" -gt 0 ] && echo "!!! read $OUT/logs/<id>.p<n>.log for the failures" >&2
exit 0
