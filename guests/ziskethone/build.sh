#!/usr/bin/env bash
# build.sh — rebuild ziskethone.elf from the record beside it.
#
#   ./guests/ziskethone/build.sh            build, verify the sha256, install over ziskethone.elf
#   ./guests/ziskethone/build.sh --check    build and verify, install nothing
#
# Every fact this needs — the two pins, the two toolchain prefixes, the sha256 to reproduce — comes
# from `ziskethone.build.json`, the shared build record (cli/buildrec.sh). Nothing is duplicated
# here, so editing the record is how you change the build, and the file a human reads is the one the
# script obeys. Its pins are INPUT to the build, which is why they sit under `source` rather than at
# the top level: the required keys mean the same thing for every guest, generated or not.
#
# This build is byte-reproducible: it embeds no path strings, which is why the FILE sha256 is
# checked as an equality here and a mismatch is fatal. The Monad guests cannot make that promise
# and are not built this way at all — `cli/build-monad` defers to the recipe in the monad tree,
# which owns its own flags and audits its own output.
#
# Env:
#   XPACKS_DIR   where the RISC-V xPacks live (default ~/.local/xPacks)
#   WORKDIR      scratch build tree (default /tmp/ziskethone-build)
#   FORCE=1      build even though the driver checkout is not at DRIVER_COMMIT
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
. "$ROOT/cli/buildrec.sh"
BUILD_RECORD="$(brec_file ziskethone)"
[ -f "$BUILD_RECORD" ] || { echo "error: no build record at $BUILD_RECORD" >&2; exit 1; }

# A missing key is a typo in the record, not a default to invent: every one of them changes the
# binary, so guessing would build something nobody asked for under a name that says otherwise.
# Dotted keys reach into the record's nested objects.
rec() {
    local v
    v="$(BREC_F="$BUILD_RECORD" BREC_K="$1" python3 -c '
import json, os, sys
d = json.load(open(os.environ["BREC_F"]))
for part in os.environ["BREC_K"].split("."):
    d = d.get(part) if isinstance(d, dict) else None
sys.stdout.write("" if d is None else str(d))')"
    [ -n "$v" ] || { echo "error: $1 missing from $BUILD_RECORD" >&2; exit 1; }
    printf '%s' "$v"
}
# The comment block above IS the usage text, so the two cannot drift. Two plain substitutions
# rather than `s/^# \?//`: BSD sed has no `\?` and would match a literal `# ?`.
bh_usage() { sed -n '2,/^set -euo/p' "$1" | sed '$d; s/^#//; s/^ //'; }

CHECK_ONLY=0
case "${1:-}" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) bh_usage "$0"; exit 0 ;;
    '') ;;
    *) echo "usage: build.sh [--check]" >&2; exit 2 ;;
esac

UPSTREAM_URL="$(rec source.upstream_url)"
UPSTREAM_COMMIT="$(rec commit)"
DRIVER_REPO="$ROOT/$(rec source.driver_repo)"
DRIVER_COMMIT="$(rec source.driver_commit)"
DRIVER="$DRIVER_REPO/$(rec source.driver)"
XPACKS="${XPACKS_DIR:-$HOME/.local/xPacks}"
XPACK="$XPACKS/$(rec toolchain.xpack)"
DMA_GCC="$XPACKS/$(rec toolchain.dma_gcc)"
ELF_OUT="$ROOT/$(rec elf)"
WANT_SHA="$(rec elf_sha256)"
WANT_BYTES="$(rec expect.bytes)"
MARKERS_MIN="$(rec expect.dma_marker_sites_min)"
SUB="$DRIVER_REPO/third_party/ziskethone"
W="${WORKDIR:-/tmp/ziskethone-build}"

# ── prerequisites ────────────────────────────────────────────────────────────────────────
# Each of these is a thing the build silently does differently rather than fails on, so they
# are checked up front where the message can name the fix.
[ -d "$DRIVER_REPO/.git" ] || {
    echo "error: no zisk-eth-client checkout at $DRIVER_REPO" >&2
    echo "       run ./cli/install-vendors" >&2; exit 1; }

have="$(git -C "$DRIVER_REPO" rev-parse HEAD)"
if [ "$have" != "$DRIVER_COMMIT" ]; then
    echo "warning: $DRIVER_REPO is at ${have:0:9}, the record pins ${DRIVER_COMMIT:0:9}" >&2
    echo "         the driver sets the guest's compiler flags, so this changes the binary." >&2
    [ "${FORCE:-0}" = 1 ] || {
        echo "         git -C $DRIVER_REPO checkout $DRIVER_COMMIT   (or FORCE=1 to build anyway)" >&2
        exit 1; }
fi

[ -f "$DRIVER" ] || { echo "error: driver not found at $DRIVER" >&2; exit 1; }

[ -x "$XPACK/bin/riscv-none-elf-g++" ] || {
    echo "error: no RISC-V xPack at $XPACK" >&2
    echo "       install xpack-dev-tools/riscv-none-elf-gcc there, or set XPACKS_DIR." >&2
    echo "       (the patched DMA compiler is NOT a prerequisite — the driver builds it)" >&2
    exit 1; }

# The submodule carries the guest sources; the pin in the record wins over whatever the
# superproject happens to have checked out, because the record is what names this ELF.
[ -d "$SUB/.git" ] || [ -f "$SUB/.git" ] || {
    echo "==> initialising third_party/ziskethone from $UPSTREAM_URL"
    git -C "$DRIVER_REPO" submodule update --init third_party/ziskethone; }

if [ "$(git -C "$SUB" rev-parse HEAD)" != "$UPSTREAM_COMMIT" ]; then
    echo "==> checking ziskethone out at ${UPSTREAM_COMMIT:0:9} ($(rec source.upstream_branch))"
    git -C "$SUB" fetch --quiet origin "$UPSTREAM_COMMIT" 2>/dev/null || git -C "$SUB" fetch --quiet origin
    git -C "$SUB" -c advice.detachedHead=false checkout --quiet "$UPSTREAM_COMMIT"
fi

# ── build ────────────────────────────────────────────────────────────────────────────────
# Out of tree, on a COPY: the driver writes cmake output and a toolchain stamp into the guest
# directory, and vendor/ is a checkout we keep clean. The copy also proves the point the
# sha256 check rests on — different absolute paths, identical bytes.
echo "==> building in $W"
rm -rf "$W"; mkdir -p "$W/shim"
cp -R "$SUB" "$W/ziskethone"

# macOS has no nproc; the driver calls it for -j. Shim only when it is actually missing, so a
# Linux host keeps its own.
if ! command -v nproc >/dev/null 2>&1; then
    printf '#!/bin/sh\nsysctl -n hw.ncpu\n' > "$W/shim/nproc"
    chmod +x "$W/shim/nproc"
fi

PATH="$W/shim:$XPACK/bin:$PATH" \
ZISKETHONE_DIR="$W/ziskethone" \
ZISK_TOOLCHAIN_PREFIX="$XPACK/bin" \
ZISK_DMA_GCC_PREFIX="$DMA_GCC" \
    bash "$DRIVER"

BUILT="$W/ziskethone/cpp-guest/zisk/build/zisk_eth_guest.elf"
[ -f "$BUILT" ] || { echo "error: driver finished but no ELF at $BUILT" >&2; exit 1; }

# ── verify ───────────────────────────────────────────────────────────────────────────────
# The DMA site count first, because it is the one check no environment confusion can fake: the
# lowering emits thousands of `csrs 0x813,rs` inline, while a build that quietly fell back to a
# stock compiler carries only the two inside ziskos's own thunks.
BH_SHA="$(shasum -a256 "$BUILT" | cut -d' ' -f1)"
BH_BYTES="$(wc -c < "$BUILT" | tr -d ' ')"
BH_MOVMEM="$("$XPACK/bin/riscv-none-elf-objdump" -d "$BUILT" \
             | grep -cE 'csrs[[:space:]]+0x813,' || true)"

echo
echo "  sha256    $BH_SHA"
echo "  bytes     $BH_BYTES"
echo "  DMA sites $BH_MOVMEM (floor $MARKERS_MIN)"

[ "$BH_MOVMEM" -ge "$MARKERS_MIN" ] || {
    echo "error: only $BH_MOVMEM DMA marker sites — the build fell back to a stock compiler." >&2
    echo "       that guest runs, passes the root gate, and answers a different question." >&2
    exit 1; }

if [ "$BH_SHA" != "$WANT_SHA" ]; then
    echo "error: sha256 mismatch — the record expects $WANT_SHA" >&2
    echo "       the build is reproducible, so this means an input moved: check the two pins," >&2
    echo "       then $XPACK. Update $BUILD_RECORD only once you know WHICH input changed." >&2
    exit 1
fi
[ "$BH_BYTES" = "$WANT_BYTES" ] || {
    echo "error: size $BH_BYTES, record says $WANT_BYTES" >&2; exit 1; }

echo "  → matches the record"

if [ "$CHECK_ONLY" = 1 ]; then
    echo "  --check: left $ELF_OUT untouched"
    exit 0
fi
cp "$BUILT" "$ELF_OUT"
# Re-stamp through the shared writer rather than by hand: it recomputes elf_sha256 from what is now
# on disk and preserves every other key, so the record cannot drift from the binary beside it.
brec_stamp "$BUILD_RECORD" "$ELF_OUT" "$UPSTREAM_COMMIT"
echo "  installed $ELF_OUT"
