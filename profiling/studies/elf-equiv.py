#!/usr/bin/env python3
"""elf-equiv — is a debug-info build the same program as the release build?

A source-location taxonomy needs DWARF, and none of the shipped guest ELFs carry any. Adding
`-g` / `-Cdebuginfo=2` is supposed to be orthogonal to codegen, so the debug build should be a
faithful lens on the binary we actually measure. "Supposed to" is not a measurement: a different
toolchain version, a stale lockfile or an LTO decision can change the code while nobody looks, and
then every attribution drawn from it describes a program that was never benchmarked.

So: run both ELFs on the same inputs and require the executed step count to match EXACTLY. Steps are
deterministic on ZisK, so equality is the right test — not "close".

    ./elf-equiv.py --a guests/zisk-reth/zisk-reth.elf --b /tmp/zec-reth-dbg \\
                   -i guests/zisk-reth/inputs/1-25552053.bin [-i ...]

Exit 0 when every input matches, 1 otherwise. Prints the per-input counts either way, because a
mismatch is a finding: it says the debug build is a different program, and by how much.
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sections(path):
    """Section names of an ELF, so the report can say which side actually carries DWARF."""
    import struct
    # read the whole file: a debug build's section headers sit past any fixed prefix, and a
    # truncated read fails with a struct error that reads like a corrupt ELF
    b = open(path, 'rb').read()
    if b[:4] != b'\x7fELF':
        return []
    shoff, = struct.unpack_from('<Q', b, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', b, 0x3a)
    base = shoff + shstrndx * shentsize
    stroff, strsz = struct.unpack_from('<QQ', b, base + 0x18)
    names = b[stroff:stroff + strsz]
    out = []
    for i in range(shnum):
        o = shoff + i * shentsize
        n, = struct.unpack_from('<I', b, o)
        out.append(names[n:names.index(b'\0', n)].decode(errors='replace'))
    return out


def steps(emu, elf, inp):
    """Executed step count, or None with the reason attached."""
    r = subprocess.run([emu, '-e', elf, '-i', inp, '-o', os.devnull, '-m'],
                       capture_output=True, text=True)
    m = re.search(r'steps=(\d+)', r.stdout + r.stderr)
    if not m:
        return None, (r.stdout + r.stderr).strip()[-200:]
    return int(m.group(1)), None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--a', required=True, help='reference ELF (the one the numbers came from)')
    ap.add_argument('--b', required=True, help='candidate ELF (the debug-info rebuild)')
    ap.add_argument('-i', '--input', action='append', required=True, help='framed input; repeatable')
    ap.add_argument('--emu', default=os.path.expanduser('~/.zisk/bin/ziskemu'))
    a = ap.parse_args()

    for p in (a.a, a.b, a.emu):
        if not os.path.exists(p):
            sys.exit(f"elf-equiv: missing {p}")

    for label, elf in (('A', a.a), ('B', a.b)):
        dbg = [s for s in sections(elf) if s.startswith('.debug')]
        print(f"  {label}  {os.path.basename(elf):<34} {os.path.getsize(elf) / 1e6:6.2f} MB  "
              f"DWARF: {', '.join(dbg[:3]) if dbg else 'none'}")
    print()

    bad = 0
    for inp in a.input:
        sa, ea = steps(a.emu, a.a, inp)
        sb, eb = steps(a.emu, a.b, inp)
        tag = os.path.basename(inp)
        if sa is None or sb is None:
            print(f"  !!  {tag:<28} A={sa or ea} B={sb or eb}")
            bad += 1
            continue
        ok = sa == sb
        bad += not ok
        d = '' if ok else f"  Δ {sb - sa:+,} ({(sb / sa - 1) * 100:+.4f}%)"
        print(f"  {'OK' if ok else '!!'}  {tag:<28} {sa:>14,} vs {sb:>14,}{d}")

    print()
    if bad:
        print(f"  {bad}/{len(a.input)} input(s) differ — the debug build is NOT the measured program.\n"
              f"  Do not draw attribution from it until the cause is found (toolchain version, "
              f"lockfile, LTO).")
        return 1
    print(f"  {len(a.input)}/{len(a.input)} identical — the debug build executes the same program, "
          f"so its DWARF describes the binary the numbers came from.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
