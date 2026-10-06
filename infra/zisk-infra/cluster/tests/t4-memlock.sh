#!/usr/bin/env bash
# t4-memlock.sh — is stripping MAP_LOCKED actually free?
#
# Our stack currently disables locked mappings in THREE places at once (globals.c patch +
# `--unlock-mapped-memory` + the nolock.so LD_PRELOAD shim), and nolock.c states the assumption
# plainly: "Unlocking is harmless with hundreds of GB of RAM — the pages simply become swappable
# (and never actually swap)." That has never been measured, and there is a reason to doubt it:
# MAP_LOCKED also PRE-FAULTS the mapping. Without it, multi-GB regions are demand-paged on first
# touch and less likely to get transparent huge pages — which costs page-fault time and TLB misses,
# not swap.
#
# The observable: `Assembly execution speed` in worker.log. We measure 719-809 MHz; ZisK advertises
# 1.5 GHz for the same asm trace execution. That ~1.9× shortfall is the thing to explain, and this
# test decides whether memlock is part of it.
#
#   bash tests/t4-memlock.sh
#
# Env: GPUS=8 · PASSES=2 · FORCE_LOCKED=1 (attempt the locked arm even if the probe says no)
#
# ⚠️ Needs a PRIVILEGED or bare-metal box for the locked arm. On the vast.ai container memlock is
# hard-capped at 64 KB with no CAP_SYS_RESOURCE, so the locked arm cannot even start — the script
# detects this, says so, and still reports the unlocked baseline against the 1.5 GHz reference.
#
# ⚠️ The locked arm REVERTS the globals.c patch and purges the ROM asm cache, so it pays a full
# `remote setup` rebuild (several minutes). Everything is restored on exit, including on Ctrl-C.
set -uo pipefail
source "$(dirname "$0")/lib.sh"

GPUS="${GPUS:-8}"
GLOBALS="$HOME/.zisk/zisk/emulator-asm/src/globals.c"
SHIM="$HOME/nolock.so"
SHIM_OFF="$HOME/nolock.so.t4off"
ASM_MHZ_REFERENCE=1500     # ZisK's published figure for asm trace execution

out_init t4-memlock
preflight || exit 1

# ── restore everything, whatever happens ──────────────────────────────────────────────────────
RESTORE_SHIM=0; RESTORE_PATCH=0
cleanup() {
  local rc=$?
  lib_cleanup_body          # GPU sampler + the "left on a test config" warning
  [[ "$RESTORE_SHIM"  == 1 && -f "$SHIM_OFF" ]] && { mv -f "$SHIM_OFF" "$SHIM"; log_ "restored $SHIM"; }
  if [[ "$RESTORE_PATCH" == 1 ]]; then
    log_ "re-applying the memlock patch (globals.c) + purging the ROM cache"
    bash "$CLUSTER_DIR/fix-memlock-patch.sh" >"$OUT/repatch.log" 2>&1 \
      && log_ "  ✓ patch re-applied — the next remote setup rebuilds the ROM asm" \
      || log_ "  ✗ RE-PATCH FAILED — see $OUT/repatch.log. The box is left UNPATCHED: run"
    log_ "     bash $CLUSTER_DIR/fix-memlock-patch.sh && bash $CLUSTER_DIR/01-setup-elf.sh $ELF"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── capability probe: can this box lock 6 GB at all? ──────────────────────────────────────────
echo "== memlock capability =="
printf '  ulimit -l (KB)     : %s\n' "$(ulimit -l)"
CAN_LOCK=0
if ( ulimit -l unlimited 2>/dev/null ); then
  echo "  ulimit -l unlimited: ALLOWED"
  CAN_LOCK=1
else
  echo "  ulimit -l unlimited: DENIED (no CAP_SYS_RESOURCE)"
fi
# The decisive check is the real syscall, not the rlimit: mlocktest.c mmaps 6 GB with MAP_LOCKED —
# the exact mapping that fails with errno 11 on this box.
if command -v gcc >/dev/null 2>&1 && [[ -f "$CLUSTER_DIR/mlocktest.c" ]]; then
  gcc -O2 -o "$OUT/mlocktest" "$CLUSTER_DIR/mlocktest.c" 2>/dev/null && {
    echo "  mlocktest, no shim : $( ( ulimit -l unlimited 2>/dev/null; "$OUT/mlocktest" ) 2>&1 | tail -1)"
    [[ -f "$SHIM" ]] && echo "  mlocktest, w/ shim : $(LD_PRELOAD="$SHIM" "$OUT/mlocktest" 2>&1 | tail -1)"
    ( ulimit -l unlimited 2>/dev/null; "$OUT/mlocktest" ) >/dev/null 2>&1 && CAN_LOCK=1 || CAN_LOCK=0
  }
fi
printf '  THP                : %s\n' "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo '?')"
[[ "${FORCE_LOCKED:-}" == 1 ]] && { echo "  FORCE_LOCKED=1 → attempting the locked arm regardless"; CAN_LOCK=1; }

DEVS="$(numa_set "$GPUS" local)" || { echo "ERROR: cannot pick $GPUS NUMA-local GPUs" >&2; exit 1; }
export CUDA_VISIBLE_DEVICES="$DEVS"
echo "== pinned to $GPUS NUMA-local GPUs: $DEVS (t1's finding held constant) =="
announce_plan "$(( CAN_LOCK == 1 ? 2 : 1 ))" "the locked arm also pays a ROM asm rebuild (several minutes)."
setup_elf_once

# ── arm A: unlocked — exactly what we run in production today ──────────────────────────────────
unset ZISK_LOCK_MEM
restart_worker unlocked && run_bench t4-memlock unlocked || log_ "  → unlocked arm failed"

# ── arm B: locked — requires reverting all three unlock mechanisms ──────────────────────────────
if [[ "$CAN_LOCK" == 1 ]]; then
  echo
  echo "== arm 'locked': reverting the memlock workaround (three places, all of them) =="
  # RESTORE_PATCH is flagged BEFORE the mutation, not after: a kill in between would otherwise leave
  # globals.c reverted with nothing scheduled to put the patch back, and the next proof would fail on
  # mmap errno 11.
  REVERTED=0
  if [[ -f "$GLOBALS.orig" ]]; then
    RESTORE_PATCH=1; REVERTED=1
    cp -f "$GLOBALS.orig" "$GLOBALS"
  elif grep -q '^int map_locked_flag = 0;' "$GLOBALS" 2>/dev/null; then
    # Patched, but with no backup beside it — an absent .orig is NOT evidence that the file is
    # pristine. Reading it that way would leave the C default unlocked and run this arm on the very
    # config `unlocked` already measured, under a different label, and "both arms equal" would then
    # exonerate memlock on a comparison that never happened. Rebuild the upstream line instead.
    RESTORE_PATCH=1; REVERTED=1
    sed -i 's|^int map_locked_flag = 0;.*$|int map_locked_flag = MAP_LOCKED;|' "$GLOBALS"
  else
    echo "  NOTE: globals.c already carries the upstream MAP_LOCKED default — nothing to revert."
  fi
  if [[ "$REVERTED" == 1 ]]; then
    grep -n 'map_locked_flag *=' "$GLOBALS" | sed 's/^/  globals.c: /'
    # The compiled microservices carry the patched default, so the cache must go or the revert is
    # cosmetic and the arm silently measures the unlocked path again.
    rm -f "$HOME"/.zisk/cache/*-hints-{mo,mt,rh}.{asm,bin} 2>/dev/null || true
    rm -f /dev/shm/ZISK* /dev/shm/*zisk* 2>/dev/null || true
    echo "  purged the ROM asm cache → remote setup will rebuild (several minutes)"
  fi
  [[ -f "$SHIM" ]] && { RESTORE_SHIM=1; mv -f "$SHIM" "$SHIM_OFF"; echo "  moved nolock.so aside (start.sh auto-preloads it when present)"; }
  export ZISK_LOCK_MEM=1        # start.sh then omits --unlock-mapped-memory
  ulimit -l unlimited 2>/dev/null || true

  log_ "rebuilding the ROM asm with locked mappings"
  setup_elf_once
  if restart_worker locked; then
    run_bench t4-memlock locked
  else
    echo
    echo "  ✗ the locked arm did not come up. If $OUT/locked.worker-startup.log shows"
    echo "    'mmap(rom) errno=11' or 'Shmem creation for mo failed', that IS the answer: this box"
    echo "    cannot run locked mappings, and the unlocked config is not a choice but a constraint."
  fi
else
  echo
  echo "== arm 'locked': SKIPPED — this box cannot lock a 6 GB mapping =="
  echo "   memlock is hard-capped and CAP_SYS_RESOURCE is absent (unprivileged container)."
  echo "   To run this arm: a privileged container (--privileged or --cap-add=SYS_RESOURCE"
  echo "   --ulimit memlock=-1) or a bare-metal host. Re-run there with the same GPUS."
fi

restore_default_worker
echo
bash "$TESTS_DIR/summary.sh" "$RESULTS"
echo
echo "== asm trace execution vs ZisK's published figure =="
awk -F, -v ref="$ASM_MHZ_REFERENCE" 'NR>1 && $11!="" {s[$2]+=$11; n[$2]++}
  END{ printf "  %-12s %10s %10s %8s\n","config","asm_MHz","reference","ratio";
       for(c in s){ m=s[c]/n[c]; printf "  %-12s %10.0f %10d %7.2fx\n", c, m, ref, ref/m } }' "$RESULTS"
echo
echo "READ IT LIKE THIS:"
echo "  • locked clearly above unlocked on asm_MHz → the nolock.c comment is wrong and the unlock is"
echo "    NOT free; the fix is a box that allows memlock, not a config change."
echo "  • both arms ≈ equal → memlock is exonerated. The remaining suspects for the ~1.9x asm"
echo "    shortfall are CPU clock (EPYC 9654 is a low-clock part) and shared-host contention;"
echo "    neither is fixable by tuning, both are fixable by box choice."
echo "  • only the unlocked arm ran → you still learn the baseline ratio above. That number alone"
echo "    is worth recording in AGENT-NOTES.md."
echo "Results: $OUT"
