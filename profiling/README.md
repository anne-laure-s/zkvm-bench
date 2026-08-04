# profiling

Three tools to analyze the guests' **execution** (no proving) — pick by the question you're asking:

| Tool | Question | Scope | Output |
|------|----------|-------|--------|
| **`compare.py`** | ***How much more*** does guest A cost than guest B, **over many blocks**? | **one same-zkVM pair**, N blocks | terminal summary + `results/compare.html` |
| **`hotspots.py`** | ***Where*** does the cost go? (drill into functions / opcodes / categories) | **one guest** (or several, compared) | per-run HTML — icicle + flamegraph |
| **`results.py`** | ***How much*** work per block? | **all guests**, side by side | one cross-zkVM table (`results/results.html`) |

Both read the per-guest artifacts in [`../guests/`](../guests/) and write self-contained HTML;
neither proves anything.

## Which tool for what — and what these are NOT

**Execution/profiling and proving are separate paths**; conflating them is the classic trap.

| Tool | Purpose | Reads | Produces |
|------|---------|-------|----------|
| `compare.py` | **aggregate A-vs-B over a block set** (median/mean/spread + optional module diff) | ELFs + inputs, per axis | terminal summary, `results/compare.html`, `--json` |
| `hotspots.py` | **execute + profile + compare** any guest, either backend (`zisk`/`sp1`) | an ELF + an input | per-run `profile.json` + HTML; `diff` / `compare` / `--aggregate` |
| `results.py` | cross-guest "how much" table | `../guests/<name>/inputs/*.exec-report.json` | `results/results.html` |
| `../guests/monad/ev.sh` | batch **execute + state-root verify**, **ZisK only** | `../guests/monad/inputs/*.witness` | `exec-verified.csv` (steps) + `execute-out/<tag>.bin` (framed input) |
| `../guests/monad-*/gen-inputs` | **proving only** — persist per-zkVM inputs for `cli/prove-farm` | Monad witnesses | `../guests/monad-*/inputs/1-<block>.bin` |

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

The one-command answer to *"is Monad's EVM more expensive than reth, and by how much?"* — run it
with no arguments and it does every axis over every block both sides can run:

```sh
./compare.py                              # every axis, every common block
./compare.py --axis zisk --limit 20       # one axis, first 20 common blocks
./compare.py --blocks 25552005-25552088   # an explicit range (or a comma list)
./compare.py --deep 5                     # + a per-module diff (hotspots.py)
```

**Axes** are same-zkVM guest pairs (work-units only compare within a VM):
`zisk` = monad-zisk vs zisk-reth · `sp1` = monad-sp1 vs rsp. Input framing is handled per backend
(SP1 raw witness · ZisK `LE64(len)+witness+pad8`), so you just name blocks.

What the summary gives, per axis: median / mean / total work with the **ratio**, work **per Mgas**
(normalises for block size; EVM gas comes from the reth ZisK guest and is pooled across axes), median
exec seconds, then the **per-block ratio distribution** (median, p10, p90, cv) with the min/max blocks
named, and a **small-vs-large-block** split to show whether the gap grows with block size. `--deep N`
appends a per-module `hotspots.py diff` so you see *which* modules carry the delta.

**Which blocks** — every block both sides can run, unless you bound it with `--block-min` / `--block-max`
(or name an explicit set with `--blocks`). Bound it deliberately: the fixtures mix a **contiguous run**
with **strided** (~every 10th block) and one-off older ones, and a median over that mixture is not a
median over consecutive mainnet blocks. At the time of writing the contiguous run where RSP, ZisK-reth
*and* Monad witnesses all exist is **25551991–25552607** — check what you actually have rather than
trusting that range:

```sh
./compare.py --axis zisk --block-min 25551991 --block-max 25552607
```

**One command does the whole thing.** A single run collects and reports work-units, prover work and its
category split, precompile counts, gas and tx counts, and the honest execution time:

```sh
./compare.py --block-min 25551991 --block-max 25552607
```

That writes the terminal summary **and** `results/compare.html` + `results/compare.json` — the report is
the deliverable, so it needs no flag (`--no-report` skips it). `--quick` is the only other opt-out, and it drops just the expensive piece (ZisK's instrumented COST pass);
anything already cached is still reported.

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

Runs are **cached** (`results/compare-cache.json`, keyed by axis/guest/block + ELF mtime), so re-runs
are instant and block sets grow incrementally; `--force` re-runs. The cache is written **as results
arrive**, so an interrupted sweep keeps everything it already computed.

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
Requires `sp1-runner` built `--features profiling` (hotspots.py tells you if it's missing):
`cd ../infra/sp1-infra/sp1-runner && cargo build --release --features profiling --target-dir target-prof`.

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
- [FINDINGS.md](FINDINGS.md) — **internal**, not a deliverable and never linked from one: the
  Monad-vs-reth results, and the
  62 traps that each produced a plausible wrong answer (gas-pass reordering the guests, taxonomy
  asymmetry inflating a ratio 9×, endpoint sampling, sampled-vs-counted profilers, silent cache misses,
  two unlabelled ratio statistics shown side by side, a non-contiguous span described as contiguous…).
  **The one that changes a conclusion:** the 68 SP1 blocks where Monad looks cheaper are blocks where
  `rsp` runs BN254 in software — Monad is flat there and `rsp` costs 1.5× more. Engine-to-engine the
  SP1 median is **1.263×**, not 1.221×. `rsp` is the only one of the four guests with **no**
  precompile-backed BN254 crate (0 % vs `monad-sp1`'s `zkvm_bn254_g1_mul`, same emulator) — a missing
  patch, not a limit of SP1.
- [CALLGRAPH-NOTES.md](CALLGRAPH-NOTES.md) — why the zisk profile is **flat** (a heuristic limit, not
  a frame-pointer issue) + the experimental `ziskemu-callstack.patch`.
- `template.html` — the shared hotspots renderer. **All generated output goes under `results/`**
  (git-ignored): the aggregated `results.html`, per-guest hotspots profiles (`results/<name>/`), and sample runs.
