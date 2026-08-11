#!/usr/bin/env bash
# ev.sh — run each Monad witness on the ZisK guest and verify the state root.
# Output: one line per block to stdout (steps, output size, PASS/MISMATCH verdict), PLUS a
# machine-readable recap in exec-verified.csv (block,steps,emu_secs,root_match).
# Runs from anywhere: paths are resolved relative to the script's location.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../guests/monad

ELF="${ELF:-$HERE/monad-zkvm-guest-zisk.elf}"         # ZisK guest that commits the state root
EMU="${EMU:-$HOME/.zisk/bin/ziskemu}"
# fixtures/, not inputs/: `fixtures` is the generation-selected witness set (see ./use-gen and the
# README) — it follows whichever guest generation is current. `inputs/` predates that split and holds
# 12 blocks from an older range, so verifying it said nothing about the set being measured.
WIT="${WIT:-$HERE/fixtures}"
OUT="${OUT:-$HERE/execute-out}"; mkdir -p "$OUT"

[ -f "$ELF" ] || { echo "ELF not found: $ELF"; exit 1; }
[ -x "$EMU" ] || { echo "ziskemu not found: $EMU"; exit 1; }

CSV="$HERE/exec-verified.csv"
echo "block,steps,emu_secs,root_match" > "$CSV"
printf "%-16s %14s  %-9s %s\n" "block" "steps" "out_bytes" "verify"
find -L "$WIT" -name '*.witness' | sort | while read -r w; do
  tag=$(basename "$w" .witness)
  bin="$OUT/$tag.bin"
  python3 - "$w" "$bin" <<'PY'
import sys,struct
d=open(sys.argv[1],'rb').read(); n=len(d); pad=(-(8+n))%8
open(sys.argv[2],'wb').write(struct.pack('<Q',n)+d+b'\x00'*pad)
PY
  t0=$(python3 -c 'import time;print(time.time())')
  "$EMU" -e "$ELF" -i "$bin" -o "$OUT/$tag.out" -m >"$OUT/$tag.log" 2>&1; rc=$?
  t1=$(python3 -c 'import time;print(time.time())')
  emu_secs=$(python3 -c 'import sys;print(f"{float(sys.argv[2])-float(sys.argv[1]):.3f}")' "$t0" "$t1")
  # same extraction as zisk-runner (first integer on a 'steps' line) so this CSV matches exec-report.json
  steps=$(grep -iE 'steps?' "$OUT/$tag.log" | grep -oE '[0-9][0-9,]*' | head -n1 | tr -d ',')
  # Prefer the FULL expected public values (96 B: post || pre || block hash) when the set carries
  # them — the guest commits to three values since the soundness binding, and checking only the
  # post root leaves two thirds of the output unverified. Falls back to .post_state_root for
  # older sets and for the first block of a set (no local parent post-root).
  pv=$(find -L "$WIT" -name "${tag}.expected_pv" | head -1)
  exp=$(find -L "$WIT" -name "${tag}*.post_state_root" | head -1)
  verdict=$(python3 - "$OUT/$tag.out" "$exp" "$rc" "$pv" <<'PY'
import sys,os
outf,expf,rc=sys.argv[1],sys.argv[2],sys.argv[3]
if rc!='0': print(f"EMU-FAIL(rc={rc})"); sys.exit()
got=open(outf,'rb').read() if os.path.exists(outf) else b''
h=''
if expf and os.path.exists(expf):
    h=open(expf).read().strip().lower()
    if h.startswith('0x'): h=h[2:]                      # the .post_state_root file is "0x<hex>"
    h=''.join(c for c in h if c in '0123456789abcdef')
exp=bytes.fromhex(h) if len(h)%2==0 and h else b''
pvf=sys.argv[4] if len(sys.argv)>4 else ''
if pvf and os.path.exists(pvf):
    # Exact, positional, all three values — `exp in got` was a SUBSTRING test: it passed on a
    # post root sitting anywhere in the output and said nothing about the other 64 bytes.
    want=open(pvf,'rb').read()
    if len(got)<len(want): print(f"SHORT({len(got)}B)")
    elif got[:len(want)]==want: print("PASS(pv3)")
    else:
        which=[n for n,(i,j) in (('post',(0,32)),('pre',(32,64)),('hash',(64,96)))
               if got[i:j]!=want[i:j]]
        print("MISMATCH(" + ",".join(which) + ")")
elif not exp: print("no-expected")
elif exp in got: print("PASS")
elif exp[::-1] in got: print("PASS(rev)")
else: print("MISMATCH")
PY
)
  ob=$(wc -c < "$OUT/$tag.out" 2>/dev/null | tr -d ' '); ob=${ob:-0}   # portable size (stat -f%z was macOS-only)
  printf "%-16s %14s  %-9s %s\n" "$tag" "${steps:-?}" "$ob" "$verdict"
  echo "$tag,${steps:-},$emu_secs,$verdict" >> "$CSV"
done
echo "wrote $CSV"
