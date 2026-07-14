# core.sh — generic, guest-agnostic pipeline steps for the OpenVM infra.
#
# Sourced by ./run. Operates on the block number + explicit paths (globals set by the
# dispatcher): BLOCK, CHAIN_ID, RPC_URL, IN_DIR, OUT_DIR, RUNNER, REMOTE, PORT, MODE,
# NUM_GPUS, PROOF.
#
# OpenVM differences:
#   * No ELF/witness artifact to ship: the guest ELF is baked into the prover binary and
#     the block witness is minted from an archive RPC by block number. So `prove` ships
#     nothing but the request; the box must already have the (patched) binary built
#     (cluster/00-install-once.sh). Optionally rsync the local rpc-cache to save a refetch.
#   * `execute` is a local CPU cycle count (no GPU).
#   * Multi-GPU is one process PER GPU (path ①, cluster/submit.sh over SSH), not a cluster.
#
# Not meant to be run directly.

# execute_local — metered execution locally (no proof) -> cycle count report + (best
# effort) public values. Cheap; use it to profile a block before/without proving.
# _inject_report_meta <report.json> <zkvm> <guest> <block>  — normalize the execute report to the
# shared core, prepended: mode · zkvm · guest · block · <work-unit> · elapsed_secs · <backend extras>.
_inject_report_meta() {
  local rep="$1"; [[ -f "$rep" ]] || return 0
  python3 - "$@" <<'PY'
import json,sys
a=sys.argv[1:]; rep,zkvm,guest=a[0],a[1],a[2]
block=int(a[3]) if len(a)>=4 and a[3].isdigit() else None
d=json.load(open(rep))
core={"mode":d.get("mode","execute"),"zkvm":zkvm,"guest":guest,"block":block}
json.dump({**core,**{k:v for k,v in d.items() if k not in core}},open(rep,"w"),indent=2)
PY
}

execute_local() {
  : "${BLOCK:?}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found/executable: $RUNNER" >&2; return 1; }
  local tag="${CHAIN_ID}-${BLOCK}"
  local dir="$IN_DIR"
  mkdir -p "$dir"
  local report="${REPORT:-$dir/$tag.exec-report.json}"
  local pv="${PV:-$dir/$tag.pv.bin}"

  echo "== execute (local metered, cycle count) =="
  echo "Block  : $BLOCK (chain $CHAIN_ID)"
  OPENVM_CACHE_DIR="${OPENVM_CACHE_DIR:-$IN_DIR/rpc-cache}" \
    "$RUNNER" --mode execute --block "$BLOCK" --chain "$CHAIN_ID" ${RPC_URL:+--rpc "$RPC_URL"} \
      --public-values "$pv" --report "$report"
  _inject_report_meta "$report" OpenVM "${GUEST:-openvm-reth}" "$BLOCK"
  echo "Report : $report"
}

# prove_remote — run cluster/submit.sh (path ①, multi-process multi-GPU) ON THE BOX over SSH and
# retrieve its run record (report.json + timing.txt + worker/aggregate logs + gpu-util.csv + proof).
# The box must already have the patched binary built + keys generated (cluster/00-install-once.sh,
# cluster/01-keygen.sh) in its openvm-infra checkout: REMOTE_INFRA (default /workspace/openvm/openvm-infra).
# Nothing but the request is shipped; set RSYNC_CACHE=1 to also push the local rpc-cache (saves a refetch).
prove_remote() {
  : "${BLOCK:?}" "${REMOTE:?set REMOTE=user@host}"
  local port="${PORT:-22}"
  local infra="${REMOTE_INFRA:-/workspace/openvm/openvm-infra}"
  local num_gpus="${NUM_GPUS:-8}"
  local rpc="${RPC_URL:-${RPC_1:-}}"
  local ssh=(ssh -p "$port" "$REMOTE")
  [[ "${MODE:-prove-stark}" == "prove-stark" ]] || \
    echo "NOTE: path ① proves prove-stark only; ignoring MODE=${MODE}." >&2

  local tag="${CHAIN_ID}-${BLOCK}"
  local run_id; run_id="$(date -u +%Y%m%d-%H%M%SZ)-$$"
  local run_dir; run_dir="$OUT_DIR/${GUEST:-openvm-reth}/$tag/prove-stark-${run_id}"
  mkdir -p "$run_dir"

  echo "== prove (remote, multi-GPU path ①) =="
  echo "Block     : $BLOCK (chain $CHAIN_ID)"
  echo "Remote    : $REMOTE:$port  (NUM_GPUS=$num_gpus, box infra=$infra)"
  echo "Run dir   : $run_dir"
  [[ -n "$rpc" ]] || echo "NOTE: no RPC_URL/RPC_1 — the box must already have block $BLOCK cached." >&2

  if [[ "${RSYNC_CACHE:-0}" == 1 ]]; then
    local lcache="$IN_DIR/rpc-cache"
    [[ -d "$lcache" ]] && { echo "Rsyncing local rpc-cache -> box..."; \
      rsync -az -e "ssh -p $port" "$lcache/" "$REMOTE:$infra/inputs/${GUEST:-openvm-reth}/rpc-cache/" || true; }
  fi

  # Run the working multi-GPU driver on the box; it writes its own run record + schema report.json
  # under $infra/cluster/runs/mg-<tag>-<ts>/. Stream live AND capture to prove.log. `\$` / escaped
  # quotes evaluate on the REMOTE shell; the ${var:+…} pieces are expanded LOCALLY into the command.
  "${ssh[@]}" "cd '$infra' && NUM_GPUS=$num_gpus CHAIN_ID=$CHAIN_ID ${rpc:+RPC_1='$rpc'} ${RUST_LOG:+RUST_LOG='$RUST_LOG'} cluster/submit.sh $BLOCK" \
    2>&1 | tee "$run_dir/prove.log"
  local rc=${PIPESTATUS[0]}
  [[ "$rc" == 0 ]] || echo ">>> remote submit.sh exited $rc (proof may be incomplete) — see $run_dir/prove.log" >&2

  # Locate the run record submit.sh just created (newest mg-<tag>-* dir) and fetch it compressed
  # (box seconds count), excluding the bulky intermediate segment proofs.
  local remote_run
  remote_run="$("${ssh[@]}" "ls -td '$infra'/cluster/runs/mg-${tag}-*/ 2>/dev/null | head -1" | tr -d '\r')"
  if [[ -n "$remote_run" ]]; then
    echo "Retrieving $remote_run -> $run_dir/ (compressed, minus segments/) ..."
    "${ssh[@]}" "cd '$remote_run' && tar czf - --exclude=./segments ." | tar xzf - -C "$run_dir" \
      || echo "WARN: fetch failed — inspect $remote_run on the box" >&2
  else
    echo "WARN: no remote run record under $infra/cluster/runs/mg-${tag}-* — see $run_dir/prove.log" >&2
  fi

  # Bundle the local cycle-count profile if it exists.
  local exec_report="$IN_DIR/$tag.exec-report.json"
  [[ -f "$exec_report" ]] && cp "$exec_report" "$run_dir/exec-report.json"

  echo "Done. Run record: $run_dir/"
  echo "  report.json  (shared schema: prove_secs, total_secs, proof_bytes, num_gpus, cycles, …)"
  echo "  timing.txt   (workers_secs / aggregate_secs / total_secs / num_segments)"
  echo "  worker-*.log · aggregate.log · gpu-util.csv · proof.json · env.txt · prove.log"
}

# verify_local — verify a saved proof. See openvm-runner notes: prove-stark self-verifies
# segments at prove time; an independent re-verify needs a verify bin (OPENVM_VERIFY_BIN).
verify_local() {
  : "${PROOF:?set PROOF=path}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found/executable: $RUNNER" >&2; return 1; }
  [[ -f "$PROOF"  ]] || { echo "ERROR: proof not found: $PROOF" >&2; return 1; }
  echo "== verify =="
  "$RUNNER" --mode verify --proof "$PROOF"
}
