#!/bin/bash
# gate-roots-record.sh <elf> <out.tsv> [jobs] [GEN=...] — the root gate, but the evidence survives.
#
# gate-roots.sh writes its per-block verdicts into a mktemp dir and deletes it on exit, so all that
# outlives a run is a line of text in a terminal. A lever reviewed later cannot be re-checked against
# that, and "200/200" then rests on someone's recollection. This writes a TSV that names the ELF by
# sha, the corpus by path, and every block with the root the guest produced and the root the corpus
# recorded -- so a disagreement can be located, not just counted.
#
# Same comparison as gate-roots.sh: the guest's output against the corpus's own recorded
# post_state_root, never against another ELF's output.
#
# FAIL CLOSED, and the first version of this script was not. Without `set -e` a glob matching no
# witness gave n_in=0, compared=0, and the final test then accepted 0 == 0 and 0 bad -- an empty
# corpus reported as a pass. Four things close it: the shell aborts on error, an empty corpus is
# refused outright, a non-zero ziskemu exit is recorded as EXEC_FAIL instead of being read as an
# empty root, and both roots must be exactly 64 hex characters before they are compared at all.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/paths.sh"
GEN="${GEN:-$(series_corpus canonical-2026-08-25815000-25815199-d49075fa3)}"
E="${1:?elf}"; OUT="${2:?out.tsv}"; JOBS="${3:-8}"
[ -f "$E" ] || { echo "no such elf: $E" >&2; exit 2; }
[ -d "$GEN" ] || { echo "no such corpus: $GEN" >&2; exit 2; }
SHA=$(shasum -a 256 "$E" | cut -c1-16)
export HERE E
RUN="$(mktemp -d)"; trap 'rm -rf "$RUN"' EXIT; export RUN

HEX64='^[0-9a-f]{64}$'
export HEX64

one() {
  # Deliberately NOT under `set -e`: every failure mode has to reach the TSV as its own verdict.
  # A silent early return is indistinguishable from a block that was never listed.
  set +e
  local w="$1" b d got want rc
  b=$(basename "$w" .witness); d="$RUN/$$"; mkdir -p "$d"
  if [ ! -f "${w%.witness}.post_state_root" ]; then printf '%s\tNOREF\t\t\n' "$b"; return 0; fi
  python3 "$HERE/frame.py" "$w" "$d/i.bin" >/dev/null 2>&1
  if [ $? -ne 0 ]; then printf '%s\tFRAME_FAIL\t\t\n' "$b"; return 0; fi
  ~/.zisk/bin/ziskemu -e "$E" -i "$d/i.bin" -o "$d/o.bin" >/dev/null 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then printf '%s\tEXEC_FAIL(rc=%s)\t\t\n' "$b" "$rc"; return 0; fi
  got=$(xxd -p -l32 "$d/o.bin" 2>/dev/null | tr -d '\n')
  want=$(sed 's/^0x//' "${w%.witness}.post_state_root" | tr -d '\n')
  # Shape before equality. Two empty strings compare equal, and that is exactly how a broken run
  # reports success; a truncated or hex-invalid root must not reach the comparison either.
  if ! [[ $got =~ $HEX64 ]]; then printf '%s\tBAD_OUTPUT\t%s\t%s\n' "$b" "$got" "$want"; return 0; fi
  if ! [[ $want =~ $HEX64 ]]; then printf '%s\tBAD_REF\t%s\t%s\n' "$b" "$got" "$want"; return 0; fi
  if [ "$got" = "$want" ]; then printf '%s\tOK\t%s\t%s\n' "$b" "$got" "$want"
  else printf '%s\tDIFFER\t%s\t%s\n' "$b" "$got" "$want"; fi
  return 0
}
export -f one

ls "$GEN"/*.witness > "$RUN/list" 2>/dev/null || true
n_in=$(wc -l < "$RUN/list" | tr -d ' ')
[ "$n_in" -gt 0 ] || { echo "gate-roots-record: corpus $GEN holds no .witness -- refusing" >&2; exit 3; }

# A recorded pass may be reused only when it still describes this exact ELF and
# corpus. Besides metadata and the summary, validate every block name and every
# expected root against the current corpus; a partial, duplicated, stale or
# failed TSV is a cache miss.
gate_cache_valid() {
  [ "${REUSE:-0}" = 1 ] && [ -s "$OUT" ] || return 1
  [ "$(awk -F'\t' '$1=="# elf_sha256_16"{print $2}' "$OUT")" = "$SHA" ] || return 1
  [ "$(awk -F'\t' '$1=="# corpus"{print $2}' "$OUT")" = "$GEN" ] || return 1
  [ "$(awk -F'\t' '$1=="# witnesses"{print $2}' "$OUT")" = "$n_in" ] || return 1
  grep -Fqx "# summary	compared=$n_in	ok=$n_in	bad=0" "$OUT" || return 1
  awk -F'\t' -v n="$n_in" '
    $1 !~ /^#/ && $1 != "block" {
      rows++; if (seen[$1]++) bad=1
      if ($2 != "OK" || $3 != $4 || length($3) != 64 || $3 !~ /^[0-9a-f]+$/) bad=1
    }
    END { exit !(rows == n && !bad) }
  ' "$OUT" || return 1
  sed 's#.*/##; s/\.witness$//' "$RUN/list" | LC_ALL=C sort > "$RUN/current-blocks"
  awk -F'\t' '$1 !~ /^#/ && $1 != "block"{print $1}' "$OUT" | LC_ALL=C sort > "$RUN/cached-blocks"
  cmp -s "$RUN/current-blocks" "$RUN/cached-blocks" || return 1
  awk -F'\t' '$1 !~ /^#/ && $1 != "block"{print $1 "\t" $4}' "$OUT" > "$RUN/cached-refs"
  local b want current
  while IFS=$'\t' read -r b want; do
    [ -f "$GEN/$b.post_state_root" ] || return 1
    current=$(sed 's/^0x//' "$GEN/$b.post_state_root" | tr -d '\n')
    [ "$current" = "$want" ] || return 1
  done < "$RUN/cached-refs"
  return 0
}

if gate_cache_valid; then
  echo "gate-roots-record: reusing complete gate elf=$SHA ok=$n_in/$n_in -> $OUT"
  exit 0
fi

{
  printf '# elf\t%s\n# elf_sha256_16\t%s\n# corpus\t%s\n# witnesses\t%s\n' \
         "$E" "$SHA" "$GEN" "$n_in"
  printf '#\nblock\tverdict\tgot\twant\n'
} > "$OUT"
xargs -P "$JOBS" -I{} bash -c 'one "$@"' _ {} < "$RUN/list" | sort >> "$OUT"

ok=$(awk -F'\t' '$1!~/^#/ && $1!="block" && $2=="OK"' "$OUT" | wc -l | tr -d ' ')
cmp_n=$(awk -F'\t' '$1!~/^#/ && $1!="block"' "$OUT" | wc -l | tr -d ' ')
bad=$((cmp_n - ok))
printf '# summary\tcompared=%s\tok=%s\tbad=%s\n' "$cmp_n" "$ok" "$bad" >> "$OUT"
awk -F'\t' '$1!~/^#/ && $1!="block" && $2!="OK" {print "  " $1 " " $2}' "$OUT" | head -5
echo "gate-roots-record: elf=$SHA compared=$cmp_n of $n_in ok=$ok bad=$bad -> $OUT"
# A run that measured fewer blocks than the corpus holds is not a pass either.
[ "$cmp_n" = "$n_in" ] || { echo "gate-roots-record: compared $cmp_n of $n_in -- FAIL" >&2; exit 4; }
[ "$bad" = 0 ] || exit 5
