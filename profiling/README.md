# profiling

Two tools to analyze the guests' **execution** (no proving) — pick by the question you're asking:

| Tool | Question | Scope | Output |
|------|----------|-------|--------|
| **`hotspots.py`** | ***Where*** does the cost go? (drill into functions / opcodes / categories) | **one guest** (or several, compared) | per-run HTML — icicle + flamegraph |
| **`results.py`** | ***How much*** work per block? | **all guests**, side by side | one cross-zkVM table (`results/results.html`) |

Both read the per-guest artifacts in [`../guests/`](../guests/) and write self-contained HTML;
neither proves anything.

## Which tool for what — and what these are NOT

**Execution/profiling and proving are separate paths**; conflating them is the classic trap.

| Tool | Purpose | Reads | Produces |
|------|---------|-------|----------|
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
- [CALLGRAPH-NOTES.md](CALLGRAPH-NOTES.md) — why the zisk profile is **flat** (a heuristic limit, not
  a frame-pointer issue) + the experimental `ziskemu-callstack.patch`.
- `template.html` — the shared hotspots renderer. **All generated output goes under `results/`**
  (git-ignored): the aggregated `results.html`, per-guest hotspots profiles (`results/<name>/`), and sample runs.
