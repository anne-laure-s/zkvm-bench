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
    pairs = [(new, old) for old, new in EDITS] if a.revert else EDITS
    missing = [i for i, (old, _) in enumerate(pairs, 1) if old not in src]

    if patched != a.revert:      # apply on a patched file, or revert on a clean one
        print(f"patch-fetch-blocks: already {'applied' if patched else 'reverted'} in {a.file}")
        return
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
