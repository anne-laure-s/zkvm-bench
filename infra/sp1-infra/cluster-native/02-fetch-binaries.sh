#!/usr/bin/env bash
# 02 — pull the v2.4.3 cluster images WITHOUT Docker (skopeo) and flatten them to
# a rootfs each. We then run the binaries from those rootfs (03-start.sh).
#   base image    -> ./rootfs-base   (/api /coordinator /node /network-gateway)
#   node-gpu image-> ./rootfs-gpu    (/app/sp1-cluster-node + CUDA libs)
set -euo pipefail
cd "$(dirname "$0")"

TAG="${CLUSTER_TAG:-v2.4.3}"
BASE="ghcr.io/succinctlabs/sp1-cluster:base-${TAG}"
GPU="ghcr.io/succinctlabs/sp1-cluster:node-gpu-${TAG}"

# Preflight: the node-gpu image links the CUDA 13.0 runtime (no forward-compat), so bail out before
# the big pull if the host driver is too old. Shared check + rationale live in _paths.sh.
source ./_paths.sh
require_cuda13 || exit 1

# Extraction can need tens of GB (node-gpu carries CUDA). Heads-up on free space.
echo "== free disk here (extraction needs ~20-30 GB; node-gpu is big) =="
df -h .

flatten() {  # <image-ref> <out-rootfs>
  local ref="$1" out="$2" tmp="_dl_$2"
  echo "== pulling $ref =="
  rm -rf "$tmp" "$out"; mkdir -p "$tmp" "$out"
  skopeo copy --override-os linux --override-arch amd64 "docker://$ref" "dir:$tmp"
  echo "== flattening layers -> $out =="
  # untar each layer in manifest order (later layers overwrite earlier)
  python3 - "$tmp" "$out" <<'PY'
import json, os, shutil, subprocess, sys
tmp, out = sys.argv[1], sys.argv[2]
man = json.load(open(os.path.join(tmp, "manifest.json")))
for layer in man["layers"]:
    blob = layer["digest"].split(":")[1]
    path = os.path.join(tmp, blob)
    free_gb = shutil.disk_usage(out).free / 1e9
    print(f"  layer {blob[:12]}  (free: {free_gb:.1f} GB)")
    # tar auto-detects gzip/zstd; ignore whiteout/permission noise but FAIL on
    # real extraction errors (e.g. ENOSPC) instead of silently half-extracting.
    r = subprocess.run(["tar", "-xf", path, "-C", out,
                        "--no-same-owner", "--exclude=dev/*"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        # whiteouts / chown noise are harmless; a disk-full or truncated layer is not
        fatal = any(k in err.lower() for k in
                    ("no space", "cannot write", "unexpected eof", "write error",
                     "archive", "not in gzip", "decompress"))
        if fatal:
            sys.exit(f"FATAL extracting layer {blob[:12]} into {out}:\n{err}")
        else:
            print("    (non-fatal tar warnings ignored)")
    # free the compressed blob immediately to keep peak disk usage low
    os.remove(path)
PY
  rm -rf "$tmp"
}

flatten "$BASE" rootfs-base
flatten "$GPU"  rootfs-gpu

echo "== sanity: binaries + loaders present? =="
miss=0
for b in rootfs-base/api rootfs-base/coordinator rootfs-base/node rootfs-base/network-gateway \
         rootfs-gpu/app/sp1-cluster-node; do
  [[ -f "$b" ]] && echo "  OK  $b" || { echo "  MISSING $b"; miss=1; }
done
for d in rootfs-base rootfs-gpu; do
  if find "$d" -name 'ld-linux-x86-64.so.2' -type f 2>/dev/null | grep -q .; then
    echo "  OK  loader in $d"
  else
    echo "  MISSING loader in $d"; miss=1
  fi
done
if [[ "$miss" != 0 ]]; then
  echo "ERROR: extraction incomplete (likely disk space — see 'free' above, or df -h)." >&2
  echo "       Fix the cause, then re-run ./02-fetch-binaries.sh. Nothing else to redo." >&2
  exit 1
fi

echo "== done (ONE-TIME, persists across stop/start of THIS instance)."
echo "   next every boot: ./boot.sh then NUM_GPUS=1 ./03-start.sh =="
