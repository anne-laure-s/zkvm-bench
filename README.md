# zkvm-bench

Apples-to-apples benchmark of **Ethereum** (and **Monad**) block proving across zkVMs —
**SP1**, **OpenVM**, **ZisK** — on the same blocks and the same class of GPU box.

## What it measures

Two kinds of number, both per block:

- **Work-units** — SP1 `cycles`, ZisK `steps`, OpenVM `instructions`: the deterministic amount of work
  a guest does (a function of block × ELF, reproducible on any machine). They are **not comparable
  across zkVMs** — different VMs, so SP1 cycles ≠ ZisK steps ≠ OpenVM instructions. Compare *within* a
  zkVM, or as a ratio (e.g. Monad vs reth on the **same** zkVM).
- **Proving wall-clock** — how long a proof takes on a **fixed class of GPU box** (16× / 8× RTX 5090).
  This is the cross-zkVM comparison, but it's run-, box- and tuning-dependent, so it's always reported
  with its run context, never as a single headline number.

So "apples-to-apples" means **same blocks, same box class, same proof mode** (recursive/compressed
STARK) — not that the raw work-units are interchangeable. The shared report contract is
[`cli/report-schema.md`](cli/report-schema.md); the reports live in [`profiling/`](profiling/).

## Layout

| Dir | What |
|-----|------|
| **`guests/<name>/`** | Per-guest artifacts (shared, prover-agnostic): compiled `<name>.elf` + `inputs/`. Guests: `rsp`, `fibonacci` (SP1 example), `zisk-reth`, `openvm-reth` (+ the special `monad`, below). |
| **`infra/<stack>-infra/`** | Tooling per zkVM (`sp1-infra` · `zisk-infra` · `openvm-infra`): the `./run` dispatcher, `scripts/`, `cluster/` (on-box multi-GPU proving), the runner, `docs/`, and each guest's **recipe** (`<stack>-infra/guests/<name>/guest.sh`). Artifacts resolve from `../../guests/`. |
| **`guests/monad/`** | The special **Monad** guest — block-replay ELFs (SP1 + ZisK) + `ev.sh` (execute-and-verify) + witnesses, for the cross-zkVM **execution**-time comparison. See `guests/monad/README.md`. |
| **`profiling/`** | Execution-analysis tools: **`hotspots.py`** (prover-agnostic hotspot profiler — *where* the cost goes) and **`results.py`** (cross-zkVM work-unit report → `results/results.html` — *how much* work per block). |
| **`vendor/`** | Upstream clones (Axiom / Succinct / Polygon): `openvm-eth`, `rsp`, `zisk-eth-client` — the witness/guest **sources** from which the ELFs are built. Not this project's code; do not reorganize. |
| **`cli/`** | Guest-agnostic driver CLIs, run from the repo root — `cli/gen-elf` · `cli/gen-witness` · `cli/execute` (each `--guest <name>`, `--list`; delegate to the guest's stack — `zisk` witness and `monad-*` return a clear error). Also holds `guests.registry` + `reg.sh`, the single source of truth (each guest → stack, params, per-capability mode `elf`/`witness`/`exec`; add a guest = add a row), and `report-schema.md`, the shared `report.json` contract every runner emits. |

This is a single git repo. The only nested git repos are the upstream clones under `vendor/` (each
keeps its own `.git`, and `vendor/` is git-ignored). Build outputs and large regenerable inputs are
git-ignored per [`.gitignore`](.gitignore); everything else — `cli/`, `guests/`, `profiling/`, and all
three `infra/` — is versioned here.

## Requirements

What you need depends on what you're doing — and you only ever need the toolchain + `vendor/` clone
for the **stack(s) you actually use**, not all three:

- **Execute locally** (deterministic work-units + profiling — no proof, no GPU; runs on a laptop) — that
  stack's build toolchain and its upstream clone in `vendor/`:
  SP1 `cargo prove` + `rsp` · ZisK `cargo-zisk`/`ziskemu` + `zisk-eth-client` · OpenVM `cargo openvm` + `openvm-eth`.
- **Prove** (the actual benchmark) — additionally a **GPU box**: a Vast.ai multi-GPU instance (RTX 5090);
  SP1's cluster also needs **CUDA ≥ 13** (driver ≥ 580).

Versions are pinned per stack (SP1 6.2.4 · ZisK v1.0.0-alpha · OpenVM main/v1.4.0). The SP1 / cluster /
RSP pins are detailed in [`versions.md`](infra/sp1-infra/docs/versions.md); the ZisK and OpenVM versions
live in their own docs (`infra/zisk-infra/docs/`, `infra/openvm-infra/docs/openvm-multigpu.md`). The exact
clone commands + box setup are in each `infra/<stack>-infra/README.md`.

## Quickstart

The model is **Mac builds, box proves**: compile the ELF + generate the witness locally, then prove on
a rented GPU box. Each stack drives itself from its infra dir; ELFs and inputs land in the top-level `guests/`:

```sh
cd infra/zisk-infra
./run build-elf GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client
./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client \
  SAMPLE=../../vendor/zisk-eth-client/bin/guests/stateless-validator-reth/inputs/<sample>.bin
./run execute   ELF=../../guests/zisk-reth/zisk-reth.elf \
                INPUT=../../guests/zisk-reth/inputs/<tag>.bin        # local, CPU, no proof
./run prove     ELF=... INPUT=... REMOTE=user@box PORT=p            # multi-GPU on the box
```

Or drive any guest from the repo root via `cli/` (delegates to its stack; `--list` shows every guest):

```sh
cli/gen-elf     --guest rsp                                            # build guests/rsp/rsp.elf
cli/gen-witness --guest rsp  --block 20000000 --rpc <archive-rpc>      # -> guests/rsp/inputs/
cli/execute     --guest rsp  --input guests/rsp/inputs/1-20000000.bin  # local cycle/step count
cli/gen-witness --guest zisk --block 20000000                         # error: needs a debug node (see message)
```

See each `infra/<stack>-infra/README.md` for the full producer/consumer pipeline and the
`cluster/` GPU-proving flows.

## Results

- **Cross-zkVM execution** (deterministic work-units per block) — generate with `profiling/results.py`
  → `results/results.html`; plus the Monad-vs-reth execution comparison in [`guests/monad/README.md`](guests/monad/README.md).
- **Per-stack proving** (GPU wall-clock) — [SP1](infra/sp1-infra/docs/sp1-benchmark-synthesis.md) ·
  [ZisK](infra/zisk-infra/docs/zisk-benchmark.md) · [OpenVM](infra/openvm-infra/docs/openvm-benchmark.md).
- **Where the cost goes** (per-guest hotspot profiles) — [`profiling/`](profiling/) (`hotspots.py`).
