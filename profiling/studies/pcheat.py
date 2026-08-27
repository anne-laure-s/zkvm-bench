#!/usr/bin/env python3
"""pcheat — an exact per-PC heat map inside one symbol, reconciled against the totals above it.

The .disasm counters ziskemu writes with --disasm are in STEPS: their sum over every PC equals the
report's STEPS line exactly, and their sum inside one symbol equals what hotspots.py reports for that
symbol. This tool checks both of those before it prints anything, then groups the hot PCs of a symbol
by DWARF source line and by instruction, which is what a function-granularity profile cannot say.

It reuses hotspots.py's own two regexes rather than parsing the format again. studies/dwarf-tax.py
carries a THIRD parser that expects the counter at the end of the line, where the current format puts
it right after the address -- on one cached file it reads 56,519 steps of 78.78 M. Do not revive that
one; there should be exactly one parser and it should be the one the profile is built on.

Not basic blocks: ZisK emits `.L` labels for its own micro-operations as well as for real block
boundaries, so a `.L`-delimited region is not a CFG block. Per-PC, per-source-line and per-instruction
is exact and enough to find a hot sequence; a CFG can come later if a lever needs one.

  ./studies/pcheat.py <disasm> --elf <elf> --steps <N> --symbol <substring> [--top 40]
"""
import argparse, importlib.util as iu, os, re, subprocess, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = iu.spec_from_file_location('_hs', os.path.join(HERE, '..', 'hotspots.py'))
_hs = iu.module_from_spec(_spec); _spec.loader.exec_module(_hs)
LAB, CNT = _hs._lab, _hs._cnt          # ONE parser, shared with the profile

PCLINE = re.compile(r'^\s*([0-9a-f]{8}):\s+(\d+)\s+(\S*)\s*(.*)$')


def parse(path):
    """PCs per symbol, in file order. `cur` follows hotspots.py: a `.L` label does not change the
    owning symbol."""
    per, cur = collections.defaultdict(list), '(prologue)'
    for line in open(path, 'r', errors='replace'):
        m = LAB.match(line)
        if m:
            if not m.group(1).startswith('.L'):
                cur = m.group(1)
            continue
        c = PCLINE.match(line)
        if c:
            per[cur].append((int(c.group(1), 16), int(c.group(2)),
                             c.group(3) or '(uop)', c.group(4).strip()))
    return per


def addr2line(elf, pcs):
    """Batch DWARF lookup. Missing line info yields '?', which is reported rather than hidden."""
    tool = os.path.expanduser('~/riscv_gcc_multilib/bin/riscv64-unknown-elf-addr2line')
    if not os.path.exists(tool) or not pcs:
        return {pc: '?' for pc in pcs}
    p = subprocess.run([tool, '-e', elf, '-f', '-i'] + [f'0x{pc:x}' for pc in pcs],
                       capture_output=True, text=True)
    out, res, i = p.stdout.splitlines(), {}, 0
    # -f -i emits pairs (function, file:line), possibly several per address for inlining; the LAST
    # pair of a group is the innermost frame, and groups are in request order.
    groups, cur = [], []
    for l in out:
        cur.append(l)
        if ':' in l and not l.endswith(':'):
            groups.append(cur); cur = []
    for pc, g in zip(pcs, groups):
        res[pc] = g[-1].split('/')[-1] if g else '?'
    for pc in pcs:
        res.setdefault(pc, '?')
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('disasm')
    ap.add_argument('--elf', required=True)
    ap.add_argument('--steps', type=int, required=True, help="the report's STEPS, for gate 1")
    ap.add_argument('--symbol', action='append', required=True)
    ap.add_argument('--top', type=int, default=40)
    a = ap.parse_args()

    per = parse(a.disasm)

    # ── gate 1: every PC in the file sums to STEPS ──────────────────────────────────────────────
    total = sum(c for pcs in per.values() for _, c, _, _ in pcs)
    ok1 = total == a.steps
    print(f"gate 1  all PCs = STEPS        {total:,} vs {a.steps:,}  {'OK' if ok1 else 'MISMATCH'}")

    # ── gate 2: the per-symbol totals match hotspots.py's own aggregation ───────────────────────
    ref_tot, ref_funcs, _ = _hs._zisk_disasm(a.disasm, 10 ** 9)
    ref = {f['name']: f['count'] for f in ref_funcs}
    ok2 = True
    for want in a.symbol:
        for name, pcs in per.items():
            if want in name:
                mine = sum(c for _, c, _, _ in pcs)
                theirs = ref.get(_hs.demangle(name)[:90])
                match = (theirs is not None and mine == theirs)
                ok2 &= match
                print(f"gate 2  {want[:28]:28} {mine:,} vs hotspots {theirs if theirs is not None else '(absent)'}"
                      f"  {'OK' if match else 'MISMATCH'}")
    if not (ok1 and ok2):
        print("\nA gate failed: the numbers below would not be reconcilable, so nothing is printed.")
        return 1

    for want in a.symbol:
        for name, pcs in sorted(per.items()):
            if want not in name:
                continue
            sym_total = sum(c for _, c, _, _ in pcs)
            lines = addr2line(a.elf, [pc for pc, _, _, _ in pcs])

            print(f"\n{'=' * 100}\n{_hs.demangle(name)[:96]}\n  {sym_total:,} steps over {len(pcs)} PCs")

            # ── by source line ──────────────────────────────────────────────────────────────────
            byline = collections.Counter()
            for pc, c, _, _ in pcs:
                byline[lines.get(pc, '?')] += c
            # ── gate 3: the groups sum back to the symbol ───────────────────────────────────────
            g3 = sum(byline.values())
            print(f"  gate 3  groups = symbol      {g3:,} vs {sym_total:,}  {'OK' if g3 == sym_total else 'MISMATCH'}")
            print(f"\n  by source line:")
            for src, c in byline.most_common(12):
                print(f"    {c:12,}  {100 * c / sym_total:5.1f}%  {src}")

            byop = collections.Counter()
            for _, c, op, _ in pcs:
                byop[op] += c
            print(f"\n  by instruction:")
            for op, c in byop.most_common(12):
                print(f"    {c:12,}  {100 * c / sym_total:5.1f}%  {op}")

            print(f"\n  hottest PCs:")
            for pc, c, op, txt in sorted(pcs, key=lambda x: -x[1])[:a.top]:
                print(f"    {c:12,}  {100 * c / sym_total:5.1f}%  {pc:08x}  {op:10} {txt[:44]:44} {lines.get(pc, '?')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
