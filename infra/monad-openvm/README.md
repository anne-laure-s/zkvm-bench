# monad-openvm — proving the Monad witness guest on OpenVM, two arms

The Monad guest on **OpenVM**, baseline vs **powdr autoprecompiles (APC)**, on the block set already
studied for monad-vs-rsp (SP1) and monad-vs-zisk-eth. Unlike `openvm-infra/` — which drives the
*reth* guest (`openvm-eth`) — everything here goes through the monad repo's own host driver,
`monad/zkvm/openvm/script`, so there is no `submit.sh`/`aggregate.sh` in play.

| | baseline arm | APC arm |
|---|---|---|
| binary | `monad-zkvm-openvm-script` | `monad-zkvm-openvm-powdr` (`--features powdr`) |
| guest | a **`.vmexe` you build** (`cargo openvm build`) | built + transpiled + APC-transformed **by the driver** |
| segment count | `--mode segment` | `--segments` |
| prove | `--prove-app` / `--prove` | *(no flag)* / `--recursion` |

## Step 0 — segment counts on CPU (`segments.sh`)

Core proving is linear in segment count, so this is what turns a block list into a GPU budget.
Nothing else predicts it: witness size does not (a 7 MB Prague witness is mostly Merkle proof, not
work) and neither do SP1 cycles. It is also a result on its own — the APC segment ratio across the
size range, against the published CPU pair on 22200003 (**63 → 43 segments, 16 APCs**).

```sh
./segments.sh                                    # the 7-point stratified sample, both arms
BLOCKS="25552210" ARMS=vanilla ./segments.sh     # one block, one arm
APCS=32 ./segments.sh                            # a different autoprecompile count
BUILD=1 ./segments.sh                            # build the missing driver binaries first
```

Writes `results/segments.csv`
(`block,arm,witness_bytes,instructions,segments,seg_mem_gib,apcs,profile_block,root_match,secs,peak_rss_gib,compile_secs,apc_setup_secs,status`),
one log per run under `results/logs/`, and prints the per-block ratio table. Resume-able per
*configuration*: a row already `ok` for the same `(block, arm, apcs, seg_mem_gib)` is skipped, so
changing `APCS` re-measures instead of silently reusing.

Correctness comes free: the driver prints the committed post-state root, checked against
`guests/monad/fixtures/<block>.post_state_root` with the same substring convention as
`guests/monad/ev.sh` (`root_match` column).

### Two invariants, or the comparison means nothing

- **`SEG_MEM_GIB` must be identical across arms** — it sets the segment count directly
  (`segmentation_config.limits.max_memory`). It is recorded in every row; never compare rows that
  disagree on it. Empty = OpenVM's default 15 GiB.
- **`PROFILE_BLOCK` is fixed across all APC runs.** The autoprecompile set is derived from the
  profiled block, so profiling per-block would compare a different program in every row. The driver
  caches generate/select/setup under `ARTIFACTS_DIR` keyed on it; changing `--input` does not
  invalidate that cache, changing `--profile-input` does.

### The block sample

Default: the 7-point stratified sample over the 340 clean blocks common to both study axes and
present in `guests/monad/fixtures` — quantiles of monad work-units, outliers excluded, spanning
0.39–13.85 MB of witness and bracketing both study ratios.

```
25551992 (min) 25552236 (p10) 25552361 (p25) 25552210 (p50) 25552250 (p75) 25552179 (p90) 25552051 (max)
```

`25552066` (1.44 MB, percentile 1) is the **bridge to the published CPU numbers** — same calibre as
22200003 — not a representative block. `25552366` (15.4 MB, p99) is where the APC gain should peak.

## Prerequisites

Both arms need the guest to build for `riscv32im`, i.e. the same prerequisites as
`cargo openvm build`: an **rv32im/ilp32 multilib** RISC-V toolchain (`RISCV_TOOLCHAIN_DIR`) and the
guest Rust toolchain (`nightly-2026-01-18`, installed by the CLI). The baseline arm additionally
needs the `.vmexe`; the APC arm compiles the guest itself.

**The `cargo-openvm` CLI must come from powdr's fork at tag `v2.0.0-beta.2-powdr.1`** — the `.vmexe`
serialisation is incompatible with openvm-org v2.0.1 (it fails to load with a bitcode error, not a
version warning). Same pin in three places: guest, `script/`, CLI.

Install it under its own `--root`: a plain `cargo install` would **replace** the `cargo-openvm` on
PATH, and `openvm-infra/` (the reth campaign) depends on a different one (v1.7.0, from its own
vendored checkout).

```sh
cargo install --root ~/.openvm-powdr --git https://github.com/powdr-labs/openvm.git \
    --tag v2.0.0-beta.2-powdr.1 cargo-openvm --no-default-features --features parallel,jemalloc --locked
cd ../../monad/zkvm/openvm && RISCV_TOOLCHAIN_DIR=$HOME/riscv_gcc_multilib \
    CMAKE_PREFIX_PATH="$(brew --prefix)" ~/.openvm-powdr/bin/cargo-openvm openvm build
```

`segments.sh` finds the resulting `.vmexe` on its own (`<pkg>/openvm/release/`, then
`<pkg>/release/`, then any `.vmexe` under the package); `EXE=` overrides.

### The cross toolchain (macOS)

`-march=rv32im -mabi=ilp32` needs a **multilib** RISC-V GCC *with* newlib/libstdc++ — the guest
includes `<optional>`, `<variant>`, `<vector>`. Homebrew's `riscv64-elf-gcc` has the multilib but is
built `--without-headers`, so `<cstdint>` does not resolve; xPack's `riscv-none-elf-gcc` ships both.
`RISCV_TOOLCHAIN_DIR` must expose the binaries under the prefix `riscv64-unknown-elf-` —
`category/core/toolchains/riscv64-elf.cmake` requires that exact name (`FATAL_ERROR` otherwise), so
symlink whatever prefix your toolchain uses. `segments.sh` preflights both the prefix and the
presence of `rv32im/ilp32` in `-print-multi-lib`.

```sh
cd ../../monad/zkvm/openvm/script && cargo build --release --bin monad-zkvm-openvm-script
```

```sh
cd ../../monad/zkvm/openvm/script && cargo build --release --features powdr --bin monad-zkvm-openvm-powdr
```

## Then: GPU

`script/Cargo.toml` already carries `cuda = ["openvm-sdk/cuda", "powdr-openvm?/cuda"]` (weak `?`, so
a baseline GPU build does not drag in the APC stack) and `bin/powdr.rs` already cfg-switches
`PowdrSdkCpu`/`PowdrSdkGpu` — so both arms build for GPU with `--features cuda` (plus `powdr` for the
APC arm). Two things are still missing before GPU numbers are trustworthy:

- **Nothing measures VRAM.** `peak_rss()` reads `VmHWM`, i.e. host memory; on GPU the binding
  resource is VRAM. Sample `nvidia-smi` alongside the run.
- **`--segment-memory-gib` may have to come down** from the CPU run's 15 GiB / 48.5 GiB peak host RSS
  to fit 32 GB of VRAM — which changes the segment count and breaks comparability with the published
  CPU figures. Decide it once, apply it to both arms, record it.
