#!/usr/bin/env bash
# t2-tune.sh — fill in the tuning table that docs/zisk-benchmark.md has been carrying EMPTY, and
# find out what the two defaults we never touched are costing us.
#
# Two knobs, and the reason each is suspect:
#   --compute-capacity : our worker advertises `Compute Cap 10CU` — the bare default — on a box with
#                        ~192 threads and 16×3 = 48 GPU streams. Upstream's rule is "one unit per
#                        physical core minus two, plus one per GPU stream", i.e. ~230 here. It is the
#                        number the coordinator uses to size the work it hands out.
#   --max-streams      : left on auto, which derived 3 streams/GPU for basic proofs from the 30.6 GB
#                        free. More streams = more overlap but less buffer each; nobody checked
#                        whether 3 is the peak or just what fits.
# Also samples GPU utilisation per config, so the "approx GPU util" column stops being a guess.
#
#   bash tests/t2-tune.sh
#
# Env: GPUS=8 (0 = use all) · STREAMS="auto 2 4" · CAPS="rec default" · PASSES=2 · RAYON=<n>
#
# ⚠️ Grid size is deliberate: |STREAMS| × |CAPS| arms, each paying a full worker registration
# (~4-6 min on 8 GPUs). The default 3×1 + 1 isolation arm ≈ 4 arms ≈ 35-45 min. A 4×3 grid is three
# hours — decide that on purpose, not by editing a default.
#
# ⚠️ Run t1 FIRST and pin GPUS to a NUMA-local set. Sweeping streams while half the GPUs are on the
# wrong socket measures the interaction of two variables, and you will not be able to separate them.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
trap lib_cleanup EXIT INT TERM   # kills the GPU sampler; warns if the box is left on a test config

GPUS="${GPUS:-8}"
STREAMS="${STREAMS:-auto 2 4}"
CAPS="${CAPS:-rec}"           # rec = recommended_capacity, default = leave unset (10 CU)

out_init t2-tune
preflight || exit 1

if [[ "$GPUS" != 0 ]]; then
  DEVS="$(numa_set "$GPUS" local)" || { echo "ERROR: cannot pick $GPUS NUMA-local GPUs (GPUS=0 to use all)" >&2; exit 1; }
  export CUDA_VISIBLE_DEVICES="$DEVS"
  echo "== pinned to $GPUS NUMA-local GPUs: $DEVS =="
else
  unset CUDA_VISIBLE_DEVICES
  GPUS="$(n_gpus_total)"
  echo "== using all $GPUS GPUs (GPUS=0) — NOTE: on a 2-socket box this reintroduces the t1 [ALARM] =="
fi
[[ -n "${RAYON:-}" ]] && { export RAYON_NUM_THREADS="$RAYON"; echo "   RAYON_NUM_THREADS=$RAYON"; }

# The isolation arm below is skipped when CAPS already contains `default`, so don't promise it here —
# a plan that overstates the work is a plan nobody trusts the second time.
ISO_ARM=1; grep -qx default <<<"$(tr ' ' '\n' <<<"$CAPS")" && ISO_ARM=0
announce_plan "$(( $(wc -w <<<"$STREAMS") * $(wc -w <<<"$CAPS") + ISO_ARM ))" \
  "grid = |STREAMS| x |CAPS|$( [[ "$ISO_ARM" == 1 ]] && echo ' + 1 isolation arm (default capacity)')."
setup_elf_once

# The capacity/streams pair is coupled: the recommendation includes one CU per GPU stream, so `rec`
# is recomputed for each streams value rather than pinned once.
run_arm() {
  local st="$1" capmode="$2" cfg s_eff
  s_eff="$st"; [[ "$st" == auto ]] && s_eff=3     # auto derived 3 on this box; used only for `rec`
  if [[ "$st" == auto ]]; then unset MAX_STREAMS; else export MAX_STREAMS="$st"; fi
  case "$capmode" in
    rec)
      # If the core count cannot be read, COMPUTE_CAPACITY would end up empty, start.sh would omit
      # the flag, and this arm would silently BE the `default` arm — two identical arms wearing
      # different labels. Refuse instead.
      local capv; capv="$(recommended_capacity "$GPUS" "$s_eff")" || {
        echo "ERROR: cannot compute the recommended capacity — pass CAPS with an explicit number, e.g. CAPS=230" >&2
        exit 1; }
      export COMPUTE_CAPACITY="$capv" ;;
    default) unset COMPUTE_CAPACITY ;;
    *)       export COMPUTE_CAPACITY="$capmode" ;;   # CAPS may also carry literal numbers
  esac
  cfg="s${st}-cap${capmode}"
  log_ "arm $cfg (streams=$st capacity=${COMPUTE_CAPACITY:-default10})"
  restart_worker "$cfg" || { log_ "  → skipping arm $cfg"; return; }
  # The program cache lives in the WORKER, so restarting it drops the setup: every prove then fails
  # in ~0.05 s with "setup not done", the arm records a column of rc=1, and the sweep measures
  # nothing. `setup_elf_once` before the loop is not enough — each arm needs its own, after
  # registration. It is idempotent, so re-running it costs one setup per arm and nothing else.
  # ⚠️ THIS DOES NOT WORK AND THE ARM WILL MEASURE NOTHING. `remote setup` is idempotent at the
  # COORDINATOR: it records that this Hash ID is set up and returns in ~2 ms, while the VK lives in the
  # WORKER that restart_worker just replaced. Every prove below then fails in ~0.05 s with rc=1.
  # Measured 2026-08-20: real setup 81 s, this one 2 ms. See infra/monad-witness/RTP-FINDINGS.md.
  # THE FIX, not yet applied: restart the coordinator per arm too, and pay a full setup each time.
  setup_elf_once
  # `--max-streams N` is a CAP, not a setting: the worker derives the real number from free VRAM and
  # can silently land below what we asked. Two arms then carry different labels and identical
  # configs, and the sweep "shows" that the knob does nothing. Compare the effective value.
  if [[ "$st" != auto ]]; then
    local eff; eff="$(cut -d, -f4 < "$OUT/$cfg.meta" 2>/dev/null)"
    [[ "$eff" == "$st" ]] || log_ "  ⚠️  asked --max-streams $st, worker reports ${eff:-?} streams/GPU — this arm is NOT its label (VRAM-capped); read it as a duplicate of s${eff:-?}"
  fi
  run_bench t2-tune "$cfg"
}

for st in $STREAMS; do
  for cap in $CAPS; do run_arm "$st" "$cap"; done
done

# One isolation arm: same streams as the very first, but the DEFAULT capacity. Without it the sweep
# cannot tell a streams effect from a capacity effect.
first_st="$(awk '{print $1}' <<<"$STREAMS")"
[[ "$ISO_ARM" == 1 ]] && run_arm "$first_st" default

restore_default_worker
echo
bash "$TESTS_DIR/summary.sh" "$RESULTS"
echo
echo "== GPU utilisation per config (over the proving window) =="
printf '  %-18s %9s %9s\n' config mean_util p50_util
for f in "$OUT"/*.gpuutil.csv; do
  [[ -f "$f" ]] || continue
  cfg="$(basename "$f" .gpuutil.csv)"
  # sort -n + awk rather than gawk's asort(): Ubuntu ships mawk, which has no asort.
  mean="$(awk -F, '{gsub(/ /,"",$2); if($2~/^[0-9]+$/){s+=$2;n++}} END{if(n) printf "%.1f", s/n}' "$f")"
  p50="$(awk -F, '{gsub(/ /,"",$2); if($2~/^[0-9]+$/) print $2}' "$f" | sort -n | awk '{v[n++]=$1} END{if(n) print v[int(n/2)]}')"
  printf '  %-18s %9s %9s\n' "$cfg" "${mean:--}" "${p50:--}"
done
echo
echo "READ IT LIKE THIS:"
echo "  • capacity rec vs default at the same streams → what the bare 10 CU default was costing."
echo "  • the streams curve should rise then fall; if 3 (auto) is already the peak, that knob is closed."
echo "  • mean util well under ~85% with the best config → the bottleneck is upstream of the GPUs"
echo "    (host bandwidth / witness generation), so go to t3, not further into this sweep."
echo "Results: $OUT"
