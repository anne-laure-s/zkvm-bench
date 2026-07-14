#!/usr/bin/env bash
# WORKAROUND for unprivileged Docker boxes (vast.ai) where `memlock` is hard-capped
# at 64 KB and CANNOT be raised. ZisK's ASM emulator mmaps the ROM/RAM/input regions
# with MAP_LOCKED by default → those locked mmaps fail with
#   "Failed calling mmap(rom) errno=11=Resource temporarily unavailable"
#   "Shmem creation for mo failed"
# at STARTING_ASM_MICROSERVICES (both `cargo-zisk setup --asm` and `prove --asm`).
#
# There IS a runtime flag `-u/--unlock-mapped-memory` that sets map_locked_flag=0, but
# the SDK does NOT propagate it from `cargo-zisk setup/prove` down to the spawned MO/MT/RH
# microservice (confirmed: setup.rs/prove.rs set it on the builder, yet the microservice
# still mmaps with MAP_LOCKED). So we patch the C DEFAULT instead: map_locked_flag = 0.
# With plenty of RAM (this box has 500+ GB) unlocking is a no-op perf-wise — pages are
# never actually swapped. Then we invalidate the cached ROM binaries so `cargo-zisk setup`
# recompiles the microservices from the patched source.
#
#   bash fix-memlock-patch.sh          # patch + purge cache
set -uo pipefail
G="$HOME/.zisk/zisk/emulator-asm/src/globals.c"
[ -f "$G" ] || { echo "ERROR: $G not found (run 00-install-once first)" >&2; exit 1; }

echo "== globals.c BEFORE =="; grep -n "map_locked_flag *=" "$G"
[ -f "$G.orig" ] || cp "$G" "$G.orig"
sed -i 's|^int map_locked_flag = MAP_LOCKED;|int map_locked_flag = 0; /* PATCH vast.ai: memlock 64KB hard-cap, unlock by default (was MAP_LOCKED) */|' "$G"
echo "== globals.c AFTER =="; grep -n "map_locked_flag *=" "$G"
grep -q "^int map_locked_flag = 0;" "$G" || { echo "ERROR: patch did not apply" >&2; exit 1; }

echo "== purge cached ROM microservice binaries (force recompile from patched src) =="
rm -fv "$HOME"/.zisk/cache/*-hints-mo.asm "$HOME"/.zisk/cache/*-hints-mo.bin \
       "$HOME"/.zisk/cache/*-hints-mt.asm "$HOME"/.zisk/cache/*-hints-mt.bin \
       "$HOME"/.zisk/cache/*-hints-rh.asm "$HOME"/.zisk/cache/*-hints-rh.bin 2>/dev/null || true

echo "== leftover ROM/asm shm cleanup =="
rm -f /dev/shm/ZISK* /dev/shm/*zisk* 2>/dev/null || true

echo "== remaining cache =="; ls -1 "$HOME/.zisk/cache/" 2>/dev/null
echo "== done — now re-run 01-setup-elf.sh =="
