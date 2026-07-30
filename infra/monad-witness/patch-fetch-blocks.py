#!/usr/bin/env python3
"""patch-fetch-blocks.py — teach the monad tree's fetch_blocks.py to follow a chosen head tag, so the
witness producer can advance BLOCK BY BLOCK instead of one epoch at a time.

Why this exists. `fetch_blocks.py` fetches up to the `finalized` tag, and `finalized` advances by whole
epochs — so it writes 32 blocks at once, the replay executes 32 at once, and witnesses arrive in bursts
~6 min apart with a ~13-19 min lag. That lag dominates every other number in the RTP pipeline, and no
setting on our side touches it. Meanwhile `run_replay.py` needs no change at all: it executes
`contiguous_head(block_db)` — whatever the fetcher has written — so the arrival granularity is entirely a
fetcher-side decision.

What it adds:
  --head-tag <tag>        latest | safe | finalized   (default: finalized — unchanged behaviour)
  --confirmations <n>     stay n blocks behind that tag (default: 0)

`--head-tag latest --confirmations 2` gives one block per slot with a ~24 s lag instead of ~18 min, while
staying clear of the single-slot reorgs that make up almost all reorgs.

REORGS. With any tag other than `finalized` a fetched block can later leave the canonical chain. The
fetcher already hash-verifies each block against the canonical chain AT FETCH TIME, so nothing silently
wrong is written; but if a reorg happens afterwards, the next block's parent no longer matches, monad
aborts the batch and `run_replay.py` leaves an ALERT. Recovery is `monad-mpt --rewind-to <n>` plus a
re-fetch of the divergent range, and it is NOT automated — that, not the tag, is the real remaining work.

The patch is applied by exact-string replacement, is idempotent, and refuses to guess: if the expected
code is not found (Sam's branch moved), it says so and changes nothing.

  ./patch-fetch-blocks.py [--file ~/monad/fetch_blocks.py] [--check] [--revert]
"""
import argparse, os, shutil, sys

MARKER = "def head_number(self, tag: str) -> int:"   # present iff the patch is applied

EDITS = [
    # 1. Generalise the head lookup.
    ('''    def finalized_number(self) -> int:
        blk = self.one("eth_getBlockByNumber", ["finalized", False])
        return int(blk["number"], 16)''',
     '''    def finalized_number(self) -> int:
        return self.head_number("finalized")

    def head_number(self, tag: str) -> int:
        """Height of the chosen head tag. `finalized` moves an epoch at a time (32 blocks); `latest`
        moves one block per slot, which is what block-by-block witness production needs."""
        blk = self.one("eth_getBlockByNumber", [tag, False])
        return int(blk["number"], 16)'''),

    # 2b. threading, for the watcher thread.
    ("import concurrent.futures", "import concurrent.futures\nimport threading"),

    # 2. The two knobs.
    ('''    ap.add_argument("--poll-interval", type=float, default=6.0)''',
     '''    ap.add_argument("--poll-interval", type=float, default=6.0)
    ap.add_argument(
        "--head-tag", default="finalized", choices=["latest", "safe", "finalized"],
        help="which head to follow (default finalized: safe, but advances 32 blocks at a time)",
    )
    ap.add_argument(
        "--confirmations", type=int, default=0,
        help="stay this many blocks behind --head-tag; 2 avoids single-slot reorgs at a 24s cost",
    )'''),

    # 3. Use them.
    ('''            target = rpc.finalized_number()''',
     '''            target = rpc.head_number(args.head_tag) - args.confirmations'''),

    # 4. Say which head the log line is talking about.
    ('''                    "fetched up to %d (finalized %d, gap %d, %.0f blocks/s)",
                    end, target, target - end, rate,''',
     '''                    "fetched up to %d (%s-%d %d, gap %d, %.0f blocks/s)",
                    end, args.head_tag, args.confirmations, target, target - end, rate,'''),
    # 5. A newHeads watcher, so the fetcher is NOTIFIED instead of polling.
    ('''class Rpc:''',
     '''def _head_watcher(ws_url, poll_interval):
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


class Rpc:'''),

    # 6. The knob, and the derived default.
    ('''    ap.add_argument(
        "--confirmations", type=int, default=0,
        help="stay this many blocks behind --head-tag; 2 avoids single-slot reorgs at a 24s cost",
    )''',
     '''    ap.add_argument(
        "--confirmations", type=int, default=0,
        help="stay this many blocks behind --head-tag; 2 avoids single-slot reorgs at a 24s cost",
    )
    ap.add_argument(
        "--ws-url", default=None,
        help="newHeads WebSocket endpoint; default = --rpc with http -> ws (the same port serves both on "
             "geth/reth when --ws is enabled). Set --ws-url '' to force plain polling.",
    )'''),

    # 7. Wait for a notification instead of sleeping a fixed interval.
    ('''            if target < next_block:
                time.sleep(args.poll_interval)
                continue''',
     '''            if target < next_block:
                # Notified, not polled: wait for the next head, with poll_interval as the timeout so a
                # missing/dead subscription degrades to exactly the old behaviour.
                if head_event is not None:
                    head_event.wait(args.poll_interval)
                    head_event.clear()
                else:
                    time.sleep(args.poll_interval)
                continue'''),

    # 8. Start the watcher once, next to the Rpc client.
    ('''    rpc = Rpc(args.rpc)
    next_block = args.start''',
     '''    rpc = Rpc(args.rpc)
    ws_url = args.ws_url
    if ws_url is None:
        ws_url = re.sub(r"^http", "ws", args.rpc)
    head_event = _head_watcher(ws_url, args.poll_interval) if ws_url else None
    next_block = args.start'''),
]

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=os.path.expanduser("~/monad/fetch_blocks.py"))
    ap.add_argument("--check", action="store_true", help="report whether it is applied; change nothing")
    ap.add_argument("--revert", action="store_true", help="undo the patch")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        sys.exit(f"patch-fetch-blocks: no such file: {a.file}")
    src = open(a.file).read()

    # One unambiguous marker decides the state. Comparing old/new text cannot: edit 2 is additive, so the
    # "old" string survives inside the "new" one and both directions look half-applied.
    patched = MARKER in src
    if a.revert:
        pairs = [(new, old) for old, new in EDITS]
        if not patched:
            print(f"patch-fetch-blocks: already reverted in {a.file}")
            return
        missing = [i for i, (old, _) in enumerate(pairs, 1) if old not in src]
    else:
        # Per-edit, not all-or-nothing: this patch set GREW (the newHeads subscription came later), so a file
        # patched by an older version of this script is partially applied. A single global marker would call
        # it done and silently skip the new edits.
        pairs = [(old, new) for old, new in EDITS if new not in src]
        if not pairs:
            print(f"patch-fetch-blocks: already applied in {a.file}")
            return
        # Check each edit against the text AS IT WILL BE when its turn comes, not against the original:
        # some edits extend what an earlier one inserted (the --ws-url knob grows the --confirmations
        # block), so their source text does not exist in a fresh file yet.
        probe, missing = src, []
        for i, (o, n) in enumerate(pairs, 1):
            if o in probe:
                probe = probe.replace(o, n, 1)
            else:
                missing.append(i)
    if missing:
        print(f"patch-fetch-blocks: cannot {'revert' if a.revert else 'apply'} — edit(s) "
              f"{', '.join(map(str, missing))} of {len(pairs)} do not match this file. The upstream "
              f"script has changed; re-read it and update EDITS rather than forcing anything.",
              file=sys.stderr)
        sys.exit(1)
    if a.check:
        print(f"patch-fetch-blocks: NOT applied ({len(pairs)} edit(s) would apply cleanly)")
        return

    out = src
    for old, new in pairs:
        out = out.replace(old, new, 1)
    backup = a.file + ".orig"
    if not os.path.exists(backup):
        shutil.copyfile(a.file, backup)
        print(f"patch-fetch-blocks: kept the original at {backup}")
    with open(a.file, "w") as f:
        f.write(out)
    print(f"patch-fetch-blocks: {'reverted' if a.revert else 'applied'} {len(pairs)} edit(s) in {a.file}")
    if not a.revert:
        print("Now restart the fetcher with, e.g.:  --head-tag latest --confirmations 2")

if __name__ == "__main__":
    main()
