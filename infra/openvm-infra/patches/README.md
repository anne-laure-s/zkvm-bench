# patches/ — the multi-GPU OpenVM patch (enables path ① per-GPU segment sharding; in-process path "②" remained broken)

OpenVM's open block prover proves continuation segments **serially on one GPU**. This patch makes
one process fan the segment proofs across all the box's GPUs. Full design + the exact code for all
edits: **`../docs/openvm-multigpu.md`**. This dir is the applier.

## `apply.sh <path-to-openvm-eth>`
Does everything, **fully automatically**:
1. Reads the pinned `openvm` / `stark-backend` revs from `openvm-eth/Cargo.lock`, clones both into
   `<openvm-eth>/.vendor/` at those exact revs.
2. Resets the tracked files to pristine (`git checkout`) and applies the full edit list (15 hunks)
   by **exact-string match** (anchors verified against the live `main` rev). Prints `OK` / `SKIP` /
   `FAIL` per edit; any `FAIL` (a string moved on a newer rev) makes the whole run exit non-zero with
   no build, so you never build a half-patched tree.
3. Generates the `cargo [patch]` section by reading `Cargo.lock` and locating every git crate's dir
   in the vendored checkout — so all crates of a source resolve to one place (no type-duplication).

| edit | file | what |
|---|---|---|
| 1   | stark-backend `cuda-common/src/common.rs` | `set_device_ordinal(i)` (append) |
| 2a  | openvm `sdk/src/prover/app.rs`            | read `OPENVM_NUM_GPUS` before the segment loop |
| 2b  | openvm `sdk/src/prover/app.rs`            | `cudaSetDevice(seg_idx % num_gpus)` before `local()` |
| 3   | openvm `sdk/src/lib.rs`                   | `Sdk::async_app_prover(exe, max_concurrency)` |
| 4a  | openvm-eth `reth-benchmark/Cargo.toml`    | `cuda` feature also enables `openvm-sdk/async` |
| 4b  | openvm-eth `reth-benchmark/src/lib.rs`    | `--num-gpus` flag on `HostArgs` |
| 4c  | openvm-eth `reth-benchmark/src/lib.rs`    | in-process multi-GPU branch in the `ProveStark` arm (path ②) |
| 5   | openvm `sdk/src/prover/app.rs`            | expose `prove_shard` (single-shard entry point) |
| 6a–6f | openvm-eth `reth-benchmark/src/lib.rs`  | `prove-segments` / `aggregate` worker modes — the multi-**process** path ① (one GPU per process) |
| wex | openvm-eth `Cargo.toml`                   | `exclude = [".vendor"]` (keep vendored clones out of the workspace) |

`applied-revs.txt` records the revs the patch was applied against (re-apply on rev drift).

## Workflow that minimizes GPU-box cost
The CUDA code is all `#[cfg(feature="cuda")]`, so **compile-fix for free, no GPU**:
```sh
patches/apply.sh ~/openvm-eth
cd ~/openvm-eth && cargo +nightly-2026-01-01 build --release \
    --bin openvm-reth-benchmark            # no --features → no cuda → no GPU needed
```
Settle Edit 3's trait bounds and the `[patch]` graph here (Mac or a cents/hr CPU box). Only the
final `--features cuda` build + the prove run need the GPU box. See `../docs/openvm-multigpu.md`
("Compile-check for free").

> Alpha crates. If `apply.sh` reports a `FAIL`, the anchor moved on a newer rev — hand-apply that
> one hunk from the doc and re-run. Note: in-process multi-device proving (Edit 4c, **path ②**) was
> **confirmed to crash on the box** (VPMM `cudaErrorInvalidResourceHandle`) — use **path ①**
> (multi-process, one GPU per process: `cluster/submit.sh`, Edits 5/6), the working multi-GPU path.
