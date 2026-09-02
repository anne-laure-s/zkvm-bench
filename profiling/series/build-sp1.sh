#!/bin/bash
# build-sp1.sh <outname> — build the Monad SP1 guest from the current worktree.
# ld.lld is not on PATH here; rust-lld from the SP1 toolchain is the same linker
# under another name, so expose it rather than installing anything.
set -euo pipefail
# MONAD is overridable and MUST be: series-build-sp1.sh checks each commit out in a tree of its
# own, and a hardcoded path here would build a DIFFERENT tree than the one under test — the
# failure build.sh records as "six commits in a row produced one identical ELF".
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
OUTD="${OUTD:-$(series_repo)/profiling/series-sp1}"
OUT="${1:-guest-sp1}"
mkdir -p "$HERE/bin" "$OUTD/elf"
LLD=$(ls ~/.sp1/toolchains/*/lib/rustlib/*/bin/rust-lld 2>/dev/null | head -1)
[ -n "$LLD" ] || { echo "no rust-lld in the SP1 toolchain"; exit 1; }
ln -sf "$LLD" "$HERE/bin/ld.lld"
export PATH="$HERE/bin:$HOME/.sp1/bin:$PATH"
# Overridable for the same reason as in build.sh: a toolchain A/B is otherwise
# indistinguishable from a source change in the resulting ELF.
export RISCV_TOOLCHAIN_DIR="${RISCV_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
export CC_riscv64im_succinct_zkvm_elf=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-gcc
export CXX_riscv64im_succinct_zkvm_elf=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++
ARCH="-march=rv64im -mabi=lp64 -mcmodel=medany -ffunction-sections -fdata-sections"
export CFLAGS_riscv64im_succinct_zkvm_elf="$ARCH -nostartfiles -nostdlib"
export CXXFLAGS_riscv64im_succinct_zkvm_elf="$ARCH -nostartfiles -nostdlib++ -fno-exceptions -fno-rtti"
cd "$MONAD/zkvm/sp1/script"
# Take cargo's OWN exit status, and refuse an ELF the build did not rewrite -- the same two guards
# build.sh carries, for the same reason. What was here before could not fail:
#   cargo build ... | grep -iE 'error|panicked' && exit 1
# under `pipefail` a failing cargo makes the PIPELINE non-zero, so the `&&` short-circuits and
# `exit 1` is never reached; and because the pipeline is the left side of an `&&`, `set -e` does not
# fire either. Execution fell through to the `find` below, which picked up the PREVIOUS build's ELF
# and copied it under the new name -- a stale binary recorded as this commit's, i.e. a run of
# commits all reporting the same sha and read as "did not move the guest".
ELF_GLOB="$MONAD/zkvm/sp1/target/release/build"
# `|| true` inside the substitution, and not just 2>/dev/null: on a tree that has never built SP1
# the directory does not exist, find exits non-zero, `pipefail` carries that to the pipeline and
# `set -e` kills the script HERE -- before cargo runs, with nothing printed. A cold worktree could
# not be built at all, which is exactly the state every commit of a fresh lineage starts in.
before=$( { find "$ELF_GLOB" -name 'monad-zkvm-guest-sp1.elf' -type f -exec stat -f%m {} + \
            2>/dev/null || true; } | sort -rn | head -1 ); before=${before:-0}
LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT
rc=0
cargo build --release > "$LOG" 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "BUILD FAILED (cargo rc=$rc):"
    grep -iE 'error|undefined symbol|cannot|not found|panicked' "$LOG" | head -6
    exit 1
fi
# The NEWEST match, not `head -1`: cargo keeps one build/<pkg>-<hash>/out per fingerprint, so the
# first path find happens to return can be an older one -- and then the freshness test below would
# fail on a build that actually succeeded.
ELF=$(find "$ELF_GLOB" -name 'monad-zkvm-guest-sp1.elf' -type f -exec stat -f'%m %N' {} + 2>/dev/null \
      | sort -rn | head -1 | cut -d' ' -f2-)
[ -n "$ELF" ] || { echo "BUILD FAILED: no ELF"; exit 1; }
after=$(stat -f%m "$ELF")
[ "$after" -gt "$before" ] || { echo "BUILD FAILED: ELF not rewritten (stale binary)"; exit 1; }
cp "$ELF" "$OUTD/elf/$OUT.elf"
echo "built $OUT sha=$(shasum -a256 "$OUTD/elf/$OUT.elf" | cut -c1-16)"
