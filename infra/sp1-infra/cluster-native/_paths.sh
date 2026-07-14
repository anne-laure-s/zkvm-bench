# _paths.sh — resolve the real dynamic loader + library paths for each rootfs.
# Sourced by 03-start.sh and submit.sh. We run the extracted binaries via the
# IMAGE's loader explicitly:  <ld> --library-path <libs> <binary>
# IMPORTANT: use the REAL loader file (not the /lib64/ld-* symlink, which is an
# absolute symlink that would escape the rootfs and resolve to the HOST loader).
PATHS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LD_BASE="$(find "$PATHS_ROOT/rootfs-base" -name 'ld-linux-x86-64.so.2' -type f 2>/dev/null | head -1)" || true
LD_GPU="$(find "$PATHS_ROOT/rootfs-gpu" -name 'ld-linux-x86-64.so.2' -type f 2>/dev/null | head -1)" || true

# Only real lib dirs (avoid symlinked dirs that escape the rootfs). Append any
# extra CUDA dir actually present in the GPU rootfs, plus the HOST driver libs
# (libcuda.so.1 comes from the host driver, not the image).
LIBS_BASE="$PATHS_ROOT/rootfs-base/usr/lib/x86_64-linux-gnu:$PATHS_ROOT/rootfs-base/usr/lib"
_cuda_dir="$(find "$PATHS_ROOT/rootfs-gpu" -name 'libcudart.so*' -printf '%h\n' 2>/dev/null | sort -u | head -1)" || true
# REQUIREMENT: the GPU binary (sp1-cluster v2.4.3) links the CUDA 13.0 runtime, so the HOST
# driver must natively support CUDA >= 13.0 (driver >= 580). We use the host libcuda below.
# Do NOT prepend the image's cuda-13.0/compat dir: CUDA forward-compatibility is rejected on
# consumer GeForce ("forward compatibility was attempted on non supported HW"), and loading it
# breaks even a correctly-driven box. On a too-old driver (e.g. CUDA 12.9 / 575) there is no
# workaround — rent a Vast box whose CUDA Version is >= 13.0.
LIBS_GPU="$PATHS_ROOT/rootfs-gpu/usr/lib/x86_64-linux-gnu:$PATHS_ROOT/rootfs-gpu/usr/lib${_cuda_dir:+:$_cuda_dir}:/usr/lib/x86_64-linux-gnu:/usr/lib64"

paths_check() {
  local ok=1
  [[ -f "${LD_BASE:-}" ]] || { echo "ERROR: base loader not found under rootfs-base (run 02 first?)" >&2; ok=0; }
  [[ -f "${LD_GPU:-}"  ]] || { echo "ERROR: gpu loader not found under rootfs-gpu" >&2; ok=0; }
  [[ "$ok" == 1 ]]
}

# require_cuda13 — the v2.4.3 node-gpu binary links the CUDA 13.0 runtime and forward-compat is
# rejected (see the LIBS_GPU note above), so the HOST driver must natively support CUDA >= 13.0
# (driver >= 580). Fail fast here instead of with a cryptic "missing CUDA/driver libs" in logs/gpu0.log.
# `nvidia-smi`'s header reports the driver's max CUDA Version (e.g. "CUDA Version: 13.2").
require_cuda13() {
  local v major
  v="$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)" || true
  if [[ -z "$v" ]]; then
    echo "WARN: could not read 'CUDA Version' from nvidia-smi — verify the box has an NVIDIA GPU + driver." >&2
    return 0
  fi
  major="${v%%.*}"
  if (( major < 13 )); then
    echo "ERROR: driver CUDA Version $v < 13.0. The v2.4.3 GPU binaries link the CUDA 13.0 runtime and" >&2
    echo "       forward-compatibility is rejected — there is NO workaround on this driver." >&2
    echo "       Rent a box whose 'nvidia-smi' CUDA Version is >= 13.0 (driver >= 580)." >&2
    return 1
  fi
  echo "driver CUDA Version $v (>= 13.0 required) — OK"
}
