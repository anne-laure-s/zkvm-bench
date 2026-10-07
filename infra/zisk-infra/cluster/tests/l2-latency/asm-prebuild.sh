#!/usr/bin/env bash
# asm-prebuild.sh — ON THE BOX: the worker's one-time ROM setup of every ELF the run will prove, done
# ahead of the first proof and several ELFs at a time. run.sh calls it once the cluster is up.
#
#   ZISK_HOME=<install> ONLY=<regex on input ids> bash asm-prebuild.sh <inputs dir> <log dir>
#
# The first `remote setup` of an ELF makes the worker compute the ROM's Merkle root and build three
# x86 emulators of the ELF (minimal traces, ROM histogram, memory ops) into $ZISK_HOME/cache, one
# ELF at a time, `as` single-threaded: on these ELFs (2.4 M ROM instructions) ~4 min each and
# ~12 GB of RAM at the peak, so ~25 min for six while the box waits. `cargo-zisk-dev program-setup`
# runs that very setup (rom_merkle_setup and generate_assembly, the worker's own calls); this runs
# it for every ELF still missing, as many at a time as the free RAM allows, and the worker then
# finds the emulators in the cache and skips its own build.
#
# Each ELF builds in a directory of its own -- generate_assembly runs `make clean` in the
# emulator-asm sources it uses, so two builds cannot share them -- from a copy of the install's
# sources (ZISK_USE_INSTALLED) into a cache of its own, whose files move to the install's cache only
# once all three emulators are there: a build that fails or is cut leaves nothing the worker would
# take for a finished one. Nothing here is required: an ELF it does not build, the worker builds at
# its first setup as before. It never fails the run.
set -uo pipefail
IN="$(cd "${1:?usage: asm-prebuild.sh <inputs dir> <log dir>}" && pwd)"
LOGD="${2:?usage: asm-prebuild.sh <inputs dir> <log dir>}"
ZH="${ZISK_HOME:-$HOME/.zisk}"
CACHE="${ZISK_CACHE_DIR:-$ZH/cache}"
DEV="$ZH/bin/cargo-zisk-dev"
mkdir -p "$LOGD" "$CACHE"

for f in "$DEV" "$ZH/bin/libziskc.a" "$ZH/bin/libziskclib.a" "$ZH/zisk/emulator-asm/Makefile" "$ZH/provingKey"; do
  [ -e "$f" ] || { echo "   no $f: the worker builds every ELF at its first setup"; exit 0; }
done

# The ELFs of the inputs this run proves, in the order bench-l2.sh meets them.
mapfile -t ELFS < <(python3 - "$IN/inputs.csv" "${ONLY:-.}" <<'EOF'
import csv, re, sys
seen = []
for r in csv.DictReader(open(sys.argv[1])):
    if re.search(sys.argv[2], r['id']) and r['elf'] not in seen:
        seen.append(r['elf'])
print('\n'.join(seen))
EOF
)
# A mark per ELF content, written once its files are in the cache: a second run on the box skips it.
mark() { echo "$CACHE/.prebuilt-$(sha256sum "$IN/$1" | cut -c1-64)"; }
TODO=()
for e in "${ELFS[@]}"; do [ -f "$(mark "$e")" ] || TODO+=("$e"); done
[ "${#TODO[@]}" -gt 0 ] || { echo "   all ${#ELFS[@]} ELF(s) already set up"; exit 0; }

# ~12 GB a build at its peak (`as` on the minimal-trace emulator), 8 GB left to everything else.
MEM_GB="$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)"
P=$(( (MEM_GB - 8) / 12 )); [ "$P" -ge 1 ] || P=1; [ "$P" -le "${#TODO[@]}" ] || P="${#TODO[@]}"
echo "   ${#TODO[@]} ELF(s) to set up, $P at a time (${MEM_GB} GB of RAM available, ~12 GB a build)"

W="$ZH/.asm-prebuild"
rm -rf "$W"; mkdir -p "$W"
one() { # one <elf>
  local e="$1" j="$W/${1%.elf}" t0 n
  mkdir -p "$j/home/bin" "$j/home/zisk" "$j/cache"
  ln -s "$ZH/bin/libziskc.a" "$ZH/bin/libziskclib.a" "$j/home/bin/"
  cp -r "$ZH/zisk/emulator-asm" "$j/home/zisk/"
  [ -d "$ZH/zisk/lib-c" ] && ln -s "$ZH/zisk/lib-c" "$j/home/zisk/lib-c"
  t0=$(date +%s)
  ZISK_HOME="$j/home" ZISK_CACHE_DIR="$j/cache" ZISK_USE_INSTALLED=1 \
    "$DEV" program-setup -e "$IN/$e" -k "$ZH/provingKey" > "$LOGD/${e%.elf}.log" 2>&1
  n="$(ls "$j/cache"/*-mt.bin "$j/cache"/*-rh.bin "$j/cache"/*-mo.bin 2>/dev/null | wc -l)"
  if [ "$n" = 3 ]; then
    # the emulators and the ROM's Merkle files; not the 2.3 GB of .asm sources they were built from
    mv "$j/cache"/*.bin "$CACHE/" && : > "$(mark "$e")" \
      && echo "   $e: set up in $(( $(date +%s) - t0 )) s"
  else
    echo "   $e: not set up ($n of 3 emulators; ${LOGD##*/}/${e%.elf}.log) -- the worker builds it at its first setup"
  fi
  rm -rf "$j"
}
for e in "${TODO[@]}"; do
  while [ "$(jobs -rp | wc -l)" -ge "$P" ]; do sleep 2; done
  one "$e" &
done
wait
rm -rf "$W"
exit 0
