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
  # THE PERMANENT CHANNEL, when it is up. `witness-pump run` is a drop-in for `ssh <prover> CMD` — stdout to
  # stdout, stderr to stderr, the command's own exit status — so every call site below is untouched and only
  # this array changes.
  #
  # What it buys is not speed. The prover dials that channel, so nothing here needs a private key ON THE BOX,
  # and the hourly `find ~/.ssh -type f -name 'id_*' -delete` stops taking the prover down with it. The box
  # still drives and still holds the clock: it sends the command and awaits the reply exactly as before, so
  # every phase timing below is measured the same way, and the runner's log still streams live on stderr.
  #
  # Fallback is the plain ssh above, chosen per call because the channel can drop between blocks. A missing
  # socket is not an error here — it is the pre-channel behaviour, which works whenever the key does.
  local exec_sock="${PUMP_EXEC_SOCK:-$HOME/.zisk-exec.sock}"
  local pump_local="${WITNESS_PUMP_LOCAL:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/witness-pump}"
  # A SOCKET FILE IS NOT A CHANNEL. `-S` only says "this path is a socket", and an agent whose peer died
  # leaves the file behind: connecting then gets ECONNREFUSED, every block dies on the workspace probe, and
  # the plain-ssh fallback that exists for exactly this case never runs. Seen 2026-08-19: 26 consecutive
  # blocks failed in 1 s each with "cannot prepare remote workspace" while ssh itself was perfectly fine.
  # So prove the channel answers before choosing it — one trivial round trip against a 12 s slot — and
  # degrade to ssh instead of failing the block.
  if [[ -S "$exec_sock" && -x "$pump_local" ]]; then
    if PUMP_EXEC_SOCK="$exec_sock" "$pump_local" run "true" >/dev/null 2>&1; then
      ssh=(env "PUMP_EXEC_SOCK=$exec_sock" "$pump_local" run)
      echo "Control over the persistent exec channel (no box-side key needed)."
    else
      echo "WARNING: $exec_sock exists but the exec channel does not answer — falling back to ssh," >&2
      echo "         which needs a key on this box. Restart the channel with rtp-up." >&2
    fi
  fi
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
  # trip. Folding the hash in here beats both a separate call and a local cache: one exchange instead of two,
  # and no stale-cache failure mode at an ELF swap — the one moment the hash actually matters.
  # Line 1 is the path, line 2 the hash (empty if the ELF is not there yet, which correctly forces an upload).
  # Timed from HERE, before the first exchange with the prover: the workspace call is part of what `input`
  # costs, and starting the clock after it would flatter the very change being measured.
  # %N (Linux — core.sh runs on the producer box): tenths, because at ~2 s whole seconds hide real gains.
  # THE FETCH RIDES THIS ROUND TRIP TOO. With the transfer itself fast, the fixed cost around it is the biggest
  # remaining term: at 90-135 ms RTT every separate box->prover exchange is ~0.19 s, and asking the pump for the
  # block is otherwise an exchange of its own. The prover already knows everything needed to decide —
  # it has just stat'd the input — so it decides and fetches in the same shell, and reports the verdict on a
  # fourth line. One exchange instead of two, on every block.
  #
  # The pump attempt belongs HERE and not in the `PULL_VIA` branch further down: the pump does not use PULL_VIA
  # at all (it has its own PUMP_BOX), so gating it on PULL_VIA leaves it silently untried on exactly the
  # configuration that wants it — PULL_VIA unset, WITNESS_PUMP set.
  local abs_input; abs_input="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
  local lin_size; lin_size="$(stat -c %s "$INPUT" 2>/dev/null || stat -f %z "$INPUT" 2>/dev/null || echo 0)"
  local pump_cmd=""
  [[ -n "${WITNESS_PUMP:-}" ]] && pump_cmd="${PUMP_SOCK:+PUMP_SOCK=$PUMP_SOCK }$WITNESS_PUMP"
  local t_start; t_start="$(date +%s.%N)"
  # AND THE SWEEP RIDES IT TOO — the safety net behind the per-block deletions further down. Those cover the
  # normal path; this covers what no per-block rule can: a killed prover, a failed submit that never reaches its
  # own cleanup, a run interrupted between the prove and the retrieve. Without it the workspace only trends to
  # empty, and a leak that needs a failure to appear is one that reaches tens of GB before anyone looks.
  #
  # Age, not block number, is the criterion, and that is deliberate. A number-based rule ("delete anything below
  # the current block") looks tighter but breaks prefetch: prove-farm ships the NEXT block ahead of time, so a
  # future input is legitimately present and a naive sweep would delete the very thing the prefetch just paid for.
  # In a pipeline where a block lives 12 s, nothing an hour old is in flight — including a prefetched input.
  # `find -delete` is one pass in C, not a shell loop, so this stays cheap even against the 10k-file backlog it
  # clears on its first run. elfs/ is never touched: the guest and the zkVM setup are what the prover legitimately
  # keeps, and they are the whole of what survives.
  local ws_keep_min="${ZISK_WS_KEEP_MIN:-60}"
  local ws_meta ws_abs rsum rinsize fetched
  ws_meta="$("${ssh[@]}" "mkdir -p \"$ws\"/elfs \"$ws\"/inputs \"$ws\"/proofs \"$ws\"/reports && cd \"$ws\" \
&& { find inputs proofs reports -type f -mmin +$ws_keep_min -delete 2>/dev/null || true; } \
&& WS=\"\$(pwd)\" && echo \"\$WS\" \
&& { sha256sum elfs/$elf_name 2>/dev/null || shasum -a 256 elfs/$elf_name 2>/dev/null; } | awk '{print \$1}' \
&& SZ=\"\$(stat -c %s inputs/$in_name 2>/dev/null || stat -f %z inputs/$in_name 2>/dev/null || echo 0)\" \
&& echo \"\$SZ\" \
&& if [ \"\$SZ\" = '$lin_size' ] && [ '$lin_size' != 0 ]; then echo PREFETCHED; \
elif [ -n '$pump_cmd' ]; then $pump_cmd get '$abs_input' \"\$WS/inputs/$in_name\" >/dev/null 2>&1 \
&& echo PUMP || echo NOFETCH; \
else echo NOFETCH; fi \
&& { echo -n 'date    : '; date -u; \
echo -n 'host    : '; uname -a; \
echo -n 'cpus    : '; nproc 2>/dev/null || sysctl -n hw.ncpu; \
echo -n 'zisk    : '; cargo-zisk --version 2>/dev/null || echo '(no cargo-zisk)'; \
echo '--- gpu ---'; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv 2>/dev/null || echo '(no nvidia-smi)'; } 2>&1")" \
    || { echo "ERROR: cannot prepare remote workspace '$ws'" >&2; return 1; }
  ws_abs="$(printf '%s\n' "$ws_meta" | sed -n 1p)"
  rsum="$(printf '%s\n' "$ws_meta" | sed -n 2p)"
  # Line 3: the size the prover ALREADY has for this input, 0 if absent. This is what makes overlap possible —
  # a witness shipped ahead of time by prove-farm's prefetcher costs nothing here.
  rinsize="$(printf '%s\n' "$ws_meta" | sed -n 3p)"
  # Line 4: PREFETCHED (already there) | PUMP (streamed over the persistent channel) | NOFETCH (fall through
  # to scp). NOFETCH is not an error — it is the designed fallback, and it must stay cheap to reach.
  fetched="$(printf '%s\n' "$ws_meta" | sed -n 4p)"
  # Lines 5+: the remote environment for the run record. It rides these lines rather than an exchange of its own,
  # because it is constant across a run — host, cpu count, zisk version, GPU model — and a per-block snapshot of
  # unchanging facts costs a round trip plus two process spawns that simply fail on a Mac prover (`cargo-zisk`,
  # `nvidia-smi`). Only `date` varies, and it is stamped here anyway.
  # A round trip is ~0.19 s at this RTT; that is what a per-block snapshot of unchanging facts was costing.
  local rem_env; rem_env="$(printf '%s\n' "$ws_meta" | sed -n '5,$p')"
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
  # REMOTE. On the witness box, PULL_VIA=<user>@<witness-box> turns 7-15 MB from ~15 s into ~3 s.
  #
  # The remote runs this non-interactively, so it needs its own key for us — no agent will be present. On
  # failure we say so loudly and fall back to pushing: a slow pipeline beats a stopped one, but a silent
  # fallback would let a broken config masquerade as a working optimisation.
  local pulled=0 prefetched=0
  # Declared at FUNCTION SCOPE, never inside a branch, even though only the scp fallback reads it. Inside the
  # `elif` that pulls the input it is unreachable for a PREFETCHED input — that takes the first branch — and
  # `set -u` then kills the retrieve step at the tar with "pull_opts: unbound variable". The failure lands on
  # exactly the blocks the prefetcher SUCCEEDED on, survives every retry (the input still sits on the prover and
  # matches on size again), and reads as a working feature in the log: `(prefetched N …)`, then
  # `<< N FAIL (rc=1 no-report no-proof)` eight seconds later — 0 % effective while 100 % fatal.
  local pull_opts="${PULL_SSH_OPTS:--C -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
  # Already there, byte-for-byte the same size? Then a prefetcher shipped it while the previous block was being
  # proved, and the transfer has already happened off the critical path. Size, not hash: a hash would cost the
  # very round trip this is avoiding, and these names are content-addressed by block — a same-named input of
  # the same length is the same witness.
  local via="scp"
  if [[ "$fetched" == PREFETCHED ]]; then
    echo "Input already on the prover (prefetched, $lin_size B) — no transfer on the critical path."
    prefetched=1; pulled=1; via="prefetched"
  elif [[ "$fetched" == PUMP ]]; then
    pulled=1; via="pump"
    echo "Pulled input over the persistent channel (setup paid once, and on the workspace round trip)."
  elif [[ -n "${PULL_VIA:-}" ]]; then
    [[ -n "$pump_cmd" ]] && echo "NOTE: the pump did not serve this block — using scp." >&2
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
    # WITNESS_PUMP — the one thing that DOES move this number: a connection that stays open across blocks.
    #
    # What the ~2 s of per-block setup buys nobody: TCP, banner, key exchange, auth, ~15 round trips at
    # 90-135 ms RTT, repaid on every block because every block spawns a fresh scp. A long-lived channel pays
    # it once. Measured byte-verified on the real link, 3 witnesses of ~10 MB, interleaved:
    #   one connection per file : 12.30 s median -> 4.10 s/witness, 19.5 Mbit/s
    #   ONE streaming connection:  8.19 s median -> 2.73 s/witness, 29.3 Mbit/s  (= the link's own rate)
    # End to end through the pump itself: 2.36-2.57 s per witness at 32.7-37.8 Mbit/s. **~1.6 s per block.**
    #
    # This is NOT what ControlMaster does, which is why that measured neutral-to-harmful above: there the
    # transfer is a separate process relaying every byte through the master over a unix socket. Here the
    # long-lived ssh IS the data path — one process, no relay. See infra/zisk-infra/witness-pump.
    #
    # The pump is tried on the workspace round trip above, not here — see the fold there. Its failure is never
    # fatal: no pump, stale socket, dead channel or a pruned witness all report NOFETCH and land in this branch.
    # Worst case is the old speed, not a stalled block.
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
    # Collected on the workspace round trip, not on one of its own — see rem_env above.
    printf '%s\n' "$rem_env"
  } > "$run_dir/env.txt"

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
${ZISK_MOCK_DIST:+ZISK_MOCK_DIST=$ZISK_MOCK_DIST }\
${ZISK_MOCK_PROOF_BYTES:+ZISK_MOCK_PROOF_BYTES=$ZISK_MOCK_PROOF_BYTES }"
  fi
  # THE ARTIFACTS COME BACK ON THIS SAME EXCHANGE. Fetching them with a second call after the run returns costs a
  # full round trip (~0.19 s at this RTT) to ask for something the prover has already finished writing. The split
  # is only in the code: the runner's own output goes to STDERR (so it still streams live, and prove-farm captures
  # both anyway), leaving STDOUT free to carry the tar. One exchange, both jobs.
  #
  # Note what this does NOT fix, because it is worth knowing: the proof still travels prover -> box, and in the
  # mock topology it then travels back through the tunnel when ethproofs-submit POSTs it to the mock running on
  # the prover itself. That byte detour is ~480 B mock / 381 KB real, i.e. ~0.1 s — cheap next to moving the run
  # record off the box, which would break the single-clock discipline rtp-latency.py depends on (t_proved is the
  # record's mtime, on the same clock as the producer's stamps).
  echo "Retrieving artifacts into $run_dir ..."
  local getdir; getdir="$(mktemp -d)"
  local members="proofs/$base.proof.bin reports/$base.json reports/$base.log proofs/$base.pv.bin"
  # PROVER-SIDE LOGS THE RUNNER DOES NOT SEE. reports/$base.log is the runner's own output; when the proof comes
  # from a cluster, the diagnosis lives in the coordinator's and workers' logs instead, and those stay on the
  # prover — so a failure inside the cluster leaves the box with the runner's view and nothing underneath it.
  # PROVER_LOG_FILES bundles their tails into the tarball the artifacts already ride: no extra round trip, and
  # they land in the run record beside the proof they belong to.
  #
  # Off by default, and TAILED rather than copied whole: these files grow without bound, and a prove is the worst
  # moment to move megabytes over the link the witness is already using. Turn it on when the proof takes long
  # enough that a few hundred lines are free — which is exactly the real-prover case.
  local plog_files="${PROVER_LOG_FILES:-}" plog_tail="${PROVER_LOG_TAIL:-200}" plog_cmd=""
  if [[ -n "$plog_files" ]]; then
    plog_cmd="{ for f in $plog_files; do [ -r \"\$f\" ] || continue; echo \"-- \$f (last $plog_tail lines) --\"; tail -n $plog_tail \"\$f\"; echo; done; } > $ws/reports/$base.prover.log 2>&1 || true;"
    members="$members reports/$base.prover.log"
  fi
  # AND THE ARTIFACTS COME BACK OFF THE TUNNEL. Everything on the box->prover ssh, in BOTH directions, is
  # TCP-in-TCP through the reverse forward: 3.8 Mbit/s against 25 direct. Sending the tar on that connection's
  # stdout was free while a mock proof was 480 bytes; at a realistic 381 KB it is ~0.82 s. The pump already
  # holds a DIRECT prover->box channel, so the prover pushes the tarball up it and pays only the ack.
  # Measured push of 381 KB: 0.53 s (5.9 Mbit/s — a transfer this small never leaves slow-start, so it does not
  # reach the link's 25). The 0.82 s it replaces is CALCULATED from the tunnel rate, not measured today: the
  # first live run should confirm it in `retrieve_secs` before this is believed.
  #
  # Fallback stays exactly the old path: if the push fails the prover cats the tarball to stdout instead, so a
  # broken pump costs the tunnel's speed, never the block.
  local box_tar="$getdir/pushed.tar"
  local push_cmd="cat $ws/art.tar"
  [[ -n "$pump_cmd" ]] && push_cmd="$pump_cmd put $ws/art.tar '$box_tar' >&2 && echo PUSHED >&2 || cat $ws/art.tar"
  # THE PROVER KEEPS NOTHING — it is a compute surface, not an archive. Everything per-block is removed on the
  # exchange that last needed it, so the workspace returns to elfs/ + the zkVM setup after every block. No extra
  # round trip pays for this: the deletions ride exchanges that already happen.
  #
  # The scale this is worth: a block leaves 7.46 MB behind it, of which the witness is 7.4 — so the input below
  # is 99 % of it — and at PROVE_EVERY=1 that is 54 GB/day.
  #
  # The INPUT is unconditionally dead here: the prove has run, and nothing reads it again. A retry re-fetches it,
  # which costs one transfer on a rare path — the right trade against carrying every witness forever.
  #
  # The ARTIFACTS are only kept when REMOTE_SUBMIT will read them off the prover to POST without sending the
  # proof back through the tunnel; that path deletes them itself once the submission has copied them out. When
  # it is unset they go here, because the box already holds all four — the `mv`s below land them in $run_dir and
  # the check after that FAILS THE BLOCK if the proof is missing. So the prover's copies are never the last copy.
  # ABSOLUTE paths, unlike the tar's member list which must stay relative to be extractable by name. The `rm`
  # shares a `;`-separated line with `cd $ws`, so a relative list would resolve against $HOME if that cd ever
  # failed — pointing a recursive delete at the wrong directory. $ws is absolute here ($ws_abs, resolved above).
  local ws_clean="$ws/inputs/$in_name" m
  [[ $have_hints == 1 ]] && ws_clean="$ws_clean $ws/inputs/$hints_name"
  if [[ -z "${REMOTE_SUBMIT:-}" ]]; then
    for m in $members; do ws_clean="$ws_clean $ws/$m"; done
  fi
  "${ssh[@]}" "set -o pipefail; { ${logpref}${mockenv}ZISK_PROVE_BACKEND=$backend $remote_runner \
    --elf $ws/elfs/$elf_name \
    --input $ws/inputs/$in_name $hints_arg \
    --mode $mode \
    --output $ws/proofs/$base.proof.bin \
    --public-values $ws/proofs/$base.pv.bin \
    --report $ws/reports/$base.json 2>&1 | tee $ws/reports/$base.log; } >&2
$plog_cmd cd $ws && tar cf - $members 2>/dev/null > $ws/art.tar; $push_cmd; rm -rf $ws/art.tar $ws_clean" \
    > "$getdir/stream.tar"
  # stderr is deliberately NOT redirected: it now carries the prover's own log, streamed live. Silencing it here
  # would have thrown away every line the prover prints — the whole reason the log moved to stderr was to keep
  # it visible once stdout was needed for the tar.
  # Whichever arrived: the pushed tarball (direct channel) or the streamed one (tunnel fallback).
  if [[ -s "$box_tar" ]]; then
    tar xf "$box_tar" -C "$getdir" 2>/dev/null; rm -f "$box_tar"
  elif [[ -s "$getdir/stream.tar" ]]; then
    tar xf "$getdir/stream.tar" -C "$getdir" 2>/dev/null
  fi
  rm -f "$getdir/stream.tar"

  # `|| true` ON EVERY ONE, and it is not defensive noise. This runs under `set -euo pipefail` (from
  # infra/zisk-infra/run), so a `mv` of a file that is not there is a failing simple command that ENDS THE
  # SCRIPT — and with `2>/dev/null` on it, silently. Two of these are legitimately absent:
  #   * prover.log — only produced when PROVER_LOG_FILES asked for it;
  #   * pv.bin     — `cargo-zisk remote prove` (the cluster path) does not emit public values at all.
  # So on a REAL prover the function died right here, every block: proof.bin, report.json and prove.log were
  # already in the run record, timing.json was never written, nothing was printed, and prove_remote returned
  # 1. cli/prove-farm read that as FAIL, re-queued the block and retried for ever — while a valid, verified
  # 414 KB proof sat in the run dir it had just fetched. Measured 2026-08-20: every real block, all afternoon.
  # The one artifact whose absence must be fatal is proof.bin, and that has its own check below, with a
  # message. Silence is the failure mode this line has to stop producing.
  mv "$getdir/proofs/$base.proof.bin" "$run_dir/proof.bin"   2>/dev/null || true
  mv "$getdir/reports/$base.json"     "$run_dir/report.json" 2>/dev/null || true
  mv "$getdir/reports/$base.log"      "$run_dir/prove.log"   2>/dev/null || true
  mv "$getdir/reports/$base.prover.log" "$run_dir/prover.log"  2>/dev/null || true
  mv "$getdir/proofs/$base.pv.bin"    "$run_dir/pv.bin"      2>/dev/null || true
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
  # `remote` CHANGED MEANING when the run and the artifact fetch became one exchange, and the change is worth
  # stating because it makes numbers recorded before it shift by ~0.2 s:
  #   before — remote = wall time of the run exchange (so it included the ssh round trip that launched it)
  #   now    — remote = the PROVER'S OWN reported duration, read from the report.json it just sent back
  #            retrieve = the rest of that exchange (the launch round trip, the tar, the artifact bytes)
  # The phase boundary survives the merge because the prover already measures itself; without that it would
  # have been lost, and a `retrieve` regression would have surfaced as "the prover got slower" — which is
  # exactly how 1.6 s of pure ssh handshake hid in this pipeline for weeks.
  # If report.json is missing (a failed prove), there is nothing to split on: attribute the whole exchange to
  # remote and leave retrieve at 0 rather than inventing a division.
  local prover_secs; prover_secs="$(python3 - "$run_dir/report.json" <<'PY' 2>/dev/null || echo ""
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    v=d.get("prove_secs") or d.get("total_secs") or d.get("elapsed_secs")
    print(f"{float(v):.3f}" if v is not None else "")
except Exception:
    print("")
PY
)"
  local ph; ph="$(awk -v a="$t_start" -v b="$t_in" -v d="$t_get" -v ps="${prover_secs:-}" \
    'BEGIN{ w=d-b; rem=(ps=="")?w:ps; get=w-rem; if (get<0) get=0;
            printf "%.1f %.1f %.1f %.1f %.1f", b-a, rem, get, (b-a)+get, d-a }')"
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
  # remote_ws / remote_base: WHERE the prover's own copies of these artifacts live. Recorded because the
  # submission can be made to happen ON the prover (see cli/prove-farm's submit_one) so the 508 KB base64 proof
  # never crosses the tunnel — and the naming convention that produces `$base` belongs here, in the one place
  # that already owns it. Without this, prove-farm would have to re-derive it and the convention would live twice.
  printf '{"input_secs":%s,"input_bytes":%s,"input_mbits":%s,"remote_secs":%s,"retrieve_secs":%s,"transport_secs":%s,"total_secs":%s,"pulled_direct":%s,"prefetched":%s,"remote_ws":"%s","remote_base":"%s"}\n' \
    "$p_in" "$in_bytes" "$in_mbits" "$p_rem" "$p_get" "$p_tr" "$p_tot" \
    "$( [[ $pulled == 1 ]] && echo true || echo false )" \
    "$( [[ $prefetched == 1 ]] && echo true || echo false )" \
    "$ws" "$base" > "$run_dir/timing.json"

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

  # 2b. EVERY public value, not only the post-state root. The guest commits three — pre-state root,
  #     post-state root, block hash — and checking one of them leaves two unchecked: a run that commits
  #     nothing at all (all-zero public values, which is what a guest built against a different zkVM
  #     toolchain than the emulator produces) passes an EXPECTED_ROOT check only because EXPECTED_ROOT was
  #     not set. cli/check-pv holds the layout and the comparison, and the submission path gates on the same
  #     file, so this verb and the pipeline agree by construction.
  #     Needs a block number: taken from the run record's report.json. RPC completes what the caller did not
  #     supply; without one, the two roots still check against EXPECTED_ROOT/the manifest.
  local checkpv; checkpv="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/cli/check-pv"
  local vblock="${BLOCK:-}"
  if [[ -z "$vblock" && -f "$pdir/report.json" ]]; then
    vblock="$(sed -n 's/.*"block"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$pdir/report.json" | head -1)"
  fi
  if [[ -x "$checkpv" && -f "$pv" && -n "$vblock" ]]; then
    echo "--- cross-checking ALL public values (block $vblock) ---"
    "$checkpv" --pv "$pv" --block "$vblock" ${MANIFEST:+--manifest "$MANIFEST"} ${RPC:+--rpc "$RPC"} \
               ${EXPECTED_ROOT:+--post "$EXPECTED_ROOT"}
    case $? in
      0) ;;
      1) echo "ERROR: the committed public values are not this block's — see above" >&2; return 1 ;;
      *) echo "WARN: could not establish ground truth for every public value (set RPC=<url> or MANIFEST=<csv>)" >&2 ;;
    esac
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
