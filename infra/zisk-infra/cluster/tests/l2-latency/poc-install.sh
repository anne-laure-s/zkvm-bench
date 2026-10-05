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
#   3. installs binaries, emulator-asm and lib-c sources and the key in ~/.zisk-poc.
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
# Where ZisK's setup scripts fetch the pil2-proofman checkout the 1.3.1 crates were published from
# (its setup assets); their default is under ~/.zisk, which belongs to the release.
export ZISK_PROOFMAN_CACHE_DIR="${ZISK_PROOFMAN_CACHE_DIR:-$HOME/pil2-proofman-cache}"
say() { printf '\n\033[1m== %s\033[0m  (%s)\n' "$*" "$(date -u +%H:%M:%S)"; }
die() { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "poc-install.sh runs on the prover box"
[ -f "$SRC/zisk-poc-src.tar.gz" ] || die "$SRC lacks zisk-poc-src.tar.gz"
command -v nvcc >/dev/null 2>&1 || export PATH="/usr/local/cuda/bin:$PATH"
command -v nvcc >/dev/null 2>&1 || die "no nvcc: the GPU prover and its expression kernels need the CUDA toolkit"
export PATH="$HOME/.cargo/bin:$PATH"

say "build dependencies"
# What ZisK's book lists for a Linux build. Idempotent.
if command -v apt-get >/dev/null 2>&1; then
  # The lock timeout lets this wait for an install up.sh may be running at the same time.
  apt-get -o DPkg::Lock::Timeout=1800 update -qq
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=1800 install -y -qq build-essential clang libclang-dev pkg-config \
    jq curl git xz-utils libgmp-dev libsodium-dev libomp-dev nlohmann-json3-dev protobuf-compiler \
    uuid-dev libssl-dev libopenmpi-dev openmpi-bin nasm libgrpc++-dev libsecp256k1-dev libpqxx-dev >/dev/null
fi
command -v cargo >/dev/null 2>&1 || { curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null; }

say "unpacking the PoC tree into $POC_TREE"
rm -rf "$POC_TREE"; mkdir -p "$POC_TREE"
tar -xzf "$SRC/zisk-poc-src.tar.gz" -C "$POC_TREE"
grep -q 'Minimal-padding PoC' "$POC_TREE/pil/zisk.pil" || die "the tarball is not the PoC tree"

say "building it with the GPU prover (CUDA_ARCHS=major) — ~/poc-build.log"
( cd "$POC_TREE" && CUDA_ARCHS=major cargo build --release ) > "$HOME/poc-build.log" 2>&1 \
  || { tail -30 "$HOME/poc-build.log" >&2; die "the build failed — ~/poc-build.log"; }
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
           --stark-src "$PROOFMAN_DIR/pil2-stark" ) > "$HOME/poc-key.log" 2>&1 \
    || { tail -30 "$HOME/poc-key.log" >&2; die "finishing the key failed — ~/poc-key.log"; }
else
  say "no key in $SRC: setting it up here with ZisK's pipeline — ~/poc-key.log"
  if ! command -v node >/dev/null 2>&1; then
    curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" | tar -xJ -C "$HOME"
    export PATH="$HOME/node-$NODE_VER-linux-x64/bin:$PATH"
  fi
  SS="$POC_TREE/setup/starkstructs.poseidon.json"; cp "$SS" "$SS.shipped"
  # CUDA_ARCHS as for the build above: the pipeline cargo-runs the tree's binaries, and another
  # value would rebuild the prover's CUDA library first.
  ( cd "$POC_TREE" && CUDA_ARCHS=major ZISK_REPO_DIR="$POC_TREE" HASH_MODE=Poseidon1 \
      SETUP_JOBS="${SETUP_JOBS:-8}" RECURSIVE_JOBS="${RECURSIVE_JOBS:-8}" \
      ./tools/test-env/setup_build.sh --build-dir build --gen-exps --exps-arch major ) \
    > "$HOME/poc-key.log" 2>&1 || { tail -30 "$HOME/poc-key.log" >&2; die "the setup failed — ~/poc-key.log"; }
  # The setup writes back every compressor it had to add. The shipped file already holds the ones
  # the Mac's setup chose, so a change means this key is not the one tested there.
  cmp -s "$SS" "$SS.shipped" || { diff "$SS.shipped" "$SS" >&2; die "this setup chose other compressors than the Mac's"; }
fi

say "installing into $POC_HOME"
mkdir -p "$POC_HOME/bin" "$POC_HOME/zisk/emulator-asm" "$POC_HOME/cache"
for b in cargo-zisk cargo-zisk-dev ziskemu zisk-coordinator zisk-worker zisk-transpiler-riscv; do
  cp "$R/$b" "$POC_HOME/bin/"
done
cp "$R/libziskclib.a" "$POC_HOME/bin/" 2>/dev/null || true
rm -rf "$POC_HOME/zisk/emulator-asm/src" "$POC_HOME/zisk/lib-c" "$POC_HOME/provingKey"
cp -r "$POC_TREE/emulator-asm/src" "$POC_TREE/emulator-asm/Makefile" "$POC_HOME/zisk/emulator-asm/"
cp -r "$POC_TREE/lib-c" "$POC_HOME/zisk/"
mv "$POC_TREE/build/provingKey" "$POC_HOME/provingKey"

say "done — $(du -sh "$POC_HOME/provingKey" | cut -f1) of key; time it with:"
echo "   ZISK_HOME=$POC_HOME FORCE_RESTART=1 bash $(cd "$(dirname "$0")" && pwd)/run.sh"
