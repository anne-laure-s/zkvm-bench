#!/usr/bin/env python3
"""
block-cycles.py — run RSP *execute* over a set of blocks to collect REAL cycle counts, then stats.

Heavy companion to pull-block-gas.py. Gas = 1 RPC call/block; CYCLES require RSP to fetch each
block's full state witness (eth_getProof — ARCHIVE node) and EXECUTE the guest. Budget accordingly:
this is minutes-to-hours for 1000 blocks, and hammers your RPC. Start small (--n 100) to get a shape.

Per block it runs (NO proving, NO GPU — same command that produced rsp/report.csv):
    <rsp> --block-number B --chain-id 1 --cache-dir CACHE --report-path REPORTS/<B>.csv
One report file per block → race-free under parallelism, and naturally resumable (skip existing).
RSP reads RPC_<chain> from $RSP_DIR/.env, so put your archive RPC there (RPC_1=...).

Usage:
  # use the EXACT blocks from the gas pull (so gas & cycles align per block):
  RSP_DIR=/path/to/rsp python3 block-cycles.py --from-gas block-gas.csv --n 100 --workers 2

  # or regenerate the same sample as pull-block-gas.py (same seed → same blocks):
  RSP_DIR=/path/to/rsp python3 block-cycles.py --start 23807739 --end 23812008 --n 1000 --seed 42

Prereq: build the binary once →  (cd $RSP_DIR && cargo build --release --bin rsp)
"""
import argparse, csv, glob, os, random, statistics, subprocess, sys, shlex
from concurrent.futures import ThreadPoolExecutor, as_completed

def quantile(v, p):
    if not v: return 0.0
    k = (len(v) - 1) * p / 100.0; f = int(k); c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)

def run_block(rsp_cmd, rsp_dir, cache, reports_dir, chain, b):
    out = os.path.join(reports_dir, f"{b}.csv")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return (b, "skip")
    cmd = shlex.split(rsp_cmd) + ["--block-number", str(b), "--chain-id", str(chain),
                                  "--cache-dir", cache, "--report-path", out]
    try:
        r = subprocess.run(cmd, cwd=rsp_dir, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
            lines = [l.strip() for l in r.stderr.splitlines() if l.strip()]
            err = next((l for l in lines if l.startswith("Error") or "429" in l or "error" in l.lower()),
                       lines[-1] if lines else "(no stderr)")
            print(f"  ! block {b} rc={r.returncode}: {err}", file=sys.stderr)
            return (b, "fail")
        return (b, "ok")
    except Exception as e:
        print(f"  ! block {b}: {e}", file=sys.stderr); return (b, "fail")

def load_cycles(reports_dir):
    rec = {}
    for fp in glob.glob(os.path.join(reports_dir, "*.csv")):
        try:
            with open(fp) as f:
                for row in csv.DictReader(f):
                    rec[int(row["block_number"])] = int(row["total_cycles_count"])
        except Exception:
            pass
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rsp-dir", default=os.environ.get("RSP_DIR"))
    ap.add_argument("--rsp-cmd", default=os.environ.get("RSP_CMD"),
                    help="defaults to <rsp-dir>/target/release/rsp, else 'cargo run --release --bin rsp --'")
    ap.add_argument("--from-gas", help="CSV from pull-block-gas.py (uses its 'block' column)")
    ap.add_argument("--start", type=int, default=23807739)
    ap.add_argument("--end",   type=int, default=23812008)
    ap.add_argument("--n",     type=int, default=0, help="limit to N blocks (0 = all from source)")
    ap.add_argument("--seed",  type=int, default=42)
    ap.add_argument("--mode",  choices=["random", "all"], default="random")
    ap.add_argument("--chain", type=int, default=1)
    ap.add_argument("--cache-dir", default="cache", help="relative to rsp-dir (reuses RSP cache)")
    ap.add_argument("--reports-dir", default="bench-reports", help="relative to rsp-dir; one CSV/block")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel RSP execs. RSP already bursts requests per block, so >1 multiplies "
                         "RPC load → 429s on shared providers (Alchemy). Raise only on a dedicated archive node.")
    a = ap.parse_args()
    if not a.rsp_dir: sys.exit("ERROR: set --rsp-dir or RSP_DIR (your rsp checkout; reads RPC from its .env)")
    a.rsp_dir = os.path.abspath(a.rsp_dir)

    rsp_cmd = a.rsp_cmd
    if not rsp_cmd:
        binp = os.path.join(a.rsp_dir, "target/release/rsp")
        rsp_cmd = binp if os.path.exists(binp) else "cargo run --release --bin rsp --"
    if rsp_cmd.startswith("cargo"):
        print("note: using 'cargo run' per block (slow). Build once for speed: "
              "(cd $RSP_DIR && cargo build --release --bin rsp)", file=sys.stderr)

    # build the block list
    if a.from_gas:
        with open(a.from_gas) as f:
            blocks = [int(r["block"]) for r in csv.DictReader(f)]
        if a.n and a.n < len(blocks):
            blocks = sorted(random.Random(a.seed).sample(blocks, a.n))
    else:
        uni = list(range(a.start, a.end + 1))
        blocks = uni if a.mode == "all" else sorted(random.Random(a.seed).sample(uni, min(a.n or 1000, len(uni))))

    reports_dir = os.path.join(a.rsp_dir, a.reports_dir)
    os.makedirs(reports_dir, exist_ok=True)
    done = {int(os.path.basename(p)[:-4]) for p in glob.glob(os.path.join(reports_dir, "*.csv"))
            if os.path.basename(p)[:-4].isdigit()}
    todo = [b for b in blocks if b not in done]
    print(f"{len(blocks)} blocks | {len(set(blocks) & done)} already done | running {len(todo)} (workers={a.workers})")
    print("⚠  RSP execute = fetch state (eth_getProof, ARCHIVE) + execute per block. This is the slow part.")

    ok = fail = skip = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(run_block, rsp_cmd, a.rsp_dir, a.cache_dir, reports_dir, a.chain, b) for b in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            b, st = fut.result()
            ok += st == "ok"; fail += st == "fail"; skip += st == "skip"
            print(f"  [{i}/{len(todo)}] block {b}: {st}  (ok={ok} fail={fail})", file=sys.stderr)

    # ---- stats ----
    rec = load_cycles(reports_dir)
    cyc = sorted(rec[b] for b in blocks if b in rec)
    if not cyc: sys.exit("no cycle data collected (check RSP build / archive RPC / eth_getProof support)")
    M = 1e6
    print(f"\n=== total_cycles (M) over {len(cyc)} blocks ===")
    print(f"  min {min(cyc)/M:.0f}  p10 {quantile(cyc,10)/M:.0f}  median {quantile(cyc,50)/M:.0f}  "
          f"mean {statistics.mean(cyc)/M:.0f}  p90 {quantile(cyc,90)/M:.0f}  p95 {quantile(cyc,95)/M:.0f}  "
          f"p99 {quantile(cyc,99)/M:.0f}  max {max(cyc)/M:.0f}")

    if a.from_gas:
        gas = {}
        with open(a.from_gas) as f:
            for r in csv.DictReader(f):
                try: gas[int(r["block"])] = int(r["gas_used"])
                except: pass
        cg = sorted(rec[b] / gas[b] for b in blocks if b in rec and gas.get(b))
        if cg:
            print(f"  cycles/gas: min {min(cg):.1f}  median {quantile(cg,50):.1f}  mean {statistics.mean(cg):.1f}  max {max(cg):.1f}")

    # estimated 16-GPU netProve from the fit you measured (netProve ≈ 6.5 + 0.033·cyc_M)
    est = sorted(6.5 + 0.033 * (c / M) for c in cyc)
    print(f"\n=== estimated netProve @16 GPU (s)  [fit 6.5 + 0.033·cyc_M; net of re-exec] ===")
    print(f"  median {quantile(est,50):.1f}  mean {statistics.mean(est):.1f}  "
          f"p90 {quantile(est,90):.1f}  p95 {quantile(est,95):.1f}  p99 {quantile(est,99):.1f}  max {max(est):.1f}")
    under = lambda t: 100 * sum(1 for e in est if e < t) / len(est)
    print(f"  share < 10s: {under(10):.1f}%   < 12s: {under(12):.1f}%   (compare: Succinct 95.4% <10s, 99.7% <12s)")
    print(f"\nPer-block reports: {reports_dir}/<block>.csv  (full RSP columns: cycles, gas, phases, syscalls)")
    print("Caveat: the netProve estimate uses YOUR measured fit; for the true percentiles, prove a random subset.")

if __name__ == "__main__":
    main()
