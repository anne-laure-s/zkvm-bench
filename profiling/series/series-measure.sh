#!/bin/bash
# series-measure.sh <offset> [jobs] — steps and ZisK COST for every distinct ELF
# on the blocks at i % 24 == offset. Rows already in measure.tsv are skipped, so
# passes compose and a killed run loses nothing. Jobs default to 6: an
# sp1-runner peaks near 7.4 GB and stacking those with these once exhausted the
# machine, so the cap is deliberate rather than tuned.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/paths.sh"
# Overridable: which corpus a measurement ran against is part of the result.
GEN="${GEN:-$(series_corpus canonical-2026-08-25815000-25815199-d49075fa3)}"
[ -d "$GEN" ] || { echo "no such corpus: $GEN" >&2; exit 2; }
# BLOCKS_FILE selects an explicit witness sample. Without it the historical
# offset interface stays available for manual/resume runs.
if [ -n "${BLOCKS_FILE:-}" ]; then
  [ -f "$BLOCKS_FILE" ] || { echo "no such block list: $BLOCKS_FILE" >&2; exit 2; }
  OFF="${1:-selected}"
else
  OFF="${1:?offset is required when BLOCKS_FILE is not set}"
fi
JOBS="${2:-6}"; EMU=~/.zisk/bin/ziskemu
# INDEX/OUT are overridable so a second lineage measured under a different
# runtime writes its own table: 1.0 and 1.1 numbers must never share one.
INDEX="${INDEX:-$HERE/index.tsv}"; OUT="${OUT:-$HERE/measure.tsv}"
export HERE EMU OUT
touch "$OUT"
RUN="$(mktemp -d)"; progress_pid=""
cleanup() {
  [ -z "${progress_pid:-}" ] || kill "$progress_pid" 2>/dev/null || true
  rm -rf "$RUN"
}
trap cleanup EXIT
export RUN
shas=$(awk -F'\t' '$3=="OK"{print $4}' "$INDEX" | awk '!seen[$0]++')
: > "$RUN/selected"
if [ -n "${BLOCKS_FILE:-}" ]; then
  while IFS= read -r w; do
    [ -n "$w" ] || continue
    [ -f "$w" ] || { echo "selected witness does not exist: $w" >&2; exit 2; }
    printf '%s\n' "$w" >> "$RUN/selected"
  done < "$BLOCKS_FILE"
else
  i=0
  for w in "$GEN"/*.witness; do
    i=$((i+1)); [ $((i % ${STRIDE:-24})) -eq "$OFF" ] || continue
    printf '%s\n' "$w" >> "$RUN/selected"
  done
fi

# compare.py owns the shared content-addressed execution cache. Import any full
# RUN entry it already has for the same ELF bytes and witness contents before
# constructing the todo list; compare can therefore run first without series
# repeating its steps+COST passes.
python3 "$HERE/import-compare-cache.py" --index "$INDEX" \
  --blocks-file "$RUN/selected" --out "$OUT" --elf-dir "$HERE/elf" || exit 2

: > "$RUN/todo"
while IFS= read -r w; do
  b=$(basename "$w" .witness)
  for sha in $shas; do
    [ -f "$HERE/elf/$sha.elf" ] || continue
    grep -q "^$sha	$b	" "$OUT" 2>/dev/null || echo "$sha $w" >> "$RUN/todo"
  done
done < "$RUN/selected"
n=$(wc -l < "$RUN/todo" | tr -d ' ')
selected=$(wc -l < "$RUN/selected" | tr -d ' ')
echo "sample $OFF: $selected blocks, $n uncached pairs, $JOBS at a time"
[ "$n" -gt 0 ] || exit 0
one() {
  local sha="$1" w="$2" d b s c
  b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  python3 "$HERE/frame.py" "$w" "$d/i.bin" || return
  s=$("$EMU" -e "$HERE/elf/$sha.elf" -i "$d/i.bin" -m 2>&1 | grep -oE 'steps=[0-9]+' | head -1 | cut -d= -f2)
  c=$("$EMU" -e "$HERE/elf/$sha.elf" -i "$d/i.bin" -X -S --sdk --opcodes 2>&1 \
      | grep -oE 'COST[[:space:]]+[0-9,]+' | head -1 | grep -oE '[0-9,]+$' | tr -d ',')
  # A guest that aborts still exits 0 and still prints a step count, so refuse
  # anything implausibly small rather than record it as a measurement.
  [ -n "$s" ] && [ "$s" -gt 1000000 ] || return
  printf '%s\t%s\t%s\t%s\n' "$sha" "$b" "$s" "${c:-NA}"
}
export -f one
: > "$RUN/done"
progress_monitor() {
  local done pct bucket last=-1 filled bar i
  while :; do
    done=$(wc -l < "$RUN/done" | tr -d ' ')
    pct=$((done * 100 / n)); bucket=$((pct / 5))
    if [ "$bucket" -ne "$last" ] || [ "$done" -eq "$n" ]; then
      filled=$bucket; bar=""; i=0
      while [ "$i" -lt 20 ]; do
        if [ "$i" -lt "$filled" ]; then bar="${bar}#"; else bar="${bar}-"; fi
        i=$((i+1))
      done
      printf 'progress [%s] %3d%% (%d/%d)\n' "$bar" "$pct" "$done" "$n"
      last=$bucket
    fi
    [ "$done" -ge "$n" ] && break
    [ -f "$RUN/finished" ] && break
    sleep 5
  done
}
progress_monitor & progress_pid=$!

# Record completion separately from successful output: a failed pair must move
# the progress bar too, otherwise one bad witness leaves it stuck below 100%.
xargs -P "$JOBS" -n2 bash -c '
  one "$@"; rc=$?
  printf "%s\n" "$rc" >> "$RUN/done"
  exit "$rc"
' _ < "$RUN/todo" >> "$OUT"
xargs_rc=$?
: > "$RUN/finished"
wait "$progress_pid"
progress_pid=""
failed=$(awk '$1 != 0 { n++ } END { print n+0 }' "$RUN/done")
attempted=$(wc -l < "$RUN/done" | tr -d ' ')
missing=$((n - attempted)); failed=$((failed + missing))
[ "$failed" -eq 0 ] || echo "WARNING: $failed/$n measurement pairs produced no row"
python3 "$HERE/import-compare-cache.py" --publish --index "$INDEX" \
  --blocks-file "$RUN/selected" --out "$OUT" --elf-dir "$HERE/elf" || \
  echo "WARNING: could not publish series rows to compare cache"
echo "sample $OFF done, rows $(wc -l < "$OUT")"
[ "$xargs_rc" -eq 0 ]
