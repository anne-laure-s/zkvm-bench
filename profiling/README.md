# profiling

Two tools to analyze the guests' **execution** (no proving) — pick by the question you're asking:

| Tool | Question | Scope | Output |
|------|----------|-------|--------|
| **`hotspots.py`** | ***Where*** does the cost go? (drill into functions / opcodes / categories) | **one guest** (or several, compared) | per-run HTML — icicle + flamegraph |
| **`results.py`** | ***How much*** work per block? | **all guests**, side by side | one cross-zkVM table (`results/results.html`) |

Both read the per-guest artifacts in [`../guests/`](../guests/) and write self-contained HTML;
neither proves anything.

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
