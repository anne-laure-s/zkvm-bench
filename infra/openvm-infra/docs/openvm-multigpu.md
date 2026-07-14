# Self-hosted multi-GPU OpenVM on one box — design + patch (deliverable = path ①; in-process path "②" remained broken)

> **STATUS (updated 2026-07-09): path ② does NOT work.** The in-process design below was implemented and,
> on the box, crashes at runtime — **VPMM `cudaErrorInvalidResourceHandle`**: the open CUDA backend keeps
> single-device global state (allocator, streams, cuBLAS, NTT tables), so driving multiple GPUs from one
> process is unsafe. The claim in "The idea" below that *"concurrent per-device contexts are the intended
> design"* turned out **false**. **The working multi-GPU path is ① (multi-process, one GPU per process):
> `cluster/submit.sh`** — see the "PATH ①" section of `../patches/apply.sh` and the results in
> [openvm-benchmark.md](openvm-benchmark.md). Keep this doc for the ② design history + the vendoring/edit
> mechanics (still accurate); ignore its claim that ② is runnable.

**Goal:** prove ONE mainnet block as fast as possible across all the GPUs on a single box,
100 % open-source (no Axiom hosted API), to get a single-block latency number directly
comparable to the SP1 cluster figure (~40 s / 8×5090 on a 200 M-cycle block).

**Why a patch is needed (verified on live code 2026-06-30):** OpenVM's open block prover
(`openvm-eth/bin/reth-benchmark`) calls the **synchronous** `StarkProver::prove`, which proves
continuation segments **serially on the default GPU**. The SDK *does* ship an `AsyncAppProver`
(tokio, one task per segment) and a device-aware CUDA backend, but nothing pins segment tasks
to different GPUs. Axiom's own open per-box worker (`openvm-eth/server/`) just runs **one**
`prove-stark` process per block with `docker run --gpus all` — one box is their unit of work. The
closed part of Axiom's stack is only the **multi-node** coordinator; single-box multi-GPU is not
provided open either — but every building block to do it is.

## The idea, in one paragraph

`StarkProver::prove` is literally:
```rust
let app_proof = self.app_prover.prove(input)?;                       // serial, 1 GPU  ← the cost
let leaf = self.agg_prover.generate_leaf_proofs(&app_proof)?;        // aggregation
self.agg_prover.aggregate_leaf_proofs(leaf, app_proof.user_public_values.public_values)
```
`AsyncAppProver::prove` returns the **same** `ContinuationVmProof<SC>`. So we (1) make
`AsyncAppProver` pin each segment task to a GPU ordinal, (2) call it instead of the sync app
prover, then (3) feed its result into the existing (single-GPU) aggregation. `StarkProver`'s
`app_prover`/`agg_prover` fields are `pub`, so step 3 needs no SDK change. The app-proof phase
dominates, so fanning *it* across GPUs is the win; aggregation can stay on GPU 0 for v1 (it is
itself parallelizable later — same trick on `generate_leaf_proofs`).

CUDA reality that makes this work: `cudaSetDevice` is **per host thread**; the SDK already
builds a fresh prover instance per segment task inside `spawn_blocking`. Pin the thread to
ordinal `d` before it allocates and its whole proof lands on GPU `d`. VPMM (the GPU allocator)
is already keyed by `device_ordinal`, so concurrent per-device contexts are the intended design.

## Vendoring (how the patch is delivered)

`openvm-eth` pins `openvm = { git, branch = "main" }` and `openvm-stark-sdk =
{ git = stark-backend, tag = "v1.4.0" }`. `patches/apply.sh` clones both at the exact revs
`openvm-eth/Cargo.lock` froze, applies the edits below, and generates the `[patch]` section
**automatically**: it reads `Cargo.lock`, lists *every* crate coming from each git source, locates
each crate's dir in the vendored checkout (`grep -rl 'name = "<crate>"'`), and emits one
`{ path = … }` override per crate. Redirecting **all** crates of a source (not just the edited
ones) is what prevents the classic "two `openvm-circuit` instances → type mismatch" failure — the
#1 way cargo `[patch]` of a git workspace goes wrong. The result looks like:
```toml
# BEGIN openvm-infra multi-gpu patch (auto-generated)
[patch."https://github.com/openvm-org/openvm.git"]
openvm-sdk       = { path = ".vendor/openvm/crates/sdk" }
openvm-circuit   = { path = ".vendor/openvm/crates/vm" }
…                                                          # every openvm.git crate in the graph
[patch."https://github.com/openvm-org/stark-backend.git"]
openvm-cuda-common = { path = ".vendor/stark-backend/crates/cuda-common" }
…
# END openvm-infra multi-gpu patch
```

> ⚠️ These are alpha crates. The edits are anchored to **exact source strings** (verified against
> the live `main` rev) and applied by `apply.sh`, which prints `OK`/`SKIP`/`FAIL` per edit and
> aborts on any `FAIL` (a string moved on a newer rev → hand-apply that one from below, re-run).
> The one thing that genuinely needs a real compile is the generic **trait bounds** on Edit 3 —
> and you can settle those **without a GPU** (see "Compile-check for free" below).

---

## Edit 1 — `stark-backend/crates/cuda-common/src/common.rs`: pin a device by ordinal

Add (the `cudaFree(null)` forces the CUDA context to be created on `ordinal` for this thread):
```rust
/// Pin the CURRENT host thread to CUDA device `ordinal` and eagerly create its context.
/// Used for multi-GPU fan-out: each segment-proving thread calls this before allocating,
/// so all of its proof work lands on GPU `ordinal`.
pub fn set_device_ordinal(ordinal: i32) -> Result<(), CudaError> {
    unsafe {
        check(cudaSetDevice(ordinal))?;
        check(cudaFree(std::ptr::null_mut()))?; // create the context on this device now
    }
    Ok(())
}
```
(`cudaSetDevice` and `cudaFree` are already declared `extern "C"` in this file.)

## Edit 2 — `openvm/crates/sdk/src/prover/app.rs`: device-pin the async segment tasks

In `AsyncAppProver::prove` (the `#[cfg(feature = "async")]` block). We assign GPUs by **`seg_idx %
num_gpus`** (round-robin by segment index) rather than a release-pool — it needs no result-wrapping
and is correct regardless (proofs are independent; `cudaSetDevice` is per host thread). With the
semaphore capped at `num_gpus` concurrent tasks (Edit 4 passes `max_concurrency = num_gpus`), the
spread is even; worst case two proofs briefly share a GPU (slower, never wrong).

**a.** Right after `let mut num_ins_last = 0;` (just before the segment loop), read the GPU count:
```rust
let num_gpus: i32 = std::env::var("OPENVM_NUM_GPUS")
    .ok().and_then(|s| s.parse().ok()).filter(|&n| n >= 1).unwrap_or(1);
#[cfg(not(feature = "cuda"))]
let _ = num_gpus;                       // silence unused on CPU builds
```
`num_gpus` (Copy) is captured into the per-segment `async move` → `spawn_blocking(move || …)`.

**b.** Inside the `spawn_blocking` closure, immediately **before** `let mut worker =
async_worker.local()?;` (which is where the device gets allocated), pin the thread via a **direct
cudart FFI**:
```rust
#[cfg(feature = "cuda")]
{
    extern "C" {
        fn cudaSetDevice(device: i32) -> i32;
        fn cudaFree(ptr: *mut core::ffi::c_void) -> i32;
    }
    let __dev = (seg_idx as i32) % num_gpus;
    let __rc = unsafe { cudaSetDevice(__dev) };
    assert_eq!(__rc, 0, "cudaSetDevice failed: {__rc}");
    unsafe { cudaFree(core::ptr::null_mut()); } // create the ctx on this device now
}
let mut worker = async_worker.local()?;
```
⚠️ **Why the direct FFI (not `openvm_cuda_common::common::set_device_ordinal`)?** `openvm-sdk` does
**not** depend on `openvm-cuda-common` — it's only a transitive dep via `openvm-cuda-backend` — so
referencing it fails to compile in a `--features cuda` build (`E0433: unresolved crate
openvm_cuda_common`). This is invisible on a CPU build (the `#[cfg]` is off). But `cudart` IS in the
cuda build's link closure (via `openvm-cuda-backend`), so declaring the extern here resolves at
final link, with zero Cargo.toml surgery. (Edit 1's `set_device_ordinal` in cuda-common is now
vestigial/unused — harmless.) `apply.sh` does both insertions by exact-string match.

## Edit 3 — `openvm/crates/sdk/src/lib.rs`: a constructor for the async prover

Mirror `Sdk::app_prover`, returning the async prover with a concurrency = GPU count:
```rust
/// Multi-GPU app prover: fans continuation-segment proofs across `max_concurrency`
/// blocking tasks, each pinned to a GPU ordinal (see AsyncAppProver::prove). Pair with
/// `prover(exe)?.agg_prover` to aggregate the result into a single STARK.
#[cfg(feature = "async")]
pub fn async_app_prover(
    &self,
    exe: impl Into<ExecutableFormat>,
    max_concurrency: usize,
) -> Result<crate::prover::AsyncAppProver<E, VB>, SdkError>
where
    E: 'static,
    VB: Clone + Send + Sync + 'static,
    VB::VmConfig: Send + Sync,
{
    let exe = self.convert_to_exe(exe)?;
    let app_pk = self.app_pk();
    let prover = crate::prover::AsyncAppProver::<E, VB>::new(
        self.app_vm_builder.clone(),
        app_pk.app_vm_pk.clone(), // already Arc<VmProvingKey> — do NOT wrap in Arc::new
        exe,
        app_pk.leaf_verifier_program_commit(),
        max_concurrency,
    )?;
    Ok(prover)
}
```
(If the extra `where` bounds clash with the surrounding `impl` block, the alternative is to
construct `AsyncAppProver` directly in reth-benchmark — but that needs `app_vm_builder` to be
public, so adding the method here is usually cleaner.)

## Edit 4 — `openvm-eth/bin/reth-benchmark`: a `--num-gpus` flag that uses it

**Cargo.toml:** `openvm-reth-benchmark` has no `async` feature of its own — `async` lives on
`openvm-sdk`. Enable it directly on the **dependency** so the multi-GPU path compiles with a plain
`cargo build` (no cuda → no GPU needed for the compile-check). Leave the `cuda` feature untouched:
```toml
# before: openvm-sdk = { git = "…/openvm.git", branch = "main", default-features = false }
openvm-sdk = { git = "https://github.com/openvm-org/openvm.git", branch = "main", default-features = false, features = ["async"] }
```
**cli.rs / HostArgs:** add
```rust
/// Number of GPUs ONE process fans segment proofs across (1 = stock single-GPU path).
#[arg(long, env = "OPENVM_NUM_GPUS", default_value_t = 1)]
pub num_gpus: usize,
```
**lib.rs `run_reth_benchmark`, the `BenchMode::ProveStark` arm:** replace its first two lines
(`let mut prover = sdk.prover(elf)?…; let proof = prover.prove(stdin)?;`) with a branch on
`num_gpus`. The rest of the arm (`block_hash`, `output_dir` write) is untouched — `proof` keeps the
same `VmStarkProof<SC>` type from either branch. Note the `block_in_place`/`block_on` bridge: this
arm runs inside `run_with_metric_collection(|| …)`, a **synchronous** closure, so we can't `.await`
directly even though `run_reth_benchmark` is `#[tokio::main]`; `block_in_place` hands the worker
thread to `block_on`, which drives the async prover on the multi-thread runtime.
```rust
BenchMode::ProveStark => {
    let proof = if args.num_gpus > 1 {
        unsafe { std::env::set_var("OPENVM_NUM_GPUS", args.num_gpus.to_string()); } // unsafe on edition 2024
        let async_prover = sdk.async_app_prover(elf.clone(), args.num_gpus)?;   // Edit 3
        let app_proof = tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current().block_on(async_prover.prove(stdin))
        })?;                                                                    // multi-GPU app phase
        let mut agg = sdk.prover(elf)?;                                         // reuse its agg_prover
        let leaf = agg.agg_prover.generate_leaf_proofs(&app_proof)?;
        agg.agg_prover.aggregate_leaf_proofs(
            leaf, app_proof.user_public_values.public_values)?
    } else {
        let mut prover = sdk.prover(elf)?.with_program_name(program_name);      // stock single-GPU
        prover.prove(stdin)?
    };
    // ... existing block_hash + output_dir write, unchanged ...
}
```
(`max_concurrency == num_gpus` so the device round-robin in Edit 2 spreads one segment per GPU. We
intentionally don't set `program_name` on the async prover to avoid moving it twice — it's only a
tracing label.) `apply.sh` applies this by exact-string match (anchors verified on the live rev).

---

## Compile-check for free (no GPU) — ALREADY DONE ✅

Every line of the patch that touches CUDA is `#[cfg(feature = "cuda")]`, and `async` is enabled on
the `openvm-sdk` dependency, so the whole prover binary compiles **without `cuda` and without a
GPU**. This was run on macOS arm64 (nightly-2026-01-01) and is **green** (`cargo check` exit 0):
```sh
patches/apply.sh ~/openvm-eth                 # vendor + patch (needs only Cargo.lock, no GPU)
cd ~/openvm-eth
# the guest ELF is include_bytes!'d; for a pure compile-check, a placeholder is enough:
mkdir -p bin/reth-benchmark/elf && touch bin/reth-benchmark/elf/openvm-stateless-guest
cargo +nightly-2026-01-01 check --release --bin openvm-reth-benchmark   # no --features → no cuda
```
Three real, build-breaking pitfalls were caught here for cents (now auto-handled by `apply.sh`):
1. **Vendored crates inside openvm-eth's tree** → their `authors.workspace = true` resolved against
   openvm-eth's `[workspace.package]` (no `authors`). Fix: `apply.sh` sets `[workspace].exclude =
   [".vendor"]`.
2. **Full lockfile re-resolve bumps `winnow` 0.7 → 1.0**, which breaks `alloy-dyn-abi 1.5.x` (142
   errors). Fix: `apply.sh` restores the committed `Cargo.lock` so the build only ADDS the vendored
   crates. (If you ever see winnow/alloy errors: `git checkout Cargo.lock && cargo build`.)
3. **`set_var` is `unsafe` on edition 2024**, and `app_pk.app_vm_pk` is already `Arc<…>` — both fixed
   in the Edit 3 / Edit 4c text above.

Only the **real run** needs the GPU box (`--features cuda`) and the **real** guest ELF (built by
`cargo openvm build`; `cluster/00-install-once.sh` does this). If macOS can't build a heavy host dep,
use a cents/hr Linux CPU box — still no GPU. The compile-fix loop costs cents, not GPU-hours.

## Build (on the box)
```sh
CUDA_ARCH=120 cluster/00-install-once.sh     # RTX 5090 = Blackwell (sm_120). clones+patches+builds
```
Requirements: NVIDIA driver + CUDA toolkit recent enough for Blackwell (CUDA ≥ 12.8), nightly
`2026-01-01`, ≥24 GB VRAM/GPU. The keys (app_pk/agg_pk) keygen on first prove unless you pass
`--app-pk-path/--agg-pk-path`. Because the `async` compile is already green from the free step
above, the box step should be just the `cuda`-gated codegen + link, then run.

## Validate it actually uses all GPUs (path ①)
```sh
NUM_GPUS=8 cluster/submit.sh 20000000 &
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv -l 1
```
During the app-proof phase (the workers) **all `NUM_GPUS` devices** should show utilization; the run's
`timing.txt` records `workers_secs` / `aggregate_secs` / `total_secs`. Cross-check correctness: the
committed block hash in the proof's public values must match a trusted source for block 20000000.

## What "good" looks like / scaling expectation
App proofs are independent, so with S segments and G GPUs the app phase floors at
`ceil(S/G) × (per-segment time)` instead of `S × (per-segment time)`. Aggregation (still 1 GPU in
v1) and the serial metered execution set the latency floor — if aggregation becomes the bottleneck
after the app phase is parallel, parallelize `generate_leaf_proofs` the same way (Edit 2 pattern on
the leaf loop) as v2. Expect strong-but-sublinear scaling, like the SP1 8→16 GPU ~1.8×.

## Audit results (what's verified vs the one remaining unknown)

**Verified against the real code / a green compile (no GPU spent):**
- **Compiles clean** — `openvm-sdk` (Edits 1/2/3) + `openvm-reth-benchmark` (Edits 4a/b/c) build with
  `cargo check` exit 0 on macOS arm64 / nightly-2026-01-01. Trait bounds settled.
- **`block_in_place`/`block_on` is safe** — `run_with_metric_collection` runs its closure **inline**
  (`let res = f();`, no thread spawn), so we're on a `#[tokio::main]` multi-thread worker → `block_on`
  won't panic.
- **CUDA arch** — `stark-backend`'s `cuda-builder::get_cuda_arch` reads `$CUDA_ARCH` (→ `-gencode
  arch=compute_120,code=sm_120` + PTX) or auto-detects. `00-install-once` sets `CUDA_ARCH=120` and
  does NOT set conflicting `NVCC_PREPEND_FLAGS`. (Box needs CUDA toolkit ≥ 12.8 for Blackwell.)
- **Timing is honest** — keygen and the host/VM re-execution comparison are OUT of the timed region:
  `01-keygen.sh` pre-generates `app_pk`/`agg_pk` (bitcode, loadable via `--*-pk-path` — format
  verified) and the runner passes `--skip-comparison`. keygen↔prove blowups are the single
  `OPENVM_PROVE_EXTRA_FLAGS` in `box-env.sh` (the binary asserts a config match on key load).
- **Guest/host version match** — `00-install-once` installs `cargo-openvm` at the *pinned* openvm rev
  (not main HEAD) before `cargo openvm build`.
- **Config prerequisites flagged at runtime** — `submit.sh` warns if no `RPC_1`/cache and if no keys.

**RESOLVED on the box (2026-07-09) — ② does not work:**
- **VPMM concurrency across devices in one process FAILED.** Despite the `device_ordinal`-keyed
  allocator and per-task `cudaSetDevice`, driving multiple GPUs from one process crashes with
  `cudaErrorInvalidResourceHandle`: the backend keeps single-device global state (allocator, streams,
  cuBLAS, NTT tables) not indexed by device. **We fell back to path ① (multi-process, one GPU per
  process) — `cluster/submit.sh` — which works** (28 seg / 8×5090 / ≈22 s app-proof; see
  [openvm-benchmark.md](openvm-benchmark.md)). The single-block decomposition is identical; only the
  aggregation stays 1-GPU.

**Known, accepted trade-offs (not blockers):**
- `set_var("OPENVM_NUM_GPUS")` is `unsafe` on edition 2024; we set it once before spawning the prove
  tasks (no concurrent env readers at that point) — benign in this flow.
- Edit 4c builds `sdk.prover(elf)` just to reuse its `agg_prover`, which also constructs an unused
  `AppProver` (commits the exe on device 0). Wasteful VRAM/time, not incorrect; if VRAM-tight, build
  only `AggStarkProver` (via `StarkProver::from_parts`).
- **Rev drift** — re-run `apply.sh` after `openvm-eth` bumps its `openvm`/`stark-backend` pins.
