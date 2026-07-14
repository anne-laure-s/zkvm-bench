#!/usr/bin/env bash
# ev.sh — run each Monad witness on the ZisK guest and verify the state root.
# Output: one line per block to stdout (steps, output size, PASS/MISMATCH verdict), PLUS a
# machine-readable recap in exec-verified.csv (block,steps,emu_secs,root_match).
# Runs from anywhere: paths are resolved relative to the script's location.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../guests/monad

ELF="$HERE/monad-zkvm-guest-zisk.elf"                 # ZisK guest that commits the state root
EMU="$HOME/.zisk/bin/ziskemu"
WIT="$HERE/inputs"
OUT="$HERE/execute-out"; mkdir -p "$OUT"

[ -f "$ELF" ] || { echo "ELF not found: $ELF"; exit 1; }
[ -x "$EMU" ] || { echo "ziskemu not found: $EMU"; exit 1; }

CSV="$HERE/exec-verified.csv"
echo "block,steps,emu_secs,root_match" > "$CSV"
printf "%-16s %14s  %-9s %s\n" "block" "steps" "out_bytes" "verify"
find "$WIT" -name '*.witness' | sort | while read -r w; do
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
  exp=$(find "$WIT" -name "${tag}*.post_state_root" | head -1)
  verdict=$(python3 - "$OUT/$tag.out" "$exp" "$rc" <<'PY'
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
if not exp: print("no-expected")
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
