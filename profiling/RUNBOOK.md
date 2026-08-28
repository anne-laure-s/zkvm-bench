# profiling — runbook

What to type, and what it writes. Every command runs from `profiling/` unless it opens with a `cd`.
For *which tool answers which question*, see [`README.md`](README.md); for the cache, see
[`cache-format.md`](cache-format.md).

Measurements are cached per block and keyed by the content of the ELF and of the input, so re-running
a command costs nothing when nothing changed. A rebuilt guest or a re-minted witness re-measures
itself; there is nothing to invalidate by hand.

---

## Prerequisites

Nothing here is in the repo: every tool and every ELF is built, and a fresh clone has none of them.
`compare.py` skips an axis it cannot run and exits 0, so a missing prerequisite looks like a report
with fewer axes rather than an error.

| needed for | what | where |
|---|---|---|
| any `zisk` axis, `hotspots --backend zisk`, `ev.sh` | **`ziskemu`** | `~/.zisk/bin/ziskemu`, installed by `ziskup`. `--emu` overrides |
| any `sp1` axis | **`sp1-runner`**, plain release build | `infra/sp1-infra/sp1-runner/target/release/sp1-runner`. `--runner` overrides |
| `hotspots --backend sp1`, `compare.py --deep` on an sp1 axis, **and the per-family columns (on by default)** | **`sp1-runner`, profiling build** | same crate, `target-prof/release/` — a *second* build, not the same binary |
| any axis naming `ziskethone` or a Monad `r10` build | **the two guest ELFs**, each with its own patched GCC | next section — they are not `cli/gen-elf --guest zisk-reth`'s business |

Installing them, from a fresh clone — the ZisK emulator first (it is the whole `zisk` side), then the
two `sp1-runner` builds:

```bash
# ZisK — the installer puts ziskup and the toolchain under ~/.zisk
# (same one-liner as infra/zisk-infra/README.md § Setup)
curl https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/ziskup/install.sh | bash
~/.zisk/bin/ziskemu --version                             # what compare.py looks for

# SP1 — two builds of the same crate, and you need both for a full report
cd ../infra/sp1-infra/sp1-runner
cargo build --release                                     # target/release       -> compare.py
cargo build --release --no-default-features \
      --features profiling --target-dir target-prof       # target-prof/release  -> hotspots.py
cd ../../../profiling
```

`--no-default-features` is not optional on the second one: the default `native-gnark` feature builds a
Go library this binary never uses (see [`sp1-runner/README.md`](../infra/sp1-infra/sp1-runner/README.md)).

---

## Building the two ZisK guests

The current headline ZisK comparison is a Monad build against **ziskethone**, upstream's C++ reference guest.
Both sides are built, neither by `cli/gen-elf --guest zisk-reth`, and each has a **different patched
GCC** — upstream's `-mzisk-dma` patch targets 14.3, while the Monad interpreter needs
`__attribute__((musttail))`, which arrived in GCC 15. They are not interchangeable:

| guest | GCC under `$XPACKS_DIR` (default `~/.local/xPacks`) | who builds it for you |
|---|---|---|
| `ziskethone` | `xpack-riscv-none-elf-gcc-14.3.0-1` + `zisk-dma-gcc-14.3.0` | the DMA compiler is built on demand by upstream's own driver; only the xPack is a prerequisite |
| Monad | `zisk-dma-gcc-15.2.0` | nobody — build it with `profiling/experiments/zisk-dma-gcc15/build-gcc15.sh` |

```bash
cd ..
./cli/gen-elf --guest ziskethone -- --check     # build, verify the sha256, install nothing
./cli/gen-elf --guest ziskethone                # ... and install guests/ziskethone/ziskethone.elf

./cli/build-monad --name monad-r10-zisk --branch al/zkvm-r10          # --commit defaults to the tip
./cli/build-monad --name monad-r10-zisk --commit c59b7ab05            # or an exact one
./cli/gen-elf     --guest monad-r10-zisk                              # rebuild the recorded commit
```

**ziskethone is byte-reproducible and the check is an equality.** It embeds no path strings, so a
build in a temp directory yields the same bytes as the shipped one. `guests/ziskethone/ziskethone.build.json`
is the record — the two pins (the `third_party/ziskethone` submodule *and* the `zisk-eth-client`
driver that sets its flags), the two toolchains, the sha256 to reproduce — and the recipe restates
none of it. Editing that file is how you change the build.

> ⚠️ **Upstream ships two builds under one name and the slow one is not a reference.** 
> `guests/ziskethone/ziskethone.elf` is the **shipped** build (`39ad249e2e50ee17`, pin `2bb899a`,
> branch `perf/dma-precompiles`). To obtain the other one deliberately, point `source.upstream_branch` + `commit` at
> `7e6c702` and run `build.sh --check`.

**A Monad guest sets no flag here, by design.** `cli/build-monad` asks `zkvm/guest/CMakeLists.txt` for
its own profile and lets it decide everything:

```
MONAD_ZKVM_CMAKE_DEFINES=MONAD_ZKVM_OFFICIAL_PROFILE=ON;MONAD_ZKVM_GIT_COMMIT=<sha>
```

That profile **forces** its option set into the CMake cache and fails on a contradicting `-D`, on a
compiler that is not GCC 15.2.0, on a non-ZisK target, and on a missing commit stamp. Measured:
passing the seven options explicitly (as the tree's own `zkvm/zisk/build-official.sh` does) yields a
**byte-identical** ELF, so restating them buys nothing. The record is
`guests/<name>/<name>.build.json`, written by the tree's `audit-official-build.py` rather than by
hand, which is why `cli/gen-elf --guest <name>` can rebuild the right commit without being told.

Two things to know before reading a sha:

- **`build-monad` never checks anything out for you.** `vendor/monad` is a working clone parked on
  someone's branch. It looks for a worktree already *clean* on the commit (a warm `target/` makes it
  fast), and otherwise adds a throwaway one — `--workdir` to place it, `--keep` to keep it.
- **The file sha256 is not reproducible across build locations**, though the program is: rustc stamps
  the build path into a `.symtab` symbol name (`zkvm_zisk.<hash>-cgu.0`). Two builds of one commit on
  one machine differed in exactly those 16 bytes. It matters because `profiling/cache` is keyed by
  file sha256, so the same guest built elsewhere is a cache **miss**, not a wrong build. Compare the
  loadable image (`objcopy -O binary`) when you need to know whether two ELFs are the same program.

**Building both guests is not enough to run the axis** — each side also needs an input for every
block, and they are different artifacts from different producers: the Monad side reads
`guests/monad/fixtures/<block>.witness` (a generation, minted on the devcore box), the ziskethone
side reads `guests/ziskethone/fixtures/1-<block>.bin` (a ZEG0 container, transcoded or proxied). Both
are in *Witnesses* below. A block missing on either side shortens the axis without a word.

**Only `al/zkvm-r10` and later build this way.** `MONAD_ZKVM_OFFICIAL_PROFILE` arrived in
`89b8a6b15`; before it the option set was chosen per commit, and those builds are reproduced from
their lineage's own `profiling/series/<lineage>-buildenv.tsv`, not from a single recipe.
`build-monad` says so and stops rather than guessing.

---

## The short way: `cli/bench-pairs`

Everything in the next section, driven for you. Name the comparisons you want and it works out what
already exists, does only the rest, and ends on a `compare.html`:

```bash
./cli/bench-pairs --host <box> \
    --pair monad-zisk:zisk-reth:zisk \
    --pair al/zkvm-r10:al/zkvm-r9:zisk \
    --pair monad-sp1:rsp:sp1
```

Each side of a `--pair` is either a **branch** (anything with a `/` — needs a build and a witness
corpus) or a **guest** that already exists (`zisk-reth`, `rsp`, `monad-zisk`, or a path under
`guests/monad-variants/`). The third field is the backend, and it has to match both sides.

**`plan` is the default**: it prints what it would do and starts nothing. Read that first — one of the
steps it may schedule resets a 350 GB trie on the box. Add `run` to execute.

What it skips, because a full run is hours and almost none of it is usually necessary:

| already there | how it knows |
|---|---|
| the branch's build | a generation's `PROVENANCE.md` names that branch and its `elf/` pair is staged |
| the witness corpus | that generation holds a witness for every block of the range |
| the selected generation | `current` already points at it, so no `use-gen` |
| a reference guest's inputs | `1-<block>.bin` present for the range |
| the measurements | nothing to skip — the cache is keyed by ELF **content**, so any build measured before is known wherever it now sits |
| the axes | an axis with these two sides is already declared |

**Branches with no corpus are generated for, and only once.** `bench-pairs` covers the whole path
from a branch to a report: the first branch that needs a corpus gets the full `witness-backfill run`
and its pair becomes the generation's canonical ELFs; every other one gets `witness-backfill again`,
which builds its guest, replays only `PROBE` blocks (8 by default) and stops as soon as the witness
bytes match — then its ELFs are filed under `guests/monad-variants/<leaf>/` and read the shared
corpus. Two branches with identical generator code emit identical witnesses, so replaying twice buys
nothing; if the bytes *differ*, `again` carries on and files its own generation, which is the right
answer for a real format change.

One thing it cannot do for you: a branch that dumps witnesses must also be able to build the guest,
because one checkout serves both. The generator (`witness_generator.cpp`, `--zkvm-witness`) and the
guest crates (`zkvm/{zisk,sp1}`) live on different branches upstream, so name a branch that carries
both. And the branch must be present on the box — `--host` reaches it over ssh, it is not pushed
there for you.

`--gen NAME` names the generation a new corpus lands in; without it `witness-backfill` coins
`<branch-leaf>-<yyyy-mm>-<commit>`, which is traceable but not a name you want to type again.

`EPHEMERAL=1` marks the axes it declares, so `./axis.py prune` clears them when the campaign is over.
`--blocks FIRST-LAST` overrides the range (default: the current generation's whole corpus), and
`--report` the output path.

**Expect gaps on the reference side.** A reth corpus is minted through a public archive RPC (Alchemy),
which does not serve every block, so a 504-block range routinely resolves to ~365 for `zisk-reth` and
~373 for `rsp`. `bench-pairs` prints the count before measuring for exactly one reason: so the `n` in
the report reads as expected rather than as something to investigate. `cli/witness-farm` can retry the
gaps, but a block the provider refused once it usually refuses again.

Use the long way below when you want to run a phase on its own, or when something failed and you need
to resume in the middle.

### On a machine other than the usual one

Three separate machines are involved and they are portable to three different degrees. Getting this
wrong does not produce an error — it produces a number.

| what | runs where | portable? |
|---|---|---|
| `bench-pairs` and `compare.py` | your laptop | **yes** — `--host` has no default and is read only when a corpus is missing |
| witness generation (`witness-backfill`) | a **devcore box** | **no**, see below |
| the pure-time calibration | the host that measured it | **no** — the seconds are that host's |

**Witness generation is tied to a box by the snapshot** — the state at `FIRST-1` — and not by
anything else: the RPC is publicly reachable and the snapshot is copyable or re-dumpable, so any
Linux box with root, ~700 GB and the toolchains will do. A box that has never done this needs a
half-day of setup on top. Both are in
[`infra/monad-witness/README.md`](../infra/monad-witness/README.md#moving-to-a-devcore-box-that-has-never-done-this).
The consequence for a report: **the block range travels with the box** — `25815000..25815199` needs
the `25814999` snapshot — so a different machine usually means a different range, and two ranges are
not comparable to each other.

**The pure-time column belongs to one host.** Work (steps/cycles) and prover cost (COST/PGU) are
deterministic and mean the same thing anywhere. Seconds do not: they are modelled from
`PURE_MSTEPS_PER_S` in `compare.py`, measured on `CALIBRATION_CPU` (today an Apple M5 Max). Rendering
a report elsewhere prints a note and puts a banner in the report — **the ratios still hold, the
absolute seconds are the calibration host's**. To publish seconds measured from a new machine,
recalibrate (below) and update **both** `PURE_MSTEPS_PER_S` and `CALIBRATION_CPU`. Do not edit one
without the other: that is precisely the state the banner exists to catch.

#### Recalibrating the pure-time table

Do this on an **idle** machine, **sequentially** — a campaign's own per-block timings run under 4-way
parallelism and read ~35 % slow, which is why the column is modelled from these rates instead of
averaged from the cache. One run per guest is enough; the point is the rate, not any one block.

```bash
# frame the witness the way ev.sh does: LE64 length + payload + pad to 8
python3 -c 'import sys,struct; d=open(sys.argv[1],"rb").read(); n=len(d); open(sys.argv[2],"wb").write(struct.pack("<Q",n)+d+b"\x00"*((-(8+n))%8))' \
    guests/monad/current/witnesses/25815000.witness /tmp/w.bin
```

ZisK — the emulator reports the rate itself, so there is nothing to fit:

```bash
~/.zisk/bin/ziskemu -e guests/monad/current/elf/monad-zkvm-guest-zisk.elf -i /tmp/w.bin -o /dev/null -m
```

It prints one line: `process_rom() steps=251953621 duration=1.6923 tp=148.8852 Msteps/s …`. **`tp=` is
the number for the table.** `duration=` times `process_rom()` only, so it already excludes the
ELF→ROM conversion — 0.6–1.7 s per invocation depending on the ELF, which on a small block *is*
essentially the whole wall-clock. Never time this with `date`.

SP1 — same idea, its own tool. `--no-gas` matters: the gas pass is a separate and much slower
estimation that does not belong in an execution figure.

```bash
infra/sp1-infra/sp1-runner/target-native/release/sp1-runner --mode execute --no-gas …
```

Read `elapsed_secs` (SDK executor time, excludes process startup) and pair it with the block's cycles
from the cache, since `--no-gas` does not populate the counter. Take `cycles / elapsed_secs`.

Two things to know before quoting any of it. Per-block rates vary with the instruction mix (109–130 M
steps/s for the optimized guest across the calibration blocks), so the column is worth ±15 % — fine
for a ratio, not for a budget. And the ZisK **ASM** backend (`cargo-zisk execute --asm`) runs the same
guests ~2.4× faster (≈300 M steps/s), so a number from that backend is not comparable to one from
here; it is a different regime, not a faster flag.

## Compare two versions of the guest, A to Z

**The shape of it.** Old-vs-new is **two axes** (one per backend) and **four commands** —
`witness-backfill` (§1) → `use-gen` (§2) → `axis.py add`, twice (§5) → `compare.py` (§5) — plus
[the prerequisites above](#prerequisites), which are a build of their own. Budget ~2 h on the box for
step 1 (it destroys the box's triedb) and ~1–2 h locally per backend at a cold cache.

**Before you start, one question decides whether this is a 20-minute job or a two-hour one:
do you already have the OLD build's ELF?**

| | |
|---|---|
| **yes** — the old generation's `gen/<OLD-GEN>/elf/` is still on disk, or you have the pair from the box | skip to **§5**. Nothing needs the box. |
| **no** | you need step 1 **twice** — once per branch — and each run produces its own generation. See *Where the old ELF comes from* in §5 before spending the first two hours. |

`witness-backfill` is where an ELF comes from: it builds **both** guest ELFs on the box from the branch
it replays and files them next to that branch's witnesses. Nothing else in this repo builds a Monad
guest — `cli/gen-elf --guest monad-*` errors "pre-supplied" by design.

From a branch of the monad tree to a ratio. Step 1 runs against the devcore box (`HOST`); 2–5 are
local. **§3–§4 build an axis against reth**, which is a different question: read them as the worked
example of `axis.py add` + `compare.py`, then do the real thing in **§5**.

### 1. Witnesses + ELFs for the branch

```bash
cd ../infra/monad-witness                   # the producer side — NOT profiling/
export HOST=<your-devcore-box>              # required, no default (see below)

./witness-backfill plan al/zkvm-r10         # print every command, start nothing (the default verb)
./witness-backfill run  al/zkvm-r10         # sync, build, replay, pull home
./witness-backfill status                   # where the remote run is
```

> ⚠️ **`run` is destructive and takes hours.** It **truncates the box's triedb** and recreates it
> (`monad-mpt --create`) — there is no way back to an old range otherwise — then loads a snapshot
> (**~66 min, ~350 GB**), fetches the block_db, replays at ~4.6 blocks/s with `--zkvm-witness`, and
> pulls the corpus home (**~3.8 GB** for 504 blocks). Count **~2 h wall**. Its preflight refuses to
> start while anything else is using the triedb, so **stop the RTP producer first**
> (`./witness-follow stop`) — and note that the backfill leaves the box on the backfilled range, not
> at the tip. `plan` prints every command and starts nothing; use it first.

`HOST` is a placeholder above and stays one: the box is your infrastructure, not part of the project,
so no hostname is committed anywhere in this repo. Everything in `infra/monad-witness/` reads it from
the environment, and every verb refuses to start without it.

Already have a loaded trie for this range from another branch? `again` rewinds it instead of
reloading the snapshot — ~66 min becomes seconds:

```bash
AGAINST=canonical-2026-08-25815000-25815199-d49075fa3 ./witness-backfill again al/zkvm-r10
```

`again` needs [`patch-fixed-history.py`](../infra/monad-witness/patch-fixed-history.py) applied on the
node: it pins the trie history so the rewind can reach the start of the range. `run_replay.py` already
forwards the flag, but an unpatched node ignores it.

Three guards fire, in this order:

1. **the generation already exists** → refused before anything is built. Whether it resembles
   `AGAINST` is beside the point; it is on disk, so it is not regenerated. `FORCE=1` overrides.
2. **`AGAINST` + `PROBE`** — `PROBE` defaults to **20** blocks. Their **bytes** are compared against
   that corpus, and the run stops if they match: this branch then needs no corpus of its own, only a
   build under `guests/monad-variants/` reading the existing one.
   `PROBE=0` skips the check; a larger value trades seconds of replay for more confidence.
3. **the rewind was refused** — usually because disk occupancy crossed 60% during the previous
   replay and the compaction took the history with it. It says so in full and asks before falling
   back to a reset + load.

One command produces a whole **generation** under `guests/monad/gen/<GEN>/`: `witnesses/` (the corpus
replayed by that branch's node) **and** `elf/monad-zkvm-guest-{zisk,sp1}.elf` (built on the box from
that branch). `GEN` defaults to `<branch-leaf>-<yyyy-mm>-<commit>` — the commit is in the name because
these branches are force-pushed and a leaf alone collides with itself within a month.

**Which blocks.** `BLOCKS` defaults to `guests/monad/current/witnesses`, i.e. *the set you already
have*, so old and new are measured on the same blocks. A fresh clone has no `current`, so name the
range instead — it also accepts a file of block numbers, or a directory:

```bash
BLOCKS=25815000-25815199 ./witness-backfill run al/zkvm-r10
```

The set must be **contiguous**; the tool refuses one with gaps rather than replay a range it cannot
bound.

### 2. Select the generation, and where the build lives

**Step 1 already brought the ELFs home.** This is worth stating plainly, because it is the answer to
"where do I get a build to compare": `witness-backfill` compiles both guest ELFs on the box from the
branch it replays, verifies their sha256 on arrival, and files them beside that branch's witnesses —

```
guests/monad/gen/<GEN>/witnesses/          the corpus that branch's node produced
guests/monad/gen/<GEN>/elf/monad-zkvm-guest-zisk.elf     ← the build, for a `zisk` axis
guests/monad/gen/<GEN>/elf/monad-zkvm-guest-sp1.elf      ← the build, for an `sp1` axis
guests/monad/gen/<GEN>/PROVENANCE.md       branch, commit, host, both sha256 — written by the tool
```

Those two paths are what §3 and §5 name on an axis. There is no build step to run locally and nothing
to copy. Run step 1 on a second branch and you get a second generation with its own `elf/` — that is
how you end up holding an old build and a new one at the same time.

```bash
cd ../../guests/monad                       # from infra/monad-witness/
./use-gen                                   # list; the new one should appear
./use-gen <GEN>                             # point `current` + `fixtures` at it, install its ELFs
```

This is what makes `guests/monad/fixtures` — the path every profiling tool globs — resolve to the new
corpus. It also copies that generation's ELFs to the two tracked top-level ones, so the switch shows
up as a modification to commit.

> ⚠️ **It also moves two axes.** `zisk` and `sp1` have `guests/monad-{zisk,sp1}/*.elf` on their `a`
> side — symlinks to the very pair `use-gen` just rewrote. From here on those two axes measure the
> generation you selected, under a label that says nothing about it, and the canonical four-axis
> report can end up describing one binary twice. Every other axis names a path of its own and is
> pinned. `compare.py` prints a warning when two axes of a run land on the same build (it compares
> `a_ident`/`b_ident`, the sha of what actually ran), but the axis names will not tell you.

An axis takes its name from the declaration (`--a-name`), never from the filename, so **no copy and no
rename makes a build axis-able** — both generations' ELFs stay where step 1 put them, and the label is
something you choose at `axis.py add` time.

Do **not** copy the pair into `monad-variants/` to give it a nicer name: that creates a second copy to
keep in step by hand and buys nothing, since builds are addressed by content sha256 (`cache.py`) and
the copy and the original are one identity. `monad-variants/` is for builds **no generation owns** —
ablations, a branch measured against a set it did not generate — and it has its own
[README](../guests/monad-variants/README.md).

### 3. Declare the axis

```bash
cd ../../profiling
./axis.py add r10-vs-reth --backend zisk \
    --a-name monad-r10-zisk \
    --a-elf guests/monad/gen/<GEN>/elf/monad-zkvm-guest-zisk.elf --a-src monad \
    --b-name zisk-reth --b-elf guests/zisk-reth/zisk-reth.elf    --b-src bin
```

It prints how many blocks the axis resolves on. **Zero means the two sides share no block** — usually
the reth side has no `.bin` for the new range; mint them with `cli/witness-farm` (section Witnesses).

> ⚠️ **`axis.py` edits `compare.py`, which is a tracked file.** `add`, `rm`, `prune` and `gc` rewrite
> the `AXES` literal in place — so declaring an axis is a **source change**, and it lands in
> `git status` next to whatever else you were doing. Nothing warns you at commit time.
>
> The edit itself is safe: it re-parses the result and refuses to write if `compare.py` would no
> longer load, and it preserves the executable bit. `git diff profiling/compare.py` shows exactly
> what your axis added.
>
> What to do with it depends on what the axis is for:
>
> | the axis is | do this |
> |---|---|
> | a durable comparison of the project (old-vs-new of a branch that shipped) | commit it — that is how the next person reproduces your report |
> | one campaign's scaffolding | declare it with `'ephemeral': '<why it exists, when to drop it>'` so `./axis.py prune` finds it later, and commit or not as you prefer |
> | a one-off you are done with | `./axis.py rm <name>`. It discards **no** measurement — the cache is keyed by build content, not by axis, so re-adding it later is a cache hit |
>
> The failure mode this prevents is a stale axis nobody removed: it still resolves, still reports
> coverage, and only the numbers look odd (see *Cleaning up after a campaign*).

### 4. Measure

Bound the run to **your** generation's range. The numbers below are the published set's; step 1 printed
yours as `first..last`, and a block outside the bounds is dropped without a word:

```bash
./compare.py --block-min 25815000 --block-max 25815199 --axis r10-vs-reth
```

Writes **`results/compare.html`** — the deliverable — plus `results/compare.json` and
`results/compare-summary.{html,md}`. Same four files as the canonical report, and an `--axis` subset
merges into the JSON rather than replacing it.

**How long.** A cold ZisK axis over ~500 blocks runs both sides at ~2 s/block (~10 min at the default
`--jobs 4`), plus the instrumented COST pass at ~19 s/block — **1–2 h**, which `--quick` skips — plus
~20 min of family profiling (`--families 0` skips it). Everything is cached per block and per build,
so a re-run, an added block or a second axis on a build already measured is **seconds**.

### 5. The same, but old build vs new build

Against reth you learn *how far from reth this build is*. Against the previous build you learn *what
your commits did*, which is usually the question. **This is the section to read if old-vs-new is what
you came for** — it is two axes of its own (one per backend, since work-units never cross zkVMs), and
three things change.

**Both sides are Monad builds, so both read the Monad witness** — and there is a single lookup head
(`guests/monad/fixtures`), so **the old build must be able to read the generation you selected**. Check
that before measuring; see the warning below.

**Ratios are `a / b`, so `a` = the new build and `b` = the old one.** Reversed, every number is the
reciprocal and the report reads as a regression.

**Both sides carry `--a-src monad` / `--b-src monad`**: old and new are both Monad builds, so both
read the shared witness. The per-backend shape — framed for `ziskos`, verbatim for SP1 — is derived,
not declared, so the ZisK and SP1 axes below differ only in `--backend` and the ELFs.

Both ELFs are read straight out of their generation's `elf/` — the paths step 1 filed them under, per
§2. `<NEW-GEN>` is the generation you selected with `use-gen`; `<OLD-GEN>` is the one the old branch
produced, and it is **not** selected — only its ELF is used, never its witnesses.

```bash
cd ../../profiling                          # from guests/monad/ (§3–§4 already left you here)
# run the format check below FIRST — it is 30 s and nothing after it re-does it

./axis.py add r10-vs-r9 --backend zisk \
    --a-name monad-r10-zisk \
    --a-elf guests/monad/gen/<NEW-GEN>/elf/monad-zkvm-guest-zisk.elf --a-src monad \
    --b-name monad-r9-zisk \
    --b-elf guests/monad/gen/<OLD-GEN>/elf/monad-zkvm-guest-zisk.elf --b-src monad

./axis.py add r10-vs-r9-sp1 --backend sp1 \
    --a-name monad-r10-sp1 \
    --a-elf guests/monad/gen/<NEW-GEN>/elf/monad-zkvm-guest-sp1.elf --a-src monad \
    --b-name monad-r9-sp1 \
    --b-elf guests/monad/gen/<OLD-GEN>/elf/monad-zkvm-guest-sp1.elf --b-src monad

./compare.py --block-min <FIRST> --block-max <LAST> --axis r10-vs-r9 --axis r10-vs-r9-sp1
```

Writes the same four files as §4, `results/compare.html` first among them. `<FIRST>`/`<LAST>` are your
generation's own bounds — step 1 printed them, and `PROVENANCE.md` records them.

`rowlink-vs-typedj` in `AXES` is exactly this shape, already declared — two Monad builds of one
lineage, one backend: `./axis.py show rowlink-vs-typedj` is the shortest way to see one before
writing your own.

> The `--a-elf` path is only ever a **location**. If the old build is not a generation's canonical pair
> — you copied it off the box, or it is an ablation — park it under `guests/monad-variants/<name>/` and
> point `--b-elf` there instead. That is what [that directory is for](../guests/monad-variants/README.md);
> a build a generation *does* own has no business being copied into it (§2).

**Where the old ELF comes from.** A fresh clone has none: `gen/*/elf/` and
`guests/monad-variants/*/*.elf` are git-ignored, and a retired generation's binaries may be deleted
outright (see `gen/offsettriedb-prerework-2026-08/PROVENANCE.md`). Two ways to get one, no third:

- **copy the pair off the box**, into `guests/monad-variants/<name>/`;
- **run step 1 again on the old branch** — which produces a *second* generation, with its own `elf/`
  and its own witnesses.

Either way, keep the NEW generation selected (`./use-gen <NEW-GEN>`): the witnesses both sides read
must be one set, or you are comparing two corpora and calling it a guest change.

> ⚠️ **Check that the old build reads this generation — nothing downstream will.** Two generations can
> share a format word, a magic and a byte size and still be incompatible (`witness-fmt` cannot separate
> them; the ⚠️ section of `gen/offsettriedb-rework-2026-08/PROVENANCE.md` is the case that happened).
> An old build predating a reader rework is exactly that case. It fails two ways: loudly, the guest
> exits non-zero and `compare.py` now reports *"N of M blocks dropped"* with the reason; quietly, the
> parse completes and commits a **wrong state root** — and `compare.py` verifies no root, so the ratio
> looks fine. One block is enough to tell:
>
> ```bash
> cd ../guests/monad
> b=$(ls -1 fixtures/*.witness | tail -1 | xargs basename | cut -d. -f1)   # tail: see below
> mkdir -p /tmp/fmt-check && ln -sf "$PWD"/fixtures/$b.* /tmp/fmt-check/
> ELF=gen/<OLD-GEN>/elf/monad-zkvm-guest-zisk.elf WIT=/tmp/fmt-check ./ev.sh
> ```
>
> **What counts as a pass** depends on what the generation carries, so read the verdict against the
> table below rather than against a single expected string:
>
> | verdict | means |
> |---|---|
> | `PASS(pv3)` | all three public values matched positionally — the strong one. Needs `.expected_pv` |
> | `PASS` | the post state root is in the output. The set has no `.expected_pv` for that block; the other two thirds are unchecked, but **the build reads this generation**, which is the question here |
> | `PASS(rev)` | root matched byte-reversed — a real endianness answer, investigate before measuring |
> | `MISMATCH…` · `EMU-FAIL` · `SHORT` | the build does **not** belong on an axis over this generation. Re-measure the old branch on its own generation and compare the two reports instead |
>
> **`tail`, not `head`**: the **first** block of a set never has an `.expected_pv` (its parent's
> post-root is not local), so `head` would hand you the weakest check on the one block you picked.
>
> **`.expected_pv` is optional and a generation may well not have one.** Nothing in the measurement
> path reads it: `compare.py`, `hotspots.py` and the runners execute the guest on the witness and never
> look at a root — the files only sharpen `ev.sh`'s verdict from "the post root is in there somewhere"
> to "all three public values, in position". `witness-backfill` does **not** produce them, so a
> generation you just made has none. Add them if you want the strong verdict, on the generation's own
> witness directory (it derives `pre` from each block's parent and cross-checks `hash` against the next
> block's `parent_hash`, so it needs the whole set, not a temp dir):
>
> ```bash
> ./gen-expected-pv.py gen/<GEN>/witnesses            # --check-only reports without writing
> ```
>
> (`ev.sh` rewrites `exec-verified.csv`; a plain `./ev.sh` regenerates the full one.) Then
> `cd ../../profiling` and run the block above.

---

## compare.py — how much more

The ratio between two builds, over a block set. `hotspots.py` below answers *where* the difference
sits; the two share one cache, so a block either has measured is never re-executed by the other.

### The canonical report

The current campaign compares the **Monad r10 guest against ziskethone**, over the 200-block corpus
`canonical-2026-08-25815000-25815199-d49075fa3`:

```bash
./compare.py --block-min 25815000 --block-max 25815199 --axis r10tip-vs-ziskethone
```

Writes `results/compare.json`, `compare.html`, `compare-summary.html`, `compare-summary.md`.

**Both bounds, always.** `--block-min` drops the two one-off blocks (`25229951`, `25229957`) that
still sit in `guests/monad/inputs/` from ~600k blocks earlier and that the ziskethone side happens to
have inputs for: letting them in moves the geometric mean from **0.602× to 0.199×**. `--block-max`
pins the top so the set stays fixed as the RTP pipeline keeps minting witnesses at the tip.

Each axis summary records `a_ident` / `b_ident` — the **sha256 of the build that was actually
measured**, not of the one the axis names. A name is a label and an ELF path is a location; neither
says which binary produced a number, and a guest under active work is rebuilt over its own path. This
is derived from the run, so it cannot disagree with it.

> ⚠️ **The four-axis report `zisk` + `sp1` + `cur-zisk` + `cur-sp1` no longer runs.** Those axes pit a
> Monad build against a **reth** guest, whose `1-<block>.bin` inputs exist for the old 504-block range
> (`25551991..25552607`) and not for the corpus selected today. Measured now: each of the four
> resolves on **2 blocks**, and **0** with the bounds this section used to prescribe. `compare.py`
> skips an axis it cannot run and exits 0, so that command produces an empty report rather than an
> error.
>
> The same applies to what read them: **`levers.py` refuses n < 300** and speaks about the r4 base, so
> it cannot run here either. Its successor for this base is **`levers-r10.py`**, which reads the
> `r10tip-vs-ziskethone` axis instead. To bring the reth axes back you need a reth corpus over the
> current range — `ALCHEMY_URL=... ./cli/witness-farm 25815000` (section *Witnesses*) — which is a
> deliberate ~200-block mint, not a step of this report.

Add `--quick` to skip ZisK's instrumented COST pass (~10× slower); everything already cached is
still reported.

### One axis, or a sample

```bash
./compare.py --block-min 25815000 --block-max 25815199 --axis r10tip-vs-ziskethone   # one axis, full set
./compare.py --axis r10tip-vs-ziskethone --limit 20 --quick   # a 20-block sample
```

An `--axis` subset **merges** into `results/compare.json`: the axes you did not run stay in the file.
The HTML shows only the axes just run and prints which others the JSON holds.

`--limit` or `--blocks` makes the run a **sample**, whose statistics describe that sample only — it
writes `results/compare-partial.*` instead of the canonical paths, and says so. Pass `--json` /
`--html` to choose a path yourself; that overrides both behaviours.

### Re-render a report without measuring

```bash
./compare.py --summary-from results/compare.json
```

Rebuilds the HTML and the summary from an existing `--json` payload. Measures nothing.

### Which blocks are off-pattern, and why

```bash
./compare.py --block-min 25815000 --block-max 25815199 --axis r10tip-vs-ziskethone --show-outliers 20
./compare.py --axis r10tip-vs-ziskethone --spread         # what makes a block expensive
./compare.py --block-min 25815000 --block-max 25815199 --axis r10tip-vs-ziskethone --deep 5   # + module diff
```

`--deep N` appends a `hotspots.py` module diff, so the summary names *which modules* carry the gap.
`--spread-side {a,b}` picks the guest to profile for `--spread` (default `a`).

---

## Axes — what they are

**An axis is one question, named.** *How much more does guest A cost than guest B, on one zkVM, over
the blocks both of them can run?* That is the unit `--axis` selects, the unit the report is sectioned
by, and the unit a ratio belongs to.

It exists because **work-units do not compare across zkVMs** — ZisK steps, SP1 cycles and OpenVM
instructions are different things. So a comparison is only meaningful inside one backend, and an axis
is what pins it there. `cur-zisk` and `cur-sp1` are the *same two guests* measured on two VMs: two
axes, never one, and their numbers are never put in the same table.

A guest can appear in many axes. `monad-r10-typedj-zisk` is the `a` side of `typedj-vs-overlay` and
the `b` side of the ablation and lever axes that follow it — same binary, several questions.

### Backends: `zisk` and `sp1`, and nothing else

`compare.py` and `hotspots.py` drive two runners: `ziskemu` and `sp1-runner`. **There is no OpenVM
axis and no OpenVM profile** — neither tool knows that backend.

OpenVM work-units reach the cross-guest table another way: `infra/openvm-infra/run execute` writes
`guests/openvm-reth/inputs/<tag>.exec-report.json`, and `./results.py` reads those. That table is
where OpenVM is comparable to the others (per block, per guest), and it is the only place.

### The declaration

Axes live in the `AXES` dict at the top of `compare.py`:

```python
'r10tip-vs-ziskethone': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-r10-tip-zisk',
                   'elf':  'guests/monad-r10-zisk/monad-r10-zisk.elf',
                   'src':  'monad'},
             'b': {'name': 'ziskethone',
                   'elf':  'guests/ziskethone/ziskethone.elf',
                   'src':  'bin'}},
```

| field | |
|---|---|
| `backend` | `zisk` or `sp1` — which runner executes both sides |
| `unit` | `steps` (ZisK) or `cycles` (SP1); a label, and it must match the backend |
| `a` / `b` | the two guests. Ratios are **a / b**, so `b` is the reference — the thing A is measured *against* |
| `name` | the label shown in reports and recorded in the cache index. Follow [`../guests/README.md`](../guests/README.md) section Naming |
| `elf` | repo-relative path. Its **sha256 is the cache identity** — the axis name is not part of any key |
| `src` | where that guest's input comes from (below). **Not** its shape — that follows the backend |

**`src` values** — two, because the field answers *where from*, not *what shape*:

| `src` | input | resolved from |
|---|---|---|
| `bin` | the guest's own pre-generated `1-<block>.bin`, already in its own format | `guests/<name>/fixtures/`, then `guests/<name>/inputs/` |
| `monad` | the shared Monad witness | `guests/monad/fixtures/<block>.witness`, then `guests/monad/inputs/1-<block>.witness` |

The **shape** is derived from the backend, never declared per side: ZisK's `ziskos` reads a
length-prefixed file, so a `monad` input is framed `LE64(len) + witness + pad8` into a temp file on the
way in; SP1 reads the buffer verbatim; a `bin` is already in its guest's own format and is never
touched. Nothing is persisted — no `.bin` is created for a Monad side.

Spelling the shape per side is what this replaced, and it could only ever agree with the backend or be
broken — with nothing rejecting the broken pairing, and a wrongly-shaped input parsing as garbage
rather than failing.

### Which blocks an axis runs on

Not configured — **derived**. The universe is every block that has a Monad witness
(`guests/monad/{fixtures,inputs}/*.witness`), and an axis keeps those where **both** sides resolve an
input.

Two consequences worth knowing before reading an `n`:

- a guest missing a `.bin` for a block **shortens the axis silently** rather than failing. `n` in the
  summary is what actually ran, and two axes over the same range routinely differ (365 vs 373).
- a block with **no Monad witness is invisible to every axis**, including one comparing two reth
  guests. The Monad corpus defines the block set for all of them.

### An axis on a reth guest (`zisk-reth`, `rsp`, `openvm-reth`)

Everything above is about Monad builds, which are pre-supplied and arrive with a witness generation.
The reth guests are the opposite: they are **built from a vendored upstream**, and they carry their
own inputs rather than reading Monad's.

```bash
cd ..
./cli/gen-elf --list                          # which guests build, and from which vendor checkout
./cli/gen-elf --guest zisk-reth               # -> guests/zisk-reth/zisk-reth.elf
ALCHEMY_URL=... ./cli/witness-farm 25551992   # -> guests/zisk-reth/fixtures/1-<block>.bin
```

`gen-elf` needs that stack's toolchain (`cargo prove` · `cargo-zisk` · `cargo openvm`) and its
`vendor/` checkout — `cli/install-vendors` clones them at pinned commits. A guest whose ELF is
**pre-supplied** (`monad-*`) returns a clear error instead of building.

Then the axis, with two things different from a Monad side:

```bash
cd profiling
./axis.py add zisk-reth-vs-r10 --backend zisk \
    --a-name zisk-reth      --a-elf guests/zisk-reth/zisk-reth.elf                       --a-src bin \
    --b-name monad-r10-zisk --b-elf guests/monad-r10-zisk/monad-r10-zisk.elf             --b-src monad
```

- **`--a-src bin`**, not `monad`. A reth guest reads its **own** `1-<block>.bin` from
  `guests/<name>/{fixtures,inputs}/`; it never touches a Monad witness. `src` is per side, so an axis
  pairing a reth guest with a Monad build carries both values — that is normal, and it is why `src` is
  a side field rather than an axis one.
- **`--a-name` must be the guest's directory name** — `zisk-reth`, `rsp`, `openvm-reth`. For a `bin`
  side the name is not just a label: it is how the input is located
  (`guests/<name>/fixtures/1-<block>.bin`). Rename it and the side resolves nothing, and the axis
  shortens to zero blocks without an error.

`openvm-reth` builds and mints inputs like the others, but **no axis can run it**: `compare.py` drives
`ziskemu` and `sp1-runner` only. Its work-units reach `./results.py` through
`infra/openvm-infra/run execute`.

### Adding and removing one

`axis.py` edits the `AXES` literal for you and runs the checks first. **It edits `compare.py` on
disk** — a tracked file, so every one of these commands shows up in `git status`; see the warning in
*Compare two versions of the guest* §3 for what to do with that diff.

```bash
./axis.py list                    # every axis, its builds, how many blocks it can run on
./axis.py show r10tip-vs-ziskethone  # one axis in detail
./axis.py add nofuse-vs-r10 --backend zisk \
    --a-name r10-no-fuse    --a-elf profiling/series/elf/no-fuse.elf                --a-src monad \
    --b-name monad-r10-zisk --b-elf guests/monad-r10-zisk/monad-r10-zisk.elf        --b-src monad
./axis.py rm nofuse-vs-r10
```

It refuses an ELF that is not there, a name already taken, an unknown `src`, and it warns loudly when
an axis would run on **zero** blocks — every one of those is a mistake that otherwise surfaces as a
missing number rather than an error. `--unit` exists but you should not need it:
the unit follows the backend (`zisk`→`steps`, `sp1`→`cycles`) and `axis.py` fills it in — setting it
by hand only relabels numbers it does not change.

#### Cleaning up after a campaign

Axes accumulate, and a stale one is quiet — it still resolves, still reports coverage, and only the
numbers look odd. Two commands, both dry runs unless you pass `--yes`:

```bash
./axis.py prune          # every axis marked `ephemeral`: one campaign's scaffolding
./axis.py gc             # every axis whose BUILD was deleted
```

Mark an axis `'ephemeral': '<why it exists, when to drop it>'` when you declare it for a single
campaign — the ablation axes are the case. They pit a variant against a *tip*, so they rot the moment
that tip moves: the comparison still runs, against a base nobody cares about. `./axis.py list` shows
them as `EPH` with their reason.

`gc` handles the other rot: an axis outliving its guest. It does not simply act on "file missing",
because a build a fresh clone never received looks identical on disk to one that was deleted. It asks
the cache: measurements recorded *here* under that build's name prove the ELF existed on this machine,
so its absence is a deletion. Those axes are removed; the others are listed and left alone.

`compare.py` enforces the same distinction at run time. A deleted build is **fatal** — it names the
axis and the fix rather than reporting on fewer axes than you asked for. A never-received build is a
warning, and every skip is recapped after the reports, where it cannot scroll away.

Removing an axis never discards a measurement: the cache is keyed by build content, not by axis.

`add` prints how many blocks the new axis resolves on **before** you measure — the difference between
"n=365 as expected" and "n=3 and nobody noticed". Both commands re-parse `compare.py` after editing
and refuse to write if the result would not load.

Then measure it over the published set: `./compare.py --block-min 25815000 --block-max 25815199 --axis <name>`. Nothing else — the cache keys on the
ELF's content, so an axis reusing a build another axis already measured is a cache hit from the first
run. There is no priming step.

Removing an axis discards nothing: its measurements are keyed by build, so any axis naming the same
ELF still reads them. Only the label goes.

By hand, the entry is the dict shown above; the same rules apply, without the checks.

---

## Witnesses — producing them, and where they go

Where an axis looks for them is not configured, it is derived from the side's `src` (see *Axes*):
`bin` resolves `guests/<name>/fixtures/1-<block>.bin` then `guests/<name>/inputs/`, `monad` resolves
`guests/monad/fixtures/<block>.witness` then `guests/monad/inputs/`. For a `bin` side the guest's
**name is the lookup path** — rename it and the axis silently shortens to zero blocks.

Three guests mint their own from an RPC, `ziskethone` is transcoded or proxied, and the Monad
witnesses cannot be made here at all.

### reth guests (`rsp`, `zisk-reth`, `openvm-reth`)

```bash
cd ..                                              # these run from the repo root
./cli/gen-witness --guest zisk-reth --block 25815000   # one block, routed to that guest's stack
./cli/gen-witness --list

ALCHEMY_URL=https://eth-mainnet.g.alchemy.com/v2/<key> ./cli/witness-farm 25815000
ALCHEMY_URL=... GUESTS=openvm STRIDE=1 MAX_BLOCKS=200 ./cli/witness-farm 25815000
```

`witness-farm` marches forward from a block and is resumable. Where each guest's witnesses land:

| guest | lands in |
|---|---|
| `rsp` | `guests/rsp/fixtures/` |
| `zisk-reth` | `guests/zisk-reth/fixtures/` — `<tag>.bin` **and** `<tag>.hints` |
| `openvm-reth` | `guests/openvm-reth/inputs/rpc-cache/` |

plus its ledger and logs under `run-data/`. `zisk-reth` needs a node exposing
`debug_executionWitness`; `rsp` and `openvm-reth` take a standard archive RPC.

### ziskethone — transcoded or proxied, never `witness-farm`

Lands in **`guests/ziskethone/fixtures/1-<block>.bin`** (git-ignored per `guests/*/fixtures/`), one
artifact per block and no `.hints` sibling — the ziskethone client is a native C++ input checker and
emits none. The file is a `ZiskStdin`-framed **`ZEG0` container**: a u64-LE length, then a state trie
the host has already pre-linearised into an opcode stream. Nothing like `zisk-reth`'s two bincode
slices, so the two are not interchangeable even though both feed a ZisK guest.

> ⚠️ **A ziskethone witness is only valid if its execution witness carried the 32-byte storage-slot
> preimages**, not just the 20-byte addresses: the ZEG0 encoder needs the plaintext slot to write a
> storage leaf's `position`. Without them the failure is silent and expensive — every storage leaf
> degrades to a phantom leaf, so `numberOfStorages == 0`, the pre-execution root **still matches** the
> trusted parent anchor, and every `SLOAD` reads zero. The only symptom is a wrong block hash at the
> very end of the run. Two guards now refuse it up front (one in the encoder, one in the guest), so
> you should never see the silent version; a witness collected through an **unfixed** proxy is the
> case they catch.

Two ways to make more, and `cli/witness-farm` is not one of them — its `zisk` arm builds an ELF and
generates hints, neither of which a ZEG0 container needs, and N workers serialise on the cargo lock.

```bash
# offline, from an existing reth witness — no RPC, no quota, works for blocks too old to serve state
vendor/zisk-eth-client/tools/reth-to-ziskethone/target/release/reth-to-ziskethone <input-or-dir> -o /tmp/zeg0
for f in /tmp/zeg0/mainnet_*_zec_ziskethone.bin; do          # adopt the harness naming
  b=$(basename "$f" | cut -d_ -f2); cp "$f" guests/ziskethone/fixtures/1-$b.bin
done

# live — Alchemy has no `debug` namespace, hence the patched proxy, then fan out input-gen
RUST_LOG=info vendor/openvm-eth/target/release/openvm-rpc-proxy --rpc-url "$ALCHEMY_URL" \
  --bind-address 127.0.0.1:8545 --rpc-retry-cu 8300 --preimage-cache-nibbles 7 \
  --witness-cache-dir run-data/wcache
```

The proxy needs the `keys` fix in `cli/vendor-patches/openvm-eth.patch` — that is what makes it emit
slot preimages. Set `--rpc-retry-cu` ~17 % **below** the account's CU/s limit: alloy's per-request
estimate runs light, and a 429 that gets retried and lands is billed twice. Full procedure, the
throughput table and the stale-cache trap: [`../guests/ziskethone/inputs/README.md`](../guests/ziskethone/inputs/README.md).

### Monad guest — not producible here

Monad witnesses come from a Monad node replaying mainnet
([`../infra/monad-witness/`](../infra/monad-witness/README.md), on the devcore box). A fresh clone has none.

They are stored **per generation** — a guest lineage plus the witness wire format it reads, which
change together:

```
guests/monad/gen/<generation>/witnesses/<block>.witness
                                        <block>.post_state_root
                                        <block>.expected_pv    optional, see below
guests/monad/gen/<generation>/elf/      the two ELFs built from the same checkout
guests/monad/gen/<generation>/PROVENANCE.md
guests/monad/current   -> gen/<generation>       symlink, set by ./use-gen
guests/monad/fixtures  -> current/witnesses      the path the tooling globs
```

`infra/monad-witness/witness-backfill` produces a whole generation — witnesses, both ELFs **and** its
`PROVENANCE.md`, which it writes itself from the commit it built; see **Compare two versions of the
guest, A to Z** above, which is the procedure. Then `./use-gen <name>`.

`.expected_pv` is the only piece it does not write, and the only optional one: 96 bytes of
`post || pre || hash` that turn `ev.sh`'s verdict into the positional `PASS(pv3)`. Execution,
profiling and `compare.py` never read it. Add it when you want that verdict —
`guests/monad/gen-expected-pv.py gen/<generation>/witnesses` (`--check-only` to report without
writing) — and expect 1 block short of the set: the first has no local parent post-root.

Never add witnesses to an existing generation's directory, and never copy between two: `witness-fmt`
cannot always tell two generations apart, and the directory is what separates them.

### Worked example — measure one new block on ZisK

Say block `25552600` was just minted. To get `zisk` numbers for it, **both sides need an input**:

```bash
cd ..
ls guests/monad/fixtures/25552600.witness        # the Monad side (a)
ls guests/zisk-reth/fixtures/1-25552600.bin      # the reth side (b)
```

Whichever is missing, produce it:

```bash
# reth side, from an RPC
ALCHEMY_URL=... ./cli/gen-witness --guest zisk-reth --block 25552600

# Monad side: it comes from the devcore box and belongs to a generation.
# For a one-off, guests/monad/inputs/1-25552600.witness is the second lookup path;
# for a set, make it a new generation (above) rather than adding to an existing one.
```

Then measure just that block, without touching the canonical report:

```bash
cd profiling
./compare.py --axis zisk --blocks 25552600
```

`--blocks` marks the run as a sample, so it writes `results/compare-partial.*`. Add `--axis sp1` for
the SP1 pair in the same run. To profile the block instead of comparing it, feed the same input to
`hotspots.py profile` (below).

There is no equivalent for OpenVM: run `infra/openvm-infra/run execute` for that block and read it
from `./results.py`.

### Proving inputs are a different artifact

`guests/monad-{sp1,zisk}/gen-inputs` persists a framed `1-<block>.bin` for `cli/prove-farm`. Profiling
does not need it — `compare.py` and `hotspots.py` read the witness directly and frame it in memory.

---

## hotspots.py — where the cost goes

Per-symbol cost for one build, or the module-level diff between two. Unlike `compare.py` it takes an
**ELF and an input directly** — there is no axis — and writes a `profile.json` with an HTML view
beside it.

### One build, one or more blocks

```bash
./hotspots.py profile --backend zisk \
    --elf ../guests/monad-r10-zisk/monad-r10-zisk.elf \
    -i ../guests/monad/execute-out/25815000.bin \
    --out results/r10-25815000 --title "monad-r10 · 25815000"     # ev.sh writes execute-out/
```

Writes `<out>/profile.json` + `<out>/index.html` — the hotspot icicle plus cost, opcode and category
breakdowns. `-i` repeats for several inputs, one tab each.

- `--backend sp1` drives the SP1 runner instead (the **profiling** build, see Prerequisites);
  `--sample-rate` sets its sampling rate.
- `--aggregate [label]` folds every `-i` into **one mean-per-block profile** instead of one tab each —
  what you want to characterise a build over a sample rather than inspect a single block.
- `--top N` caps the individually-named functions (default 200); the rest aggregates per module.
- `--verify-roots DIR` checks each output against `<tag>.post_state_root` in `DIR`.
- `--force` re-runs instead of reading the cache.
- `--tab-prefix <p>` namespaces the tags, so two guests' profiles can be rendered side by side without
  colliding.

> The input must be in the guest's own shape — `hotspots.py` takes the file you give it and does not
> frame anything. For a Monad guest on ZisK that means a **framed** `.bin` (e.g. from
> `guests/monad/execute-out/`), not a raw `.witness`. `compare.py` frames on the fly because it knows
> the axis's backend; here you are the one choosing. A raw witness fed to a ZisK guest does not fail
> cleanly — it parses as garbage.

### Two builds, same input

```bash
./hotspots.py compare --backend zisk \
    --elf-before ../profiling/series/elf/no-fuse.elf \
    --elf-after  ../guests/monad-r10-zisk/monad-r10-zisk.elf \
    -i ../guests/monad/execute-out/25815000.bin --out results/fuse-lever
```

One command for before/after: profiles both and renders the delta. `--label-before` / `--label-after`
name them.

### Two profiles already collected

```bash
./hotspots.py diff --json results/a/profile.json --json results/b/profile.json
```

Exactly two, A then B; `--label` twice to name them. Prints the per-module and per-function delta to
the terminal — this is what `compare.py --deep N` calls under the hood.

### Re-render without measuring

```bash
./hotspots.py render --json results/r10-25815000/profile.json --out results/r10-25815000
```

Rebuilds the HTML from an existing `profile.json`. For template or `--meta` changes.

## Regenerate a published artifact

One script, one document. These read measurements produced elsewhere rather than making their own —
run the canonical report first when a section says it needs one. (`inline-robust.py` is the exception:
it collects profiles unless given `--verdict-only`.)

### The ranked levers document

```bash
./levers.py
```

Writes `results/levers.html` — what to fix in the Monad guest, ranked, with a re-measure protocol per
item. Requires `results/compare.json` carrying the `zisk` and `sp1` axes with **n ≥ 300**; it refuses
a partial run rather than build a plausible wrong document. Run the canonical report first.
`--out` writes elsewhere.

### The optimised-branch report

```bash
./optimized.py            # results/optimized-{zisk,sp1}.json
./compare-optimized.py    # results/compare-optimized.html
```

Neither measures anything: both read a **frozen measurement set** left under `results/` by the levers
campaign. Nothing regenerates it — the branch's ELFs are gone — and `results/` is git-ignored, so **on
a fresh clone these two have nothing to read**. Skip them, or bring the payload in yourself.

### Cross-guest work table

```bash
./results.py
```

No arguments. Reads every `../guests/<name>/inputs/*.exec-report.json` and writes
`results/results.html` — how much work each guest does per block, side by side.

### Inlining-robustness verdict

```bash
./inline-robust.py --verdict-only
```

Writes `results/inline-verdict.json` from profiles already in the cache — the optional correction
column `compare.py` and `levers.py` read. Without `--verdict-only`, `--elf <no-inline build>` collects
the profiles first (`--limit`, `--chunk`, `--guest`, `--emu` tune that pass).

### RTP end-to-end latency

```bash
./rtp-latency.py --manifest ~/witness-manifest.csv --results ../infra/zisk-infra/results \
    --submissions ../run-data/ethproofs-mock-data/submissions.jsonl --csv rtp-latency.csv
```

Joins the producer manifest, the prover run records and the submission log into one per-block latency
table. `--guest`, `--chain-id`, `--queue` select what to join; `--cross-clock` when producer and
prover do not share a clock.

---

## The Monad guest itself

The only commands here that do **not** run from `profiling/` — they act on the guest directory that
every axis reads.

### Witness generations

```bash
cd ../guests/monad
./use-gen                              # list generations, mark the current one, show each format
./use-gen zkvm-r8-canonical-25815000-25815199-0df7094a1   # select it and install its ELFs
./witness-fmt fixtures/<block>.witness # name a witness's wire format
```

`use-gen` refuses a generation whose witnesses are not one format throughout, and one with no ELFs.
Selecting a generation rewrites the two top-level ELFs, which are tracked — the switch shows up as a
modification to commit.

> Two generations can share a format word and still be incompatible. `witness-fmt` cannot separate
> them; the directory they sit in is what does. Never copy witnesses between generation directories,
> and read the target's `PROVENANCE.md` before measuring against it.

### Execute + verify state roots

```bash
cd ../guests/monad && ./ev.sh
```

Runs every witness of the selected generation on the ZisK guest, printing one line per block (steps,
output size, PASS/MISMATCH) and writing `exec-verified.csv`. ZisK only.

---

## One-shot studies

```bash
./studies/elf-equiv.py     # is a debug build the same program as the release build?
./studies/dwarf-tax.py     # attribute cost by source location instead of symbol name
```

A pair: `dwarf-tax.py` is only meaningful on a debug build `elf-equiv.py` has certified equivalent.
