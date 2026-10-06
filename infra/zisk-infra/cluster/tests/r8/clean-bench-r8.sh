#!/usr/bin/env bash
# clean-bench-r8.sh — clean-bench.sh for the monad r8 guest. RUNS ON THE BOX, not on the Mac.
#
# Differs from cluster/clean-bench.sh in three places, all forced by the guest:
#   * ELF and inputs are parameters, not $HOME/zisk-reth.elf and $HOME/1-*.bin
#   * NO --hints anywhere: the monad guest uses no precompile hints, and passing --hints with a
#     missing file fails the prove. This is also why --asm is absent.
#   * output goes to ~/bench-r8 so another arm in the same session is not overwritten
#
# timings.csv keeps the same 6 columns, so the runbook's awk fit reads it unchanged.
#
#   PASSES=3 WARMUPS=2 bash clean-bench-r8.sh
set -u
export PATH="$HOME/.zisk/bin:$PATH"
COORD="${COORD:-http://127.0.0.1:7000}"
# Self-locating: the bundle ships inside cluster/, so it lands wherever cluster/ lands and must not
# assume a path. Inputs sit beside this script; the ELF ships to $HOME.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELF="${ELF:-$HOME/monad-r8-zisk.elf}"
INPUTS="${INPUTS:-$HERE/inputs}"
OUT="${OUT:-$HOME/bench-r8}"; mkdir -p "$OUT"
WLOG="$HOME/zisk-infra/cluster/logs/worker.log"
PASSES="${PASSES:-3}"; WARMUPS="${WARMUPS:-2}"

[ -f "$ELF" ] || { echo "ERROR: no ELF at $ELF"; exit 1; }
mapfile -t BLOCKS < <(ls "$INPUTS"/1-*.bin 2>/dev/null | grep -v '\.pv\.bin$')
[ "${#BLOCKS[@]}" -ge 1 ] || { echo "ERROR: no $INPUTS/1-*.bin inputs"; exit 1; }
# 1.0.0-alpha logs "Registered worker", 1.1.0-alpha "WorkerId(…) registered successfully".
# Match either, or the guard blocks a cluster that is in fact up.
grep -qaiE "registered (worker|successfully)" "$HOME/zisk-infra/cluster/logs/coordinator.log" 2>/dev/null \
  || { echo "ERROR: worker not registered — run start.sh and wait for registration first."; exit 1; }


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

# The sha is the identity, not the commit.
echo "== guest =="
echo "  $ELF"
echo "  sha256 $(sha256sum "$ELF" | cut -d' ' -f1)"
echo "  expect fd39fe8c27533b6d06e83734ea15ab942fdfdac71800d5206d79efa155f6aae4"
echo "  ${#BLOCKS[@]} input(s) from $INPUTS"
echo "  cargo-zisk $(cargo-zisk --version 2>/dev/null)"

echo "== remote setup (idempotent, no --hints) =="
cargo-zisk remote setup -e "$ELF" --coordinator "$COORD" 2>&1 | grep -aE "Hash ID|completed|Error|failed" || true

echo "== warm-up x$WARMUPS (discarded) =="
for i in $(seq 1 "$WARMUPS"); do
  # Keep the warm-up's output: it is the first thing that can fail, and discarding it means the
  # first diagnostic of a broken run is thrown away.
  cargo-zisk remote prove -e "$ELF" -i "${BLOCKS[0]}" \
    -o /tmp/warm-r8.proof --coordinator "$COORD" > "$OUT/warmup.$i.log" 2>&1 \
    && echo "  warm $i ok" \
    || { echo "  warm $i FAIL — $OUT/warmup.$i.log:"; sed 's/^/    /' "$OUT/warmup.$i.log" | tail -8; }
done

echo "pass,tag,wall_secs,wlog_start,wlog_end,rc" > "$OUT/timings.csv"
fails=0; total=0
for p in $(seq 1 "$PASSES"); do
  for bin in "${BLOCKS[@]}"; do
    tag="$(basename "$bin" .bin)"
    s=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    t0=$(date +%s.%N)
    cargo-zisk remote prove -e "$ELF" -i "$bin" \
      -o "$OUT/${tag}.proof" --coordinator "$COORD" > "$OUT/${tag}.p${p}.log" 2>&1
    rc=$?; t1=$(date +%s.%N); e=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    dt=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
    echo "$p,$tag,$dt,$s,$e,$rc" >> "$OUT/timings.csv"
    printf "  pass %s  %-14s %7ss  (rc=%s)\n" "$p" "$tag" "$dt" "$rc"
    total=$((total+1)); [ "$rc" -ne 0 ] && fails=$((fails+1))
  done
done
save_logs
echo "== DONE — results in $OUT/ =="
# A CSV of nothing but rc!=0 rows reads like a result and is not one: the fit drops every row and
# reports n=0. Say so here rather than let it be discovered at the fit.
if [ "$fails" -eq "$total" ]; then
  echo "!!! ALL $total prove(s) FAILED — timings.csv carries no usable row." >&2
  echo "!!! Read $OUT/<tag>.p1.log and cluster/logs/worker.log before fitting anything." >&2
elif [ "$fails" -gt 0 ]; then
  echo "WARN: $fails/$total prove(s) failed and are excluded from the fit." >&2
fi
cat "$OUT/timings.csv"
