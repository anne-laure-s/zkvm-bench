#!/usr/bin/env python3
"""Fetch Ethereum mainnet blocks from an RPC node into a monad block_db.

Pulls raw block RLP via debug_getRawBlock up to the node's "finalized" tag,
verifies keccak256(header RLP) against the node's canonical block hash, then
writes each block as a Brotli-compressed file the monad BlockDb reader
understands. New blocks are written flat into the block_db root
(<block_db>/<number>) — BlockDb tries the flat path before the sharded
<block_db>/<N/1e6>M/<number> layout, so flat files coexist with read-only
sharded history behind symlinks.

Runs forever; when caught up it polls for new finalized blocks.
"""

import argparse
import concurrent.futures
import threading
import json
import logging
import os
import re
import sys
import time

import brotli
import requests
from Crypto.Hash import keccak

log = logging.getLogger("fetch")


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def header_span(block_rlp: bytes) -> bytes:
    """Return the RLP bytes of the header (first item of the block list)."""
    b0 = block_rlp[0]
    if b0 < 0xF8:
        payload_start = 1  # short list (never true for a real block, but cheap)
    else:
        payload_start = 1 + (b0 - 0xF7)
    h0 = block_rlp[payload_start]
    if h0 < 0xF8:
        header_len = 1 + (h0 - 0xC0)
    else:
        lenlen = h0 - 0xF7
        header_len = (
            1
            + lenlen
            + int.from_bytes(
                block_rlp[payload_start + 1 : payload_start + 1 + lenlen], "big"
            )
        )
    return block_rlp[payload_start : payload_start + header_len]


def _head_watcher(ws_url, poll_interval):
    """Subscribe to newHeads and return a threading.Event set on every new head, or None.

    This is what the ethproofs reference clients do (chain-follow over WS), and it removes the polling
    latency entirely: the loop below waits on the event instead of sleeping a fixed interval.

    Deliberately best-effort. The watcher runs in a daemon thread, reconnects on failure, and the main loop
    passes `poll_interval` as its wait timeout — so if the library is missing, the endpoint refuses, or the
    link drops, the fetcher degrades EXACTLY to its previous polling behaviour instead of stalling.
    Needs a WebSocket client: `pip install websocket-client`.
    """
    try:
        import websocket  # websocket-client, sync API
    except ImportError:
        log.warning("websocket-client not installed — falling back to polling every %ss "
                    "(pip install websocket-client to get newHeads notifications)", poll_interval)
        return None

    event = threading.Event()

    def run():
        sub = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                          "params": ["newHeads"]})
        while True:
            try:
                ws = websocket.create_connection(ws_url, timeout=30)
                ws.send(sub)
                log.info("subscribed to newHeads at %s", ws_url)
                while True:
                    ws.recv()          # any frame means the head moved (or the subscription ack)
                    event.set()
            except Exception as e:     # noqa: BLE001 — never take the fetcher down with it
                log.warning("newHeads subscription dropped (%s) — retrying in 5s, polling meanwhile", e)
                event.set()            # do not leave the loop waiting on a dead subscription
                time.sleep(5)

    threading.Thread(target=run, daemon=True).start()
    return event


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()

    def call(self, payload):
        for attempt in range(5):
            try:
                r = self.session.post(self.url, json=payload, timeout=60)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, json.JSONDecodeError) as e:
                wait = min(2**attempt, 30)
                log.warning("rpc error (%s), retrying in %ss", e, wait)
                time.sleep(wait)
        raise RuntimeError(f"rpc failed after retries: {self.url}")

    def one(self, method, params):
        resp = self.call({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
        if "error" in resp:
            raise RuntimeError(f"{method}{params}: {resp['error']}")
        return resp["result"]

    def finalized_number(self) -> int:
        return self.head_number("finalized")

    def head_number(self, tag: str) -> int:
        """Height of the chosen head tag. `finalized` moves an epoch at a time (32 blocks); `latest`
        moves one block per slot, which is what block-by-block witness production needs."""
        blk = self.one("eth_getBlockByNumber", [tag, False])
        return int(blk["number"], 16)

    def fetch_block(self, n: int) -> bytes:
        """Fetch raw block n and verify its header hash against the canonical
        chain. Returns the raw RLP bytes."""
        batch = [
            {"jsonrpc": "2.0", "method": "debug_getRawBlock", "params": [hex(n)], "id": 1},
            {"jsonrpc": "2.0", "method": "eth_getBlockByNumber", "params": [hex(n), False], "id": 2},
        ]
        by_id = {r["id"]: r for r in self.call(batch)}
        for r in by_id.values():
            if "error" in r:
                raise RuntimeError(f"block {n}: {r['error']}")
        raw = bytes.fromhex(by_id[1]["result"][2:])
        canonical = by_id[2]["result"]["hash"]
        got = "0x" + keccak256(header_span(raw)).hex()
        if got != canonical:
            raise RuntimeError(
                f"block {n}: header hash {got} != canonical {canonical}"
            )
        return raw


def existing_max_block(block_db: str) -> int:
    """Highest block number present (flat files and NM/ shard dirs)."""
    best = -1
    for entry in os.listdir(block_db):
        path = os.path.join(block_db, entry)
        if entry.isdigit():
            best = max(best, int(entry))
        elif re.fullmatch(r"\d+M", entry) and os.path.isdir(path):
            nums = [int(f) for f in os.listdir(path) if f.isdigit()]
            if nums:
                best = max(best, max(nums))
    return best


def write_block(block_db: str, n: int, raw: bytes, quality: int) -> None:
    data = brotli.compress(raw, quality=quality)
    tmp = os.path.join(block_db, f".tmp.{n}")
    with open(tmp, "wb") as f:
        f.write(data)
    os.rename(tmp, os.path.join(block_db, str(n)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--block-db", required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--brotli-quality", type=int, default=5)
    ap.add_argument("--poll-interval", type=float, default=6.0)
    ap.add_argument(
        "--head-tag", default="finalized", choices=["latest", "safe", "finalized"],
        help="which head to follow (default finalized: safe, but advances 32 blocks at a time)",
    )
    ap.add_argument(
        "--confirmations", type=int, default=0,
        help="stay this many blocks behind --head-tag; 2 avoids single-slot reorgs at a 24s cost",
    )
    ap.add_argument(
        "--ws-url", default=None,
        help="newHeads WebSocket endpoint; default = --rpc with http -> ws (the same port serves both on "
             "geth/reth when --ws is enabled). Set --ws-url '' to force plain polling.",
    )
    ap.add_argument(
        "--start", type=int, default=None,
        help="first block to fetch (default: max existing block + 1)",
    )
    ap.add_argument(
        "--count", type=int, default=None,
        help="fetch only this many blocks, then exit (default: run forever)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    rpc = Rpc(args.rpc)
    ws_url = args.ws_url
    if ws_url is None:
        ws_url = re.sub(r"^http", "ws", args.rpc)
    head_event = _head_watcher(ws_url, args.poll_interval) if ws_url else None
    next_block = args.start
    if next_block is None:
        next_block = existing_max_block(args.block_db) + 1
        if next_block == 0:
            log.error("empty block_db and no --start given")
            sys.exit(1)
    last_block = None
    if args.count is not None:
        last_block = next_block + args.count - 1
        log.info("fetching blocks %d..%d", next_block, last_block)
    else:
        log.info("starting fetch at block %d", next_block)

    last_report = time.monotonic()
    fetched_since_report = 0
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        while True:
            if last_block is not None and next_block > last_block:
                log.info("fetched requested range, exiting")
                return
            target = rpc.head_number(args.head_tag) - args.confirmations
            if last_block is not None:
                target = min(target, last_block)
            if target < next_block:
                # Notified, not polled: wait for the next head, with poll_interval as the timeout so a
                # missing/dead subscription degrades to exactly the old behaviour.
                if head_event is not None:
                    head_event.wait(args.poll_interval)
                    head_event.clear()
                else:
                    time.sleep(args.poll_interval)
                continue
            # Bounded window keeps memory flat and ordering simple.
            end = min(target, next_block + 4096 - 1)
            blocks = range(next_block, end + 1)
            for n, raw in zip(blocks, pool.map(rpc.fetch_block, blocks)):
                write_block(args.block_db, n, raw, args.brotli_quality)
            next_block = end + 1
            fetched_since_report += len(blocks)
            now = time.monotonic()
            if now - last_report > 10:
                rate = fetched_since_report / (now - last_report)
                log.info(
                    "fetched up to %d (%s-%d %d, gap %d, %.0f blocks/s)",
                    end, args.head_tag, args.confirmations, target, target - end, rate,
                )
                last_report = now
                fetched_since_report = 0


if __name__ == "__main__":
    main()
