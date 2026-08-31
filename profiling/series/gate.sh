#!/bin/bash
# gate.sh <elfA> <elfB> [step] — public-value output of A vs B over the corpus.
# One job per block: the emulator is single-threaded and blocks are independent.
# Each job gets its own scratch dir — two gates sharing fixed temp names once
# reported divergences that were not there.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/paths.sh"
# Overridable, for the reason gate-roots.sh gives: which corpus a gate ran against is part of
# the result, and a hardcoded path makes "504 compared" unfalsifiable.
GEN="${GEN:-$(series_corpus zkvm-r4-gen-2026-08-9d7540181)}"
A="${1:?elfA}"; B="${2:?elfB}"; STEP="${3:-1}"; JOBS="${JOBS:-6}"
[ -f "$A" ] || { echo "no such elf: $A" >&2; exit 2; }
[ -f "$B" ] || { echo "no such elf: $B" >&2; exit 2; }
[ -d "$GEN" ] || { echo "no such corpus: $GEN" >&2; exit 2; }
export HERE A B
RUN="$(mktemp -d)"; trap 'rm -rf "$RUN"' EXIT; export RUN
one() {
  local w="$1" b d ra rb
  b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  python3 "$HERE/frame.py" "$w" "$d/g.bin" || { echo "FRAME_FAIL $b"; return; }
  ~/.zisk/bin/ziskemu -e "$A" -i "$d/g.bin" -o "$d/a.bin" >/dev/null 2>&1; ra=$?
  ~/.zisk/bin/ziskemu -e "$B" -i "$d/g.bin" -o "$d/b.bin" >/dev/null 2>&1; rb=$?
  # Exit codes and shape BEFORE equality. Two guests that fail the same way agree perfectly:
  # a guest that cannot speak the emulator's runtime runs to completion and writes 256 ZERO bytes,
  # so `cmp` would call that a match. That is the failure gate-roots.sh exists to avoid, and this
  # gate has to refuse it rather than report it as agreement.
  [ "$ra" -eq 0 ] || { echo "EXEC_FAIL_A(rc=$ra) $b"; return; }
  [ "$rb" -eq 0 ] || { echo "EXEC_FAIL_B(rc=$rb) $b"; return; }
  for f in a b; do
    [ -s "$d/$f.bin" ] || { echo "NO_OUTPUT_${f} $b"; return; }
    [ -n "$(xxd -p -l32 "$d/$f.bin" | tr -d '\n0')" ] || { echo "ZERO_ROOT_${f} $b"; return; }
  done
  if cmp -s "$d/a.bin" "$d/b.bin"; then echo "OK $b"; else echo "DIFFER $b"; fi
}
export -f one
# FAIL CLOSED. An unexpanded glob used to walk this loop once with the literal pattern: frame.py
# then failed on it, `res` held one FRAME_FAIL line, and the summary printed
# `compared=1 differ=0` -- a missing corpus reporting as a pass. The count is taken from the LIST
# and every non-OK verdict is a failure, so nothing reads as agreement by default.
i=0; : > "$RUN/list"
for w in "$GEN"/*.witness; do
  [ -f "$w" ] || continue
  i=$((i+1)); [ $((i % STEP)) -eq 0 ] && echo "$w" >> "$RUN/list"
done
n_in=$(wc -l < "$RUN/list" | tr -d ' ')
[ "$n_in" -gt 0 ] || { echo "gate: corpus $GEN holds no .witness (step=$STEP) -- refusing" >&2; exit 3; }
xargs -P "$JOBS" -I{} bash -c 'one "$@"' _ {} < "$RUN/list" > "$RUN/res"
grep -v '^OK ' "$RUN/res" | head -10
n_out=$(wc -l < "$RUN/res" | tr -d ' '); ok=$(grep -c '^OK ' "$RUN/res")
bad=$((n_out - ok))
# Naming the corpus is part of the verdict: "compared=504 ok=504" over an unnamed GEN is
# unfalsifiable, and the two gates in this directory do not share a default.
echo "gate: listed=$n_in compared=$n_out ok=$ok not-ok=$bad  corpus=$GEN"
# A run that produced fewer verdicts than it listed is not a pass either: a killed job leaves no line.
[ "$n_out" = "$n_in" ] || { echo "gate: $n_out verdicts for $n_in blocks -- FAIL" >&2; exit 4; }
[ "$bad" = 0 ] || exit 5
