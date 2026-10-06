#!/usr/bin/env bash
# poc-install.sh — ON THE PROVER BOX: put the minimal-padding PoC build of ZisK beside the release,
# in ~/.zisk-poc, so that run.sh times it with ZISK_HOME=~/.zisk-poc.
#
#   bash poc-install.sh <dir>     <dir> holds what poc-pack.sh made on the Mac:
#                                 zisk-poc-src.tar.gz, the PoC tree, and optionally
#                                 provingKey.tar, its key as set up and tested on the Mac
#
# The PoC is ZisK 1.3.1-alpha with shorter instances (branch al/poc-min-padding: pil/zisk.pil,
# the traces.rs generated from it, setup/starkstructs.poseidon.json). This script:
#
#   1. builds the tree with the GPU prover (CUDA_ARCHS=major, as ZisK's build_zisk.sh does);
#   2. makes the key: with provingKey.tar, unpacks it and redoes what depends on the box (the
#      recursion witness libraries for Linux, the per-air GPU expression kernels); without it,
#      runs ZisK's own setup pipeline here (tools/test-env/setup_build.sh, Poseidon1 like the
#      release key, --gen-exps), which needs no upload but takes the box about half an hour;
#   3. installs binaries, emulator-asm and lib-c sources and the key in ~/.zisk-poc, with the
#      memlock patch 00-install-once.sh applies to the release (see step 3 below).
#
# The constant trees are left to run.sh: up.sh runs check-setup --gpu on whatever key ZISK_HOME
# names and writes the missing ones. The release in ~/.zisk is not touched.
#
# Env: POC_HOME=~/.zisk-poc · POC_TREE=~/zisk-poc (where the tree is unpacked and built)
#      SETUP_JOBS / RECURSIVE_JOBS (key built here; default 8 each)
set -euo pipefail
SRC="$(cd "${1:?usage: poc-install.sh <dir with zisk-poc-src.tar.gz [and provingKey.tar]>}" && pwd)"
POC_HOME="${POC_HOME:-$HOME/.zisk-poc}"
POC_TREE="${POC_TREE:-$HOME/zisk-poc}"
NODE_VER=v24.21.0
# Logs named after the install, so that two keys set up at once do not write the same files.
LOGS="$HOME/$(basename "$POC_HOME" | sed 's/^\.//')"
# Where ZisK's setup scripts fetch the pil2-proofman checkout the 1.3.1 crates were published from
# (its setup assets); their default is under ~/.zisk, which belongs to the release. One per
# install, for the same reason as the logs.
export ZISK_PROOFMAN_CACHE_DIR="${ZISK_PROOFMAN_CACHE_DIR:-$LOGS-proofman}"
# What every install on the box shares (packages, rustup, Node) is installed under this lock, so
# that two installs started together (poc-run.sh with several keys) take turns there.
LOCK="$HOME/.poc-install.lock"
locked() { if command -v flock >/dev/null 2>&1; then flock "$LOCK" "$@"; else "$@"; fi; }
say() { printf '\n\033[1m== %s\033[0m  (%s)\n' "$*" "$(date -u +%H:%M:%S)"; }
die() { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "poc-install.sh runs on the prover box"
[ -f "$SRC/zisk-poc-src.tar.gz" ] || die "$SRC lacks zisk-poc-src.tar.gz"
command -v nvcc >/dev/null 2>&1 || export PATH="/usr/local/cuda/bin:$PATH"
command -v nvcc >/dev/null 2>&1 || die "no nvcc: the GPU prover and its expression kernels need the CUDA toolkit"
export PATH="$HOME/.cargo/bin:$PATH"

say "build dependencies"
# What ZisK's book lists for a Linux build. Idempotent.
deps() {
  if command -v apt-get >/dev/null 2>&1; then
    # The lock timeout lets this wait for an install up.sh may be running at the same time.
    apt-get -o DPkg::Lock::Timeout=1800 update -qq
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=1800 install -y -qq build-essential clang libclang-dev pkg-config \
      jq curl git xz-utils libgmp-dev libsodium-dev libomp-dev nlohmann-json3-dev protobuf-compiler \
      libprotobuf-dev uuid-dev libssl-dev libopenmpi-dev openmpi-bin nasm libgrpc++-dev libsecp256k1-dev \
      libpqxx-dev zstd numactl >/dev/null
  fi
  command -v cargo >/dev/null 2>&1 || { curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null; }
}
locked bash -c "$(declare -f deps); set -e; deps"

say "unpacking the PoC tree into $POC_TREE"
rm -rf "$POC_TREE"; mkdir -p "$POC_TREE"
tar -xzf "$SRC/zisk-poc-src.tar.gz" -C "$POC_TREE"
grep -q 'Minimal-padding PoC' "$POC_TREE/pil/zisk.pil" || die "the tarball is not the PoC tree"

say "building it with the GPU prover (CUDA_ARCHS=major) — ~/${LOGS##*/}-build.log"
( cd "$POC_TREE" && CUDA_ARCHS=major cargo build --release ) > "$LOGS-build.log" 2>&1 \
  || { tail -30 "$LOGS-build.log" >&2; die "the build failed — ~/${LOGS##*/}-build.log"; }
R="$POC_TREE/target/release"
"$R/cargo-zisk" --version | grep -q '\[gpu\]' || die "cargo-zisk is not a [gpu] build: $("$R/cargo-zisk" --version)"

if [ -f "$SRC/provingKey.tar" ]; then
  say "the key from the Mac: unpacking it"
  rm -rf "$POC_TREE/build/provingKey"; mkdir -p "$POC_TREE/build"
  tar -xf "$SRC/provingKey.tar" -C "$POC_TREE/build"
  [ -d "$POC_TREE/build/provingKey/zisk" ] || die "provingKey.tar did not unpack to provingKey/zisk"
  # setup_common.sh resolves the pil2-proofman checkout the crates were published from and
  # exports the paths of its setup assets (circom helpers, goldilocks sources); sourced from the
  # tree root, as setup_build.sh does.
  ( cd "$POC_TREE" && export ZISK_REPO_DIR="$POC_TREE" ROOT_DIR="$POC_TREE" \
      && . tools/test-env/setup_common.sh \
      && "$R/cargo-zisk-dev" proofman-setup rebuild-witness-libs --proving-key build/provingKey \
      && "$R/cargo-zisk-dev" proofman-setup gen-exps --proving-key build/provingKey --arch major \
           --stark-src "$PROOFMAN_DIR/pil2-stark" ) > "$LOGS-key.log" 2>&1 \
    || { tail -30 "$LOGS-key.log" >&2; die "finishing the key failed — ~/${LOGS##*/}-key.log"; }
else
  say "no key in $SRC: setting it up here with ZisK's pipeline — ~/${LOGS##*/}-key.log"
  export PATH="$HOME/node-$NODE_VER-linux-x64/bin:$PATH"
  command -v node >/dev/null 2>&1 || locked bash -c "set -o pipefail; [ -x '$HOME/node-$NODE_VER-linux-x64/bin/node' ] \
    || curl -fsSL 'https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz' | tar -xJ -C '$HOME'"
  command -v node >/dev/null 2>&1 || die "Node $NODE_VER did not install"
  SS="$POC_TREE/setup/starkstructs.poseidon.json"; cp "$SS" "$SS.shipped"
  # CUDA_ARCHS as for the build above: the pipeline cargo-runs the tree's binaries, and another
  # value would rebuild the prover's CUDA library first.
  ( cd "$POC_TREE" && CUDA_ARCHS=major ZISK_REPO_DIR="$POC_TREE" HASH_MODE=Poseidon1 \
      SETUP_JOBS="${SETUP_JOBS:-8}" RECURSIVE_JOBS="${RECURSIVE_JOBS:-8}" \
      ./tools/test-env/setup_build.sh --build-dir build --gen-exps --exps-arch major ) \
    > "$LOGS-key.log" 2>&1 || { tail -30 "$LOGS-key.log" >&2; die "the setup failed — ~/${LOGS##*/}-key.log"; }
  # The setup writes back every compressor it had to add. The shipped file already holds the ones
  # the Mac's setup chose, so a change means this key is not the one tested there.
  cmp -s "$SS" "$SS.shipped" || { diff "$SS.shipped" "$SS" >&2; die "this setup chose other compressors than the Mac's"; }
fi

say "installing into $POC_HOME"
# What ZisK's tools/test-env/build_zisk.sh installs on Linux. The two static libraries are not
# optional: the worker builds every ELF's ASM emulator with emulator-asm/Makefile, run in
# $POC_HOME/zisk/emulator-asm, which links -lziskc -lziskclib from ../../bin, $POC_HOME/bin.
rm -f "$POC_HOME/.installed"
mkdir -p "$POC_HOME/bin" "$POC_HOME/zisk/emulator-asm"
for b in cargo-zisk cargo-zisk-dev ziskemu zisk-coordinator zisk-worker zisk-transpiler-riscv libziskclib.a; do
  cp "$R/$b" "$POC_HOME/bin/"
done
cp "$POC_TREE/target/zisk-libs/libziskc.a" "$POC_HOME/bin/"
# A cache left by an earlier install holds ASM emulators built from its sources and setups made
# with its key: the first remote setup of each ELF rebuilds them.
rm -rf "$POC_HOME/zisk/emulator-asm/src" "$POC_HOME/zisk/lib-c" "$POC_HOME/provingKey" "$POC_HOME/cache"
mkdir -p "$POC_HOME/cache"
cp -r "$POC_TREE/emulator-asm/src" "$POC_TREE/emulator-asm/Makefile" "$POC_HOME/zisk/emulator-asm/"
cp -r "$POC_TREE/lib-c" "$POC_HOME/zisk/"
mv "$POC_TREE/build/provingKey" "$POC_HOME/provingKey"

# The memlock patch 00-install-once.sh applies to the release (fix-memlock-patch.sh says why): on an
# unprivileged container (vast.ai) memlock is capped at 64 KB, and the ASM microservices the worker
# builds from these sources mmap with MAP_LOCKED by default and die with "mmap(rom) errno=11". A box
# that never installed the release has neither that patch nor the nolock.so shim start.sh preloads.
GLB="$POC_HOME/zisk/emulator-asm/src/globals.c"
if grep -q '^int map_locked_flag = MAP_LOCKED;' "$GLB"; then
  cp "$GLB" "$GLB.orig"
  sed -i 's|^int map_locked_flag = MAP_LOCKED;|int map_locked_flag = 0; /* PATCH: unprivileged-Docker memlock cap, unlock by default */|' "$GLB"
fi
grep -q '^int map_locked_flag = 0;' "$GLB" || die "the memlock patch did not apply to $GLB"
NOLOCK_C="$(cd "$(dirname "$0")/../.." && pwd)/nolock.c"
if [ ! -f "$HOME/nolock.so" ] && [ -f "$NOLOCK_C" ]; then
  gcc -shared -fPIC -O2 -o "$HOME/nolock.so" "$NOLOCK_C" -ldl || echo "   WARN: nolock.so did not build"
fi

# The mark poc-run.sh reads: the tree this install was made from, written last, so that an install
# that stopped part way, or one of another tree, reads as missing.
sha256sum "$SRC/zisk-poc-src.tar.gz" > "$POC_HOME/.installed"

say "done — $(du -sh "$POC_HOME/provingKey" | cut -f1) of key; time it with:"
echo "   ZISK_HOME=$POC_HOME FORCE_RESTART=1 bash $(cd "$(dirname "$0")" && pwd)/run.sh"
