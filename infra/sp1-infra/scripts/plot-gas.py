#!/usr/bin/env python3
"""
plot-gas.py — render a self-contained SVG of the gas distribution from block-gas.csv.
No deps (stdlib only). English labels. Two panels: histogram + cumulative distribution (CDF).

Usage:  python3 plot-gas.py [--csv block-gas.csv] [--out docs/gas-distribution.svg]
"""
import argparse, csv, statistics

def quantile(v, p):
    v = sorted(v); k = (len(v) - 1) * p / 100; f = int(k); c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="block-gas.csv")
    ap.add_argument("--out", default="docs/gas-distribution.svg")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.csv)))
    g = [int(r["gas_used"]) / 1e6 for r in rows]      # gas in millions
    n = len(g); gs = sorted(g)
    med, mean = quantile(g, 50), statistics.mean(g)

    BIN = 5; NB = 9                                     # bins 0-5 .. 40-45M
    hist = [0] * NB
    for x in g:
        hist[min(int(x // BIN), NB - 1)] += 1
    hmax = max(hist)
    thr = [i for i in range(0, 47)]                    # CDF thresholds 0..46M
    cdf = [100 * sum(1 for x in g if x <= t) / n for t in thr]

    # ---- layout ----
    W, H = 880, 760
    L, R = 70, 28
    pw = W - L - R
    def panel(top, h): return top, top + h
    A0, A1 = panel(74, 250)        # histogram plot area (y)
    B0, B1 = panel(452, 250)       # cdf plot area (y)

    INK, SUB, MUT = "#2C2C2A", "#5F5E5A", "#888780"
    GRID, BAR, LINE = "#E1E0D9", "#378ADD", "#BA7517"
    s = []
    s.append(f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,Segoe UI,Roboto,sans-serif">')
    s.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    s.append(f'<text x="{L}" y="34" fill="{INK}" font-size="19" font-weight="500">Ethereum gas per block — SP1 Hypercube benchmark window</text>')
    s.append(f'<text x="{L}" y="56" fill="{SUB}" font-size="13">{n} random blocks · 23807742–23811998 · 15–16 Nov 2025 · gas limit ~45M</text>')

    # ---- Panel A: histogram ----
    s.append(f'<text x="{L}" y="{A0-8}" fill="{INK}" font-size="14" font-weight="500">Distribution of gas used</text>')
    # y gridlines + labels (count)
    for i in range(0, 6):
        val = round(hmax * i / 5 / 10) * 10
        y = A1 - (A1 - A0) * (val / hmax if hmax else 0)
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{L-8}" y="{y+4:.1f}" fill="{MUT}" font-size="11" text-anchor="end">{val}</text>')
    bw = pw / NB
    for i, c in enumerate(hist):
        x = L + i * bw
        bh = (A1 - A0) * (c / hmax)
        s.append(f'<rect x="{x+5:.1f}" y="{A1-bh:.1f}" width="{bw-10:.1f}" height="{bh:.1f}" fill="{BAR}" rx="3"/>')
        s.append(f'<text x="{x+bw/2:.1f}" y="{A1-bh-6:.1f}" fill="{SUB}" font-size="11" text-anchor="middle">{c}</text>')
        s.append(f'<text x="{x+bw/2:.1f}" y="{A1+16:.1f}" fill="{MUT}" font-size="11" text-anchor="middle">{i*BIN}-{(i+1)*BIN}</text>')
    s.append(f'<text x="{L+pw/2:.1f}" y="{A1+38:.1f}" fill="{SUB}" font-size="12" text-anchor="middle">Gas used (millions)</text>')
    s.append(f'<text x="{L-46}" y="{(A0+A1)/2:.1f}" fill="{SUB}" font-size="12" text-anchor="middle" transform="rotate(-90 {L-46} {(A0+A1)/2:.1f})">Number of blocks</text>')
    # median / mean markers
    for val, lab, col in [(med, f"median {med:.1f}M", LINE), (mean, f"mean {mean:.1f}M", "#993C1D")]:
        x = L + pw * (val / (BIN*NB))
        s.append(f'<line x1="{x:.1f}" y1="{A0}" x2="{x:.1f}" y2="{A1}" stroke="{col}" stroke-width="1.5" stroke-dasharray="4 3"/>')
        s.append(f'<text x="{x+4:.1f}" y="{A0+12}" fill="{col}" font-size="11">{lab}</text>')

    # ---- Panel B: CDF ----
    s.append(f'<text x="{L}" y="{B0-8}" fill="{INK}" font-size="14" font-weight="500">Cumulative distribution</text>')
    for i in range(0, 6):
        pct = i * 20
        y = B1 - (B1 - B0) * pct / 100
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{L-8}" y="{y+4:.1f}" fill="{MUT}" font-size="11" text-anchor="end">{pct}%</text>')
    maxt = thr[-1]
    pts = []
    for t, c in zip(thr, cdf):
        x = L + pw * t / maxt; y = B1 - (B1 - B0) * c / 100
        pts.append(f"{x:.1f},{y:.1f}")
    s.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{LINE}" stroke-width="2.5"/>')
    for t in range(0, maxt + 1, 5):
        x = L + pw * t / maxt
        s.append(f'<text x="{x:.1f}" y="{B1+16:.1f}" fill="{MUT}" font-size="11" text-anchor="middle">{t}</text>')
    s.append(f'<text x="{L+pw/2:.1f}" y="{B1+38:.1f}" fill="{SUB}" font-size="12" text-anchor="middle">Gas used (millions)</text>')
    s.append(f'<text x="{L-46}" y="{(B0+B1)/2:.1f}" fill="{SUB}" font-size="12" text-anchor="middle" transform="rotate(-90 {L-46} {(B0+B1)/2:.1f})">% of blocks ≤ x</text>')
    # annotate median & p90 on the CDF
    for p, lab in [(50, "median"), (90, "p90")]:
        val = quantile(g, p); x = L + pw * val / maxt; y = B1 - (B1 - B0) * p / 100
        s.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{LINE}"/>')
        s.append(f'<text x="{x+6:.1f}" y="{y+4:.1f}" fill="{SUB}" font-size="11">{lab}: {val:.1f}M</text>')
    s.append("</svg>")

    import os
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w").write("\n".join(s))
    print(f"wrote {a.out}  (n={n}, median {med:.1f}M, mean {mean:.1f}M)")

if __name__ == "__main__":
    main()
