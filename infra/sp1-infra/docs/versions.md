# Versions — SP1 / cluster / RSP

Measured 2026-06-29 from the repo files + the execution logs. Source of truth for benchmark
traceability.

## SP1

| | |
|---|---|
| SP1 crates (`sp1-sdk`, `sp1-prover`, `sp1-core-executor`, `sp1-build`) | **`=6.2.4`** (strict pin in `rsp/Cargo.toml`) |
| Circuit version (runtime, proven) | **`v6.1.0`** (coordinator logs + `prove.log`) |
| Guest toolchain (`cargo-prove`) | `sp1 cfb5544` (2026-06-08) |
| Guest Rust channel (`rsp/rust-toolchain.toml`) | `1.94.0` (succinct fork, `riscv64im-succinct-zkvm-elf`) |

SP1 **6.x = Hypercube generation** (Jagged PCS + LogUp GKR + sumcheck proof system — confirmed by the
`jagged`/`logup`/`gkr`/`sumcheck`/`basefold` logs). This is NOT the old FRI stack.

## Cluster (`sp1-cluster`)

| | |
|---|---|
| Version | **`v2.4.3`** (`git checkout v2.4.3`) |
| Docker images | `ghcr.io/succinctlabs/sp1-cluster:base-v2.4.3` · `:node-gpu-v2.4.3` |
| Embedded SP1 | **6.2.4** |
| Coordinator binary | built 2026-06-19 (version string: `unknown` — source build with no embedded tag) |

## RSP

| | |
|---|---|
| Commit | **`c2734ce`** (2026-06-10) |
| Tag | **`reth-2.2.0-sp1-6.2.4`** |
| SP1 | **6.2.4** |
| reth | **`v2.2.0`** (paradigmxyz/reth, tag `v2.2.0`) |
| Local patch | **`RSP_BENCH`** — *uncommitted*, **benchmark-only** |

### `RSP_BENCH` patch (local, uncommitted)
Bypasses post-execution validation to work around **issue #181** (gas mismatch on EIP-7702 / type-4
txs, post-Pectra; under-counting of `PER_AUTH_BASE_COST` = 12 500 gas/auth). The gas mismatch does not
change the instruction count → valid cycles. Patched sites (`warn` instead of abort):
- `crates/executor/host/src/host_executor.rs`: `validate_block_post_execution` + state-root check
- `crates/executor/client/src/executor.rs`: `validate_block_post_execution` (→ rebuild of the guest ELF)

⚠️ **Do NOT ship to the proving cluster** (validation disabled = unsound proofs). The patched build
lives only in `vendor/rsp/target/`; `guests/rsp/rsp.elf` (the cluster ELF) stays **intact**.

## Consistency

All three are aligned on **SP1 6.2.4 / circuit v6.1.0** → RSP (host/witness-gen, reth 2.2.0) and the
cluster (prover, v2.4.3) are **compatible** (same SP1 version → same ELF/proof format). The only
deliberate divergence: the local `RSP_BENCH` patch, which serves only for cycle counting.
