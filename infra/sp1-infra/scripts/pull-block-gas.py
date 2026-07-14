#!/usr/bin/env python3
"""
pull-block-gas.py — pull gas_used for a set of Ethereum blocks via JSON-RPC, write CSV + print stats.

Default: a reproducible random 1000-block sample from Succinct's SP1 Hypercube benchmark range
(blocks 23807739..23812008, ~15-16 Nov 2025, gas limit 45M) so you can compare apples-to-apples
with their "95.4% <10s / 99.7% <12s" claim. All knobs are overridable.

Needs an ARCHIVE node for historical blocks (your RSP `RPC_1` works). Stdlib only, no deps.

Examples:
  # reproduce Succinct's window (random 1000 of the 4270-block range)
  RPC_1=https://your-archive-rpc python3 pull-block-gas.py

  # the FULL Succinct window (all 4270 blocks)
  RPC_1=... python3 pull-block-gas.py --mode all

  # current mainnet: last 1000 blocks
  RPC_1=... python3 pull-block-gas.py --latest 1000

  # arbitrary range
  RPC_1=... python3 pull-block-gas.py --start 25000000 --end 25004000 --n 1000
"""
import argparse, csv, json, os, random, statistics, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

def rpc(url, method, params, retries=6):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "pull-block-gas/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            if "error" in j: last = j["error"]; continue   # JSON-RPC error (e.g. CU exhausted)
            return j.get("result")
        except Exception as e:
            last = str(e)
            # Alchemy rate-limit (HTTP 429) / transient: exponential backoff before retry
            time.sleep(min(0.5 * (2 ** attempt), 8.0))
    raise RuntimeError(f"{method}{params} failed: {last}")

def get_block(url, n):
    try:
        b = rpc(url, "eth_getBlockByNumber", [hex(n), False])
        if not b:
            return None
        return (n, int(b["gasUsed"], 16), int(b["gasLimit"], 16), len(b["transactions"]), int(b["timestamp"], 16))
    except Exception as e:
        print(f"  ! block {n}: {e}", file=sys.stderr)
        return None

def quantile(vals, p):  # vals sorted; p in [0,100]
    if not vals: return 0.0
    k = (len(vals) - 1) * p / 100.0
    f = int(k); c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("RPC_1"), help="archive RPC URL (or set RPC_1)")
    ap.add_argument("--start", type=int, default=23807739, help="range start (Succinct benchmark start)")
    ap.add_argument("--end",   type=int, default=23812008, help="range end (Succinct benchmark end)")
    ap.add_argument("--n",     type=int, default=1000, help="sample size (mode=random)")
    ap.add_argument("--mode",  choices=["random", "all"], default="random")
    ap.add_argument("--latest", type=int, default=0, help="if >0: pull the last N blocks (overrides range)")
    ap.add_argument("--seed",  type=int, default=42, help="RNG seed (reproducible sample)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out",   default="block-gas.csv")
    a = ap.parse_args()
    if not a.rpc:
        sys.exit("ERROR: set --rpc or the RPC_1 env var (must be an archive node).")

    if a.latest > 0:
        head = int(rpc(a.rpc, "eth_blockNumber", []), 16)
        blocks = list(range(head - a.latest + 1, head + 1))
        print(f"head={head}; pulling last {a.latest} blocks ({blocks[0]}..{blocks[-1]})")
    else:
        universe = list(range(a.start, a.end + 1))
        if a.mode == "random":
            random.seed(a.seed)
            blocks = sorted(random.sample(universe, min(a.n, len(universe))))
        else:
            blocks = universe
        print(f"pulling {len(blocks)} blocks from {a.start}..{a.end} (mode={a.mode}, seed={a.seed})")

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(get_block, a.rpc, b) for b in blocks]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r: rows.append(r)
            if i % 100 == 0: print(f"  {i}/{len(blocks)} ...", file=sys.stderr)
    rows.sort()
    if not rows:
        sys.exit("no blocks fetched (check RPC / archive support)")

    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["block", "gas_used", "gas_limit", "pct_full", "tx_count", "timestamp"])
        for bn, gu, gl, tx, ts in rows:
            w.writerow([bn, gu, gl, round(100 * gu / gl, 2), tx, ts])
    print(f"\nwrote {len(rows)} rows -> {a.out}  ({len(blocks) - len(rows)} failed)")

    gas  = sorted(g for _, g, _, _, _ in rows)
    full = [100 * g / gl for _, g, gl, _, _ in rows]
    txs  = [tx for _, _, _, tx, _ in rows]
    M = 1e6
    print("\n=== gas_used (M) ===")
    print(f"  n={len(gas)}  min {min(gas)/M:.1f}  p10 {quantile(gas,10)/M:.1f}  "
          f"median {quantile(gas,50)/M:.1f}  mean {statistics.mean(gas)/M:.1f}  "
          f"p90 {quantile(gas,90)/M:.1f}  p95 {quantile(gas,95)/M:.1f}  p99 {quantile(gas,99)/M:.1f}  max {max(gas)/M:.1f}")
    print(f"  mean fill: {statistics.mean(full):.0f}% of gas limit   |   mean tx/block: {statistics.mean(txs):.0f}")
    print("\n=== fill distribution (% of gas limit) ===")
    for lo, hi in [(0, 10), (10, 25), (25, 50), (50, 75), (75, 101)]:
        c = sum(1 for f in full if lo <= f < hi)
        label = f"{lo}-{hi-1 if hi <= 100 else 100}%"
        print(f"  {label:>8} : {c:>4} ({100*c/len(full):.0f}%)  {'#' * round(40*c/len(full))}")
    print("\nNote: gas is only a proxy for proving cost. For the real distribution, run RSP")
    print(f"      `--mode execute` on these blocks (from {a.out}) to get cycles per block.")

if __name__ == "__main__":
    main()
