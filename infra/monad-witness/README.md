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
| **`witness-profile`** | idle CPU → the block's deterministic work-unit, while the GPU proves (opt-in) | here |
| **`witness-follow`** | brings them up per phase, and reports where they are | here |
| **`witness-sim`** | rehearse the whole pipeline from an existing witness corpus, no node, no GPU | here |

```
chi-001 RPC ─► block_db ─► [monad replay + triedb] ─► ~/witnesses/<block>.witness
                                                                │
                                          witness-tap  ─────────┤
                                            ├─ frame    (gen-inputs --src-dir, no staging copy)
                                            ├─ prune    (drop what is proved, keep what is interesting)
                                            ├─ manifest (timestamps)
                                            └─ heartbeat → POST /status
                                                                ▼
              guests/monad-zisk/fixtures/1-<block>.bin  (+ .exec-report.json from witness-profile)
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

The prover cannot keep up, and that is expected: ~20–70 s per proof against a 12 s slot. `cli/prove-farm
--newest-first` therefore always proves the freshest queued block and never returns for older ones, so
the **fraction of blocks actually proved (coverage)** is a result in its own right — reported by
[`profiling/rtp-latency.py`](../../profiling/rtp-latency.py) next to the per-block latency. A 2 s latency
on 10 % of blocks is not real-time proving, and the report says both numbers.

The framed input is written straight from the dump dir (`gen-inputs --src-dir`) — no in-repo staging copy.
At the RTP cadence, staging would double disk and I/O for nothing.

## Retention: a witness exists to be proved

At one block per 12 s × ~7 MB the dump dir grows **~2 GB/h**, and the replay never cleans up. So the tap
drops a witness once its proof is in hand — retention is driven by `cli/prove-farm.csv` (the same record
`prove-farm`'s own `is_proven` reads), not by a blind ring, and a block is kept while it is still
*interesting*:

| state | kept? | why |
|---|---|---|
| proved ok | **dropped** (raw + framed) | the proof is the artefact now |
| proof FAILED | kept | exactly what needs re-proving or debugging |
| never proved, < `SKIP_AFTER` behind | kept | still a candidate |
| never proved, ≥ `SKIP_AFTER` behind | **dropped**, counted as *abandoned* | `--newest-first` will never come back for it; the abandoned count **is** the coverage gap, so it is logged |
| `block % KEEP_SAMPLE == 0` | kept forever | the reproducible corpus this repo's cross-zkVM comparison needs |
| witness ≥ `KEEP_BIG_MB`, or proof ≥ `KEEP_SLOW_SECS` | kept | the heavy blocks are the ones worth profiling |
| newest `KEEP_RECENT` | always kept | nothing in flight is pulled from under a prover |

`*.exec-report.json` is **never** deleted: a few hundred bytes carrying the block's deterministic
work-unit, long after its 7 MB witness is gone.

Each cycle logs the breakdown, e.g. `dropped=18 kept[big=1 proof-failed=1 recent=5 sample=1 slow=1
unproved=3]` — so what the box is keeping, and why, is never a mystery.

The tap is the **only** piece of the whole pipeline that removes files, so: it refuses to touch anything
outside `WITNESS_DIR` or not named `<digits>.{witness,post_state_root}`, and `--dry-run` previews without
deleting *or recording* (a recorded block is never reprocessed, so a dry run that wrote the manifest would
silently skip the next real promotion).

## Profiling for free, while the GPU proves

`./witness-profile` (opt-in: `PROFILE=1` for `witness-follow`) executes each queued block on this box's
idle CPU and writes `<queue>/<chain>-<block>.exec-report.json`. Proving gives wall-clock; execution gives
the **deterministic work-unit** (ZisK steps) — the number this repo actually compares across zkVMs, and
one that no prove run record carries.

No wiring needed: that path is already what `prove_remote` copies into the run record
([`core.sh`](../zisk-infra/scripts/core.sh) `exec_report`), and `rtp-latency.py --queue` reads it for the
`work` column.

It runs **nice'd** and skips itself whenever the newest queued block is more than `MAX_LAG` behind the
chain head — profiling must never cost the pipeline a block. Doing it here rather than on the GPU box is
deliberate: work-units are deterministic so the machine doesn't change the number, but stealing CPU on the
prover would corrupt the very proving time we are measuring, and shipping 7 MB to the laptop defeats
keeping witnesses on the box.

⚠️ **Needs `ziskemu` on this box** (the ZisK toolchain, via `ziskup`). nyc-003 has only `libziskc.a` /
`riscv2zisk` today, so the profiler exits with that message instead of silently producing nothing. The
deep hotspot profile ([`profiling/hotspots.py`](../../profiling/README.md)) is far heavier and is not part
of this loop — run it on the blocks retention deliberately kept (`big`, `slow`, `proof-failed`), which are
exactly the interesting ones.

## The manifest — what latency is computed from

`witness-manifest.csv`, one row per block. Every stamp except `t_block` is **this box's clock**, on
purpose: pipeline latency must be measured on one clock, never mixed with the laptop's.

| column | meaning |
|---|---|
| `t_block` | the block header's own timestamp (chain time) — cadence blocks only, 1 RPC call each |
| `t_avail` | mtime of the `block_db` file — when the fetcher had the block locally |
| `t_witness` | mtime of the witness — when the replay finished executing the block |
| `t_queued` | when the framed input reached the prover queue |
| `bytes`, `post_state_root`, `promoted` | witness size, the expected root (verification anchor), queued or ring-only |

Proving and submission stamps deliberately live elsewhere (`report.json` from `cli/prove-farm`,
`submissions.jsonl` from `cli/ethproofs-mock`); joining the three is the latency report's job. Reported
as **two separate numbers, never one**: *pipeline latency* (`t_witness` → submitted) and the *finality
delay* (`t_block` → `t_witness`), which is an artefact of following `finalized` and vanishes on `latest`.

The tap also heartbeats `POST /status` to the mock with the **real** chain head (`eth_blockNumber`, i.e.
`latest`) — not the finalized tag it follows — so the dashboard's lag shows the finality delay instead of
hiding it. Point it at the mock with `ETHPROOFS_URL=` (through a reverse SSH tunnel if the mock runs on
your laptop: `ssh -R 8547:localhost:8547 nyc-003`).

## Known gaps

- **We follow `finalized`, ethproofs follows the tip.** Both `fetch_blocks.py` (`finalized_number()`) and
  `run_replay.py` poll the `finalized` tag, so witnesses arrive ~13 min behind the head. Moving to
  `latest` needs reorg handling on both sides — detect that the new head's parent isn't what we executed,
  then `monad-mpt --rewind-to <n>` and re-fetch. The lever exists; the logic doesn't. Until then the
  finality delay is *reported*, not hidden.
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
