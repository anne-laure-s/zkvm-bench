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
| **`infra/monad-witness/`** | The **producer** side, on a Monad node: drives the node's replay of Ethereum mainnet to dump Monad-guest witnesses at the tip, then queues the cadence blocks, prunes the rest, and records the timestamps the latency report joins on (`witness-follow` · `witness-tap`). The witness seam for the Monad guest — there is no other source for these witnesses. See [`infra/monad-witness/README.md`](infra/monad-witness/README.md). |
| **`profiling/`** | Execution-analysis tools. **`compare.py`** — *how much more* does one guest cost than another, over a block set (the headline ratios, `results/compare.html`); **`hotspots.py`** — *where* the cost goes, per function and opcode; **`results.py`** — *how much* work per block, all guests side by side. Start at [`profiling/RUNBOOK.md`](profiling/RUNBOOK.md): every workflow as a command, including building a guest from a branch and measuring it end to end. |
| **`vendor/`** | Upstream clones (Axiom / Succinct / Polygon): `openvm-eth`, `rsp`, `zisk-eth-client` — the witness/guest **sources** the ELFs are built from, reproduced by `cli/install-vendors` at pinned commits. Also holds **`vendor/monad/`**, the Monad client tree (`category-labs/monad`): that one is a *working* clone on a branch, **not** a pinned vendor — `install-vendors` does not manage it, and `infra/monad-openvm/` builds the OpenVM guest from `vendor/monad/zkvm/openvm`. Not this project's code; do not reorganize. |
| **`cli/`** | Guest-agnostic driver CLIs, run from the repo root — `cli/gen-elf` · `cli/gen-witness` · `cli/execute` · `cli/prove-farm` (each `--guest <name>`, `--list`; delegate to the guest's stack — `zisk` witness and `monad-*` return a clear error). Plus the two bulk **farm** drivers `cli/witness-farm` (collect witnesses) → `cli/prove-farm` (prove them on the cluster, via the uniform `run prove-cluster` verb). Also holds `guests.registry` + `reg.sh`, the single source of truth (each guest → stack, params, per-capability mode `elf`/`witness`/`exec`; add a guest = add a row), and `report-schema.md`, the shared `report.json` contract every runner emits. |
| **`run-data/`** | **Runtime output** of every `cli/` driver, in one place and wholly git-ignored: `wcache/`, `witness-farm.csv`, `prove-farm.csv`, the `*-logs/`, the `ethproofs-mock-data/` store. Nothing here is a source — all of it regenerates by re-running the driver. Scripts reach it through `$RUNDATA` (overridable, to point a trial run at a throwaway directory). |

This is a single git repo. The only nested git repos are the upstream clones under `vendor/` (each
keeps its own `.git`, and `vendor/` is git-ignored). Build outputs and large regenerable inputs are
git-ignored per [`.gitignore`](.gitignore); everything else — `cli/`, `guests/`, `profiling/`, and all
three `infra/` — is versioned here.

So the root holds nothing but **sources** (`cli/`, `guests/`, `infra/`, `profiling/`) and two ignored
directories (`vendor/`, `run-data/`). A driver output that lands anywhere other than `run-data/` is a
bug, not an exception to add to `.gitignore`.

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

### What a fresh clone cannot give you

Most of what `.gitignore` excludes comes back from a command. This is the short list of what does
not, because none of it announces itself — you find out when a run fails, or when a number comes out
wrong.

**Must be copied from a machine that has them:**

| what | where | why not in git |
|---|---|---|
| `guests/monad/gen/*/witnesses/` | ~7 GB, 504 per generation | only a devcore box can mint them (snapshot + hours). `PROVENANCE.md` **is** tracked, so the *record* of which branch produced which numbers survives — the bytes do not |
| `guests/monad/gen/*/elf/` | 2 per generation | built on the box (rv64 toolchain). The sha256s are in that generation's `PROVENANCE.md` |
| `guests/monad-variants/*/*.elf` | ~55 MB | ablation builds, same story; sha256s in [`guests/monad-variants/README.md`](guests/monad-variants/README.md) |
| `guests/monad/inputs/` | 92 MB, 34 files | pre-supplied witnesses for an older range, not regenerable here |
| `AGENT-NOTES.md`, `profiling/FINDINGS.md`, `profiling/CALLGRAPH-FINDINGS.md`, `infra/monad-witness/RTP-FINDINGS.md` | ~370 KB total | working notebooks, deliberately out of git. Irreplaceable in a different way from the rest: they hold the traps and the refuted levers, i.e. the reasons not to redo work |

**Must be created, not copied:**

- **`ALCHEMY_URL`** in the environment — an archive-RPC key for minting reth-side witnesses
  (`cli/witness-farm`, `cli/gen-witness`). Never stored in the repo.
- **`vendor/monad`** — a working clone on a branch, so `cli/install-vendors` does **not** manage it
  (it handles the other four). A plain `git clone`, just not automatic.

The devcore box has its own per-machine set (toolchain config, cmake dir, memlock):
[`infra/monad-witness/README.md`](infra/monad-witness/README.md#moving-to-a-devcore-box-that-has-never-done-this).

Everything else that is ignored comes back from a command: reth ELFs (`cli/gen-elf`), reth witnesses
(`cli/gen-witness`, needs `ALCHEMY_URL`), `.expected_pv` files
(`guests/monad/gen-expected-pv.py`, needs witnesses + an RPC), the `current`/`fixtures` symlinks
(`guests/monad/use-gen`), `profiling/cache/`, `profiling/results/`, `run-data/`, and the other four
`vendor/` trees (`cli/install-vendors`).

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
cli/prove-farm  --guest rsp  --remote user@box --port p 20000000       # prove on the cluster -> proof + logs + report
cli/gen-witness --guest zisk --block 20000000                         # error: needs a debug node (see message)
```

For **bulk** runs, the two farm drivers pair up: `cli/witness-farm` continuously collects RSP + ZisK
witnesses, and `cli/prove-farm --guest <g> --remote user@box` batch-proves them on the cluster — each
block via the uniform `infra/<stack>/run prove-cluster` verb (proof + detailed log + `report.json`).
See [`cli/README.md`](cli/README.md#farming-collect--prove).

See each `infra/<stack>-infra/README.md` for the full producer/consumer pipeline and the
`cluster/` GPU-proving flows.

## Results

- **Cross-zkVM execution** (deterministic work-units per block) — generate with `profiling/results.py`
  → `results/results.html`; plus the Monad-vs-reth execution comparison in [`guests/monad/README.md`](guests/monad/README.md).
- **Per-stack proving** (GPU wall-clock) — [SP1](infra/sp1-infra/docs/sp1-benchmark-synthesis.md) ·
  [ZisK](infra/zisk-infra/docs/zisk-benchmark.md) · [OpenVM](infra/openvm-infra/docs/openvm-benchmark.md).
- **Where the cost goes** (per-guest hotspot profiles) — [`profiling/`](profiling/) (`hotspots.py`).
