# monad-witness — the witness seam, on a Monad node

The **producer** side of the RTP pipeline: a Monad execution node replaying **Ethereum mainnet** blocks
and dumping, per block, the zkVM witness the Monad guest consumes. It is the home-made stand-in for the
"co-located reth node" seam of [`cli/ethproofs-pipeline.md`](../../cli/ethproofs-pipeline.md) — except it
feeds the **Monad** guest, not reth, so there is no other way to get these witnesses.

Everything here runs on the devcore box (`nyc-003`). Two of the three long-running pieces are **not** in
this repo — they live in the monad tree (branch `sam/osaka_witness_gen`) and are driven, not
reimplemented:

| piece | what | whose |
|---|---|---|
| `fetch_blocks.py` | RPC → `block_db` (raw blocks, verified against the canonical hash), follows forever | monad tree |
| `run_replay.py` | `block_db` → triedb, `--zkvm-witness <dir>` dumps a witness + post-state root per block | monad tree |
| **`witness-tap`** | witnesses → **queue + manifest + heartbeat**, and prunes what is proved | here |
| `witness-profile` | benchmark-only: a block's deterministic work-unit (steps). **Not part of the RTP pipeline** — see below | here |
| **`witness-follow`** | brings them up per phase, and reports where they are | here |
| **`witness-sim`** | rehearse the whole pipeline from an existing witness corpus, no node, no GPU | here |
| **`patch-fetch-blocks.py`** | teaches the fetcher `--head-tag latest --confirmations N` → block-by-block instead of epoch bursts | here |

```
chi-001 RPC ─► block_db ─► [monad replay + triedb] ─► ~/witnesses/<block>.witness
                                                                │
                                          witness-tap  ─────────┤
                                            ├─ frame    (gen-inputs --src-dir, no staging copy)
                                            ├─ prune    (drop what is proved, keep what is interesting)
                                            ├─ manifest (timestamps)
                                            └─ heartbeat → POST /status
                                                                ▼
              guests/monad-zisk/fixtures/1-<block>.bin
                     = the queue cli/prove-farm drains
```

## Box prerequisites

Validated 2026-07-29 on nyc-003. The first three are hard blockers — the DB will not even open otherwise.

- **memlock** — monad hard-asserts `!mlock(...)` in `HugeMem`; the default 8 MB hard limit is far too low
  (`ulimit -Hl` should be GBs). Hugepages must be configured too.
- **triedb sized to the FILESYSTEM, not to wishful thinking.** The DB self-regulates as a *fraction of
  its own capacity*: slow-list compaction starts at `disk_usage > 0.6`, history shrinks above `0.8`, and
  below `0.6` it *raises* history back to the max — so a manual `monad-mpt --reset-history-length N` is
  reverted within seconds of the writer restarting. Meanwhile Ethereum replay grows the DB **~6.5 MB per
  block** with no expiry of State/Code (`OnDiskMachine::auto_expire()` is true only for the
  TxHash/BlockHash tables). Consequence: with a 1 TB file on a 913 GB filesystem, the **fs fills at
  ~625 GB of triedb, long before the DB's own 0.6/0.8 defenses ever fire**. Size it so `0.8 × capacity`
  still fits alongside everything else — on nyc-003 (~245 GB used outside the triedb) that is **600 G**:
  ```sh
  truncate -s 600G ~/triedb.db
  monad-mpt --storage ~/triedb.db --create --state-machine ethereum
  ```
  `MIN_HISTORY_LENGTH` is 300, comfortably above the 256-block BLOCKHASH window, so witness dumping
  survives maximum history pressure.
- **snapshot** — `monad-cli --db ~/triedb.db --version <V> --load-binary-snapshot <parent-dir>` (the tool
  appends `/<V>`, which must exist). ~66 min for ~350 GB; it lands the state for `V-255 .. V`.
- **block_db, including the 256-block ancestor window** — `dump_witness` reads ancestor headers and
  crashes without them (`Could not query ancestor header N-1 from blockdb`). Fetch from `snapshot-255`,
  not from `snapshot+1`. A non-archive RPC is fine here: only *state* was pruned, raw blocks are always
  served.
- **pyenv** — a fresh tmux window has no pyenv in `PATH`; use the absolute interpreter (the `PY` knob).

## The two phases

They have opposite needs, so they are separate runs:

```sh
./witness-follow plan  catchup     # print the commands, start nothing
./witness-follow start catchup     # fetch + replay, WITHOUT --zkvm-witness
./witness-follow status
./witness-follow start tip         # fetch + replay WITH witnesses + the tap
```

**catch-up** — `--zkvm-witness` forces the interpreter (~4.6 vs ~8 blocks/s) *and* would dump ~7 MB for
every block of the backlog: tens of thousands of blocks, hundreds of GB, all of it useless. So the
backlog is replayed with the flag **off**. Measured on nyc-003: **7.1–9.0 blocks/s** (~8 avg) with
compaction active, i.e. ~3 h for ~87 k blocks.

**tip** — small batches (`MIN_FOLLOW_BATCH=1`, one monad restart per block) to keep the lag at one block,
witnesses on, and the tap running. This is the RTP producer.

Stop cleanly with `./witness-follow stop`: it SIGTERMs `run_replay.py`, which forwards to monad and exits
**without** writing an `ALERT` (monad commits per block, so a mid-batch stop is safe and resumable). An
`ALERT` file means a batch genuinely *failed* — read it, fix the cause, then remove it. Never clear it
blindly; `witness-follow start` refuses to run while it exists.

## Cadence: every block, because the target is real-time

`QUEUE_MODULUS=1` by default — **every block, as it arrives**. That is what real-time proving means; the
÷100 sampling of an ethproofs benchmark cluster is a different, cheaper mode (`QUEUE_MODULUS=100`).

How close the prover can get is now measurable rather than guessed. Four real monad witnesses emulated to
**182–311 Msteps**, and this repo's own 16×5090 numbers solve to `secs ≈ 3.8 + steps/28.3e6` (479 Msteps →
20.7 s; ~14 Msteps/s at 74–105 M vs 22–24 at 410–479 M). That puts these blocks at **10–15 s**, and a p99
witness (~13.9 MB, ~600 Msteps) at ~25 s — i.e. the prover **straddles the 12 s slot** rather than being
hopelessly behind, as an earlier extrapolation from zisk-reth's largest block suggested.

So `cli/prove-farm --newest-first` always proves the freshest queued block and never returns for older
ones, and the **fraction actually proved (coverage)** is a result in its own right — reported by
[`profiling/rtp-latency.py`](../../profiling/rtp-latency.py) next to the per-block latency. A 2 s latency on
10 % of blocks is not real-time proving, and the report states both numbers. Expect the proved set to be
non-contiguous: at the slot boundary the prover falls behind, then jumps to the newest and leaves a hole.

The framed input is written straight from the dump dir (`gen-inputs --src-dir`) — no in-repo staging copy.
At the RTP cadence, staging would double disk and I/O for nothing.

## Retention: the raw witness dies at framing

At one block per 12 s × ~7 MB the dump dir grows **~2 GB/h** and the replay never cleans up. The tap
therefore deletes each raw witness **the moment it is framed**, because it is strictly redundant: the framed
input is `LE64(len) + witness + pad`, so it *contains* the witness verbatim and the original is recoverable
by dropping 8 bytes. Keeping both meant holding 14 MB per block across two directories for nothing.

So the dump dir drains every cycle — watch `raw=` in the tap's log line, it should sit at **0–1**; anything
piling up there means framing is failing or the replay is outrunning the tap. A witness younger than
`SETTLE_SECS` (2 s) is left alone: framing a half-written one used to be recoverable from the raw copy, and
no longer is.

Everything below therefore acts on the **framed input**, driven by `cli/prove-farm.csv` (the same record
`prove-farm`'s own `is_proven` reads) rather than by a blind ring. A block is kept while it is still
*interesting*:

| state | kept? | why |
|---|---|---|
| proved ok | **dropped** | the proof is the artefact now |
| proof FAILED | kept | exactly what needs re-proving or debugging |
| never proved, < `SKIP_AFTER` behind | kept | still a candidate |
| never proved, ≥ `SKIP_AFTER` behind | **dropped**, counted as *abandoned* | `--newest-first` will never come back for it; the abandoned count **is** the coverage gap, so it is logged |
| `block % KEEP_SAMPLE == 0` | kept forever | the reproducible corpus this repo's cross-zkVM comparison needs |
| witness ≥ `KEEP_BIG_MB`, or proof ≥ `KEEP_SLOW_SECS` | kept | the heavy blocks are the ones worth profiling |
| newest `KEEP_RECENT` | always kept | nothing in flight is pulled from under a prover |

The `big` rule needs the witness size *after* the raw file is gone, so it falls back to the framed input's
size minus its 8-byte prefix. Without that, an outlier would only be recognised on the cycle that framed it
and would be dropped the moment it was proved.

Each cycle logs the breakdown, e.g. `dropped=18 kept[big=1 proof-failed=1 recent=5 sample=1 slow=1
unproved=3] queue=12 raw=0` — so what the box is keeping, and why, is never a mystery.

The tap is the **only** piece of the whole pipeline that removes files, so: it refuses to touch anything
outside `WITNESS_DIR` or not named `<digits>.{witness,post_state_root}`, and `--dry-run` previews without
deleting *or recording* (a recorded block is never reprocessed, so a dry run that wrote the manifest would
silently skip the next real promotion).

## Work-units are a benchmark quantity, not an RTP one

`./witness-profile` executes a block on the CPU and writes `<queue>/<chain>-<block>.exec-report.json`
carrying its **deterministic work-unit** (ZisK steps) — the number this repo compares across zkVMs.

It is deliberately **not** wired into `witness-follow start tip`. The real-time pipeline needs a witness,
a proof and timestamps; it never reads a step count. Producing one live would mean a third long-running
process, more failure surface, and 7 MB reads competing with the replay's triedb writes — for data nobody
consumes there. Run it by hand, on a curated corpus, off the live path:

```sh
REPO=~/zkvm-bench ./witness-profile --once        # needs ziskemu on this box (ziskup)
```

The one place emulation legitimately belongs in a live run is inside the **mock** prover
(`cli/prove-farm --mock-exec-fallback`): it needs a duration, deriving it from steps is the only defensible
model, and the emulation happens exactly where a real prover would be working — inside the fiction, with
its cost deducted from the simulated sleep. On a real cluster it disappears. There is always headroom for
that deduction: the emulator runs ~150 Msteps/s against a modelled prover at ~28 Msteps/s.

Anything the mock leaves behind is a side effect, not a pipeline output. `rtp-latency.py --queue` will
happily read those reports for its `work` column if they exist, and leave it blank if they don't.

## Known gaps

- **Block-by-block vs epoch bursts — half closed.** Measured on nyc-003: following `finalized` makes
  witnesses arrive in bursts of **exactly 32 blocks** (one epoch) every ~6 min, **~13–19 min** behind the
  head (observed lags: 66, 71, 89 blocks). That delay dominates every other number in the pipeline, and
  `--min-follow-batch 1` cannot touch it — the granularity comes from the *fetcher*, not the replay:
  `run_replay.py` executes `contiguous_head(block_db)`, i.e. whatever has been written, so it needs no
  change at all.

  `./patch-fetch-blocks.py` adds `--head-tag latest --confirmations 2` to the monad tree's fetcher
  (idempotent, revertible, refuses to apply if upstream moved), which gives **one block per slot at a
  ~24 s lag** instead of ~18 min. `witness-follow` passes it through as `HEAD_TAG` / `CONFIRMATIONS`.

  What is **not** done is reorg recovery. Off `finalized`, a fetched block can leave the canonical chain;
  the fetcher hash-verifies at fetch time so nothing wrong is written silently, but the next block's parent
  then mismatches, monad aborts and `run_replay.py` leaves an `ALERT`. Recovery is `monad-mpt --rewind-to
  <n>` plus a re-fetch of the divergent range, and it is manual. Two confirmations avoid the single-slot
  reorgs that make up almost all of them; deeper ones will stop the producer until someone rewinds.

  Cosmetic side effect: once execution passes finality, `status.json`'s `gap_to_finalized` goes negative
  (`run_replay.py` compares against the `finalized` tag). Harmless — the batch gate is `max(1, …)`, so a
  negative gap simply means "execute as soon as one block is ready".
- **triedb growth is only bounded by the DB's own thresholds.** ~6.5 MB/block until compaction and the
  0.8 history shrink take over. Sizing the file to the fs is what makes those thresholds meaningful — if
  a long catch-up still walks up to the capacity, that is a question for the monad side, not a knob here.
- **Witnesses are not regenerable from this repo alone** — they need the node, the snapshot and the
  block_db. `cli/gen-witness --guest monad-*` still errors "pre-supplied"; wiring it to this producer is
  the next step.

## Deploying

The box has a clone of this repo (`~/zkvm-bench`); `git pull` it. The Monad ELFs are versioned, so the
clone carries everything the queue and the prover driver need — no build on the box.

```sh
cd ~/zkvm-bench && git pull && cd infra/monad-witness && ./witness-follow status
```
