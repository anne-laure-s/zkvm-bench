# `run-r10.sh` from a fresh clone

This is the shortest supported path to `profiling/results/compare.html` and
`profiling/results/series-r10.html`. Run every command from the repository root.

## 1. Install the executable tools

```bash
curl https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/ziskup/install.sh | bash
~/.zisk/bin/ziskup --version 1.1.0-alpha --nokey -y
~/.zisk/bin/ziskemu --version
~/.zisk/bin/cargo-zisk --version
```

The campaign is pinned to ZisK `1.1.0-alpha`; the preflight rejects a different
emulator or builder rather than mixing runtime-dependent measurements.

Monad's official profile needs the patched GCC 15.2.0 installed by
`profiling/experiments/zisk-dma-gcc15/build-gcc15.sh`. Its default output is:

```bash
export SERIES_STOCK_TOOLCHAIN_DIR="$HOME/riscv_gcc_multilib"
export SERIES_TOOLCHAIN_DIR="$HOME/.local/xPacks/zisk-dma-gcc-15.2.0"
```

The stock GCC 15.2.0 xPack is used by commits before DMA was introduced and is
also the target-side input used to build the patched compiler. The builder
accepts `ZISK_XPACK_DIR`, `ZISK_DMA_GCC_SRC`,
`ZISK_DMA_GCC_BUILD` and `ZISK_DMA_GCC_PREFIX`; the tracked
[`upstream/README.md`](../experiments/zisk-dma-gcc15/upstream/README.md) documents the source
preparation. Copying an existing installation to the same path is also sufficient—the preflight
checks the compiler before starting.

## 2. Create the dedicated Monad worktree

```bash
git clone https://github.com/category-labs/monad.git vendor/monad
git -C vendor/monad fetch origin al/zkvm-r10
MONAD_SERIES="$(cd .. && pwd)/monad-series"
git -C vendor/monad worktree add "$MONAD_SERIES" origin/al/zkvm-r10
```

The script never drives `vendor/monad`: it checks commits out with
`git checkout -f`, so it requires this clean, dedicated sibling worktree.
`SERIES_MONAD=/another/path` overrides the default.

## 3. Copy the non-Git artifacts

These artifacts cannot come from the repository. Copy them from the shared
artifact host or from a machine where this campaign already runs:

```bash
SOURCE=user@host:/absolute/path/to/zkvm-bench

rsync -a "$SOURCE/guests/monad/gen/canonical-2026-08-25815000-25815199-d49075fa3/" \
  guests/monad/gen/canonical-2026-08-25815000-25815199-d49075fa3/
rsync -a "$SOURCE/guests/monad/gen/zkvm-r8-canonical-25815000-25815199-0df7094a1/" \
  guests/monad/gen/zkvm-r8-canonical-25815000-25815199-0df7094a1/
rsync -a "$SOURCE/guests/ziskethone/fixtures/" guests/ziskethone/fixtures/

guests/monad/use-gen zkvm-r8-canonical-25815000-25815199-0df7094a1
```

The required payload is currently about 2.7 GB: 200 Monad witnesses plus
their post-state roots, and 200 ZisKethone inputs. `.expected_pv` files are
optional and are not read by execution, profiling, the root gate or compare.
The ZisKethone ELF is tracked at `guests/ziskethone/ziskethone.elf`.

To make `--skip-build` fast on the first machine handoff, optionally copy
`profiling/series/elf/`, `r10-index.tsv`, `r10-measure.tsv` and
`profiling/cache/`. They are caches, not prerequisites; reuse still validates
the commit, build environment and ELF hash.

## 4. Check, then run

```bash
./profiling/series/run-r10.sh --check
./profiling/series/run-r10.sh

# Later runs: reuse valid historical builds and build only new/missing commits.
./profiling/series/run-r10.sh --skip-build
```

`--check` reports every missing prerequisite in one pass and verifies aggregate
digests for both 200-block corpora. It also verifies that the current tip still inherits the
official-profile build recipe; a rebase that removed an `@after` anchor fails here, before a build.
The real run freezes
the locally known `origin/al/zkvm-r10`, builds and gates its tip, writes
`compare.html`, then builds/measures the historical lineage. A failed root gate
or failed compare stops before historical builds. A failed historical commit or
measurement stops before `series-r10.html`; partial series are never published.
Incremental builds are checkpointed separately from the published index, so an interrupted
`--skip-build` resumes the commits already validated instead of rebuilding them again.
