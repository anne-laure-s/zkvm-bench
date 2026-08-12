#!/usr/bin/env python3
"""Drive the monad client over a growing block_db, batch by batch.

The monad runloop hard-asserts if a block is missing from the block_db, so
this orchestrator only ever asks it to execute ranges the fetcher has already
written: each iteration it finds the contiguous head of available blocks,
runs `monad --nblocks <n>` over them, and repeats. The client resumes from
the triedb's last finalized block on every start, so crashes and restarts
are safe.

State verification: the client itself validates the computed state root
(and receipts/txn/withdrawals roots, gas, bloom) against each block header
via validate_output_header, aborting on mismatch — and those headers were
fetched from the erigon oracle and hash-verified. So "the run advanced past
block N" means state matched mainnet for every block <= N. On any non-zero
exit this script preserves the log, writes an ALERT file, and stops.

Progress is written to status.json each iteration.
"""

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

import requests


def rpc_block_number(url: str, tag: str) -> int:
    r = requests.post(
        url,
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [tag, False],
            "id": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    return int(r.json()["result"]["number"], 16)


def last_executed(monad_cli: str, db: str) -> int:
    """Latest finalized block in the triedb, via monad-cli's db summary."""
    out = subprocess.run(
        [monad_cli, "--db", db],
        capture_output=True,
        text=True,
        timeout=300,
    )
    m = re.search(r"latest_finalized_block_id=(\d+)", out.stdout)
    if not m:
        raise RuntimeError(
            f"could not parse monad-cli db summary:\n{out.stdout}\n{out.stderr}"
        )
    return int(m.group(1))


def block_exists(block_db: str, n: int) -> bool:
    return os.path.exists(os.path.join(block_db, str(n))) or os.path.exists(
        os.path.join(block_db, f"{n // 1_000_000}M", str(n))
    )


def contiguous_head(block_db: str, start: int) -> int:
    """Last block N such that all of [start, N] are present."""
    n = start
    while block_exists(block_db, n):
        n += 1
    return n - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monad-bin", required=True)
    ap.add_argument("--monad-cli-bin", required=True)
    ap.add_argument("--block-db", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--logs-dir", required=True)
    ap.add_argument("--status-file", required=True)
    ap.add_argument("--nthreads", type=int, default=8)
    ap.add_argument(
        "--zkvm-witness", default=None,
        help="directory to dump per-block zkVM witnesses to (passed through "
        "to monad; forces the interpreter, ~7 MB per block)",
    )
    ap.add_argument(
        "--fixed-history-length", type=int, default=None,
        help="pin the trie history to N versions (passed through to monad). Needed for "
        "`monad-mpt --rewind-to` to reach the start of a bounded replay afterwards; must exceed "
        "the replayed span. Requires a node built with the matching patch.",
    )
    ap.add_argument("--max-batch", type=int, default=50_000)
    ap.add_argument(
        "--min-follow-batch", type=int, default=8,
        help="when at the tip, wait for this many blocks before restarting "
        "the client (amortizes startup cost; bounds how far we lag)",
    )
    ap.add_argument("--poll-interval", type=float, default=12.0)
    args = ap.parse_args()

    os.makedirs(args.logs_dir, exist_ok=True)
    alert_file = os.path.join(args.logs_dir, "ALERT")
    if os.path.exists(alert_file):
        print(f"refusing to start: {alert_file} exists from a previous failure")
        sys.exit(1)

    child = None

    def forward_signal(signum, _frame):
        if child is not None:
            child.send_signal(signum)
        sys.exit(1)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    while True:
        last = last_executed(args.monad_cli_bin, args.db)
        avail = contiguous_head(args.block_db, last + 1)
        # The finalized lookup is ADVISORY: it feeds `gap`, which is a display quantity and a
        # near-finality shortcut in the batch threshold below. Letting a 30 s timeout or a 5xx
        # from the RPC kill an otherwise healthy replay — with no ALERT, since ALERT is only
        # written when the monad child fails — costs a whole run to save nothing.
        try:
            head = rpc_block_number(args.rpc, "finalized")
            gap = head - last
        except Exception as exc:
            head, gap = None, None
            print(f"warning: finalized lookup failed ({exc!r}) — continuing, gap unknown",
                  flush=True)

        status = {
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_executed": last,
            "available": avail,
            "finalized_head": head,
            "gap_to_finalized": gap,
        }
        with open(args.status_file, "w") as f:
            json.dump(status, f, indent=1)

        n_ready = avail - last
        # gap is None == "distance to finality unknown". Fall back to min_follow_batch alone: the
        # original behaviour minus the near-finality shortcut, which is what not knowing the
        # distance should give you. (With --min-follow-batch 1 the threshold is 1 either way.)
        need = max(1, args.min_follow_batch if gap is None else min(args.min_follow_batch, gap))
        if n_ready < need:
            time.sleep(args.poll_interval)
            continue

        nblocks = min(n_ready, args.max_batch)
        log_path = os.path.join(
            args.logs_dir, f"monad_{last + 1}_{last + nblocks}.log"
        )
        print(
            f"executing blocks {last + 1}..{last + nblocks} "
            f"(gap to finalized: {'unknown' if gap is None else gap}) -> {log_path}",
            flush=True,
        )
        t0 = time.monotonic()
        cmd = [
            args.monad_bin,
            "--chain", "ethereum_mainnet",
            "--block-db", args.block_db,
            "--db", args.db,
            "--nblocks", str(nblocks),
            "--nthreads", str(args.nthreads),
        ]
        if args.zkvm_witness is not None:
            cmd += ["--zkvm-witness", args.zkvm_witness]
        if args.fixed_history_length is not None:
            cmd += ["--fixed-history-length", str(args.fixed_history_length)]
        with open(log_path, "w") as logf:
            child = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
            rc = child.wait()
            child = None
        elapsed = time.monotonic() - t0

        if rc != 0:
            tail = subprocess.run(
                ["tail", "-40", log_path], capture_output=True, text=True
            ).stdout
            msg = (
                f"monad exited {rc} on batch {last + 1}..{last + nblocks}\n"
                f"log: {log_path}\nlast lines:\n{tail}"
            )
            with open(alert_file, "w") as f:
                f.write(msg)
            print(f"ALERT: {msg}", flush=True)
            sys.exit(2)

        print(
            f"batch done: {nblocks} blocks in {elapsed:.0f}s "
            f"({nblocks / elapsed:.1f} blocks/s)",
            flush=True,
        )


if __name__ == "__main__":
    main()
