#!/usr/bin/env python3
"""summary.md from a run.sh results directory. Runs on the box at the end of run.sh, and anywhere
else on a fetched copy.

    summarize.py <results dir> [--tps 50]

Medians over passes, per input, of the proofs that succeeded (one pass by default). Then, per
arm and worker config,
the line `secs = fixed + slope x Msteps` -- the shape zkvm-bench's mainnet runs gave
(5.33 + 0.159 x Msteps on one RTX 5090, ZisK 1.1, measured 18-289 Msteps). These blocks reach
down to a single transaction, so the fixed part is measured here instead of extrapolated, and it
is the figure a latency target lives or dies by.

The sizing table puts that line against a transaction rate: blocks every `interval` seconds hold
`tps x interval` transactions, a prover takes `secs` for one, and keeping up takes
ceil(secs / interval) provers -- each with this run's GPUs -- working on successive blocks. The
latency is a transaction's wait for its block to close (half an interval on average) plus the
proof. The proofs are STARK (VADCOP final) proofs: there is no SNARK wrap to add.

A run that proves every input of one ELF in a row (bench-l2.sh's ORDER=elf, the default) times
the arms in different stretches of it; recheck.csv, the first ELF's first inputs proved again at
the end, gives the end-over-start ratio that bounds how much of an arm-to-arm ratio is drift.
"""
import csv
import math
import pathlib
import statistics as st
import sys

res = pathlib.Path(sys.argv[1])
TPS = float(sys.argv[sys.argv.index('--tps') + 1]) if '--tps' in sys.argv else 50.0
inputs_csv = next(res.rglob('inputs.csv'), None) or (pathlib.Path(__file__).parent / 'inputs' / 'inputs.csv')
INP = {r['id']: r for r in csv.DictReader(open(inputs_csv))}


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def ols(xs, ys):
    n = len(xs)
    if n < 2:
        return math.nan, math.nan, math.nan
    mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return math.nan, math.nan, math.nan
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / tot if tot else math.nan
    return a, b, r2


def medians(path, key='secs', kind=None):
    """{id: median secs} over the successful rows of a timings csv."""
    by = {}
    for r in csv.DictReader(open(path)):
        if r.get('rc') != '0' or (kind and r.get('kind') != kind):
            continue
        by.setdefault(r['id'], []).append(fnum(r[key]))
    return {k: st.median(v) for k, v in by.items()}


out = []
p = out.append
p(f'# L2 proof latency — {res.name}\n')
env = res / 'stark-all' / 'env.txt'
env = env if env.exists() else next(res.rglob('env.txt'), None)
if env:
    p('```\n' + env.read_text().strip() + '\n```\n')

pre = res / 'precheck.csv'
if pre.exists():
    rs = list(csv.DictReader(open(pre)))
    p(f'Inputs replayed under this box\'s ziskemu before proving: '
      f'{sum(r["ok"] == "True" for r in rs)}/{len(rs)} publish their block.\n')
pub = res / 'publics.csv'
if pub.exists():
    rs = list(csv.DictReader(open(pub)))
    p(f'Proofs verified and read back after the timing: '
      f'{sum(r["ok"] == "True" for r in rs)}/{len(rs)} commit to their block'
      + (f', verified under the timed install\'s own setup key' if rs and all(r.get('pinned') == 'True' for r in rs) else '') + '.\n')

fits = {}
for tdir in sorted(res.glob('stark-*')):
    t = tdir / 'timings.csv'
    if not t.exists():
        continue
    gset = tdir.name[len('stark-'):]
    med = medians(t)
    rows = list(csv.DictReader(open(t)))
    n_ok = sum(r['rc'] == '0' for r in rows)
    p(f'## STARK proofs, worker on {gset} GPU(s) — {n_ok}/{len(rows)} proves succeeded\n')

    def sec(rid):
        return med.get(rid, math.nan)

    rc = tdir / 'recheck.csv'
    if rc.exists():
        tried = list(csv.DictReader(open(rc)))
        again = [r for r in tried if r['rc'] == '0']
        rel = [fnum(r['secs']) / sec(r['id']) for r in again if not math.isnan(sec(r['id']))]
        if tried and not rel:
            p(f'Drift check: none of the {len(tried)} proofs taken again at the end succeeded, so '
              'this run has no bound on its drift (recheck.csv, logs/*.recheck.log).\n')
        if rel:
            p(f"Drift check: {len(rel)} proofs of `{again[0]['arm']}` taken again at the end of the "
              f"run, end / start = {st.median(rel):.3f} (each: "
              + ', '.join(f'{x:.3f}' for x in rel) + '). Arm-to-arm ratios within that much of 1 '
              'are within the drift.\n')

    # the sweep, every arm side by side, one row per block size (medians over its blocks): the
    # keccak chain's L2 (JUMPDEST in software), each optional arm that was staged -- the JUMPDEST
    # precompile, Keccak-f in software, the Poseidon2 trie with and without it, the chain with
    # everything on Poseidon2 -- and the plaintext control; ratios are block for block, then
    # medians, each the lever's arm over the arm without it
    pairs = sorted({r['pair'] for r in INP.values() if r['set'] == 'sweep'})
    staged = {r['arm'] for r in INP.values()}
    # (arm, its seconds column, the ratio's numerator and denominator arms, the ratio's column)
    extra = [e for e in (
        ('l2-precompile', 'L2, precompile s', 'l2', 'l2-precompile', 'L2 / precompile'),
        ('l2-keccak-sw', 'L2, Keccak-f sw s', 'l2-keccak-sw', 'l2', 'Keccak-f sw / L2'),
        ('l2-poseidon', 'L2, Poseidon2 trie s', 'l2-poseidon', 'l2', 'Poseidon2 trie / L2'),
        ('l2-poseidon-ksw', 'L2, Poseidon2 trie + Keccak-f sw s', 'l2-poseidon-ksw', 'l2',
         'Poseidon2 trie + Keccak-f sw / L2'),
        ('l2-poseidon-sig', 'L2, Poseidon2 trie and signatures s', 'l2-poseidon-sig', 'l2',
         'Poseidon2 trie and signatures / L2'),
        ('l2-poseidon-sig-ksw', 'L2, Poseidon2 trie and signatures + Keccak-f sw s',
         'l2-poseidon-sig-ksw', 'l2', 'Poseidon2 trie and signatures + Keccak-f sw / L2'),
        ('l2-poseidon-all-ksw', 'L2, all Poseidon2 + Keccak-f sw s', 'l2-poseidon-all-ksw', 'l2',
         'all Poseidon2 + Keccak-f sw / L2'),
    ) if e[0] in staged]
    md = lambda v: st.median(v) if v else math.nan
    # an arm that did not prove a size (the Keccak-f arm stops at 250 tx) shows a dash
    f2 = lambda x, spec='.2f': '–' if math.isnan(x) else format(x, spec)

    if 'l2' not in staged:
        # Arms staged by name (prepare-inputs.py --arm), none of them the keccak chain's L2 the
        # tables below are built around: every arm per block size, medians over the size's blocks,
        # and each arm over the first one staged, block for block.
        names = list(dict.fromkeys(r['arm'] for r in INP.values()))
        ref = names[0]
        p(f"The sweep, payouts' mix: median s per block size, and each arm over `{ref}`, block "
          'for block.\n')
        p('| tx | ' + ' | '.join(f'{n} s' for n in names) + ' |'
          + ''.join(f' {n} / {ref} |' for n in names[1:]))
        p('|---:|' + '---:|' * (2 * len(names) - 1))
        for n_tx in sorted({int(r['txs']) for r in INP.values() if r['set'] == 'sweep'}):
            qs = sorted({r['pair'] for r in INP.values()
                         if r['set'] == 'sweep' and int(r['txs']) == n_tx})
            col = lambda arm: md([x for x in (sec(f'{arm}-{q}') for q in qs) if not math.isnan(x)])
            rat = lambda arm: md([x for x in (sec(f'{arm}-{q}') / sec(f'{ref}-{q}') for q in qs)
                                  if not math.isnan(x)])
            if all(math.isnan(col(n)) for n in names):
                continue
            p(f'| {n_tx:,} | ' + ' | '.join(f2(col(n)) for n in names) + ' |'
              + ''.join(f' {f2(rat(n), ".3f")} |' for n in names[1:]))
        p('')
        presets = sorted({r['pair'] for r in INP.values() if r['set'] == 'preset'})
        if any(not math.isnan(sec(f'{n}-{q}')) for n in names for q in presets):
            p('| preset block | ' + ' | '.join(f'{n} s' for n in names) + ' |')
            p('|---|' + '---:|' * len(names))
            for q in presets:
                p(f"| {q[len('preset-'):]} | " + ' | '.join(f2(sec(f'{n}-{q}')) for n in names) + ' |')
            p('')
        p('| arm | n | fixed s | s per Msteps | Msteps/s | R2 |')
        p('|---|---:|---:|---:|---:|---:|')
        for arm in names:
            xs = [int(INP[rid]['steps']) / 1e6 for rid, x in med.items()
                  if rid in INP and INP[rid]['arm'] == arm and not math.isnan(x)]
            ys = [x for rid, x in med.items()
                  if rid in INP and INP[rid]['arm'] == arm and not math.isnan(x)]
            a_, b_, r2 = ols(xs, ys)
            if xs:
                p(f'| {arm} | {len(xs)} | {a_:.2f} | {b_:.4f} | {1 / b_ if b_ else math.nan:.1f} '
                  f'| {r2:.4f} |')
        p('')
        gpu = tdir / 'gpu.csv'
        if gpu.exists():
            util = [fnum(l.split(',')[1]) for l in open(gpu) if l.count(',') >= 2]
            util = [u for u in util if not math.isnan(u)]
            if util:
                p(f'GPU utilisation while proving: mean {st.mean(util):.0f} %, median '
                  f'{st.median(util):.0f} %.\n')
        continue

    def ratio(qs, a, b):
        v = [sec(f'{a}-{q}') / sec(f'{b}-{q}') for q in qs]
        return md([x for x in v if not math.isnan(x)])

    def size_table(groups):
        """One row per block size, every arm side by side; `groups` are the pairs of each size."""
        p('| tx / block | Msteps (L2) | L2 s |' + ''.join(f' {e[1]} |' for e in extra) +
          ' control s |' + ''.join(f' {e[4]} |' for e in extra) + ' L2 / control |')
        p('|---:|---:|---:|' + '---:|' * len(extra) + '---:|' + '---:|' * len(extra) + '---:|')
        for qs in sorted(groups, key=lambda qs: int(INP[f'l2-{qs[0]}']['txs'])):
            ms = st.median(int(INP[f'l2-{q}']['steps']) for q in qs) / 1e6
            col = lambda arm: md([s for s in (sec(f'{arm}-{q}') for q in qs) if not math.isnan(s)])
            # a size the run left out (ONLY) has no row
            if all(math.isnan(col(arm)) for arm in ['l2', 'control'] + [e[0] for e in extra]):
                continue
            row = f"| {INP[f'l2-{qs[0]}']['txs']} | {ms:.2f} | {f2(col('l2'))} |"
            row += ''.join(f" {f2(col(e[0]))} |" for e in extra)
            row += f" {f2(col('control'))} |"
            row += ''.join(f" {f2(ratio(qs, e[2], e[3]), '.3f')} |" for e in extra)
            row += f" {f2(ratio(qs, 'l2', 'control'), '.3f')} |"
            p(row)
        p('')

    def by_label(set_name):
        qs = sorted({r['pair'] for r in INP.values() if r['set'] == set_name and f"l2-{r['pair']}" in INP})
        labs = sorted({INP[f'l2-{q}']['label'] for q in qs})
        return [[q for q in qs if INP[f'l2-{q}']['label'] == lab] for lab in labs]

    p('The sweep, payouts\' mix:\n')
    size_table(by_label('sweep'))
    # the wholesale preset at more sizes, with the preset's own blocks as its 21-transaction row
    ws = by_label('wholesale')
    if ws:
        pw = sorted(q for q in {r['pair'] for r in INP.values()} if q.startswith('preset-wholesale-b')
                    and f'l2-{q}' in INP)
        p('Wholesale, by size:\n')
        size_table(ws + ([pw] if pw else []))
    pres = sorted({r['pair'] for r in INP.values() if r['set'] == 'preset'})
    if pres:
        p('| preset block | tx | Msteps (L2) | L2 s |' + ''.join(f' {e[1]} |' for e in extra) +
          ' control s |')
        p('|---|---:|---:|---:|' + '---:|' * len(extra) + '---:|')
        for q in pres:
            l2, ct = f'l2-{q}', f'control-{q}'
            if l2 in INP:
                p(f"| {q[len('preset-'):]} | {INP[l2]['txs']} | {int(INP[l2]['steps'])/1e6:.2f} | "
                  f"{f2(sec(l2))} |" + ''.join(f" {f2(sec(e[0] + '-' + q))} |" for e in extra) +
                  f" {f2(sec(ct))} |")
        p('')
    mn = [r for r in INP.values() if r['arm'] == 'mainnet' and not math.isnan(sec(r['id']))]
    if mn:
        p('| mainnet block | Msteps | s | zkvm-bench 1.1 fit, 5.33 + 0.159 x Msteps |')
        p('|---|---:|---:|---:|')
        for r in mn:
            ms = int(r['steps']) / 1e6
            p(f"| {r['label']} | {ms:.1f} | {sec(r['id']):.2f} | {5.33 + 0.159 * ms:.2f} |")
        p('')

    p('| arm | n | fixed s | s per Msteps | Msteps/s | R2 |')
    p('|---|---:|---:|---:|---:|---:|')
    for arm in ('l2', 'l2-precompile', 'l2-keccak-sw', 'l2-poseidon', 'l2-poseidon-ksw',
                'l2-poseidon-sig', 'l2-poseidon-sig-ksw', 'l2-poseidon-all-ksw', 'control',
                'mainnet'):
        xs, ys = [], []
        for rid, s in med.items():
            r = INP.get(rid)
            if r and r['arm'] == arm and not math.isnan(s):
                xs.append(int(r['steps']) / 1e6)
                ys.append(s)
        a, b, r2 = ols(xs, ys)
        fits[(gset, arm)] = (a, b)
        if xs:
            p(f'| {arm} | {len(xs)} | {a:.2f} | {b:.4f} | {1/b if b else math.nan:.1f} | {r2:.4f} |')
    p('')

    ph = tdir / 'phases.csv'
    if ph.exists():
        spans = {}
        for r in csv.DictReader(open(ph)):
            spans.setdefault((r['id'], r['phase']), {}).setdefault(r['pass'], 0)
            spans[(r['id'], r['phase'])][r['pass']] = max(spans[(r['id'], r['phase'])][r['pass']],
                                                         fnum(r['ms']))
        # the sweep sizes this run proved (ONLY can leave some out): all of them up to six, else
        # four spread across the range
        shown = [f'l2-{q}' for q in pairs if f'l2-{q}' in INP and q.endswith('-b1')
                 and not math.isnan(sec(f'l2-{q}'))]
        shown.sort(key=lambda i: int(INP[i]['txs']))
        if len(shown) > 6:
            shown = [shown[i] for i in sorted({0, len(shown) // 3, 2 * len(shown) // 3,
                                               len(shown) - 1})]
        names = {}
        for (rid, name), v in spans.items():
            if rid in shown:
                names[name] = names.get(name, 0) + st.median(v.values())
        top = sorted(names, key=lambda n: -names[n])[:8]
        if top:
            p('Longest worker phases, median ms (each span\'s longest occurrence in a prove):\n')
            p('| phase | ' + ' | '.join(f"{INP[i]['txs']} tx" for i in shown) + ' |')
            p('|---|' + '---:|' * len(shown))
            for n in top:
                cells = [f"{st.median(spans[(i, n)].values()):.0f}" if (i, n) in spans else ''
                         for i in shown]
                p(f'| {n} | ' + ' | '.join(cells) + ' |')
            p('')

    gpu = tdir / 'gpu.csv'
    if gpu.exists():
        util = [fnum(l.split(',')[1]) for l in open(gpu) if l.count(',') >= 2]
        util = [u for u in util if not math.isnan(u)]
        if util:
            p(f'GPU utilisation while proving: mean {st.mean(util):.0f} %, median {st.median(util):.0f} %.\n')

# steps as a function of the transaction count, from the L2 sweep
xs = [int(r['txs']) for r in INP.values() if r['arm'] == 'l2' and r['set'] == 'sweep']
ys = [int(r['steps']) for r in INP.values() if r['arm'] == 'l2' and r['set'] == 'sweep']
c, d, r2s = ols(xs, ys)
if not math.isnan(d):
    p(f'## Sizing at {TPS:g} TPS\n')
    p(f'L2 steps per block = {c/1e6:.2f} M + {d/1e3:.2f} K x transactions (R2 {r2s:.4f}, the sweep).\n')
    for (gset, arm), (a, b) in sorted(fits.items()):
        if arm != 'l2' or math.isnan(a):
            continue
        p(f'Worker on {gset} GPU(s): proof = {a:.2f} s + {b:.4f} s x Msteps.\n')
        p('| block every | tx / block | proof s | provers to keep up | latency s |')
        p('|---:|---:|---:|---:|---:|')
        for dt in (0.25, 0.5, 1, 2, 5, 10, 30):
            txs = TPS * dt
            secs = a + b * (c + d * txs) / 1e6
            p(f'| {dt:g} s | {txs:g} | {secs:.2f} | {math.ceil(secs / dt)} | {dt / 2 + secs:.2f} |')
        p('')
    p('A transaction waits half a block interval on average for its block to close, then the '
      'proof. Provers: independent workers on successive blocks, each with the GPUs of the row\'s '
      'worker; the count keeps up with the rate, it does not shorten a proof.\n')

print('\n'.join(out))
