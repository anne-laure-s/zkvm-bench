#!/usr/bin/env python3
"""report — what each commit of al/zkvm-r4 costs, measured one commit at a time.

Reads what the scripts beside it produce:
  index.tsv        commit order -> ELF sha (a repeated sha means the commit did not change the guest)
  measure.tsv      (sha, block, steps, cost) over a sample of the corpus
  ../series-sp1/   the same two files for the SP1 guest, on a narrower sample

Writes profiling/results/series-r4.html.
"""
import argparse, html, math, os, statistics, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SP1 = os.path.join(os.path.dirname(HERE), 'series-sp1')

# Categorical slots 1 and 2 of the reference palette, in their fixed order. Not a
# custom pair: that order is the configuration its own validation certifies on
# the adjacent pairlist, which is what a two-line chart uses.
C1L, C2L = '#2a78d6', '#eb6834'
C1D, C2D = '#3987e5', '#d95926'


def read_index(d, name='index.tsv'):
    out = []
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return out
    for line in open(p):
        q = line.rstrip('\n').split('\t')
        if len(q) >= 5:
            out.append({'i': int(q[0]), 'commit': q[1], 'status': q[2],
                        'sha': q[3], 'subject': q[4]})
    return out


def read_measure(d, name='measure.tsv', blocks=None):
    per = defaultdict(dict)
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return per
    for line in open(p):
        q = line.rstrip('\n').split('\t')
        # COST may be absent: a `--quick` pass records steps and leaves `NA` for the instrumented
        # metric, which costs about three times as much. Dropping those rows made a quick table read
        # as an EMPTY one -- the measurement was there, the reader refused it. Steps are kept, cost
        # is None, and every consumer below decides for itself whether it has enough.
        if (len(q) == 4 and q[2] not in ('NA', '')
                and (blocks is None or int(q[1]) in blocks)):
            cost = int(q[3]) if q[3] not in ('NA', '') else None
            per[q[0]][int(q[1])] = (int(q[2]), cost)
    return per


def medians(per, base_sha):
    """Median per-block ratio of every ELF against the base, on shared blocks."""
    out, base = {}, per.get(base_sha, {})
    for sha, rows in per.items():
        common = sorted(set(rows) & set(base))
        if not common:
            continue
        # Cost only over the blocks where BOTH sides carry it. A half-quick table then yields a
        # complete steps curve and a cost curve over what was actually measured, instead of one
        # ratio silently taken on a different set from its neighbour.
        cb = [b for b in common if rows[b][1] is not None and base[b][1] is not None]
        out[sha] = {'n': len(common),
                    'steps': statistics.median(rows[b][0] / base[b][0] for b in common),
                    'cost': statistics.median(rows[b][1] / base[b][1] for b in cb) if cb else None,
                    'n_cost': len(cb)}
    return out


def pairwise(per, a_sha, b_sha, field, biggest=()):
    """This commit against the one before it, block by block.

    Returns {med, pooled, big, n, n_big, lo, hi, better} -- three views of one set of ratios,
    because a lever can separate them and the separation is the information.

    PAIRED, because a ratio of two medians-to-base is not. Each side's median can sit on a
    different block, so the ratio of the two moves by the gap between neighbours rather than by
    what the commit did -- and the two disagree exactly when a commit's effect is concentrated,
    which is the case worth seeing. r10's `state: index a FlatStorage row` is +0.027 % paired and
    -0.268 % unpaired over one set of 100 blocks, opposite signs.

    `pooled` is total over total on the same blocks: what a prover pays, where `med` is what a
    typical block does. `big` is pooled over the LARGEST TENTH -- what a proving budget spends on
    the blocks that dominate it.

    `big` is POOLED and not a median over that tenth, which is the mistake this column was built
    to avoid and reproduced one level down when it was. r10-zisk `state: index a FlatStorage row`
    over its top 20 blocks: median +0.017 %, pooled -0.809 %. The win inside the tenth sits on a
    few of its blocks, so a median across them sees nothing, exactly as the corpus median sees
    nothing across the corpus.

    The tenth is a DECILE and not a quartile because a quartile dilutes it: r10's three
    `index ... once it is big enough` commits measure -0.026 % over the top 25 % and -1.314 % over
    the top 10 %. On an effect that is uniform all three columns agree to the third decimal, which
    is how to tell one shape from the other.
    """
    a, b = per.get(a_sha, {}), per.get(b_sha, {})
    common = [x for x in set(a) & set(b)
              if a[x][field] is not None and b[x][field] is not None and a[x][field]]
    if not common:
        return None
    r = {x: b[x][field] / a[x][field] for x in common}
    vals = sorted(r.values())
    # `biggest` is ordered ONCE for the page, by the base build's step count, so every row's "big
    # blocks" are the same blocks. Ordering per row by each commit's own steps would move the set
    # under the reader and make two rows incomparable.
    big = [x for x in biggest if x in r] or common
    n_big = max(1, len(big) // 10)
    top = big[-n_big:]
    return {'med': statistics.median(vals),
            'pooled': sum(b[x][field] for x in common) / sum(a[x][field] for x in common),
            'big': sum(b[x][field] for x in top) / sum(a[x][field] for x in top),
            'n': len(common), 'n_big': n_big,
            'lo': vals[len(vals) // 10], 'hi': vals[-1 - len(vals) // 10],
            'better': sum(1 for v in vals if v < 1)}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def svg_chart(points, w=1360, h=380, branch='al/zkvm-r4'):
    """points: (i, subject, steps, cost, moved, commit, dwork, dcost, backends).

    Two choices here are about reading the TAIL of a long series, which is where the work is:

    LOG Y. The series is a ratio, so equal percentages should occupy equal height. On a linear axis
    they do not: over a 1.000x..0.343x run a 1 % commit is 3.4 px at the base and 1.2 px at the tip
    — the chart gives ancient history three times the resolution of the present. In log it is 2.3 px
    wherever it falls, and two commits are comparable by eye regardless of position.

    BAND HIT TARGETS. One invisible full-height rect per commit, half a slot either side, instead of
    a circle per point. At 95 commits the slot is ~13 px while the old r=11 circles spanned 22, so
    they overlapped about three deep and the browser hit-tested the last one painted: the tooltip
    named the RIGHTMOST commit of the pile, not the one under the cursor. Bands cannot overlap, and
    one band per commit also drops the work/cost ambiguity — the tooltip showed both anyway.
    """
    if not points:
        return '<p class=note>no data yet</p>'
    pad_l, pad_r, pad_t, pad_b = 58, 206, 18, 66
    # The viewBox is wider than the 920 these type sizes were chosen for, and CSS font-size inside an
    # SVG is in user units — the same class would render the labels a third smaller. Scale them.
    S = w / 920.0
    fs = lambda base: 'style="font-size:%.1fpx"' % (base * S)
    xs = [p[0] for p in points]
    # A series is drawn only when EVERY commit has it. A `--quick` pass measures steps and leaves
    # COST for later, and a line broken across the commits that have it would read as a shape
    # rather than as missing data. Steps alone is the honest picture of a quick table.
    #
    # Settled HERE, above the first read: the y-range below is taken over the series that will be
    # drawn, so it has to know which those are, and a comprehension is a scope of its own -- naming
    # SER from one before the assignment binds it raises rather than falling back to the module.
    SER = tuple(x for x in ((3, 'c2', 'prover cost'), (2, 'c1', 'steps'))
                if all(p[x[0]] is not None for p in points))
    vals = [p[x[0]] for p in points for x in SER if p[x[0]] and p[x[0]] > 0]
    if not vals:
        return '<p class=note>no positive ratio to plot</p>'
    lo, hi = min(vals) / 1.04, max(vals) * 1.04
    ratio = max(hi / lo, 1.02)
    X = lambda i: pad_l + (i - min(xs)) / max(1, (max(xs) - min(xs))) * (w - pad_l - pad_r)
    Y = lambda v: pad_t + math.log(hi / max(v, 1e-9)) / math.log(ratio) * (h - pad_t - pad_b)
    slot = (w - pad_l - pad_r) / max(1, len(points) - 1)

    g = []
    # Ticks stay round ratios; on a log axis they are simply not evenly spaced, which is the point.
    # Take the coarsest step that still leaves at least five lines.
    step = 0.50
    for cand in (0.01, 0.02, 0.05, 0.10, 0.20, 0.50):
        if (hi - lo) / cand <= 11:
            step = cand
            break
    t = round(hi / step) * step
    while t >= lo:
        if lo <= t <= hi:
            g.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                     % (pad_l, Y(t), w - pad_r, Y(t)))
            g.append('<text x="%d" y="%.1f" class="tick" %s text-anchor="end">%.2f×</text>'
                     % (pad_l - 8, Y(t) + 4 * S, fs(11), t))
        t -= step
    g.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="axis"/>'
             % (pad_l, h - pad_b, w - pad_r, h - pad_b))

    # Layers, not per-series blocks: drawing each series complete in turn puts the
    # second line over the first line's markers and takes a bite out of them.
    for series, colvar, _ in SER:
        d = ' '.join('%s%.1f,%.1f' % ('M' if k == 0 else 'L', X(p[0]), Y(p[series]))
                     for k, p in enumerate(points))
        g.append('<path d="%s" class="line %s"/>' % (d, colvar))

    # A dot per commit is noise once the slot is narrower than a dot, and it hides the line it is
    # meant to annotate. Mark what MOVED something, plus both ends for orientation.
    def _delta(p):
        try:
            return max(abs(float(p[6])), abs(float(p[7])))
        except (TypeError, ValueError):
            return 0.0
    DOT_PCT = 0.5
    ends = (points[0][0], points[-1][0])
    marked = [p for p in points if p[4] and (_delta(p) >= DOT_PCT or p[0] in ends)]
    for series, colvar, _ in SER:
        for p in marked:
            g.append('<circle cx="%.1f" cy="%.1f" r="3.8" class="dot %s"/>'
                     % (X(p[0]), Y(p[series]), colvar))

    for p in points:
        # Clamp BOTH edges and derive the width from them. Clamping only x while keeping a full-slot
        # width makes the first band reach half a slot into the second one -- the same overlap this
        # replaced, reintroduced at the left edge.
        x0 = min(max(pad_l, X(p[0]) - slot / 2), w - pad_r)
        x1 = min(max(pad_l, X(p[0]) + slot / 2), w - pad_r)
        g.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" '
                 'data-c="%s" data-s="%s" data-w="%s" data-k="%s" data-b="%s"/>'
                 % (x0, pad_t, x1 - x0, h - pad_b - pad_t, p[5], p[1], p[6], p[7], p[8]))

    last = points[-1]
    for series, colvar, label in SER:
        g.append('<text x="%d" y="%.1f" class="dl %s" %s>%s %.3f×</text>'
                 % (w - pad_r + 10, Y(last[series]) + 4 * S, colvar, fs(12), label, last[series]))
    tick_every = 5 if len(points) <= 60 else 10
    for p in points:
        if p[0] % tick_every == 0 or p[0] == last[0]:
            g.append('<text x="%.1f" y="%.1f" class="tick" %s text-anchor="middle">%d</text>'
                     % (X(p[0]), h - pad_b + 20 * S, fs(11), p[0]))
    g.append('<text x="%d" y="%d" class="tick" %s text-anchor="middle">commit number on %s, '
             'oldest first — log scale, so equal percentages are equal heights</text>'
             % ((pad_l + w - pad_r) / 2, h - 8, fs(11), branch))
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="steps and prover cost across the '
            'commit series, as a ratio to the base, on a log scale">' % (w, h)
            + ''.join(g) + '</svg>')


def svg_bars(points, w=1360, h=300, branch='al/zkvm-r4'):
    """The per-commit step, as bars. Same points tuple as svg_chart.

    The curve above answers "where are we"; it answers "which commit paid" only as a slope, and at
    95 commits a 1 % commit is a couple of pixels of slope. A signed bar states it directly.

    The scale is LINEAR and spans the real range, so two bars are comparable by eye and a bar near
    nothing means a commit that did nearly nothing — which is the honest reading, not a rendering
    artefact. A log or symlog axis would make the small movers legible at the price of heights that
    cannot be compared, and this panel exists precisely to be compared.
    """
    if len(points) < 2:
        return '<p class=note>not enough commits yet</p>'
    pad_l, pad_r, pad_t, pad_b = 58, 206, 18, 66
    S = w / 920.0
    fs = lambda base: 'style="font-size:%.1fpx"' % (base * S)

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    # The first commit has no predecessor, so it has no step: the bars start at the second.
    bars = [(p[0], _f(p[6]), _f(p[7]), p) for p in points[1:]]
    vals = [v for _i, a, b, _p in bars for v in (a, b)]
    dmin, dmax = min(vals + [0.0]), max(vals + [0.0])
    pad = max((dmax - dmin) * 0.08, 0.05)
    dmin, dmax = dmin - pad, dmax + pad
    xs = [p[0] for p in points]
    X = lambda i: pad_l + (i - min(xs)) / max(1, (max(xs) - min(xs))) * (w - pad_l - pad_r)
    Y = lambda v: pad_t + (dmax - v) / (dmax - dmin) * (h - pad_t - pad_b)
    slot = (w - pad_l - pad_r) / max(1, len(points) - 1)
    bw = max(1.6, slot * 0.34)

    g = []
    step = 5.0
    for cand in (0.1, 0.25, 0.5, 1.0, 2.0, 5.0):
        if (dmax - dmin) / cand <= 12:
            step = cand
            break
    t = round(dmax / step) * step
    while t >= dmin:
        if dmin <= t <= dmax:
            cls = 'axis' if abs(t) < 1e-9 else 'grid'
            g.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="%s"/>'
                     % (pad_l, Y(t), w - pad_r, Y(t), cls))
            g.append('<text x="%d" y="%.1f" class="tick" %s text-anchor="end">%+.2f %%</text>'
                     % (pad_l - 8, Y(t) + 4 * S, fs(11), t))
        t -= step
    y0 = Y(0.0)

    # work left of the slot centre, cost right of it, in the colours the curve above uses.
    for off, series, colvar in ((-1, 1, 'c1'), (0, 2, 'c2')):
        for i, dw, dk, _p in bars:
            v = dw if series == 1 else dk
            x = X(i) + off * bw - bw * 0.05
            top, hh = min(Y(v), y0), abs(Y(v) - y0)
            if hh < 0.5:
                continue          # a commit that moved nothing draws nothing, not a stub
            g.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="bar %s"/>'
                     % (x, top, bw * 0.9, hh, colvar))

    for p in points[1:]:
        x0 = min(max(pad_l, X(p[0]) - slot / 2), w - pad_r)
        x1 = min(max(pad_l, X(p[0]) + slot / 2), w - pad_r)
        g.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" '
                 'data-c="%s" data-s="%s" data-w="%s" data-k="%s" data-b="%s"/>'
                 % (x0, pad_t, x1 - x0, h - pad_b - pad_t, p[5], p[1], p[6], p[7], p[8]))

    tick_every = 5 if len(points) <= 60 else 10
    for p in points:
        if p[0] % tick_every == 0 or p[0] == points[-1][0]:
            g.append('<text x="%.1f" y="%.1f" class="tick" %s text-anchor="middle">%d</text>'
                     % (X(p[0]), h - pad_b + 20 * S, fs(11), p[0]))
    g.append('<text x="%d" y="%d" class="tick" %s text-anchor="middle">commit number on %s — '
             'each bar is that commit against the one before it, so below the line is cheaper'
             '</text>' % ((pad_l + w - pad_r) / 2, h - 8, fs(11), branch))
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="what each commit changed, in percent '
            'against the previous commit">' % (w, h) + ''.join(g) + '</svg>')


CSS = """
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#e3e3e3;--card:#fafafa;--accent:#b45309;
--c1:%s;--c2:%s;--grid:#ececec}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--card:#1a1a1a;
--accent:#fbbf24;--c1:%s;--c2:%s;--grid:#2a2a2a}}
:root[data-theme="dark"]{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--card:#1a1a1a;
--accent:#fbbf24;--c1:%s;--c2:%s;--grid:#2a2a2a}
:root[data-theme="light"]{--bg:#fff;--fg:#111;--mut:#666;--line:#e3e3e3;--card:#fafafa;
--accent:#b45309;--c1:%s;--c2:%s;--grid:#ececec}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
.eyebrow{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
h1{font-size:29px;line-height:1.2;margin:0 0 14px}
h2{font-size:19px;margin:36px 0 10px;padding-top:16px;border-top:1px solid var(--line)}
.prov,.note{font-size:13px;color:var(--mut)}
.prov{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 14px}
.figure{border:1px solid var(--line);border-radius:10px;padding:12px 8px 4px;margin:14px 0;overflow-x:auto}
svg{display:block;width:100%%;height:auto;min-width:980px}
.grid{stroke:var(--grid);stroke-width:1}
.axis{stroke:var(--line);stroke-width:1}
.tick{fill:var(--mut);font-size:11px}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.dot{stroke:var(--bg);stroke-width:2}
.bar{stroke:none}
/* The legend is the control, so it looks like one. Hiding a curve is the answer to two lines that
   run close enough to read as one -- which they do wherever the two metrics track each other. */
.lg{appearance:none;background:none;border:0;padding:2px 6px;font:inherit;color:var(--mut);
cursor:pointer;border-radius:5px;display:inline-flex;align-items:center}
.lg:hover{background:var(--card)}
.lg:not(.on){opacity:.4;text-decoration:line-through}
.hint2{font-size:11.5px;color:var(--mut);opacity:.8}
.figure.off-c1 svg .c1,.figure.off-c2 svg .c2{display:none}
.copied{color:var(--c1);font-weight:600}
/* Only the failure tooltip takes the pointer. Left on permanently it sits under the moving
   cursor and steals the mouseout that hides it, which reads as flicker. */
#tip.grab{pointer-events:auto}
.pick{user-select:all;-webkit-user-select:all;background:var(--card);padding:1px 4px;border-radius:4px}
.bar.c1{fill:var(--c1)} .bar.c2{fill:var(--c2)}
.dl{font-size:12px;font-weight:600}
.c1{stroke:var(--c1)} .dl.c1{fill:var(--c1);stroke:none} circle.c1{fill:var(--c1)}
.c2{stroke:var(--c2)} .dl.c2{fill:var(--c2);stroke:none} circle.c2{fill:var(--c2)}
.hit{fill:transparent;cursor:copy}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mut);padding:2px 8px 10px}
.swatch{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
table{width:100%%;border-collapse:collapse;font-size:13px;margin:10px 0}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.flat td{color:var(--mut)}
.na{color:var(--mut);border-bottom:1px dotted var(--mut);cursor:help}
.win{color:var(--c1);font-weight:600}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace}
a.elf{color:inherit;text-decoration:none;border-bottom:1px dotted var(--mut)}
a.elf:hover{color:var(--c1);border-bottom-color:var(--c1)}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .08s;z-index:9;
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:8px 10px;font-size:12.5px;line-height:1.45;max-width:340px;box-shadow:0 4px 14px rgba(0,0,0,.18)}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--mut)}
""" % (C1L, C2L, C1D, C2D, C1D, C2D, C1L, C2L)

TIP_JS = """<script>
(function(){
  var tip=document.getElementById('tip');
  function show(e){
    var t=e.target; if(!t.classList||!t.classList.contains('hit')) return;
    tip.innerHTML='<b>'+t.dataset.c+'</b>'+(t.dataset.b?' · '+t.dataset.b:'')+'<br>'+t.dataset.s+
      '<br>steps '+t.dataset.w+' %  ·  cost '+t.dataset.k+' % vs the previous commit';
    tip.classList.remove('grab');
    tip.style.opacity=1; move(e);
  }
  function move(e){
    var pad=14,w=tip.offsetWidth,h=tip.offsetHeight,x=e.clientX+pad,y=e.clientY+pad;
    if(x+w>innerWidth-8) x=e.clientX-w-pad;
    if(y+h>innerHeight-8) y=e.clientY-h-pad;
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }
  // Hide a curve: two ratios that track each other overlap, and no amount of colour separates
  // them where they coincide. Scoped to the figure, so two charts on one page stay independent.
  document.addEventListener('click',function(e){
    var b=e.target.closest&&e.target.closest('.lg'); if(!b) return;
    var fig=b.closest('.figure'); if(!fig) return;
    var on=b.classList.toggle('on');
    fig.classList.toggle('off-'+b.dataset.ser,!on);
  });
  // Click a point to copy its commit. The sha is what every follow-up needs -- a build, a diff, a
  // bisect -- and reading nine characters off a tooltip to retype them is where they get corrupted.
  document.addEventListener('click',function(e){
    var t=e.target; if(!t.classList||!t.classList.contains('hit')) return;
    var sha=t.dataset.c; if(!sha) return;
    var done=function(ok){
      // On failure the sha is still put where a person can take it: `user-select:all` makes one
      // click select the whole thing. A browser may refuse the clipboard for reasons the page
      // cannot fix (no secure context, no user activation), and "it did not work" is not a result.
      tip.innerHTML=(ok?'<b>'+sha+'</b><br><span class=copied>copied to the clipboard</span>'
        :'<code class=pick>'+sha+'</code><br>the clipboard refused — click the sha to select it');
      tip.classList.toggle('grab',!ok);
      tip.style.opacity=1; move(e);
      if(ok) setTimeout(function(){tip.style.opacity=0;},1400);
    };
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(sha).then(function(){done(true);},function(){done(false);});
    } else {
      // file:// without a secure context has no async clipboard; the textarea trick still works.
      var a=document.createElement('textarea'); a.value=sha; document.body.appendChild(a); a.select();
      var ok=false; try{ok=document.execCommand('copy');}catch(_){}
      document.body.removeChild(a); done(ok);
    }
  });
  document.addEventListener('mouseover',show);
  document.addEventListener('mousemove',function(e){if(tip.style.opacity==1)move(e);});
  document.addEventListener('mouseout',function(e){
    if(e.target.classList&&e.target.classList.contains('hit')) tip.style.opacity=0;});
})();
</script>"""


def backend_label(c, has_sp1, zisk_moved):
    if not has_sp1:
        return 'ZisK' if zisk_moved else '—'
    return (('ZisK ' if zisk_moved else '') + ('SP1' if c.get('sp1_moved') else '')) or '—'


def render(out, index_name='index.tsv', measure_name='measure.tsv', with_sp1=True,
           branch='al/zkvm-r4', runtime=None, failed=(), base_ref=None, blocks_file=None,
           print_deltas=False):
    # A lineage measured under another ZisK runtime keeps its own pair of tables:
    # its ELFs share elf/ (the key is the sha, which already separates them) but
    # its numbers are on a different cost model and must not be merged.
    blocks = None
    if blocks_file:
        blocks = set()
        for line in open(blocks_file):
            name = os.path.basename(line.strip())
            if name:
                blocks.add(int(name.split('.', 1)[0]))
        if not blocks:
            sys.exit(f'empty block list: {blocks_file}')

    idx = read_index(HERE, index_name)
    per = read_measure(HERE, measure_name, blocks)
    sp1_idx = ({c['commit']: c['sha'] for c in read_index(SP1) if c['status'] == 'OK'}
               if with_sp1 else {})
    sp1_per = read_measure(SP1, blocks=blocks) if with_sp1 else defaultdict(dict)

    ok = [c for c in idx if c['status'] == 'OK' and c['sha']]
    if not ok:
        sys.exit('no built commits in index.tsv')
    base = ok[0]['sha']
    med = medians(per, base)
    # One size ordering for the whole page, taken from the BASE build's step counts: the blocks
    # called big must be the same blocks on every row, or two rows are not comparable.
    biggest = sorted(per.get(base, {}), key=lambda b: per[base][b][0])
    measured = [c for c in ok if c['sha'] in med]
    if not measured:
        sys.exit('no measurements yet — run series-measure.sh')
    sp1_base = sp1_idx.get(ok[0]['commit'])
    sp1_med = medians(sp1_per, sp1_base) if sp1_base else {}

    prev_s = None
    for c in measured:
        cur = sp1_idx.get(c['commit'])
        c['sp1_moved'] = bool(cur) and cur != prev_s
        if cur:
            prev_s = cur

    # A commit that does not build leaves no ELF, so the next commit's step is
    # measured across both and belongs to neither. Such a step is withheld rather
    # than shown: a delta that silently spans two commits is worse than no delta.
    unbuilt = {c['i'] for c in idx if c['status'] != 'OK'}

    pts, rows, prev, pooled_delta, big_delta, spread = [], [], None, {}, {}, {}
    for c in measured:
        m = med[c['sha']]
        moved = (prev is None) or (c['sha'] != prev['sha'])
        spans = bool(prev) and any(r in unbuilt for r in range(prev['i'] + 1, c['i']))
        c['spans'] = spans
        if not prev:
            dw = dk = 0.0
            pooled_delta[c['commit']] = big_delta[c['commit']] = 0.0
        else:
            _s = pairwise(per, prev['sha'], c['sha'], 0, biggest)
            _k = pairwise(per, prev['sha'], c['sha'], 1, biggest)
            dw = (_s['med'] - 1) * 100 if _s else None
            dk = (_k['med'] - 1) * 100 if _k else None
            big_delta[c['commit']] = (_k['big'] - 1) * 100 if _k else None
            if _k:
                spread[c['commit']] = (
                    f"{(_k['lo'] - 1) * 100:+.2f} % to {(_k['hi'] - 1) * 100:+.2f} % across the "
                    f"middle eight tenths; {_k['better']} of {_k['n']} blocks cheaper")
            # Keyed by commit rather than carried in the row tuple, which three other places
            # unpack positionally.
            pooled_delta[c['commit']] = (_k['pooled'] - 1) * 100 if _k else None
        if spans:
            dw = dk = None
            pooled_delta[c['commit']] = big_delta[c['commit']] = None
        pts.append((c['i'], c['subject'].replace('"', '&quot;'), m['steps'], m['cost'], moved,
                    c['commit'],
                    'n/a' if dw is None else f'{dw:+.2f}',
                    'n/a' if dk is None else f'{dk:+.2f}',
                    backend_label(c, bool(sp1_idx), moved)))
        rows.append((c, m, moved, dw, dk))
        prev = c

    # `n` is PER ELF, not one number for the page: medians() intersects each ELF's blocks with the
    # base's, and an ELF measured on fewer blocks than the base is the normal state of a series
    # still filling in. Reporting only the base's count read as "every row rests on this many
    # blocks" -- on the r10 lineage that was 114 against a tip measured on 89.
    n = med[base]['n']
    ns = sorted(med[c['sha']]['n'] for c in measured)
    n_lo, n_hi = ns[0], ns[-1]
    same_n = n_lo == n_hi
    h = ['<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         f'<title>{branch} — cost per commit</title>', f'<style>{CSS}</style>',
         '<div class=wrap>', '<p class=eyebrow>monad zkvm guest</p>',
         '<h1>What each commit costs, one commit at a time</h1>',
         '<div class=prov>'
         f'<b>Method</b> one guest ELF built per commit of <code>{branch}</code>'
         + (f' over <code>{base_ref}</code>' if base_ref else '')
         + ', then measured against the first commit. Ratios are the median of per-block ratios.'
         + (f'<br><b>{n} blocks</b>, the same set for every commit.' if same_n else
            f'<br>⚠️ <b>Not one block set.</b> Each ratio is the median over the blocks that ELF and '
            f'the base share, and that count runs from <b>{n_lo}</b> to <b>{n_hi}</b> blocks '
            f'(the base itself: {n}). The <code>n</code> column gives it per row, and a Δ between '
            f'two rows with different <code>n</code> is a step between medians taken on different '
            f'sets. Fill the gaps with more <code>series-measure.sh</code> offsets before quoting '
            f'the curve.')
         + '<br><b>Greyed commits did not change the ELF</b> — identical sha, so the commit cannot '
           'have moved that guest.'
         + ' <b>A greyed row cost nothing</b> — either because the ELF is byte-identical, and then '
           'its Δ is empty, or because a different binary measured the same, and then its Δ reads '
           '0.00 %. The second is worth a look: it is what a lever whose build option never '
           'reached the compiler looks like.'
         + (f'<br><b>Cost model</b> {runtime}. Numbers from another runtime are not '
            'comparable to these and are not mixed in.' if runtime else '')
         + (('<br><b>' + str(len(failed)) + ' commit(s) absent</b> — they do not compile, so no ELF '
             'exists to measure: ' + ', '.join(f'<code>{c}</code>' for c in failed)
             + '. Absent is not the same as unchanged.') if failed else '')
         + '</div>']

    h.append('<div class=figure><div class=legend>'
             '<button class="lg on" data-ser=c1><span class=swatch style="background:var(--c1)"></span>steps</button>'
             '<button class="lg on" data-ser=c2><span class=swatch style="background:var(--c2)"></span>prover COST</button>'
             '<span class=hint2>click a colour to isolate it · click a point to copy its sha</span></div>')
    h.append(svg_chart(pts, branch=branch))
    h.append('</div>')
    lastm = rows[-1][1]
    h.append(f'<p>End to end: <b class=win>{lastm["steps"]:.4f}×</b> the steps and '
             f'<b class=win>{lastm["cost"]:.4f}×</b> the prover COST of the base — '
             f'−{(1-lastm["steps"])*100:.1f} % and −{(1-lastm["cost"])*100:.1f} %.</p>')

    # The same numbers as the curve, differenced. A second panel rather than a replacement: the curve
    # answers "where are we", which bars cannot, and the bars answer "which commit paid", which the
    # curve can only express as a slope of a couple of pixels at this length.
    _moved = [q for q in pts[1:] if abs(_num(q[6])) >= 0.005 or abs(_num(q[7])) >= 0.005]
    h.append('<h2>What each commit changed</h2>')
    h.append(f'<p class=note>The step from the previous commit, in percent — the differences of the '
             f'curve above, on the same {n} blocks. <b>{len(_moved)} of {len(pts)-1} commits moved '
             f'something</b>; a commit that moved nothing draws no bar. The scale is linear, so two '
             f'bars are comparable and a bar near nothing is a commit that did nearly nothing.</p>')
    h.append('<div class=figure><div class=legend>'
             '<button class="lg on" data-ser=c1><span class=swatch style="background:var(--c1)"></span>steps</button>'
             '<button class="lg on" data-ser=c2><span class=swatch style="background:var(--c2)"></span>prover COST</button>'
             '<span class=hint2>click a colour to isolate it · click a point to copy its sha</span></div>')
    h.append(svg_bars(pts, branch=branch))
    h.append('</div>')

    # SP1 gets its own figure, and only once the sample is both wide enough and
    # reaches the tip. The run fills every ELF on one block before moving to the
    # next, so it looks complete long before it is representative: a partial one
    # once read as "SP1 gained nothing", from three blocks.
    sp1_ok = [c for c in measured if sp1_idx.get(c['commit']) in sp1_med]
    sp1_n = sp1_med[sp1_base]['n'] if sp1_base in sp1_med else 0
    if sp1_ok and sp1_n >= 10 and sp1_ok[-1]['commit'] == measured[-1]['commit']:
        spts, sprev = [], None
        for c in sp1_ok:
            m = sp1_med[sp1_idx[c['commit']]]
            ds = dkk = 0.0
            if sprev and sprev in sp1_med:
                ds = (m['steps'] / sp1_med[sprev]['steps'] - 1) * 100
                dkk = (m['cost'] / sp1_med[sprev]['cost'] - 1) * 100
            spts.append((c['i'], c['subject'].replace('"', '&quot;'), m['steps'], m['cost'],
                         sp1_idx[c['commit']] != sprev, c['commit'], f'{ds:+.2f}', f'{dkk:+.2f}', 'SP1'))
            sprev = sp1_idx[c['commit']]
        h.append('<h2>The same series on SP1</h2>')
        h.append(f'<p class=note>Cycles and PGU on <b>{sp1_n} blocks</b> — deliberately narrower than '
                 'the ZisK sample, because SP1 execution costs about twenty times as much per block. '
                 'The two charts do not share a sample; read each against itself.</p>')
        h.append('<div class=figure><div class=legend>'
                 '<button class="lg on" data-ser=c1><span class=swatch style="background:var(--c1)"></span>cycles</button>'
                 '<button class="lg on" data-ser=c2><span class=swatch style="background:var(--c2)"></span>PGU</button>'
                 '<span class=hint2>click a colour to isolate it · click a point to copy its sha</span></div>')
        h.append(svg_chart(spts, branch=branch))
        h.append('</div>')
        h.append(f'<p>End to end on SP1: <b class=win>{spts[-1][2]:.4f}×</b> the cycles and '
                 f'<b class=win>{spts[-1][3]:.4f}×</b> the PGU of the base.</p>')
    elif sp1_med:
        h.append('<h2>The same series on SP1</h2>')
        h.append(f'<p class=note>Measuring — {len(sp1_ok)} of {len(measured)} commits on {sp1_n} '
                 'block(s). This renders at ten blocks with the branch tip covered.</p>')

    # Both backends' prover cost side by side, and nothing else: work and cycles
    # are the input to that number, and showing four columns per commit made the
    # table unreadable at 57 rows. A commit with no SP1 ELF shows "—" rather than
    # a blank, so "not built" and "did not move" stay distinguishable.
    sp1_by_commit, sprev = {}, None
    for c in measured:
        sh = sp1_idx.get(c['commit'])
        if sh in sp1_med:
            d = (sp1_med[sh]['cost'] / sp1_med[sprev]['cost'] - 1) * 100 if sprev in sp1_med else 0.0
            sp1_by_commit[c['commit']] = (sp1_med[sh]['cost'], d, sh != sprev)
        if sh:
            sprev = sh

    # Three distinct states, three notations, and "—" only ever means no data:
    #   +0.12 %  measured, the ELF changed
    #   0.00 %   measured, the ELF changed, the effect rounds to nothing
    #   (blank)  the ELF did not change, so there is nothing to report
    #   —        not built for that backend
    # The ratio links to the ELF it was measured from. Relative to this file's
    # own directory, so it resolves wherever the results tree is opened; the
    # binaries are gitignored, so this reaches the local build, not a URL.
    def elf_link(sha, backend, text):
        d = 'series' if backend == 'zisk' else 'series-sp1'
        return (f'<a class=elf href="../{d}/elf/{sha}.elf" '
                f'title="{backend} ELF {sha}" download>{text}</a>')

    def cell(v, moved):
        if not moved:
            return ''
        if v is None:
            return ('<span class=na title="the preceding commit does not build, so this step '
                    'spans two commits and is not attributable to either">n/a</span>')
        return f'{v:+.2f} %' if abs(v) >= 0.005 else '0.00 %' 

    any_span = any(c.get('spans') for c, _, _, _, _ in rows)
    h.append('<h2>Every commit</h2>')
    h.append('<p class=note>Prover cost only — ' + ('ZisK COST and SP1 PGU, each' if with_sp1
             else 'ZisK COST') + ' as a ratio to the base and '
             'as the step from the previous commit. Work and cycles drive those numbers and are in '
             'the charts above. <b>Each ratio links to the ELF it was measured from</b> — the '
             'binaries live beside this report and are not in git, so the link reaches your local '
             'build.<br>A <b>blank</b> Δ means the commit did not change that guest\'s '
             'ELF and <b>0.00 %</b> means it did but the effect rounds to nothing'
             + (', and <b>—</b> means the guest was not built for that backend at that commit'
                if with_sp1 else '')
             + ('. <b>n/a</b> means the preceding commit does not build, so the step would span '
                'two commits and belong to neither' if any_span else '') + '.</p>')
    h.append('<table><tr><th class=n>#</th><th>commit</th><th>subject</th>'
             '<th class=n title="blocks this ELF and the base share">n</th>'
             '<th class=n>ZisK COST</th>'
             '<th class=n title="median of this commit\'s per-block ratios against the one '
             'before it: what a typical block pays">Δ</th>'
             '<th class=n title="total COST over the corpus against the commit before it: what a '
             'prover pays. It parts from the median exactly when a commit pays on the blocks that '
             'do the work it targets and not on the rest">Δ corpus</th>'
             '<th class=n title="total COST over the largest tenth of the corpus by step count '
             '-- the blocks a proving budget is actually spent on. Pooled like Δ corpus and not a '
             'median, because within that tenth too the win sits on a few blocks. A median '
             'regression is worth taking when this column pays for it; hover the Δ cell for the '
             'spread">Δ top 10%</th>'
             + ('<th class=n>SP1 PGU</th><th class=n>Δ</th>' if with_sp1 else '') + '</tr>')
    for c, m, moved, dw, dk in rows:
        sp = sp1_by_commit.get(c['commit'])
        # Grey means "this commit cost nothing", and it has to mean that consistently: a row
        # showing 0.00 % in full contrast beside a greyed one that shows nothing reads as an
        # inconsistency, because from the reader's side both are the same news. The DISTINCTION
        # survives in the Δ cell, which stays empty when the ELF is byte-identical and prints
        # 0.00 % when a different binary measured the same -- two different facts, and the second
        # is the one that catches a lever whose option never reached the compiler.
        # Still means still on EVERY view. A commit flat on the median and on the corpus but
        # -1.3 % over the big blocks is the one row that must not be greyed out.
        _dp = pooled_delta.get(c['commit'])
        _db = big_delta.get(c['commit'])
        _still = ((dk is None or abs(dk) < 0.005) and (dw is None or abs(dw) < 0.005)
                  and (_dp is None or abs(_dp) < 0.005) and (_db is None or abs(_db) < 0.005))
        _s = spread.get(c['commit'])
        _sp_title = f' title="{html.escape(_s, quote=True)}"' if _s and moved else ''
        flat = '' if ((moved or (sp and sp[2])) and not _still) else ' class=flat'
        if sp:
            sp_ratio, sp_cell = f'{sp[0]:.4f}×', cell(sp[1], sp[2])
        else:
            sp_ratio, sp_cell = '—', '—'
        zisk_cell = elf_link(c['sha'], 'zisk', f'{m["cost"]:.4f}×')
        if sp:
            sp_ratio = elf_link(sp1_idx[c['commit']], 'sp1', f'{sp[0]:.4f}×')
        # A short row is not an error, but it is not the base's sample either -- say which.
        n_cell = (f'{m["n"]}' if m['n'] == n else
                  f'<span class=na title="measured on {m["n"]} of the base\'s {n} blocks — this '
                  f'ratio and the one above it do not rest on the same set">{m["n"]}</span>')
        h.append(f'<tr{flat}><td class=n>{c["i"]}</td><td><code>{c["commit"]}</code></td>'
                 f'<td>{c["subject"]}</td><td class=n>{n_cell}</td>'
                 f'<td class=n>{zisk_cell}</td>'
                 f'<td class=n{_sp_title}>{cell(dk, moved)}</td>'
                 f'<td class=n>{cell(pooled_delta.get(c["commit"]), moved)}</td>'
                 f'<td class=n>{cell(big_delta.get(c["commit"]), moved)}</td>'
                 + (f'<td class=n>{sp_ratio}</td><td class=n>{sp_cell}</td>' if with_sp1 else '')
                 + '</tr>')
    h.append('</table>')

    # Every commit that changed either ELF, not a magnitude shortlist: a commit
    # that moves the guest by +0.04 % is still a commit that moved the guest, and
    # cutting it made the table quietly disagree with the one above.
    movers = [r for r in rows if r[2] or sp1_by_commit.get(r[0]['commit'], (0, 0, False))[2]]
    if movers:
        h.append('<h2>What moved the guest</h2>')
        h.append(f'<p class=note>Every one of the {len(movers)} commits that changed an ELF, ordered '
                 'by effect on ZisK COST. No magnitude cut — a change too small to prove is still a '
                 'change, and the floor is a statement about evidence, not about size.</p>')
        h.append('<table><tr><th>commit</th><th>subject</th><th class=n>ZisK COST Δ</th>'
                 + ('<th class=n>SP1 PGU Δ</th>' if with_sp1 else '') + '</tr>')
        for c, m, moved, dw, dk in sorted(movers, key=lambda r: (r[4] is None, r[4] or 0)):
            sp = sp1_by_commit.get(c['commit'])
            h.append(f'<tr><td><code>{c["commit"]}</code></td><td>{c["subject"]}</td>'
                     f'<td class=n>{cell(dk, moved)}</td>'
                     + (f'<td class=n>{cell(sp[1], sp[2]) if sp else "—"}</td>' if with_sp1 else '')
                     + '</tr>')
        h.append('</table>')

    h.append('<div id=tip></div>')
    h.append(TIP_JS)
    h.append('<footer>Generated by <code>profiling/series/report.py</code> from '
             '<code>index.tsv</code> and <code>measure.tsv</code>. The table is the chart\'s data '
             'view; every number in the prose comes from it.</footer></div>')
    open(out, 'w').write('\n'.join(h))
    print(f'wrote {out}  ({len(rows)} commits, {n} blocks)')
    if print_deltas:
        # The same four figures as the page's table, one line per commit, for a run that only
        # wants to know what its last commits did. The first row is the reference: it has no step.
        pct = lambda v: '      n/a' if v is None else f'{v:+8.3f}%'
        print(f"\n{'':>5} {'commit':9}  {'steps':>9} {'COST':>9} {'pooled':>9} {'top 10%':>9}  (vs previous row)")
        for j, (c, m, moved, dw, dk) in enumerate(rows):
            if j == 0:
                cells = f"{'ref':>9} {'ref':>9} {'ref':>9} {'ref':>9}"
            else:
                cells = (f"{pct(dw)} {pct(dk)} {pct(pooled_delta.get(c['commit']))} "
                         f"{pct(big_delta.get(c['commit']))}")
            print(f"{'#' + str(c['i']):>5} {c['commit'][:9]:9}  {cells}  {c['subject'][:64]}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(os.path.dirname(HERE), 'results', 'series-r4.html'))
    ap.add_argument('--index', default='index.tsv')
    ap.add_argument('--measure', default='measure.tsv')
    ap.add_argument('--no-sp1', action='store_true')
    ap.add_argument('--branch', default='al/zkvm-r4')
    ap.add_argument('--runtime', default=None)
    # NOT defaulted to a branch name. The page used to print `origin/sam/zkvm-zisk-sp1` in every
    # case, while series-build-lineage.sh walks BASE (default 3d237fe69) -- a ref that has since
    # moved, so the page named a base it had not measured. Unnamed beats wrong.
    ap.add_argument('--base', default=None, metavar='REF',
                    help='the BASE series-build-lineage.sh walked from; omitted if not given')
    ap.add_argument('--blocks-file', metavar='PATH',
                    help='restrict the report to these witnesses/blocks without pruning the cache')
    ap.add_argument('--print-deltas', action='store_true',
                    help='also print each commit\'s step from the previous row to stdout')
    a = ap.parse_args()
    failed = tuple(c['commit'] for c in read_index(HERE, a.index) if c['status'] != 'OK')
    render(a.out, a.index, a.measure, not a.no_sp1, a.branch, a.runtime, failed, a.base,
           a.blocks_file, a.print_deltas)
