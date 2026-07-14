#!/usr/bin/env bash
# 00 — ONE-TIME install. Persists across stop/start; only re-run on a fresh or
# destroyed instance. (Starting services is in boot.sh — run that every boot.)
set -uo pipefail
echo "== installing deps (one-time) =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  redis-server postgresql skopeo jq ca-certificates python3 curl zstd \
  build-essential pkg-config libssl-dev protobuf-compiler libprotobuf-dev \
  numactl
mkdir -p "$HOME/.sp1/circuits"
echo "== done. next: ./02-fetch-binaries.sh (also one-time), then ./boot.sh =="
