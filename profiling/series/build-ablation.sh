#!/bin/bash
# build-ablation.sh <outname> OPT=ON|OFF [OPT=...] — build ONE ablation arm.
#
#   build-ablation.sh no-fuse MONAD_ZKVM_FUSE=OFF     # every lever on except that one
#
# An ablation arm contradicts the official profile by construction, so this path
# turns the profile OFF and spells every option out. That is the whole reason it
# exists, and the reason it cannot be folded into cli/build-monad: the profile
# FORCES its option set and FATAL_ERRORs on a contradicting -D, so there is no
# way to ask it for "everything on except FUSE".
#
# For the official guest, use `cli/build-monad` instead. It asks the guest's own
# CMakeLists for MONAD_ZKVM_OFFICIAL_PROFILE and sets nothing else. This script
# used to have a second path that built the official guest too, and that was the
# bug: two ways to build one guest gave it two identities (the flag string is
# signed into the binary), so the cache measured it twice and no pinned sha could
# be met by the other path.
#
# `build.sh` builds what CMake defaults to, and CMake defaults every performance
# option OFF because each one needs something the plain toolchain does not have.
# So the default build is nobody's guest: not what we measure, not what we would
# ship. Forgetting one option yields a guest that runs, passes the root gate, and
# answers a different question -- which is exactly how an A/B ends up comparing
# two chains and reading the difference as a lever. Hence: every option named,
# every time, and a fingerprint of what was actually built.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: build-ablation.sh <outname> OPT=ON|OFF [OPT=...]}"; shift || true
[ "$#" -gt 0 ] || { echo "error: name at least one option to flip -- an arm that flips nothing is" >&2
                    echo "       the official guest, and that is cli/build-monad's job." >&2
                    echo "usage: build-ablation.sh <outname> OPT=ON|OFF [OPT=...]" >&2; exit 1; }

# The patched compiler. -mzisk-dma is a backend lowering that is not upstream
# gcc; see profiling/experiments/zisk-dma-gcc15/.
DMA_GCC="${ZISK_DMA_GCC:-$HOME/.local/xPacks/zisk-dma-gcc-15.2.0}"

# Every performance option, with the value the reference guest wants. Anything
# passed on the command line overrides one of these; an unknown name is a typo
# and stops the build rather than being silently ignored.
# These are the values the official guest would have, so an arm differs from it
# in exactly what you name on the command line. They are spelled out because the
# profile is off here -- with it on, the guest's own CMakeLists would force them
# and this list would be a second copy waiting to drift.
DEFAULTS="MONAD_ZKVM_ZISK_DMA=ON \
MONAD_ZKVM_TABLE_ARG=ON \
MONAD_ZKVM_FUSE=ON \
MONAD_ZKVM_KECCAKF_MEMO=ON \
MONAD_ZKVM_KECCAK_SITES=OFF \
MONAD_ZKVM_CHECK_SEQUENTIAL_MERGE=OFF \
MONAD_ZKVM_NO_DIRTY_ACCOUNTS=ON \
MONAD_ZKVM_NO_MERGE_CONSTRAINTS=ON \
MONAD_ZKVM_VARCODE_CACHE=ON \
MONAD_ZKVM_WIDE_MEMORY_SIZE=ON \
MONAD_ZKVM_SELFTEST=OFF"

# Plain strings, not an associative array: /bin/bash here is 3.2 and has none.
opt_of() {
    _v=""
    for _kv in $DEFAULTS; do [ "${_kv%%=*}" = "$1" ] && _v=${_kv#*=}; done
    for _kv in $OVERRIDES; do [ "${_kv%%=*}" = "$1" ] && _v=${_kv#*=}; done
    printf '%s' "$_v"
}

# The guest may declare an option this script has never heard of. With the profile
# off, setting the options is ours to get right, and an omission would silently
# build something else.
CML="$MONAD/zkvm/guest/CMakeLists.txt"
[ -f "$CML" ] || { echo "error: no guest CMakeLists at $CML" >&2; exit 1; }

OVERRIDES=""
for kv in "$@"; do
    k=${kv%%=*}
    echo "$DEFAULTS" | tr ' ' '\n' | grep -q "^$k=" ||
        { echo "error: unknown option '$k'" >&2; exit 1; }
    OVERRIDES="$OVERRIDES $kv"
done
MISSING=""
for k in $(sed -n 's/^[[:space:]]*option(\(MONAD_ZKVM_[A-Z_]*\).*/\1/p' "$CML"); do
    [ "$k" = MONAD_ZKVM_OFFICIAL_PROFILE ] && continue
    echo "$DEFAULTS" | tr ' ' '\n' | grep -q "^$k=" || MISSING="$MISSING $k"
done
[ -z "$MISSING" ] || {
    echo "error: the guest declares options this script cannot set:$MISSING" >&2
    echo "       add each to DEFAULTS, then re-run." >&2; exit 1; }

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

DEFS="MONAD_ZKVM_OFFICIAL_PROFILE=OFF"
for kv in $DEFAULTS; do
    k=${kv%%=*}
    DEFS="$DEFS;$k=$(opt_of "$k")"
done
export MONAD_ZKVM_CMAKE_DEFINES="$DEFS"
# No default MARCH. The guest's CMakeLists appends its own -march last, so a value here cannot
# change an instruction -- it only lengthens the flag string, and under the official profile that
# string is hashed into the binary, which is how the same program once came out under two
# identities. Off that path it is merely useless. Pass one only to fork the identity on purpose.
[ -z "${MARCH:-}" ] || export MARCH

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
TC="${RISCV_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
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
echo "  toolchain : ${RISCV_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
# Unset is the normal case, and it is worth printing as such: the guest appends its own -march,
# and a value here would fork the build signature. See the note beside the export above.
echo "  march     : ${MARCH:-unset - the guest supplies its own}"
