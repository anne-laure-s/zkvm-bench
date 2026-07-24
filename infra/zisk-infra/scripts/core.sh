# core.sh — generic, guest-agnostic pipeline steps for the ZisK infra.
#
# Sourced by ./run. Operates purely on explicit artifact paths (globals set by
# the dispatcher): ELF, INPUT, HINTS, PROOF, OUT_DIR, RUNNER, REMOTE, PORT, MODE,
# REMOTE_WS. These steps never regenerate artifacts — they consume existing ones.
#
# ZisK specifics:
#   * The runner is the shell wrapper ./zisk-runner (not a compiled binary).
#   * A witness is a PAIR: <tag>.bin + <tag>.hints — prove ships BOTH.
#   * execute uses the ZisK emulator (steps, not cycles); the runner abstracts that.
#
# Not meant to be run directly.

# _artifact_rel <input_path> — derive a proofs/ sub-path (no .bin) from an input.
#   .../inputs/zisk-reth/1-20000000.bin       -> zisk-reth/1-20000000  (mirror under inputs/)
#   .../guests/zisk-reth/inputs/1-20000000.bin -> zisk-reth/1-20000000 (group by guest)
#   /some/where/foo.bin                       -> foo                  (bare stem fallback)
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

# _hints_for <input_path> — the hints sibling of an input (convention: <stem>.hints).
# May be a file OR a directory depending on the hints-gen version. Override with HINTS=.
_hints_for() { echo "${HINTS:-${1%.bin}.hints}"; }

# execute_local — emulate the guest locally (no proof) and save a report (step
# count, the ZisK analogue of SP1 cycles) + public values. Cheap; use it to
# profile a block and keep its stats before/without proving.
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
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found/executable: $RUNNER" >&2; return 1; }
  [[ -f "$ELF"   ]] || { echo "ERROR: ELF not found: $ELF" >&2; return 1; }
  [[ -f "$INPUT" ]] || { echo "ERROR: input not found: $INPUT" >&2; return 1; }

  # Execute artifacts describe the INPUT — write them next to the input.
  _resolve_artifacts "$INPUT"              # -> BASE (stem)
  local dir; dir="$(dirname "$INPUT")"
  local report="${REPORT:-$dir/$BASE.exec-report.json}"
  local pv="${PV:-$dir/$BASE.pv.bin}"

  echo "== execute (local emulation) =="
  echo "ELF    : $ELF"
  echo "Input  : $INPUT"
  "$RUNNER" --elf "$ELF" --input "$INPUT" --mode execute \
    --public-values "$pv" --report "$report"
  local commit_file="${ELF%.elf}.commit"
  [[ -f "$commit_file" ]] && export REPORT_COMMIT="$(cat "$commit_file")"
  _inject_report_meta "$report" ZisK "$INPUT"
  unset REPORT_COMMIT
  echo "Report : $report"
  echo "PV     : $pv"
}

# prove_remote — ship ELF+INPUT+HINTS to the remote GPU prover, prove, retrieve.
# The box must already be running the coordinator/worker (cluster/start.sh) when
# the runner uses the remote backend (default). For a one-shot single-process GPU
# prove, set REMOTE_PROVE_BACKEND=local (runner uses `cargo-zisk prove -g`).
prove_remote() {
  : "${ELF:?}" "${INPUT:?}" "${REMOTE:?set REMOTE=user@host}"
  [[ -f "$ELF"   ]] || { echo "ERROR: ELF not found: $ELF (build it: ./run build-elf GUEST=...)" >&2; return 1; }
  [[ -f "$INPUT" ]] || { echo "ERROR: input not found: $INPUT (generate it: ./run gen-input GUEST=...)" >&2; return 1; }

  # Hints are a proving-time OPTIMIZATION, not part of the witness — the prover's emulator regenerates
  # them at prove time if absent (see guests/zisk-reth/guest.sh, and submit.sh which already treats
  # --hints as optional). So ship them when present, prove without them otherwise (slower, still valid).
  # This is what lets hints-less witnesses prove — e.g. monad (no reth hints-gen), or reth blocks whose
  # hints-gen failed (secp256r1/p256verify).
  local hints have_hints=0; hints="$(_hints_for "$INPUT")"
  [[ -e "$hints" ]] && have_hints=1 || echo "NOTE: no hints for $INPUT — proving without them (prover regenerates; slower)." >&2

  local port="${PORT:-22}" mode="${MODE:-prove-compressed}" ws="${REMOTE_WS:-/workspace}"
  local remote_runner="${REMOTE_RUNNER:-zisk-runner}"
  local backend="${REMOTE_PROVE_BACKEND:-remote}"   # remote (coordinator) | local (cargo-zisk prove -g)
  local ssh=(ssh -p "$port" "$REMOTE")

  local elf_name; elf_name="$(basename "$ELF")"
  local in_name;  in_name="$(basename "$INPUT")"
  local hints_name; hints_name="$(basename "$hints")"
  _resolve_artifacts "$INPUT"              # -> BASE, REL_DIR
  local base="$BASE"
  # One directory per run (never overwritten): results/<…>/<tag>/<mode>-<timestamp>/
  local run_id; run_id="$(date -u +%Y%m%d-%H%M%SZ)-$$"
  local run_dir; run_dir="$(_under "$OUT_DIR")/$BASE/${mode}-${run_id}"
  mkdir -p "$run_dir"

  echo "== prove =="
  echo "ELF       : $ELF"
  echo "Input     : $INPUT"
  echo "Hints     : $([[ $have_hints == 1 ]] && echo "$hints" || echo '(none — prover regenerates)')"
  echo "Remote    : $REMOTE:$port (mode=$mode, backend=$backend)"
  echo "Run dir   : $run_dir"

  # Create the workspace AND resolve it to an absolute path in one round trip.
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
    echo "NOTE: ELF changed — re-run the per-ELF setup on the box: cluster/01-setup-elf.sh $ws/elfs/$elf_name" >&2
  else
    echo "ELF already present on remote, skipping upload."
  fi

  echo "Uploading input${have_hints:+ + hints}..."
  scp -P "$port" "$INPUT" "$REMOTE:$ws/inputs/$in_name"
  [[ $have_hints == 1 ]] && scp -rP "$port" "$hints" "$REMOTE:$ws/inputs/$hints_name"

  # Record the run context (hardware, versions) — what the benchmark ran on.
  {
    echo "run_id   : $run_id"
    echo "input    : $INPUT"
    echo "hints    : $hints"
    echo "elf      : $ELF"
    echo "elf_sha256: $lsum"
    echo "mode     : $mode"
    echo "backend  : $backend"
    echo "remote   : $REMOTE:$port"
    echo "--- remote environment ---"
  } > "$run_dir/env.txt"
  "${ssh[@]}" "{ echo -n 'date    : '; date -u; \
    echo -n 'host    : '; uname -a; \
    echo -n 'cpus    : '; nproc 2>/dev/null || sysctl -n hw.ncpu; \
    echo -n 'zisk    : '; cargo-zisk --version 2>/dev/null || echo '(no cargo-zisk)'; \
    echo '--- gpu ---'; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>/dev/null || echo '(no nvidia-smi)'; }" \
    >> "$run_dir/env.txt" 2>&1 || true

  local logpref=""
  [[ -n "${RUST_LOG:-}" ]] && logpref="RUST_LOG=$RUST_LOG "

  # Run the proof, streaming output live AND capturing the full proving log to a
  # file. `pipefail` so a prover failure still propagates through the `tee`.
  local hints_arg=""; [[ $have_hints == 1 ]] && hints_arg="--hints $ws/inputs/$hints_name"
  "${ssh[@]}" "set -o pipefail; ${logpref}ZISK_PROVE_BACKEND=$backend $remote_runner \
    --elf $ws/elfs/$elf_name \
    --input $ws/inputs/$in_name $hints_arg \
    --mode $mode \
    --output $ws/proofs/$base.proof.bin \
    --public-values $ws/proofs/$base.pv.bin \
    --report $ws/reports/$base.json 2>&1 | tee $ws/reports/$base.log"

  echo "Retrieving artifacts into $run_dir ..."
  scp -P "$port" "$REMOTE:$ws/proofs/$base.proof.bin" "$run_dir/proof.bin"
  scp -P "$port" "$REMOTE:$ws/reports/$base.json"     "$run_dir/report.json"
  scp -P "$port" "$REMOTE:$ws/reports/$base.log"      "$run_dir/prove.log"
  # Public values are best-effort (ZisK may expose them differently per version).
  scp -P "$port" "$REMOTE:$ws/proofs/$base.pv.bin"    "$run_dir/pv.bin" 2>/dev/null || true

  # Bundle the local emulation profile (steps) if it exists, so the run record is
  # self-contained.
  local exec_report="${INPUT%.bin}.exec-report.json"
  [[ -f "$exec_report" ]] && cp "$exec_report" "$run_dir/exec-report.json"

  echo "Done. Run record: $run_dir/"
  echo "  proof.bin"
  echo "  report.json  (timings, proof_bytes, steps)"
  echo "  prove.log    (full ZisK proving trace)"
  echo "  env.txt      (GPU / host / versions)"
  echo "Verify with : ./run verify ELF=$ELF INPUT=$INPUT PROOF=$run_dir/proof.bin"
}

# verify_local — verify an existing PROOF locally (cryptographic validity).
# Requires the ZisK verify key in ~/.zisk (ziskup). Best-effort PV cross-check:
# re-emulate the guest to recompute the expected public values and compare.
verify_local() {
  : "${ELF:?}" "${INPUT:?}" "${PROOF:?set PROOF=path}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found/executable: $RUNNER" >&2; return 1; }
  [[ -f "$ELF"    ]] || { echo "ERROR: ELF not found: $ELF" >&2; return 1; }
  [[ -f "$INPUT"  ]] || { echo "ERROR: input not found: $INPUT" >&2; return 1; }
  [[ -f "$PROOF"  ]] || { echo "ERROR: proof not found: $PROOF (run: ./run prove ...)" >&2; return 1; }

  local pdir; pdir="$(dirname "$PROOF")"
  local expected_pv="$pdir/expected_pv.bin"
  local remote_pv="$pdir/pv.bin"

  echo "== verify =="
  echo "Proof     : $PROOF"

  # 1a. Recompute expected public values locally (emulation, cheap).
  echo "--- recomputing expected public values (local emulation) ---"
  "$RUNNER" --elf "$ELF" --input "$INPUT" --mode execute --public-values "$expected_pv" || \
    echo "WARN: could not recompute expected PV (emulation failed)"

  # 1b. Verify the proof cryptographically.
  echo "--- verifying proof ---"
  "$RUNNER" --mode verify --proof "$PROOF"

  # Cross-check the retrieved remote PV matches what we recomputed.
  if [[ -f "$remote_pv" && -f "$expected_pv" ]]; then
    if cmp -s "$remote_pv" "$expected_pv"; then
      echo "Remote PV cross-check: OK"
    else
      echo "WARNING: remote PV differs from locally recomputed PV!" >&2
    fi
  fi

  echo "Public values: $expected_pv"
  echo "Decode with  : ./run decode-pv GUEST=<name> PV=$expected_pv"
}
