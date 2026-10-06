#!/usr/bin/env bash
# paired.sh — the two guests on the SAME seven blocks, alternating. RUNS ON THE BOX.
#
# The r8 and reth arms measured earlier ran on disjoint block sets, so only their SLOPES could be
# compared. This runs both guests block by block, so the ratio is per block and the three readings
# (steps, COST, prove time) sit on the same rows.
#
# Order alternates per block: a fixed order lets the first guest of each pair pay costs the second
# does not, and that reversed a verdict once already in this repo (RTP-FINDINGS.md, gzip vs zstd).
#
# The setup runs per prove. Different ELFs get different Hash IDs so both could coexist, but the
# coordinator cache has already surprised us once (see tests/reth/README.md) — 2 s of setup is
# cheaper than another poisoned run, and it sits outside the clock.
#
#   PASSES=2 bash paired.sh
set -u
export PATH="$HOME/.zisk/bin:$PATH"
COORD="${COORD:-http://127.0.0.1:7000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ELF_R8="${ELF_R8:-$HOME/monad-r8-zisk.elf}"
ELF_RETH="${ELF_RETH:-$HOME/zisk-reth.elf}"
IN_R8="${IN_R8:-$HERE/../r8/inputs}"
IN_RETH="${IN_RETH:-$HERE/inputs-reth}"
OUT="${OUT:-$HOME/bench-paired}"; mkdir -p "$OUT"
WLOG="$HOME/zisk-infra/cluster/logs/worker.log"
PASSES="${PASSES:-2}"; WARMUPS="${WARMUPS:-1}"

for f in "$ELF_R8" "$ELF_RETH"; do [ -f "$f" ] || { echo "ERROR: no ELF at $f"; exit 1; }; done
mapfile -t BLOCKS < <(ls "$IN_R8"/1-*.bin 2>/dev/null | xargs -n1 basename | sed 's/\.bin$//')
[ "${#BLOCKS[@]}" -ge 1 ] || { echo "ERROR: no inputs in $IN_R8"; exit 1; }
for t in "${BLOCKS[@]}"; do [ -f "$IN_RETH/$t.bin" ] || { echo "ERROR: $IN_RETH/$t.bin missing — the two sides must cover the same blocks"; exit 1; }; done
# The const-tree generation can fail while the install still exits 0, leaving a key at its extraction
# size (~30 GB instead of ~70). The worker then starts, connects, and every setup fails. Cheaper to
# check the size here than to discover it 28 failed proves later.
KEYGB=$(du -sm "$HOME/.zisk/provingKey" 2>/dev/null | cut -f1); KEYGB=$((${KEYGB:-0}/1024))
[ "$KEYGB" -ge 40 ] || { echo "ERROR: provingKey is ${KEYGB} GB — const-trees look absent (expect ~70 GB)."; \
  echo "       Re-run: cargo-zisk check-setup --proving-key ~/.zisk/provingKey -a --gpu"; exit 1; }
echo "provingKey ${KEYGB} GB — ok"

grep -qaiE "registered (worker|successfully)" "$HOME/zisk-infra/cluster/logs/coordinator.log" 2>/dev/null \
  || { echo "ERROR: worker not registered — run start.sh first."; exit 1; }

snapshot_env() {
  { echo "date_utc      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "cargo_zisk    $(cargo-zisk --version 2>/dev/null)"
    echo "elf_r8        $(sha256sum "$ELF_R8"   | cut -d' ' -f1)"
    echo "elf_reth      $(sha256sum "$ELF_RETH" | cut -d' ' -f1)"
    echo "blocks        ${BLOCKS[*]}"
    nvidia-smi --query-gpu=name,driver_version,memory.used --format=csv,noheader 2>/dev/null | sed 's/^/gpu           /'
    echo "cpu           $(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo | sed 's/^ //')  ($(nproc) alloc)"
    echo "disk          $(df -h / | awk 'NR==2{print $3" used, "$4" free"}')"
  } > "$OUT/env.txt"; echo "== env -> $OUT/env.txt =="; sed 's/^/  /' "$OUT/env.txt"; }
save_logs() {
  cp "$WLOG" "$OUT/worker.log" 2>/dev/null || true
  cp "$HOME/zisk-infra/cluster/logs/coordinator.log" "$OUT/coordinator.log" 2>/dev/null || true; }

snapshot_env
echo "  r8   sha $(sha256sum "$ELF_R8"   | cut -c1-16)  expect fd39fe8c27533b6d"
echo "  reth sha $(sha256sum "$ELF_RETH" | cut -c1-16)  expect 979da60df5826c5e"

one() {  # one <tag> <guest> <pass>
  local tag="$1" g="$2" p="$3" elf inp t0 t1 rc dt
  if [ "$g" = r8 ]; then elf="$ELF_R8"; inp="$IN_R8/$tag.bin"; else elf="$ELF_RETH"; inp="$IN_RETH/$tag.bin"; fi
  # Outside the clock — and NOT discarded. A silent setup failure makes every prove fail with
  # "setup not done", and throwing its output away is how that becomes a mystery instead of a message.
  if ! cargo-zisk remote setup -e "$elf" --coordinator "$COORD" > "$OUT/setup.$g.p$p.log" 2>&1; then
    echo "  SETUP FAILED for $g — $OUT/setup.$g.p$p.log:"; sed 's/^/    /' "$OUT/setup.$g.p$p.log" | tail -6
  fi
  t0=$(date +%s.%N)
  cargo-zisk remote prove -e "$elf" -i "$inp" -o "$OUT/$tag.$g.proof" \
    --coordinator "$COORD" > "$OUT/$tag.$g.p$p.log" 2>&1
  rc=$?; t1=$(date +%s.%N); dt=$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")
  echo "$p,$tag,$g,$dt,$rc" >> "$OUT/paired.csv"
  printf "  p%-2s %-12s %-5s %8ss  (rc=%s)\n" "$p" "$tag" "$g" "$dt" "$rc"
}

echo "== warm-up x$WARMUPS (discarded) =="
for i in $(seq 1 "$WARMUPS"); do
  if ! cargo-zisk remote setup -e "$ELF_R8" --coordinator "$COORD" > "$OUT/setup.warm.$i.log" 2>&1; then
    echo "  SETUP FAILED — $OUT/setup.warm.$i.log:"; sed 's/^/    /' "$OUT/setup.warm.$i.log" | tail -8
  fi
  cargo-zisk remote prove -e "$ELF_R8" -i "$IN_R8/${BLOCKS[0]}.bin" -o /tmp/warm-p.proof \
    --coordinator "$COORD" > "$OUT/warmup.$i.log" 2>&1 \
    && echo "  warm $i ok" \
    || { echo "  warm $i FAIL — $OUT/warmup.$i.log:"; sed 's/^/    /' "$OUT/warmup.$i.log" | tail -8; }
done

echo "pass,tag,guest,secs,rc" > "$OUT/paired.csv"
i=0
for p in $(seq 1 "$PASSES"); do
  for t in "${BLOCKS[@]}"; do
    i=$((i+1))
    if [ $((i % 2)) -eq 1 ]; then one "$t" r8 "$p";   one "$t" reth "$p"
    else                        one "$t" reth "$p"; one "$t" r8   "$p"; fi
  done
done
save_logs
echo "== DONE — $OUT/paired.csv =="
tot=$(awk -F, 'NR>1' "$OUT/paired.csv" | wc -l | tr -d ' ')
bad=$(awk -F, 'NR>1&&$5!=0' "$OUT/paired.csv" | wc -l | tr -d ' ')
[ "$bad" = "$tot" ] && echo "!!! ALL $tot prove(s) FAILED — read $OUT/*.p1.log before fitting anything." >&2
[ "$bad" -gt 0 ] && [ "$bad" != "$tot" ] && echo "WARN: $bad/$tot rc!=0 — a pair with one bad half is dropped whole by pair-guests.py." >&2
cat "$OUT/paired.csv"
