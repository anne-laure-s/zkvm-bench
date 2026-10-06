#!/usr/bin/env bash
# hints-ab.sh — what do ZisK's precompile hints buy on the reth guest? RUNS ON THE BOX.
#
# The monad guest uses no hints; reth does. Before any r8-vs-reth ratio is quoted, that difference
# needs a price. This is the paired A/B that prices it.
#
# METHOD, and it is not negotiable: the two arms ALTERNATE which goes first, round by round. An
# interleaved-but-fixed order already produced a reversed verdict once in this repo (gzip vs zstd,
# see infra/monad-witness/RTP-FINDINGS.md) — the first arm of a round pays cache and scheduler costs
# the second does not. Two block sizes, because the gain may be flat (per proof) or proportional
# (per precompile call), and only a size spread separates those.
#
#   ROUNDS_S=6 ROUNDS_L=4 bash hints-ab.sh
set -u
export PATH="$HOME/.zisk/bin:$PATH"
COORD="${COORD:-http://127.0.0.1:7000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELF="${ELF:-$HOME/zisk-reth.elf}"
IN="${IN:-$HOME}"
OUT="${OUT:-$HOME/bench-hints-ab}"; mkdir -p "$OUT"
SMALL="${SMALL:-1-24647140}"   #  58.3 Msteps
LARGE="${LARGE:-1-24697073}"   # 110.7 Msteps
ROUNDS_S="${ROUNDS_S:-6}"; ROUNDS_L="${ROUNDS_L:-4}"

[ -f "$ELF" ] || { echo "ERROR: no ELF at $ELF"; exit 1; }
for b in "$SMALL" "$LARGE"; do
  [ -f "$IN/$b.bin" ]   || { echo "ERROR: missing $IN/$b.bin"; exit 1; }
  [ -f "$IN/$b.hints" ] || { echo "ERROR: missing $IN/$b.hints"; exit 1; }
done
# 1.0.0-alpha logs "Registered worker", 1.1.0-alpha "WorkerId(…) registered successfully".
# Match either, or the guard blocks a cluster that is in fact up.
grep -qaiE "registered (worker|successfully)" "$HOME/zisk-infra/cluster/logs/coordinator.log" 2>/dev/null \
  || { echo "ERROR: worker not registered — run start.sh first."; exit 1; }


# ── env snapshot ────────────────────────────────────────────────────────────────────────────────
# A fit with no record of the stack that produced it is unreadable six months on, and a version
# mismatch is exactly what invalidated an earlier run of this track. Written before any proving.
snapshot_env() {
  {
    echo "date_utc      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host          $(hostname)"
    echo "cargo_zisk    $(cargo-zisk --version 2>/dev/null)"
    echo "ziskemu       $(ziskemu --version 2>/dev/null | head -1)"
    echo "zisk_ver_env  ${ZISK_VER:-<unset>}"
    echo "elf           $1"
    echo "elf_sha256    $(sha256sum "$1" 2>/dev/null | cut -d' ' -f1)"
    echo "worker_bin    $(ps -o args= -C zisk-worker 2>/dev/null | head -1)"
    nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv,noheader 2>/dev/null \
      | sed 's/^/gpu           /'
    echo "cpu           $(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | sed 's/^ //')"
    echo "cpu_allocated $(nproc)"
    echo "mem           $(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%.0f GB total, %.0f GB available",t/1048576,a/1048576}' /proc/meminfo)"
    echo "disk          $(df -h / | awk 'NR==2{print $3" used, "$4" free"}')"
  } > "$OUT/env.txt"
  echo "== env snapshot -> $OUT/env.txt =="; cat "$OUT/env.txt" | sed 's/^/  /'
}

# Both cluster logs, not just the worker's: the coordinator is where a phase timeout, a failed job
# and a "Cluster unavailable" recovery are recorded, and none of those appear worker-side.
save_logs() {
  cp "$HOME/zisk-infra/cluster/logs/worker.log"      "$OUT/worker.log"      2>/dev/null || true
  cp "$HOME/zisk-infra/cluster/logs/coordinator.log" "$OUT/coordinator.log" 2>/dev/null || true
}

snapshot_env "$ELF"

echo "== guest =="
echo "  $ELF"
echo "  sha256 $(sha256sum "$ELF" | cut -d' ' -f1)"
echo "  expect 979da60df5826c5e…  (zisk-reth rebuilt on ZisK 1.1)"
echo "  cargo-zisk $(cargo-zisk --version 2>/dev/null)"

# THE SETUP IS PER ARM AND THE TWO CANNOT COEXIST. Both configurations register under the same
# Hash ID (it is the ELF's), so the second setup overwrites the first: prove with --hints against a
# cache last set up without them and the job hangs in CALCULATING_CONTRIBUTIONS until the 300 s
# monitor timeout, which then drops the cluster into recovery and kills every later round.
# So each prove re-runs the setup for its own arm. It costs ~5 s (hints) / ~65 s (hint-free) per
# switch, outside the timed region — one() starts its clock after this returns.
setup_for() {
  if [ "$1" = hints ]; then cargo-zisk remote setup -e "$ELF" --hints --coordinator "$COORD" >/dev/null 2>&1
  else                      cargo-zisk remote setup -e "$ELF"          --coordinator "$COORD" >/dev/null 2>&1; fi
}

one() {  # one() <tag> <arm> <round> -> appends a row
  local tag="$1" arm="$2" r="$3" t0 t1 rc dt
  setup_for "$arm"          # outside the clock, and mandatory: see setup_for
  t0=$(date +%s.%N)
  if [ "$arm" = hints ]; then
    cargo-zisk remote prove -e "$ELF" -i "$IN/$tag.bin" --hints "$IN/$tag.hints" \
      -o "$OUT/$tag.$arm.proof" --coordinator "$COORD" > "$OUT/$tag.$arm.r$r.log" 2>&1
  else
    cargo-zisk remote prove -e "$ELF" -i "$IN/$tag.bin" \
      -o "$OUT/$tag.$arm.proof" --coordinator "$COORD" > "$OUT/$tag.$arm.r$r.log" 2>&1
  fi
  rc=$?; t1=$(date +%s.%N); dt=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
  echo "$r,$tag,$arm,$dt,$rc" >> "$OUT/hints-ab.csv"
  printf "  r%-2s %-14s %-8s %8ss  (rc=%s)\n" "$r" "$tag" "$arm" "$dt" "$rc"
}

echo "round,tag,arm,secs,rc" > "$OUT/hints-ab.csv"
echo "== warm-up (discarded) =="
# The warm-up is the first thing that can fail, so its output is the first thing worth keeping.
setup_for hints
cargo-zisk remote prove -e "$ELF" -i "$IN/$SMALL.bin" --hints "$IN/$SMALL.hints" \
  -o /tmp/warm-ab.proof --coordinator "$COORD" > "$OUT/warmup.log" 2>&1 \
  && echo "  warm ok" \
  || { echo "  warm FAIL — $OUT/warmup.log:"; sed 's/^/    /' "$OUT/warmup.log" | tail -8; }

for spec in "$SMALL $ROUNDS_S" "$LARGE $ROUNDS_L"; do
  set -- $spec; tag="$1"; n="$2"
  echo "== $tag, $n rounds, order alternating =="
  for r in $(seq 1 "$n"); do
    if [ $((r % 2)) -eq 1 ]; then one "$tag" hints "$r";   one "$tag" nohints "$r"
    else                         one "$tag" nohints "$r"; one "$tag" hints "$r"; fi
  done
done

save_logs
echo "== DONE — $OUT/hints-ab.csv =="
cnt() { awk -F, -v a="$1" -v w="$2" 'NR>1&&$3==a&&(w=="any"||($5!=0)==(w=="bad"))' "$OUT/hints-ab.csv" | wc -l | tr -d ' '; }
hb=$(cnt hints bad); ht=$(cnt hints any); nb=$(cnt nohints bad); nt=$(cnt nohints any)
# A verdict needs a CONTRAST. One arm failing says nothing on its own: a single failure puts the
# coordinator into recovery and every later prove is rejected instantly, whichever arm it belongs to.
if [ "$nb" = "$nt" ] && [ "$hb" = "0" ] && [ "$nt" -gt 0 ]; then
  echo "RESULT: hint-free failed $nb/$nt while every hints prove succeeded — hints are required here." >&2
elif [ "$hb" -gt 0 ] && [ "$nb" -gt 0 ]; then
  echo "INCONCLUSIVE: BOTH arms failed ($hb/$ht hints, $nb/$nt hint-free). One failure poisons the" >&2
  echo "  cluster, so this says nothing about hints. Read $OUT/*.r1.log, fix the cause, re-run." >&2
elif [ "$((hb+nb))" -gt 0 ]; then
  echo "WARN: $((hb+nb)) row(s) with rc!=0 — exclude them before pairing." >&2
fi
cat "$OUT/hints-ab.csv"
