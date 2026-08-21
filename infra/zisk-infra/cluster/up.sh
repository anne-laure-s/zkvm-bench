#!/usr/bin/env bash
# up.sh — bring the cluster to a usable state from WHATEVER state the box is in, and do not return
# until a worker is actually registered. Safe to run every time; it decides what is missing.
#
# It exists because every failure mode below cost a run at least once:
#
#   * `00-install-once.sh` exits 0 on an incomplete key — its const-tree guard is only in the RAM-key
#     branch. The key then sits at its extraction size and every `remote setup` fails with
#     "all workers failed setup, no VK received", tens of minutes later. This script does not guess
#     from the key's size: that figure is version-dependent (~30 GB extracted on 1.0.0-alpha, 54 GB
#     complete on 1.1.0-alpha) and is a correlate, not the fact. It asks `check-setup`, which is the
#     authoritative check and generates only what is missing.
#   * `stop.sh` kills by pid file, so a process whose pid file was lost (a second start.sh, a vast.ai
#     stop/start) survives invisibly. The stale coordinator then holds port 7000
#     ("Address already in use") and the stale worker holds the card, so the new worker finds
#     "0.78 GB free / 31.36 GB total" and dies on "Fixed polynomials need 4.54 GB".
#   * Logs are never rotated, so a registration line from a previous session reads as this one's. Three
#     hours were spent on paniques from two days earlier, read as current.
#   * VRAM at ~30 GB with no registration is a ZOMBIE, not a worker starting up. That reading was taken
#     as health repeatedly.
#   * `cmd | tee f && next` tests tee's exit code, which is always 0, so `next` runs on a failed cmd.
#
# So: no `sleep` standing in for a check. Every wait is a loop on the condition itself, with a deadline
# and, on timeout, the log that explains it.
#
#   bash up.sh                 # detect and do what is needed — including a 15-60 min install when
#                              # the binaries or the key are missing (box only; it refuses elsewhere)
#   FORCE_RESTART=1 bash up.sh # tear the cluster down first, whatever its state
#   SKIP_PROBE=1 bash up.sh    # skip the closing box record. It only READS (nvidia-smi, lscpu, df)
#                              # and takes well under a second; it measures no d2h itself, it prints
#                              # the one command that does.
#
# Env: MIN_KEY_GB=5 (absent-extraction floor only) · REG_TIMEOUT=600 · VRAM_FLOOR_MIB=15000 · API_PORT=7000
#      SKIP_TOPO=1 to skip the pre-install d2h gate · FORCE_INSTALL=1 to install past a bad verdict
#      (the gate only runs when an install is needed, i.e. on a fresh box)
set -uo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.zisk/bin:$PATH"

# A floor for "the extraction never happened", not for "the const-trees are missing" —
# no real key of any version falls under this. Completeness is check-setup's job, below.
MIN_KEY_GB="${MIN_KEY_GB:-5}"
REG_TIMEOUT="${REG_TIMEOUT:-600}"
VRAM_FLOOR_MIB="${VRAM_FLOOR_MIB:-15000}"
# Exported, not merely assigned: start.sh defaults to 7000 independently, so without this a changed
# port here would leave this script watching one port while the coordinator binds another.
export API_PORT="${API_PORT:-7000}"
KEY="$HOME/.zisk/provingKey"
COORD_LOG="logs/coordinator.log"
WORKER_LOG="logs/worker.log"

say()  { printf '\033[1m==\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

# ── probes, each answering exactly one question ───────────────────────────────────────────────────

# A live daemon of ours, whatever the port and the card say. Bracketed so the pattern cannot match
# the pgrep/pkill running it. This is the third leg of the teardown decision: a worker that is alive
# but has not yet allocated leaves the port free AND the VRAM low, and starting a second one on top of
# it is the exact failure that grafting a hand-started worker onto a live coordinator produced.
procs_up() { pgrep -f '[z]isk-(coordinator|worker)' >/dev/null 2>&1; }
vram_mib() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
             | awk '{s+=$1} END{print s+0}'; }
key_gb()   { local m; m="$(du -sm "$KEY" 2>/dev/null | cut -f1)"; echo $(( ${m:-0} / 1024 )); }
port_held() { # $1 = port. ss, then lsof, then a bare TCP connect — containers vary.
  local out                       # captured, not piped: pipefail would fail the test on ss's own rc
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -lnt 2>/dev/null || true)"; case "$out" in *":$1 "*) return 0 ;; esac
  fi
  if command -v lsof >/dev/null 2>&1; then lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && return 0; fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; }
  return 1
}
# Every probe answers in 0/1 so the tests read alike; these two turn one into the word the operator
# reads, so no message ever shows a bare digit for a state that has a name.
port_word() { [ "$1" = 1 ] && echo held || echo free; }
yes_no()    { [ "$1" = 1 ] && echo yes  || echo no; }
# Has the box let go of everything? The three legs at once, so the teardown can wait on one answer.
released() { [ "$(vram_mib)" -lt "$VRAM_FLOOR_MIB" ] && ! port_held "$API_PORT" && ! procs_up; }
wait_released() { local i; for i in $(seq 1 "$1"); do released && return 0; sleep 1; done; released; }
# The worker's own summary of what it is driving — read in two places, so extracted once.
streams_line() { grep -a 'streams per GPU' "$WORKER_LOG" 2>/dev/null | tail -1 | sed 's/.*INFO: //'; }
# DISTINCT workers registered since the log was rotated — not matching lines. One worker emits BOTH
# wordings, 130 µs apart, on the same version (measured, 1.1.0-alpha):
#   INFO: Registered worker: WorkerId(b613a72c…) (total: 1 CC: 10CU ACC: 0CU)
#   INFO: WorkerId(b613a72c…) registered successfully
# so a line count reports 2 for a single worker and 2N for N. Match either wording — -i is
# load-bearing for that capital R — then count the ids they carry. The fallback keeps the answer
# usable if a build ever logs a registration with no id: `grep -c` prints 0 AND exits 1 on no match,
# so `|| echo 0` would append a second 0 and every arithmetic test would then die with "integer
# expression expected" — on the main path, a fresh log with no registration yet.
reg_count() { local n
  n="$(grep -aiE "registered (worker|successfully)" "$COORD_LOG" 2>/dev/null \
       | grep -aoiE "workerid\([^)]*\)" | sort -u | wc -l | tr -d ' ')"
  [ "${n:-0}" -gt 0 ] 2>/dev/null \
    || n="$(grep -aciE "registered (worker|successfully)" "$COORD_LOG" 2>/dev/null)"
  echo "${n:-0}"; }

# ── 1. what state is this box in ──────────────────────────────────────────────────────────────────

say "state"
HAVE_ZISKUP=0; command -v cargo-zisk >/dev/null 2>&1 && HAVE_ZISKUP=1
KGB="$(key_gb)"; VRAM="$(vram_mib)"; REG="$(reg_count)"
COORD_UP=0; port_held "$API_PORT" && COORD_UP=1
PROCS=0; procs_up && PROCS=1
printf '   ziskup:%s  key:%s GB  port %s:%s  daemons:%s  VRAM:%s MiB  registrations:%s\n' \
  "$(yes_no "$HAVE_ZISKUP")" "$KGB" "$API_PORT" \
  "$(port_word "$COORD_UP")" "$(yes_no "$PROCS")" "$VRAM" "$REG"

# Healthy means port, registration and card all agree. Any two out of three is a broken state, not a
# starting one — the case that matters being VRAM held with no registration, which is a zombie. The
# daemon probe is deliberately NOT a fourth leg: if those three hold, the processes are alive by
# construction, and it only earns its place in the teardown decision below, where they can disagree.
# The cost of being wrong (a genuinely-up cluster whose log was rotated by something else) is one
# restart, ~2 min.
HEALTHY=0
[ "$COORD_UP" = 1 ] && [ "$REG" -ge 1 ] && [ "$VRAM" -ge "$VRAM_FLOOR_MIB" ] && HEALTHY=1

if [ "$HEALTHY" = 1 ] && [ "${FORCE_RESTART:-0}" != 1 ]; then
  say "cluster already healthy — nothing to do"
  echo "   worker       $(streams_line || true)"
  exit 0
fi

# ── 2. install, if there is nothing to start or nothing to prove with ─────────────────────────────

# The key counts as much as the binaries: a RAM key does not survive a reboot, which leaves
# ~/.zisk/provingKey a dangling symlink (so -d is false and du reports nothing) on a box that still
# has cargo-zisk. 00-install-once.sh is what re-fetches it — it guards rustup and re-runs ziskup — so
# let it run rather than dying with advice to run the very script this one exists to drive.
NEED_INSTALL=0; WHY=""
[ "$HAVE_ZISKUP" = 1 ] || { NEED_INSTALL=1; WHY="no cargo-zisk"; }
# Exclusive on purpose: an absent key also measures 0 GB, and reporting both reads as two faults.
if [ ! -d "$KEY" ]; then
  NEED_INSTALL=1; WHY="${WHY:+$WHY, }no key at $KEY"
elif [ "$KGB" -lt "$MIN_KEY_GB" ]; then
  NEED_INSTALL=1; WHY="${WHY:+$WHY, }key only ${KGB} GB"
fi
if [ "$NEED_INSTALL" = 1 ]; then
  # The one branch that changes the machine, so it refuses to run anywhere but a box:
  # 00-install-once.sh carries no platform guard of its own — it apt-installs, curls rustup and
  # writes ~/.zisk — and this script is also run on the Mac, read-only, to look at a cluster.
  [ "$(uname -s)" = Linux ] || die "$WHY — and installing is a box-only action ($(uname -s) here). \
Everything above this point is read-only; run 00-install-once.sh on the box itself."
  command -v nvidia-smi >/dev/null 2>&1 || die "$WHY — and no nvidia-smi. This installs the GPU \
prover and hard-fails on a non-[gpu] build, so it will not run on a box without a card."
  # A RAM key is a symlink into tmpfs, and tmpfs does not survive an instance stop — the very state
  # step 2 now installs for. But 00-install-once.sh takes its RAM path only when ZISK_KEY_DIR is set,
  # and its disk path then fails the pre-flight on the small disk such a box has BY CONSTRUCTION
  # (that is why the key was in RAM). The dead symlink still names its target, so recover the mode
  # from it instead of silently reinstalling into a disk that cannot hold the key.
  if [ -z "${ZISK_KEY_DIR:-}" ] && [ -L "$KEY" ]; then
    export ZISK_KEY_DIR="$(dirname "$(readlink "$KEY")")"
    say "RAM-key box: recovered ZISK_KEY_DIR=$ZISK_KEY_DIR from the key symlink"
  fi
  # ── the d2h gate, before spending an hour ────────────────────────────────────────────────────
  # Nothing in a listing shows d2h. Two hosts identical on every advertised field — same GPU model,
  # same gen5 x16 MAX, same driver — differed 2.37× on it (24.07 against 56.97 GB/s) while h2d differed
  # only 1.11×, and the slow one took 2.82× longer on the largest block of the set. A fresh box is the
  # one moment this is measurable without contending with a proof, and it costs 2 min against 15-60.
  # The test is the RATIO as much as the level: the failure mode is directional, so a d2h far below its
  # own h2d is the signature. Thresholds come from two boxes only — 24.07 bad, 56.97 good — so they are
  # a screen, not a law.
  if [ "${SKIP_TOPO:-0}" = 1 ]; then
    warn "SKIP_TOPO=1 — installing without measuring d2h, the one figure that has rejected a box."
  elif [ ! -f tests/t3-topo.sh ]; then
    warn "tests/t3-topo.sh is not here, so the d2h gate cannot run. Ship cluster/tests/ and re-run,"
    warn "or accept installing blind on the figure that separated a usable box from an unusable one."
  fi
  if [ "${SKIP_TOPO:-0}" != 1 ] && [ -f tests/t3-topo.sh ]; then
    say "measuring d2h before installing (~2 min; nothing is running yet, so BW=1 is safe)"
    TOPO="$HOME/tests/t3-topo-up-$(date -u +%Y%m%d-%H%M%SZ)"
    OUT="$TOPO" BW=1 bash tests/t3-topo.sh > "$HOME/topo.log" 2>&1 \
      || warn "t3-topo exited non-zero — read ~/topo.log"
    BWCSV="$TOPO/bandwidth.csv"
    if [ ! -s "$BWCSV" ]; then
      warn "no bandwidth.csv — the CUDA probe did not run (nvcc absent?). Read ~/topo.log."
      warn "Proceeding blind on the one figure that decided a box. Not a reason to stop, but know it."
    else
      # t3-topo writes TWO rows per GPU — `serial` and `concurrent` — so "the minimum d2h" silently
      # judged on serial for one box and concurrent for another. Reduce per MODE, print both, and let
      # the worse one decide, naming it: serial isolates one link, concurrent exposes a shared uplink,
      # and a gate must say which number it acted on. Within each mode, the worst GPU wins — one
      # starved link sets the pace for the whole box.
      BWSUM="$(awk -F, 'NR>1 && $2=="ok" {
            m=$5; gsub(/[ \t\r]/,"",m)
            if (!(m in md) || $4+0 < md[m]) { md[m]=$4+0; mh[m]=$3+0 }
          }
          END {
            for (m in md) {
              printf "MODE %s %.2f %.2f\n", m, mh[m], md[m]
              if (w == "" || md[m] < md[w]) w=m
            }
            if (w != "") printf "WORST %s %.2f %.2f\n", w, mh[w], md[w]
          }' "$BWCSV")"
      printf '%s\n' "$BWSUM" | awk '$1=="MODE"{printf "   %-11s h2d %7s   d2h %7s\n", $2, $3, $4}'
      WORST="$(printf '%s\n' "$BWSUM" | awk '$1=="WORST"{print $3" "$4" "$2}')"
      H="${WORST%% *}"; WREST="${WORST#* }"; D="${WREST%% *}"; MODE="${WREST#* }"
      [ -n "${H:-}" ] || { H=0; D=0; MODE="?"; }
      VERDICT="$(awk -v h="$H" -v d="$D" 'BEGIN {
        if (h <= 0 || d <= 0) { print "unknown"; exit }
        r = d / h
        if (d < 40 || r < 0.70)      print "bad"
        else if (d < 48 || r < 0.85) print "marginal"
        else                          print "good" }')"
      RATIO="$(awk -v h="$H" -v d="$D" 'BEGIN { if (h>0) printf "%.2f", d/h; else printf "?" }')"
      case "$VERDICT" in
        good)     say "worst mode ${MODE}: d2h ${D} GB/s, h2d ${H}, ratio ${RATIO} — healthy, this box is fed" ;;
        marginal) warn "worst mode ${MODE}: d2h ${D} GB/s, h2d ${H}, ratio ${RATIO} — below the healthy box (56.97, 1.02)"
                  warn "not the starved case either. Installing; keep this figure with any rate measured here." ;;
        unknown)  warn "bandwidth.csv holds no usable row — read ~/topo.log. Proceeding blind." ;;
        bad)
          warn "THIS IS THE STARVED CASE (mode ${MODE}): d2h ${D} GB/s against h2d ${H} — ratio ${RATIO}."
          warn "The deficit is directional, which is why no listing and no PCIe generation shows it."
          warn "Measured consequence on such a box: the card computes a third of the time, and large"
          warn "blocks take up to 2.82x longer. No tuning recovers it — the reference box read 56.97."
          if [ "${FORCE_INSTALL:-0}" = 1 ]; then
            warn "FORCE_INSTALL=1 — installing anyway, on your call."
          elif [ -t 0 ]; then
            printf '\033[33m??\033[0m Install anyway? [y/N] ' >&2
            read -r ANS
            case "$ANS" in
              [yY]*) say "proceeding on your call — record the d2h beside every number from this box" ;;
              *)     die "stopped before the install. Drop this box and rent another; \
you have spent 2 minutes, not an hour." ;;
            esac
          else
            die "no terminal to ask on. Re-run with FORCE_INSTALL=1 to install anyway, or drop the box."
          fi ;;
      esac
    fi
  fi

  say "$WHY — running 00-install-once.sh (15-60 min)"
  # pipefail is set, so a failed install does NOT fall through to the next step.
  ./00-install-once.sh 2>&1 | tee "$HOME/install.log" \
    || die "install failed — read ~/install.log"
  command -v cargo-zisk >/dev/null 2>&1 || die "install claimed success but cargo-zisk is absent"
  KGB="$(key_gb)"
fi

# ── 3. make the key complete, by asking rather than measuring ─────────────────────────────────────
# `check-setup` IS the completeness check — that is what it is for, and it is idempotent: it verifies
# and generates only what is missing. Running it unconditionally is more robust than any size
# threshold, which would be version-dependent and a proxy for the wrong question. Its elapsed time
# tells the operator whether it actually had work to do.

# Step 2 installs whenever either of these is false, so reaching a failure here means the install
# ran and did not deliver — never that nobody asked for one.
[ -d "$KEY" ] || die "still no proving key at $KEY after the install — read ~/install.log"
if [ "$KGB" -lt "$MIN_KEY_GB" ]; then
  die "provingKey is only ${KGB} GB after the install — the extraction did not happen. Read \
~/install.log; check-setup cannot rebuild a key that was never downloaded."
fi

# AGGREGATION IS ON, deliberately — do not add `-a` back.
# `-a` is `--no-aggregation`, and proofman's check_setup builds the compressor/recursion fixed trees
# ONLY when aggregation is true (pil2-proofman proofman.rs:546 — the basic AIRs are built either way,
# then `if aggregation { … calculate_fixed_tree(sctx_compressor …) }`). We DO aggregate: the STARK
# reduction tree is what gives a proof its fixed size. Passing `-a` therefore skipped exactly the
# trees the prover needs, leaving them to be built per ELF by the first `remote setup` — 81 s
# measured, against 2 ms once cached.
# PLONK/Groth16 stay out of scope (no on-chain proof), so -s/--plonk is deliberately absent.
# `--gpu` does exist as -g, but under #[cfg(not(feature = "cpu-only"))]: it is compiled out of a CPU
# build and missing from --help there, so pass it only when the binary on THIS box advertises it.
# check-setup is a cargo-zisk-DEV subcommand — plain cargo-zisk answers "unrecognized subcommand",
# so there is no fallback to offer.
CHECK="$(command -v cargo-zisk-dev || true)"
[ -n "$CHECK" ] || die "cargo-zisk-dev is absent — check-setup lives there, not on cargo-zisk. \
ziskup installs both; a partial install is the usual cause."
# Read --help into a variable rather than testing the pipeline: pipefail makes
# `--help | grep -q` non-zero whenever --help itself exits non-zero, which would silently drop --gpu
# on a box that has it and leave check-setup building CPU const-trees the prover cannot use.
CHECK_GPU=()
CHECK_HELP="$("$CHECK" check-setup --help 2>&1 || true)"
case "$CHECK_HELP" in *--gpu*) CHECK_GPU=(--gpu) ;; esac

say "verifying the key (check-setup — generates const trees only if they are missing)"
KGB_BEFORE="$KGB"; T0=$(date +%s)
"$CHECK" check-setup --proving-key "$KEY" ${CHECK_GPU[@]+"${CHECK_GPU[@]}"} > "$HOME/check-setup.log" 2>&1 \
  || { warn "check-setup failed:"; tail -12 "$HOME/check-setup.log" | sed 's/^/     /' >&2
       die "the key is unusable — read ~/check-setup.log"; }
ELAPSED=$(( $(date +%s) - T0 ))
KGB="$(key_gb)"
# Judge on the fact, not on elapsed time: a slow box can spend a while merely verifying, and would
# then be reported as having repaired something.
if [ "$KGB" -gt "$KGB_BEFORE" ]; then
  say "const trees written in ${ELAPSED}s — the key grew ${KGB_BEFORE} -> ${KGB} GB"
  warn "it had been left incomplete by an install that still exited 0; that is the gap this closes."
else
  say "key already complete (${KGB} GB, verified in ${ELAPSED}s)"
fi

# ── 4. tear down whatever is alive, or holding the port or the card ───────────────────────────────
# By process, port and card — the three independent ways to observe a live cluster — because
# `stop.sh` alone cannot be trusted (see the pid-file failure mode above). Bracketed patterns, or
# pkill -f matches the shell running it and kills this script.

# Re-probe: the values from step 1 can be 40 minutes old if an install ran, and a neighbour or a
# crash may have changed the picture since.
VRAM="$(vram_mib)"; port_held "$API_PORT" && COORD_UP=1 || COORD_UP=0
PROCS=0; procs_up && PROCS=1
if [ "$COORD_UP" = 1 ] || [ "$PROCS" = 1 ] || [ "$VRAM" -ge "$VRAM_FLOOR_MIB" ] \
   || [ "${FORCE_RESTART:-0}" = 1 ]; then
  say "tearing down (port:$(port_word "$COORD_UP") daemons:$(yes_no "$PROCS") \
VRAM:${VRAM} MiB forced:${FORCE_RESTART:-0})"
  # The polite path first — though stop.sh deletes its pid files even when the kill fails, so it gets
  # exactly one chance and never a second.
  ./stop.sh >/dev/null 2>&1 || true
  pkill -f '[z]isk-coordinator' 2>/dev/null || true
  pkill -f '[z]isk-worker'      2>/dev/null || true
  # A kill is a request, not a fact. Nothing above sends anything harder than SIGTERM — stop.sh uses a
  # bare `kill`, and so do these — and a worker inside a CUDA call can take seconds to leave. So wait,
  # then escalate once, rather than reporting a box we could have freed. SIGKILL against processes
  # that are already gone matches nothing and costs nothing.
  if ! wait_released 15; then
    warn "SIGTERM did not free the box after 15 s — escalating to SIGKILL"
    pkill -9 -f '[z]isk-coordinator' 2>/dev/null || true
    pkill -9 -f '[z]isk-worker'      2>/dev/null || true
    wait_released 15 || true
  fi
  VRAM="$(vram_mib)"; port_held "$API_PORT" && COORD_UP=1 || COORD_UP=0
  [ "$VRAM" -lt "$VRAM_FLOOR_MIB" ] || warn "VRAM still ${VRAM} MiB after teardown — another tenant?"
  [ "$COORD_UP" = 0 ] || die "port $API_PORT still held after teardown — a process outside this tree owns it"
  SURVIVORS="$(pgrep -f '[z]isk-(coordinator|worker)' 2>/dev/null | tr '\n' ' ' || true)"
  SURVIVORS="${SURVIVORS% }"
  [ -z "$SURVIVORS" ] || die "a zisk daemon survived pkill (pid $SURVIVORS) — starting a second one \
on top of it is what this refuses to do"
  say "down (VRAM ${VRAM} MiB, port $(port_word "$COORD_UP"), no daemons)"
fi

# ── 5. rotate the logs, then start ────────────────────────────────────────────────────────────────
# Rotation is not tidiness: without it a registration line from a previous session satisfies the wait
# below, and the script returns on a cluster that is not up.

mkdir -p logs
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
for f in "$COORD_LOG" "$WORKER_LOG"; do
  [ -s "$f" ] && mv "$f" "${f%.log}.$STAMP.log"
done
say "logs rotated to *.$STAMP.log"

say "starting cluster"
# setsid so the daemons outlive the ssh session that launched this, and redirected so the session
# can close at all — a backgrounded child holding the pipe keeps ssh from returning.
# start.sh backgrounds both daemons and returns 0 within ~15 s, so its exit is NOT the signal — only a
# NON-ZERO exit is (binary not on PATH, bad MPI args). Record that status in a file: by the first poll
# the process is gone either way, and whether `wait` still reports a reaped child's status differs
# between bash 3.2 (it does) and 5.x (127, "not a child of this shell"). A file is the same fact on
# every version. setsid is not in every minimal container, hence conditional.
rm -f "$HOME/start.rc"
SETSID=(); command -v setsid >/dev/null 2>&1 && SETSID=(setsid)
${SETSID[@]+"${SETSID[@]}"} bash -c \
  "./start.sh > '$HOME/start.out' 2>&1; echo \$? > '$HOME/start.rc'" < /dev/null &

# ── 6. wait on the CONDITION, with a deadline ─────────────────────────────────────────────────────

say "waiting for a worker to register (timeout ${REG_TIMEOUT}s)"
DEADLINE=$(( $(date +%s) + REG_TIMEOUT ))
LAST=""
while :; do
  REG="$(reg_count)"; VRAM="$(vram_mib)"; port_held "$API_PORT" && COORD_UP=1 || COORD_UP=0
  [ "$COORD_UP" = 1 ] && [ "$REG" -ge 1 ] && [ "$VRAM" -ge "$VRAM_FLOOR_MIB" ] && break

  # start.sh failing outright would otherwise sit out the whole deadline. A non-empty start.rc also
  # means the launch is DONE, which the death check below depends on.
  START_RC="$(cat "$HOME/start.rc" 2>/dev/null || true)"
  if [ -n "$START_RC" ] && [ "$START_RC" != 0 ]; then
    warn "start.sh exited $START_RC — ~/start.out:"; tail -12 "$HOME/start.out" | sed 's/^/     /' >&2
    die "the cluster was never launched"
  fi

  # These two name the failure precisely when they match, but NOTHING may depend on them matching:
  # neither wording appears in any log or document in this repo, only here, so they are what was read
  # off a screen once. The wording-independent check follows them.
  if grep -qa "Fixed polynomials need" "$WORKER_LOG" 2>/dev/null; then
    warn "worker refused to start — the card had no room:"
    grep -a "Fixed polynomials need\|GB free /" "$WORKER_LOG" | tail -3 | sed 's/^/     /' >&2
    die "step 4 left this card free, so the VRAM is held from outside this tree — another tenant, \
or a card too small for this key. FORCE_RESTART cannot reach it."
  fi
  if grep -qa "Address already in use" "$COORD_LOG" 2>/dev/null; then
    die "coordinator could not bind port $API_PORT, which step 4 left free — something outside this \
tree took it in between. Pick another with API_PORT=<n>."
  fi

  # The check that needs no wording: once start.sh has returned, the worker HAS been launched, so a
  # missing process means it died — whatever it printed on the way out, and including every failure
  # the two greps above do not know about. This is the one that must not be removed.
  if [ -n "$START_RC" ] && ! pgrep -f '[z]isk-worker' >/dev/null 2>&1; then
    warn "no zisk-worker process is alive and start.sh has already returned. Worker log tail:"
    tail -20 "$WORKER_LOG" 2>/dev/null | sed 's/^/     /' >&2
    die "the worker died during startup — the tail above is the whole story"
  fi
  # Coarsened to GiB on purpose: at MiB granularity the value changes on every poll while the card
  # fills, so the "print only on change" below would print 300 lines instead of ~30.
  NOW="port:$(port_word "$COORD_UP") registrations:$REG VRAM:$(( VRAM / 1024 )) GiB"
  [ "$NOW" != "$LAST" ] && { printf '   %s\n' "$NOW"; LAST="$NOW"; }
  [ "$(date +%s)" -ge "$DEADLINE" ] && {
    warn "still not registered after ${REG_TIMEOUT}s. Worker log tail:"
    tail -20 "$WORKER_LOG" 2>/dev/null | sed 's/^/     /' >&2
    die "giving up"
  }
  sleep 2                                  # polling interval, not a substitute for the check
done
say "registered — VRAM ${VRAM} MiB, $REG registration(s)"

# ── 7. record what this box is, because a rate is a property of the host ──────────────────────────
# A box measured 24.07 GB/s d2h against another's 56.97 while their h2d differed by only 1.11×, and on
# the same seven blocks it took 1.21× longer on the smallest and 2.82× on the largest — the penalty
# grows with block size. Neither figure shows on a listing, and neither was captured on the box that
# produced the anomalous number. The enforced power limit is worth recording for the same reason, but
# on both boxes measured it was never reached, so record it and do not price it.

if [ "${SKIP_PROBE:-0}" != 1 ]; then
  say "box (keep this with any rate measured here)"
  # .current as well as .max: the two boxes that differed 2.37× on d2h had IDENTICAL max (gen5 x16),
  # so the max discriminates nothing. Note .current reads low at idle (ASPM) — it is only meaningful
  # under load, which is why the negotiated link is still an open question in RTP-FINDINGS.md.
  nvidia-smi --query-gpu=name,driver_version,memory.total,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current \
    --format=csv,noheader 2>/dev/null | sed 's/^/   gpu          /'
  # Stop at "Module Power Readings": it repeats every label with N/A and, being last, overwrote both
  # values — this printed "power limit N/A (default N/A)" on driver 580 until it was caught.
  nvidia-smi -q -d POWER 2>/dev/null \
    | awk -F: '/Module Power Readings/{skip=1} skip{next}
        /Current Power Limit/{c=$2} /Default Power Limit/{d=$2}
        END{gsub(/ /,"",c);gsub(/ /,"",d);
        if(c!="")printf "   power limit  %s (default %s)%s\n", c, d, (c!=d?"  <- BELOW DEFAULT":"")}'
  echo "   cpu          $(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null | sed 's/^ //')  ($(nproc 2>/dev/null) alloc)"
  echo "   L3           $(lscpu 2>/dev/null | awk -F: '/L3 cache/{gsub(/^ +/,"",$2); print $2}')"
  echo "   RAM          $(awk '/MemTotal/{printf "%.0f GB", $2/1048576}' /proc/meminfo 2>/dev/null)"
  echo "   key          ${KGB} GB   disk $(df -h / 2>/dev/null | awk 'NR==2{print $4" free"}')"
  echo "   cargo-zisk   $(cargo-zisk --version 2>/dev/null)"
  # Kept as a row even when absent: a vanished line reads as a probe that was never run.
  STREAMS="$(streams_line || true)"
  echo "   streams      ${STREAMS:-<not logged yet>}"
  warn "d2h is the one figure a listing cannot show, and it decided a box:"
  warn "  BW=0 bash tests/t3-topo.sh   # topology only — the cluster is UP now, and BW=1 allocates"
  warn "  ~1 GB per GPU and contends with a proof. For d2h use an idle card, or let step 2 measure it"
  warn "  on the next fresh box, where the install is gated on it."
fi

say "cluster up. Per-ELF setup is NOT done by this script:"
echo "   cargo-zisk remote setup -e <elf> [--hints] --coordinator http://127.0.0.1:$API_PORT"
echo "   The cache is keyed on (program, with_hints) under the ELF's Hash ID, so only ONE"
echo "   configuration is live at a time and it lives in the WORKER — anything that restarts the"
echo "   worker drops it, and a later 'idempotent' setup no-ops at the coordinator instead of"
echo "   re-arming it. Re-setup per configuration, after this script returns."
