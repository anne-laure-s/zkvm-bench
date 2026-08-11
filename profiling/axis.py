#!/usr/bin/env python3
"""axis — list, add and remove compare.py axes, with the checks that catch a bad one early.

An axis is a same-zkVM guest pair (see RUNBOOK.md). Adding one by hand is three lines of dict, and the
mistakes it invites are all silent: an ELF that is not there resolves to nothing and the axis reads as
coverage while measuring nothing; a `unit` that disagrees with its backend mislabels every number; a
`src` that does not match how the guest reads its input yields a garbage-parse rather than an error.

    ./axis.py list                 every axis, its builds, and how many blocks it can run on
    ./axis.py show <name>          one axis in detail
    ./axis.py add <name> --backend zisk \\
        --a-name monad-r4-zisk --a-elf guests/monad-variants/r4/monad-r4-zisk.elf --a-src monad-framed \\
        --b-name zisk-reth       --b-elf guests/zisk-reth/zisk-reth.elf          --b-src bin
        ./axis.py rm <name>            remove one axis
        ./axis.py prune                remove every axis marked `ephemeral` — a campaign's scaffolding
        ./axis.py gc                   remove axes whose build was DELETED, told apart from a build
                                       this checkout never received by what the cache measured here

    `prune` and `gc` are dry runs unless given --yes. Both exist because a stale axis is quiet: the
    ELF resolves or it does not, and either way the axis goes on presenting itself as a comparison.

`add`, `rm`, `prune` and `gc` edit the AXES literal in compare.py in place. All re-parse the file
afterwards and refuse to write if the result does not load — an unparsable compare.py is worse than a
missing axis.
"""
import argparse
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache as _cachemod   # noqa: E402  (build identity + what was measured here)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMPARE = os.path.join(HERE, 'compare.py')

BACKENDS = {'zisk': 'steps', 'sp1': 'cycles'}
# Where the input comes from. NOT its shape: that is derived from the backend (compare.needs_framing),
# because a per-side shape could only ever match the backend or be broken.
SRCS = ('bin', 'monad')


def _axes_span(src):
    """(start, end) offsets of the AXES literal in compare.py's source."""
    i = src.index('AXES = {')
    j, depth = i + len('AXES = '), 0
    for j in range(i + len('AXES = '), len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                break
    return i + len('AXES = '), j + 1


def load_axes(src=None):
    src = src if src is not None else open(COMPARE).read()
    a, b = _axes_span(src)
    return ast.literal_eval(re.sub(r'#.*', '', src[a:b]))


def blocks_for(ax):
    """How many blocks this axis could run on, with NO --block-min/--block-max — the same resolution
    rule compare.py uses, over the whole corpus.

    The universe is every block with a Monad witness; an axis keeps those where BOTH sides resolve an
    input. A side missing inputs shortens the axis silently, so knowing the count before measuring is
    the difference between 'n=365 as expected' and 'n=3 and nobody noticed'."""
    import glob
    uni = set()
    for pat in ('guests/monad/fixtures/*.witness', 'guests/monad/inputs/*.witness'):
        for f in glob.glob(os.path.join(ROOT, pat)):
            m = re.search(r'(\d{6,})', os.path.basename(f))
            if m:
                uni.add(int(m.group(1)))

    def resolves(side, b):
        if side['src'] == 'bin':
            return any(os.path.exists(os.path.join(ROOT, 'guests', side['name'], d, f'1-{b}.bin'))
                       for d in ('fixtures', 'inputs'))
        return any(os.path.exists(os.path.join(ROOT, p)) for p in
                   (f'guests/monad/fixtures/{b}.witness', f'guests/monad/inputs/1-{b}.witness',
                    f'guests/monad/fixtures/1-{b}.witness'))
    return sum(1 for b in uni if resolves(ax['a'], b) and resolves(ax['b'], b))


def check(name, ax, axes, strict=True):
    """-> list of problems. Everything here is a mistake that would otherwise surface as a wrong
    number rather than as an error."""
    p = []
    if ax['backend'] not in BACKENDS:
        p.append(f"backend {ax['backend']!r} is not one of {sorted(BACKENDS)}")
    elif ax['unit'] != BACKENDS[ax['backend']]:
        p.append(f"unit {ax['unit']!r} does not match backend {ax['backend']!r} "
                 f"(expected {BACKENDS[ax['backend']]!r}) — every number would be mislabelled")
    for k in 'ab':
        s = ax.get(k)
        if not s:
            p.append(f"side {k!r} missing"); continue
        if s['src'] not in SRCS:
            p.append(f"{k}.src {s['src']!r} is not one of {list(SRCS)}")
        if not os.path.exists(os.path.join(ROOT, s['elf'])):
            p.append(f"{k}.elf does not exist: {s['elf']} — the axis would resolve nothing "
                     f"and read as coverage")
    if strict and name in axes:
        p.append(f"axis {name!r} already declared")
    return p


def cmd_list(args):
    axes = load_axes()
    print(f"{len(axes)} axes\n")
    print(f"  {'axis':16} {'bk':5} {'a':20} {'b':20} {'blocks*':>8}")
    n_eph = 0
    for name, ax in axes.items():
        probs = check(name, ax, axes, strict=False)
        n = blocks_for(ax) if not probs else '-'
        eph = ax.get('ephemeral')
        n_eph += bool(eph)
        print(f"  {name:16} {ax['backend']:5} {ax['a']['name']:20} {ax['b']['name']:20} {str(n):>7}"
              + ("  EPH" if eph else "     ") + ("  ⚠" if probs else ""))
        if eph:
            print(f"      ephemeral: {eph}")
        for x in probs:
            print(f"      ⚠ {x}")
    if n_eph:
        print(f"\n  EPH = {n_eph} ephemeral axis(es): one campaign's scaffolding, not a durable "
              f"comparison of the project.\n    Each pits a variant against a TIP, so it rots the "
              f"moment that tip moves — silently, since the ELF still\n    resolves and the axis still "
              f"reports coverage. `./axis.py prune` lists and removes them.")
    print("\n  * blocks with an input on BOTH sides, over the whole corpus and with no bounds."
          "\n    The canonical report adds --block-min/--block-max, so its n is lower.")
    return 0


def cmd_show(args):
    axes = load_axes()
    ax = axes.get(args.name)
    if not ax:
        sys.exit(f"no such axis: {args.name} (see ./axis.py list)")
    print(f"{args.name}  backend={ax['backend']} unit={ax['unit']}")
    for k in 'ab':
        s = ax[k]
        here = os.path.join(ROOT, s['elf'])
        print(f"  {k}: {s['name']}  src={s['src']}")
        print(f"     {s['elf']}  {'' if os.path.exists(here) else '<-- MISSING'}")
    print(f"  runs on {blocks_for(ax)} block(s) over the whole corpus (no --block-min/--block-max)")
    for x in check(args.name, ax, axes, strict=False):
        print(f"  ⚠ {x}")
    return 0


def cmd_add(args):
    src = open(COMPARE).read()
    axes = load_axes(src)
    ax = {'backend': args.backend, 'unit': BACKENDS.get(args.backend, args.unit),
          'a': {'name': args.a_name, 'elf': args.a_elf, 'src': args.a_src},
          'b': {'name': args.b_name, 'elf': args.b_elf, 'src': args.b_src}}
    if args.ephemeral:
        ax['ephemeral'] = args.ephemeral
    probs = check(args.name, ax, axes)
    if probs:
        for p in probs:
            print(f"  ✗ {p}")
        sys.exit(1)

    n = blocks_for(ax)
    # `ephemeral` first, so the reason an axis exists is the first thing read — and so `prune` and a
    # human skimming the literal see the same marker in the same place.
    eph = (f"'ephemeral': {ax['ephemeral']!r},\n             " if ax.get('ephemeral') else '')
    entry = (f"    '{args.name}': {{{eph}'backend': '{ax['backend']}', 'unit': '{ax['unit']}',\n"
             f"             'a': {{'name': '{ax['a']['name']}', 'elf': '{ax['a']['elf']}',\n"
             f"                   'src': '{ax['a']['src']}'}},\n"
             f"             'b': {{'name': '{ax['b']['name']}', 'elf': '{ax['b']['elf']}',\n"
             f"                   'src': '{ax['b']['src']}'}}}},\n")
    a, b = _axes_span(src)
    close = src.rindex('}', a, b)                    # the AXES closing brace
    out = src[:close] + entry + src[close:]
    _write_checked(out, args.name)
    print(f"  added {args.name} — runs on {n} block(s)")
    # Both bounds: --block-min drops the older unrelated blocks in guests/monad/inputs/, --block-max
    # pins the top so a set that grows at the tip does not silently widen a published comparison.
    print(f"  measure it: ./compare.py --block-min 25551991 --block-max 25552607 "
          f"--axis {args.name}")
    return 0


def cmd_rm(args):
    src = open(COMPARE).read()
    if args.name not in load_axes(src):
        sys.exit(f"no such axis: {args.name}")
    a, b = _axes_span(src)
    m = re.search(rf"\n[ \t]*'{re.escape(args.name)}':\s*\{{", src[a:b])
    if not m:
        sys.exit(f"{args.name} is declared in a shape this tool cannot edit — remove it by hand")
    start = a + m.start()
    depth, i = 0, a + m.end() - 1
    while i < b:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = src.index('\n', i)
    _write_checked(src[:start] + src[end:], args.name)
    print(f"  removed {args.name}")
    print("  its cached measurements are keyed by build, not by axis — nothing was discarded")
    return 0


def cmd_prune(args):
    """Remove every axis carrying `ephemeral`. Dry run unless --yes, because this edits compare.py.

    Pruning is a normal end-of-campaign step, not damage control. An ephemeral axis pits a variant
    against whatever the tip was that week; once the tip moves it compares against a base nobody cares
    about, and nothing says so — the ELF resolves, the blocks resolve, the numbers look like numbers.
    """
    axes = load_axes()
    doomed = [(n, a['ephemeral']) for n, a in axes.items() if a.get('ephemeral')]
    if not doomed:
        print("  no ephemeral axis to prune")
        return 0
    print(f"  {len(doomed)} ephemeral axis(es):")
    for n, why in doomed:
        print(f"    {n:18} {why}")
    if not args.yes:
        print("\n  dry run — nothing changed. Re-run with --yes to remove them.")
        return 0
    print()
    for n, _why in doomed:
        args.name = n
        cmd_rm(args)
    print(f"\n  pruned {len(doomed)}; the durable comparisons are untouched")
    return 0


def cmd_gc(args):
    """Remove axes whose build is GONE — deleted, not merely absent. Dry run unless --yes.

    An axis outliving its guest is worse than a broken one: compare.py skips it with a warning and
    carries on, so a campaign reports on fewer axes than it was asked for and the stale declaration
    survives in the source. Deciding which absences count is the whole job, and the filesystem cannot:
    a build never copied into a fresh clone looks exactly like one that was deleted. The cache settles
    it — measurements recorded HERE under that build's name prove the ELF existed on this machine.
    """
    axes = load_axes()
    c = _cachemod.Cache()
    doomed, unknown = [], []
    for name, ax in axes.items():
        for k in ('a', 'b'):
            side = ax[k]
            if os.path.exists(os.path.join(ROOT, side['elf'])):
                continue
            n = sum(len(c.profiles_for(i)) for i, _mt in c.builds_by_name(side['name']))
            (doomed if n else unknown).append((name, k, side['name'], side['elf'], n))
            break
    if unknown:
        print(f"  {len(unknown)} axis(es) whose build is absent but was never measured here — left "
              f"alone, most likely a build this checkout never received:")
        for nm, k, bn, elf, _n in unknown:
            print(f"    {nm:18} {k}: {bn}  ({elf})")
        print()
    if not doomed:
        print("  no axis has outlived its build")
        return 0
    print(f"  {len(doomed)} axis(es) whose build was measured here and is now gone:")
    for nm, k, bn, elf, n in doomed:
        print(f"    {nm:18} {k}: {bn}  {n} cached measurement(s)  ({elf})")
    if not args.yes:
        print("\n  dry run — nothing changed. Re-run with --yes to remove them.")
        print("  The measurements are keyed by build, not by axis: removing the axis discards nothing,")
        print("  and levers.py can still read a retired build by name.")
        return 0
    print()
    for nm, _k, _bn, _elf, _n in doomed:
        args.name = nm
        cmd_rm(args)
    print(f"\n  collected {len(doomed)} axis(es)")
    return 0


def _write_checked(out, name):
    """Write only if the result still parses. A compare.py that does not load costs more than a
    missing axis, and this tool edits it by text."""
    try:
        load_axes(out)
        compile(out, COMPARE, 'exec')
    except Exception as e:
        sys.exit(f"refusing to write: the edit would break compare.py ({type(e).__name__}: {e})")
    # Preserve the mode: os.replace swaps in a file created with the default umask, which drops
    # compare.py's executable bit and makes `./compare.py` fail with permission denied.
    mode = os.stat(COMPARE).st_mode
    tmp = COMPARE + '.tmp'
    with open(tmp, 'w') as fh:
        fh.write(out)
    os.chmod(tmp, mode)
    os.replace(tmp, COMPARE)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='every axis, with the blocks it can run on')
    sp = sub.add_parser('show', help='one axis in detail'); sp.add_argument('name')
    sp = sub.add_parser('add', help='declare a new axis in compare.py')
    sp.add_argument('name')
    sp.add_argument('--backend', required=True, choices=sorted(BACKENDS))
    sp.add_argument('--unit', help='defaults to the backend’s unit; only set it to override')
    for k in 'ab':
        sp.add_argument(f'--{k}-name', required=True)
        sp.add_argument(f'--{k}-elf', required=True, help='repo-relative path')
        sp.add_argument(f'--{k}-src', required=True, choices=SRCS)
    sp.add_argument('--ephemeral', metavar='WHY',
                    help="mark this axis as one campaign's scaffolding: say why it exists and when to "
                         "drop it. ./axis.py prune removes every axis carrying it.")
    sp = sub.add_parser('rm', help='remove an axis from compare.py'); sp.add_argument('name')
    sp = sub.add_parser('prune', help="remove every axis marked 'ephemeral' (dry run without --yes)")
    sp.add_argument('--yes', action='store_true', help='actually edit compare.py')
    sp = sub.add_parser('gc', help='remove axes whose build was deleted (dry run without --yes)')
    sp.add_argument('--yes', action='store_true', help='actually edit compare.py')
    args = ap.parse_args()
    return {'list': cmd_list, 'show': cmd_show, 'add': cmd_add, 'rm': cmd_rm,
            'prune': cmd_prune, 'gc': cmd_gc}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
