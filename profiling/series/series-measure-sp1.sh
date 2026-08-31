#!/bin/bash
# series-measure-sp1.sh [nblocks] [jobs] — cycles and PGU per distinct SP1 ELF on
# a spread sample. Narrower than the ZisK series on purpose: SP1 execution costs
# roughly twenty times as much per block.
#
# Two things this gets wrong if copied carelessly:
#   - SP1 reads the witness VERBATIM. Framing it the ziskos way (LE64 + pad) does
#     not fail, it parses as garbage and returns ~8,000 cycles on every block.
#   - sp1-runner defaults to the CUDA prover and panics without that feature.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/paths.sh"
# All three overridable: which corpus a measurement ran against is part of the result, and a
# hardcoded runner hides an sp1-runner A/B the same way a hardcoded toolchain hides a compiler one.
OUTD="${OUTD:-$(series_repo)/profiling/series-sp1}"
GEN="${GEN:-$(series_corpus zkvm-r4-gen-2026-08-9d7540181)}"
RUNNER="${RUNNER:-$(series_repo)/infra/sp1-infra/sp1-runner/target/release/sp1-runner}"
[ -d "$GEN" ]    || { echo "no such corpus: $GEN" >&2; exit 2; }
[ -x "$RUNNER" ] || { echo "no sp1-runner at $RUNNER (cargo build --release)" >&2; exit 2; }
N="${1:-20}"; JOBS="${2:-4}"
export SP1_PROVER=cpu OUTD RUNNER
touch "$OUTD/measure.tsv"
RUN="$(mktemp -d)"; trap 'rm -rf "$RUN"' EXIT; export RUN
shas=$(awk -F'\t' '$3=="OK"{print $4}' "$OUTD/index.tsv" | awk '!seen[$0]++')
all=("$GEN"/*.witness); step=$(( ${#all[@]} / N )); [ "$step" -lt 1 ] && step=1
: > "$RUN/todo"; i=0
for w in "${all[@]}"; do
  i=$((i+1)); [ $((i % step)) -eq 0 ] || continue
  b=$(basename "$w" .witness)
  for sha in $shas; do
    [ -f "$OUTD/elf/$sha.elf" ] || continue
    grep -q "^$sha	$b	" "$OUTD/measure.tsv" 2>/dev/null || echo "$sha $w" >> "$RUN/todo"
  done
done
n=$(wc -l < "$RUN/todo" | tr -d ' ')
echo "sp1: $n pairs, $JOBS at a time"
[ "$n" -gt 0 ] || exit 0
one() {
  local sha="$1" w="$2" d b; b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  "$RUNNER" --mode execute --elf "$OUTD/elf/$sha.elf" --input "$w" --report "$d/r.json" >/dev/null 2>&1
  [ -f "$d/r.json" ] || return
  python3 -c "
import json
j=json.load(open('$d/r.json')); c=j.get('cycles'); g=j.get('gas')
# a guest that gives up still reports; refuse anything implausibly small
if c and c > 1000000: print('$sha\t$b\t%s\t%s' % (c, g))"
}
export -f one
xargs -P "$JOBS" -n2 bash -c 'one "$@"' _ < "$RUN/todo" >> "$OUTD/measure.tsv"
echo "sp1 done, rows $(wc -l < "$OUTD/measure.tsv")"
