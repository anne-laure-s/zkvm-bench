#!/usr/bin/env bash
# t1-numa.sh — THE test. Does the `[ALARM] got 8/16 NUMA-local GPUs` in our own worker.log
# actually cost us throughput?
#
# Compares 8 GPUs that are ALL on one NUMA node against 8 GPUs straddling both sockets. Same GPU
# count, same everything else — the only variable is whether host↔device traffic crosses the
# inter-socket link. If `local8` beats `split8` on Msteps/s, the ALARM is real and the 16-GPU
# single-process NO_MPI config is leaving performance on the table.
#
# This doubles as the 8-GPU RTP bring-up: `local8` IS the config we want to ship, so its numbers go
# straight into the 8-GPU latency model instead of the derived one.
#
#   cd ~/zisk-infra/cluster && ./start.sh          # coordinator must be up (worker gets replaced)
#   bash tests/t1-numa.sh
#
# Env: GPUS=8 · PASSES=2 · BLOCKS="1-… 1-…" · AB=1 (A-B-A ordering) · WITH_ALL16=1 (add a 16-GPU arm)
#
# ⚠️ A-B-A by default: this box is a SHARED host, so a neighbour's load during one arm would read as
# a config difference. `local8` runs twice, first and last; if the two disagree by more than the
# local8↔split8 gap, the run is contaminated and proves nothing. Do not skip it to save 10 minutes.
#
# Cost: ~4 arms × (worker registration ~4-6 min + 1 warm-up + PASSES × |BLOCKS| proofs) ≈ 35-45 min.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
trap lib_cleanup EXIT INT TERM   # kills the GPU sampler; warns if the box is left on a test config

GPUS="${GPUS:-8}"
AB="${AB:-1}"

out_init t1-numa
preflight || exit 1

NTOT="$(n_gpus_total)"
echo "== box: $NTOT GPU(s), NUMA map (cuda_index numa_node pci_bdf) =="
gpu_numa_map | tee "$OUT/gpu-numa.txt"

# Checked here, before the count checks below, so the -1 case gets its own explanation instead of
# being reported as "not enough GPUs on one node" — which would send you looking at the wrong thing.
if awk '$2=="-1"{f=1} END{exit !f}' "$OUT/gpu-numa.txt"; then
  echo "STOP: at least one GPU reports numa_node = -1 — the kernel exposes no affinity here (NUMA off" >&2
  echo "      in BIOS, or the container hides it). Neither arm of this test can be built honestly:" >&2
  echo "      a 'split' set assembled from unknown-affinity GPUs is not a cross-socket set." >&2
  echo "      Re-run on a box that exposes both nodes; t3-topo.sh §3 shows what this box exposes." >&2
  exit 2
fi

LOCAL_SET="$(numa_set "$GPUS" local)" || { echo "ERROR: cannot pick $GPUS GPUs on a single NUMA node (box has $NTOT)" >&2; exit 1; }
SPLIT_SET="$(numa_set "$GPUS" split)" || {
  echo "NOTE: cannot build a cross-socket set of $GPUS — this box exposes a single NUMA node." >&2
  echo "      Then the [ALARM] is unfixable HERE: retest on a box that exposes both nodes." >&2
  exit 2
}
echo "  local$GPUS = $LOCAL_SET"
echo "  split$GPUS = $SPLIT_SET"
[[ "$LOCAL_SET" == "$SPLIT_SET" ]] && { echo "ERROR: the two arms resolved to the same GPUs — refusing to run a null experiment" >&2; exit 1; }

# One arm = (label, device list). A-B-A puts the repeat of A last so drift is visible end-to-end.
# Built BEFORE announce_plan, which counts it: announcing a plan the run does not follow is worse
# than not announcing one.
ARMS=("local$GPUS:$LOCAL_SET" "split$GPUS:$SPLIT_SET")
[[ "$AB" == 1 ]] && ARMS+=("local${GPUS}b:$LOCAL_SET")
[[ "${WITH_ALL16:-}" == 1 ]] && ARMS+=("all$NTOT:")

announce_plan "${#ARMS[@]}" "AB=0 drops the drift-check arm; WITH_ALL16=1 adds a 16-GPU arm."
setup_elf_once

for arm in "${ARMS[@]}"; do
  cfg="${arm%%:*}"; devs="${arm#*:}"
  if [[ -n "$devs" ]]; then export CUDA_VISIBLE_DEVICES="$devs"; else unset CUDA_VISIBLE_DEVICES; fi
  restart_worker "$cfg" || { log_ "  → skipping arm $cfg"; continue; }
  run_bench t1-numa "$cfg"
done

restore_default_worker
echo
bash "$TESTS_DIR/summary.sh" "$RESULTS"
echo
echo "READ IT LIKE THIS:"
echo "  • numa_local == numa_total on local$GPUS (no [ALARM]) and < on split$GPUS → the arms really differ."
echo "  • local$GPUS faster on msteps_per_s_per_gpu → NUMA confirmed; ship 8 GPUs from ONE socket,"
echo "    and the 16-GPU config needs the MPI multi-rank path (t5) to stop straddling."
echo "  • local$GPUS ≈ split$GPUS → NUMA is NOT the main cost; go spend the time on t2/t3 instead."
echo "  • local$GPUS vs local${GPUS}b disagreeing → shared-host noise. Re-run when the box is quiet."
echo "Results: $OUT"
