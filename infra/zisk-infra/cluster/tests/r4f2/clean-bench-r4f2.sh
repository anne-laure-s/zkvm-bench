#!/usr/bin/env bash
# clean-bench-r4f2.sh — clean-bench.sh for a monad guest. RUNS ON THE BOX, not on the Mac.
#
# Differs from cluster/clean-bench.sh in three places, all forced by the guest:
#   * ELF and inputs are parameters, not $HOME/zisk-reth.elf and $HOME/1-*.bin
#   * NO --hints anywhere: the monad guest uses no precompile hints, and passing --hints with a
#     missing file fails the prove. This is also why --asm is absent.
#   * output goes to ~/bench-r4f2 so a zisk-reth run in the same session is not overwritten
#
# timings.csv keeps the same 6 columns, so the runbook's awk fit reads it unchanged.
#
#   PASSES=3 WARMUPS=2 bash clean-bench-r4f2.sh
set -u
export PATH="$HOME/.zisk/bin:$PATH"
COORD="${COORD:-http://127.0.0.1:7000}"
# Self-locating: the bundle ships inside cluster/, so it lands wherever cluster/ lands and must not
# assume a path. Inputs sit beside this script; the ELF ships to $HOME next to zisk-reth.elf.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELF="${ELF:-$HOME/monad-zkvm-guest-zisk.elf}"
INPUTS="${INPUTS:-$HERE/inputs}"
OUT="${OUT:-$HOME/bench-r4f2}"; mkdir -p "$OUT"
WLOG="$HOME/zisk-infra/cluster/logs/worker.log"
PASSES="${PASSES:-3}"; WARMUPS="${WARMUPS:-2}"

[ -f "$ELF" ] || { echo "ERROR: no ELF at $ELF"; exit 1; }
mapfile -t BLOCKS < <(ls "$INPUTS"/1-*.bin 2>/dev/null | grep -v '\.pv\.bin$')
[ "${#BLOCKS[@]}" -ge 1 ] || { echo "ERROR: no $INPUTS/1-*.bin inputs"; exit 1; }
grep -qa "Registered worker" "$HOME/zisk-infra/cluster/logs/coordinator.log" 2>/dev/null \
  || { echo "ERROR: worker not registered — run start.sh (NO_MPI) and wait for registration first."; exit 1; }

# The sha is the identity, not the commit: the guest carries -mtune=generic-ooo and a build without
# it is a different binary at ~8% more work.
echo "== guest =="
echo "  $ELF"
echo "  sha256 $(sha256sum "$ELF" | cut -d' ' -f1)"
echo "  expect 362b8eb0318bc6a6f7f1f958e851374b79557051f1d8f823f33f52322503c2e2"
echo "  ${#BLOCKS[@]} input(s) from $INPUTS"

echo "== remote setup (idempotent, no --hints) =="
cargo-zisk remote setup -e "$ELF" --coordinator "$COORD" 2>&1 | grep -aE "Hash ID|completed|Error|failed" || true

echo "== warm-up x$WARMUPS (discarded) =="
for i in $(seq 1 "$WARMUPS"); do
  cargo-zisk remote prove -e "$ELF" -i "${BLOCKS[0]}" \
    -o /tmp/warm-r4f2.proof --coordinator "$COORD" --timeout 0 >/dev/null 2>&1 && echo "  warm $i ok" || echo "  warm $i FAIL"
done

echo "pass,tag,wall_secs,wlog_start,wlog_end,rc" > "$OUT/timings.csv"
for p in $(seq 1 "$PASSES"); do
  for bin in "${BLOCKS[@]}"; do
    tag="$(basename "$bin" .bin)"
    s=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    t0=$(date +%s.%N)
    cargo-zisk remote prove -e "$ELF" -i "$bin" \
      -o "$OUT/${tag}.proof" --coordinator "$COORD" --timeout 0 > "$OUT/${tag}.p${p}.log" 2>&1
    rc=$?; t1=$(date +%s.%N); e=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    dt=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
    echo "$p,$tag,$dt,$s,$e,$rc" >> "$OUT/timings.csv"
    printf "  pass %s  %-12s  %6ss  (rc=%s)\n" "$p" "$tag" "$dt" "$rc"
  done
done
cp "$WLOG" "$OUT/worker.log" 2>/dev/null || true
echo "== DONE — results in $OUT/ =="; cat "$OUT/timings.csv"
