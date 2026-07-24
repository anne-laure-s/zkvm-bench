# core.sh — generic, guest-agnostic pipeline steps.
#
# Sourced by ./run. Operates purely on explicit artifact paths (globals set by
# the dispatcher): ELF, INPUT, PROOF, OUT_DIR, RUNNER, REMOTE, PORT, MODE,
# REMOTE_WS. These steps never regenerate artifacts — they consume existing ones.
#
# Not meant to be run directly.

# _artifact_rel <input_path> — derive a proofs/ sub-path (no .bin) from an input.
#   .../guests/rsp/inputs/1-25367437.bin    -> rsp/1-25367437 (group by guest)
#   .../inputs/fibonacci/n20.bin            -> fibonacci/n20  (mirror under inputs/)
#   /some/where/foo.bin                     -> foo            (bare stem fallback)
_artifact_rel() {
  local in="$1" rel
  case "$in" in
    */guests/*/inputs/*)   # top-level guests/<name>/inputs/<tag>
                  local a="${in##*/guests/}"; rel="${a%%/*}/${a##*/}" ;;
    */inputs/*)   rel="${in##*/inputs/}" ;;
    *)            rel="${in##*/}" ;;
  esac
  echo "${rel%.bin}"
}

# _resolve_artifacts <input_path> — set BASE (stem) and REL_DIR (sub-path, "." if none).
_resolve_artifacts() {
  local rel; rel="$(_artifact_rel "$1")"
  BASE="$(basename "$rel")"
  REL_DIR="$(dirname "$rel")"
}

# _under <base_dir> — join base_dir with the resolved REL_DIR (drops a bare ".").
_under() {
  if [[ "$REL_DIR" == "." ]]; then echo "$1"; else echo "$1/$REL_DIR"; fi
}

# execute_local — run the guest locally (CPU, no proof) and save a rich
# execution report (cycle tracker, opcode/syscall counts, gas) + public values.
# Cheap; use it to profile a block and keep its stats before/without proving.
# _inject_report_meta <report.json> <zkvm> <input-path>            (guest+block derived from path)
#                     <report.json> <zkvm> <guest> <block>         (explicit; openvm block-based)
# Normalize the execute report to the shared core, prepended in order:
#   mode · zkvm · guest · block · <work-unit> · elapsed_secs · <backend extras>
_inject_report_meta() {
  local rep="$1"; [[ -f "$rep" ]] || return 0
  python3 - "$@" <<'PY'
import json,sys,os,re
a=sys.argv[1:]; rep,zkvm=a[0],a[1]
d=json.load(open(rep))
if len(a)>=4:
    guest=a[2]; block=int(a[3]) if a[3].isdigit() else None
else:
    inp=a[2]; m=re.search(r'guests/([^/]+)/',inp); guest=m.group(1) if m else None
    stem=os.path.splitext(os.path.basename(inp))[0]; seg=stem.split('-')[-1]
    block=int(seg) if seg.isdigit() else None
commit=(os.environ.get("REPORT_COMMIT") or "").strip() or None
core={"mode":d.get("mode","execute"),"zkvm":zkvm,"guest":guest,"block":block,"commit":commit}
json.dump({**core,**{k:v for k,v in d.items() if k not in core}},open(rep,"w"),indent=2)
PY
}

execute_local() {
  : "${ELF:?}" "${INPUT:?}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found: $RUNNER (cargo build --release in sp1-runner/)" >&2; return 1; }
  [[ -f "$ELF"   ]] || { echo "ERROR: ELF not found: $ELF" >&2; return 1; }
  [[ -f "$INPUT" ]] || { echo "ERROR: input not found: $INPUT" >&2; return 1; }

  # Execute artifacts describe the INPUT — write them next to the input.
  _resolve_artifacts "$INPUT"              # -> BASE (stem)
  local dir; dir="$(dirname "$INPUT")"
  local report="${REPORT:-$dir/$BASE.exec-report.json}"
  local pv="${PV:-$dir/$BASE.pv.bin}"

  echo "== execute (local CPU) =="
  echo "ELF    : $ELF"
  echo "Input  : $INPUT"
  SP1_PROVER=cpu "$RUNNER" --elf "$ELF" --input "$INPUT" --mode execute \
    --public-values "$pv" --report "$report"
  local commit_file="${ELF%.elf}.commit"
  [[ -f "$commit_file" ]] && export REPORT_COMMIT="$(cat "$commit_file")"
  _inject_report_meta "$report" SP1 "$INPUT"
  unset REPORT_COMMIT
  echo "Report : $report"
  echo "PV     : $pv"
}

# prove_remote — ship ELF+INPUT to the remote GPU prover, prove, retrieve.
prove_remote() {
  : "${ELF:?}" "${INPUT:?}" "${REMOTE:?set REMOTE=user@host}"
  [[ -f "$ELF"   ]] || { echo "ERROR: ELF not found: $ELF (build it: ./run build-elf GUEST=...)" >&2; return 1; }
  [[ -f "$INPUT" ]] || { echo "ERROR: input not found: $INPUT (generate it: ./run gen-input GUEST=...)" >&2; return 1; }

  local port="${PORT:-22}" mode="${MODE:-prove-compressed}" ws="${REMOTE_WS:-/workspace}"
  # Defaults assume the box has `sp1-runner` on PATH (SP1_PROVER=cuda). For a
  # bare SSH host, override REMOTE_RUNNER / REMOTE_PROVER / REMOTE_WS.
  local remote_runner="${REMOTE_RUNNER:-sp1-runner}"
  local prover_prefix=""
  [[ -n "${REMOTE_PROVER:-}" ]] && prover_prefix="SP1_PROVER=$REMOTE_PROVER "
  # Network backend (the self-hosted 16-GPU cluster): the box runner needs the gateway URL + a key
  # (AUTH_MODE=none, but the SDK requires one to exist) — mirror cluster-native/submit.sh. Defaults
  # assume the gateway is local to the box (localhost:50061); override NETWORK_RPC_URL for another.
  if [[ "${REMOTE_PROVER:-}" == network ]]; then
    prover_prefix+="NETWORK_RPC_URL=${NETWORK_RPC_URL:-http://localhost:50061} "
    prover_prefix+="NETWORK_PRIVATE_KEY=${NETWORK_PRIVATE_KEY:-0x0000000000000000000000000000000000000000000000000000000000000001} "
  fi
  local ssh=(ssh -p "$port" "$REMOTE")

  local elf_name; elf_name="$(basename "$ELF")"
  local in_name;  in_name="$(basename "$INPUT")"
  _resolve_artifacts "$INPUT"              # -> BASE, REL_DIR
  local base="$BASE"                       # remote artifact filename stem
  # One directory per run (never overwritten): results/<…>/<tag>/<mode>-<timestamp>/
  # PID suffix guarantees uniqueness even for two runs in the same second.
  local run_id; run_id="$(date -u +%Y%m%d-%H%M%SZ)-$$"
  local run_dir; run_dir="$(_under "$OUT_DIR")/$BASE/${mode}-${run_id}"
  mkdir -p "$run_dir"

  echo "== prove =="
  echo "ELF       : $ELF"
  echo "Input     : $INPUT"
  echo "Remote    : $REMOTE:$port (mode=$mode)"
  echo "Run dir   : $run_dir"

  # Create the workspace AND resolve it to an absolute path in one round trip.
  # The remote shell expands ~ / $HOME / relative paths here; scp (SFTP) would
  # not, so we must hand scp the already-resolved absolute path.
  local ws_abs
  ws_abs="$("${ssh[@]}" "mkdir -p \"$ws\"/elfs \"$ws\"/inputs \"$ws\"/proofs \"$ws\"/reports && cd \"$ws\" && pwd")" \
    || { echo "ERROR: cannot prepare remote workspace '$ws'" >&2; return 1; }
  ws="$ws_abs"

  # Upload the ELF only when the remote copy differs (cheap to repeat per input).
  local lsum rsum
  lsum="$(shasum -a 256 "$ELF" | awk '{print $1}')"
  rsum="$("${ssh[@]}" "sha256sum $ws/elfs/$elf_name 2>/dev/null | awk '{print \$1}'" || true)"
  if [[ "$lsum" != "$rsum" ]]; then
    echo "Uploading ELF (checksum changed/missing)..."
    scp -P "$port" "$ELF" "$REMOTE:$ws/elfs/$elf_name"
  else
    echo "ELF already present on remote, skipping upload."
  fi

  echo "Uploading input..."
  scp -P "$port" "$INPUT" "$REMOTE:$ws/inputs/$in_name"

  # Record the run context (hardware, versions) — what the benchmark ran on.
  # Derive the SP1 version from the runner's Cargo.toml pin so env.txt can't drift from it
  # (the pin is the single source of truth; a bump there now updates every run record).
  local sp1_ver
  sp1_ver="$(sed -n 's/^sp1-sdk[^"]*"=\{0,1\}\([0-9][^"]*\)".*/\1/p' "$ROOT/sp1-runner/Cargo.toml" 2>/dev/null | head -1)"
  {
    echo "run_id   : $run_id"
    echo "input    : $INPUT"
    echo "elf      : $ELF"
    echo "elf_sha256: $lsum"
    echo "mode     : $mode"
    echo "sp1      : ${sp1_ver:-?} (Hypercube / jagged PCS; from sp1-runner/Cargo.toml pin)"
    echo "rust_log : ${RUST_LOG:-<remote default>}"
    echo "remote   : $REMOTE:$port"
    echo "--- remote environment ---"
  } > "$run_dir/env.txt"
  "${ssh[@]}" "{ echo -n 'date    : '; date -u; \
    echo -n 'host    : '; uname -a; \
    echo -n 'cpus    : '; nproc 2>/dev/null || sysctl -n hw.ncpu; \
    echo '--- gpu ---'; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>/dev/null || echo '(no nvidia-smi)'; }" \
    >> "$run_dir/env.txt" 2>&1 || true

  # Optional extra prover verbosity (default RUST_LOG=info; use RUST_LOG=debug
  # for the full Hypercube phase-by-phase trace).
  local logpref=""
  [[ -n "${RUST_LOG:-}" ]] && logpref="RUST_LOG=$RUST_LOG "

  # Run the proof, streaming output live AND capturing the full proving log
  # (SP1 phase-by-phase timings) to a file. `pipefail` so a prover failure
  # still propagates through the `tee`.
  "${ssh[@]}" "set -o pipefail; ${prover_prefix}${logpref}$remote_runner \
    --elf $ws/elfs/$elf_name \
    --input $ws/inputs/$in_name \
    --mode $mode \
    --output $ws/proofs/$base.proof.bin \
    --public-values $ws/proofs/$base.pv.bin \
    --vkey $ws/proofs/$base.vkey.txt \
    --report $ws/reports/$base.json 2>&1 | tee $ws/reports/$base.log"

  echo "Retrieving artifacts into $run_dir ..."
  scp -P "$port" "$REMOTE:$ws/proofs/$base.proof.bin" "$run_dir/proof.bin"
  scp -P "$port" "$REMOTE:$ws/proofs/$base.pv.bin"    "$run_dir/pv.bin"
  scp -P "$port" "$REMOTE:$ws/proofs/$base.vkey.txt"  "$run_dir/vkey.txt"
  scp -P "$port" "$REMOTE:$ws/reports/$base.json"     "$run_dir/report.json"
  scp -P "$port" "$REMOTE:$ws/reports/$base.log"      "$run_dir/prove.log"

  # Bundle the local execution profile (cycles/gas/opcodes) if it exists, so the
  # run record is self-contained.
  local exec_report="${INPUT%.bin}.exec-report.json"
  [[ -f "$exec_report" ]] && cp "$exec_report" "$run_dir/exec-report.json"

  echo "Done. Run record: $run_dir/"
  echo "  proof.bin / pv.bin / vkey.txt"
  echo "  report.json  (timings, proof_bytes, vkey)"
  echo "  prove.log    (full SP1 proving trace)"
  echo "  env.txt      (GPU / host / versions)"
  [[ -f "$run_dir/exec-report.json" ]] && echo "  exec-report.json (cycles / gas / opcodes)"
  echo "Verify with : ./run verify ELF=$ELF INPUT=$INPUT PROOF=$run_dir/proof.bin"
}

# verify_local — verify an existing PROOF locally, bound to expected PV.
verify_local() {
  : "${ELF:?}" "${INPUT:?}" "${PROOF:?set PROOF=path}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found: $RUNNER (cargo build --release in sp1-runner/)" >&2; return 1; }
  [[ -f "$ELF"    ]] || { echo "ERROR: ELF not found: $ELF" >&2; return 1; }
  [[ -f "$INPUT"  ]] || { echo "ERROR: input not found: $INPUT" >&2; return 1; }
  [[ -f "$PROOF"  ]] || { echo "ERROR: proof not found: $PROOF (run: ./run prove ...)" >&2; return 1; }

  # Sibling artifacts live next to the proof in its run directory
  # (prove_remote writes proof.bin / pv.bin / … into one dir per run).
  local pdir; pdir="$(dirname "$PROOF")"
  local expected_pv="$pdir/expected_pv.bin"
  local remote_pv="$pdir/pv.bin"

  echo "== verify =="
  echo "Proof     : $PROOF"

  # 1a. Recompute expected public values locally (CPU execute, cheap).
  echo "--- recomputing expected public values (local execute) ---"
  SP1_PROVER=cpu "$RUNNER" --elf "$ELF" --input "$INPUT" --mode execute --public-values "$expected_pv"

  # 1b. Verify the proof, bound to those expected public values.
  echo "--- verifying proof (bound to expected PV) ---"
  SP1_PROVER=cpu "$RUNNER" --elf "$ELF" --mode verify --proof "$PROOF" --public-values "$expected_pv"

  # Cross-check the retrieved remote PV matches what we recomputed.
  if [[ -f "$remote_pv" ]]; then
    if cmp -s "$remote_pv" "$expected_pv"; then
      echo "Remote PV cross-check: OK"
    else
      echo "WARNING: remote PV differs from locally recomputed PV!" >&2
    fi
  fi

  echo "Public values: $expected_pv"
  echo "Decode with  : ./run decode-pv GUEST=<name> PV=$expected_pv"
}
