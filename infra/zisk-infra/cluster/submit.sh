#!/usr/bin/env bash
# submit.sh — prove a block on the ZisK cluster ON THE BOX, through the coordinator,
# using zisk-runner as the client, and save a COMPLETE run record under
# runs/<tag>-<ts>/: proof.bin · report.json (timings + proof_bytes) · prove.log ·
# env.txt (+ coordinator/worker logs).
#
#   ./submit.sh <elf> <input.bin> <hints> [prove-compressed|prove-plonk]
# NOTE: pass the RAW input (.bin) AND the hints — the prover needs both.
set -uo pipefail
cd "$(dirname "$0")"

ELF="${1:?usage: ./submit.sh <elf> <input.bin> <hints> [mode]}"
INPUT="${2:?usage: ./submit.sh <elf> <input.bin> <hints> [mode]}"
HINTS="${3:?usage: ./submit.sh <elf> <input.bin> <hints> [mode]}"
MODE="${4:-prove-compressed}"
abspath() { case "$1" in /*) echo "$1";; *) echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")";; esac; }
ELF="$(abspath "$ELF")"; INPUT="$(abspath "$INPUT")"; HINTS="$(abspath "$HINTS")"
[[ -f "$ELF" && -f "$INPUT" && -e "$HINTS" ]] || { echo "ERROR: elf/input/hints not found" >&2; exit 1; }

# Locate the runner: explicit $RUNNER, ~/zisk-runner, or the sibling in this repo.
if   [[ -n "${RUNNER:-}" && -x "${RUNNER:-}" ]]; then :
elif [[ -x "$HOME/zisk-runner" ]]; then RUNNER="$HOME/zisk-runner"
elif [[ -x "../zisk-runner" ]];   then RUNNER="$(cd .. && pwd)/zisk-runner"
else echo "ERROR: zisk-runner not found (set \$RUNNER, or put it at ~/zisk-runner)" >&2; exit 1; fi

export PATH="$HOME/.zisk/bin:$PATH"
API_PORT="${API_PORT:-7000}"
export ZISK_PROVE_BACKEND="${ZISK_PROVE_BACKEND:-remote}"
export ZISK_COORDINATOR_URL="${ZISK_COORDINATOR_URL:-http://127.0.0.1:$API_PORT}"

tag="$(basename "${INPUT%.bin}")"
run="runs/${tag}-$(date -u +%Y%m%d-%H%M%SZ)"
mkdir -p "$run"

{ echo "tag=$tag mode=$MODE date=$(date -u)"
  echo "elf=$ELF input=$INPUT hints=$HINTS"
  echo "backend=$ZISK_PROVE_BACKEND coordinator=$ZISK_COORDINATOR_URL"
  echo "zisk=$(cargo-zisk --version 2>/dev/null || echo '?')"
  echo "--- gpus ---"; nvidia-smi -L 2>/dev/null || echo "(no nvidia-smi)"; } > "$run/env.txt"

echo "== prove $tag ($MODE) via ZisK coordinator -> $run =="
RUST_LOG="${RUST_LOG:-info}" \
  "$RUNNER" --elf "$ELF" --input "$INPUT" --hints "$HINTS" --mode "$MODE" --skip-verify \
    --output "$run/proof.bin" \
    --report "$run/report.json" 2>&1 | tee "$run/prove.log"
rc=${PIPESTATUS[0]}
# --skip-verify so a flaky verify can't discard a good proof; verify the saved
# proof afterwards:  cargo-zisk verify -p <run>/proof.bin

# Bundle the cluster-side logs into the run record (skip with BUNDLE_LOGS=0).
if [[ "${BUNDLE_LOGS:-1}" != 0 ]]; then
  for f in logs/coordinator.log logs/worker.log; do
    [[ -f "$f" ]] && cp -f "$f" "$run/" 2>/dev/null || true
  done
fi

if [[ "$rc" == 0 ]]; then
  echo "OK — proof: $run/proof.bin ($(wc -c < "$run/proof.bin" 2>/dev/null || echo '?') bytes)"
else
  echo ">>> runner exited $rc (proof may be incomplete) — see $run/prove.log"
fi
echo "Run record: $run/"
exit "$rc"
