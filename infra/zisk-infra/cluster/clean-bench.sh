#!/usr/bin/env bash
# clean-bench.sh — a "clean" ZisK benchmark: discarded warm-up + N passes over all blocks.
# For each (pass, block): high-precision wall-clock time + bounds (line offsets) in
# worker.log → lets you then extract the exact EXECUTE→proof window without guessing the order.
#
# Prerequisites: cluster UP (coordinator + patched NO_MPI worker, registered) + inputs ~/1-*.{bin,hints}.
#   Settings: PASSES=3 WARMUPS=2 bash clean-bench.sh
set -u
export PATH="$HOME/.zisk/bin:$PATH"
COORD="http://127.0.0.1:7000"
ELF="$HOME/zisk-reth.elf"
WLOG="$HOME/zisk-infra/cluster/logs/worker.log"
OUT="$HOME/bench"; mkdir -p "$OUT"
PASSES="${PASSES:-3}"; WARMUPS="${WARMUPS:-2}"

mapfile -t BLOCKS < <(ls "$HOME"/1-*.bin 2>/dev/null | grep -v '\.pv\.bin$')
[ "${#BLOCKS[@]}" -ge 1 ] || { echo "ERROR: no ~/1-*.bin inputs"; exit 1; }
grep -qa "Registered worker" "$HOME/zisk-infra/cluster/logs/coordinator.log" 2>/dev/null \
  || { echo "ERROR: worker not registered — run start.sh (NO_MPI) and wait for registration first."; exit 1; }

echo "== remote setup (idempotent) =="
cargo-zisk remote setup -e "$ELF" --hints --coordinator "$COORD" 2>&1 | grep -aE "Hash ID|completed|Error|failed" || true

echo "== warm-up x$WARMUPS (discarded) =="
for i in $(seq 1 "$WARMUPS"); do
  cargo-zisk remote prove -e "$ELF" -i "${BLOCKS[0]}" --hints "${BLOCKS[0]%.bin}.hints" \
    -o /tmp/warm.proof --coordinator "$COORD" --timeout 0 >/dev/null 2>&1 && echo "  warm $i ok" || echo "  warm $i FAIL"
done

echo "pass,tag,wall_secs,wlog_start,wlog_end,rc" > "$OUT/timings.csv"
for p in $(seq 1 "$PASSES"); do
  for bin in "${BLOCKS[@]}"; do
    tag="$(basename "$bin" .bin)"; hints="${bin%.bin}.hints"
    s=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    t0=$(date +%s.%N)
    cargo-zisk remote prove -e "$ELF" -i "$bin" --hints "$hints" \
      -o "$OUT/${tag}.proof" --coordinator "$COORD" --timeout 0 > "$OUT/${tag}.p${p}.log" 2>&1
    rc=$?; t1=$(date +%s.%N); e=$(wc -l < "$WLOG" 2>/dev/null || echo 0)
    dt=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
    echo "$p,$tag,$dt,$s,$e,$rc" >> "$OUT/timings.csv"
    printf "  pass %s  %-12s  %6ss  (rc=%s)\n" "$p" "$tag" "$dt" "$rc"
  done
done
cp "$WLOG" "$OUT/worker.log"
echo "== DONE — results in ~/bench/ =="; cat "$OUT/timings.csv"
