#!/usr/bin/env bash
# t5-mpi.sh — does upstream's OWN multi-GPU topology beat ours on the same 16 GPUs?
#
# We prove with ONE process driving every GPU (`NO_MPI`), because the MPI
# multi-rank path segfaults on this unprivileged container: `--bind-to numa` cannot bind memory on
# socket-1 ranks. That workaround is exactly what produces the `[ALARM] got 8/16 NUMA-local GPUs`,
# because a single process pinned to NUMA 0 ends up driving eight GPUs across the socket link.
# Upstream's layout — ~2 GPUs per rank, each rank NUMA-bound — has no such straddle.
#
# So this runs LAST and on ALL GPUs, deliberately: t1 answers the 8-GPU question by choosing GPUs,
# t5 answers the 16-GPU question by choosing a process layout. Do not restrict CUDA_VISIBLE_DEVICES
# here — with 8 GPUs on one node, `ppr:N:numa` would still place ranks on the empty node and hand
# them remote GPUs, which measures nothing.
#
# Arms:
#   nompi-all : what we ship today (1 process, all GPUs) — the reference
#   mpi-numa  : upstream's layout, NP from mpi_params.sh, `-map-by ppr:N:numa --bind-to numa`
#   mpi-slot  : the container-safe fallback start.sh documents — 1 rank per GPU, `slot` + no binding.
#               Deterministic 1:1 without asking the container for a NUMA bind it cannot grant.
#
#   bash tests/t5-mpi.sh
#
# Env: ZISK_SRC=~/zisk (needs the clone for mpi_params.sh) · FETCH_MPI_PARAMS=1 (fetch just that one
#      file from upstream instead of cloning) · PASSES=2 · ARMS="nompi-all mpi-numa mpi-slot"
#
# ⚠️ Expect mpi-numa to FAIL on the vast.ai container — that is a result, not a bug. The script
# records it and moves on. Its value is the comparison on a box where it DOES come up.
#
# ⚠️ 16-GPU registration is ~8-12 min per arm (30 GB × 16 allocation). Three arms ≈ 45-60 min.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
trap lib_cleanup EXIT INT TERM   # kills the GPU sampler; warns if the box is left on a test config

ARMS="${ARMS:-nompi-all mpi-numa mpi-slot}"
ZISK_SRC="${ZISK_SRC:-$HOME/zisk}"
MP_REL="distributed/deploy/scripts/common/mpi_params.sh"

out_init t5-mpi
preflight || exit 1

unset CUDA_VISIBLE_DEVICES      # see the header: this test is about layout, not device selection
NTOT="$(n_gpus_total)"
# Guard before it is used as a divisor below: `nproc / 0` is a bash arithmetic fatal, not a warning.
[[ "$NTOT" =~ ^[0-9]+$ && "$NTOT" -ge 1 ]] || { echo "ERROR: nvidia-smi reported no GPUs ($NTOT)" >&2; exit 1; }
echo "== box: $NTOT GPU(s) — all of them, on purpose =="
gpu_numa_map | tee "$OUT/gpu-numa.txt"

command -v mpirun >/dev/null 2>&1 || {
  echo "NOTE: mpirun not found (apt install openmpi-bin). Only the nompi-all arm can run." >&2
  ARMS="nompi-all"
}

# ── mpi_params.sh must exist AND emit shell assignments ────────────────────────────────────────
# start.sh does `eval "$(bash "$mp" --quiet)"`. Upstream's tools/ copy prints human tables and no
# assignments at all, so a wrong path here fails as "socket detection failed" and sends you hunting
# a NUMA problem that is really a missing file. Check it before the 12-minute registration.
if [[ "$ARMS" == *mpi-* ]]; then
  if [[ ! -f "$ZISK_SRC/$MP_REL" && "${FETCH_MPI_PARAMS:-}" == 1 ]]; then
    log_ "fetching $MP_REL from upstream into $OUT/zisk-src (FETCH_MPI_PARAMS=1)"
    mkdir -p "$OUT/zisk-src/$(dirname "$MP_REL")"
    curl -fsSL -o "$OUT/zisk-src/$MP_REL" \
      "https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/$MP_REL" \
      && ZISK_SRC="$OUT/zisk-src" \
      || echo "  ✗ fetch failed — falling back to whatever is at $ZISK_SRC" >&2
  fi
  if [[ -f "$ZISK_SRC/$MP_REL" ]]; then
    export ZISK_SRC
    echo "== mpi_params.sh pre-check ($ZISK_SRC/$MP_REL) =="
    bash "$ZISK_SRC/$MP_REL" --quiet 2>&1 | tee "$OUT/mpi_params.out" | sed 's/^/  /'
    if grep -q 'MPI_NP=' "$OUT/mpi_params.out"; then
      eval "$(grep -E '^(export )?MPI_[A-Z_]+=' "$OUT/mpi_params.out")" || true
      echo "  → MPI_NP=${MPI_NP:-?} MPI_PPR=${MPI_PPR:-?} RAYON=${MPI_RAYON_NUM_THREADS:-?}"
    else
      echo "  ✗ no MPI_NP= in its output — start.sh's eval will produce nothing and the MPI arms"
      echo "    will abort with 'socket detection failed'. Skipping them."
      ARMS="nompi-all"
    fi
  else
    echo "NOTE: $ZISK_SRC/$MP_REL not found — set ZISK_SRC=<zisk clone> or FETCH_MPI_PARAMS=1." >&2
    echo "      Skipping the MPI arms." >&2
    ARMS="nompi-all"
  fi
fi

announce_plan "$(wc -w <<<"$ARMS")" "16-GPU registrations are the slow ones here."
setup_elf_once

for arm in $ARMS; do
  unset USE_MPI NO_MPI MPI_NP_OVERRIDE MPI_MAPBY MPI_BIND MPI_RAYON_OVERRIDE
  case "$arm" in
    nompi-all) : ;;                                    # start.sh's default is single-process
    mpi-numa)
      export USE_MPI=1                                 # NP/PPR/RAYON come from mpi_params.sh
      ;;
    mpi-slot)
      export USE_MPI=1 MPI_NP_OVERRIDE="$NTOT" MPI_MAPBY=slot MPI_BIND=none
      # 1 rank per GPU with no NUMA binding: the layout start.sh documents for containers where
      # membind is blocked. Ranks still land wherever the scheduler puts them, so this tests the
      # rank-per-GPU split WITHOUT testing NUMA locality — read it as a middle point, not a fix.
      export MPI_RAYON_OVERRIDE="$(( $(nproc) / NTOT ))"
      ;;
    *) log_ "unknown arm '$arm' — skipping"; continue ;;
  esac
  log_ "arm $arm (USE_MPI=${USE_MPI:-0} NP=${MPI_NP_OVERRIDE:-${MPI_NP:-auto}} map=${MPI_MAPBY:-auto} bind=${MPI_BIND:-auto})"
  if restart_worker "$arm"; then
    run_bench t5-mpi "$arm"
  else
    # Classify the failure — "it crashed" and "it crashed for the documented NUMA reason" are
    # different findings, and only the second one is evidence about this box.
    f="$OUT/$arm.worker-startup.log"
    if grep -qai 'failed to bind memory' "$f" 2>/dev/null; then
      echo "  → CONFIRMED: '--bind-to numa' cannot bind memory in this container (the documented cause)."
    elif grep -qai 'segmentation fault' "$f" 2>/dev/null; then
      echo "  → segfault during rank startup (see $f) — the ASM-microservice startup race."
    else
      echo "  → did not register for another reason; read $f before concluding anything about NUMA."
    fi
  fi
done

restore_default_worker
echo
bash "$TESTS_DIR/summary.sh" "$RESULTS"
echo
echo "READ IT LIKE THIS:"
echo "  • mpi-numa up AND faster, with numa_local == numa_total → upstream's layout is the 16-GPU fix;"
echo "    the cost of getting it is a privileged container or bare metal, nothing in our code."
echo "  • mpi-numa refuses to start → on THIS class of box the ALARM is structural. Then the 16-GPU"
echo "    config is capped, and 2 hosts × 8 NUMA-local GPUs beats 1 host × 16 (which is, in fact,"
echo "    upstream's own topology: one worker per host, coordinator fans segments out)."
echo "  • mpi-slot between the two → the win is partly the rank split, partly NUMA. Worth keeping"
echo "    even on the container, since it needs no extra privilege."
echo "Results: $OUT"
