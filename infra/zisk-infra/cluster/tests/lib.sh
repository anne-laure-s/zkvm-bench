#!/usr/bin/env bash
# lib.sh — shared plumbing for the t1…t5 diagnosis tests. SOURCED, never executed.
#
# Why a lib: the five tests differ only in WHICH worker config they compare. Everything else —
# restart the worker without losing the coordinator's ELF cache, wait for registration, run N
# passes, carve the matching window out of worker.log, emit one canonical CSV row — is identical,
# and getting it subtly different per test is how you end up comparing two things that were not
# measured the same way.
#
# THE CANONICAL ROW (every test appends to results.csv with this exact header):
#   test,config,pass,tag,msteps,wall_secs,exec_ms,contrib_ms,inner_ms,final_ms,
#   asm_mhz,instances,n_gpus,numa_local,numa_total,streams,capacity,rc
# `numa_local/numa_total` come from ZisK's own `[ALARM]` line — that is the metric t1 exists to move.
# Derived numbers (Msteps/s, per-GPU) are NOT stored: summary.sh computes them, so a fix to the
# arithmetic never means re-running a 20-minute benchmark.

# ── paths ─────────────────────────────────────────────────────────────────────────────────────
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_DIR="$(cd "$TESTS_DIR/.." && pwd)"
WLOG="$CLUSTER_DIR/logs/worker.log"
CLOG="$CLUSTER_DIR/logs/coordinator.log"
export PATH="$HOME/.zisk/bin:$PATH"

ELF="${ELF:-$HOME/zisk-reth.elf}"
COORD="${COORD:-http://127.0.0.1:${API_PORT:-7000}}"
PASSES="${PASSES:-2}"
WARMUPS="${WARMUPS:-1}"
REG_TIMEOUT="${REG_TIMEOUT:-1200}"   # 8 GPU ≈ 4-6 min, 16 GPU ≈ 8-12 min (30 GB alloc/GPU)
# Default block set: one small, one mid, one large — the shape of the size curve is what matters,
# and 11 blocks × N configs × N passes is hours. Override with BLOCKS="1-… 1-…".
BLOCKS="${BLOCKS:-1-24647140 1-24697070 1-24628611}"

# CUDA's default device order is FASTEST_FIRST, nvidia-smi's is PCI bus order. On a homogeneous box
# they usually coincide — usually is not a basis for a NUMA experiment, since a silent remap would
# make `CUDA_VISIBLE_DEVICES=0..7` select a different 8 GPUs than the ones we resolved from sysfs.
export CUDA_DEVICE_ORDER=PCI_BUS_ID

CSV_HEADER="test,config,pass,tag,msteps,wall_secs,exec_ms,contrib_ms,inner_ms,final_ms,asm_mhz,instances,n_gpus,numa_local,numa_total,streams,capacity,rc"

# ── output dir ────────────────────────────────────────────────────────────────────────────────
# out_init <test-name> → sets OUT (and RESULTS), creates it, writes the CSV header.
out_init() {
  local name="$1"
  OUT="${OUT:-$HOME/tests/${name}-$(date -u +%Y%m%d-%H%M%SZ)}"
  RESULTS="$OUT/results.csv"
  mkdir -p "$OUT"
  [[ -f "$RESULTS" ]] || echo "$CSV_HEADER" > "$RESULTS"
  echo "== $name → $OUT =="
}

log_() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ── hardware discovery ────────────────────────────────────────────────────────────────────────
# gpu_numa_map → one "<cuda_index> <numa_node> <pci_bdf>" line per GPU, PCI-bus ordered.
# Resolved from sysfs rather than from ZisK's log, because we need it BEFORE starting a worker.
gpu_numa_map() {
  nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null \
  | while IFS=, read -r idx bus; do
      idx="${idx// /}"; bus="${bus// /}"
      # nvidia-smi prints an 8-digit domain (00000000:C1:00.0); sysfs uses 4 (0000:c1:00.0).
      local rest="${bus#*:}"; rest="$(printf '%s' "$rest" | tr 'A-Z' 'a-z')"
      local d="/sys/bus/pci/devices/0000:$rest" node=""
      [[ -r "$d/numa_node" ]] || d="$(echo /sys/bus/pci/devices/*:"$rest" | awk '{print $1}')"
      [[ -r "$d/numa_node" ]] && node="$(cat "$d/numa_node")"
      # -1 means "kernel exposes no affinity" (BIOS with NUMA off, or a container hiding it);
      # that is NOT the same as node 0 and must not be silently folded into it.
      printf '%s %s %s\n' "$idx" "${node:--1}" "$bus"
    done
}

n_gpus_total() { nvidia-smi -L 2>/dev/null | wc -l | tr -d ' '; }

# numa_set <n> <mode>  → a CUDA_VISIBLE_DEVICES string.
#   mode=local : n GPUs all from the SAME numa node (the node with the most GPUs)
#   mode=split : n GPUs spread as evenly as possible across nodes  ← the config we accidentally ship
# Prints nothing and returns 1 if the box cannot honour the request (e.g. split asked for on a
# single-socket box) — the caller must treat that as "skip this arm", not as an empty device list.
numa_set() {
  local want="$1" mode="$2" map; map="$(gpu_numa_map)"
  [[ -n "$map" ]] || return 1
  # A GPU whose numa_node is -1 has UNKNOWN affinity. Folding it into a set would produce an arm that
  # looks valid and means nothing — a "split" built from -1 GPUs is not a cross-socket set, it is a
  # set we cannot reason about. Refuse rather than emit it.
  if awk '$2=="-1"{f=1} END{exit !f}' <<<"$map"; then
    echo "ERROR: some GPUs report numa_node=-1 (kernel exposes no affinity) — cannot build a" >&2
    echo "       NUMA-based device set on this box. See t3-topo.sh §3." >&2
    return 1
  fi
  local nodes; nodes="$(awk '{print $2}' <<<"$map" | sort -u)"
  local n_nodes; n_nodes="$(wc -l <<<"$nodes" | tr -d ' ')"
  local got=""
  if [[ "$mode" == local ]]; then
    local best; best="$(awk '{c[$2]++} END{m=0; for(k in c) if(c[k]>m){m=c[k];b=k}; print b}' <<<"$map")"
    got="$(awk -v n="$best" '$2==n{print $1}' <<<"$map" | head -n "$want" | paste -sd, -)"
  else
    [[ "$n_nodes" -ge 2 ]] || return 1
    local per=$(( want / n_nodes ))
    [[ "$per" -ge 1 ]] || return 1
    local nd part
    while read -r nd; do
      part="$(awk -v n="$nd" '$2==n{print $1}' <<<"$map" | head -n "$per" | paste -sd, -)"
      got="${got:+$got,}$part"
    done <<<"$nodes"
  fi
  # Count elements, not commas: with want=1 a comma count of 0 also matches an EMPTY string, and an
  # empty CUDA_VISIBLE_DEVICES means NO GPUs visible — a config that would run and prove nothing.
  [[ -n "$got" && "$(tr , '\n' <<<"$got" | grep -c .)" -eq "$want" ]] || return 1
  printf '%s' "$got"
}

# recommended_capacity <n_gpus> [streams] — upstream's rule of thumb, which our runs never applied:
# "one unit per physical CPU core (minus two for OS overhead), plus one per GPU stream".
# We advertise the bare default of 10 CU on a ~192-thread, 48-stream box.
recommended_capacity() {
  local g="$1" s="${2:-3}" cores=""
  cores="$(lscpu 2>/dev/null | awk -F: '/^Core\(s\) per socket/{c=$2} /^Socket\(s\)/{k=$2} END{gsub(/ /,"",c); gsub(/ /,"",k); if(c&&k) print c*k}')"
  [[ -n "$cores" ]] || cores="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
  # No core count at all → say nothing rather than emit `-2 + g*s`, which looks like a real
  # recommendation and would be silently passed to --compute-capacity.
  [[ "$cores" =~ ^[0-9]+$ && "$cores" -gt 2 ]] || { echo "ERROR: cannot determine physical core count" >&2; return 1; }
  echo $(( cores - 2 + g * s ))
}

# ── worker lifecycle ──────────────────────────────────────────────────────────────────────────
coordinator_up() { [[ -f "$CLUSTER_DIR/run/coordinator.pid" ]] && kill -0 "$(cat "$CLUSTER_DIR/run/coordinator.pid")" 2>/dev/null; }

wait_registered() {
  local mark="$1" t0 now el tick=0 vram   # mark = coordinator.log line count before the worker started
  t0="$(date +%s)"
  while :; do
    # Registration is 4-12 minutes of near-total silence while ~30 GB per GPU is allocated. Their own
    # watch.sh exists because that silence gets mistaken for a hang — so print progress, and print
    # the thing that actually moves: peak VRAM climbing.
    now="$(date +%s)"; el=$((now-t0))
    if [[ $((el/30)) -gt "$tick" ]]; then
      tick=$((el/30))
      vram="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
              | tr -d ' ' | grep -E '^[0-9]+$' | sort -n | tail -1)"
      log_ "    … still registering, ${el}s elapsed (normal: 4-6 min on 8 GPU, 8-12 min on 16) — peak VRAM ${vram:-?} MiB"
    fi
    # `(^|[^a-z])registered` — NOT a bare 'regist'. Tearing down the previous arm makes the
    # coordinator log an UNregistered/DEregistered line, and depending on flush timing it can land
    # after `mark`; a substring match would then return "ready" while the new worker is still
    # allocating 30 GB per GPU, and the first prove would fail for no visible reason.
    # The `[^a-z]` guard is what excludes "unregistered"/"deregistered" while still matching both
    # wordings upstream has used ("Registered worker …" and "worker registered: …").
    if tail -n +"$((mark+1))" "$CLOG" 2>/dev/null | grep -qaiE '(^|[^a-z])registered'; then return 0; fi
    # Fail fast on the DOCUMENTED hard failures instead of burning the full 20-minute timeout.
    # Deliberately specific: a bare 'invalid argument' also appears in benign messages and would
    # abort a perfectly good arm.
    if tail -n 200 "$WLOG" 2>/dev/null | grep -qaiE 'segmentation fault|failed to bind memory|errno=11|Shmem creation for|cudaMemset'; then
      return 2
    fi
    if [[ -f "$CLUSTER_DIR/run/worker.pid" ]] && ! kill -0 "$(cat "$CLUSTER_DIR/run/worker.pid")" 2>/dev/null; then
      return 3   # worker died without a recognised message
    fi
    [[ "$el" -lt "$REG_TIMEOUT" ]] || return 4
    sleep 5
  done
}

# announce_plan <n_arms> [note] — say what this will cost BEFORE spending it. Every arm pays a full
# worker registration, which is what turns a "quick sweep" into an hour.
announce_plan() {
  local arms="$1" note="${2:-}" nb proofs
  nb="$(wc -w <<<"$BLOCKS" | tr -d ' ')"
  proofs=$(( arms * (WARMUPS + PASSES * nb) ))
  echo "== plan: $arms arm(s) × ($WARMUPS warm-up + $PASSES pass × $nb block) = $proofs proofs,"
  echo "         + $arms worker registration(s) at ~4-6 min (8 GPU) / ~8-12 min (16 GPU)."
  [[ -n "$note" ]] && echo "         $note"
  echo "         Ctrl-C is safe: it stops the GPU sampler and prints how to restore the worker."
}

# The 30 GB/GPU allocation is released only when the old worker really exits. Starting the next
# config while it lingers gets you an OOM that looks like a config problem but is a race.
wait_gpu_free() {
  local t0 used; t0="$(date +%s)"
  while :; do
    # nounits still prints "[N/A]" for a GPU that is resetting or unreachable. `[[ str -lt n ]]`
    # arithmetic on that raises a syntax error to stderr every 5 s and evaluates false — a 3-minute
    # wall of noise ending in a needless SIGKILL. Filter to digits first.
    used="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
            | tr -d ' ' | grep -E '^[0-9]+$' | sort -n | tail -1)"
    [[ -z "$used" || "$used" -lt 2000 ]] && return 0
    [[ $(( $(date +%s) - t0 )) -lt 180 ]] || {
      log_ "WARN: GPU memory still ${used} MiB after 180 s — SIGKILL leftover workers"
      # -f 'zisk-worker' matches the worker (and its mpirun ranks) but not zisk-coordinator, whose
      # cmdline does not contain the string. The coordinator MUST survive: it holds the ELF cache.
      pkill -9 -f 'zisk-worker' 2>/dev/null; sleep 20; return 0; }
    sleep 5
  done
}

# restart_worker <config-label> — swap the worker under a live coordinator, keeping its ELF/proving-key
# cache (a coordinator restart would make every config pay the setup again). Env read by ../start.sh:
# CUDA_VISIBLE_DEVICES · MAX_STREAMS · COMPUTE_CAPACITY · ZISK_LOCK_MEM · USE_MPI · MPI_* · RAYON_NUM_THREADS.
# Returns nonzero if the worker never registered; writes <config>.meta either way.
restart_worker() {
  local cfg="$1" mark rc
  LIB_WORKER_TOUCHED=1
  # The operator's own worker.log (from their ./start.sh, possibly holding an earlier run) is about
  # to be overwritten by start.sh's redirection. Keep a copy once, before the first arm.
  if [[ "${LIB_WLOG_SAVED:-0}" != 1 ]]; then
    LIB_WLOG_SAVED=1
    [[ -s "$WLOG" ]] && cp -f "$WLOG" "$OUT/worker.log.before-tests" 2>/dev/null || true
  fi
  WORKERS_ONLY=1 bash "$CLUSTER_DIR/stop.sh" >/dev/null 2>&1 || true
  wait_gpu_free
  : > "$WLOG"                       # fresh log per config → unambiguous startup banner
  mark="$(wc -l < "$CLOG" 2>/dev/null || echo 0)"
  log_ "starting worker [$cfg] CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<all>} MAX_STREAMS=${MAX_STREAMS:-<auto>} COMPUTE_CAPACITY=${COMPUTE_CAPACITY:-<default 10>}"
  WORKERS_ONLY=1 bash "$CLUSTER_DIR/start.sh" >"$OUT/$cfg.start.log" 2>&1
  wait_registered "$mark"; rc=$?
  cp -f "$WLOG" "$OUT/$cfg.worker-startup.log" 2>/dev/null || true
  worker_meta "$cfg"
  if [[ "$rc" != 0 ]]; then
    log_ "  ✗ [$cfg] worker did not register (rc=$rc) — see $OUT/$cfg.worker-startup.log"
    return 1
  fi
  log_ "  ✓ [$cfg] registered — $(cut -d, -f1-5 <"$OUT/$cfg.meta" | tr '\n' ' ')"
}

# worker_meta <cfg> — scrape the startup banner ONCE per config. These lines appear only at worker
# start, never inside a per-job window, so a per-row scrape would find nothing.
worker_meta() {
  # Two statements, not one: bash expands every RHS of a `local` before assigning any of them, so
  # `local cfg="$1" f="$OUT/$cfg.meta"` dies under `set -u` on an unbound `cfg`.
  local cfg="$1"
  local f="$OUT/$cfg.meta" g nl nt st cap
  g="$(grep -aoE 'Using minimum memory across [0-9]+ GPUs' "$WLOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+' | head -1)"
  [[ -n "$g" ]] || g="$(grep -acE '^\[INFO\] PilStark: Process rank 0 - GPU ' "$WLOG" 2>/dev/null || echo)"
  if grep -qa '\[ALARM\]' "$WLOG" 2>/dev/null; then
    nl="$(grep -aoE 'got [0-9]+/[0-9]+ NUMA-local' "$WLOG" | tail -1 | grep -oE '[0-9]+' | head -1)"
    nt="$(grep -aoE 'got [0-9]+/[0-9]+ NUMA-local' "$WLOG" | tail -1 | grep -oE '[0-9]+' | tail -1)"
  else
    nl="$g"; nt="$g"          # no ALARM = every GPU is NUMA-local
  fi
  st="$(grep -aoE 'Using [0-9]+ streams per GPU for basic' "$WLOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+' | head -1)"
  cap="$(grep -aoE 'Compute Cap +[0-9]+CU' "$WLOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+')"
  printf '%s,%s,%s,%s,%s\n' "${g:-}" "${nl:-}" "${nt:-}" "${st:-}" "${cap:-}" > "$f"
}

# ── steps table ───────────────────────────────────────────────────────────────────────────────
# Msteps is the denominator of every throughput number here, so it must not be guessed. steps.csv
# carries the 11 blocks already measured with `ziskemu -m`.
#
# Resolved ONCE, UP FRONT — never inside the measurement loop. ziskemu is a CPU-saturating emulator;
# invoking it between passes would contend with the prover's own ASM emulator and quietly inflate the
# next pass. And a block that has no entry and cannot be measured would otherwise re-run ziskemu for
# every single row (passes × blocks × arms) for nothing.
resolve_steps() {
  STEPS_CACHE="$OUT/steps.resolved"
  : > "$STEPS_CACHE"
  local tag s missing=""
  for tag in $BLOCKS; do
    s="$(awk -F, -v t="$tag" '$1==t{print $2}' "$TESTS_DIR/steps.csv" 2>/dev/null | head -1)"
    if [[ -z "$s" ]] && command -v ziskemu >/dev/null 2>&1 && [[ -f "$HOME/$tag.bin" ]]; then
      log_ "measuring steps for $tag (ziskemu, once — a few seconds per 100 Msteps)"
      s="$(ziskemu -e "$ELF" -i "$HOME/$tag.bin" -m 2>/dev/null | grep -aoE '[0-9]+' | tail -1)"
      [[ -n "$s" ]] && echo "$tag,$s" >> "$TESTS_DIR/steps.csv"
    fi
    if [[ -n "$s" ]]; then echo "$tag,$s" >> "$STEPS_CACHE"; else missing="$missing $tag"; fi
  done
  if [[ -n "$missing" ]]; then
    echo "WARN: no step count for:$missing" >&2
    echo "      those rows will carry an empty msteps, so they contribute NO throughput number and" >&2
    echo "      are absent from the summary's rankings. Add them to tests/steps.csv:" >&2
    echo "        ziskemu -e $ELF -i ~/<tag>.bin -m" >&2
  fi
}
steps_of() { awk -F, -v t="$1" '$1==t{print $2}' "${STEPS_CACHE:-/dev/null}" 2>/dev/null | head -1; }

# ── worker.log window → phase times ───────────────────────────────────────────────────────────
# Bounded by the line offsets taken around the prove call (clean-bench.sh's trick), so concurrent
# or retried jobs cannot bleed into another row's numbers.
# → "exec_ms contrib_ms inner_ms final_ms asm_mhz instances"
phases_of_range() {
  local s="$1" e="$2" w
  [[ "$s" -lt 1 ]] && s=1
  w="$(sed -n "${s},${e}p" "$WLOG" 2>/dev/null)"
  # Underscore-prefixed: a helper named `get` would be defined GLOBALLY (a function definition inside
  # a function is not scoped) and could shadow something else's helper.
  _ph_get() { grep -aoE "<<< $1 \([0-9]+ms\)" <<<"$w" | tail -1 | grep -oE '[0-9]+' | head -1; }
  printf '%s %s %s %s %s %s\n' \
    "$(_ph_get EXECUTE)" "$(_ph_get CALCULATING_CONTRIBUTIONS)" "$(_ph_get GENERATING_INNER_PROOFS)" "$(_ph_get GENERATE_VADCOP_FINAL_PROOF)" \
    "$(grep -aoE 'Assembly execution speed: [0-9]+MHz' <<<"$w" | tail -1 | grep -oE '[0-9]+' | head -1)" \
    "$(grep -aoE 'Total process instances: [0-9]+' <<<"$w" | tail -1 | grep -oE '[0-9]+' | head -1)"
}

# ── GPU utilisation sampler ───────────────────────────────────────────────────────────────────
# Fills the "approx GPU util" column that zisk-benchmark.md has been carrying empty. Sampled only
# while passes run, so the mean is over proving, not over an idle cluster.
gpu_sampler_start() {
  local f="$1"
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader,nounits -lms 500 > "$f" 2>/dev/null &
  SAMPLER_PID=$!
  # Also on disk: a Ctrl-C between start and stop would otherwise orphan an nvidia-smi loop that
  # keeps appending to that file forever — one per aborted run, invisible until the disk notices.
  echo "$SAMPLER_PID" > "$OUT/.sampler.pid" 2>/dev/null || true
}
gpu_sampler_stop() {
  local p="${SAMPLER_PID:-}"
  [[ -z "$p" && -f "${OUT:-/nonexistent}/.sampler.pid" ]] && p="$(cat "$OUT/.sampler.pid" 2>/dev/null)"
  [[ -n "$p" ]] && kill "$p" 2>/dev/null
  SAMPLER_PID=""
  rm -f "${OUT:-/nonexistent}/.sampler.pid" 2>/dev/null || true
  return 0
}

# ── interrupt handling ────────────────────────────────────────────────────────────────────────
# lib_cleanup_body is what runs on the way out; lib_cleanup adds the exit. t4 has its own trap and
# calls the body, so the two do not fight over who exits.
# Deliberately does NOT try to restore the worker on interrupt: that costs another registration
# (minutes) and someone hitting Ctrl-C wants out now. It prints the one command instead — silently
# leaving the box on a test config is the failure mode worth avoiding, not the restore itself.
lib_cleanup_body() {
  gpu_sampler_stop
  if [[ "${LIB_WORKER_TOUCHED:-0}" == 1 && "${LIB_RESTORED:-0}" != 1 ]]; then
    echo
    echo "⚠️  the box is left running the LAST test config, not a default worker."
    echo "    Restore it with:"
    echo "      cd $CLUSTER_DIR && WORKERS_ONLY=1 ./stop.sh && WORKERS_ONLY=1 ./start.sh"
  fi
}
lib_cleanup() { local rc=$?; lib_cleanup_body; exit "$rc"; }

# ── the measurement ───────────────────────────────────────────────────────────────────────────
setup_elf_once() {
  log_ "remote setup (idempotent)"
  cargo-zisk remote setup -e "$ELF" --hints --coordinator "$COORD" 2>&1 \
    | grep -aE "Hash ID|completed|Error|failed" || true
}

warmup() {
  local b; b="$(awk '{print $1}' <<<"$BLOCKS")"
  for i in $(seq 1 "$WARMUPS"); do
    cargo-zisk remote prove -e "$ELF" -i "$HOME/$b.bin" --hints "$HOME/$b.hints" \
      -o /tmp/warm.proof --coordinator "$COORD" --timeout 0 >/dev/null 2>&1 \
      && log_ "  warm $i ok" || log_ "  warm $i FAIL"
  done
}

# run_bench <test> <config> — PASSES passes over $BLOCKS, one CSV row each. Assumes the worker for
# this config is registered (restart_worker succeeded).
run_bench() {
  local test="$1" cfg="$2"
  # Without a steps cache every row gets an empty msteps and vanishes from the summary's rankings —
  # a whole benchmark that runs, succeeds, and answers nothing. Say so before spending the time.
  [[ -n "${STEPS_CACHE:-}" && -s "${STEPS_CACHE:-/nonexistent}" ]] \
    || log_ "WARN: no resolved step counts (preflight/resolve_steps did not run) — this run will produce NO throughput numbers"
  local g="" nl="" nt="" st="" cap=""
  IFS=, read -r g nl nt st cap < "$OUT/$cfg.meta" 2>/dev/null || true
  warmup
  # Sampler AFTER the warm-up: otherwise the mean utilisation covers a proof we deliberately throw
  # away, plus the idle gap before it, and understates the real figure.
  gpu_sampler_start "$OUT/$cfg.gpuutil.csv"
  local p tag s e t0 t1 dt rc msteps
  local ph_ex ph_co ph_in ph_fi ph_mhz ph_inst
  for p in $(seq 1 "$PASSES"); do
    for tag in $BLOCKS; do
      [[ -f "$HOME/$tag.bin" ]] || { log_ "  skip $tag (no ~/$tag.bin)"; continue; }
      s="$(wc -l < "$WLOG" 2>/dev/null || echo 0)"
      t0="$(date +%s.%N)"
      cargo-zisk remote prove -e "$ELF" -i "$HOME/$tag.bin" --hints "$HOME/$tag.hints" \
        -o "$OUT/$cfg.$tag.proof" --coordinator "$COORD" --timeout 0 \
        > "$OUT/$cfg.$tag.p$p.log" 2>&1
      rc=$?; t1="$(date +%s.%N)"; e="$(wc -l < "$WLOG" 2>/dev/null || echo 0)"
      dt="$(awk "BEGIN{printf \"%.2f\", $t1-$t0}")"
      read -r ph_ex ph_co ph_in ph_fi ph_mhz ph_inst <<<"$(phases_of_range "$s" "$e")"
      msteps="$(steps_of "$tag")"
      printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "$test" "$cfg" "$p" "$tag" "${msteps:-}" "$dt" "${ph_ex:-}" "${ph_co:-}" "${ph_in:-}" "${ph_fi:-}" \
        "${ph_mhz:-}" "${ph_inst:-}" "${g:-}" "${nl:-}" "${nt:-}" "${st:-}" "${cap:-}" "$rc" >> "$RESULTS"
      printf '  %-14s pass %s  %-12s %7ss  asm=%sMHz  inner=%sms  rc=%s%s\n' \
        "$cfg" "$p" "$tag" "$dt" "${ph_mhz:-?}" "${ph_in:-?}" "$rc" \
        "$( [[ "$rc" != 0 ]] && printf '  ← FAILED, see %s' "$OUT/$cfg.$tag.p$p.log" )"
    done
  done
  gpu_sampler_stop
  cp -f "$WLOG" "$OUT/$cfg.worker.log" 2>/dev/null || true
}

# preflight — refuse to produce numbers that cannot mean anything.
preflight() {
  command -v cargo-zisk >/dev/null 2>&1 || { echo "ERROR: cargo-zisk not on PATH (run 00-install-once.sh)" >&2; return 1; }
  command -v nvidia-smi >/dev/null 2>&1 || { echo "ERROR: nvidia-smi not found — these tests are GPU-only" >&2; return 1; }
  [[ -f "$ELF" ]] || { echo "ERROR: ELF not found: $ELF (set ELF=…)" >&2; return 1; }
  coordinator_up || { echo "ERROR: coordinator is not running — start it first:  cd $CLUSTER_DIR && ./start.sh" >&2; return 1; }
  local missing="" tag
  for tag in $BLOCKS; do [[ -f "$HOME/$tag.bin" && -f "$HOME/$tag.hints" ]] || missing="$missing $tag"; done
  [[ -z "$missing" ]] || { echo "ERROR: missing witnesses in \$HOME:$missing" >&2; return 1; }
  [[ -n "${OUT:-}" ]] || { echo "ERROR: preflight called before out_init" >&2; return 1; }
  resolve_steps      # up front, so ziskemu never runs between timed passes
  return 0
}

# Every test leaves the box with a worker up, whatever happened — a half-torn-down cluster is the
# worst state to hand back, because the next run's registration then races the corpse.
restore_default_worker() {
  log_ "restoring a default worker (all GPUs, no overrides)"
  unset CUDA_VISIBLE_DEVICES MAX_STREAMS COMPUTE_CAPACITY ZISK_LOCK_MEM USE_MPI NO_MPI \
        MPI_NP_OVERRIDE MPI_MAPBY MPI_BIND MPI_RAYON_OVERRIDE RAYON_NUM_THREADS
  WORKERS_ONLY=1 bash "$CLUSTER_DIR/stop.sh" >/dev/null 2>&1 || true
  wait_gpu_free
  WORKERS_ONLY=1 bash "$CLUSTER_DIR/start.sh" >/dev/null 2>&1 || true
  LIB_RESTORED=1     # suppresses lib_cleanup's "left on a test config" warning
  # NOT waited on: registration takes minutes and nothing after this needs it. The box is left
  # coming up, which is the state the next run expects.
  log_ "  (registration continues in the background — tail $CLUSTER_DIR/logs/worker.log)"
}
