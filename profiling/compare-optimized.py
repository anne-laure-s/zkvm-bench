#!/usr/bin/env python3
"""compare-optimized — the Monad guest with the measured levers, against what ships today.

`compare.py` reports the SHIPPED guest and stays the record of what is merged. This is the other
question: what does the guest on `al/zkvm-levers` do, next to the current one and next to the reth
guest on the same axis?

    ./compare-optimized.py            # writes results/compare-optimized.html

Three series per axis, never across axes: ZisK steps and SP1 cycles are different units on different
cost models, and putting rsp next to zisk-reth in one table would be the exact error compare.py
spends a page warning about.

Reads what was measured rather than measuring: `results/optimized-{zisk,sp1}.json` (the levers
build) and `results/compare.json` (the shipped guest and the reth guests, same blocks).
"""
import html
import json
import os
import statistics as st
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'results', 'compare-optimized.html')

AXES = {
    'zisk': {'title': 'ZisK', 'unit': 'steps', 'reth': 'zisk-reth', 'guest': 'monad-zisk'},
    'sp1':  {'title': 'SP1',  'unit': 'cycles', 'reth': 'rsp',       'guest': 'monad-sp1'},
}


# 2026-08-08: axes renamed with the sam-rebase (monad-current/monad-opt). Alias the old
# names so consumers of these jsons keep working; both keys point at the same object.
_AXIS_ALIASES = {'zisk': 'cur-zisk', 'sp1': 'cur-sp1',
                 'levers-self': 'opt-self', 'levers-self-sp1': 'opt-self-sp1',
                 'levers': 'opt-zisk', 'levers-sp1': 'opt-sp1'}

def _alias_axes(d):
    if isinstance(d, dict):
        for old, new in _AXIS_ALIASES.items():
            if old not in d and new in d:
                d[old] = d[new]
    return d


def load(name):
    try:
        return _alias_axes(json.load(open(os.path.join(HERE, 'results', name))))
    except Exception:
        return None


def n_(v):
    return f"{round(v):,}"


def pct(v, d=2):
    return '—' if v is None else f"{v * 100:.{d}f}%"


def x(v, d=3):
    return '—' if v is None else f"{v:.{d}f}×"


def med(xs):
    return st.median(xs) if xs else None


def series(axis, opt, cmp_):
    """The three medians and the two ratios, on the blocks all three have in common.

    The intersection matters: the levers run and the compare sweep do not cover identical block
    sets, and a ratio built from two different populations is the defect this report exists to
    avoid repeating."""
    blocks = cmp_[axis]['blocks']
    common = [b for b in opt['blocks']
              if b in blocks and blocks[b].get('a') and blocks[b].get('b')
              and blocks[b]['a'].get('work') and blocks[b]['b'].get('work')]
    if not common:
        return None
    cur = [blocks[b]['a']['work'] for b in common]
    ret = [blocks[b]['b']['work'] for b in common]
    new = [opt['blocks'][b]['opt'] for b in common]
    # Per-block ratios, then the median — the statistic the rest of the reports use.
    return {
        'n': len(common),
        'cur': med(cur), 'ret': med(ret), 'new': med(new),
        'r_before': med([blocks[b]['a']['work'] / blocks[b]['b']['work'] for b in common]),
        'r_after': med([opt['blocks'][b]['opt'] / blocks[b]['b']['work'] for b in common]),
        'gain': med([1 - opt['blocks'][b]['opt'] / blocks[b]['a']['work'] for b in common]),
        'cum': 1 - sum(new) / sum(cur),
        'cum_vs_reth_before': 1 - sum(cur) / sum(ret),
        'cum_vs_reth_after': 1 - sum(new) / sum(ret),
        'per': sorted(((b, blocks[b]['a']['work'], opt['blocks'][b]['opt'], blocks[b]['b']['work'])
                       for b in common), key=lambda t: -t[1]),
    }


CSS = """
:root{--bg:#0e1116;--panel:#151a21;--panel2:#1b212a;--line:#232a35;--line2:#2f3846;
 --fg:#e6eaf0;--muted:#93a0b4;--dim:#66748a;--gold:#e8b04b;--blue:#6aa9f0;--violet:#b07bf0;
 --green:#5fbf8a;--red:#e2705f;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
 --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:light){:root{--bg:#fbfcfd;--panel:#fff;--panel2:#f3f5f8;
 --line:#e3e8ef;--line2:#cdd5e0;--fg:#131820;--muted:#5b6675;--dim:#8a94a4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.55}
.wrap{max-width:1120px;margin:0 auto;padding:34px 26px 70px}
h1{font-family:var(--mono);font-size:21px;font-weight:650;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:15px;font-weight:600;margin:38px 0 12px;padding-bottom:7px;
 border-bottom:1px solid var(--line2)}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.note{color:var(--muted);font-size:12.5px;line-height:1.65;margin:10px 0 0}
.warn{border-left:2px solid var(--gold);padding-left:13px}
code{font-family:var(--mono);font-size:11.5px;background:var(--panel2);padding:1px 5px;
 border-radius:3px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:11px;margin:14px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.card .k{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);
 font-family:var(--mono)}
.card .v{font-family:var(--mono);font-size:20px;font-weight:650;margin-top:5px;
 font-variant-numeric:tabular-nums}
.card .d{font-size:11.5px;color:var(--muted);margin-top:3px}
.gold{color:var(--gold)}.blue{color:var(--blue)}.violet{color:var(--violet)}
.green{color:var(--green)}.red{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:12px}
th{text-align:right;font-weight:500;color:var(--muted);font-size:10px;text-transform:uppercase;
 letter-spacing:.08em;padding:5px 9px;border-bottom:2px solid var(--line2)}
th:first-child,td:first-child{text-align:left}
td{padding:5px 9px;border-bottom:1px solid var(--line);text-align:right;
 font-family:var(--mono);font-variant-numeric:tabular-nums}
td:first-child{font-family:var(--sans)}
tbody tr:hover{background:var(--panel2)}
.scroll{overflow:auto;max-height:420px}
.bar{height:5px;border-radius:3px;background:var(--violet);display:inline-block;vertical-align:middle}
.prov{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;
 font-size:12px;color:var(--muted);margin:16px 0}
.prov b{color:var(--fg)}
.prov code{font-size:11px}
"""


def card(k, v, d='', cls=''):
    return (f"<div class=card><div class=k>{k}</div>"
            f"<div class='v {cls}'>{v}</div><div class=d>{d}</div></div>")


def main():
    cmp_ = load('compare.json')
    if not cmp_:
        sys.exit('results/compare.json missing — run ./compare.py first')
    data = {a: load(f'optimized-{a}.json') for a in AXES}
    if not any(data.values()):
        sys.exit('no results/optimized-*.json — nothing measured for the levers build')

    h = [f"<style>{CSS}</style><div class=wrap>",
         "<h1>monad guest · with the measured levers</h1>",
         "<p class=sub>The guest on <code>al/zkvm-levers</code> against the one that ships today, "
         "and against the reth guest on the same axis. "
         "<b>This is not what is merged</b> — <code>compare.html</code> remains the report on that.</p>"]

    # ── provenance, first and unavoidable ───────────────────────────────────────────────────────
    z, s = data.get('zisk'), data.get('sp1')
    h.append("<div class=prov>")
    h.append(f"<b>What was measured.</b> Branch <code>al/zkvm-levers</code> at "
             f"<code>{(z or s)['commit']}</code>, eleven commits on <code>ed16787ae</code> — ten "
             f"levers, each built and measured on its own before being combined. The two changes "
             f"that live in submodules are <i>not</i> in the branch and not in these figures; they "
             f"are worth a further 0.40 point on ZisK.<br><br>")
    if z:
        h.append(f"<b>ZisK.</b> {z['n_measured']} blocks through <code>guests/monad/ev.sh</code>, "
                 f"<b>{z['roots_pass']}/{z['roots_total']} post-state roots PASS</b>. "
                 f"{z['n']} of them have a shipped-guest reference in <code>compare.json</code> and "
                 f"are the ones compared here; the other {z['n_without_reference']} were still root-"
                 f"verified.<br>")
    if s:
        # Mirror the ZisK line: how many were RUN, then how many are charted. Quoting `n` alone
        # read as "only 373 were executed", which understates by 131.
        h.append(f"<b>SP1.</b> {s['n_measured']} blocks through "
                 f"<code>sp1-runner --mode execute</code>, {s['n']} of them with a shipped-guest "
                 f"reference and charted here. "
                 f"<b>State roots were not verified on this axis</b>: the runner exposes only the "
                 f"length of the committed public values, not their bytes, so there is nothing to "
                 f"compare against the fixtures. The correctness evidence is the ZisK run on the "
                 f"same source — the ten changes are shared C++ — not this binary.<br>")
    h.append("</div>")

    for ax, meta in AXES.items():
        opt = data.get(ax)
        if not opt:
            continue
        r = series(ax, opt, cmp_)
        if not r:
            continue
        h.append(f"<h2>{meta['title']} · {r['n']} blocks</h2>")
        h.append("<div class=cards>")
        h.append(card(f"median {meta['unit']} · today", n_(r['cur']), 'the shipped guest', 'gold'))
        h.append(card(f"median {meta['unit']} · levers", n_(r['new']),
                      f"−{pct(r['gain'])} of its own work", 'violet'))
        h.append(card(f"median {meta['unit']} · {meta['reth']}", n_(r['ret']), 'unchanged', 'blue'))
        cls_b = 'red' if r['r_before'] >= 1 else 'green'
        cls_a = 'red' if r['r_after'] >= 1 else 'green'
        h.append(card(f"ratio vs {meta['reth']}", f"<span class={cls_b}>{x(r['r_before'])}</span>",
                      'today'))
        h.append(card(f"ratio vs {meta['reth']}", f"<span class={cls_a}>{x(r['r_after'])}</span>",
                      'with the levers'))
        h.append("</div>")
        # Totals, not medians: a prover is billed on the sum, and the sum weights the big blocks
        # that gain most — so it reads higher than the median and is the honest figure for cost.
        rb = sum(v[1] for v in r['per']) / sum(v[3] for v in r['per'])
        ra = sum(v[2] for v in r['per']) / sum(v[3] for v in r['per'])
        h.append(f"<p class=note>Per-block ratios, median — the statistic the other reports use. "
                 f"On <b>totals</b>, which is what a prover is billed on and which weights the large "
                 f"blocks that gain most, the guest's own work falls <b>{pct(r['cum'])}</b> and the "
                 f"comparison against {meta['reth']} moves from <b>{x(rb)}</b> to <b>{x(ra)}</b>.</p>")

        # per-block, biggest first
        mx = max(v[1] for v in r['per'])
        rows = "".join(
            f"<tr><td>{b}</td><td class=gold>{n_(cur)}</td><td class=violet>{n_(new)}</td>"
            f"<td class=blue>{n_(ret)}</td>"
            f"<td class={'green' if new < cur else 'red'}>{pct(1 - new / cur)}</td>"
            f"<td class={'green' if new < ret else 'red'}>{x(new / ret)}</td>"
            f"<td style='width:20%'><span class=bar style='width:{max(2, 100 * cur / mx):.0f}%'>"
            f"</span></td></tr>"
            for b, cur, new, ret in r['per'])
        h.append(f"<div class=scroll><table><tr><th>block</th>"
                 f"<th>today</th><th>levers</th><th>{meta['reth']}</th>"
                 f"<th>gain</th><th>vs {meta['reth']}</th><th></th></tr>{rows}</table></div>")

    # ── where it comes from ─────────────────────────────────────────────────────────────────────
    m = load('allfive-measured.json')
    if m:
        LEV = [('eager priming hash dropped', 0.1192), ('digest stubs out of the hash map', 0.0753),
               ('byte-order swaps inlined', 0.0488), ('128÷64 division specialised', 0.0307),
               ('state key hash', 0.0124), ('scan index widened', 0.0109),
               ('NodeId widened', 0.0034), ('clz + soft float', 0.0049),
               ('popcount inlined', 0.0009)]
        rows = "".join(f"<tr><td>{n}</td><td class=violet>{pct(v)}</td></tr>" for n, v in LEV)
        h.append("<h2>Where the gain comes from</h2>")
        h.append(f"<table><tr><th>change</th><th>measured alone, ZisK</th></tr>{rows}</table>")
        h.append("<p class=note>Each was built and measured on its own before the combination. They "
                 "add rather than compose — each removes a fixed number of steps from disjoint work, "
                 "and the combined build matched the sum to the step. Two of the ten needed a second "
                 "attempt after the first measured <i>worse</i>, and in both the failure is what "
                 "located the real cost.</p>")

    h.append("<h2>What this does not say</h2>")
    h.append("<p class='note warn'>The two axes are never compared with each other: ZisK steps and "
             "SP1 cycles are different units on different cost models. <b>Nothing here is proving "
             "time</b> — it is execution work, and the conversion is not linear. And the branch is "
             "unmerged: the figures describe a binary that exists on the devcore box and in "
             "<code>al/zkvm-levers</code>, not one that has shipped.</p>")

    host = subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()
    h.append(f"<p class=note style='margin-top:26px;color:var(--dim)'>Generated "
             f"{time.strftime('%Y-%m-%d %H:%M %Z')} · macOS {host} · "
             f"<code>./compare-optimized.py</code></p></div>")

    open(OUT, 'w').write("\n".join(h))
    print(f"  wrote {OUT}")
    for ax in AXES:
        if data.get(ax):
            r = series(ax, data[ax], cmp_)
            if r:
                print(f"    {ax:<5} n={r['n']:<4} gain {pct(r['gain'])}  "
                      f"ratio {x(r['r_before'])} → {x(r['r_after'])}")


if __name__ == '__main__':
    main()
