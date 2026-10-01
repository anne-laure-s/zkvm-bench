#!/usr/bin/env python3
"""summary.md from a run.sh results directory. Runs on the box at the end of run.sh, and anywhere
else on a fetched copy.

    summarize.py <results dir> [--tps 50]

Medians over passes, per input, of the proofs that succeeded. Then, per arm and worker config,
the line `secs = fixed + slope x Msteps` -- the shape zkvm-bench's mainnet runs gave
(5.33 + 0.159 x Msteps on one RTX 5090, ZisK 1.1, measured 18-289 Msteps). These blocks reach
down to a single transaction, so the fixed part is measured here instead of extrapolated, and it
is the figure a latency target lives or dies by.

The sizing table puts that line against a transaction rate: blocks every `interval` seconds hold
`tps x interval` transactions, a prover takes `secs` for one, and keeping up takes
ceil(secs / interval) provers -- each with this run's GPUs -- working on successive blocks. The
latency is a transaction's wait for its block to close (half an interval on average) plus the
proof. The proofs are STARK (VADCOP final) proofs: there is no SNARK wrap to add.
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
      f'{sum(r["ok"] == "True" for r in rs)}/{len(rs)} commit to their block.\n')

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

    # the sweep, every arm side by side, one row per block size (medians over its blocks): the
    # L2 as built by default (JUMPDEST in software), with the precompile if that arm was staged,
    # and the plaintext control; ratios are block for block, then medians
    pairs = sorted({r['pair'] for r in INP.values() if r['set'] == 'sweep'})
    sizes = sorted({INP[f'l2-{q}']['label'] for q in pairs if f'l2-{q}' in INP})
    pre = any(r['arm'] == 'l2-precompile' for r in INP.values())
    md = lambda v: st.median(v) if v else math.nan

    def ratio(qs, a, b):
        v = [sec(f'{a}-{q}') / sec(f'{b}-{q}') for q in qs]
        return md([x for x in v if not math.isnan(x)])

    p('| tx / block | Msteps (L2) | L2 s |' + (' L2, precompile s |' if pre else '') +
      ' control s |' + (' L2 / precompile |' if pre else '') + ' L2 / control |')
    p('|---:|---:|---:|' + ('---:|' if pre else '') + '---:|' + ('---:|' if pre else '') + '---:|')
    for lab in sizes:
        qs = [q for q in pairs if f'l2-{q}' in INP and INP[f'l2-{q}']['label'] == lab]
        ms = st.median(int(INP[f'l2-{q}']['steps']) for q in qs) / 1e6
        col = lambda arm: md([s for s in (sec(f'{arm}-{q}') for q in qs) if not math.isnan(s)])
        row = f"| {int(lab[1:])} | {ms:.2f} | {col('l2'):.2f} |"
        if pre:
            row += f" {col('l2-precompile'):.2f} |"
        row += f" {col('control'):.2f} |"
        if pre:
            row += f" {ratio(qs, 'l2', 'l2-precompile'):.3f} |"
        row += f" {ratio(qs, 'l2', 'control'):.3f} |"
        p(row)
    p('')
    pres = sorted({r['pair'] for r in INP.values() if r['set'] == 'preset'})
    if pres:
        p('| preset block | tx | Msteps (L2) | L2 s |' + (' L2, precompile s |' if pre else '') +
          ' control s |')
        p('|---|---:|---:|---:|' + ('---:|' if pre else '') + '---:|')
        for q in pres:
            l2, ct = f'l2-{q}', f'control-{q}'
            if l2 in INP:
                p(f"| {q[len('preset-'):]} | {INP[l2]['txs']} | {int(INP[l2]['steps'])/1e6:.2f} | "
                  f"{sec(l2):.2f} |" + (f" {sec('l2-precompile-' + q):.2f} |" if pre else '') +
                  f" {sec(ct):.2f} |")
        p('')
    mn = [r for r in INP.values() if r['arm'] == 'mainnet']
    if mn:
        p('| mainnet block | Msteps | s | zkvm-bench 1.1 fit, 5.33 + 0.159 x Msteps |')
        p('|---|---:|---:|---:|')
        for r in mn:
            ms = int(r['steps']) / 1e6
            p(f"| {r['label']} | {ms:.1f} | {sec(r['id']):.2f} | {5.33 + 0.159 * ms:.2f} |")
        p('')

    p('| arm | n | fixed s | s per Msteps | Msteps/s | R2 |')
    p('|---|---:|---:|---:|---:|---:|')
    for arm in ('l2', 'l2-precompile', 'control', 'mainnet'):
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
        shown = [f'l2-{q}' for q in pairs if f'l2-{q}' in INP and q.endswith('-b1')]
        shown.sort(key=lambda i: int(INP[i]['txs']))
        shown = [shown[i] for i in sorted({0, len(shown) // 3, 2 * len(shown) // 3, len(shown) - 1})
                 if 0 <= i < len(shown)]
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
