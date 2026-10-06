#!/usr/bin/env python3
"""pair.py — paired read of hints-ab.csv. RUNS ON THE MAC, after repatriation.

Two medians side by side answer the wrong question: rounds are not independent, they are pairs run
back to back on the same box. What carries is the PAIRED difference and how many rounds favour which
arm — the same statistic infra/monad-witness/RTP-FINDINGS.md uses for every A/B in this repo.

Rows with rc != 0 drop out with their partner, so a pair is never half-counted.

  python3 pair.py ~/bench-hints-ab/hints-ab.csv
"""
import csv, statistics as st, sys
from collections import defaultdict

rows = [r for r in csv.DictReader(open(sys.argv[1]))]
bad = [r for r in rows if r["rc"] != "0"]
ok = defaultdict(dict)                      # (tag, round) -> {arm: secs}
for r in rows:
    if r["rc"] == "0":
        ok[(r["tag"], r["round"])][r["arm"]] = float(r["secs"])

STEPS = {}                                  # tag -> Msteps, for the per-Mstep read
try:
    for r in csv.DictReader(open(sys.argv[0].rsplit("/", 1)[0] + "/steps-reth.csv")):
        STEPS[r["tag"]] = int(r["steps"]) / 1e6
except OSError:
    pass

if bad:
    print(f"{len(bad)} row(s) with rc!=0 dropped "
          f"({sum(1 for r in bad if r['arm']=='nohints')} of them hint-free)\n")

for tag in dict.fromkeys(r["tag"] for r in rows):
    pairs = [(v["hints"], v["nohints"]) for (t, _), v in ok.items()
             if t == tag and "hints" in v and "nohints" in v]
    if not pairs:
        print(f"{tag}: no complete pair\n"); continue
    h = [p[0] for p in pairs]; n = [p[1] for p in pairs]
    d = [b - a for a, b in pairs]           # nohints - hints; positive = hints win
    fav = sum(1 for x in d if x > 0)
    m = STEPS.get(tag)
    print(f"{tag}  ({m:.1f} Msteps)" if m else f"{tag}")
    print(f"  hints   median {st.median(h):7.2f}s   ({min(h):.2f}–{max(h):.2f})")
    print(f"  nohints median {st.median(n):7.2f}s   ({min(n):.2f}–{max(n):.2f})")
    print(f"  paired  median {st.median(d):+7.2f}s   {fav}/{len(d)} rounds favour hints"
          f"   = {100*st.median(d)/st.median(n):+.1f}% of the hint-free time")
    if m:
        print(f"  per Mstep      {st.median(d)/m*1000:+7.2f} ms/Mstep")
    print()

tags = [t for t in dict.fromkeys(r["tag"] for r in rows)
        if any(t2 == t for (t2, _) in ok)]
if len(tags) == 2 and all(t in STEPS for t in tags):
    g = {}
    for t in tags:
        p = [(v["hints"], v["nohints"]) for (t2, _), v in ok.items()
             if t2 == t and "hints" in v and "nohints" in v]
        if p: g[t] = st.median([b - a for a, b in p])
    if len(g) == 2:
        (t1, t2) = sorted(g, key=lambda t: STEPS[t])
        rs, rg = STEPS[t2] / STEPS[t1], (g[t2] / g[t1] if g[t1] else float("inf"))
        print(f"flat or proportional: {rs:.2f}x the steps, {rg:.2f}x the gain")
        # Flat would put the gain ratio at 1 (same absolute saving whatever the block);
        # proportional would put it at the step ratio. Report the distance to each and let the
        # middle ground read as the middle ground rather than be forced into a verdict.
        df, dp = abs(rg - 1), abs(rg - rs)
        if dp < df / 2:
            v = "proportional — it tracks precompile volume, so it holds in relative terms on any block"
        elif df < dp / 2:
            v = "flat — a fixed per-proof cost, so it shrinks in relative terms as blocks grow"
        else:
            v = (f"between the two ({df:.2f} from flat, {dp:.2f} from proportional) — two block "
                 f"sizes cannot separate them; add a third far from both")
        print(f"  -> {v}")
