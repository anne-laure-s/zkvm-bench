# profiling

Three tools to analyze the guests' **execution** (no proving) — pick by the question you're asking:

| Tool | Question | Scope | Output |
|------|----------|-------|--------|
| **`compare.py`** | ***How much more*** does guest A cost than guest B, **over many blocks**? | **one same-zkVM pair**, N blocks | terminal summary + `results/compare.html` |
| **`hotspots.py`** | ***Where*** does the cost go? (drill into functions / opcodes / categories) | **one guest** (or several, compared) | per-run HTML — icicle + flamegraph |
| **`results.py`** | ***How much*** work per block? | **all guests**, side by side | one cross-zkVM table (`results/results.html`) |

Both read the per-guest artifacts in [`../guests/`](../guests/) and write self-contained HTML;
neither proves anything.

**Looking for the command to type? → [`RUNBOOK.md`](RUNBOOK.md).** It lists every workflow — the
canonical report, a one-block profile, the levers document, the latency table — with what each one
writes. This file explains *which* tool answers *which* question, and the framing rules behind the
numbers.

## Which tool for what — and what these are NOT

**Execution/profiling and proving are separate paths**; conflating them is the classic trap.

| Tool | Purpose | Reads | Produces |
|------|---------|-------|----------|
| `compare.py` | **aggregate A-vs-B over a block set** (median/mean/spread + optional module diff) | ELFs + inputs, per axis | terminal summary, `results/compare.html`, `--json` |
| `hotspots.py` | **execute + profile + compare** any guest, either backend (`zisk`/`sp1`) | an ELF + an input, **cache first** | per-run `profile.json` + HTML; `diff` / `compare` / `--aggregate` |
| `results.py` | cross-guest "how much" table | `../guests/<name>/inputs/*.exec-report.json` | `results/results.html` |
| `../guests/monad/ev.sh` | batch **execute + state-root verify**, **ZisK only** | `../guests/monad/inputs/*.witness` | `exec-verified.csv` (steps) + `execute-out/<tag>.bin` (framed input) |
| `../guests/monad-*/gen-inputs` | **proving only** — persist per-zkVM inputs for `cli/prove-farm` | Monad witnesses | `../guests/monad-*/inputs/1-<block>.bin` |
| `levers.py` | **what to fix in the Monad guest, ranked**, with a re-measure protocol per item | the profile cache + the ELF symbol table | its own report — *not* compare.py's, and it has a shelf life |
| `rtp-latency.py` | end-to-end **latency** of the RTP pipeline, joined per block | witness manifest + run records + mock submissions | latency table (`--csv`) |
| `axis.py` | **list, add, remove and retire** compare.py's axes (`prune` drops a campaign's ephemeral ones, `gc` drops those whose build was deleted), with the checks that catch a bad one before it measures | `compare.py`'s `AXES` + the inputs on disk | edits `AXES` in place; `list` / `show` print |
| [`series/`](RUNBOOK.md#series--one-elf-per-commit-of-a-lineage) | **each commit of a branch against its own predecessor** — one ELF built per commit, so a lineage reads as a curve and an inert commit is visible as one. Not an A-vs-B: `compare.py` prices a build against a rival, this prices a *branch against itself* | a monad worktree (it builds), the corpus, `<lineage>-index.tsv` + `<lineage>-measure.tsv` | `results/series-<lineage>.html`; driven end to end by `series/run-r10.sh` |

Three more produce an artifact that another tool reads, rather than a report of their own. They are
easy to mistake for optional extras, so: if the file they write is missing, the consumer silently
drops a column or a series — it does not fail.

| Tool | Writes | Read by |
|------|--------|---------|
| `inline-robust.py` | `results/inline-verdict.json` — how much of a family's cross-guest ratio is real vs. an inlining artefact | `compare.py`, `levers.py` (optional column) |
| `optimized.py` | `results/optimized-{zisk,sp1}.json`, derived from a **frozen measurement set** (below) | `compare-optimized.py` |
| `compare-optimized.py` | `results/compare-optimized.html` — the levers branch next to what ships | — (a deliverable) |

> ⚠️ Those two read a **frozen measurement set**, not a cache: nothing can regenerate it — there is no
> `levers-*` axis and no ELF left to run (see
> [`../guests/monad-variants/README.md`](../guests/monad-variants/README.md) § `levers/`). It lands
> under `results/`, which is git-ignored, so **a fresh clone has nothing for them to read and they
> cannot run there at all**. Everything downstream inherits that.

`series/` has the same shape of dependency, and it is worth knowing before opening the directory: the
scripts are tracked, everything they read and write is not (`series/elf/`, `<lineage>-index.tsv`,
`<lineage>-measure.tsv` are git-ignored). A fresh clone gets the method and no data — and unlike the
frozen set above, this one **is** regenerable: `run-r10.sh` rebuilds it from the branch, given a monad
worktree and hours.

Two axes running the same build share its measurements, with nothing to prime by hand: a slot is keyed
by the sha256 of the ELF, so the axis never enters the key (see [`cache-format.md`](cache-format.md)).

Anything under [`studies/`](studies/README.md) is a **one-shot**: a question that was asked once and
answered. They are kept because they are the method behind a number, not because they are meant to be
run again.

Things that bit us and are easy to forget:
- **To profile/compare a guest you do NOT need `gen-inputs`.** That is a `prove-farm` concern; `hotspots.py`
  runs the ELF directly on a witness. `gen-inputs` only *persists* a proving-input file.
- **`ev.sh` is ZisK-only** (it hardcodes the ZisK ELF + `ziskemu`). There is **no SP1 `ev.sh`** — to
  execute/profile Monad **on SP1**, use `hotspots.py --backend sp1` (recipe below), which drives
  `sp1-runner --mode execute`.
- **Monad is not in `results.py`'s table by default**: `ev.sh` writes `exec-verified.csv`, not
  `exec-report.json`. Its `steps` are extracted the *same way* as the ZisK runner's (so they are
  comparable to `zisk-reth`'s report), but for Monad comparisons reach for `hotspots.py`.
- **Input framing is per-backend** (the #1 gotcha): **SP1 reads the raw witness**; **ZisK needs it framed**
  `LE64(len)+witness+pad8`. See the recipe below.

## compare.py — how much more, over a whole block set

The one-command answer to *"is Monad's EVM more expensive than reth, and by how much?"*:

```sh
./compare.py                              # the default pair of axes, every common block
./compare.py --axis zisk --axis sp1       # name others explicitly
./compare.py --blocks 25552005-25552088   # an explicit range (or a comma list)
./compare.py --deep 5                     # + a per-module diff (hotspots.py)
```

A bare run does **`cur-zisk` + `cur-sp1`** — not every axis. Naming axes is how you get the others,
and the canonical report needs four of them; [`RUNBOOK.md`](RUNBOOK.md) has that command.

**Axes** are same-zkVM guest pairs (work-units only compare within a VM):
`zisk` = monad-zisk vs zisk-reth · `sp1` = monad-sp1 vs rsp. Input framing is handled per backend
(SP1 raw witness · ZisK `LE64(len)+witness+pad8`), so you just name blocks.

What the summary gives, per axis: median / mean / total work with the **ratio**, work **per Mgas**
(normalises for block size; EVM gas comes from the reth ZisK guest and is pooled across axes), median
exec seconds, then the **per-block ratio distribution** (median, geometric mean, arithmetic mean, p10,
p90, cv) with the min/max blocks named, and a **small-vs-large-block** split to show whether the gap
grows with block size. `--deep N` appends a per-module `hotspots.py diff` so you see *which* modules
carry the delta.

**Which average of the per-block ratios** — the report gives four, because they answer different
questions, and the HTML has a table (*which average of the per-block ratios*) laying them side by side
with the measured spread between them:

| statistic | what it answers |
| --- | --- |
| **geometric mean** | the average to quote for a *set of ratios*. Multiplicative, so 2× cancels 0.5× and A÷B is the exact reciprocal of B÷A. Its dispersion is `ratio_gsd`, read **×/÷**, not ± |
| **median** | the headline in the report's cards — robust to the off-pattern blocks the page lists by name |
| **arithmetic mean** | kept for continuity only. It depends on which guest you divide by: `ratio_mean` × `ratio_mean_inv` comes to 1.0026 (ZisK) and 1.0307 (SP1) where an average of ratios owes exactly 1. Far enough out, both directions read above 1 and each names the other guest as the dearer one — that does *not* happen on this data, so quote the product, not the warning |
| **total ÷ total** (pooled) | how much more work over the whole set — weighted by block size, so the largest blocks dominate |

Every one of them is in `results/compare.json` under `<axis>.summary` (`ratio_gmean`, `ratio_median`,
`ratio_mean`, `ratio_mean_inv`, `ratio_pooled`, `ratio_gsd`), and the same file carries the **per-block
raw values** under
`<axis>.blocks.<block>.{a,b}.work` — so any other statistic can be recomputed without re-running
anything. The rule of thumb: **geometric mean** when a single average of ratios is wanted, **median**
when robustness matters (it is the headline here because a minority of blocks run 256-bit curve
arithmetic in software and are not from the same population), and **never the arithmetic mean alone**.

The two rows that matter are the two sides of a settled argument, which is why the page carries both.
Fleming & Wallace (*How not to lie with statistics*, CACM 29(3), 1986) showed that averaging
**normalised** results arithmetically is invalid — the answer depends on which side you divide by —
and that the geometric mean is the one invariant to that choice. Smith (*Characterizing computer
performance with a single number*, CACM 31(10), 1988) answered that the geometric mean preserves no
**total**, so where the quantity of interest is total time or total work, the figure must be
proportional to the total consumed. Both are right about different questions: the geometric mean is
the `ratio` row, Smith's is the `total ÷ total` row.

Being the *correct* average does not make the geometric mean a *robust* one — it is the arithmetic
mean in log space, so an extreme ratio still moves it. Measured on this data: letting the two one-off
blocks `25229951` / `25229957` into the set (ratios **0.099×** and **0.164×** against a ~1.23× body)
moves the ZisK geometric mean **1.235× → 1.220×** and its `ratio_gsd` **1.052 → 1.192**, while the
median does not move at all. That is the case for keeping both on the page, and for bounding the range
(next section) rather than trusting whatever the fixtures happen to contain.

**Which blocks** — every block both sides can run, unless you bound it with `--block-min` / `--block-max`
(or name an explicit set with `--blocks`). Bound it deliberately: the fixtures mix a **contiguous run**
with **strided** (~every 10th block) and one-off older ones, and a median over that mixture is not a
median over consecutive mainnet blocks. At the time of writing the contiguous run where RSP, ZisK-reth
*and* Monad witnesses all exist is **25551991–25552607** — check what you actually have rather than
trusting that range:

```sh
./compare.py --axis zisk --block-min 25551991 --block-max 25552607
```

This is not optional for a report meant to sit beside an existing one. Every published report — the canonical
one (365 ZisK / 373 SP1 blocks) and the levers-branch one (504 / 365 / 373 / 504) — was produced with **`--block-min 25551991 --block-max 25552607`**. A bare `./compare.py` is a
*different run*: it also admits `25229951` and `25229957`, one-offs from ~322k blocks earlier that
still sit in `guests/monad/**`, so the block set silently stops matching the report next to it (see
the geometric-mean figures above for what that costs).

The **max** matters for the opposite reason: the RTP pipeline keeps minting witnesses at the tip, so
without it a re-run quietly widens the set and the report stops describing the same population as the
one beside it. Today the corpus ends below that max and it changes nothing — which is the point.

**One command does the whole thing.** A single run collects and reports work-units, prover work and its
category split, precompile counts, gas and tx counts, and the honest execution time:

```sh
./compare.py --block-min 25551991 --block-max 25552607
```

That writes the terminal summary **and** `results/compare.html` + `results/compare.json` — the report is
the deliverable, so it needs no flag (`--no-report` skips it).

**A restricted run does not write the canonical report:**

- `--limit` / `--blocks` measure a **sample**, so the summary statistics describe that sample and
  nothing else. Those runs write `compare-partial.*` instead, and say so.
- `--axis` measures **fewer axes**, not fewer blocks: the numbers are canonical, they just do not
  cover everything. `compare.json` is therefore **merged**: running one axis leaves the others in the
  file. The HTML still shows only the axes just run, and prints which axes the JSON
  holds beyond it.

Naming `--json` / `--html` yourself overrides all of this. `--quick` is the only other opt-out, and it drops just the expensive piece (ZisK's instrumented COST pass);
anything already cached is still reported.

Alongside the full report it also writes **`compare-summary.html` + `compare-summary.md`** — the
**one-page synthesis** (median gap in %, prover-work gap, stability, which work families carry the
delta), same numbers rendered from the same run so the two cannot disagree. The `.md` is made for
pasting into Slack/Notion; the full report stays the reference for methodology and per-block detail,
and each page links to the other.

To rebuild that synthesis from a run you already have — including an older one — point it at the
run's `--json` payload; it measures nothing, never loads the cache, and takes under a second:

```sh
./compare.py --summary-from results/compare.json          # -> compare-summary.{html,md}
# --axis then selects AND orders the sections (here: each backend's baseline pair, then its reference)
./compare.py --summary-from results/compare.json \
    --axis opt-self --axis opt-self-sp1 --axis opt-zisk --axis opt-sp1
```

The summary is named after the report it comes from, so pointing this at another run's payload writes
beside that run rather than over this one.

Re-renders a report from `--json` output without touching the measurements. A plain re-run is cheap
too: the cache is read per block, on demand, so a fully-cached sweep is sub-second.

**Prover work** — beyond raw steps/cycles, each backend can report what a block costs
the *prover*: **ZisK `COST`** and **SP1 `PGU`**. Both are **trace area** — the polynomial area the prover
commits to — so they weight each operation by its real proving cost (a keccak precompile ≫ an ADD), and
proving time is proportional to them on any hardware:

| | what it is | source |
|---|---|---|
| ZisK `COST` | per-op *columns × rows*, e.g. `KECCAK_COST = 25 × 3022` | `zisk core/src/zisk_ops_costs.rs`, headed *“Cost definitions: Area x Op”* |
| SP1 `PGU` | trace area accumulated by the executor's ShapeChecker, normalised ×10/191 for cross-version stability; also sets shard boundaries | `sp1-core-executor vm/shapes.rs` + `report.rs::gas()` |

PGU is what SP1's prover network prices proofs in, but it is **not GPU-specific** — it's hardware-agnostic
trace area. The two models describe different circuits at different scales, so compare the **A/B ratio**
within an axis, never raw COST against raw PGU. Cost note: SP1 reports PGU for free, ZisK needs a second
**instrumented** pass whose execution is ~7× slower (~19 s/block vs ~2 s → roughly 10× the sweep). It runs
by default and is cached; `--quick` skips it when you only want a fast look.

**Diagnosing a shape from the page** — in the HTML the histogram bars are **clickable**: each opens the
blocks that landed in it, with a synthesis and a per-block table. The point is to answer *why* a cluster
exists without leaving the report. What it shows, and where each figure comes from:

| | what it tells you | cost |
|---|---|---|
| gas, **tx count**, gas/tx | is this cluster made of big blocks, or many cheap txs? | free — the reth ZisK guest prints both |
| work/Mgas per side | ratio with block size divided out, so buckets group by *efficiency* | derived |
| **precompile counts A÷B** (SP1: keccak, secp256k1, all syscalls) | the mechanism: *“A hashes 1.9× more than B here”* | free — already in the SP1 report |
| **% opcodes / % precompiles** of trace cost, both axes | is the block precompile-bound or plain-execution-bound? Over the ZisK sample **% opcodes separates the off-pattern blocks ~3× better than anything else shown** | free — ZisK prints the split, SP1's is derived from the same weights `hotspots.py` uses |
| **witness bytes** per guest | the figure behind "each guest is fed its own witness" | free — `getsize` |

Clicking a row in the off-pattern list goes one level deeper: a **per-opcode delta against the median
block** for whichever guest strayed, computed in the page (no profiling), plus the ready-to-run
`hotspots.py` command for function-level detail.

Caveat: precompile counts belong to each zkVM's own syscall set, so they compare **within** an axis, not
across. Deliberately left out: `touched_memory_addresses` (always 0) and anything needing an RPC (it would
break the page's self-containment). Note that SP1's `DIV`/`REM` counters stay tiny even on blocks dominated
by 256-bit modular arithmetic — that arithmetic is done in *software* (shifts/branches), so look at those
groups instead. Collecting all this needs one re-run per axis; afterwards it's cached.

**What kind of work, and how much** — the HTML breaks each guest's instructions into families
(hashing, 256-bit arithmetic, state/trie, EVM interpreter, containers/runtime, memory) by classifying
**function names**, since the two guests share no module names. It needs one profiling run per guest
per sampled block (~13 s ZisK, ~24 s SP1), cached until the guest ELF changes; `--families N` sets the
sample size (default 50), `--families 0` skips it; profiles are cached **per block**, so raising N
only profiles the blocks you add.

The sample is **stratified**: one block from the middle of each of N equal-population slices of the
ratio distribution. That matters more than N does — sampling the endpoints (0–100 %) gives the two most
extreme blocks 1/N of the weight each though they stand for one block in hundreds, which inflated the
256-bit-arithmetic family 2.5× (146 M vs 59 M instructions); clustering near the median makes the
mirror mistake, dropping tails that carry real work. Stratified is unbiased for the mean and steadier
than a random draw of the same size. On sample size, measured rather than assumed: 5 → 10 moved every family by
1–4 % except the EVM-interpreter row (−17 %), and 10 → 50 moved the C++ family from 13.6× to 5.11× on SP1 — a small sample was
not settled, so **50 is the default**. Profiles are cached per block, so the cost is paid once.

Note the two profilers differ in kind: ZisK's attributes essentially every instruction, SP1's **samples**
(1 in ~270 measured), so its family counts are scaled to the real cycle count. Ratios survive sampling;
absolute figures on that axis are estimates, and the report says so.

**Representative blocks, and why there is no "median flamegraph"** — the summary names the **real**
blocks at the median (the typical one), p10 and p90 of the ratio. It deliberately does *not* build a
synthetic median/decile profile: averaging is linear, so a mean-per-function profile still sums to the
mean total (that's what `hotspots.py --aggregate` gives you, plus a per-function `cv` for spread) —
but a **median is not linear**, so a profile whose every function carries its cross-block median sums
to no real block's total, breaks parent ≥ children, and corresponds to no execution that ever ran. A
real block at the quantile is honest and openable.

`--spread` is the sound version of "show me a decile profile": it profiles the **real p90 and p10
blocks** and diffs them, answering *what makes a block relatively costly*. Both sides are runs that
actually happened. Caveat printed with the output: those are quantiles of the **ratio**, not of block
size, so read the normalised `Δ%oftot` column rather than raw `Δcount`.

**Outliers** — blocks that don't behave like the rest are listed automatically, detected on the
per-block ratio with a **robust z-score** (median + MAD, not mean + stdev: a few odd blocks would
inflate stdev and hide themselves). Default bar `|z| ≥ 3.5` (`--outlier-z`), 8 shown (`--show-outliers`,
all land in `--json`, and the HTML flags them ⚠ with their z). Each line carries the block's gas and
both work counts, plus the `hotspots.py` command to open one — they're the blocks worth a deep dive.

Runs are **cached** per block, keyed by the sha256 of the ELF and of the input
([`cache-format.md`](cache-format.md)), so re-runs are instant and block sets grow incrementally;
`--force` re-runs. `hotspots.py` reads and writes the same cache, and writes the projection
`compare.py` reads — so a block profiled by one is never re-executed by the other. The cache is written **as results arrive**, so an interrupted sweep keeps
everything it already computed. A rebuilt guest or a re-minted witness changes its hash and is
re-measured on its own — nothing has to be invalidated by hand.

> ⏱ **The SP1 startup cost, and how it's amortised.** The SP1 runner pays a **fixed ~6.3 s startup per
> process** — building the `ProverClient`, before its own timer even starts. Measured with a
> 5 297-cycle fibonacci guest: **6.31 s wall for 0.006 s of execution** (~59 s of CPU across cores). So
> the report's `elapsed_secs` (what compare.py records) is the honest execution number, while naive
> wall-clock per block is ~90 % startup. A real block: 7.31 s wall / 0.67 s executing, vs ZisK 0.68 s /
> 0.08 s — SP1 is ~3× slower *per work-unit*, not 10×.
>
> Because that cost is per-PROCESS, not per-input, `sp1-runner` grew a **`--batch`** mode (a file of
> input paths, one per line, `--report-dir` for the per-input JSON) and compare.py chunks each side's
> blocks into `--jobs` batched processes. A 748-run sweep goes from 748 startups to ~6 — about **78 min
> of pure startup removed** — while still running chunks in parallel. If the runner is older than
> `--batch`, compare.py detects it and falls back to one process per block with a note.
>
> ⏱ **The reported exec time is the `--no-gas` one.** SP1's gas-estimation pass inflates execution
> (~1.7× here), but switching it off makes the report return `cycles = 0` — so each SP1 chunk is run
> **twice**: gas-on for cycles/PGU/precompile counts, `--no-gas` for the honest timing. The report shows
> the latter and states the measured overhead of the former. The second pass is the cheap one, and the
> startup is amortised across the batch either way.

## hotspots.py — where the cost goes
```sh
./hotspots.py profile --backend zisk --elf ../guests/zisk-reth/zisk-reth.elf \
    -i ../guests/zisk-reth/inputs/<tag>.bin --out results/reth
#  -> results/reth/index.html   (open in a browser)
```
Compare several guests in one page: profile each with a distinct `--tab-prefix`, then
`render --json a.json --json b.json`. Three more sub-commands:
- **`diff`** — per-module / per-function delta between two profiles (`diff --json A --json B`) — e.g.
  Monad vs reth on the same zkVM: *where* one guest spends more trace.
- **`compare`** — the before/after tool for a guest change: profiles the SAME inputs through two ELFs
  and prints the diff in one shot (`compare --backend sp1 --elf-before OLD --elf-after NEW -i …`).
- **`profile --aggregate`** — fold many blocks into one mean-per-block profile (+ per-function `cv`),
  instead of one tab each.

### Compare two guests on one zkVM (e.g. Monad vs reth)

Work-units compare **only within a zkVM** (SP1 cycles ≠ ZisK steps), so compare **same-backend pairs**:
`monad-sp1` vs `rsp` on **SP1**, `monad-zisk` vs `zisk-reth` on **ZisK**. Profile each side, then `diff`.
(This is the *Monad-EVM vs reth* comparison: same block, same zkVM, two guest programs.)

**SP1 axis** — SP1 reads the **raw** witness (no framing); point `-i` straight at the `.witness`:
```sh
./hotspots.py profile --backend sp1 --elf ../guests/monad-sp1/monad-sp1.elf \
    -i ../guests/monad/fixtures/<block>.witness --out results/mon-sp1 --tab-prefix monad
./hotspots.py profile --backend sp1 --elf ../guests/rsp/rsp.elf \
    -i ../guests/rsp/fixtures/1-<block>.bin       --out results/rsp-sp1 --tab-prefix rsp
./hotspots.py diff --json results/mon-sp1/*.json --json results/rsp-sp1/*.json
```
Requires `sp1-runner` built `--features profiling` (hotspots.py tells you if it's missing) — a *second*
build of the crate, and `--no-default-features` is part of it:
```sh
cd ../infra/sp1-infra/sp1-runner
cargo build --release --no-default-features --features profiling --target-dir target-prof
```
Every prerequisite, with its install command: [`RUNBOOK.md`](RUNBOOK.md#prerequisites).

**ZisK axis** — ZisK needs the **framed** input `LE64(len)+witness+pad8`. Frame it once (or reuse
`../guests/monad-zisk/gen-inputs` output, or `ev.sh`'s `execute-out/<tag>.bin`):
```sh
python3 -c "import struct;d=open('../guests/monad/fixtures/<block>.witness','rb').read();open('/tmp/m.zisk.bin','wb').write(struct.pack('<Q',len(d))+d+b'\x00'*((-(8+len(d)))%8))"
./hotspots.py profile --backend zisk --elf ../guests/monad-zisk/monad-zisk.elf \
    -i /tmp/m.zisk.bin --out results/mon-zisk --tab-prefix monad
./hotspots.py profile --backend zisk --elf ../guests/zisk-reth/zisk-reth.elf \
    -i ../guests/zisk-reth/inputs/1-<block>.bin --out results/reth-zisk --tab-prefix reth
./hotspots.py diff --json results/mon-zisk/*.json --json results/reth-zisk/*.json
```
For **many blocks** (e.g. the whole dense zone), pass several `-i` with `--aggregate` instead of one
input each → a mean-per-block profile, and `diff` the two aggregates. Historical single-block numbers +
findings live in [`../guests/monad/README.md`](../guests/monad/README.md).

Full manual — backends (`zisk`/`sp1`), options, what **COST** measures, the flat-icicle-vs-flamegraph
story, multi-guest compare, `diff` / `compare` / `--aggregate`, adding a backend: **[hotspots.md](hotspots.md)**.

## results — how much work per block
```sh
./results.py                        # -> results/results.html
./results.py --snapshot snap.json   # + dump {stack:{commit,blocks}} for regression tracking
./results.py --baseline snap.json   # diff current vs a snapshot (exit 1 on a SAME-commit change)
```
Reads each guest's `../guests/<name>/inputs/*.exec-report.json` and builds the cross-zkVM table. The
work-units are **deterministic** (reproducible on any machine) but **not comparable across zkVMs**
(SP1 cycles ≠ ZisK steps ≠ OpenVM instructions) — the report says so, and proving times are *not*
aggregated (run/box/tuning-dependent). See [`../cli/report-schema.md`](../cli/report-schema.md).

Also per block: **gas** (from the RSP report) and a **work-unit/gas** lens — blocks above 1.5× a
stack's median ratio are flagged `‡` (costly to prove relative to their gas). Each column notes the
**ELF commit** its reports were built from (`commit` field); a guest whose reports mix commits triggers
a warning — work-units only compare within one ELF version. `--baseline` uses that same commit to tell
an expected ELF-bump change from a real determinism regression (exit 1 only on the latter → CI-friendly).

## Also here
- [RUNBOOK.md](RUNBOOK.md) — every workflow as a command, and what it writes.
- `axis.py` — declare an axis without editing `compare.py` by hand. It refuses an ELF that is not
  there, a name already taken and an unknown `src`, and it prints how many blocks the axis resolves
  on **before** you measure — the difference between an expected `n` and an empty one nobody noticed.
  It still rewrites `compare.py`, which is **tracked**: an axis is a source change and lands in
  `git status` ([RUNBOOK.md](RUNBOOK.md#3-declare-the-axis) says what to do with that diff).
- [cache-format.md](cache-format.md) — the profile cache: layout, identity, what is not stored in it.
- **The correction that changes a conclusion**, kept here because it belongs to the numbers this
  directory produces: the 68 SP1 blocks where Monad looks cheaper are blocks where `rsp` runs BN254 in
  software — Monad is flat there and `rsp` costs 1.5× more. Engine-to-engine the SP1 median is
  **1.263×**, not 1.221×. `rsp` is the only one of the four guests with **no** precompile-backed BN254
  crate (0 % vs `monad-sp1`'s `zkvm_bn254_g1_mul`, same emulator) — a missing patch, not a limit of SP1.
- The ZisK profile is **flat** by a heuristic limit, not a frame-pointer issue; the experimental way
  out is `ziskemu-callstack.patch`, tracked here. See [hotspots.md](hotspots.md).
- [studies/](studies/README.md) — one-shot methodology checks, kept as the method behind a number.
- `template.html` — the shared hotspots renderer. **All generated output goes under `results/`**
  (git-ignored): the aggregated `results.html`, per-guest hotspots profiles (`results/<name>/`), and sample runs.
