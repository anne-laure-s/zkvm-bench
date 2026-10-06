#!/usr/bin/env bash
# poc-run.sh — ON THE PROVER BOX: the minimal-padding PoC, timed on 1, 2 and 4 GPUs of this box,
# in one command. RUNBOOK-POC.md is the procedure; poc-box.sh (on the Mac) copies what this needs
# and starts it.
#
#   bash ~/zisk-infra/cluster/tests/l2-latency/poc-run.sh
#
# Needs the bundle unpacked in ~ and, for each key, the tree poc-pack.sh made on the Mac in
# ~/<key>-dist/ (zisk-poc-src.tar.gz, and provingKey.tar if it was packed --with-key). The release
# is neither installed nor timed: its numbers are the earlier runs'.
#
#   1. checks the box: Linux, GPUs, the CUDA toolkit (the PoC is built from source), disk;
#   2. on a box that has no PoC install yet, measures d2h first (t3-topo.sh, ~2 min), the gate
#      up.sh applies before a release install, with its thresholds;
#   3. builds and sets up every key not installed yet, all at once (poc-install.sh, ~45 min);
#   4. for each key, run.sh: the worker restarted on each GPU set in turn (1, then 2, then 4
#      GPUs), PASSES passes over the inputs ONLY selects, every proof verified pinned to that key,
#      the run packed into ~/l2-latency-<stamp>-<key>.tar.gz, and the 1/2/4-GPU table
#      (poc-summary.py) in ~/poc-summary-<stamp>-<key>.md.
#
# It detaches itself (nohup setsid) unless POC_FG=1; the log is ~/poc-run-<stamp>.log.
#
# Env: POC_KEYS="poc50f" (each one installed in ~/.zisk-<key>; "poc50f poc50" times both)
#      GPU_SETS="1 2 4" · PASSES=3 · ONLY=<regex on input ids>, default every staged arm's sweep
#      selection at 1 to 1,000 transactions · MIN_FREE_GB=70 per key to install
#      SKIP_TOPO=1 · FORCE_INSTALL=1 (install past a bad d2h verdict) · DRY_RUN=1 (print the plan)
#      VRAM_FLOOR_MIB (up.sh's; it reaches up.sh through run.sh)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="$(cd "$HERE/../.." && pwd)"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
LOG="$HOME/poc-run-$STAMP.log"
POC_KEYS="${POC_KEYS:-poc50f}"
GPU_SETS="${GPU_SETS:-1 2 4}"
PASSES="${PASSES:-3}"
ONLY="${ONLY:--sweep-d(0001-b[123]|0010-b1|0025-b1|0050-b1|0100-b1|0250-b1|1000-b1)$}"
MIN_FREE_GB="${MIN_FREE_GB:-70}"
DRY="${DRY_RUN:-0}"
say()  { printf '\n\033[1m== %s\033[0m  (%s)\n' "$*" "$(date -u +%H:%M:%S)"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$DRY" != 1 ] && [ "${POC_FG:-0}" != 1 ]; then
  STAMP="$STAMP" POC_FG=1 nohup setsid bash "$0" "$@" > "$LOG" 2>&1 < /dev/null &
  echo "running in the background (pid $!) — follow it with:"
  echo "  tail -f $LOG"
  exit 0
fi

# ── 1. the box ────────────────────────────────────────────────────────────────────────────────
say "PoC run: keys [$POC_KEYS], GPU sets [$GPU_SETS], $PASSES pass(es)"
if [ "$DRY" != 1 ]; then
  [ "$(uname -s)" = Linux ] || die "poc-run.sh runs on the prover box (Linux), not $(uname -s)"
  command -v nvidia-smi >/dev/null 2>&1 || die "no nvidia-smi — this is not a GPU box"
fi
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3 >/dev/null; } \
  || die "python3 is missing and could not be installed"
NGPU="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')"
nvidia-smi -L 2>/dev/null | sed 's/^/   /'
for set in $GPU_SETS; do
  [ "$set" = all ] || [ "$set" -le "${NGPU:-0}" ] 2>/dev/null \
    || warn "GPU set $set > the $NGPU GPU(s) here: run.sh will skip it"
done
NSEL=$(python3 - "$HERE/inputs/inputs.csv" "$ONLY" <<'EOF'
import csv, re, sys
print(sum(1 for r in csv.DictReader(open(sys.argv[1])) if re.search(sys.argv[2], r['id'])))
EOF
)
[ "${NSEL:-0}" -gt 0 ] || die "ONLY selects no input of $HERE/inputs/inputs.csv"
echo "   $NSEL inputs selected (ONLY=$ONLY)"

# Which keys still need their install. poc-install.sh writes ~/.zisk-<key>/.installed last of
# all, with the sha256 of the tree it installed: a key counts as installed when that is the tree in
# ~/<key>-dist now. It starts from scratch, so it runs only for the others.
TODO=()
for key in $POC_KEYS; do
  home="$HOME/.zisk-$key"
  src="$HOME/$key-dist/zisk-poc-src.tar.gz"
  have="$(cut -d' ' -f1 "$home/.installed" 2>/dev/null || true)"
  if [ -n "$have" ] && { [ ! -f "$src" ] || [ "$have" = "$(sha256sum "$src" | cut -d' ' -f1)" ]; }; then
    echo "   $key: installed in $home"
  else
    [ -f "$src" ] || die "$key: no $src — copy it from the Mac's poc/dist${key#poc}/ (poc-box.sh start does)"
    echo "   $key: to install from ~/$key-dist ($(ls "$HOME/$key-dist" | tr '\n' ' '))${have:+, replacing an install of another tree}"
    TODO+=("$key")
  fi
done

if [ "${#TODO[@]}" -gt 0 ]; then
  export PATH="/usr/local/cuda/bin:$PATH"
  command -v nvcc >/dev/null 2>&1 || [ "$DRY" = 1 ] \
    || die "no nvcc: the PoC is built from source and needs the CUDA toolkit. Rent an image with it (a -devel CUDA image)."
  # poc-install.sh builds for ZisK's "major" archs, sm_80 to sm_120 (RTX 50xx): nvcc 12.8 or later,
  # or the build stops on compute_120 a quarter of an hour in.
  NVCC_REL="$(nvcc --version 2>/dev/null | sed -n 's/.*release \([0-9]*\.[0-9]*\).*/\1/p')"
  echo "   nvcc ${NVCC_REL:-none}"
  [ "$DRY" = 1 ] || awk -v v="${NVCC_REL:-0}" 'BEGIN { split(v, a, "."); exit !(a[1] > 12 || (a[1] == 12 && a[2] >= 8)) }' \
    || die "nvcc ${NVCC_REL:-?} is too old to build for sm_120 (RTX 50xx): rent an image with CUDA 12.8 or later"
  FREE_GB="$(df -Pk "$HOME" | awk 'NR==2{print int($4/1048576)}')"
  NEED_GB=$(( MIN_FREE_GB * ${#TODO[@]} ))
  echo "   disk: ${FREE_GB} GB free, ~${NEED_GB} GB needed (tree, key and its GPU constant trees: ${MIN_FREE_GB} GB a key)"
  [ "$FREE_GB" -ge "$NEED_GB" ] || [ "$DRY" = 1 ] \
    || die "too little disk for ${#TODO[@]} key(s). Rent a bigger disk, or set MIN_FREE_GB lower on your own judgement."
fi

# ── 2. d2h, while the box is still idle ───────────────────────────────────────────────────────
# up.sh measures it before installing the release, the one moment a figure no listing shows can be
# taken without a proof in flight; a PoC-only box never takes that branch, so it is taken here.
# Same probe, same reduction (the worst GPU of the worse mode), same thresholds as up.sh's gate.
if [ "${#TODO[@]}" -gt 0 ] && [ "${SKIP_TOPO:-0}" != 1 ]; then
  say "1/3 d2h on the idle box (t3-topo.sh, ~2 min)"
  if [ "$DRY" = 1 ]; then
    echo "   [dry run] OUT=~/tests/t3-topo-poc-$STAMP BW=1 bash $CLUSTER/tests/t3-topo.sh"
  else
    TOPO="$HOME/tests/t3-topo-poc-$STAMP"
    OUT="$TOPO" BW=1 bash "$CLUSTER/tests/t3-topo.sh" > "$HOME/topo.log" 2>&1 || warn "t3-topo exited non-zero — read ~/topo.log"
    VERDICT="$(awk -F, 'NR>1 && $2=="ok" {
          m=$5; gsub(/[ \t\r]/,"",m)
          if (!(m in md) || $4+0 < md[m]) { md[m]=$4+0; mh[m]=$3+0 }
        }
        END {
          for (m in md) if (w == "" || md[m] < md[w]) w=m
          if (w == "") { print "unknown"; exit }
          h=mh[w]; d=md[w]; r=(h>0)?d/h:0
          v=(h<=0||d<=0)?"unknown":(d<40||r<0.70)?"bad":(d<48||r<0.85)?"marginal":"good"
          printf "%s %s d2h %.2f GB/s h2d %.2f ratio %.2f\n", v, w, d, h, r
        }' "$TOPO/bandwidth.csv" 2>/dev/null)"
    echo "   ${VERDICT:-unknown (no bandwidth.csv — read ~/topo.log)}"
    case "${VERDICT%% *}" in
      bad) if [ "${FORCE_INSTALL:-0}" = 1 ]; then warn "the starved case — FORCE_INSTALL=1, going on"
           else die "THIS IS THE STARVED CASE: drop this box (FORCE_INSTALL=1 to go on anyway, keeping this figure)"; fi ;;
      marginal) warn "below the healthy box (d2h 56.97, ratio 1.02): keep this figure with the results" ;;
      good) ;;
      *) warn "d2h unknown — read ~/topo.log; going on blind" ;;
    esac
  fi
else
  [ "${#TODO[@]}" -gt 0 ] && why="SKIP_TOPO=1" || why="nothing to install, the box may be busy"
  say "1/3 d2h gate skipped ($why)"
fi

# ── 3. the PoC installs ───────────────────────────────────────────────────────────────────────
if [ "${#TODO[@]}" -gt 0 ]; then
  say "2/3 building and setting up ${TODO[*]} (~45 min; logs ~/<key>-install.log)"
  pids=()
  for key in "${TODO[@]}"; do
    if [ "$DRY" = 1 ]; then
      echo "   [dry run] POC_HOME=~/.zisk-$key POC_TREE=~/zisk-$key bash $HERE/poc-install.sh ~/$key-dist"
      continue
    fi
    POC_HOME="$HOME/.zisk-$key" POC_TREE="$HOME/zisk-$key" \
      bash "$HERE/poc-install.sh" "$HOME/$key-dist" > "$HOME/$key-install.log" 2>&1 &
    pids+=("$!")
  done
  fail=0
  for p in ${pids[@]+"${pids[@]}"}; do wait "$p" || fail=1; done
  for key in "${TODO[@]}"; do
    [ "$DRY" = 1 ] && continue
    if [ -f "$HOME/.zisk-$key/provingKey/zisk/vadcop_final/vadcop_final.verkey.json" ]; then
      echo "   $key: $(tail -2 "$HOME/$key-install.log" | head -1 | sed 's/\x1b\[[0-9;]*m//g')"
    else
      tail -15 "$HOME/$key-install.log" | sed 's/^/     /' >&2
      warn "$key did not install — read ~/$key-install.log"; fail=1
    fi
  done
  [ "$fail" = 0 ] || die "an install failed: nothing timed"
else
  say "2/3 every key already installed"
fi

# ── 4. the timings, one key after the other ───────────────────────────────────────────────────
# One stack setting for every worker: a 2-GPU worker once died at startup on a stack overflow,
# and these are what let it start (RUST_MIN_STACK for the Rust threads, OMP_STACKSIZE for OpenMP's).
export RUST_MIN_STACK="${RUST_MIN_STACK:-67108864}" OMP_STACKSIZE="${OMP_STACKSIZE:-64M}"
ulimit -l unlimited 2>/dev/null || true
say "3/3 STARK proofs on GPU sets [$GPU_SETS]"
RESULTS=()
for key in $POC_KEYS; do
  KSTAMP="$(date -u +%Y%m%dT%H%M%SZ)-$key"
  echo "   $key -> ~/l2-latency-$KSTAMP"
  if [ "$DRY" = 1 ]; then
    echo "   [dry run] ZISK_HOME=~/.zisk-$key FORCE_RESTART=1 GPU_SETS='$GPU_SETS' PASSES=$PASSES STAMP=$KSTAMP L2LAT_FG=1 ONLY='$ONLY' bash $HERE/run.sh"
    continue
  fi
  ZISK_HOME="$HOME/.zisk-$key" FORCE_RESTART=1 GPU_SETS="$GPU_SETS" PASSES="$PASSES" ONLY="$ONLY" \
    STAMP="$KSTAMP" L2LAT_FG=1 bash "$HERE/run.sh" || warn "run.sh exited non-zero for $key"
  RESULTS+=("$HOME/l2-latency-$KSTAMP.tar.gz")
  python3 "$HERE/poc-summary.py" "$HOME/l2-latency-$KSTAMP" > "$HOME/poc-summary-$KSTAMP.md" \
    && cat "$HOME/poc-summary-$KSTAMP.md" || warn "poc-summary.py failed for $key"
done

say "done"
for r in ${RESULTS[@]+"${RESULTS[@]}"}; do
  [ -f "$r" ] && echo "   $r ($(du -h "$r" | cut -f1))" || warn "no $r — read $LOG"
done
echo "   from the Mac:  bash poc-box.sh fetch <host> <port>"
