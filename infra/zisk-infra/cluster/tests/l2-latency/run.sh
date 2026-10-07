#!/usr/bin/env bash
# run.sh — the whole L2 latency measurement, in one command, ON THE PROVER BOX:
#
#   bash ~/zisk-infra/cluster/tests/l2-latency/run.sh
#
# It detaches itself (nohup) unless L2LAT_FG=1, because a fresh box spends up to an hour
# installing and an ssh session that drops must not take the run with it. Follow it with the
# `tail -f` it prints. What it does, from whatever state the box is in:
#
#   1. up.sh: installs ZisK 1.3.1-alpha and its proving key if they are missing, brings the
#      coordinator and one worker on every GPU up, and waits for the worker to register;
#   2. with PRECHECK=1, replays the inputs ONLY selects through this box's ziskemu, which must
#      publish what its block's manifest records (check.py emu) -- a bad bundle stops there, not
#      an hour later. Off by default: a bundle already replayed on its release adds nothing, and
#      step 4 checks every proof against its block whatever this step did;
#   3. times a STARK proof of every input, PASSES times, for each GPU_SETS entry (bench-l2.sh):
#      by default every input of one ELF in a row, after that ELF's warm-up, then a few of the
#      first ELF's again to bound the box's drift over the run;
#   4. checks that every kept proof verifies and commits to its block (check.py publics);
#   5. writes summary.md and packs the run into ~/l2-latency-<stamp>.tar.gz.
#
# The proofs are STARK (VADCOP final) proofs, timed and verified as they are: no SNARK wrap.
#
# Env: PASSES=1 · WARMUPS=1 · ORDER=elf|pair · RECHECK=3 (see bench-l2.sh) · PRECHECK=0|1
#      GPU_SETS="all" — or e.g. "1 all" to time a one-GPU worker too (restarts between sets)
#      ONLY=<regex on input ids> · OUT=<results dir> · L2LAT_FG=1 (stay in the foreground)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="$(cd "$HERE/../.." && pwd)"
ZISK_INFRA="$(cd "$CLUSTER/.." && pwd)"
IN="$HERE/inputs"
STAMP="${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="${OUT:-$HOME/l2-latency-$STAMP}"
mkdir -p "$OUT"

if [ "${L2LAT_FG:-0}" != 1 ]; then
  STAMP="$STAMP" OUT="$OUT" L2LAT_FG=1 nohup setsid bash "$0" "$@" > "$OUT/run.log" 2>&1 < /dev/null &
  echo "running in the background (pid $!) — follow it with:"
  echo "  tail -f $OUT/run.log"
  exit 0
fi

# ZISK_HOME picks the ZisK install, ~/.zisk unless set: binaries, proving key, ELF cache. A build
# kept beside the release (the minimal-padding PoC in ~/.zisk-poc) runs through the very same
# steps; every ZisK binary reads ZISK_HOME as well.
export ZISK_HOME="${ZISK_HOME:-$HOME/.zisk}"
export PATH="$ZISK_HOME/bin:$HOME/.cargo/bin:$PATH"
export ZISK_VER=1.3.1-alpha
PASSES="${PASSES:-1}"; WARMUPS="${WARMUPS:-1}"; ORDER="${ORDER:-elf}"; RECHECK="${RECHECK:-3}"
GPU_SETS="${GPU_SETS:-all}"
say()  { printf '\n\033[1m== %s\033[0m  (%s)\n' "$*" "$(date -u +%H:%M:%S)"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "run.sh runs on the prover box (Linux), not $(uname -s)"
command -v nvidia-smi >/dev/null 2>&1 || die "no nvidia-smi — this is not a GPU box"
[ -f "$IN/inputs.csv" ] || die "no $IN/inputs.csv — the bundle was built without its inputs"
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3 >/dev/null; } \
  || die "python3 is missing and could not be installed"
cp "$IN/inputs.csv" "$IN/provenance.txt" "$OUT/" 2>/dev/null
say "L2 latency run -> $OUT"
sed 's/^/   /' "$IN/provenance.txt" 2>/dev/null
nvidia-smi -L | sed 's/^/   /'
NGPU="$(nvidia-smi -L | wc -l | tr -d ' ')"

up() { # up <log> [env…] — up.sh with this run's env, never leaving a failure unnoticed
  local log="$1"; shift
  env "$@" bash "$CLUSTER/up.sh" > "$log" 2>&1
  local rc=$?
  tail -4 "$log" | sed 's/^/   /'
  return $rc
}

# ── 1. the cluster ────────────────────────────────────────────────────────────────────────────
say "1/5 cluster up (installs ZisK $ZISK_VER on a fresh box: 15-60 min)"
up "$OUT/up.log" || die "up.sh failed — read $OUT/up.log"
# Each ELF's one-time ROM setup (its Merkle root and its three ASM emulators), several ELFs at a
# time, rather than one by one at each ELF's first setup while the box waits (asm-prebuild.sh).
say "1/5 the ELFs' ROM setups, ahead of the first proof"
ONLY="${ONLY:-.}" bash "$HERE/asm-prebuild.sh" "$IN" "$OUT/asm-prebuild" \
  || warn "asm-prebuild.sh failed — the worker sets up each ELF at its first setup"

# ── 2. the inputs, through this box's emulator ────────────────────────────────────────────────
if [ "${PRECHECK:-0}" = 1 ]; then
  say "2/5 ziskemu replay of the selected inputs"
  ONLY="${ONLY:-.}" python3 "$HERE/check.py" emu "$IN" "$OUT/precheck.csv" \
    || die "the staged inputs do not reproduce on this box — read $OUT/precheck.csv"
else
  say "2/5 ziskemu replay skipped (PRECHECK=1 to run it)"
fi

# ── 3. STARK proofs, per worker config ────────────────────────────────────────────────────────
NSEL=$(python3 - "$IN/inputs.csv" "${ONLY:-.}" <<'EOF'
import csv, re, sys
print(sum(1 for r in csv.DictReader(open(sys.argv[1])) if re.search(sys.argv[2], r['id'])))
EOF
)
say "3/5 STARK proofs: ${PASSES} pass(es) over $NSEL of $(($(wc -l < "$IN/inputs.csv") - 1)) inputs, order $ORDER, GPU sets: $GPU_SETS"
LAST_SET=all
for set in $GPU_SETS; do
  if [ "$set" = all ]; then
    unset CUDA_VISIBLE_DEVICES
    [ "$LAST_SET" = all ] || up "$OUT/up-gpus-all.log" FORCE_RESTART=1 \
      || { warn "could not restart on all GPUs — skipping that set"; continue; }
  else
    [ "$set" -le "$NGPU" ] 2>/dev/null || { warn "GPU set $set > $NGPU GPUs here — skipped"; continue; }
    export CUDA_VISIBLE_DEVICES="$(seq -s, 0 $((set - 1)))"
    up "$OUT/up-gpus-$set.log" FORCE_RESTART=1 CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
      || { warn "could not restart on $set GPU(s) — skipping that set"; continue; }
  fi
  LAST_SET="$set"
  OUT="$OUT/stark-$set" GPUS="$set" PASSES="$PASSES" WARMUPS="$WARMUPS" ORDER="$ORDER" \
    RECHECK="$RECHECK" ONLY="${ONLY:-.}" \
    bash "$HERE/bench-l2.sh" || warn "bench-l2.sh exited non-zero for GPU set $set"
  cp "$CLUSTER/logs/worker.log" "$OUT/stark-$set/worker.log" 2>/dev/null
  cp "$CLUSTER/logs/coordinator.log" "$OUT/stark-$set/coordinator.log" 2>/dev/null
done
unset CUDA_VISIBLE_DEVICES

# ── 4. public values, now that nothing is being timed ─────────────────────────────────────────
say "4/5 every kept proof, verified and read back"
ZP="$ZISK_INFRA/zisk-publics"
ZPBIN=""
# A runnable binary answers no arguments with its usage and exit 2; one built against another
# glibc fails to load (127) and is passed over for a build.
for c in "$ZP/bin/zisk-publics" "$ZP/target/release/zisk-publics"; do
  if [ -x "$c" ]; then
    "$c" >/dev/null 2>&1
    [ $? -eq 2 ] && { ZPBIN="$c"; break; }
  fi
done
if [ -z "$ZPBIN" ]; then
  echo "   no runnable prebuilt zisk-publics — building it (zisk-sdk 1.3.1-alpha, cpu-only)"
  command -v rustup >/dev/null 2>&1 && rustup toolchain install stable --profile minimal > "$OUT/zisk-publics-build.log" 2>&1
  ( cd "$ZP" && cargo +stable build --release --locked >> "$OUT/zisk-publics-build.log" 2>&1 ) \
    && ZPBIN="$ZP/target/release/zisk-publics" \
    || warn "zisk-publics did not build — read $OUT/zisk-publics-build.log; proofs left unchecked"
fi
if [ -n "$ZPBIN" ]; then
  ZISK_PUBLICS="$ZPBIN" python3 "$HERE/check.py" publics "$IN" "$OUT" \
    || warn "some proofs do not verify or do not commit to their block — read $OUT/publics.csv"
fi

# ── 5. summary and archive ────────────────────────────────────────────────────────────────────
say "5/5 summary"
python3 "$HERE/summarize.py" "$OUT" > "$OUT/summary.md" 2> "$OUT/summarize.err" \
  || warn "summarize.py failed — read $OUT/summarize.err (the raw CSVs are all there)"
cat "$OUT/summary.md"
tar czf "$HOME/l2-latency-$STAMP.tar.gz" --exclude='*.proof' \
  -C "$(dirname "$OUT")" "$(basename "$OUT")"
say "done — $HOME/l2-latency-$STAMP.tar.gz ($(du -h "$HOME/l2-latency-$STAMP.tar.gz" | cut -f1))"
echo "   fetch it from the Mac with:  scp -P <port> root@<host>:l2-latency-$STAMP.tar.gz ."
