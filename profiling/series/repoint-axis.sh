#!/bin/bash
# repoint-axis.sh <axis> <sha|elf-path> — point ONE compare.py axis at a series ELF.
#
# Use this and never a bare `sed` over compare.py. The pattern
# `profiling/series/elf/<sha>.elf` appears 23 times across 15 axes, so substituting it
# file-wide repoints every one of them — including the ablation and lever axes, whose two
# sides then name the SAME ELF and report a ratio of exactly 1.000x, read as "this lever
# does nothing". Nothing warns: the report renders, the numbers are simply false. That is
# not hypothetical; it is why the driver stopped doing it, and it stays invisible whenever
# the named axis already holds the tip, because then the command looks like a no-op.
#
# So: scope the edit to the named axis, assert exactly one replacement, and refuse anything
# else. Idempotent — re-running on an axis already at <sha> changes nothing and exits 0.
#
#   ./repoint-axis.sh r10tip-vs-ziskethone 148f53c42c04310a
#   ./repoint-axis.sh r10tip-vs-ziskethone elf/148f53c42c04310a.elf   # path also accepted
#   DRY_RUN=1 ./repoint-axis.sh <axis> <sha>                          # print, change nothing
#
# Exit: 0 done or already there · 2 usage · 3 axis absent · 4 not exactly one ELF path
#       · 5 no such ELF in the series cache
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
COMPARE="${COMPARE:-$HERE/../compare.py}"

AXIS="${1:-}"; REF="${2:-}"
[ -n "$AXIS" ] && [ -n "$REF" ] || {
  echo "usage: $(basename "$0") <axis> <sha|elf-path>   (env: COMPARE, DRY_RUN)" >&2; exit 2; }

# Accept `elf/<sha>.elf`, an absolute path, or the bare sha — the tip is quoted as all three
# across the runbook and the driver, and guessing wrong here is a silent mis-measure.
SHA="$(basename "$REF" .elf)"
case "$SHA" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "not a 16-hex series ELF sha: $REF" >&2; exit 2 ;;
esac

# An axis pointed at an ELF that is not in the cache measures nothing and fails later, far from
# the cause. Check here, where the fix is obvious.
[ -f "$HERE/elf/$SHA.elf" ] || { echo "no such ELF in the series cache: $HERE/elf/$SHA.elf" >&2; exit 5; }
[ -f "$COMPARE" ] || { echo "no such compare.py: $COMPARE" >&2; exit 2; }

python3 - "$COMPARE" "$AXIS" "$SHA" "${DRY_RUN:-}" <<'PY'
import re, sys, pathlib
target, axis, sha, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
p = pathlib.Path(target)
s = p.read_text()
# The axis entry: from its quoted name to the closing `}},` of its last side.
m = re.search(r"('" + re.escape(axis) + r"':\s*\{.*?\}\},\n)", s, re.S)
if not m:
    print("axis %s not found in %s -- refusing to touch anything" % (axis, p.name),
          file=sys.stderr)
    raise SystemExit(3)
block = m.group(1)
# An axis that FOLLOWS a lineage resolves its own tip on every run; the `elf` line left in its
# declaration is an informational last-value, so rewriting it would change nothing measured and
# would read as if it had. Say so instead of doing it.
mt = re.search(r"'tip':\s*'([^']+)'", block)
if mt:
    print("%s follows %s and resolves its own tip -- nothing to repoint" % (axis, mt.group(1)))
    raise SystemExit(0)
new_block, n = re.subn(r"profiling/series/elf/[0-9a-f]{16}\.elf",
                       "profiling/series/elf/%s.elf" % sha, block)
if n != 1:
    print("axis %s names %d series ELF paths, expected exactly 1 -- refusing" % (axis, n),
          file=sys.stderr)
    raise SystemExit(4)
if new_block == block:
    print("%s already at %s" % (axis, sha))
elif dry:
    old = re.search(r"profiling/series/elf/([0-9a-f]{16})\.elf", block).group(1)
    print("DRY RUN: %s would move %s -> %s (1 path)" % (axis, old, sha))
else:
    old = re.search(r"profiling/series/elf/([0-9a-f]{16})\.elf", block).group(1)
    p.write_text(s[:m.start(1)] + new_block + s[m.end(1):])
    print("%s repointed %s -> %s (1 path)" % (axis, old, sha))
PY
