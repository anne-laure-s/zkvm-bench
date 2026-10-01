#!/usr/bin/env python3
"""meta-report — statistics over a whole lineage: how its gain is spread across commits.

Reads what the series scripts produce, plus the lineage's git history:
  <lineage>-index.tsv     commit order -> ELF
  <lineage>-measure.tsv   (ELF, block, steps, COST)
  --monad                 a checkout holding the lineage commits (dates, subjects, files touched)
Writes one self-contained HTML page (results/series-<lineage>-meta.html from run-r10.sh).

Each commit is compared with the one before it on the same blocks: the delta is the ratio of the
block totals, minus one. Deltas are summed as logs, so every share below adds up exactly to the
lineage's net change. A commit's delta is what it did when it landed, not what removing it from the
tip would cost.
"""
import argparse, collections, datetime, json, math, os, re, statistics as st, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEUTRAL = 5e-4   # |delta| at or under 0.05 % counts as neutral

# Where a commit works, by the directory it touches most. Ordered: the first prefix that matches wins.
AREAS = (
    ('category/vm/', 'VM (interpreter, runtime)'), ('zkvm/category/vm', 'VM (interpreter, runtime)'),
    ('category/execution/ethereum/db', 'Trie / db'), ('category/mpt', 'Trie / db'),
    ('category/execution/ethereum/state', 'State (journal, accounts)'),
    ('category/execution/ethereum/rlp', 'RLP / decoding'),
    ('category/execution/ethereum/precompiles', 'Crypto'),
    ('category/execution', 'Execution (block, tx, gas)'),
    ('zkvm/guest/keccak', 'Keccak'), ('category/core/keccak', 'Keccak'),
    ('category/core', 'Core (hashing, bytes)'), ('zkvm/core', 'Core (hashing, bytes)'),
    ('zkvm/category/crypto', 'Crypto'),
    ('zkvm/', 'zkvm (build, profile)'), ('third_party/', 'Dependencies'),
)


def area_of(path):
    for prefix, name in AREAS:
        if path.startswith(prefix):
            return name
    return 'Other'


def read_index(path):
    rows = []
    for line in open(path):
        f = line.rstrip('\n').split('\t')
        if len(f) >= 5:
            rows.append({'i': int(f[0]), 'sha': f[1], 'status': f[2], 'elf': f[3], 'subj': f[4]})
    return rows


def read_measure(path, blocks):
    per = collections.defaultdict(dict)
    for line in open(path):
        f = line.rstrip('\n').split('\t')
        if len(f) >= 4 and f[2].isdigit() and f[3].isdigit():
            b = int(f[1])
            if blocks is None or b in blocks:
                per[f[0]][b] = (int(f[2]), int(f[3]))
    return per


def read_blocks(path):
    out = set()
    for line in open(path):
        m = re.search(r'(\d{6,})', os.path.basename(line.strip()))
        if m:
            out.add(int(m.group(1)))
    return out


def git_meta(monad, shas):
    """Author date, subject and touched files of each commit, in one git call."""
    out = subprocess.run(['git', '-C', monad, 'log', '--no-walk=unsorted', '--numstat',
                          '--format=@@%h%x09%H%x09%aI%x09%s', *shas],
                         capture_output=True, text=True, check=True).stdout
    meta, cur = {}, None
    for line in out.splitlines():
        if line.startswith('@@'):
            short, full, date, subj = line[2:].split('\t', 3)
            cur = {'date': date, 'subj': subj, 'lines': 0, 'files': []}
            meta[short] = meta[full] = cur
        elif line and cur is not None:
            a, d, p = line.split('\t', 2)
            if a != '-':
                cur['lines'] += int(a) + int(d)
            cur['files'].append(p)
    return meta


def stats(rows, per, meta):
    blocks = sorted(set.intersection(*(set(per[r['elf']]) for r in rows)))
    if len(blocks) < 2:
        sys.exit(f'meta-report: only {len(blocks)} block(s) measured for every commit')
    total = lambda e, k: sum(per[e][b][k] for b in blocks)
    for r in rows:
        m = meta.get(r['sha'])
        if m is None:
            sys.exit(f'meta-report: commit {r["sha"]} is not in the checkout given by --monad')
        r.update(date=m['date'][:10], lines=m['lines'])
        files = [p for p in m['files'] if not p.endswith(('.md', '_test.cpp')) and '/test' not in p]
        c = collections.Counter(area_of(p) for p in files)
        r['area'] = c.most_common(1)[0][0] if c else 'Other'
    for p, r in zip(rows, rows[1:]):
        for k, name in ((0, 'steps'), (1, 'cost')):
            r[name] = total(r['elf'], k) / total(p['elf'], k) - 1
            ratios = [per[r['elf']][b][k] / per[p['elf']][b][k] for b in blocks]
            r[name + '_median'] = st.median(ratios) - 1
            r[name + '_better'] = sum(x < 1 for x in ratios) / len(ratios)
            r[name + '_worse'] = sum(x > 1 for x in ratios) / len(ratios)
        r['identical'] = all(per[r['elf']][b] == per[p['elf']][b] for b in blocks)
    d = rows[1:]
    lg = lambda r: math.log1p(r['cost'])
    TC = sum(lg(r) for r in d)
    TS = sum(math.log1p(r['steps']) for r in d)
    mean = lambda v: sum(v) / len(v) if v else 0.0
    gains = sorted((r for r in d if r['cost'] < 0), key=lambda r: r['cost'])
    gross = sum(lg(r) for r in gains)

    def k_for(frac):
        acc = 0.0
        for n, r in enumerate(gains, 1):
            acc += lg(r)
            if acc <= frac * gross:
                return n
        return len(gains)

    changed = [r for r in d if not r['identical']]
    x = [r['steps'] for r in changed]; y = [r['cost'] for r in changed]
    slope = r_ = 0.0
    if len(x) > 2 and st.pstdev(x) > 0 and st.pstdev(y) > 0:
        mx, my = mean(x), mean(y)
        slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sum((a - mx) ** 2 for a in x)
        r_ = slope * st.pstdev(x) / st.pstdev(y)
    win = [r for r in d if r['cost'] < -NEUTRAL]
    summary = {
        'n_commits': len(rows), 'n_deltas': len(d), 'first': rows[0]['sha'], 'last': rows[-1]['sha'],
        'blocks': len(blocks), 'block_min': blocks[0], 'block_max': blocks[-1],
        'cost_total': math.expm1(TC), 'steps_total': math.expm1(TS),
        'cost_mean': mean([r['cost'] for r in d]), 'cost_median': st.median(r['cost'] for r in d),
        'cost_mean_changed': mean([r['cost'] for r in changed]), 'n_changed': len(changed),
        'n_gain': len(win), 'n_loss': sum(r['cost'] > NEUTRAL for r in d), 'n_identical': len(d) - len(changed),
        'n_neutral': sum(abs(r['cost']) <= NEUTRAL for r in changed),
        'k50': k_for(.5), 'k80': k_for(.8), 'robust_all': sum(r['cost_better'] == 1 for r in win),
        'slope': slope, 'r': r_,
    }
    edges = [(-1, -.02, '< −2 %'), (-.02, -.01, '−2 to −1 %'), (-.01, -.005, '−1 to −0.5 %'),
             (-.005, -.002, '−0.5 to −0.2 %'), (-.002, -NEUTRAL, '−0.2 to −0.05 %'), (-NEUTRAL, NEUTRAL, '±0.05 %'),
             (NEUTRAL, .002, '+0.05 to +0.2 %'), (.002, 1, '> +0.2 %')]
    share = lambda rs: sum(lg(r) for r in rs) / TC if TC else 0.0
    buckets = [{'label': nm, 'n': len(sel), 'share': share(sel), 'kind': 'loss' if lo >= NEUTRAL else 'neutral' if lo < 0 < hi else 'gain'}
               for lo, hi, nm in edges for sel in [[r for r in d if lo <= r['cost'] < hi]]]

    def groups(key):
        by = collections.defaultdict(list)
        for r in d:
            by[key(r)].append(r)
        return by

    areas = []
    for name, rs in groups(lambda r: r['area']).items():
        best = min(rs, key=lambda r: r['cost'])
        areas.append({'name': name, 'n': len(rs), 'net': math.expm1(sum(lg(r) for r in rs)), 'share': share(rs),
                      'mean': mean([r['cost'] for r in rs]), 'median': st.median(r['cost'] for r in rs),
                      'wins': sum(r['cost'] < -NEUTRAL for r in rs), 'best': best['subj'], 'best_d': best['cost']})
    areas.sort(key=lambda a: a['net'])
    week_of = lambda r: (lambda t: (t - datetime.timedelta(days=t.weekday())).isoformat())(datetime.date.fromisoformat(r['date']))
    weeks = [{'week': w, 'n': len(rs), 'net': math.expm1(sum(lg(r) for r in rs)), 'mean': mean([r['cost'] for r in rs])}
             for w, rs in sorted(groups(week_of).items())]
    curve, cc, cs = [], 0.0, 0.0
    for r in rows:
        if 'cost' in r:
            cc += lg(r); cs += math.log1p(r['steps'])
        curve.append({'i': r['i'], 'sha': r['sha'], 'subj': r['subj'], 'date': r['date'], 'area': r['area'],
                      'c': math.exp(cc), 's': math.exp(cs), 'dc': r.get('cost'), 'ds': r.get('steps')})
    pick = lambda r: {'i': r['i'], 'sha': r['sha'], 'subj': r['subj'], 'area': r['area'], 'dc': r['cost'],
                      'dm': r['cost_median'], 'ds': r['steps'], 'better': r['cost_better'], 'worse': r['cost_worse']}
    return {'summary': summary, 'buckets': buckets, 'areas': areas, 'weeks': weeks, 'curve': curve,
            'top': [pick(r) for r in gains[:15]],
            'losses': [pick(r) for r in sorted(d, key=lambda r: -r['cost']) if r['cost'] > NEUTRAL]}


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* The series page's theme (report.py): same surfaces, ink and rules, and the same two series colours
   in the same roles -- steps in slot 1, prover COST in slot 2. Regressions keep the status red. */
:root {
  --bg: #fff; --surface: #fafafa; --ink: #111; --ink-2: #666; --muted: #666; --rule: #e3e3e3;
  --grid: #ececec; --steps: #2a78d6; --cost: #eb6834; --bar: #eb6834; --neutral: #999;
  --critical: #d03b3b; --tip: #fafafa; --tip-ink: #111; --tip-rule: #e3e3e3;
  --sans: "Public Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #111; --surface: #1a1a1a; --ink: #eee; --ink-2: #999; --muted: #999; --rule: #333;
    --grid: #2a2a2a; --steps: #3987e5; --cost: #d95926; --bar: #d95926; --neutral: #666;
    --critical: #e06060; --tip: #1a1a1a; --tip-ink: #eee; --tip-rule: #333;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #111; --surface: #1a1a1a; --ink: #eee; --ink-2: #999; --muted: #999; --rule: #333;
  --grid: #2a2a2a; --steps: #3987e5; --cost: #d95926; --bar: #d95926; --neutral: #666;
  --critical: #e06060; --tip: #1a1a1a; --tip-ink: #eee; --tip-rule: #333;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 var(--sans); }
.wrap { max-width: 1100px; margin: 0 auto; padding-inline: 20px; padding-block: 32px 56px; display: grid; gap: 40px; }
header { display: grid; gap: 6px; }
h1 { font-size: 30px; line-height: 1.15; margin: 0; font-weight: 700; letter-spacing: -0.01em; text-wrap: balance; }
.sub { color: var(--ink-2); margin: 0; font-size: 14px; }
code { font: 500 12.5px var(--mono); color: var(--ink-2); }
h2 { font-size: 18px; margin: 0; font-weight: 650; text-wrap: balance; }
section { display: grid; gap: 14px; }
.lede { color: var(--ink-2); margin: 0; font-size: 14px; max-width: 70ch; }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (min-width: 1060px) { .kpis { grid-template-columns: repeat(6, 1fr); } }
@media (max-width: 560px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
.kpi { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 14px 16px; display: grid; gap: 4px; align-content: start; }
.kpi .label { font-size: 11.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
.kpi .value { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.1; }
.kpi .note { font-size: 12.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.chart { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 12px 8px 4px; position: relative; }
.legend { display: flex; gap: 18px; flex-wrap: wrap; font-size: 13px; color: var(--ink-2); padding: 0 8px 4px; }
.legend span::before { content: ""; display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; background: var(--c); }
svg text { fill: var(--muted); font: 11.5px var(--sans); font-variant-numeric: tabular-nums; }
svg .end { font-weight: 600; font-size: 12.5px; fill: var(--ink); }
svg .rank { font-weight: 600; fill: var(--ink); }
.tip { position: absolute; pointer-events: none; background: var(--tip); color: var(--tip-ink); border: 1px solid var(--tip-rule); border-radius: 6px; padding: 8px 10px; font-size: 12.5px; line-height: 1.45; max-width: 340px; box-shadow: 0 4px 14px rgb(0 0 0 / 0.12); font-variant-numeric: tabular-nums; }
.tip b { font-weight: 600; }
.tip .k { color: var(--muted); }
.scroll { overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th { text-align: left; font-weight: 600; font-size: 11.5px; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); padding: 10px 12px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: 8px 12px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
tr:last-child td { border-bottom: 0; }
td.r, th.r { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.subj { min-width: 260px; }
.barcell { width: 32%; min-width: 140px; }
.bar { height: 10px; border-radius: 0 4px 4px 0; background: var(--bar); }
.bar.neutral { background: var(--neutral); }
.bar.loss { background: var(--critical); }
.two { display: grid; grid-template-columns: 1fr; gap: 40px; }
@media (min-width: 900px) { .two { grid-template-columns: 1fr 1fr; } }
footer { color: var(--muted); font-size: 12.5px; max-width: 90ch; }
footer p { margin: 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>__TITLE__</h1>
    <p class="sub" id="sub"></p>
  </header>
  <section aria-label="Summary"><div class="kpis" id="kpis"></div></section>
  <section>
    <h2>COST and steps along the lineage</h2>
    <p class="lede">Total over the blocks at each commit, as a share of commit 1. Hover the chart for each commit and what it did. Numbers mark the six largest gains.</p>
    <div class="chart" id="curve">
      <div class="legend"><span style="--c: var(--steps)">steps</span><span style="--c: var(--cost)">prover COST</span></div>
      <svg id="curve-svg" viewBox="0 0 960 360" role="img" aria-label="Cumulative COST and steps per commit" style="width:100%;height:auto;display:block"></svg>
      <div class="tip" id="tip" hidden></div>
    </div>
  </section>
  <div class="two">
    <section>
      <h2>Size of the deltas</h2>
      <p class="lede">Each commit's effect on COST against the previous one, and its share of the lineage's net gain.</p>
      <div class="scroll"><table id="buckets"></table></div>
    </section>
    <section>
      <h2>By week</h2>
      <p class="lede">By author date. The gain is the combined COST change of that week's commits.</p>
      <div class="scroll"><table id="weeks"></table></div>
    </section>
  </div>
  <section>
    <h2>By area</h2>
    <p class="lede">The area of the directory a commit touches most.</p>
    <div class="scroll"><table id="areas"></table></div>
  </section>
  <section>
    <h2>The 15 largest gains</h2>
    <div class="scroll"><table id="top"></table></div>
  </section>
  <section>
    <h2>Regressions</h2>
    <div class="scroll"><table id="losses"></table></div>
  </section>
  <footer><p>__FOOTER__</p></footer>
</div>
<script>
const D = __DATA__;
const S = D.summary;
const pct = (x, d = 2) => (x < 0 ? "−" : x > 0 ? "+" : "") + Math.abs(x * 100).toFixed(d) + " %";
const abs = (x, d = 1) => Math.abs(x * 100).toFixed(d) + " %";
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const el = id => document.getElementById(id);
el("sub").innerHTML = `${S.n_commits} commits, <code>${S.first}</code> to <code>${S.last}</code> · ZisK __RUNTIME__ · ${S.blocks} blocks ${S.block_min}–${S.block_max}`;
el("kpis").innerHTML = [
  ["Total COST", pct(S.cost_total, 1), "since commit 1"],
  ["Total steps", pct(S.steps_total, 1), `COST ≈ ${S.slope.toFixed(2)} × steps per commit`],
  ["Mean per commit", pct(S.cost_mean), `median ${pct(S.cost_median)} · ${pct(S.cost_mean_changed)} over the ${S.n_changed} that change the program`],
  ["Gains > 0.05 %", String(S.n_gain), `${S.n_neutral} neutral · ${S.n_loss} losses · ${S.n_identical} no effect`],
  ["Concentration", String(S.k50), `commits make 50 % of the gross gain · ${S.k80} make 80 %`],
  ["Robustness", `${S.robust_all}/${S.n_gain}`, `gains that make every block cheaper`],
].map(([l, v, n]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${n}</div></div>`).join("");

(function curve() {
  const svg = el("curve-svg"), W = 960, H = 360, m = { l: 44, r: 96, t: 14, b: 34 };
  const C = D.curve, n = C.length, i0 = C[0].i, i1 = C[n - 1].i;
  const x = i => m.l + (i - i0) / Math.max(1, i1 - i0) * (W - m.l - m.r);
  const y = v => m.t + (1 - v) * (H - m.t - m.b);
  let g = "";
  for (let v = 0; v <= 1.0001; v += 0.2) {
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>`;
    g += `<text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${Math.round(v * 100)} %</text>`;
  }
  const step = Math.max(1, Math.round((i1 - i0) / 5 / 10) * 10);
  for (let i = i0; i < i1 - step / 2; i += step) g += `<text x="${x(i)}" y="${H - m.b + 18}" text-anchor="middle">#${i}</text>`;
  g += `<text x="${x(i1)}" y="${H - m.b + 18}" text-anchor="middle">#${i1}</text>`;
  g += `<text x="${(m.l + W - m.r) / 2}" y="${H - 4}" text-anchor="middle">lineage commit</text>`;
  const path = k => C.map((p, j) => `${j ? "L" : "M"}${x(p.i).toFixed(1)},${y(p[k]).toFixed(1)}`).join("");
  g += `<path d="${path("s")}" fill="none" stroke="var(--steps)" stroke-width="2" stroke-linejoin="round"/>`;
  g += `<path d="${path("c")}" fill="none" stroke="var(--cost)" stroke-width="2" stroke-linejoin="round"/>`;
  const last = C[n - 1];
  g += `<text class="end" x="${x(last.i) + 8}" y="${y(last.c) + 4}">COST ${abs(last.c)}</text>`;
  g += `<text class="end" x="${x(last.i) + 8}" y="${y(last.s) + 4}">steps ${abs(last.s)}</text>`;
  D.top.slice(0, 6).forEach((t, r) => {
    const p = C.find(q => q.i === t.i);
    g += `<circle cx="${x(p.i)}" cy="${y(p.c)}" r="4.5" fill="var(--cost)" stroke="var(--surface)" stroke-width="2"/>`;
    g += `<text class="rank" x="${x(p.i)}" y="${y(p.c) - 10}" text-anchor="middle">${r + 1}</text>`;
  });
  g += `<line id="cx" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>`;
  g += `<circle id="cc" r="4.5" fill="var(--cost)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  g += `<circle id="cs" r="4.5" fill="var(--steps)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  g += `<rect id="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/>`;
  svg.innerHTML = g;
  const tip = el("tip"), box = el("curve"), cx = el("cx"), cc = el("cc"), cs = el("cs");
  el("hit").addEventListener("pointermove", ev => {
    const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
    const j = Math.max(0, Math.min(n - 1, Math.round((loc.x - m.l) / (W - m.l - m.r) * (n - 1))));
    const p = C[j];
    for (const [e, v] of [[cc, p.c], [cs, p.s]]) { e.setAttribute("cx", x(p.i)); e.setAttribute("cy", y(v)); e.setAttribute("visibility", "visible"); }
    cx.setAttribute("x1", x(p.i)); cx.setAttribute("x2", x(p.i)); cx.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>#${p.i}</b> <code>${p.sha}</code> · ${p.date}<br>${esc(p.subj)}<br>` +
      (p.dc == null ? `<span class="k">reference</span>` :
        `<span class="k">this commit</span> COST <b>${pct(p.dc)}</b> · steps ${pct(p.ds)}<br><span class="k">area</span> ${esc(p.area)}`) +
      `<br><span class="k">cumulative</span> COST ${abs(p.c)} · steps ${abs(p.s)} of commit 1`;
    tip.hidden = false;
    const r = box.getBoundingClientRect(), tw = tip.offsetWidth;
    let left = ev.clientX - r.left + 14; if (left + tw > r.width - 8) left = ev.clientX - r.left - tw - 14;
    tip.style.left = Math.max(8, left) + "px"; tip.style.top = Math.max(8, ev.clientY - r.top - 20) + "px";
  });
  el("hit").addEventListener("pointerleave", () => { tip.hidden = true; for (const e of [cx, cc, cs]) e.setAttribute("visibility", "hidden"); });
})();

const share = v => (v * 100).toFixed(1) + " %";
const barRow = (w, cls) => `<td class="barcell"><div class="bar ${cls || ""}" style="width:${Math.max(0.5, w * 100)}%"></div></td>`;
const mB = Math.max(...D.buckets.map(b => Math.abs(b.share)), 1e-9);
el("buckets").innerHTML = `<thead><tr><th>COST delta</th><th class="r">commits</th><th class="barcell">share of net gain</th><th class="r">share</th></tr></thead><tbody>` +
  D.buckets.map(b => `<tr><td>${b.label}</td><td class="r">${b.n}</td>${barRow(Math.abs(b.share) / mB, b.kind === "gain" ? "" : b.kind)}<td class="r">${share(b.share)}</td></tr>`).join("") + `</tbody>`;
const mW = Math.max(...D.weeks.map(w => Math.abs(w.net)), 1e-9);
el("weeks").innerHTML = `<thead><tr><th>week of</th><th class="r">commits</th><th class="barcell">COST gain</th><th class="r">net</th><th class="r">mean / commit</th></tr></thead><tbody>` +
  D.weeks.map(w => `<tr><td>${w.week}</td><td class="r">${w.n}</td>${barRow(Math.abs(w.net) / mW, w.net > 0 ? "loss" : "")}<td class="r">${pct(w.net)}</td><td class="r">${pct(w.mean)}</td></tr>`).join("") + `</tbody>`;
const mA = Math.max(...D.areas.map(a => Math.abs(a.net)), 1e-9);
el("areas").innerHTML = `<thead><tr><th>area</th><th class="r">commits</th><th class="barcell">COST gain</th><th class="r">net</th><th class="r">share</th><th class="r">mean / commit</th><th class="r">median / commit</th><th class="r">gains &gt; 0.05 %</th><th>best commit</th></tr></thead><tbody>` +
  D.areas.map(a => `<tr><td>${esc(a.name)}</td><td class="r">${a.n}</td>${barRow(Math.abs(a.net) / mA, a.net > 0 ? "loss" : "")}<td class="r">${pct(a.net)}</td><td class="r">${share(a.share)}</td><td class="r">${pct(a.mean, 3)}</td><td class="r">${pct(a.median, 3)}</td><td class="r">${a.wins}</td><td class="subj">${pct(a.best_d)} · ${esc(a.best)}</td></tr>`).join("") + `</tbody>`;
const rowsOf = (rs, ranked) => rs.map((t, k) => `<tr>${ranked ? `<td class="r">${k + 1}</td>` : ""}<td class="r">${pct(t.dc)}</td><td class="r">${pct(t.dm)}</td><td class="r">${pct(t.ds)}</td><td class="r">${Math.round((ranked ? t.better : t.worse) * 100)} %</td><td class="r">#${t.i}</td><td><code>${t.sha}</code></td><td class="subj">${esc(t.subj)}</td><td>${esc(t.area)}</td></tr>`).join("");
el("top").innerHTML = `<thead><tr><th class="r">rank</th><th class="r">COST</th><th class="r">block median</th><th class="r">steps</th><th class="r">blocks cheaper</th><th class="r">#</th><th>commit</th><th>subject</th><th>area</th></tr></thead><tbody>${rowsOf(D.top, true)}</tbody>`;
el("losses").innerHTML = D.losses.length
  ? `<thead><tr><th class="r">COST</th><th class="r">block median</th><th class="r">steps</th><th class="r">blocks dearer</th><th class="r">#</th><th>commit</th><th>subject</th><th>area</th></tr></thead><tbody>${rowsOf(D.losses, false)}</tbody>`
  : `<tbody><tr><td>No commit costs more than 0.05 %.</td></tr></tbody>`;
</script>
</body>
</html>
'''

FOOTER = ('ziskemu __RUNTIME__ measurements, every commit built with the official profile. A delta compares '
          'the total over the blocks with the previous commit; the block median is the median of the per-block '
          'ratios. Shares of the net gain are computed in logs, so they add up exactly along the lineage. The '
          'mean per commit is the arithmetic mean of the deltas. A commit shows the effect it had when it '
          'landed; removed from the tip it can be worth something else.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--index', required=True, help='the lineage index, relative to profiling/series/')
    ap.add_argument('--measure', required=True, help='the lineage measurement table, relative to profiling/series/')
    ap.add_argument('--monad', required=True, help='a checkout holding the lineage commits')
    ap.add_argument('--lineage', required=True, help='the name the page carries, e.g. r10-zisk')
    ap.add_argument('--blocks-file', help='the measured sample (one witness path per line); default: every block')
    ap.add_argument('--runtime', help='ZisK release of the tables; default: the index .runtime stamp')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    idx_path = os.path.join(HERE, a.index)
    rows = read_index(idx_path)
    bad = [r for r in rows if r['status'] != 'OK']
    if len(rows) < 2 or bad:
        sys.exit(f'meta-report: {len(rows)} rows, {len(bad)} not OK -- a lineage statistic needs a complete walk')
    runtime = a.runtime
    if runtime is None and os.path.exists(idx_path + '.runtime'):
        runtime = open(idx_path + '.runtime').read().strip()
    blocks = read_blocks(a.blocks_file) if a.blocks_file else None
    per = read_measure(os.path.join(HERE, a.measure), blocks)
    missing = [r['sha'] for r in rows if r['elf'] not in per]
    if missing:
        sys.exit(f'meta-report: {len(missing)} commit(s) have no measurement (first: {missing[0]})')
    data = stats(rows, per, git_meta(a.monad, [r['sha'] for r in rows]))
    title = f'{a.lineage} optimizations'
    page = (PAGE.replace('__TITLE__', title).replace('__FOOTER__', FOOTER)
            .replace('__RUNTIME__', runtime or 'unknown release')
            .replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/')))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    tmp = a.out + '.tmp'
    with open(tmp, 'w') as fh:
        fh.write(page)
    os.replace(tmp, a.out)
    s = data['summary']
    print(f"wrote {a.out}: {s['n_commits']} commits, COST {s['cost_total']*100:+.2f} %, "
          f"mean per commit {s['cost_mean']*100:+.3f} %")


if __name__ == '__main__':
    main()
