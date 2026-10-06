#!/usr/bin/env bash
# t3-topo.sh — the box's own topology, which we have never actually written down. READ-ONLY: it
# starts nothing, proves nothing, touches no cluster state, and is safe to run at any time —
# including while a proof is in flight (the bandwidth probe is the one exception, see BW below).
#
# What it answers: 16 GPUs on a 2-socket board almost never means 16 × PCIe 5.0 x16. If the links are
# x8, or if groups sit behind PLX switches sharing an uplink, then the 69% of proving time spent in
# GENERATING_INNER_PROOFS is partly host↔device transfer we cannot tune away — and that changes the
# conclusion of every other test.
#
#   bash tests/t3-topo.sh
#
# Env: BW=1 (default; set BW=0 to skip the H2D/D2H bandwidth probe) · BW_MB=512
#
# ⚠️ BW=1 briefly allocates ~1 GB per GPU. Harmless on an idle box; it will contend with a running
# proof, so either run it while the cluster is idle or pass BW=0.
set -uo pipefail
source "$(dirname "$0")/lib.sh"

OUT="${OUT:-$HOME/tests/t3-topo-$(date -u +%Y%m%d-%H%M%SZ)}"
mkdir -p "$OUT"
R="$OUT/report.txt"
echo "== t3-topo → $OUT =="
command -v nvidia-smi >/dev/null 2>&1 || { echo "ERROR: nvidia-smi not found" >&2; exit 1; }

{
  echo "########## t3-topo — $(date -u) ##########"
  echo
  echo "########## 1. GPUs ##########"
  nvidia-smi -L 2>/dev/null
  echo
  nvidia-smi --query-gpu=driver_version,name,memory.total --format=csv 2>/dev/null | head -2
  echo
  echo "########## 2. PCIe links — the number that matters ##########"
  echo "# gen/width CURRENT can idle below MAX; what counts is that MAX is x16 gen5."
  echo "# A width MAX of 8 (or 4) is a hard ceiling on host↔device bandwidth for every proof."
  nvidia-smi --query-gpu=index,pci.bus_id,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current \
    --format=csv 2>/dev/null | tee "$OUT/pcie.csv"
  echo
  echo "########## 3. GPU → NUMA (from sysfs, not from ZisK's log) ##########"
  echo "# cuda_index numa_node pci_bdf   — numa_node = -1 means the kernel exposes no affinity"
  gpu_numa_map
  echo
  echo "# GPU count per NUMA node:"
  gpu_numa_map | awk '{c[$2]++} END{for(k in c) printf "  node %s: %s GPU(s)\n", k, c[k]}'
  echo
  echo "########## 4. nvidia-smi topo — switches and inter-GPU paths ##########"
  echo "# PIX/PXB = behind one or more PCIe switches (shared uplink → oversubscribed)."
  echo "# SYS     = the path crosses the inter-socket link. NODE = same NUMA node, no switch."
  nvidia-smi topo -m 2>/dev/null || echo "(nvidia-smi topo unavailable)"
  echo
  echo "########## 5. CPU / NUMA ##########"
  lscpu 2>/dev/null | grep -iE 'model name|^cpu\(s\)|thread|core\(s\) per socket|socket|numa|mhz|cache' || true
  echo
  numactl --hardware 2>/dev/null || echo "(numactl not installed — apt install numactl)"
  echo
  echo "########## 6. Container limits — why the MPI path segfaults and memlock is stripped ##########"
  printf 'ulimit -l (memlock, KB) : %s\n' "$(ulimit -l)"
  printf 'ulimit -l unlimited     : '; ( ulimit -l unlimited 2>/dev/null && echo "ALLOWED (privileged enough for the locked arm of t4)" ) || echo "DENIED (CAP_SYS_RESOURCE absent — t4's locked arm cannot run here)"
  # CAP_SYS_RESOURCE = capability 24 → bit 24 of the effective set.
  ce="$(awk '/^CapEff/{print $2}' /proc/self/status 2>/dev/null)"
  printf 'CapEff                  : %s\n' "${ce:-?}"
  if [[ -n "$ce" ]]; then
    for c in 24:CAP_SYS_RESOURCE 23:CAP_SYS_NICE 21:CAP_SYS_ADMIN; do
      bit="${c%%:*}"; nm="${c#*:}"
      have="$(python3 - "$ce" "$bit" <<'PY' 2>/dev/null || echo '?'
import sys
print("yes" if (int(sys.argv[1],16) >> int(sys.argv[2])) & 1 else "no")
PY
)"
      printf '  %-18s : %s\n' "$nm" "$have"
    done
  fi
  printf 'THP enabled             : %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo '?')"
  printf 'nr_hugepages            : %s\n' "$(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo '?')"
  printf '/dev/shm                : %s\n' "$(df -h /dev/shm 2>/dev/null | awk 'NR==2{print $2" total, "$4" free"}')"
  printf 'RAM                     : %s\n' "$(free -g 2>/dev/null | awk 'NR==2{print $2" GB total, "$7" GB available"}')"
  printf 'disk /                  : %s\n' "$(df -h / | awk 'NR==2{print $3" used, "$4" free ("$5")"}')"
  printf 'container               : %s\n' "$( [[ -f /.dockerenv ]] && echo 'docker (/.dockerenv present)' || echo 'no /.dockerenv' )"
  echo
} > "$R" 2>&1

# ── 7. actual H2D/D2H bandwidth ────────────────────────────────────────────────────────────────
# The link width above is the ceiling; this is what we really get, per GPU. A group of GPUs behind a
# shared switch uplink shows up as fine individually and collapses when driven together — so we
# measure both: each GPU alone, then all of them at once.
if [[ "${BW:-1}" == 1 ]]; then
  BW_MB="${BW_MB:-512}"
  BWBIN=""
  for c in bandwidthTest /usr/local/cuda/extras/demo_suite/bandwidthTest \
           /usr/local/cuda/samples/bin/x86_64/linux/release/bandwidthTest; do
    command -v "$c" >/dev/null 2>&1 && { BWBIN="$c"; break; }
    [[ -x "$c" ]] && { BWBIN="$c"; break; }
  done
  if [[ -z "$BWBIN" ]] && command -v nvcc >/dev/null 2>&1; then
    # No CUDA samples on the box, but nvcc is here: a 40-line pinned-memory H2D/D2H probe is enough,
    # and pinned is the right mode — it is what the prover's transfers use.
    cat > "$OUT/bw.cu" <<'CU'
#include <cstdio>
#include <cstdlib>          // atoi/atol — not guaranteed by <cstdio>, and a missing decl here
                            // fails the build, which reads as "no nvcc" and silently skips §7
#include <cuda_runtime.h>
int main(int argc,char**argv){
  int dev = argc>1?atoi(argv[1]):0; size_t mb = argc>2?atol(argv[2]):512;
  cudaSetDevice(dev);
  size_t n = mb<<20; void *h=nullptr,*d=nullptr;
  if(cudaHostAlloc(&h,n,cudaHostAllocDefault)!=cudaSuccess){printf("%d,PINNED_ALLOC_FAILED,0,0\n",dev);return 1;}
  if(cudaMalloc(&d,n)!=cudaSuccess){printf("%d,DEV_ALLOC_FAILED,0,0\n",dev);return 1;}
  cudaEvent_t a,b; cudaEventCreate(&a); cudaEventCreate(&b); float ms;
  cudaMemcpy(d,h,n,cudaMemcpyHostToDevice); cudaDeviceSynchronize();      // warm
  cudaEventRecord(a); for(int i=0;i<5;i++) cudaMemcpy(d,h,n,cudaMemcpyHostToDevice);
  cudaEventRecord(b); cudaEventSynchronize(b); cudaEventElapsedTime(&ms,a,b);
  double h2d = 5.0*n/(ms/1000.0)/1e9;
  cudaEventRecord(a); for(int i=0;i<5;i++) cudaMemcpy(h,d,n,cudaMemcpyDeviceToHost);
  cudaEventRecord(b); cudaEventSynchronize(b); cudaEventElapsedTime(&ms,a,b);
  double d2h = 5.0*n/(ms/1000.0)/1e9;
  printf("%d,ok,%.2f,%.2f\n",dev,h2d,d2h);
  return 0;
}
CU
    if nvcc -O2 -o "$OUT/bw" "$OUT/bw.cu" >"$OUT/bw.build.log" 2>&1; then BWBIN="$OUT/bw"; fi
  fi

  if [[ -n "$BWBIN" && "$BWBIN" == "$OUT/bw" ]]; then
    echo "gpu,status,h2d_GBps,d2h_GBps,mode" > "$OUT/bandwidth.csv"
    NG="$(n_gpus_total)"
    echo "== H2D/D2H per GPU (pinned, ${BW_MB} MB), one at a time =="
    for i in $(seq 0 $((NG-1))); do "$BWBIN" "$i" "$BW_MB" | sed 's/$/,serial/' >> "$OUT/bandwidth.csv"; done
    echo "== the same, ALL GPUs concurrently — this is where a shared switch uplink shows up =="
    for i in $(seq 0 $((NG-1))); do "$BWBIN" "$i" "$BW_MB" | sed 's/$/,concurrent/' >> "$OUT/bandwidth.csv" & done
    wait
    column -s, -t < "$OUT/bandwidth.csv" | tee -a "$R"
  elif [[ -n "$BWBIN" ]]; then
    echo "== bandwidthTest ($BWBIN) — GPU 0 only; re-run per device with --device=N ==" | tee -a "$R"
    "$BWBIN" --memory=pinned --mode=quick 2>&1 | tee -a "$R"
  else
    { echo "## bandwidth: SKIPPED — no bandwidthTest and no nvcc on this box."
      echo "## The link width in §2 is then the only evidence; it is a ceiling, not a measurement."
      echo "## Cheapest fix: apt install cuda-samples, or run this on a box with the CUDA toolkit."; } | tee -a "$R"
  fi
fi

echo
sed -n '1,200p' "$R"
echo
echo "Full report: $R"
echo "READ IT LIKE THIS:"
echo "  • pcie.link.width.max < 16, or many PIX/PXB pairs in §4 → host bandwidth is a real ceiling;"
echo "    expect t2's stream sweep to plateau early and GPU util to sit low."
echo "  • concurrent h2d_GBps collapsing vs serial → GPUs share a switch uplink. That is a BOX choice,"
echo "    not a config one: it argues for fewer GPUs per host, which is upstream's own topology."
echo "  • 'ulimit -l unlimited DENIED' → t4's locked arm needs a privileged/bare-metal box."
