#!/usr/bin/env bash
# poc-install.sh — ON THE PROVER BOX: put the minimal-padding PoC build of ZisK beside the release,
# in ~/.zisk-poc, so that run.sh times it with ZISK_HOME=~/.zisk-poc.
#
#   bash poc-install.sh <dir>     <dir> holds what poc-pack.sh made on the Mac:
#                                 zisk-poc-src.tar.gz (the PoC tree) and provingKey.tar (its key)
#
# The PoC is ZisK 1.3.1-alpha with shorter instances (branch al/poc-min-padding: pil/zisk.pil,
# the traces.rs generated from it, setup/starkstructs.poseidon.json). Its proving key was set up on
# the Mac with ZisK's own pipeline; what depends on the box is finished here:
#
#   1. the tree is built with the GPU prover (CUDA_ARCHS=major, as ZisK's build_zisk.sh does);
#   2. binaries, emulator-asm and lib-c sources and the key go to ~/.zisk-poc;
#   3. the key's recursion witness libraries are rebuilt for Linux (the Mac built .dylib), and
#      its per-air GPU expression kernels are generated (ZisK's --gen-exps, arch major).
#
# The constant trees are left to run.sh: up.sh runs check-setup --gpu on whatever key ZISK_HOME
# names and writes the missing ones. The release in ~/.zisk is not touched.
#
# Env: POC_HOME=~/.zisk-poc · POC_TREE=~/zisk-poc (where the tree is unpacked and built)
#      PROOFMAN_SHA (the pil2-proofman commit the 1.3.1 crates were published from)
set -euo pipefail
SRC="$(cd "${1:?usage: poc-install.sh <dir with zisk-poc-src.tar.gz and provingKey.tar>}" && pwd)"
POC_HOME="${POC_HOME:-$HOME/.zisk-poc}"
POC_TREE="${POC_TREE:-$HOME/zisk-poc}"
PROOFMAN_SHA="${PROOFMAN_SHA:-8db05c95d94d40ddb08547afc6b96d36a541a869}"
PROOFMAN_DIR="$HOME/pil2-proofman-$PROOFMAN_SHA"
say() { printf '\n\033[1m== %s\033[0m  (%s)\n' "$*" "$(date -u +%H:%M:%S)"; }
die() { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "poc-install.sh runs on the prover box"
[ -f "$SRC/zisk-poc-src.tar.gz" ] && [ -f "$SRC/provingKey.tar" ] || die "$SRC lacks zisk-poc-src.tar.gz or provingKey.tar"
command -v nvcc >/dev/null 2>&1 || [ -x /usr/local/cuda/bin/nvcc ] || die "no nvcc: the GPU prover needs the CUDA toolkit"
command -v nvcc >/dev/null 2>&1 || export PATH="/usr/local/cuda/bin:$PATH"
export PATH="$HOME/.cargo/bin:$PATH"

say "build dependencies"
# What ZisK's book lists for a Linux build, minus what a prover box already has. Idempotent.
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang libclang-dev pkg-config \
    jq curl git libgmp-dev libsodium-dev libomp-dev nlohmann-json3-dev protobuf-compiler uuid-dev \
    libssl-dev libopenmpi-dev openmpi-bin nasm libgrpc++-dev libsecp256k1-dev libpqxx-dev >/dev/null
fi
command -v cargo >/dev/null 2>&1 || { curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null; }

say "unpacking the PoC tree into $POC_TREE"
rm -rf "$POC_TREE"; mkdir -p "$POC_TREE"
tar -xzf "$SRC/zisk-poc-src.tar.gz" -C "$POC_TREE"
grep -q 'Minimal-padding PoC' "$POC_TREE/pil/zisk.pil" || die "the tarball is not the PoC tree"

say "building it with the GPU prover (CUDA_ARCHS=major)"
( cd "$POC_TREE" && CUDA_ARCHS=major cargo build --release ) > "$HOME/poc-build.log" 2>&1 \
  || { tail -30 "$HOME/poc-build.log" >&2; die "the build failed — ~/poc-build.log"; }
R="$POC_TREE/target/release"
"$R/cargo-zisk" --version | grep -q '\[gpu\]' || die "cargo-zisk is not a [gpu] build: $("$R/cargo-zisk" --version)"

say "installing into $POC_HOME"
mkdir -p "$POC_HOME/bin" "$POC_HOME/zisk/emulator-asm" "$POC_HOME/cache"
for b in cargo-zisk cargo-zisk-dev ziskemu zisk-coordinator zisk-worker zisk-transpiler-riscv; do
  cp "$R/$b" "$POC_HOME/bin/"
done
cp "$R/libziskclib.a" "$POC_HOME/bin/" 2>/dev/null || true
rm -rf "$POC_HOME/zisk/emulator-asm/src" "$POC_HOME/zisk/lib-c"
cp -r "$POC_TREE/emulator-asm/src" "$POC_TREE/emulator-asm/Makefile" "$POC_HOME/zisk/emulator-asm/"
cp -r "$POC_TREE/lib-c" "$POC_HOME/zisk/"
rm -rf "$POC_HOME/provingKey"
tar -xf "$SRC/provingKey.tar" -C "$POC_HOME"
[ -d "$POC_HOME/provingKey/zisk" ] || die "provingKey.tar did not unpack to provingKey/zisk"

say "pil2-proofman $PROOFMAN_SHA (setup assets: circom helpers, goldilocks sources, pil2-stark)"
if [ ! -f "$PROOFMAN_DIR/.git/zisk_fetch_ok" ]; then
  rm -rf "$PROOFMAN_DIR"; git init -q "$PROOFMAN_DIR"
  git -C "$PROOFMAN_DIR" remote add origin https://github.com/0xPolygonHermez/pil2-proofman.git
  git -C "$PROOFMAN_DIR" fetch -q --depth 1 origin "$PROOFMAN_SHA"
  git -C "$PROOFMAN_DIR" checkout -q --detach "$PROOFMAN_SHA"
  touch "$PROOFMAN_DIR/.git/zisk_fetch_ok"
fi
export CIRCOM_HELPERS_DIR="$PROOFMAN_DIR/setup/circom"
export GOLDILOCKS_SRC_DIR="$PROOFMAN_DIR/pil2-stark/src/goldilocks/src"

say "recursion witness libraries for Linux"
"$POC_HOME/bin/cargo-zisk-dev" proofman-setup rebuild-witness-libs --proving-key "$POC_HOME/provingKey" \
  > "$HOME/poc-witness-libs.log" 2>&1 || { tail -20 "$HOME/poc-witness-libs.log" >&2; die "rebuild-witness-libs failed"; }

say "GPU expression kernels (gen-exps, arch major)"
"$POC_HOME/bin/cargo-zisk-dev" proofman-setup gen-exps --proving-key "$POC_HOME/provingKey" --arch major \
  --stark-src "$PROOFMAN_DIR/pil2-stark" > "$HOME/poc-gen-exps.log" 2>&1 \
  || { tail -20 "$HOME/poc-gen-exps.log" >&2; die "gen-exps failed"; }

say "done — $(du -sh "$POC_HOME/provingKey" | cut -f1) of key; time it with:"
echo "   ZISK_HOME=$POC_HOME FORCE_RESTART=1 bash $(cd "$(dirname "$0")" && pwd)/run.sh"
