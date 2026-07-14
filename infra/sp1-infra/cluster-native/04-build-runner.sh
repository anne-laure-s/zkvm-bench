#!/usr/bin/env bash
# 04 — build our sp1-runner as a NETWORK client (one-time; persists across stop/start).
# It talks to the cluster's network-gateway, so it RETRIEVES + SAVES the proof
# (proof.bin / pv.bin / report.json) — unlike `cli bench` which only times.
# Needs the sp1-runner crate at ~/sp1-runner (scp it from your Mac).
set -euo pipefail

[[ -d "$HOME/sp1-runner" ]] || {
  echo "ERROR: ~/sp1-runner not found. From your Mac:"
  echo "  scp -P <port> -r sp1-runner root@ssh.vast.ai:~/sp1-runner"
  exit 1
}
command -v cargo >/dev/null 2>&1 || {
  echo "== installing Rust =="
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
}
source "$HOME/.cargo/env" 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends build-essential pkg-config libssl-dev protobuf-compiler 2>/dev/null || true

echo "== building sp1-runner (network client; no CUDA, no Go/gnark) =="
# --no-default-features drops native-gnark (the Go/gnark FFI) — not needed for a
# network client and it would otherwise require Go on the box.
cd "$HOME/sp1-runner" && cargo build --release --no-default-features --features network

# Isolate the binary, THEN reclaim disk. On the 32 GB overlay the CUDA rootfs (02)
# + the cluster's proving artifacts need the space; target/ (~3.4 GB) and the cargo
# caches (~3-5 GB) would otherwise starve proving. submit.sh prefers ~/sp1-runner-bin,
# so we keep only that (~62 MB). Re-running 04 re-fetches deps as needed.
cp -f "$HOME/sp1-runner/target/release/sp1-runner" "$HOME/sp1-runner-bin"
[[ -x "$HOME/sp1-runner-bin" ]] || { echo "ERROR: failed to isolate binary to ~/sp1-runner-bin" >&2; exit 1; }
echo "== isolated binary -> ~/sp1-runner-bin ($(du -h "$HOME/sp1-runner-bin" | cut -f1)); reclaiming build scratch =="
rm -rf "$HOME/sp1-runner/target" \
       "$HOME/.cargo/registry/cache" "$HOME/.cargo/registry/src" "$HOME/.cargo/git"
echo "== done. runner = ~/sp1-runner-bin =="
df -h / | tail -1
