# profiling — runbook

What to type, and what it writes. Every command runs from `profiling/`.
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

```bash
cd ../infra/sp1-infra/sp1-runner
cargo build --release                                     # target/release       -> compare.py
cargo build --release --no-default-features \
      --features profiling --target-dir target-prof       # target-prof/release  -> hotspots.py
```

---

## Compare two versions of the guest, A to Z

**In five lines.** Old-vs-new is **two axes** (one per backend), **three commands** (`witness-backfill`
→ `axis.py add` → `compare.py`), and **one check you cannot skip**: that the old build can read the
witness generation you selected — nothing downstream verifies it. Budget ~2 h on the box for step 1
(it destroys the box's triedb) and ~1–2 h locally for step 4 at a cold cache. Everything below is the
detail; step 5 is the old-vs-new part.

From a branch of the monad tree to a ratio. Steps 1–2 run against the devcore box (`HOST`); 3–5 are
local.

### 0. Does this branch even need its own corpus?

A witness is produced by the **node**; the ELF is the **guest** that reads it. A branch that changes
only the guest emits the same witness bytes, and then it needs **no corpus of its own** — its build is
a *variant* reading the existing one (`guests/monad-variants/`, as `r3` does beside the `sam`
generation), and the whole reset + load + replay is waste.

That is answered by **comparing bytes**, not by reading source. A source diff is not an answer: clean
would prove sameness, but two branches almost always differ somewhere outside `zkvm/` — a test, a
README — so it reports "unknown" and helps nobody. Identical guest ELFs are not an answer either:
they pin the *format*, not the *content*, and the node can change which trie nodes it emits without
the reader noticing.

So the probe is built into `again` (step 1): it replays the first **`PROBE=20`** blocks (the default;
override it, or set `PROBE=0` to skip), compares their bytes to the corpus you name, and stops if they
match.

### 1. Witnesses + ELFs for the branch

```bash
./witness-backfill plan al/zkvm-r4          # print every command, start nothing
./witness-backfill run  al/zkvm-r4          # sync, build, replay, pull home
./witness-backfill status                   # where the remote run is
```

> ⚠️ **`run` is destructive and takes hours.** It **truncates the box's triedb** and recreates it
> (`monad-mpt --create`) — there is no way back to an old range otherwise — then loads a snapshot
> (**~66 min, ~350 GB**), fetches the block_db, replays at ~4.6 blocks/s with `--zkvm-witness`, and
> pulls the corpus home (**~3.8 GB** for 504 blocks). Count **~2 h wall**. Its preflight refuses to
> start while anything else is using the triedb, so **stop the RTP producer first**
> (`./witness-follow stop`) — and note that the backfill leaves the box on the backfilled range, not
> at the tip. `plan` prints every command and starts nothing; use it first.

`HOST` has no default and no example anywhere in this repo, deliberately: the box is your
infrastructure, not part of the project. Everything in `infra/monad-witness/` reads it from the
environment.

Already have a loaded trie for this range from another branch? `again` rewinds it instead of
reloading the snapshot — ~66 min becomes seconds:

```bash
AGAINST=offsettriedb-rework-2026-08 ./witness-backfill again al/zkvm-r4
```

`again` needs both node patches — [`patch-fixed-history.py`](../infra/monad-witness/patch-fixed-history.py)
pins the trie history so the rewind can reach the start, and
[`patch-run-replay-history.py`](../infra/monad-witness/patch-run-replay-history.py) forwards the flag,
which `run_replay.py` will not do on its own. Both or neither.

Three guards fire, in this order:

1. **the generation already exists** → refused before anything is built. Whether it resembles
   `AGAINST` is beside the point; it is on disk, so it is not regenerated. `FORCE=1` overrides.
2. **`AGAINST` + `PROBE`** — `PROBE` defaults to **20** blocks. Their **bytes** are compared against
   that corpus, and the run stops if they match: this branch then needs no corpus, only a variant.
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
BLOCKS=25551991-25552494 ./witness-backfill run al/zkvm-r4
```

The set must be **contiguous**; the tool refuses one with gaps rather than replay a range it cannot
bound.

### 2. Select the generation, and where the build lives

```bash
cd ../../guests/monad
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

The pair the axis will name is already in `guests/monad/gen/<GEN>/elf/` — **nothing to move**. An axis
takes its name from the declaration (`--a-name`), never from the filename, so no copy and no rename
makes a build axis-able, and its sha256 is already in the generation's `PROVENANCE.md`.

Do **not** copy the pair into `monad-variants/` to give it a nicer name: that creates a second copy to
keep in step by hand and buys nothing, since builds are addressed by content sha256 (`cache.py`) and
the copy and the original are one identity. `monad-variants/` is for builds **no generation owns** —
ablations, a branch measured against a set it did not generate — and it has its own
[README](../guests/monad-variants/README.md).

### 3. Declare the axis

```bash
cd ../../profiling
./axis.py add r4-vs-reth --backend zisk \
    --a-name monad-r4-zisk \
    --a-elf guests/monad/gen/<GEN>/elf/monad-zkvm-guest-zisk.elf --a-src monad \
    --b-name zisk-reth --b-elf guests/zisk-reth/zisk-reth.elf    --b-src bin
```

It prints how many blocks the axis resolves on. **Zero means the two sides share no block** — usually
the reth side has no `.bin` for the new range; mint them with `cli/witness-farm` (section Witnesses).

### 4. Measure

Bound the run to **your** generation's range. The numbers below are the published set's; step 1 printed
yours as `first..last`, and a block outside the bounds is dropped without a word:

```bash
./compare.py --block-min 25551991 --block-max 25552494 --axis r4-vs-reth
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
your commits did*, which is usually the question. It is one more axis, and three things change.

**Both sides are Monad builds, so both read the Monad witness** — and there is a single lookup head
(`guests/monad/fixtures`), so **the old build must be able to read the generation you selected**. Check
that before measuring; see the warning below.

**Ratios are `a / b`, so `a` = the new build and `b` = the old one.** Reversed, every number is the
reciprocal and the report reads as a regression.

**Both sides carry `--a-src monad` / `--b-src monad`**: old and new are both Monad builds, so both
read the shared witness. The per-backend shape — framed for `ziskos`, verbatim for SP1 — is derived,
not declared, so the ZisK and SP1 axes below differ only in `--backend` and the ELFs:

```bash
./axis.py add r4-vs-r3 --backend zisk \
    --a-name monad-r4-zisk --a-elf guests/monad-variants/r4/monad-r4-zisk.elf --a-src monad \
    --b-name monad-r3-zisk --b-elf guests/monad-variants/r3/monad-r3-zisk.elf --b-src monad

./axis.py add r4-vs-r3-sp1 --backend sp1 \
    --a-name monad-r4-sp1 --a-elf guests/monad-variants/r4/monad-r4-sp1.elf --a-src monad \
    --b-name monad-r3-sp1 --b-elf guests/monad-variants/r3/monad-r3-sp1.elf --b-src monad

./compare.py --block-min 25551991 --block-max 25552494 --axis r4-vs-r3 --axis r4-vs-r3-sp1
```

**Where the old ELF comes from.** A fresh clone has none: `guests/monad-variants/*/*.elf` and
`gen/*/elf/` are git-ignored, and a retired generation's binaries may be deleted outright (see
`gen/offsettriedb-prerework-2026-08/PROVENANCE.md`). So either copy the pair off the box, or run
**step 1 again on the old branch** — which produces a *second* generation. Keep the NEW one selected
(`./use-gen <GEN>`): the witnesses both sides read must be one set, or you are comparing two corpora
and calling it a guest change.

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
> ELF=../monad-variants/r3/monad-r3-zisk.elf WIT=/tmp/fmt-check ./ev.sh    # expect PASS(pv3)
> ```
>
> **`tail`, not `head`**: the strong verdict is `PASS(pv3)` — all three public values, compared
> positionally against `<block>.expected_pv`. The **first** block of a generation is the one that has
> no `.expected_pv` (its parent's post-root is not local, see the generation's `PROVENANCE.md`), so
> `ev.sh` falls back there to a substring test on the post root alone — the weakest check in the set,
> on the one block you would have picked. Anything other than `PASS(pv3)` (`PASS` alone, `PASS(rev)`,
> `MISMATCH(...)`, `EMU-FAIL`, `SHORT`) means that build does not belong on an
> axis over this generation — re-measure the old branch on its own generation instead, and compare the
> two reports. (`ev.sh` rewrites `exec-verified.csv`; a plain `./ev.sh` regenerates the full one.)

---

## The canonical report

```bash
./compare.py --block-min 25551991 --block-max 25552607 \
             --axis zisk --axis sp1 --axis cur-zisk --axis cur-sp1
```

Writes `results/compare.json`, `compare.html`, `compare-summary.html`, `compare-summary.md`.

Each axis summary records `a_ident` / `b_ident` — the **sha256 of the build that was actually
measured**, not of the one the axis names. A name is a label and an ELF path is a location; neither
says which binary produced a number, and a guest under active work is rebuilt over its own path. This
is derived from the run, so it cannot disagree with it.

> ⚠️ **Today two of these four are duplicates.** `zisk`/`sp1` follow `use-gen` (see *Compare two
> versions*, step 2) and the selected generation installs the very build the `cur-*` axes name — they
> both resolve `gen/<GEN>/elf/` — so `zisk` ≡ `cur-zisk` and `sp1` ≡ `cur-sp1`, and the run ends on the
> `one build, several labels` warning. That is the report telling the truth, not a bug to silence:
> the four sections are two comparisons. The pair is kept because **`levers.py` resolves `zisk` and
> `sp1` by name** (and refuses n < 300), so dropping them from the command breaks it. Until that is
> re-pointed, read the duplicate sections as one, and check `a_ident` in the JSON before quoting two
> figures as if they came from two builds.

**Why four axes are named.** `compare.py`'s own default is two — `cur-zisk` and `cur-sp1` — so a bare
`./compare.py` produces a report `levers.py` cannot use: it also reads the `zisk` and `sp1` axes.
Nothing in the CLI enforces this; the four are spelled out because the *report* needs them, not
because the tool asks.

**Why both bounds.** `--block-min` drops the older, unrelated blocks that sit in
`guests/monad/inputs/`; `--block-max` pins the top so the published set stays fixed as the RTP
pipeline keeps minting new witnesses at the tip. Today the corpus ends below the max and it changes
nothing — that is the point: the day it grows, the report does not silently grow with it. This is the
exact command `levers.py` prints in its own reports as the way to reproduce them, so the two must
match.

That is why this max (`25552607`) sits above the one used in *Compare two versions* (`25552494`, the
generation's actual last block). Both select the same blocks today; the canonical one is a pin that
must not move, the other is a range you read off your own generation.

Add `--quick` to skip ZisK's instrumented COST pass (~10× slower); everything already cached is
still reported.

## One axis, or a sample

```bash
./compare.py --block-min 25551991 --block-max 25552607 --axis cur-zisk   # one axis, full set
./compare.py --axis cur-zisk --limit 20 --quick          # a 20-block sample
```

An `--axis` subset **merges** into `results/compare.json`: the axes you did not run stay in the file.
The HTML shows only the axes just run and prints which others the JSON holds.

`--limit` or `--blocks` makes the run a **sample**, whose statistics describe that sample only — it
writes `results/compare-partial.*` instead of the canonical paths, and says so. Pass `--json` /
`--html` to choose a path yourself; that overrides both behaviours.

## Re-render a report without measuring

```bash
./compare.py --summary-from results/compare.json
```

Rebuilds the HTML and the summary from an existing `--json` payload. Measures nothing.

## Which blocks are off-pattern, and why

```bash
./compare.py --block-min 25551991 --block-max 25552607 --axis cur-zisk --show-outliers 20
./compare.py --axis cur-zisk --spread                     # what makes a block expensive
./compare.py --block-min 25551991 --block-max 25552607 --axis cur-zisk --deep 5   # + module diff
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

A guest can appear in many axes. `monad-r3-zisk` is the `a` side of `opt-zisk` and the `b` side of all
thirteen ablation axes — same binary, thirteen questions.

### Backends: `zisk` and `sp1`, and nothing else

`compare.py` and `hotspots.py` drive two runners: `ziskemu` and `sp1-runner`. **There is no OpenVM
axis and no OpenVM profile** — neither tool knows that backend.

OpenVM work-units reach the cross-guest table another way: `infra/openvm-infra/run execute` writes
`guests/openvm-reth/inputs/<tag>.exec-report.json`, and `./results.py` reads those. That table is
where OpenVM is comparable to the others (per block, per guest), and it is the only place.

### The declaration

Axes live in the `AXES` dict at the top of `compare.py`:

```python
'opt-zisk': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-r3-zisk',
                   'elf':  'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src':  'monad'},
             'b': {'name': 'zisk-reth',
                   'elf':  'guests/zisk-reth/zisk-reth.elf',
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
./axis.py add zisk-reth-vs-r3 --backend zisk \
    --a-name zisk-reth     --a-elf guests/zisk-reth/zisk-reth.elf                --a-src bin \
    --b-name monad-r3-zisk --b-elf guests/monad-variants/r3/monad-r3-zisk.elf    --b-src monad
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

`axis.py` edits the `AXES` literal for you and runs the checks first:

```bash
./axis.py list                    # every axis, its builds, how many blocks it can run on
./axis.py show cur-zisk           # one axis in detail
./axis.py add kec2-vs-reth --backend zisk \
    --a-name ab2-no-kec2 --a-elf guests/monad-variants/ab/ab2-no-kec2.elf --a-src monad \
    --b-name zisk-reth   --b-elf guests/zisk-reth/zisk-reth.elf          --b-src bin
./axis.py rm kec2-vs-reth
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

Then measure it over the published set: `./compare.py --block-min 25551991 --block-max 25552607 --axis <name>`. Nothing else — the cache keys on the
ELF's content, so an axis reusing a build another axis already measured is a cache hit from the first
run. There is no priming step.

Removing an axis discards nothing: its measurements are keyed by build, so any axis naming the same
ELF still reads them. Only the label goes.

By hand, the entry is the dict shown above; the same rules apply, without the checks.

---

## Witnesses — producing them, and where they go

Three guests mint their own from an RPC; the Monad witnesses cannot be made here at all.

### reth guests (`rsp`, `zisk-reth`, `openvm-reth`)

```bash
cd ..                                              # these run from the repo root
./cli/gen-witness --guest zisk-reth --block 25552053   # one block, routed to that guest's stack
./cli/gen-witness --list

ALCHEMY_URL=https://eth-mainnet.g.alchemy.com/v2/<key> ./cli/witness-farm 25551992
ALCHEMY_URL=... GUESTS=openvm STRIDE=1 MAX_BLOCKS=503 ./cli/witness-farm 25551992
```

`witness-farm` marches forward from a block and is resumable. Where each guest's witnesses land:

| guest | lands in |
|---|---|
| `rsp` | `guests/rsp/fixtures/` |
| `zisk-reth` | `guests/zisk-reth/fixtures/` — `<tag>.bin` **and** `<tag>.hints` |
| `openvm-reth` | `guests/openvm-reth/inputs/rpc-cache/` |

plus its ledger and logs under `run-data/`. `zisk-reth` needs a node exposing
`debug_executionWitness`; `rsp` and `openvm-reth` take a standard archive RPC.

### Monad guest — not producible here

Monad witnesses come from a Monad node replaying mainnet
([`../infra/monad-witness/`](../infra/monad-witness/README.md), on the devcore box). A fresh clone has none.

They are stored **per generation** — a guest lineage plus the witness wire format it reads, which
change together:

```
guests/monad/gen/<generation>/witnesses/<block>.witness
                                        <block>.post_state_root
guests/monad/current   -> gen/<generation>       symlink, set by ./use-gen
guests/monad/fixtures  -> current/witnesses      the path the tooling globs
```

`infra/monad-witness/witness-backfill` produces a whole generation — witnesses *and* ELFs — from a
branch; see **Compare two versions of the guest, A to Z** above, which is the procedure. Write its
`PROVENANCE.md`, then `./use-gen <name>`.

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

`compare.py` says *how much more*; `hotspots.py` says *where*. Same two backends, same cache: a block
one has measured is never re-executed by the other.

### One build, one or more blocks

```bash
./hotspots.py profile --backend zisk \
    --elf ../guests/monad-variants/r3/monad-r3-zisk.elf \
    -i ../guests/zisk-reth/fixtures/1-25552053.bin \
    --out results/r3-25552053 --title "monad-r3 · 25552053"
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
    --elf-before ../guests/monad-variants/ab/ab2-no-kec2.elf \
    --elf-after  ../guests/monad-variants/r3/monad-r3-zisk.elf \
    -i ../guests/zisk-reth/fixtures/1-25552053.bin --out results/kec2-lever
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
./hotspots.py render --json results/r3-25552053/profile.json --out results/r3-25552053
```

Rebuilds the HTML from an existing `profile.json`. For template or `--meta` changes.

## The ranked levers document

```bash
./levers.py
```

Writes `results/levers.html` — what to fix in the Monad guest, ranked, with a re-measure protocol per
item. Requires `results/compare.json` carrying the `zisk` and `sp1` axes with **n ≥ 300**; it refuses
a partial run rather than build a plausible wrong document. Run the canonical report first.
`--out` writes elsewhere.

## The optimised-branch report

```bash
./optimized.py            # results/optimized-{zisk,sp1}.json
./compare-optimized.py    # results/compare-optimized.html
```

Neither measures anything: both read a **frozen measurement set** left under `results/` by the levers
campaign. Nothing regenerates it — the branch's ELFs are gone — and `results/` is git-ignored, so **on
a fresh clone these two have nothing to read**. Skip them, or bring the payload in yourself.

## Cross-guest work table

```bash
./results.py
```

No arguments. Reads every `../guests/<name>/inputs/*.exec-report.json` and writes
`results/results.html` — how much work each guest does per block, side by side.

## Inlining-robustness verdict

```bash
./inline-robust.py --verdict-only
```

Writes `results/inline-verdict.json` from profiles already in the cache — the optional correction
column `compare.py` and `levers.py` read. Without `--verdict-only`, `--elf <no-inline build>` collects
the profiles first (`--limit`, `--chunk`, `--guest`, `--emu` tune that pass).

## RTP end-to-end latency

```bash
./rtp-latency.py --manifest ~/witness-manifest.csv --results ../infra/zisk-infra/results \
    --submissions ../run-data/ethproofs-mock-data/submissions.jsonl --csv rtp-latency.csv
```

Joins the producer manifest, the prover run records and the submission log into one per-block latency
table. `--guest`, `--chain-id`, `--queue` select what to join; `--cross-clock` when producer and
prover do not share a clock.

---

## Monad witness generations

```bash
cd ../guests/monad
./use-gen                              # list generations, mark the current one, show each format
./use-gen offsettriedb-rework-2026-08  # select it and install its ELFs
./witness-fmt fixtures/<block>.witness # name a witness's wire format
```

`use-gen` refuses a generation whose witnesses are not one format throughout, and one with no ELFs.
Selecting a generation rewrites the two top-level ELFs, which are tracked — the switch shows up as a
modification to commit.

> Two generations can share a format word and still be incompatible. `witness-fmt` cannot separate
> them; the directory they sit in is what does. Never copy witnesses between generation directories,
> and read the target's `PROVENANCE.md` before measuring against it.

## Execute + verify state roots

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
