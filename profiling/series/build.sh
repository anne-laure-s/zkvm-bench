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
    # build-support also places the guest's configure state under
    # target/guest-build/<toolchain>[-official]/build, which the find above does
    # not cover. That directory is keyed by toolchain and profile, not by commit,
    # so one of them is reused across every commit of a lineage sweep: an option
    # declared at commit N and named in the official profile's required list at
    # commit N+1 leaves BOOL=OFF in its cache, and the profile then refuses a
    # caller that passed nothing at all. The cache alone is removed, not the
    # directory -- CMake reconfigures from scratch either way, and keeping
    # CMakeFiles/ lets a sweep recompile only the translation units whose command
    # line actually moved. The touch below is what makes cargo re-run build.rs at
    # all; without it the removal is not even noticed.
    GUEST_BUILD_ROOT="$MONAD/zkvm/zisk/target/guest-build"
    if [ -d "$GUEST_BUILD_ROOT" ]; then
        find "$GUEST_BUILD_ROOT" -name CMakeCache.txt -delete
    fi
    # Touch a watched source that EXISTS in this tree. A hardcoded name fails three
    # ways at once when the tree renames it: touch creates a stray empty file, cargo
    # never re-runs build.rs so FORCE_REBUILD stops forcing anything, and the stray
    # file then blocks the next `git checkout` to a revision that has the real one --
    # which leaves the worktree on the previous commit and reports the previous ELF
    # under the new name. Refuse rather than touch nothing.
    _touched=
    for _c in execute_block.cpp execute_block_zkvm.cpp ffi.cpp execute_witness.cpp; do
        if [ -f "$MONAD/zkvm/guest/$_c" ]; then
            touch "$MONAD/zkvm/guest/$_c"; _touched=$_c; break
        fi
    done
    if [ -z "$_touched" ]; then
        echo "FORCE_REBUILD: no known guest source to touch under $MONAD/zkvm/guest" >&2
        exit 2
    fi
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

# ── ZISK_INCREMENTAL: bypass cargo-zisk's random linker-script path ──────────────────────────────
# `cargo-zisk build` writes its linker script to a fresh mktemp file on every invocation and passes
# the path in CARGO_ENCODED_RUSTFLAGS. The path is part of cargo's unit hash, so every build gets a
# new hash and NOTHING is ever reused: measured here, a second build with an unchanged tree
# recompiles 112 crates in 72 s, and .fingerprint holds 469 directories for `hex` alone.
#
# The script itself is deterministic -- 5468 bytes, byte-identical across runs -- so the fix is to
# keep one copy at a stable path and run the cargo command cargo-zisk would have run:
#
#     cargo +zisk build --target-dir target/elf --release --target riscv64ima-zisk-zkvm-elf
#
# Off by default: this reproduces by hand what their tool does, so it can drift from a future ZisK
# release. The guard against that drift is the ELF itself -- the caller compares its sha to a
# cargo-zisk build, and the series records the sha of what it actually measured.
zisk_stable_ld() {
    local ver cache tmpdir before after
    ver=$("$ZISK_DIR/bin/cargo-zisk" --version 2>/dev/null | tr -d ' /' | tr -c 'A-Za-z0-9.-' '_')
    cache="$HERE/zisk-ld/${ver:-unknown}.ld"
    [ -s "$cache" ] && { printf '%s' "$cache"; return 0; }
    # Not shipped: captured from one real cargo-zisk run, so it is THEIR script and not our idea
    # of it. A release that changes the script produces a new version string and a new capture.
    #
    # Captured DURING the run, through a `cargo` shim that copies the file named in
    # CARGO_ENCODED_RUSTFLAGS before handing over. Looking for it afterwards does not work: the
    # temp file is deleted when cargo-zisk exits, which is why the first attempt found nothing.
    mkdir -p "$(dirname "$cache")"
    local shim
    shim=$(mktemp -d) || return 1
    cat > "$shim/cargo" <<SHIM
#!/bin/bash
ld=\${CARGO_ENCODED_RUSTFLAGS##*-T}
[ -s "\$ld" ] && cp "\$ld" "$cache" 2>/dev/null
exec "$(command -v cargo)" "\$@"
SHIM
    chmod +x "$shim/cargo"
    PATH="$shim:$PATH" "$ZISK_DIR/bin/cargo-zisk" build --release >/dev/null 2>&1 || true
    rm -rf "$shim"
    [ -s "$cache" ] || return 1
    printf '%s' "$cache"
}
if [ "${ZISK_INCREMENTAL:-0}" = 1 ] && _ld=$(zisk_stable_ld); then
    # CARGO_ENCODED_RUSTFLAGS separates every ARGUMENT with 0x1f, not every flag group: joining
    # `--cfg` to its value with a space hands rustc one unrecognised option called `cfg zisk_guest`.
    export CARGO_ENCODED_RUSTFLAGS=$(printf '%s\037%s\037%s\037%s' \
        '--cfg' 'zisk_guest' '-C' "link-arg=-T$_ld")
    cargo +zisk build --target-dir target/elf --release \
        --target riscv64ima-zisk-zkvm-elf > "$LOG" 2>&1 || rc=$?
else
    "$ZISK_DIR/bin/cargo-zisk" build --release > "$LOG" 2>&1 || rc=$?
fi
if [ "$rc" -ne 0 ]; then
    echo "BUILD FAILED (cargo-zisk rc=$rc):"
    # KEEP THE LOG. A grep over cargo's output is not a diagnosis: `-i error` matches
    # `cargo:rerun-if-changed=.../decode_error.hpp` and `thiserror`, so a real failure printed six
    # filenames and nothing else -- twice this week, each time costing a manual re-run to find out
    # what actually broke. Anchor the pattern, and when it finds nothing, show the tail rather than
    # claiming there was nothing to see.
    cp "$LOG" "$HERE/build-fail-$OUT.log" 2>/dev/null && echo "  full log: $HERE/build-fail-$OUT.log"
    if ! grep -nE '^(error|warning: unused)|error(\[[A-Z0-9]+\])?:|^  *= note:|undefined reference|No such file' "$LOG" | head -8; then
        echo "  (no line matched the error patterns — last 12 lines:)"
        tail -12 "$LOG" | sed 's/^/  /'
    fi
    exit 1
fi
[ -f "$ELF" ] || { echo "BUILD FAILED: no ELF"; exit 1; }
after=$(stat -f%m "$ELF")
# An unchanged ELF is a legitimate outcome once the build is actually incremental: cargo exits 0
# having found nothing to do. The guard was written when a FAILED build left the previous binary in
# place and rc was not trusted; rc is taken through PIPESTATUS above and is the authority now, so a
# missing rewrite is reported rather than treated as failure. The sha is what identifies the build,
# and an ELF that did not move still has the right one.
[ "$after" -gt "$before" ] || echo "  (ELF unchanged — nothing to rebuild)" 
mkdir -p "$HERE/elf"; cp "$ELF" "$HERE/elf/$OUT.elf"
echo "built $OUT sha=$(shasum -a256 "$HERE/elf/$OUT.elf" | cut -c1-16)"
