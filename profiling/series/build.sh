#!/bin/bash
# build.sh <outname> [EXTRA cflags] — build the Monad ZisK guest from the current
# worktree state and copy the ELF into the series cache.
set -euo pipefail
# Overridable, and it MUST be: series-build-lineage.sh checks each commit out in a worktree
# of its own, and a hardcoded path here silently builds a different tree than the one under
# test -- six commits in a row produced one identical ELF before this was fixed.
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
OUT="${1:-guest}"
# Overridable for the same reason MONAD is: a toolchain A/B (the zisk-dma GCC, say)
# is otherwise indistinguishable from a source change in the resulting ELF.
export RISCV_TOOLCHAIN_DIR="${RISCV_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
export CC_riscv64ima_zisk_zkvm_elf=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-gcc
export CXX_riscv64ima_zisk_zkvm_elf=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++
ARCH="-march=${MARCH:-rv64ima} -mabi=lp64 -mcmodel=medany -ffunction-sections -fdata-sections ${EXTRA:-}"
export CFLAGS_riscv64ima_zisk_zkvm_elf="$ARCH -nostartfiles -nostdlib"
export CXXFLAGS_riscv64ima_zisk_zkvm_elf="$ARCH -nostartfiles -nostdlib++ -fno-exceptions -fno-rtti"
cd "$MONAD/zkvm/zisk"
ELF="$MONAD/zkvm/zisk/target/elf/riscv64ima-zisk-zkvm-elf/release/monad-zkvm-zisk"
# The lineage builder sets this for every Monad commit, even when Cargo would otherwise decide the
# checkout/env is unchanged from the previous run. Touch one watched C++ source: Cargo reruns
# build.rs, CMake recompiles and relinks the Monad guest, while deterministic bytes retain the same
# sha and therefore reuse the measurement cache. Ziskethone is not built by this path at all.
if [ "${FORCE_REBUILD:-0}" = 1 ]; then
    # cmake-rs persists configure state below Cargo's package build directory.
    # Keeping it would let an ON/OFF option from another commit leak into this
    # one. Remove only Monad's generated CMake sub-build; Rust dependencies stay
    # cached in target/.
    CARGO_BUILD_ROOT="$MONAD/zkvm/zisk/target/elf/riscv64ima-zisk-zkvm-elf/release/build"
    if [ -d "$CARGO_BUILD_ROOT" ]; then
        find "$CARGO_BUILD_ROOT" -mindepth 3 -maxdepth 3 -type d \
            -path '*/monad-zkvm-zisk-*/out/build' -exec rm -rf {} +
    fi
    touch "$MONAD/zkvm/guest/execute_block_zkvm.cpp"
fi
# The ELF existing is NOT evidence the build succeeded: a failed build leaves the
# previous one in place, and copying that reports the old binary under a new name.
# Take cargo's exit status through PIPESTATUS, and refuse an ELF older than the
# build we just ran.
before=$(stat -f%m "$ELF" 2>/dev/null || echo 0)
LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT
# `|| rc=$?` and not a bare call: under set -e a failing build would kill the
# script before the diagnostic below could print, so the failure would be silent.
rc=0
# Overridable for the same reason MONAD and RISCV_TOOLCHAIN_DIR are: the guest LINKS this
# install's libziskclib.a, so the ZisK release is a build input like the compiler. Two
# installs live side by side here (~/.zisk = 1.1.0-alpha, ~/.zisk-1.2 = 1.2.0-alpha) and a
# hardcoded path makes a runtime A/B indistinguishable from a source change in the ELF.
ZISK_DIR="${ZISK_DIR:-$HOME/.zisk}"
"$ZISK_DIR/bin/cargo-zisk" build --release > "$LOG" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "BUILD FAILED (cargo-zisk rc=$rc):"
    grep -iE 'error|undefined symbol|cannot|not found' "$LOG" | head -6
    exit 1
fi
[ -f "$ELF" ] || { echo "BUILD FAILED: no ELF"; exit 1; }
after=$(stat -f%m "$ELF")
[ "$after" -gt "$before" ] || { echo "BUILD FAILED: ELF not rewritten (stale binary)"; exit 1; }
mkdir -p "$HERE/elf"; cp "$ELF" "$HERE/elf/$OUT.elf"
echo "built $OUT sha=$(shasum -a256 "$HERE/elf/$OUT.elf" | cut -c1-16)"
