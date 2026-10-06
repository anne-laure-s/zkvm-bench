#!/usr/bin/env bash
# prepare-inputs.sh — stage the zisk-reth side of the paired round. RUNS ON THE MAC.
#
# The r8 arm's inputs come from tests/r8/. This stages the SAME SEVEN BLOCKS for zisk-reth, so the two
# guests are compared block by block instead of through a slope over two disjoint sets.
#
# No framing here: zisk-reth's inputs are already framed input.bin from input-gen, unlike the monad
# witnesses. No hints either — measured to buy nothing (tests/reth/README.md), which is what makes this
# round possible at all: no .hints exists inside the axis range.
#
#   bash prepare-inputs.sh [OUTDIR]        default OUTDIR: ./inputs-reth
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../../../.." && pwd)"
[ -f "$REPO/profiling/compare.py" ] || { echo "ERROR: $REPO is not the repo root" >&2; exit 1; }
OUT="${1:-$HERE/inputs-reth}"

ELF="$REPO/guests/zisk-reth/zisk-reth.elf"
WANT_SHA="979da60df5826c5eb1e178380d46d95235bc01ddd064a6e0bb387b90a418c0d3"
AXIS="$REPO/profiling/r8-compare.json"
ZISKEMU="$HOME/.zisk/bin/ziskemu"
BLOCKS=(25552266 25552304 25552294 25552158 25552167 25552110 25552376)

for f in "$ELF" "$AXIS" "$ZISKEMU"; do [ -e "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }; done
GOT="$(shasum -a 256 "$ELF" | cut -d' ' -f1)"
[ "$GOT" = "$WANT_SHA" ] || { echo "ERROR: $ELF is not the 1.1 zisk-reth build" >&2
  echo "  got    $GOT" >&2; echo "  expect $WANT_SHA" >&2; exit 1; }
echo "guest sha256 OK — zisk-reth $WANT_SHA"
V="$("$ZISKEMU" --version 2>&1 | head -1)"
case "$V" in *1.1*) echo "ziskemu OK — $V" ;;
  *) echo "ERROR: the axis is ZisK 1.1; this ziskemu is '$V'" >&2; exit 1 ;; esac

mkdir -p "$OUT"
python3 - "$OUT" "$ELF" "$AXIS" "$ZISKEMU" "$REPO" "${BLOCKS[@]}" <<'PY'
import json, os, shutil, subprocess, sys
out, elf, axis, ziskemu, repo, *blocks = sys.argv[1:]
rec = json.load(open(axis))['r8-vs-zisk-reth']['blocks']
rows, bad = [], []
print(f"  {'block':<10} {'axis (reth)':>14} {'ziskemu':>14}")
for b in blocks:
    src = os.path.join(repo, 'guests/zisk-reth/inputs', f'1-{b}.bin')
    dst = os.path.join(out, f'1-{b}.bin')
    if not os.path.exists(src):
        bad.append((b, 'no .bin')); print(f"  {b:<10} {'':>14} {'MISSING':>14}"); continue
    shutil.copyfile(src, dst)
    o = subprocess.run([ziskemu, '-e', elf, '-i', dst, '-m'], capture_output=True, text=True).stdout
    got = int(next(t for t in o.split() if t.startswith('steps=')).split('=')[1])
    exp = rec[b]['b']['work']
    print(f"  {b:<10} {exp:>14,} {got:>14,}  {'OK' if got == exp else 'MISMATCH'}")
    (rows if got == exp else bad).append((b, exp))
if bad:
    sys.exit(f"\nERROR: {len(bad)} input(s) unusable — do not ship this set.")
with open(os.path.join(os.path.dirname(out.rstrip('/')) or '.', 'steps-reth-paired.csv'), 'w') as f:
    f.write('tag,steps\n')
    for b, s in sorted(rows, key=lambda r: r[1]): f.write(f'1-{b},{s}\n')
print(f"\nall {len(rows)} input(s) reproduce the axis")
PY
echo; echo "staged in $OUT ($(du -sh "$OUT" | cut -f1))"
