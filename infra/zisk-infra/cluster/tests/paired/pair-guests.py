#!/usr/bin/env python3
"""pair-guests.py — per-block r8-vs-reth from paired.csv. RUNS ON THE MAC.

The earlier arms ran on disjoint block sets, so only their slopes could be compared. Here both guests
prove the same block back to back, so the ratio is per block and the three readings — steps, COST,
prove time — sit on the same rows.

A pair with one failed half is dropped whole: half a pair is not a measurement.

  python3 pair-guests.py ~/bench-paired/paired.csv
"""
import csv, json, os, statistics as st, sys
from collections import defaultdict

rows = list(csv.DictReader(open(sys.argv[1])))
repo = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), '../../../../..'))
axis = json.load(open(os.path.join(repo, 'profiling/r8-compare.json')))['r8-vs-zisk-reth']['blocks']

ok = defaultdict(dict)
for r in rows:
    if r['rc'] == '0':
        ok[(r['tag'], r['pass'])][r['guest']] = float(r['secs'])
dropped = sum(1 for v in ok.values() if len(v) < 2) + sum(1 for r in rows if r['rc'] != '0')
if dropped:
    print(f"{dropped} row(s)/half-pair(s) dropped\n")

per = defaultdict(list)
for (tag, _), v in ok.items():
    if 'r8' in v and 'reth' in v:
        per[tag].append((v['r8'], v['reth']))

print(f"{'block':<12} {'r8 s':>8} {'reth s':>8} {'time':>7} {'steps':>7} {'COST':>7}")
tr, ts, tc = [], [], []
for tag in sorted(per, key=lambda t: axis[t.replace('1-', '')]['a']['work']):
    b = tag.replace('1-', '')
    r8 = st.median(p[0] for p in per[tag]); rt = st.median(p[1] for p in per[tag])
    a, bb = axis[b]['a'], axis[b]['b']
    rtime, rstep, rcost = r8 / rt, a['work'] / bb['work'], a['cost'] / bb['cost']
    tr.append(rtime); ts.append(rstep); tc.append(rcost)
    print(f"{b:<12} {r8:8.2f} {rt:8.2f} {rtime:6.3f}x {rstep:6.3f}x {rcost:6.3f}x")

if tr:
    print(f"\n{'median':<12} {'':>8} {'':>8} {st.median(tr):6.3f}x {st.median(ts):6.3f}x {st.median(tc):6.3f}x")
    print(f"\nWhich unit predicts the prove-time ratio:")
    for lab, v in (('steps', st.median(ts)), ('COST ', st.median(tc))):
        d = v - st.median(tr)
        print(f"  {lab}: {v:.3f}x against a measured {st.median(tr):.3f}x  ->  {d:+.3f} ({100*d/st.median(tr):+.1f}%)")
    print("\nThe closer one is the unit to quote for a prove-time claim. Both remain valid for what they")
    print("measure — steps are the deterministic work-unit, COST is ZisK's trace-area model.")
