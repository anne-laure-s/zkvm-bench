# Monad guest — execution & cross-zkVM comparison

Monad block-replay guests, and a comparison of **execution** times (no
proving) against the reth clients on SP1 / ZisK / OpenVM, over the common blocks.

> **Which tool does what** (the commands below are the *low-level* runners):
> - *Where* the cost goes + side-by-side guest comparison → [`profiling/hotspots.py`](../../profiling/README.md)
>   (`profile --backend sp1|zisk` wraps these same runners; adds `diff` / `compare` / `--aggregate`).
>   It's how you execute/profile Monad **on SP1** — there is no SP1 `ev.sh`.
> - `ev.sh` (here) = batch execute + state-root verify, **ZisK only** → `exec-verified.csv` (steps).
> - `gen-inputs` (in `guests/monad-*/`) = persist per-zkVM inputs for **proving** via `cli/prove-farm` —
>   **not** needed for execution/profiling/comparison.
> - Input framing per backend: **SP1 = raw `.witness`**, **ZisK = `LE64(len)+witness+pad8`**.
> - New Monad witnesses land in `guests/monad/fixtures/<block>.witness` (+ `.post_state_root`);
>   how they're generated (Monad snapshot + block_db + `run_replay.py`) is in "Witnesses — provenance" below.

> ⚠️ All commands run from the repo root.
> The binaries are **not** on `PATH` → use their full paths.

## Contents

- `guests/monad/monad-zkvm-guest-sp1.elf` — Monad guest for SP1 ✅ works
- `guests/monad/monad-zkvm-guest-zisk.elf` — Monad guest for ZisK ✅ works
  (correctly commits the state root, unlike the old guest that wrote zero)
- `guests/monad/inputs/1-<block>.witness` — inputs (12 blocks) + `1-<block>.post_state_root` (expected root)
- `guests/monad/ev.sh` — runs each witness on the ZisK guest and **verifies the state root**
  (one line per block: steps, output size, PASS/MISMATCH verdict). Runs from anywhere.
- `guests/monad/execute-out/` — `ev.sh` outputs (framed `1-<block>.bin` · `.out` · `.log`)
- `guests/monad/exec-verified.csv` — steps + root-verification recap (corrected guest)

## Layout: one directory per generation

A **generation** is a guest lineage plus the witness wire format it reads. The two change together
and are not interchangeable — `PartialTrieDb::from_witness` reads an RLP list of node preimages,
`OffsetTrieDb` reads an offset blob — while the witness **filenames are identical across formats**.
Mixing two sets is therefore invisible in a directory listing, which is why they never share a
directory.

```
gen/<generation>/
  witnesses/            <block>.witness + <block>.post_state_root   (git-ignored, GBs)
                        + <block>.expected_pv — OPTIONAL, see below
  elf/                  the two ELFs that read that format          (git-ignored)
  PROVENANCE.md         branch, builder, ELF sha256, measured format, counts — TRACKED
current   -> gen/<generation>                 selects the generation (git-ignored, local state)
fixtures  -> current/witnesses                the path the tooling globs (git-ignored)
monad-zkvm-guest-{zisk,sp1}.elf               the current ELFs — TRACKED, what produced the numbers
```

`fixtures/` is the stable path: `profiling/compare.py` and `infra/monad-openvm/segments.sh` glob it
and need no change when the generation switches. Switching is one command:

```sh
./use-gen                            # list generations, mark the current one, show each format
./use-gen offsettriedb-rework-2026-08   # select it
```

`use-gen` **refuses** a generation whose witnesses are not one format throughout (it samples the
first, middle and last block), refuses one with no ELFs, and prints the ELF hashes it installed.
Since the top-level ELFs are tracked, a switch shows up as a modification to commit — deliberately:
the ELF is what produced the numbers.

Only `PROVENANCE.md` is committed. It is the sole record of which branch and which ELF a set of
figures came from, and it states the witness format as **measured** (the first four bytes of field
[1]: `4d5a5701` = `MZW\x01` for the offset format, an RLP list header for the old one) rather than
inferred from a directory name.

### `.expected_pv` — optional, and not needed to run anything

A witness and its ELF are all it takes to **execute** a block: that is what `profiling/compare.py`,
`profiling/hotspots.py` and the two runners do, and none of them opens a root file. Verification is a
separate concern, and it has two levels:

| present in the set | `ev.sh` verdict | checks |
|---|---|---|
| `.post_state_root` only | `PASS` | the post root appears in the output (substring) |
| `+ .expected_pv` (96 B, `post \|\| pre \|\| hash`) | `PASS(pv3)` | all three public values, positionally |

`witness-backfill` writes the first and not the second, so a freshly produced generation reports `PASS`
— that is a complete answer to "does this build read this generation", not a degraded one. To get the
stronger verdict, generate them over the whole witness directory (it derives `pre` from each block's
parent and cross-checks `hash` against the next block's `parent_hash`, so it needs the full set):

```sh
./gen-expected-pv.py gen/<generation>/witnesses     # --check-only reports without writing
```

Expect one block short of the set: the first has no local parent post-root.

## Witnesses — provenance (git-ignored)

**Two locations**, and which one a tool reads is not a detail — they hold different sets:

- `fixtures/<block>.witness` (+ `.post_state_root`) — a **symlink to the selected generation**
  (`current/witnesses`, see above). This is the working set: `profiling/compare.py` looks here
  **first**, `ev.sh` and `infra/monad-openvm/segments.sh` read it exclusively, and
  `guests/monad-*/gen-inputs --from fixtures` sources it. Switch generation and every one of them
  follows, with no path to change.
- `inputs/1-<block>.witness` (+ `.post_state_root`) — an older set, batch-tagged `1-<block>`, that
  predates the generation split. It is `compare.py`'s **second** lookup and the default source of
  `gen-inputs`. It covers a different, much smaller block range than the current generation, so a
  block present in both can resolve to two different witnesses depending on which tool asks.

The `1-<block>.witness` files are **pre-generated from a Monad node** (the Monad block-replay input) and
are **not** regenerable through this repo — `cli/gen-witness --guest monad-*` deliberately errors
("pre-supplied"). They are **git-ignored** (see the root `.gitignore`), so a fresh clone does **not**
contain them and `ev.sh` cannot run until they are copied in. The whole
`inputs/` dir — witnesses **and** their `1-<block>.post_state_root` roots — is git-ignored, and so is
the `exec-verified.csv` recap (`ev.sh` regenerates it).

- **Blocks:** these 12 are what `inputs/` holds — `25224730`–`25224739` (Monad-only) plus `25229951`
  and `25229957` (common with the reth guests). They are **not** the corpus the reports use: that is
  the selected generation under `fixtures/` (see above), currently 504 blocks. `inputs/` predates the
  generation split and stays as `compare.py`'s second lookup.
- **To obtain a corpus:** the procedure is `infra/monad-witness/witness-backfill`, which replays a
  branch on the devcore box and files the result as a **generation** (witnesses + both ELFs +
  `PROVENANCE.md`) — see [`profiling/RUNBOOK.md` § Compare two versions of the guest, A to
  Z](../../profiling/RUNBOOK.md#compare-two-versions-of-the-guest-a-to-z). That is the only supported
  path, and what every current report was produced from.
- **The 12 blocks in `inputs/` predate it** and are the exception, not the model: they were hand-copied
  before the generation split, following the internal Notion doc [*Running a witness through the zkVM
  guest (x86 · ZisK · SP1)*](https://app.notion.com/p/Running-a-witness-through-the-zkVM-guest-x86-ZisK-SP1-37475b0ba84081869a5ee686cbad7899).
  To reproduce that set specifically: drop each `1-<block>.witness` **and** its
  `1-<block>.post_state_root` into `guests/monad/inputs/`, then run `guests/monad/ev.sh`. Do **not**
  use this to grow a corpus — a set landing outside a generation is the sign that a generation is
  missing.

## Binaries used

| stack | binary |
|---|---|
| SP1 (Monad, RSP) | `infra/sp1-infra/sp1-runner/target/release/sp1-runner` |
| ZisK | `~/.zisk/bin/ziskemu` |
| OpenVM | `vendor/openvm-eth/target/release/openvm-reth-benchmark` |

## Execution commands (`execute` mode, CPU, no proof)

Example with block `25229951` (replace with `25229957` or another).

### 1) Monad (Monad engine, on SP1)
```bash
SP1_PROVER=cpu infra/sp1-infra/sp1-runner/target/release/sp1-runner --mode execute \
  --elf   guests/monad/monad-zkvm-guest-sp1.elf \
  --input guests/monad/inputs/1-25229951.witness \
  --public-values /tmp/monad-25229951.pv \
  --report /tmp/monad-25229951.json
```
> For an **optimal execute timing**, add `--no-gas`: SP1 skips its gas-estimation
> pass (1.5× to 2.4× faster). Trade-off: the report then has `cycles=0` / `gas=None`.
> Since cycles are deterministic, do one gas-on run for the counters and one
> `--no-gas` run for the time.

### 2) RSP (reth, on SP1)
```bash
SP1_PROVER=cpu infra/sp1-infra/sp1-runner/target/release/sp1-runner --mode execute \
  --elf   guests/rsp/rsp.elf \
  --input guests/rsp/inputs/1-25229951.bin \
  --report /tmp/rsp-25229951.json
```

### 3a) Monad (Monad engine, on ZisK)
The input must be framed `LE64(len)+witness+pad8` (the raw `.witness` is not enough):
```bash
python3 - guests/monad/inputs/1-25229951.witness /tmp/monad-zisk-25229951.bin <<'PY'
import sys,struct
d=open(sys.argv[1],'rb').read(); n=len(d); pad=(-(8+n))%8
open(sys.argv[2],'wb').write(struct.pack('<Q',n)+d+b'\x00'*pad)
PY
~/.zisk/bin/ziskemu -m \
  -e guests/monad/monad-zkvm-guest-zisk.elf \
  -i /tmp/monad-zisk-25229951.bin \
  -o /tmp/monad-zisk-25229951.out
```

### 3b) ZisK-reth (reth, on ZisK)
```bash
~/.zisk/bin/ziskemu -m \
  -e guests/zisk-reth/zisk-reth.elf \
  -i guests/zisk-reth/inputs/1-25229951.bin \
  -o /tmp/zisk-25229951.out
```

### 4) OpenVM-reth (reth, on OpenVM)
```bash
vendor/openvm-eth/target/release/openvm-reth-benchmark --mode execute \
  --block-number 25229951 --chain-id 1 \
  --cache-dir guests/openvm-reth/inputs/rpc-cache
```

### Notes
- **`SP1_PROVER=cpu` is mandatory** for SP1 (otherwise the runner tries CUDA and fails on a Mac).
- **Monad / RSP**: `--public-values <file>` to write the output (the state root);
  `--report <file>` for the JSON (cycles, elapsed, gas).
- **ZisK**: `-o <file>` writes the output, `-m` prints the metrics (steps/duration).
- **OpenVM**: reads the witness from `--cache-dir` (offline). `input.json` is NOT the cache format.
  Without the cache, you'd need `--rpc-url` (archive-node access).
- The SP1 Monad guest's input is the **raw** `.witness` (the runner does `SP1Stdin::write_slice`);
  on the ZisK side the `.bin` input is the framed witness `LE64(len)+witness+pad8`.

## Monad program output

The block's **post-execution state root** (root of the Ethereum state trie after
applying the block). Verified == `guests/monad/inputs/1-<block>.post_state_root`.
- SP1: output = 32 bytes (the exact root).
- ZisK: output = 256 bytes, the root in the **first 32**, the rest zero.

## Common blocks (intersection)

Only **`25229951` and `25229957`** are common to both the Monad witnesses AND the
pre-generated reth inputs (RSP / ZisK / OpenVM). The range `25224730`–`25224739` is Monad-only.

## Results — execution time (M5 Max, execute-only)

The "internal work" counters are **deterministic** (identical from one machine to
another); only `elapsed` depends on the host CPU. Units are **not comparable across zkVMs**
(SP1 cycles ≠ ZisK steps ≠ OpenVM instructions).

⚠️ **SP1 is measured with `--no-gas`** (gas-estimation pass off), otherwise SP1 is
1.5× to 2.4× slower — a stats overhead no other stack has. The `cycles` come from a
gas-on run (deterministic), the `elapsed` from a `--no-gas` run.
Gas on→off measured: Monad 951 10.3→6.9 s · RSP 951 9.9→6.5 s · Monad 957 4.1→1.7 s · RSP 957 3.3→1.4 s.

### Block 25229951
| stack | zkVM | client | internal work | elapsed (M5 Max) |
|---|---|---|---|---|
| Monad | SP1 | monad | 1 038 607 442 cyc | 6.91 s |
| RSP | SP1 | reth | 894 275 593 cyc | 6.46 s |
| Monad | ZisK | monad | 564 671 269 steps | 3.60 s |
| ZisK | ZisK | reth | 413 614 075 steps | 2.89 s |
| OpenVM | OpenVM | reth | 521 864 701 insn | 2.53 s |

### Block 25229957
| stack | zkVM | client | internal work | elapsed (M5 Max) |
|---|---|---|---|---|
| Monad | SP1 | monad | 242 927 736 cyc | 1.68 s |
| RSP | SP1 | reth | 180 407 799 cyc | 1.44 s |
| Monad | ZisK | monad | 119 996 824 steps | 0.77 s |
| ZisK | ZisK | reth | 78 779 780 steps | 0.57 s |
| OpenVM | OpenVM | reth | 92 538 157 insn | 0.91 s |

### Findings
- **Monad vs reth, at equal zkVM** (internal work directly comparable): the Monad
  engine consistently generates more trace than reth.
  - on **SP1**: +16% (block 951), +35% (block 957)
  - on **ZisK**: +37% (block 951), +52% (block 957)
- **SP1 ↔ ZisK parity on the Monad side**: both guests commit the **same correct state root**
  (the old ZisK guest wrote zero — fixed).
- **Between zkVMs** (same HW, SP1 in `--no-gas`): ZisK & OpenVM stay **~2–2.5× faster** in
  wall-clock than the SP1 executor (the gap drops from ~4× once SP1's gas is off).
- A **Monad-on-OpenVM** guest is still missing to fully close the matrix.
- These are **execution** times; **proving** (GPU) would rank them differently.
