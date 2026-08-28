#!/bin/bash
# build-reference.sh <outname> [OPT=ON|OFF ...] — build the reference ZisK guest.
#
# `build.sh` builds what CMake defaults to, and CMake defaults every performance
# option OFF because each one needs something the plain toolchain does not have.
# So the default build is nobody's guest: not what we measure, not what we would
# ship. Three separate overrides stand between the two, and forgetting any of
# them yields a guest that runs, passes the root gate, and answers a different
# question -- which is exactly how an A/B ends up comparing two chains and
# reading the difference as a lever.
#
# This turns them all on, and prints a fingerprint of what it actually built so
# that a mismatched pair is visible in the log rather than in the final number.
#
#   build-reference.sh ref                        # every lever on
#   build-reference.sh base MONAD_ZKVM_FUSE=OFF   # exactly one flipped, for an A/B
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: build-reference.sh <outname> [OPT=ON|OFF ...]}"; shift || true

# The patched compiler. -mzisk-dma is a backend lowering that is not upstream
# gcc; see profiling/experiments/zisk-dma-gcc15/.
DMA_GCC="${ZISK_DMA_GCC:-$HOME/.local/xPacks/zisk-dma-gcc-15.2.0}"

# Every performance option, with the value the reference guest wants. Anything
# passed on the command line overrides one of these; an unknown name is a typo
# and stops the build rather than being silently ignored.
DEFAULTS="MONAD_ZKVM_ZISK_DMA=ON \
MONAD_ZKVM_TABLE_ARG=ON \
MONAD_ZKVM_FUSE=ON \
MONAD_ZKVM_KECCAKF_MEMO=ON \
MONAD_ZKVM_KECCAK_SITES=OFF \
MONAD_ZKVM_SELFTEST=OFF \
MONAD_ZKVM_OFFICIAL_PROFILE=OFF"

# Plain strings, not an associative array: /bin/bash here is 3.2 and has none.
opt_of() {  # opt_of NAME -> the value, after any override
    _v=""
    for _kv in $DEFAULTS; do [ "${_kv%%=*}" = "$1" ] && _v=${_kv#*=}; done
    for _kv in $OVERRIDES; do [ "${_kv%%=*}" = "$1" ] && _v=${_kv#*=}; done
    printf '%s' "$_v"
}
# The option list lives in the guest's CMakeLists, in the other repository. If
# it grows one this script does not know, the reference build would silently
# omit it -- which is the failure this script exists to prevent, arriving by a
# different door. So read the truth from there and refuse to guess.
CML="$MONAD/zkvm/guest/CMakeLists.txt"
[ -f "$CML" ] || { echo "error: no guest CMakeLists at $CML" >&2; exit 1; }
MISSING=""
for k in $(sed -n 's/^[[:space:]]*option(\(MONAD_ZKVM_[A-Z_]*\).*/\1/p' "$CML"); do
    echo "$DEFAULTS" | tr ' ' '\n' | grep -q "^$k=" || MISSING="$MISSING $k"
done
if [ -n "$MISSING" ]; then
    echo "error: the guest declares options this script has no policy for:$MISSING" >&2
    echo "       add each to DEFAULTS with the value the reference guest wants," >&2
    echo "       then re-run. Refusing to build a guest that is missing one." >&2
    exit 1
fi

OVERRIDES=""
for kv in "$@"; do
    k=${kv%%=*}
    echo "$DEFAULTS" | tr ' ' '\n' | grep -q "^$k=" ||
        { echo "error: unknown option '$k'" >&2; exit 1; }
    OVERRIDES="$OVERRIDES $kv"
done

# Fail closed on the toolchain. A stock compiler rejects -mzisk-dma, so a build
# that forgets ZISK_DMA_GCC would otherwise fall back to a guest without the
# lowering and quietly measure it instead.
if [ "$(opt_of MONAD_ZKVM_ZISK_DMA)" = ON ]; then
    CXX="$DMA_GCC/bin/riscv-none-elf-g++"
    [ -x "$CXX" ] || { echo "error: no patched compiler at $DMA_GCC" >&2
                       echo "       build one with profiling/experiments/zisk-dma-gcc15/build-gcc15.sh" >&2
                       exit 1; }
    echo 'int main(){}' | "$CXX" -x c++ -march=rv64ima_zicsr -mabi=lp64 -mzisk-dma \
        -fsyntax-only - 2>/dev/null ||
        { echo "error: $CXX does not accept -mzisk-dma" >&2; exit 1; }
    export RISCV_TOOLCHAIN_DIR="$DMA_GCC"
fi

DEFS=""
for kv in $DEFAULTS; do
    k=${kv%%=*}
    DEFS="$DEFS${DEFS:+;}$k=$(opt_of "$k")"
done
export MONAD_ZKVM_CMAKE_DEFINES="$DEFS"
# The guest's CMakeLists appends its own -march last, so this only has to agree.
export MARCH="${MARCH:-rv64ima_zbb_zbs_zbkb_zicsr}"

MONAD="${MONAD:?set MONAD to the monad worktree}" bash "$HERE/build.sh" "$OUT"

# ── fingerprint ──────────────────────────────────────────────────────────────
# What was built, not what was asked for. The port-site counts are the check
# that matters: they separate a DMA guest (~9,300 movmem) from a plain one
# (~4,550, all of them inside ziskos's own Rust precompiles), and no amount of
# environment confusion can fake them.
ELF="$HERE/elf/$OUT.elf"

# The disassembler that goes with the toolchain, under either of the two names
# the xPack installs. Nothing here is macOS- or Linux-specific: bash 3.2 (what
# macOS ships) has no associative arrays, which is why the options above are
# plain strings.
TC="${RISCV_TOOLCHAIN_DIR:-/Users/anne-laure/riscv_gcc_multilib}"
OBJDUMP=""
for cand in "$TC/bin/riscv64-unknown-elf-objdump" "$TC/bin/riscv-none-elf-objdump" \
            riscv64-unknown-elf-objdump riscv-none-elf-objdump llvm-objdump; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then OBJDUMP="$cand"; break; fi
done
[ -n "$OBJDUMP" ] || { echo "error: no riscv objdump found for the fingerprint" >&2; exit 1; }

OBJDUMP="$OBJDUMP" python3 - "$ELF" <<'PY'
import subprocess, sys, re, collections, os
OD = os.environ["OBJDUMP"]
out = subprocess.run([OD, '-d', sys.argv[1]], capture_output=True, text=True).stdout
c = collections.Counter()
for line in out.splitlines():
    m = re.match(r'\s+[0-9a-f]+:\s+([0-9a-f]{8})\s', line)
    if not m:
        continue
    w = int(m.group(1), 16)
    if (w & 0x7f) == 0x73 and ((w >> 12) & 7) in (1, 2, 3, 5, 6, 7):
        c[(w >> 20) & 0xfff] += 1
print(f"  ports DMA : movmem(0x813)={c[0x813]:,}  setmem(0x816)={c[0x816]:,}  "
      f"cmpmem(0x814)={c[0x814]:,}")
PY
echo "  options   : $DEFS"
echo "  toolchain : ${RISCV_TOOLCHAIN_DIR:-/Users/anne-laure/riscv_gcc_multilib}"
echo "  march     : $MARCH"
