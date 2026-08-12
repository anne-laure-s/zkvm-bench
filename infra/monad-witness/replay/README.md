# replay/ — the two orchestrator scripts a backfill runs on the box

`fetch_blocks.py` fills a `block_db` from an RPC; `run_replay.py` replays it through `monad` with
`--zkvm-witness`, dumping `<block>.witness` + `<block>.post_state_root`. `witness-backfill` copies both
to `$MONAD_DIR` and drives them — you do not invoke them by hand.

Two defaults bite if you ever do:

- **`--min-follow-batch 1`** on `run_replay.py`, for any bounded range. The default is 8 and the guard
  is `n_ready < max(1, min(min_follow_batch, gap))`, so a batch ending with fewer than 8 blocks left
  **idles forever without finishing them** — no error, no `ALERT`.
- **There is no end-block flag.** A run stops when the `block_db` runs out, so prune it to the wanted
  range first.

`fetch_blocks.py --head-tag latest --confirmations 2` fetches one block per slot (~24 s behind the
head) instead of one epoch at a time (~13–19 min). It accepts reorg exposure in exchange: a fetched
block can leave the canonical chain, and recovery (`monad-mpt --rewind-to` plus a re-fetch) is manual.
