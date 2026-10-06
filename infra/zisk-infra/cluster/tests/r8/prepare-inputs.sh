#!/usr/bin/env bash
# prepare-inputs.sh — stage the 7 framed r8 inputs for the Track C r8 arm. RUNS ON THE MAC.
#
# The inputs are not tracked: .gitignore keeps witnesses and framed .bin out of the tree because they
# are large and regenerable. This is the regeneration, and it is also the integrity check — every
# framed input is replayed through the ELF with ziskemu and must reproduce the step count recorded in
# the r8-vs-zisk-reth axis. A mismatch means the ELF, the witness generation and the runtime do not
# belong together, which is the one failure that produces confident nonsense downstream.
#
#   bash prepare-inputs.sh [OUTDIR]        default OUTDIR: ./inputs
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# r8 -> tests -> cluster -> zisk-infra -> infra -> repo root. Validated against a marker rather than
# trusted, so moving this directory fails loudly instead of reading the wrong tree.
REPO="$(cd "$HERE/../../../../.." && pwd)"
[ -f "$REPO/profiling/compare.py" ] || { echo "ERROR: $REPO is not the repo root — has this directory moved?" >&2; exit 1; }
OUT="${1:-$HERE/inputs}"

ELF="$REPO/guests/monad-variants/r8/monad-r8-zisk.elf"
WANT_SHA="fd39fe8c27533b6d06e83734ea15ab942fdfdac71800d5206d79efa155f6aae4"
TAR="$REPO/guests/monad/gen/zkvm-r4-gen-2026-08-9d7540181/witnesses-25551991-25552494.tar.zst"
AXIS="$REPO/profiling/r8-compare.json"
ZISKEMU="$HOME/.zisk/bin/ziskemu"

# p02 p15 p35 p50 p70 p90 p99+ of the r8 step distribution — spread on purpose, the fit needs a lever
# arm and a set clustered at the median gives a slope with no leverage.
BLOCKS=(25552266 25552304 25552294 25552158 25552167 25552110 25552376)

for f in "$ELF" "$TAR" "$AXIS" "$ZISKEMU"; do
  [ -e "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }
done

GOT_SHA="$(shasum -a 256 "$ELF" | cut -d' ' -f1)"
[ "$GOT_SHA" = "$WANT_SHA" ] || {
  echo "ERROR: $ELF is not monad-r8-zisk" >&2
  echo "  got    $GOT_SHA" >&2
  echo "  expect $WANT_SHA" >&2
  exit 1
}
echo "guest sha256 OK — $WANT_SHA"

# The step counts in the axis were taken with the ZisK 1.1 emulator; a 1.0 ziskemu counts a rebuilt
# guest differently and every verification below would fail for the wrong reason.
V="$("$ZISKEMU" --version 2>&1 | head -1)"
case "$V" in *1.1*) echo "ziskemu OK — $V" ;;
  *) echo "ERROR: the axis is ZisK 1.1; this ziskemu is '$V'" >&2; exit 1 ;; esac

mkdir -p "$OUT"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "extracting ${#BLOCKS[@]} witness(es) from $(basename "$TAR")"
tar --zstd -xf "$TAR" -C "$TMP" "${BLOCKS[@]/%/.witness}"

echo "framing and verifying against $(basename "$AXIS") [r8-vs-zisk-reth]"
python3 - "$TMP" "$OUT" "$ELF" "$AXIS" "$ZISKEMU" "${BLOCKS[@]}" <<'PY'
import json, os, struct, subprocess, sys
tmp, out, elf, axis, ziskemu, *blocks = sys.argv[1:]
rec = json.load(open(axis))['r8-vs-zisk-reth']['blocks']
rows, bad = [], []
print(f"  {'block':<10} {'recorded':>14} {'ziskemu':>14}")
for b in blocks:
    d = open(os.path.join(tmp, f'{b}.witness'), 'rb').read()
    dst = os.path.join(out, f'1-{b}.bin')
    with open(dst, 'wb') as f:
        f.write(struct.pack('<Q', len(d)) + d + b'\x00' * ((-(8 + len(d))) % 8))
    o = subprocess.run([ziskemu, '-e', elf, '-i', dst, '-m'],
                       capture_output=True, text=True).stdout
    got = int(next(t for t in o.split() if t.startswith('steps=')).split('=')[1])
    exp = rec[b]['a']['work']
    print(f"  {b:<10} {exp:>14,} {got:>14,}  {'OK' if got == exp else 'MISMATCH'}")
    (rows if got == exp else bad).append((b, exp))
if bad:
    sys.exit(f"\nERROR: {len(bad)} input(s) do not reproduce the axis — do not ship this set.")
with open(os.path.join(os.path.dirname(out.rstrip('/')) or '.', 'steps-r8.csv'), 'w') as f:
    f.write('tag,steps\n')
    for b, s in sorted(rows, key=lambda r: r[1]):
        f.write(f'1-{b},{s}\n')
print(f"\nall {len(rows)} input(s) reproduce the axis")
PY

echo
echo "staged in $OUT ($(du -sh "$OUT" | cut -f1))"
echo "steps-r8.csv rewritten from the axis"
