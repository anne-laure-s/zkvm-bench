#!/bin/bash
# gate-roots.sh <elf> [jobs] — check the guest's post-state root against the
# corpus's own recorded value, for every witness.
#
# This and not a two-ELF comparison whenever a runtime boundary is in play: a
# 1.0-runtime guest under the 1.1 emulator runs to completion and writes 256 zero
# bytes, so comparing outputs would compare a guest that speaks to one that does
# not. The corpus root is independent of both.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/paths.sh"
# Overridable, and it must be: which corpus a gate ran against is part of the result, and a
# hardcoded path here makes a report of "200 roots verified" unfalsifiable when the default holds
# 504.
GEN="${GEN:-$(series_corpus zkvm-r4-gen-2026-08-9d7540181)}"
E="${1:?elf}"; JOBS="${2:-6}"
[ -f "$E" ] || { echo "no such elf: $E" >&2; exit 2; }
[ -d "$GEN" ] || { echo "no such corpus: $GEN" >&2; exit 2; }
export HERE E
RUN="$(mktemp -d)"; trap 'rm -rf "$RUN"' EXIT; export RUN
one() {
  local w="$1" b d got want
  b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  [ -f "${w%.witness}.post_state_root" ] || { echo "NOREF $b"; return; }
  python3 "$HERE/frame.py" "$w" "$d/i.bin" || { echo "FRAME_FAIL $b"; return; }
  ~/.zisk/bin/ziskemu -e "$E" -i "$d/i.bin" -o "$d/o.bin" >/dev/null 2>&1
  got=$(xxd -p -l32 "$d/o.bin" 2>/dev/null | tr -d '\n')
  want=$(sed 's/^0x//' "${w%.witness}.post_state_root")
  if [ "$got" = "$want" ]; then echo "OK $b"; else echo "DIFFER $b"; fi
}
export -f one
# FAIL CLOSED, same reasoning as gate-roots-record.sh: an empty corpus used to print
# `compared=0 ok=0 bad=0` and exit 0, which reads as a pass. Refuse it, and refuse a run that
# returned fewer verdicts than it listed.
ls "$GEN"/*.witness > "$RUN/list" 2>/dev/null || true
n_in=$(wc -l < "$RUN/list" | tr -d ' ')
[ "$n_in" -gt 0 ] || { echo "gate-roots: corpus $GEN holds no .witness -- refusing" >&2; exit 3; }
xargs -P "$JOBS" -I{} bash -c 'one "$@"' _ {} < "$RUN/list" > "$RUN/res"
grep -v '^OK ' "$RUN/res" | head -5
n_out=$(wc -l < "$RUN/res" | tr -d ' '); ok=$(grep -c '^OK ' "$RUN/res")
bad=$((n_out - ok))
# Same as gate.sh: name the corpus. gate-roots.sh and gate-roots-record.sh default to
# DIFFERENT generations (r4-gen vs canonical-25815xxx), so which one ran is not guessable
# from the script name.
echo "gate-roots: listed=$n_in compared=$n_out ok=$ok bad=$bad  elf=$(shasum -a256 "$E" | cut -c1-16)  corpus=$GEN"
[ "$n_out" = "$n_in" ] || { echo "gate-roots: $n_out verdicts for $n_in blocks -- FAIL" >&2; exit 4; }
[ "$bad" = 0 ] || exit 5
