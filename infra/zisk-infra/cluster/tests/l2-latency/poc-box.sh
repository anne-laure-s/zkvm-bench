#!/usr/bin/env bash
# poc-box.sh — ON THE MAC: drive the PoC run on a rented box, four verbs, one argument pair.
# RUNBOOK-POC.md is the procedure.
#
#   bash poc-box.sh check <host> <port>   ten seconds: GPUs, driver, disk, OS and glibc, CUDA
#                                         toolkit, memlock — before anything is copied
#   bash poc-box.sh start <host> <port>   copies the bundle and each key's tree (skipping what the
#                                         box already holds with the same sha256), checks them
#                                         there, unpacks the bundle and starts poc-run.sh
#   bash poc-box.sh log   <host> <port>   follows the run (Ctrl-C stops following, not the run)
#   bash poc-box.sh fetch <host> <port>   brings the result archives, the 1/2/4-GPU tables and the
#                                         run log back to results/gpu/, then reminds you to
#                                         destroy the instance
#
# <host> as vast.ai prints it (ssh5.vast.ai, or an IP); the user is root unless BOX_USER says so.
#
# Env: POC_KEYS="poc50f" · GPU_SETS="1 2 4" · PASSES=1 · ONLY · DRY_RUN=1 (passed to poc-run.sh)
#      L2BENCH=~/Documents/zkvms/l2-bench (the bundle in bundle/, the trees in poc/dist<suffix>/)
set -euo pipefail
L2BENCH="${L2BENCH:-$HOME/Documents/zkvms/l2-bench}"
BUNDLE="${BUNDLE:-$L2BENCH/bundle/l2-latency-bundle-poc.tar.gz}"
POC_KEYS="${POC_KEYS:-poc50f}"
CMD="${1:-}"; HOST="${2:-}"; PORT="${3:-}"
[ -n "$CMD" ] && [ -n "$HOST" ] && [ -n "$PORT" ] \
  || { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 2; }
DEST="${BOX_USER:-root}@$HOST"
SSH=(ssh -p "$PORT" "$DEST")
say() { printf '\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
# The box's sha256 of a file, the hash alone: a box that prints a banner on every ssh command would
# otherwise make every comparison fail.
rsha() { "${SSH[@]}" "sha256sum '$1' 2>/dev/null" | grep -oE '^[0-9a-f]{64}' | tail -1 || true; }

# The env poc-run.sh reads, forwarded only when set here.
forward() {
  local e="POC_KEYS='$POC_KEYS'" v
  for v in GPU_SETS PASSES ONLY SKIP_TOPO FORCE_INSTALL MIN_FREE_GB MIN_SHM_GB DRY_RUN VRAM_FLOOR_MIB; do
    [ -n "${!v:-}" ] && e="$e $v='${!v}'"
  done
  echo "$e"
}

case "$CMD" in
  check)
    "${SSH[@]}" 'nvidia-smi --query-gpu=index,name,memory.total,driver_version,power.limit --format=csv,noheader
      echo "disk:   $(df -h ~ | awk "NR==2{print \$4\" free of \"\$2}")"
      echo "os:     $(. /etc/os-release; echo "$PRETTY_NAME"), $(ldd --version | head -1)"
      echo "nvcc:   $( (PATH=/usr/local/cuda/bin:$PATH; nvcc --version 2>/dev/null | tail -1) || true)"
      echo "memlock: $(ulimit -l)"
      echo "shm:    $(df -h /dev/shm | awk "NR==2{print \$4\" free of \"\$2}")"
      echo "cpu:    $(nproc) threads, $(awk "/MemTotal/{printf \"%.0f GB\", \$2/1048576}" /proc/meminfo) RAM"'
    echo
    echo "Want: 4 GPUs with 32 GB or more each, nvcc 12.8 or later (a CUDA -devel image), Ubuntu 22.04+"
    echo "(glibc 2.35+), /dev/shm of 16 GB or more, about 95 GB free per PoC key ($POC_KEYS). memlock 64 is the usual vast.ai cap:"
    echo "the PoC install patches around it."
    ;;

  start)
    files=("$BUNDLE")
    for key in $POC_KEYS; do
      d="$L2BENCH/poc/dist${key#poc}"
      [ -f "$d/zisk-poc-src.tar.gz" ] || die "no $d/zisk-poc-src.tar.gz: run ZTREE=zisk${key#poc} poc/poc-pack.sh"
      files+=("$d/zisk-poc-src.tar.gz")
      [ -f "$d/provingKey.tar" ] && files+=("$d/provingKey.tar")
    done
    [ -f "$BUNDLE" ] || die "no bundle at $BUNDLE"
    say "copying to $DEST:$PORT"
    "${SSH[@]}" "mkdir -p $(for key in $POC_KEYS; do printf '%s-dist ' "$key"; done)"
    for f in "${files[@]}"; do
      case "$f" in
        "$BUNDLE") rel="$(basename "$f")" ;;
        *) key="poc${f#"$L2BENCH/poc/dist"}"; key="${key%%/*}"; rel="$key-dist/$(basename "$f")" ;;
      esac
      want="$(sha "$f")"
      have="$(rsha "$rel")"
      if [ "$have" = "$want" ]; then
        echo "   $rel: already there"
      else
        echo "   $rel ($(du -h "$f" | cut -f1))"
        scp -q -P "$PORT" "$f" "$DEST:$rel"
        have="$(rsha "$rel")"
        [ "$have" = "$want" ] || die "$rel arrived with another sha256 ($have, want $want)"
      fi
    done
    say "starting poc-run.sh ($(forward))"
    "${SSH[@]}" "tar xzf '$(basename "$BUNDLE")' && $(forward) bash zisk-infra/cluster/tests/l2-latency/poc-run.sh"
    echo
    echo "follow it:   bash $0 log $HOST $PORT"
    ;;

  log)
    "${SSH[@]}" -t 'tail -n 60 -f $(ls -t ~/poc-run-*.log | head -1)'
    ;;

  fetch)
    out="$L2BENCH/results/gpu"
    mkdir -p "$out"
    say "results -> $out"
    # One scp for the three kinds; a kind not there yet (no table before the run ends) is reported
    # by scp and does not stop the others.
    scp -P "$PORT" "$DEST:l2-latency-20*.tar.gz" "$DEST:poc-run-*.log" "$DEST:poc-summary-*.md" "$out/" \
      || echo "   (some of these were not on the box yet)"
    ls -la "$out" | tail -6
    echo
    echo "Destroy the instance once these are here; to keep it, stop the cluster first:"
    echo "  ssh -p $PORT $DEST 'bash ~/zisk-infra/cluster/stop.sh'"
    ;;

  *) die "unknown verb $CMD: check, start, log or fetch" ;;
esac
