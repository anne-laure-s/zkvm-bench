#!/usr/bin/env bash
# 01 — per-ELF ROM / proving setup ON THE BOX. Run ONCE per ELF (re-run only when
# the ELF changes). This is the ZisK step with no SP1 equivalent: it precomputes
# program-specific proving artifacts tied to the installed proving key + GPU build.
# It is NOT a per-block step and is NOT part of the measured proving time.
#
#   ./01-setup-elf.sh <path-to-elf>
set -uo pipefail
export PATH="$HOME/.zisk/bin:$PATH"   # ziskup installs here; a fresh shell may not have it yet

ELF="${1:?usage: ./01-setup-elf.sh <path-to-elf>}"
[[ -f "$ELF" ]] || { echo "ERROR: ELF not found: $ELF" >&2; exit 1; }
ELF="$(cd "$(dirname "$ELF")" && pwd)/$(basename "$ELF")"

command -v cargo-zisk >/dev/null 2>&1 || { echo "ERROR: cargo-zisk not found at ~/.zisk/bin (run 00-install-once.sh)" >&2; exit 1; }

# The proving key must be installed first (setup reads it). If missing, ziskup didn't
# fetch it during 00-install-once.
PK="${PROVING_KEY:-$HOME/.zisk/provingKey}"
[[ -d "$PK" ]] || { echo "ERROR: proving key not found at $PK — install it: ziskup --provingkey" >&2; exit 1; }

# LOCAL single-process proving needs the ASM + hints setup (the reth guest proves
# WITH precompile hints, and `cargo-zisk prove --hints` requires `--asm`). Verified
# against cargo-zisk v1.0.0-alpha: `setup --asm --hints`.
#
# -u / --unlock-mapped-memory: REQUIRED on unprivileged Docker boxes (vast.ai).
# The ASM emulator mmaps the ROM (and RAM/input regions) with MAP_LOCKED by default;
# vast.ai containers pin `memlock` at 64 KB (hard limit, non-raisable), so the locked
# mmap fails with "mmap(rom) errno=11 (EAGAIN) / Shmem creation failed". `-u` sets
# map_locked_flag=0 (pages swappable). With 500 GB RAM this is a no-op perf-wise —
# the pages are never actually swapped. The SAME flag must go on `cargo-zisk prove`
# (see zisk-runner) and the distributed worker (`--unlock-mapped-memory`, see start.sh).
echo "== cargo-zisk setup -e $ELF --asm --hints -u =="
# shellcheck disable=SC2086
if ! cargo-zisk setup -e "$ELF" --asm --hints -u ${SETUP_EXTRA_FLAGS:-}; then
  echo "ERROR: cargo-zisk setup failed (see message above; proving key missing/incomplete?)" >&2
  exit 1
fi
echo "== done (local setup). You can now: zisk-runner --mode prove ... (backend=local) =="
echo
echo "NOTE: for the DISTRIBUTED (coordinator/worker) path, setup is done on the"
echo "coordinator instead, once it's up:"
echo "    cargo-zisk remote setup -e $ELF --hints --coordinator http://127.0.0.1:7000"
