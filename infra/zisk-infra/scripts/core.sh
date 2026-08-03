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
    */guests/*/inputs/*|*/guests/*/fixtures/*)
                  # top-level guests/<name>/{inputs,fixtures}/<tag> — group by guest. fixtures/ is the
                  # farm QUEUE, so without this case every farmed run record lands in a bare results/<tag>/
                  # shared by ALL guests: two guests proving the same block become indistinguishable, and
                  # same-named run dirs (e.g. mock-run) silently overwrite each other.
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

  # ONE ssh connection for the whole block, compressed. Proving a block opens ~8 separate ssh/scp calls
  # (mkdir, checksum, the input, the run, then proof/report/log/pv back). Without multiplexing each pays a
  # full TCP + key exchange, and over a high-latency or tunnelled link that handshake cost dominated the
  # transfer itself. ControlMaster makes the first call establish a shared channel the rest reuse, and -C
  # compresses the 7 MB witness on the wire (trie nodes and RLP compress well).
  # ZISK_SSH_MUX=0 disables it if a host ever misbehaves; ZISK_SSH_COMPRESS=0 drops the compression.
  local mux=()
  if [[ "${ZISK_SSH_MUX:-1}" != 0 ]]; then
    local cpath="${TMPDIR:-/tmp}/zisk-mux-%C"
    mux=(-o ControlMaster=auto -o "ControlPath=$cpath" -o ControlPersist=120)
  fi
  [[ "${ZISK_SSH_COMPRESS:-1}" != 0 ]] && mux+=(-C)
  local ssh=(ssh "${mux[@]}" -p "$port" "$REMOTE")
  # scp takes the same options but spells the port -P.
  local scpo=("${mux[@]}" -P "$port")

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

  # Create the workspace, resolve it to an absolute path, AND read the remote ELF's hash — all in ONE round
  # trip. The hash used to be its own ssh call, paid on every block to learn something that changes almost
  # never. Folding it in here beats caching it locally: one fewer exchange and no stale-cache failure mode.
  # Line 1 is the path, line 2 the hash (empty if the ELF is not there yet, which correctly forces an upload).
  # Timed from HERE, before the first exchange with the prover: the workspace call is part of what `input`
  # costs, and starting the clock after it would flatter the very change being measured.
  # %N (Linux — core.sh runs on the producer box): tenths, because at ~2 s whole seconds hide real gains.
  local t_start; t_start="$(date +%s.%N)"
  local ws_meta ws_abs rsum rinsize
  ws_meta="$("${ssh[@]}" "mkdir -p \"$ws\"/elfs \"$ws\"/inputs \"$ws\"/proofs \"$ws\"/reports && cd \"$ws\" && pwd \
&& { sha256sum elfs/$elf_name 2>/dev/null || shasum -a 256 elfs/$elf_name 2>/dev/null; } | awk '{print \$1}' \
&& { stat -c %s inputs/$in_name 2>/dev/null || stat -f %z inputs/$in_name 2>/dev/null || echo 0; }")" \
    || { echo "ERROR: cannot prepare remote workspace '$ws'" >&2; return 1; }
  ws_abs="$(printf '%s\n' "$ws_meta" | sed -n 1p)"
  rsum="$(printf '%s\n' "$ws_meta" | sed -n 2p)"
  # Line 3: the size the prover ALREADY has for this input, 0 if absent. This is what makes overlap possible —
  # a witness shipped ahead of time by prove-farm's prefetcher costs nothing here.
  rinsize="$(printf '%s\n' "$ws_meta" | sed -n 3p)"
  [[ -n "$ws_abs" ]] || { echo "ERROR: remote workspace '$ws' did not resolve" >&2; return 1; }
  ws="$ws_abs"

  # Upload the ELF only when the remote copy differs (cheap to repeat per input).
  # Portable on both ends: coreutils ships sha256sum, macOS ships shasum. And never let two EMPTY hashes
  # compare equal — that would silently skip the upload and leave the remote to fail on a missing ELF.
  # rsum already came back with the workspace setup above — no second round trip for it.
  local lsum
  if   command -v sha256sum >/dev/null 2>&1; then lsum="$(sha256sum "$ELF" | awk '{print $1}')"
  elif command -v shasum    >/dev/null 2>&1; then lsum="$(shasum -a 256 "$ELF" | awk '{print $1}')"
  else lsum=""; echo "NOTE: no sha256sum/shasum here — uploading the ELF unconditionally." >&2; fi
  if [[ -z "$lsum" || "$lsum" != "$rsum" ]]; then
    echo "Uploading ELF (checksum changed/missing)..."
    scp "${scpo[@]}" "$ELF" "$REMOTE:$ws/elfs/$elf_name"
    echo "NOTE: ELF changed — re-run the per-ELF setup on the box: cluster/01-setup-elf.sh $ws/elfs/$elf_name" >&2
  else
    echo "ELF already present on remote, skipping upload."
  fi

  # PULL_VIA — send the bytes the OTHER WAY ROUND, over a connection the remote opens itself.
  #
  # Measured on the RTP prototype: pushing through a reverse ssh tunnel to a home Mac gave 3.8 Mbit/s, while
  # a direct connection between the SAME two machines gave 25 Mbit/s or better. The link was never the
  # limit — TCP inside TCP was. The asymmetry is only about who can be reached: a datacenter box is publicly
  # addressable, a laptop behind NAT is not, which is why the tunnel exists at all.
  #
  # So keep the tunnel for the control channel (a few hundred bytes of ssh command) and let the remote pull
  # the payload over a direct, un-nested connection: PULL_VIA is this machine's ssh target AS SEEN FROM THE
  # REMOTE. On the witness box, PULL_VIA=aschmitt@nyc-003 turns 7-15 MB from ~15 s into ~3 s.
  #
  # The remote runs this non-interactively, so it needs its own key for us — no agent will be present. On
  # failure we say so loudly and fall back to pushing: a slow pipeline beats a stopped one, but a silent
  # fallback would let a broken config masquerade as a working optimisation.
  local pulled=0 prefetched=0
  # BOTH directions need these options — the input pull below AND the artifact push-back in `retrieve`. Declare
  # them here, at function scope, never inside a branch. They used to be declared inside the `elif` that pulls
  # the input, so a PREFETCHED input took the first branch, left pull_opts unset, and `set -u` killed the
  # retrieve step at the tar: "line 301: pull_opts: unbound variable". Every block the prefetcher successfully
  # shipped therefore FAILED — and failed again on every retry, because the input was still sitting on the
  # prover and matched on size again. 31 blocks poisoned, 0 recovered, while the feature looked like it was
  # working: `(prefetched N …)` in the farm log, `<< N FAIL (rc=1 no-report no-proof)` eight seconds later.
  # The prefetch measured as 0 % effective for exactly as long as it was 100 % fatal.
  local pull_opts="${PULL_SSH_OPTS:--C -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
  # Already there, byte-for-byte the same size? Then a prefetcher shipped it while the previous block was being
  # proved, and the transfer has already happened off the critical path. Size, not hash: a hash would cost the
  # very round trip this is avoiding, and these names are content-addressed by block — a same-named input of
  # the same length is the same witness.
  local lin_size; lin_size="$(stat -c %s "$INPUT" 2>/dev/null || stat -f %z "$INPUT" 2>/dev/null || echo 0)"
  if [[ "$lin_size" != 0 && "$rinsize" == "$lin_size" ]]; then
    echo "Input already on the prover (prefetched, $lin_size B) — no transfer on the critical path."
    prefetched=1; pulled=1
  elif [[ -n "${PULL_VIA:-}" ]]; then
    local abs_input; abs_input="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
    # -C only. Multiplexing was tried here and made things WORSE: input went from 3-4 s to 2.5-23 s, wildly
    # variable, while `retrieve` (small, same options) stayed at 0.5 s. Sharing one ControlMaster channel means
    # sharing one TCP connection and one ssh channel window, and a single 10 MB stream does not want either —
    # it wants its own connection, which is what it had. Multiplexing pays off for MANY SMALL round trips (the
    # box->prover control calls, where it belongs and stays); it is the wrong tool for one bulk transfer.
    # The handshake it would have saved is ~0.3 s. The contention it caused was measured in seconds.
    #
    # NO -O, and no raw `ssh cat` either — both were tried and neither survives contact with production.
    #
    # `scp -O` (the legacy SCP protocol instead of SFTP) benched at 19 % on an 8-round A/B and delivered
    # **+1 %** over 43 real blocks, size-normalised (0.572 -> 0.566 s/MB). A raw stream (`ssh host "cat f"`)
    # benched 9 % better than -O, which on that showing is also noise. And on INCOMPRESSIBLE data with
    # compression off — the clean test — scp and a raw stream are identical at 25-28 Mbit/s, i.e. the link.
    # There is no protocol-level win here to collect.
    #
    # A cautionary note on how the 9 % nearly became "3.6x": in `while read f; do ssh ...; done < list`, ssh
    # CONSUMES STDIN and eats the rest of the list, so the loop transfers one file while the scp arm transfers
    # three. Use `ssh -n` in a loop, and verify bytes received, not just elapsed time. The tell was a result
    # above the link rate — 69 Mbit/s on a 28 Mbit/s link is a broken measurement, never a fast one.
    echo "Pulling input from $PULL_VIA (direct, bypassing the tunnel for the payload)..."
    if "${ssh[@]}" "scp -q $pull_opts '$PULL_VIA:$abs_input' '$ws/inputs/$in_name'"; then
      pulled=1
    else
      echo "WARNING: $REMOTE could not pull from $PULL_VIA — falling back to pushing through the tunnel." >&2
      echo "         Check from the remote: ssh $pull_opts $PULL_VIA true" >&2
    fi
  fi
  if [[ $pulled == 0 ]]; then
    echo "Uploading input${have_hints:+ + hints}..."
    scp "${scpo[@]}" "$INPUT" "$REMOTE:$ws/inputs/$in_name"
  fi
  [[ $have_hints == 1 ]] && scp -r "${scpo[@]}" "$hints" "$REMOTE:$ws/inputs/$hints_name"
  local t_in; t_in="$(date +%s.%N)"

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
  # Mock env, when the caller wants a MOCK prover on a REAL remote: everything on this side is unchanged
  # (uploads, invocation, run-record fetch), only the far end sleeps instead of proving. That is the whole
  # point — the transport legs stay real and measured; see zisk-runner's ZISK_MOCK_SECS branch.
  # Only the MODEL crosses; the remote measures the work itself (it has the witness and the emulator) and
  # derives its own duration. Nothing here decides how long the mock takes.
  local mockenv=""
  if [[ -n "${ZISK_MOCK_SECS:-}" || -n "${ZISK_MOCK_PER_MSTEP:-}" ]]; then
    mockenv="${ZISK_MOCK_SECS:+ZISK_MOCK_SECS=$ZISK_MOCK_SECS }\
${ZISK_MOCK_PER_MSTEP:+ZISK_MOCK_PER_MSTEP=$ZISK_MOCK_PER_MSTEP }\
${ZISK_MOCK_SPEED:+ZISK_MOCK_SPEED=$ZISK_MOCK_SPEED }\
${ZISK_MOCK_JITTER:+ZISK_MOCK_JITTER=$ZISK_MOCK_JITTER }\
${ZISK_MOCK_DIST:+ZISK_MOCK_DIST=$ZISK_MOCK_DIST }"
  fi
  "${ssh[@]}" "set -o pipefail; ${logpref}${mockenv}ZISK_PROVE_BACKEND=$backend $remote_runner \
    --elf $ws/elfs/$elf_name \
    --input $ws/inputs/$in_name $hints_arg \
    --mode $mode \
    --output $ws/proofs/$base.proof.bin \
    --public-values $ws/proofs/$base.pv.bin \
    --report $ws/reports/$base.json 2>&1 | tee $ws/reports/$base.log"

  local t_run; t_run="$(date +%s.%N)"

  # ONE round trip instead of four. The four artifacts are tiny — a 480 B proof, three small text files — so
  # what those scp calls cost was not bandwidth but latency: a full request/response each, over a tunnel where
  # a round trip is expensive. Multiplexing removed the handshakes, not the round trips. tar streams all four
  # in a single exchange on the connection that is already open.
  #
  # Public values are best-effort (ZisK exposes them differently across versions), so tar is allowed to report
  # a missing member on stderr and still write the rest — hence 2>/dev/null on both ends rather than a check.
  echo "Retrieving artifacts into $run_dir ..."
  local getdir; getdir="$(mktemp -d)"
  local members="proofs/$base.proof.bin reports/$base.json reports/$base.log proofs/$base.pv.bin"
  if [[ -n "${PULL_VIA:-}" ]]; then
    # Same inversion as the witness, in the other direction: the tar stream rides a connection the PROVER opens
    # to us, not the tunnel. The payload is small, so what this buys is the round trip, not bandwidth — but at
    # this point the round trip IS the cost. The ssh command itself still goes through the tunnel; it is a few
    # hundred bytes.
    #
    # And since the round trip IS the cost, multiplex THIS connection — the opposite call from the input pull,
    # and not a contradiction of it. The rule the pull comment states is the one being applied: mux is for many
    # small round trips, not for one bulk stream. This is the small-round-trip case. Measured prover->box:
    # a fresh handshake is 1.09-1.22 s, a reused channel 0.19-0.20 s, and `retrieve` currently sits at a 1.6 s
    # median for ~1 KB of artifacts — i.e. it is almost entirely handshake. One control socket per PULL_VIA,
    # persisted across blocks, so only the first block of a run pays it.
    #
    # The socket lives on the PROVER (this ssh runs there), so the path must be short and prover-side: a unix
    # socket path is capped at 104 bytes on macOS and %C alone is 64 hex chars, which $ws/... would overflow.
    # PULL_BACK_SSH_OPTS overrides; set it to "$pull_opts" to go back to a fresh connection per block.
    local back_opts="${PULL_BACK_SSH_OPTS:-$pull_opts -o ControlMaster=auto -o ControlPath=/tmp/zisk-pb-%C -o ControlPersist=120}"
    local tarball="$getdir/artifacts.tar"
    if "${ssh[@]}" "cd $ws && tar cf - $members 2>/dev/null | ssh $back_opts '$PULL_VIA' \"cat > '$tarball'\"" \
       && [[ -s "$tarball" ]]; then
      tar xf "$tarball" -C "$getdir" 2>/dev/null; rm -f "$tarball"
    else
      echo "WARNING: direct push-back from $REMOTE failed — falling back to the tunnel." >&2
      "${ssh[@]}" "cd $ws && tar cf - $members 2>/dev/null" | tar xf - -C "$getdir" 2>/dev/null
    fi
  else
    "${ssh[@]}" "cd $ws && tar cf - $members 2>/dev/null" | tar xf - -C "$getdir" 2>/dev/null
  fi
  mv "$getdir/proofs/$base.proof.bin" "$run_dir/proof.bin"   2>/dev/null
  mv "$getdir/reports/$base.json"     "$run_dir/report.json" 2>/dev/null
  mv "$getdir/reports/$base.log"      "$run_dir/prove.log"   2>/dev/null
  mv "$getdir/proofs/$base.pv.bin"    "$run_dir/pv.bin"      2>/dev/null
  rm -rf "$getdir"
  # The proof is the one artifact whose absence must fail loudly: with four separate scp calls a missing proof
  # failed on its own line, and a single stream must not turn that into a silent empty run record.
  [[ -s "$run_dir/proof.bin" ]] || { echo "ERROR: no proof came back from $REMOTE — see $run_dir/prove.log" >&2; return 1; }

  local t_get; t_get="$(date +%s.%N)"
  # Per-phase timing, because attributing this by subtracting `prove` from the wall clock is how the transport
  # got blamed for 20-34 s that turned out to be a mixture of throughput and round trips. Whole seconds are
  # Tenths, not whole seconds: once transport is down to a couple of seconds, integer granularity is coarser
  # than the changes being made and a real improvement can read as no change at all. The clock is read on the
  # PRODUCER (Linux), so date +%N is available — the prover's macOS bash never sees these.
  # bash has no float arithmetic, so every subtraction goes through awk. One call, not four.
  local ph; ph="$(awk -v a="$t_start" -v b="$t_in" -v c="$t_run" -v d="$t_get" \
    'BEGIN{printf "%.1f %.1f %.1f %.1f %.1f", b-a, c-b, d-c, (b-a)+(d-c), d-a}')"
  local p_in p_rem p_get p_tr p_tot; read -r p_in p_rem p_get p_tr p_tot <<<"$ph"
  local in_bytes; in_bytes="$(stat -c %s "$INPUT" 2>/dev/null || stat -f %z "$INPUT" 2>/dev/null || echo 0)"
  local in_mbits; in_mbits="$(awk -v b="$in_bytes" -v s="$p_in" 'BEGIN{printf "%.1f", (s>0? b*8/s/1000000 : 0)}')"
  printf 'Timing    : input %ss (%s MB @ %s Mbit/s) · remote %ss · retrieve %ss · total %ss\n' \
    "$p_in" "$(awk -v b="$in_bytes" 'BEGIN{printf "%.1f", b/1048576}')" "$in_mbits" "$p_rem" "$p_get" "$p_tot"
  # Also as data, not just as a log line: these are the numbers that told us the transport was TWO problems,
  # and they belong in the run record so ethproofs-submit can carry them to the leaderboard. A phase timing
  # buried in prose gets re-derived by subtraction, which is exactly how it went wrong three times.
  # `transport` is the operator-facing figure: everything that is not the prover's own work.
  # Bytes too, so a slow `input` can be told apart from a big one. Without it a 35 s outlier is unattributable:
  # 15 MB in 35 s is a collapsed link, 150 MB in 35 s would be a normal one — and guessing between them is how
  # the transport got misdiagnosed twice. mbits is the number to look at; it should sit near the link rate.
  printf '{"input_secs":%s,"input_bytes":%s,"input_mbits":%s,"remote_secs":%s,"retrieve_secs":%s,"transport_secs":%s,"total_secs":%s,"pulled_direct":%s,"prefetched":%s}\n' \
    "$p_in" "$in_bytes" "$in_mbits" "$p_rem" "$p_get" "$p_tr" "$p_tot" \
    "$( [[ $pulled == 1 ]] && echo true || echo false )" \
    "$( [[ $prefetched == 1 ]] && echo true || echo false )" > "$run_dir/timing.json"

  # Bundle the local emulation profile (steps) if it exists, so the run record is
  # self-contained.
  local exec_report="${INPUT%.bin}.exec-report.json"
  [[ -f "$exec_report" ]] && cp "$exec_report" "$run_dir/exec-report.json"
  # A local emulation (execute_local) writes the guest's REAL public values next to the input, alongside the
  # exec-report. They are not the prover's output, so they land as expected_pv.bin and never overwrite
  # pv.bin — but they are genuine (the real guest, the real witness), which is what makes a root
  # cross-check meaningful even when the proof beside them is a mock.
  local local_pv="${INPUT%.bin}.pv.bin"
  [[ -f "$local_pv" ]] && cp "$local_pv" "$run_dir/expected_pv.bin"

  echo "Done. Run record: $run_dir/"
  echo "  proof.bin"
  echo "  report.json  (timings, proof_bytes, steps)"
  echo "  prove.log    (full ZisK proving trace)"
  echo "  env.txt      (GPU / host / versions)"
  echo "Verify with : ./run verify PROOF=$run_dir/proof.bin            (proof only — no ELF, no witness)"
  echo "  + root    : ./run verify PROOF=$run_dir/proof.bin EXPECTED_ROOT=<0x…|path>"
}

# verify_local — verify an existing PROOF. The cryptographic check needs NOTHING but
# the proof: the runner's `--mode verify --proof` is `cargo-zisk verify -p`, which
# carries its own verification key (~/.zisk, via ziskup). No ELF, no witness.
# That is the point — verification must run wherever the proof lands (the ethproofs
# mock on a laptop, a submitter, a third party), and a 7 MB witness must never be a
# prerequisite for it.
#
# On top of that, an OPTIONAL public-values cross-check — "is this proof about the
# state root the chain actually has?" — cheapest form first:
#
#   EXPECTED_ROOT=<0x…|file>   compare the post-state root committed in the proof's
#                              public values (first 32 bytes) with the expected root.
#                              32 bytes of input, witness-free. This is what a block
#                              proof actually claims, and what the ethproofs seam can
#                              afford to ship. `.post_state_root` files are accepted
#                              as-is (hex text, with or without 0x).
#   ELF + INPUT                legacy full cross-check: re-emulate the guest to
#                              recompute every public value. Only runs when BOTH are
#                              present — never required.
#
# PV defaults to the run record's pv.bin (fetched by prove_remote).
verify_local() {
  : "${PROOF:?set PROOF=path}"
  [[ -x "$RUNNER" ]] || { echo "ERROR: runner not found/executable: $RUNNER" >&2; return 1; }
  [[ -f "$PROOF"  ]] || { echo "ERROR: proof not found: $PROOF (run: ./run prove ...)" >&2; return 1; }

  local pdir; pdir="$(dirname "$PROOF")"
  # pv.bin is the PROVER's output; expected_pv.bin is what a local emulation of the guest committed. Prefer
  # the prover's when it exists, fall back to the emulated one, and say which was used — they support
  # different claims: "the proof is about the right state" vs "the guest computes the right state".
  local pv="${PV:-$pdir/pv.bin}" pv_kind="prover"
  local expected_pv="$pdir/expected_pv.bin"
  if [[ ! -f "$pv" && -f "$expected_pv" ]]; then pv="$expected_pv"; pv_kind="emulated"; fi

  echo "== verify =="
  echo "Proof     : $PROOF"

  # 1. The verification itself — proof only. SKIP_PROOF_CHECK=1 runs ONLY the cross-checks below, which is
  # how you interrogate a mock run: its proof is fake by construction, but its public values may be real.
  if [[ -n "${SKIP_PROOF_CHECK:-}" ]]; then
    echo "--- SKIPPING the cryptographic check (SKIP_PROOF_CHECK=1) — cross-checks only ---"
  else
    echo "--- verifying proof ---"
    "$RUNNER" --mode verify --proof "$PROOF" || {
      echo "ERROR: proof verification FAILED" >&2; return 1; }
  fi

  # 2. Root cross-check (witness-free). A mismatch means the proof is valid but about
  #    the WRONG state — a correctness failure, so it fails the verb.
  if [[ -n "${EXPECTED_ROOT:-}" ]]; then
    echo "--- cross-checking committed post-state root ---"
    local want; want="$(_norm_root "$EXPECTED_ROOT")"
    if [[ ! -f "$pv" ]]; then
      echo "WARN: no public values at $pv — cannot cross-check the root (set PV=<path>)" >&2
    elif [[ -z "$want" ]]; then
      echo "WARN: could not read an expected root from EXPECTED_ROOT=$EXPECTED_ROOT" >&2
    else
      # ZisK writes the guest output padded (Monad guest: 256 B, root in the first 32).
      local got; got="$(xxd -p -l 32 "$pv" | tr -d '\n')"
      if [[ "$got" == "$want" ]]; then
        echo "Root cross-check: OK (0x$got)  [$pv_kind public values]"
      else
        echo "ERROR: root MISMATCH — proof commits 0x$got, expected 0x$want" >&2
        return 1
      fi
    fi
  fi

  # 3. Legacy full PV cross-check, only when the ELF and witness happen to be here.
  if [[ -n "${ELF:-}" && -n "${INPUT:-}" && -f "${ELF:-}" && -f "${INPUT:-}" ]]; then
    echo "--- recomputing expected public values (local emulation) ---"
    if "$RUNNER" --elf "$ELF" --input "$INPUT" --mode execute --public-values "$expected_pv"; then
      if [[ -f "$pv" ]]; then
        cmp -s "$pv" "$expected_pv" \
          && echo "Full PV cross-check: OK" \
          || { echo "ERROR: public values differ from locally recomputed PV!" >&2; return 1; }
      fi
    else
      echo "WARN: could not recompute expected PV (emulation failed)" >&2
    fi
  fi

  [[ -f "$pv" ]] && echo "Public values: $pv"
  echo "Decode with  : ./run decode-pv GUEST=<name> PV=$pv"
}

# _norm_root <0xhex|path> — echo a bare lowercase 32-byte hex root, or nothing.
# Accepts a hex string, a file of hex text (`.post_state_root`), or 32 raw bytes.
_norm_root() {
  local v="$1" hex
  if [[ -f "$v" ]]; then
    if [[ "$(wc -c < "$v")" -eq 32 ]]; then hex="$(xxd -p "$v" | tr -d '\n')"
    else hex="$(tr -d '[:space:]' < "$v")"; fi
  else
    hex="$v"
  fi
  hex="${hex#0x}"; hex="${hex#0X}"
  hex="$(printf '%s' "$hex" | tr 'A-F' 'a-f')"
  [[ "$hex" =~ ^[0-9a-f]{64}$ ]] && printf '%s' "$hex"
}
