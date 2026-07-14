# hotspots

A **prover-agnostic** hotspot profiler for zkVM guests. It renders an interactive
**hotspot icicle** (module → function, width = share of executed instructions), an
**interactive call-tree flamegraph** (sp1; click-to-zoom), plus the cost-category and
top-opcode breakdowns, as a single self-contained HTML page.

> Part of [`profiling/`](README.md) — the sibling **`results`** tool does the complementary view
> (*how much* work per block, cross-zkVM). This file is the `hotspots.py` manual.

The design is split so it can grow beyond ZisK:
- a **backend** collects a profile into a common JSON schema;
- one **shared renderer** (`template.html`) turns any backend's JSON into the same report.

**Backends:** `zisk` (via `ziskemu`), `sp1` (via `sp1-runner` built `--features profiling`).

Module names are extracted for Rust **and** C++ symbols, so the ZisK `reth` guest (Rust)
and the Monad `monad-execution` guest (C++) both render out of the box.

## Prerequisites
- **zisk backend:** `ziskemu` on PATH (installed by `ziskup` into `~/.zisk/bin/`; runs on macOS too).
- A guest ELF (ELF64 · RISC-V · entry `0x80000000` for ZisK).
- Framed inputs: `LE64(len) || payload || zero-pad to a multiple of 8` (same format
  `cargo-zisk prove -i` expects). Raw `.witness` files must be framed first.

## Usage

```sh
# profile one or more inputs through a guest ELF  ->  <out>/profile.json + <out>/index.html
./hotspots.py profile --backend zisk \
    --elf ../guests/zisk-reth/zisk-reth.elf \
    -i ../guests/zisk-reth/inputs/1-25229957.bin \
    -i ../guests/zisk-reth/inputs/1-25229951.bin \
    --out results/reth --title "reth guest — where the proving cost goes"

# Monad guest (C++), verifying each block's post-state root as it goes
./hotspots.py profile --backend zisk \
    --elf ../guests/monad/monad-zkvm-guest-zisk.elf \
    -i ../guests/monad/execute-out/1-25229957.bin -i ../guests/monad/execute-out/1-25229951.bin \
    --out results/monad --verify-roots ../guests/monad/inputs \
    --title "Monad guest — where the proving cost goes"

# SP1 backend — needs a profiling-enabled runner (one-time build):
#   cd ../infra/sp1-infra/sp1-runner && cargo build --release --no-default-features \
#       --features profiling --target-dir target-prof
# Inputs are RAW (.witness / rsp .bin) — NO LE64 framing (that's zisk-only).
./hotspots.py profile --backend sp1 \
    --elf ../guests/monad/monad-zkvm-guest-sp1.elf \
    -i ../guests/monad/inputs/1-25229951.witness -i ../guests/monad/inputs/1-25229957.witness \
    --verify-roots ../guests/monad/inputs \
    --out results/monad-sp1 --title "Monad guest on SP1 — where the proving cost goes"
# RSP (reth) on SP1:
./hotspots.py profile --backend sp1 --elf ../guests/rsp/rsp.elf \
    -i ../guests/rsp/inputs/1-25229951.bin -i ../guests/rsp/inputs/1-25229957.bin \
    --out results/rsp-sp1 --title "RSP on SP1"
# [sp1] extra flags: --runner <path> (default target-prof/…), --sample-rate N
#   (TRACE_SAMPLE_RATE cycles/sample, default 200), --costs <rv64im_costs.json>

# re-render the HTML from an existing profile.json (fast; design tweaks, --meta, --labels)
./hotspots.py render --json results/reth/profile.json --out results/reth --title "reth guest"
```

Open `<out>/index.html` in a browser.

### Options
| flag | meaning |
|------|---------|
| `--backend zisk` | profile collector (default `zisk`; `sp1` also available) |
| `-i/--input` | framed input (repeatable → one tab per input) |
| `--verify-roots DIR` | *(zisk)* compare `output[:32]` to `DIR/<tag>.post_state_root`, show a ✓ badge |
| `--top N` | functions named individually (default **200**); each module's remainder aggregates into a hatched per-module tail. Higher N → smaller tails (reth: top-200 ≈ 0.1% tail; top-60 ≈ 6%). Cost is only a bigger JSON (~20 KB/tab at 200) — the disasm already holds every function. A global top-N beats a per-module cap here: big modules (revm/monad) have long *hot* tails that a per-module cap would truncate. |
| `--title / --subtitle / --eyebrow` | header copy (`--subtitle` accepts inline HTML) |
| `--labels '{"tag":"label"}'` | custom tab labels (JSON literal or a path to a `.json`) |
| `--meta '{"tag":{...}}'` | merge extra block info into a tab's cards — see below (JSON literal or path) |
| `--name FILE` | HTML filename (default `index.html`) |
| `--emu PATH` | *(zisk)* ziskemu path (default `~/.zisk/bin/ziskemu`) |
| `--tab-prefix STR` | *(profile)* prefix every tab key → namespaces a guest so several profiles merge without colliding |
| `--json` *(render, repeatable)* | `render --json a --json b` merges several guests' profiles into one report |

### Comparing multiple guests
`profile` takes a single `--elf`, so profile each guest on its own, then `render` **merges** their
`profile.json` files (`--json` is repeatable). Give each guest a `--tab-prefix` so same-block tabs
don't collide — the tab key is the input filename, and the **sp1** backend strips a leading `1-`, so
RSP's `1-25229957` and Monad's `25229957` would otherwise land on the same key:

```sh
./hotspots.py profile --backend sp1 --elf ../guests/rsp/rsp.elf \
    -i ../guests/rsp/inputs/1-25229957.bin --tab-prefix rsp- --out results/rsp
./hotspots.py profile --backend sp1 --elf ../guests/monad/monad-zkvm-guest-sp1.elf \
    -i ../guests/monad/inputs/1-25229957.witness --verify-roots ../guests/monad/inputs \
    --tab-prefix monad- --out results/monad

./hotspots.py render --json results/rsp/profile.json --json results/monad/profile.json \
    --out results/compare --name rsp-vs-monad.html
```
`render` **errors** (rather than silently dropping a tab) if two profiles share a key — re-run the
colliding guest with a different `--tab-prefix`.

#### `--meta` — add block info a guest doesn't print
Cards are driven by each tab's `meta`. The reth guest prints `txs` / `gas` / block `hash`
to stdout (auto-picked up); the Monad guest prints none of them. Since two guests running
the **same block** share the same block facts, `--meta` lets you fill the gaps so both
tabs show the same cards. Recognized fields: `txs`, `gas`, `hash`, `root`, `root_ok`
(`cost` / `steps` / `exec` stay per-guest and are never overwritten).

```sh
./hotspots.py render --json results/rsp/profile.json --json results/monad/profile.json \
  --out results/compare --name rsp-vs-monad.html \
  --meta '{"rsp-25229957":{"txs":107,"gas":9768139,"hash":"0x335f70…","root":"0xcd7eb6…","root_ok":true},
           "monad-25229957":{"txs":107,"gas":9768139,"hash":"0x335f70…","root":"0xcd7eb6…","root_ok":true}}'
```

## What it measures
- **COST (proving cost)** = the backend's model of the **total STARK trace size** the
  proof must commit to — the sum, over everything executed, of how many trace rows/cells
  each operation contributes across the AIRs (Main, Binary, Arith, Keccakf, Memory, …).
  For ZisK, per-op weights live in `emulator/src/emu_costs.rs` (`.cost()` per opcode; mem
  read = 16 cells, a `keccakf` precompile = hundreds). Units are trace cells, **not** time
  — but since STARK proving work is ~proportional to trace size, COST is a
  **hardware-independent proxy for proving time**, far more faithful than raw steps.
  Categories: **Base** (fixed const-tree overhead, ~constant) / **Main** (∝ steps) /
  **Opcodes** (secondary-AIR rows) / **Precompiles** (keccak/sha/… — usually the biggest
  lever; `keccak` alone is ~20% despite a tiny share of step *count*) / **Memory**.
- **Hotspot icicle** = each executed instruction attributed to its enclosing function
  symbol, grouped by module.

## Call tree — flamegraph (sp1) vs flat-only (zisk)
The **sp1** backend emits a real call-tree (`tree` field) reconstructed from the Gecko
trace's per-sample stacks, and the report renders it as an **interactive flamegraph**
(bottom = entry, width = share of samples, **click a frame to zoom**, breadcrumb to zoom out)
below the flat icicle. A backend that omits `tree` (currently **zisk**) simply shows no
flamegraph panel.

### zisk stays flat — why
For zisk this is a **flat** per-function hotspot (icicle grouped by module), *not* a call-stack
flamegraph. ziskemu's native call-stack profiler (`--profiler-output`, Firefox-Profiler format)
reconstructs frames with an **`ra`-based heuristic** that (1) hard-disables at the first
call/return mismatch and (2) — even with that removed — does **not nest** for optimized guests
(reth/Monad come out depth-1). It is **not** a frame-pointer problem, and a guest rebuild with
frame pointers alone does nothing (ziskemu has no FP unwinder). A genuine deep call graph is real
profiler work, not a quick patch. Full investigation, the measured result, a starter patch
(`ziskemu-callstack.patch`), and the options are in **[CALLGRAPH-NOTES.md](CALLGRAPH-NOTES.md)**.

## Adding a backend (e.g. SP1)
Write `profile_<name>(args) -> dict` producing the common schema below, and register it in
`BACKENDS` in `hotspots.py`. The renderer, module extraction, `--meta` / `--labels` and CLI
are already shared — a new backend only implements *collection*.

```jsonc
{ "<tag>": {
    "meta": {"steps":…, "cost":…, "emu":…, "txs":…, "gas":…, "hash":…, "root":…, "root_ok":…},
    "total_count": <int>,                          // total attributed instructions
    "functions": [{"name":…, "module":…, "count":…}, …],  // top-N, hottest first (flat icicle)
    "categories": [{"name":…, "cost":…, "pct":…}, …],
    "opcodes":    [{"name":…, "cost":…, "pct":…}, …],
    "tree": {"name":…,"module":…,"value":…,"children":[…]} }}  // OPTIONAL — enables the flamegraph
```
The `sp1` backend (implemented) drives the SP1 executor: the Gecko trace (`TRACE_FILE`,
needs a `--features profiling` runner) gives `functions` + `tree`; the ExecutionReport's
opcode/syscall counts × `rv64im_costs.json` give `categories`/`opcodes`. `tree` is optional —
omit it and the flamegraph panel just doesn't render.

## Files
- `hotspots.py` — CLI + shared renderer + `zisk` backend.
- `template.html` — the report design (edit, then `render` to apply; placeholders
  `__DATA__` / `__CFG__` filled at generation time).

Sample outputs live under `results/prof/`: `prof_data.json` (reth), `monad_prof_data.json`
(Monad), `compare_profile.json` (reth vs Monad), and rendered `*.html`.
