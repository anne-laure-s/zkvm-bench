#!/usr/bin/env python3
"""inline-robust — how much of a family's cross-guest ratio is real, and how much is inlining?

The family taxonomy classifies by symbol name, so work that gets inlined has no symbol and is charged
to whatever function absorbed it. The two guests inline very differently, and on one block that turned
a 12.80x hashing "gap" into 1.07x once the reth guest was rebuilt with inlining suppressed — while the
EVM interpreter barely moved (0.78x -> 0.77x). Some rows are readable as they stand; others are not,
and nothing in the report says which.

This measures that per family: profile the SAME blocks through the shipped guest and through a
no-inline rebuild, and report how far each family's share travels. A family that stays put can be
compared across guests; one that moves cannot, and the no-inline figure is the better estimate of
where the work actually lives.

    ./inline-robust.py --elf <no-inline.elf> --guest zisk-reth [--limit N] [--out out.json]

A no-inline build costs ~22% more steps — those are the calls inlining had removed — so it is a
DIAGNOSTIC instrument: it says where code lives, never what it costs. Results are cached per block, so
the run resumes rather than restarting.
"""
import argparse
import collections
import glob
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# `cache` is a sibling module, not a package — see the same note in compare.py.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cache as _cachemod          # noqa: E402
_cache = _cachemod.Cache()
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
hs = importlib.import_module('hotspots')


def reth_input(block):
    for d in ('inputs', 'fixtures'):
        for p in glob.glob(os.path.join(REPO, 'guests', 'zisk-reth', d, f'*{block}*.bin')):
            return p
    return None


def profile(elf, inputs, emu):
    """One hotspots process for a batch; returns {tag: {fn: count}}.

    Batched because process startup dominates a single block, chunked by the caller because a bad
    input aborts the whole process — losing a chunk is cheap, losing 365 blocks is not."""
    with tempfile.TemporaryDirectory() as td:
        cmd = [os.path.join(HERE, 'hotspots.py'), 'profile', '--backend', 'zisk',
               '--elf', elf, '--emu', emu, '--out', td]
        for i in inputs:
            cmd += ['-i', i]
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        pj = os.path.join(td, 'profile.json')
        if r.returncode != 0 or not os.path.exists(pj):
            return None, (r.stderr or r.stdout).strip()[-200:]
        return json.load(open(pj)), None


def verdict(out, verdict_path):
    """Reduce the no-inline profiles to one publishable ratio per family, and write them out.

    This exists because the reported figures must be rebuildable from the repo. The correction
    column in compare.html and the withdrawn claims in levers.html were first computed by a
    throwaway script: the numbers were right, but nothing could regenerate or re-check them, which
    for a published measurement is the same problem as being wrong.

    Method, chosen to match the ratio the table shows in the adjacent column rather than to be
    independently defensible: family counts scaled to real work per side (the cached profile keeps
    the top 120 symbols, ~98% of steps, so the unscaled sums understate each side by its own
    coverage — and the two coverages differ by 1.3%, which lands entirely in the ratio), then the
    MEDIAN OF PER-BLOCK RATIOS, which is the statistic the rest of the report uses."""
    import statistics as st
    ni_all = json.load(open(out))
    ni = ni_all[max(ni_all)]                      # newest no-inline stamp

    # Profiles come from the per-block cache (cache-format.md), looked up by build NAME and restricted
    # to that build's newest mtime. Restricting matters: a guest gets rebuilt and the older build
    # lingers in the cache, and reading it silently compared the wrong binary once already.
    def side(guest):
        profs, blocks, _mt = _cache.profiles_by_name(guest)
        return {str(b): v for b, v in zip(blocks, profs) if v.get('total')}

    M, E = side('monad-zisk'), side('zisk-reth')
    tri = sorted(set(M) & set(E) & set(ni))
    if not tri:
        print('  no block common to all three profiles — nothing to write'); return 1

    def scaled(v, f):
        kept = sum(n for _x, n in v['fns'])
        return sum(n for full, n in v['fns'] if hs.family(full) == f) * (v['total'] / kept)

    fams = {f for b in tri for f in ni[b]['fam']} | {hs.family(x) for b in tri for x, _n in M[b]['fns']}
    res = {}
    for f in sorted(fams):
        rs = [scaled(M[b], f) / scaled(E[b], f) for b in tri if scaled(E[b], f)]
        rn = [scaled(M[b], f) / ni[b]['fam'][f] for b in tri if ni[b]['fam'].get(f)]
        if len(rs) < 10 or len(rn) < 10:
            continue
        s, n_ = st.median(rs), st.median(rn)
        # A family the Monad guest never enters (runtime plumbing: 0 against reth's 76 k) has a
        # median ratio of 0, and no factor exists for it. Skip rather than publish a 0 the reader
        # would take for a measurement of the correction.
        if not s:
            continue
        res[f] = {'ship': round(s, 4), 'ni': round(n_, 4), 'factor': round(n_ / s, 4),
                  'n': min(len(rs), len(rn)),
                  # The three counts the ratios come from, so the prose can quote them instead of
                  # carrying them as literals: a stale "30.4 M" once contradicted the very column
                  # it was explaining (the real figure was 40.5 M, and the ratio 1.02x not 1.30x).
                  'steps': {'monad': round(st.median([scaled(M[b], f) for b in tri])),
                            'ship': round(st.median([scaled(E[b], f) for b in tri])),
                            'ni': round(st.median([ni[b]['fam'].get(f, 0) for b in tri]))}}
    # ── groups, because a single family's corrected ratio can be true and still mislead ───────────
    # `state / trie` reads 0.84x shipped and 1.92x de-inlined, and taken alone that says the Monad
    # guest is behind. It is the wrong unit: the SAME de-inlining sends `containers` the other way
    # (6.98x -> 0.34x), because reth's trie functions had inlined their container and hashing
    # helpers. The work did not appear, it changed label. Grouped, the three are at parity — which no
    # per-family column can show, so the groups are measured here and cited by both reports.
    GROUPS = {
        'trie+containers+hashing': ['state / trie', 'containers / abstraction',
                                    'hashing (keccak/sha)'],
        # The grouping levers.py already uses. It turns out to be the robust one: the relocation
        # happens INSIDE it, so its ratio barely moves between the two attributions.
        'reader trio': ['state / trie', 'containers / abstraction', 'witness decoding'],
    }
    grp = {}
    for name, fs in GROUPS.items():
        rs = [sum(scaled(M[b], f) for f in fs) / sum(scaled(E[b], f) for f in fs)
              for b in tri if sum(scaled(E[b], f) for f in fs)]
        rn = [sum(scaled(M[b], f) for f in fs) / sum(ni[b]['fam'].get(f, 0) for f in fs)
              for b in tri if sum(ni[b]['fam'].get(f, 0) for f in fs)]
        if rs and rn:
            grp[name] = {'ship': round(st.median(rs), 4), 'ni': round(st.median(rn), 4),
                         'families': fs}
    # The step inflation belongs in the file, not in the prose that reads it: the report used to
    # state it as a typed "~22%" with nothing to check it against.
    infl = st.median([ni[b]['steps'] for b in tri]) / st.median([E[b]['total'] for b in tri])
    # Two counts, and they are not the same: `blocks` is the three-way intersection the ratios
    # need, `ni_blocks` is the whole no-inline profile — which is what a claim about a family
    # being ABSENT from that build has to be quantified over.
    res['_meta'] = {'blocks': len(tri), 'ni_blocks': len(ni), 'inflation': round(infl, 4),
                    'groups': grp,
                    'method': 'median of per-block ratios, families scaled to real work per side'}
    json.dump(res, open(verdict_path, 'w'), indent=1)
    print(f"  wrote {verdict_path} — {len(res) - 1} families over {len(tri)} common blocks")
    for f, v in sorted(res.items(), key=lambda t: t[0]):
        if f != '_meta':
            print(f"    {f:<26} {v['ship']:>7.2f}x → {v['ni']:>6.2f}x   facteur {v['factor']:.2f}")
    print(f"    inflation du build no-inline : {(infl - 1) * 100:.1f}%")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--elf', help='the no-inline rebuild (not needed by --verdict-only)')
    ap.add_argument('--verdict-only', action='store_true',
                    help='recompute results/inline-verdict.json from the caches, profile nothing')
    ap.add_argument('--guest', default='zisk-reth')
    ap.add_argument('--emu', default=os.path.expanduser('~/.zisk/bin/ziskemu'))
    ap.add_argument('--limit', type=int, help='first N blocks only (smoke test)')
    ap.add_argument('--chunk', type=int, default=8)
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'inline-robust.json'))
    a = ap.parse_args()

    if a.verdict_only:
        return verdict(a.out, os.path.join(HERE, 'results', 'inline-verdict.json'))
    if not a.elf:
        ap.error('--elf is required unless --verdict-only')

    cmp_ = json.load(open(os.path.join(HERE, 'results', 'compare.json')))['zisk']
    blocks = sorted(cmp_['blocks'])
    if a.limit:
        blocks = blocks[:a.limit]

    cache = json.load(open(a.out)) if os.path.exists(a.out) else {}
    stamp = str(int(os.path.getmtime(a.elf)))
    cache.setdefault(stamp, {})
    store = cache[stamp]

    todo = [b for b in blocks if b not in store and reth_input(b)]
    print(f"  {len(blocks)} blocks · {len(store)} already cached · {len(todo)} to profile", flush=True)

    for i in range(0, len(todo), a.chunk):
        chunk = todo[i:i + a.chunk]
        ins = [reth_input(b) for b in chunk]
        prof, err = profile(a.elf, ins, a.emu)
        if prof is None:
            # One bad input aborts the whole hotspots process, so a chunk failure says nothing about
            # the other seven. Retry singly: a genuinely bad block then costs one block, not eight.
            print(f"  [warn] batch {chunk[0]}..{chunk[-1]} failed ({err}) — retrying block by block",
                  flush=True)
            prof = {}
            for b, one in zip(chunk, ins):
                p1, e1 = profile(a.elf, [one], a.emu)
                if p1 is None:
                    print(f"  [skip] {b}: {e1}", flush=True)
                else:
                    prof.update(p1)
        for tag, rec in prof.items():
            m = re.search(r'(\d{8})', tag)
            if not m:
                continue
            fam = collections.Counter()
            for f in rec['functions']:
                fam[hs.family(f['name'])] += f['count']
            store[m.group(1)] = {'total': rec['total_count'], 'steps': rec['meta']['steps'],
                                 'fam': dict(fam)}
        json.dump(cache, open(a.out, 'w'))
        print(f"  {min(i + a.chunk, len(todo))}/{len(todo)}", flush=True)

    print(f"\n  wrote {a.out} — {len(store)} blocks under stamp {stamp}")
    # Reduce in the same run: the two files drifted apart once already, the verdict having been
    # computed at 55 blocks while this cache kept growing to 193.
    return verdict(a.out, os.path.join(HERE, 'results', 'inline-verdict.json'))


if __name__ == '__main__':
    sys.exit(main())
