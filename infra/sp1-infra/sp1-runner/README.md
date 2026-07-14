# sp1-runner — build variants

One Rust binary, one codebase, **four builds**: the SP1 backend (and a couple of runtime behaviors)
are gated behind cargo features. The CLI is identical across builds
(`--mode execute|prove-core|prove-compressed|prove-groth16|verify`, `--elf`, `--input`, `--report`, …)
and every build emits the shared [`report.json`](../../../cli/report-schema.md).

| Build | Command (run in `sp1-runner/`) | Features | Output binary | Used for | Runtime env |
|-------|--------------------------------|----------|---------------|----------|-------------|
| **Local CPU** *(default)* | `cargo build --release` | `native-gnark` | `target/release/sp1-runner` | Mac: `execute` / `verify` — cycle counts, PV recompute; validate before paying for a GPU | `SP1_PROVER=cpu` **(mandatory locally — see below)** |
| **Single-box CUDA** | `cargo build --release --features cuda` | `native-gnark` + `cuda` | `target/release/sp1-runner` | one GPU box, direct prove (no cluster) | `SP1_PROVER=cuda` *(the default)* |
| **Cluster network client** | `cargo build --release --no-default-features --features network` | `network` (+ `reserved-capacity`); no gnark | `target/release/sp1-runner` | proving through the self-hosted sp1-cluster gateway — built on the box by [`cluster-native/04-build-runner.sh`](../cluster-native/04-build-runner.sh) | `SP1_PROVER=network`, `NETWORK_RPC_URL=<gateway>`, `NETWORK_PRIVATE_KEY=<key>` |
| **Profiling** | `cargo build --release --no-default-features --features profiling --target-dir target-prof` | `profiling`; no gnark | `target-prof/release/sp1-runner` | function-level hotspot profiles for [`profiling/hotspots.py`](../../../profiling/hotspots.py) (its default `--runner`) | `TRACE_FILE=out.json` [`TRACE_SAMPLE_RATE=N`] on `--mode execute` → Gecko trace (open in profiler.firefox.com) |

## Notes

- **Prover default = CUDA.** If `SP1_PROVER` is unset the runner forces `cuda`, so a CPU-only build then
  fails trying to reach a GPU. **Locally you must pass `SP1_PROVER=cpu`** (the `./run execute` / `verify`
  wrappers already do). An explicit `SP1_PROVER` always wins, so nothing extra is needed on a GPU box.
- **`native-gnark` (Go/gnark FFI)** is only for LOCAL groth16 wrapping — it ships in the default
  (CPU / CUDA) builds and **needs a Go toolchain**. The `network` and `profiling` builds drop it via
  `--no-default-features` (the cluster does the wrap; profiling never proves), so they build without Go.
- **Why `--target-dir target-prof` for profiling.** Its feature set differs from the normal build, so a
  shared `target/` would trigger a full rebuild on every switch and clobber `target/release/sp1-runner`.
  A separate dir lets the profiling and normal binaries coexist.
- **`network` also gates a *runtime* behavior**, not just the backend: the network client skips the SDK's
  pre-submit local CPU re-execution (`skip_simulation`), cfg-gated so it only compiles into the network
  build. That keeps ~1.4–18.6 s/block of local re-execution out of the timed submit — see
  [`docs/optimisation-report.md`](../docs/optimisation-report.md).
- **`reserved-capacity`** (folded into `network`) flips the SDK to Reserved / "hosted" fulfillment; the
  self-hosted gateway does not implement the auction-mode `get_proof_request_params` call. Details in the
  `[features]` comments of [`Cargo.toml`](Cargo.toml).

## Who builds / expects which

- [`scripts/core.sh`](../scripts/core.sh) — `RUNNER` defaults to `target/release/sp1-runner` (the local
  CPU build on the Mac; the CUDA build on the box).
- [`cluster-native/04-build-runner.sh`](../cluster-native/04-build-runner.sh) — builds the **network**
  client on the box.
- [`profiling/hotspots.py`](../../../profiling/hotspots.py) — defaults `--runner` to
  `target-prof/release/sp1-runner` (the **profiling** build).

Crate/SDK versions are pinned in [`docs/versions.md`](../docs/versions.md).
