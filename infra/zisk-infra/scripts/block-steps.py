#!/usr/bin/env python3
"""
block-steps.py — aggregate ZisK execute step-counts across blocks into distribution stats.

Lighter than re-running the guest: instead of re-executing each block,
it reads the `*.exec-report.json` files that `./run execute` / `./run gen-input` already wrote
(schema: {mode, steps, elapsed_secs, public_values_bytes}). ZisK "steps" are the analog of SP1
"cycles" — the deterministic work unit (elapsed is host-dependent, steps are not).

Usage:
    scripts/block-steps.py [--dir ../../guests/zisk-reth/inputs] [--csv steps.csv] [--top 0]

Prints a per-block table (steps, elapsed, Msteps/s) sorted by steps, then summary stats
(count / min / median / p90 / p99 / max / mean / total). Stdlib only, no deps.
With --csv, also writes the per-block rows as CSV.
"""
import argparse, glob, json, os, sys


def quantile(v, p):
    if not v:
        return 0.0
    v = sorted(v)
    k = (len(v) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def main():
    ap = argparse.ArgumentParser(description="Aggregate ZisK execute step-counts into stats.")
    ap.add_argument("--dir", default="../../guests/zisk-reth/inputs",
                    help="dir holding <tag>.exec-report.json files (default: ../../guests/zisk-reth/inputs)")
    ap.add_argument("--csv", help="also write the per-block rows to this CSV file")
    ap.add_argument("--top", type=int, default=0,
                    help="print only the N heaviest blocks (0 = all)")
    args = ap.parse_args()

    reports = sorted(glob.glob(os.path.join(args.dir, "*.exec-report.json")))
    if not reports:
        sys.exit(f"no *.exec-report.json under {args.dir} — run ./run execute / gen-input first")

    rows = []
    for path in reports:
        tag = os.path.basename(path)[:-len(".exec-report.json")]
        try:
            d = json.load(open(path))
        except (OSError, ValueError) as e:
            print(f"WARN: skipping {path}: {e}", file=sys.stderr)
            continue
        steps = int(d.get("steps") or 0)   # runner writes null when its step regex misses; `or 0` survives it
        elapsed = float(d.get("elapsed_secs", 0.0) or 0.0)
        msteps_s = (steps / 1e6 / elapsed) if elapsed > 0 else 0.0
        rows.append((tag, steps, elapsed, msteps_s))

    if not rows:
        sys.exit("no usable reports (all missing a 'steps' field?)")

    rows.sort(key=lambda r: r[1], reverse=True)
    shown = rows[:args.top] if args.top > 0 else rows

    print(f"{'block':<16}{'steps':>15}{'elapsed_s':>11}{'Msteps/s':>11}")
    for tag, steps, elapsed, msteps_s in shown:
        print(f"{tag:<16}{steps:>15,}{elapsed:>11.3f}{msteps_s:>11.2f}")

    steps_all = [r[1] for r in rows]
    n = len(steps_all)
    print(f"\n{n} block(s):")
    print(f"  min     {min(steps_all):>15,}")
    print(f"  median  {int(quantile(steps_all, 50)):>15,}")
    print(f"  p90     {int(quantile(steps_all, 90)):>15,}")
    print(f"  p99     {int(quantile(steps_all, 99)):>15,}")
    print(f"  max     {max(steps_all):>15,}")
    print(f"  mean    {int(sum(steps_all) / n):>15,}")
    print(f"  total   {sum(steps_all):>15,}")

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("block,steps,elapsed_secs,msteps_per_s\n")
            for tag, steps, elapsed, msteps_s in rows:
                f.write(f"{tag},{steps},{elapsed:.4f},{msteps_s:.4f}\n")
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
