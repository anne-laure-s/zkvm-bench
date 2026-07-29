#!/usr/bin/env python3
"""rtp-latency.py — join the three artefacts the RTP pipeline leaves behind into one latency table.

Nothing in the pipeline measures latency end to end, on purpose: each stage records what it knows, where
it happens. This joins them per block.

  producer   infra/monad-witness/witness-tap  -> witness-manifest.csv
             t_block (chain time) · t_avail (block in block_db) · t_witness (replay done) · t_queued
  prover     cli/prove-farm -> <stack>/results/<chain>-<block>/<run>/report.json
             prove_secs · steps · proof_bytes · mode (mock or a real backend); t_proved = the record's mtime
  submitter  cli/ethproofs-mock -> submissions.jsonl
             the server-side verdict (verified / rejected) and its own timestamp

CLOCKS. The producer stamps and the run records are on the SAME machine (the box), so
`t_witness -> t_proved` is a single-clock measurement — that is the number to trust and the one reported
as the pipeline latency. submissions.jsonl comes from wherever the mock runs (typically a laptop, via a
reverse tunnel), so its timestamps are NOT comparable; they are used for the verdict only, and the
submission delta is printed as indicative when --cross-clock is passed.

MOCK RUNS ARE NOT LATENCY. A `mode=mock` record means cli/prove-farm --mock fabricated the proof in
milliseconds. Those rows are shown (they prove the plumbing works) but excluded from every summary
statistic, and the summary says so.

  profiling/rtp-latency.py --manifest ~/witness-manifest.csv --results infra/zisk-infra/results
  profiling/rtp-latency.py --manifest … --results … --submissions ethproofs-mock-data/submissions.jsonl
                           [--guest monad-zisk] [--chain-id 1] [--csv rtp-latency.csv] [--cross-clock]
"""
import argparse, csv, glob, json, os, statistics, sys

def read_manifest(path):
    """block -> the producer's stamps. Missing/blank cells stay None: this joins real data, it does not
    invent it."""
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                b = int(row["block"])
            except (KeyError, TypeError, ValueError):
                continue
            def num(k):
                v = (row.get(k) or "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None
            out[b] = {"t_block": num("t_block"), "t_avail": num("t_avail"),
                      "t_witness": num("t_witness"), "t_queued": num("t_queued"),
                      "bytes": num("bytes"), "root": (row.get("post_state_root") or "").strip(),
                      "promoted": (row.get("promoted") or "").strip() == "1"}
    return out

def read_runs(results_dir, chain_id, guest):
    """block -> the newest run record for it. `t_proved` is the report file's mtime — report.json carries
    no timestamp of its own (see cli/report-schema.md), and the file is written when the proof comes
    back, on the same clock as the manifest.

    Guest-scoped on purpose. Run records live at results/<guest>/<tag>/<run>/, and a record whose
    report.json names a DIFFERENT guest is skipped: joining by block number alone silently pairs this
    guest's witnesses with another guest's proofs of the same block, which produces confident nonsense
    (deltas of days). Older records predating the guest-scoped layout are still read from the legacy
    results/<tag>/ path, but only when their report.json claims this guest."""
    out = {}
    patterns = [os.path.join(results_dir, guest, f"{chain_id}-*", "*", "report.json"),
                os.path.join(results_dir, f"{chain_id}-*", "*", "report.json")]   # legacy, unscoped
    for rep in (p for pat in patterns for p in glob.glob(pat)):
        try:
            with open(rep) as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        scoped = os.sep + guest + os.sep in rep
        if r.get("guest") not in (None, guest) or (not scoped and r.get("guest") != guest):
            continue                          # another guest's proof, or unattributable — never guess
        b = r.get("block")
        if b is None:
            try:
                b = int(os.path.basename(os.path.dirname(os.path.dirname(rep))).split("-", 1)[1])
            except (IndexError, ValueError):
                continue
        t = os.path.getmtime(rep)
        if b not in out or t > out[b]["t_proved"]:
            out[b] = {"t_proved": t, "prove_secs": r.get("prove_secs"), "total_secs": r.get("total_secs"),
                      "proof_bytes": r.get("proof_bytes"), "mode": r.get("mode"),
                      "work": r.get("steps", r.get("cycles")), "run_dir": os.path.dirname(rep)}
    return out

def read_exec_reports(queue_dir, chain_id):
    """block -> the deterministic work-unit, from the exec-report the profiler leaves next to each queued
    input (infra/monad-witness/witness-profile). A prove run record carries prove_secs but not the
    work-unit, and these reports outlive the 7 MB witness they describe — so they are where `work` comes
    from once the witness has been dropped."""
    out = {}
    if not queue_dir:
        return out
    for rep in glob.glob(os.path.join(queue_dir, f"{chain_id}-*.exec-report.json")):
        try:
            with open(rep) as f:
                r = json.load(f)
            b = int(os.path.basename(rep).split("-", 1)[1].split(".", 1)[0])
        except (OSError, ValueError, IndexError):
            continue
        w = r.get("steps", r.get("cycles"))
        if w is not None:
            out[b] = w
    return out

def read_submissions(path):
    """block -> the server's last word on it (verdict + the mock's own clock)."""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            b = r.get("block_number")
            if b is None or r.get("event") != "proved":
                continue
            out[b] = {"ts": r.get("ts"), "verified": r.get("verified"), "mock": bool(r.get("mock")),
                      "reason": r.get("verify_reason"), "cluster": r.get("cluster_id")}
    return out

def fmt(v, unit="s", nd=1):
    return "—" if v is None else f"{v:.{nd}f}{unit}"

def main():
    ap = argparse.ArgumentParser(description="join producer + prover + submitter into a latency table")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--results", required=True, help="a stack's results/ dir (run records)")
    ap.add_argument("--submissions", help="ethproofs-mock submissions.jsonl (verdicts)")
    ap.add_argument("--queue", help="the prover queue dir (guests/<guest>/fixtures) — read the "
                                    "exec-reports there for the deterministic work-unit")
    ap.add_argument("--chain-id", default="1")
    ap.add_argument("--guest", default="monad-zisk")
    ap.add_argument("--csv", help="also write the joined rows here")
    ap.add_argument("--cross-clock", action="store_true",
                    help="also show witness→submitted, which mixes two machines' clocks")
    a = ap.parse_args()

    man = read_manifest(a.manifest)
    runs = read_runs(a.results, a.chain_id, a.guest)
    subs = read_submissions(a.submissions) if a.submissions else {}
    work = read_exec_reports(a.queue, a.chain_id)
    blocks = sorted(b for b in man if b in runs)
    if not blocks:
        sys.exit(f"rtp-latency: no block is in BOTH the manifest ({len(man)}) and this guest's run "
                 f"records ({len(runs)}) — is --results the right stack, --guest right, --chain-id right?")

    # A proof cannot predate the witness it was made from. When it looks like it does, the two artefacts
    # come from different runs (a stale record, a reused block number) — drop the row and say how many,
    # rather than print a delta of days as if it meant something.
    incoherent = [b for b in blocks
                  if man[b]["t_witness"] is not None and runs[b]["t_proved"] < man[b]["t_witness"]]
    blocks = [b for b in blocks if b not in set(incoherent)]
    if not blocks:
        sys.exit(f"rtp-latency: every candidate row was incoherent ({len(incoherent)} proof(s) older "
                 f"than their witness) — the run records don't belong to this manifest.")

    rows = []
    for b in blocks:
        m, r = man[b], runs[b]
        s = subs.get(b, {})
        def delta(a_, b_):
            return (a_ - b_) if (a_ is not None and b_ is not None) else None
        rows.append({
            # the run record's work-unit if it has one, else the profiler's exec-report
            "block": b, "mode": r["mode"], "work": r["work"] if r["work"] is not None else work.get(b),
            "proof_bytes": r["proof_bytes"],
            "exec_lag": delta(m["t_witness"], m["t_block"]),      # chain time -> witness (incl. finality)
            "queue": delta(m["t_queued"], m["t_witness"]),        # witness -> framed in the queue
            "prove_secs": r["prove_secs"],
            "pipeline": delta(r["t_proved"], m["t_witness"]),     # THE number: witness -> proof in hand
            "submitted": s.get("ts"), "verified": s.get("verified"),
            "mock": s.get("mock") or r["mode"] == "mock", "reason": s.get("reason"),
        })

    w = max(len("block"), 8)
    hdr = (f"{'block':>{w}}  {'mode':<12} {'work':>13} {'chain→wit':>10} {'wit→queue':>10} "
           f"{'prove':>8} {'wit→proof':>10}  verdict")
    print(hdr); print("-" * len(hdr))
    for x in rows:
        verdict = ("✓ verified" if x["verified"] is True else
                   "✗ rejected" if x["verified"] is False else "—")
        if x["mock"]:
            verdict += "  [MOCK]"
        print(f"{x['block']:>{w}}  {str(x['mode'] or '—'):<12} {str(x['work'] or '—'):>13} "
              f"{fmt(x['exec_lag']):>10} {fmt(x['queue']):>10} {fmt(x['prove_secs']):>8} "
              f"{fmt(x['pipeline']):>10}  {verdict}")

    real = [x for x in rows if not x["mock"] and x["pipeline"] is not None]
    mocks = len(rows) - len([x for x in rows if not x["mock"]])
    print()

    # COVERAGE — the RTP question. The target is every block as it arrives, but one cluster proves a block
    # in ~20-70 s against a 12 s slot, so with --newest-first most blocks are skipped by design. The
    # fraction actually proved is therefore as much a result as the per-block latency: a 2 s latency on
    # 10% of blocks is not real-time proving.
    lo, hi = min(rows[0]["block"], min(man)), max(man)
    arrived = [b for b in man if lo <= b <= hi]
    if arrived:
        pct = 100.0 * len(rows) / len(arrived)
        print(f"coverage: {len(rows)}/{len(arrived)} block(s) proved over {lo}..{hi} ({pct:.0f}%)"
              + (" — mock proofs, so this measures the plumbing's reach, not a prover's" if mocks else ""))
    if real:
        p = sorted(x["pipeline"] for x in real)
        print(f"pipeline latency (witness → proof in hand), {len(real)} real proof(s): "
              f"median {statistics.median(p):.1f}s · min {p[0]:.1f}s · max {p[-1]:.1f}s")
        pv = [x["prove_secs"] for x in real if x["prove_secs"] is not None]
        if pv:
            print(f"  of which proving: median {statistics.median(pv):.1f}s "
                  f"(the number ethproofs reports)")
        el = [x["exec_lag"] for x in real if x["exec_lag"] is not None]
        if el:
            print(f"  chain→witness (finality + execution, NOT ours to fix yet): "
                  f"median {statistics.median(el):.0f}s")
    else:
        print("no real proof yet — nothing to summarise.")
    if mocks:
        print(f"{mocks} mock row(s) shown but EXCLUDED from the summary: a mock-cluster proof is "
              f"fabricated in milliseconds and measures nothing.")
    if incoherent:
        print(f"{len(incoherent)} row(s) dropped as incoherent (proof older than its witness — records "
              f"from a different run): {', '.join(str(b) for b in incoherent[:6])}"
              f"{' …' if len(incoherent) > 6 else ''}")
    if a.cross_clock and subs:
        print("\n(cross-clock, indicative only — the submitter runs on another machine:)")
        for x in rows:
            if x["submitted"]:
                print(f"  {x['block']}  submitted at {x['submitted']}")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader(); wr.writerows(rows)
        print(f"\nwrote {a.csv} ({len(rows)} row(s))")

if __name__ == "__main__":
    main()
