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
`render --json a.json --json b.json`. Full manual — backends (`zisk`/`sp1`), options, what **COST**
measures, the flat-icicle-vs-flamegraph story, multi-guest compare, adding a backend:
**[hotspots.md](hotspots.md)**.

## results — how much work per block
```sh
./results.py         # -> results/results.html
```
Reads each guest's `../guests/<name>/inputs/*.exec-report.json` and builds the cross-zkVM table. The
work-units are **deterministic** (reproducible on any machine) but **not comparable across zkVMs**
(SP1 cycles ≠ ZisK steps ≠ OpenVM instructions) — the report says so, and proving times are *not*
aggregated (run/box/tuning-dependent). See [`../cli/report-schema.md`](../cli/report-schema.md).

## Also here
- [CALLGRAPH-NOTES.md](CALLGRAPH-NOTES.md) — why the zisk profile is **flat** (a heuristic limit, not
  a frame-pointer issue) + the experimental `ziskemu-callstack.patch`.
- `template.html` — the shared hotspots renderer. **All generated output goes under `results/`**
  (git-ignored): the aggregated `results.html`, per-guest hotspots profiles (`results/<name>/`), and sample runs.
