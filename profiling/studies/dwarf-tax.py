#!/usr/bin/env python3
"""dwarf-tax — attribute a guest's work by SOURCE LOCATION instead of by symbol name.

Why this exists. The taxonomy in `hotspots.py` matches regexes against demangled symbol names, and
that cannot see inlined code: work with no symbol is charged to whatever function absorbed it. The
two guests inline differently — C++ calls an outlined `__bswapdi2` while Rust inlines `swap_bytes`,
and the reth guest inlines its keccak wrapper while Monad calls one — so the two families carrying
the ENTIRE measured gap (hashing +36.0 M, byte/bit +23.6 M against a +53.9 M total on ZisK) are the
two a name-based taxonomy cannot compare. Renaming families does not create the missing information.

DWARF does. `.debug_line` maps every instruction address to a source file:line, and
`DW_TAG_inlined_subroutine` records the inline stack, so inlined work is attributed to the function
it was WRITTEN in rather than the one it was folded into. Combined with ziskemu's per-instruction
counters (`--disasm`, the same ones hotspots.py already reads), that gives a taxonomy that means the
same thing on both guests.

    ./dwarf-tax.py --elf <elf-with-dwarf> --input <framed.bin> [--top 25] [--json out.json]

Requires the ELF to carry DWARF: build with `-Cdebuginfo=2` (Rust) or `-g` (C++), and check with
`elf-equiv.py` that the debug build executes the identical step count first — otherwise the
attribution describes a program nobody benchmarked.
"""
import argparse
import bisect
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def line_table(elf_path):
    """Sorted (address, source-file) pairs from .debug_line.

    One entry per row of the line program, not per instruction: addresses between two rows belong to
    the earlier row, which is what the bisect at lookup time reproduces."""
    from elftools.elf.elffile import ELFFile
    with open(elf_path, 'rb') as f:
        elf = ELFFile(f)
        if not elf.has_dwarf_info():
            sys.exit(f"dwarf-tax: {elf_path} carries no DWARF — rebuild with -Cdebuginfo=2 / -g")
        dw = elf.get_dwarf_info()
        rows = []
        for cu in dw.iter_CUs():
            lp = dw.line_program_for_CU(cu)
            if lp is None:
                continue
            files = lp.header.get('file_entry') or []
            dirs = lp.header.get('include_directory') or []

            def name_of(idx):
                # DWARF 4 indexes files from 1; DWARF 5 from 0. pyelftools exposes the raw table, so
                # try the version first and fall back rather than silently mis-naming every row.
                v = lp.header.get('version', 4)
                i = idx if v >= 5 else idx - 1
                if not (0 <= i < len(files)):
                    return '?'
                fe = files[i]
                nm = fe.name.decode(errors='replace') if isinstance(fe.name, bytes) else str(fe.name)
                d = fe.get('dir_index', 0)
                if d and 0 <= (d - (0 if v >= 5 else 1)) < len(dirs):
                    dd = dirs[d - (0 if v >= 5 else 1)]
                    dd = dd.decode(errors='replace') if isinstance(dd, bytes) else str(dd)
                    return f"{dd}/{nm}"
                return nm

            for ent in lp.get_entries():
                st = ent.state
                if st is None or st.end_sequence:
                    continue
                rows.append((st.address, name_of(st.file)))
    rows.sort(key=lambda r: r[0])
    # collapse consecutive rows that name the same file: the lookup only needs boundaries
    out = []
    for a, f in rows:
        if not out or out[-1][1] != f:
            out.append((a, f))
    return out


def disasm_counts(emu, elf, inp):
    """{address: executed count} from ziskemu's annotated disassembly.

    Same source hotspots.py uses, read here per ADDRESS rather than per symbol — the whole point is
    to not go through symbols."""
    with tempfile.NamedTemporaryFile(suffix='.disasm', delete=False) as tf:
        dpath = tf.name
    try:
        subprocess.run([emu, '-e', elf, '-i', inp, '-o', os.devnull,
                        '-X', '-S', '--sdk', '--disasm', dpath],
                       capture_output=True, text=True)
        if not os.path.exists(dpath) or os.path.getsize(dpath) == 0:
            sys.exit("dwarf-tax: ziskemu produced no disassembly (--disasm)")
        counts = {}
        pat = re.compile(r'^\s*([0-9a-fA-F]{4,16})\s*:?\s.*?\s(\d[\d,]*)\s*$')
        for ln in open(dpath, errors='replace'):
            m = pat.match(ln.rstrip())
            if m:
                counts[int(m.group(1), 16)] = int(m.group(2).replace(',', ''))
        return counts
    finally:
        if os.path.exists(dpath):
            os.remove(dpath)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--elf', required=True)
    ap.add_argument('--input', required=True)
    ap.add_argument('--emu', default=os.path.expanduser('~/.zisk/bin/ziskemu'))
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--json', dest='json_out')
    a = ap.parse_args()

    rows = line_table(a.elf)
    print(f"  .debug_line : {len(rows)} plages d'adresses")
    counts = disasm_counts(a.emu, a.elf, a.input)
    print(f"  disassembly: {len(counts)} addresses executed\n")

    addrs = [r[0] for r in rows]
    per = collections.Counter()
    unattributed = 0
    for ad, c in counts.items():
        i = bisect.bisect_right(addrs, ad) - 1
        if i < 0:
            unattributed += c
            continue
        per[rows[i][1]] += c
    tot = sum(per.values()) + unattributed
    if not tot:
        sys.exit("dwarf-tax: nothing attributed — check that the ELF and the disassembly match")

    print(f"  {'part':>7}  fichier source")
    for f, c in per.most_common(a.top):
        print(f"  {c / tot * 100:6.2f}%  {f}")
    if unattributed:
        print(f"  {unattributed / tot * 100:6.2f}%  (hors table de lignes)")

    if a.json_out:
        json.dump({'elf': a.elf, 'input': a.input, 'total': tot,
                   'by_file': dict(per), 'unattributed': unattributed},
                  open(a.json_out, 'w'), indent=1)
        print(f"\n  wrote {a.json_out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
