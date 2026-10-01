#!/bin/bash
# series-measure.sh <offset> [jobs] — steps and ZisK COST for every distinct ELF
# on the blocks at i % 24 == offset. Rows already in measure.tsv are skipped, so
# passes compose and a killed run loses nothing.
#
# Jobs default to 12, measured on this host (18 cores, 24 blocks of one ELF through the
# instrumented pass):
#
#     6 jobs   25 s   1.00x   2.6 GiB
#    12 jobs   16 s   1.56x   5.1 GiB
#    16 jobs   14 s   1.79x   6.6 GiB
#
# An instrumented ziskemu holds ~0.4 GiB, so memory is not the constraint it is for an
# sp1-runner, which peaks near 7.4 GB -- stacking THOSE exhausted this machine once, and a
# concurrent SP1 campaign is still the reason to pass a lower number by hand.
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
JOBS="${2:-12}"
# Overridable, and it must be: the ZisK release is both a BUILD input (its Rust toolchain
# compiles the guest) and a MEASUREMENT input (its cost model). 1.2.0-alpha prices a keccak
# permutation at 25x1538 where 1.1.0-alpha charged 25x3022, so the same ELF reports a COST
# ~20 % lower under it. A hardcoded path here would silently mix two cost models in one table.
EMU="${EMU:-$HOME/.zisk/bin/ziskemu}"
# INDEX/OUT are overridable so a second lineage measured under a different
# runtime writes its own table: 1.0 and 1.1 numbers must never share one.
INDEX="${INDEX:-$HERE/index.tsv}"; OUT="${OUT:-$HERE/measure.tsv}"
. "$HERE/root-ref.sh"
export HERE EMU OUT
export -f root_match root_refs
touch "$OUT"
RUN="$(mktemp -d)"; progress_pid=""
cleanup() {
  [ -z "${progress_pid:-}" ] || kill "$progress_pid" 2>/dev/null || true
  rm -rf "$RUN"
}
trap cleanup EXIT
export RUN
# Only ELFs that are actually in the cache: 85 tests here rather than one per (ELF, block) pair.
: > "$RUN/shas"
for sha in $(awk -F'\t' '$3=="OK"{print $4}' "$INDEX" | awk '!seen[$0]++'); do
  [ -f "$HERE/elf/$sha.elf" ] && printf '%s\n' "$sha" >> "$RUN/shas"
done
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
# The table is read ONCE, and the pairs still to do fall out of it. A test per (ELF, block) pair
# is 16,800 of them over an 85-commit lineage, each rescanning a 45,000-row table: eleven minutes
# of awk before the first measurement starts, during which the run prints nothing at all and is
# indistinguishable from a hang. One pass is under a second and selects exactly the same pairs.
#
# What counts as "already measured" depends on the mode. In full mode a QUICK row is not done: it
# carries steps and `NA` for COST, and skipping it would leave the table permanently half-measured
# with no way to notice. In quick mode any row will do, since quick adds nothing to one that exists.
awk -F'\t' -v quick="${QUICK:-0}" -v shasf="$RUN/shas" -v outf="$OUT" '
  FILENAME==shasf { shas[++n]=$1; next }
  FILENAME==outf  { if (NF>=4 && (quick==1 || ($4!="NA" && $4!=""))) done[$1 SUBSEP $2]=1; next }
  { b=$0; sub(/.*\//, "", b); sub(/\.witness$/, "", b)
    for (i=1; i<=n; i++) if (!((shas[i] SUBSEP b) in done)) print shas[i], $0 }
' "$RUN/shas" "$OUT" "$RUN/selected" > "$RUN/todo"
n=$(wc -l < "$RUN/todo" | tr -d ' ')
selected=$(wc -l < "$RUN/selected" | tr -d ' ')
echo "sample $OFF: $selected blocks, $n uncached pairs, $JOBS at a time"
[ "$n" -gt 0 ] || exit 0
one() {
  local sha="$1" w="$2" d b s c out rc got
  b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  python3 "$HERE/frame.py" "$w" "$d/i.bin" || {
    echo "MEASURE_FAIL sha=$sha block=$b reason=frame" >&2; return 1;
  }
  # ONE emulator run per pair. The instrumented pass reports STEPS, honours -o and yields COST, so
  # it already delivers everything the plain `-m` pass delivers: identical step counts and
  # byte-identical outputs on every block checked, and the same exit status on a bad input. QUICK=1
  # wants the steps alone, and for those `-m` is the cheaper of the two.
  if [ "${QUICK:-0}" = 1 ]; then
    out=$("$EMU" -e "$HERE/elf/$sha.elf" -i "$d/i.bin" -o "$d/o.bin" -m 2>&1); rc=$?
    s=$(printf '%s\n' "$out" | grep -oE 'steps=[0-9]+' | head -1 | cut -d= -f2)
    c=""
  else
    out=$("$EMU" -e "$HERE/elf/$sha.elf" -i "$d/i.bin" -o "$d/o.bin" -X -S --sdk --opcodes 2>&1); rc=$?
    s=$(printf '%s\n' "$out" | grep -oE 'STEPS[[:space:]]+[0-9,]+' | head -1 | grep -oE '[0-9,]+$' | tr -d ',')
    c=$(printf '%s\n' "$out" | grep -oE 'COST[[:space:]]+[0-9,]+' | head -1 | grep -oE '[0-9,]+$' | tr -d ',')
  fi
  [ "$rc" -eq 0 ] || {
    echo "MEASURE_FAIL sha=$sha block=$b reason=ziskemu-rc-$rc" >&2; return 1;
  }
  [ -n "$s" ] || {
    echo "MEASURE_FAIL sha=$sha block=$b reason=no-steps" >&2; return 1;
  }
  # Validate the execution itself instead of guessing from its size. Block 25815183 is a legitimate
  # ~300k-step block, so the old 1M-step floor rejected it for almost every ELF. Conversely, an
  # incompatible runtime can exit 0 and report steps while writing 256 zero bytes, so the run is
  # accepted on its public output and not on its length.
  [ -n "$(root_refs "$w")" ] || {
    echo "MEASURE_FAIL sha=$sha block=$b reason=no-reference-root" >&2; return 1;
  }
  got=$(xxd -p -l32 "$d/o.bin" 2>/dev/null | tr -d '\n')
  # Either public output is accepted: the lineage emits the post-state root up to
  # `guest: only expose blockhash as a public input` and the block hash after it, and both are
  # established independently of the guest under test (gen-blockhashes.sh).
  root_match "$w" "$got" >/dev/null || {
    echo "MEASURE_FAIL sha=$sha block=$b reason=root-mismatch got=${got:-none} refs=$(root_refs "$w")" >&2
    return 1
  }
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
