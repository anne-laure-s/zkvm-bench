# report.json — the shared runner contract

Every runner (`sp1-runner`, `zisk-runner`, `openvm-runner`) emits JSON in **one shared shape**, so
one consumer (e.g. the `profiling/results.py` work-unit aggregator) can read all three
without special-casing. There are two report kinds.

> ⚠️ The **work-unit** (`cycles` for SP1/OpenVM, `steps` for ZisK) is **deterministic within a zkVM**
> but **not comparable across zkVMs** (SP1 cycles ≠ ZisK steps ≠ OpenVM instructions). `elapsed_secs`
> / `prove_secs` are host-dependent. Compare like-for-like; never sum or rank the raw work-units.

## 1. Execute report — `<tag>.exec-report.json`
Written next to the input by `./run execute` (local, no proof). **Common core** + backend extras.

| field | type | emitted by | meaning |
|-------|------|-----------|---------|
| `mode` | `"execute"` | **all** | report kind |
| `zkvm` | str | **all** | which zkVM — `SP1` · `ZisK` · `OpenVM` |
| `guest` | str | **all** | guest name — `rsp` · `zisk-reth` · `openvm-reth` · … |
| `block` | int \| null | **all** | mainnet block (null for non-block guests, e.g. `fibonacci`) |
| `commit` | str \| null | **all** | source commit of the ELF the report was built from (from `guests/<guest>/<guest>.commit`) — pins the work-unit to an ELF version; null if unknown |
| `elapsed_secs` | float | **all** | wall-clock of the run (host-dependent) |
| `cycles` | int | sp1 · openvm | work-unit (SP1 cycles / OpenVM instructions) |
| `steps` | int | zisk | work-unit (ZisK steps) |
| `public_values_bytes` | int \| null | sp1 · zisk | committed-PV size (**null for openvm** — its `execute` is metered-only, no PV) |
| `gas`, `total_syscalls`, `touched_memory_addresses`, `exit_code`, `execution_report` | — | sp1 | rich SP1 breakdown |

The `zkvm` / `guest` / `block` / `commit` core is injected by `./run execute` (the dispatcher), because
the runners are guest-agnostic (they only see an ELF + input, or a block number). `commit` comes from
the guest's `<name>.commit` pin (via `REPORT_COMMIT`).

## 2. Prove report — `report.json`
Written into the run record (`results/…/report.json`) by `./run prove` / `./run prove-cluster`
(the uniform cluster-prove verb `cli/prove-farm` drives) / `cluster*/submit.sh`. **Common core** +
backend extras. The run record also carries the **proof** (`proof.bin` / `segments/`) and a detailed
**proving log** (`prove.log`, or `worker-*.log` + `aggregate.log` for OpenVM).

| field | type | emitted by | meaning |
|-------|------|-----------|---------|
| `mode` | str | **all** | `prove-compressed` · `prove-core` · `prove-stark` · … |
| `prove_secs` | float | **all** | proving wall-clock |
| `total_secs` | float | **all** | end-to-end wall-clock |
| `proof_bytes` | int | **all** | proof size |
| `verified` | bool \| null | **all** | verification result (null = not run inline) |
| `setup_secs`, `verify_secs`, `vkey_hash`, `public_values_bytes` | — | sp1 | |
| `verify_secs`, `backend` | — | zisk | |
| `zkvm`, `block`, `num_gpus`, `multi_gpu`, `cycles`, `keygen_excluded`, `comparison_skipped`, `backend` | — | openvm | GPU fan-out context |

## The invariant
Every **execute** report carries the same core, in order: **`mode` · `zkvm` · `guest` · `block` ·
`commit`** + `elapsed_secs` + a **work-unit** (`cycles` \| `steps`). **Prove** reports carry **`mode`**, timings
(`prove_secs` + `total_secs`) and a **size** (`proof_bytes`). `public_values_bytes` is present for SP1
and ZisK but **null for OpenVM's execute** (metered-only, no PV). Backend-specific keys are additive
and safe to ignore. A new stack plugs in by emitting this core — the `./run` dispatcher injects
`zkvm`/`guest`/`block`.
