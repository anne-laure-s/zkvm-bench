#!/usr/bin/env bash
# submit.sh — prove a block on the cluster (through the network-gateway) using our
# sp1-runner as the client, and save a COMPLETE run record under runs/<tag>-<ts>/:
#   proof.bin · pv.bin · vkey.txt · report.json (timings + proof_bytes) · prove.log
#   + the cluster-side logs (coordinator / gateway / cpu-node* / gpu*).
# Single-runner parity, but the backend is the 16-GPU cluster.
#
#   ./submit.sh <elf> <raw-witness.bin> [prove-compressed|prove-core|prove-groth16]
# NOTE: 2nd arg is the RAW witness (.bin) — the runner wraps it itself (not .stdin).
set -uo pipefail
cd "$(dirname "$0")"

ELF="${1:?usage: ./submit.sh <elf> <raw-witness.bin> [mode]}"
WIT="${2:?usage: ./submit.sh <elf> <raw-witness.bin> [mode]}"
MODE="${3:-prove-compressed}"
ELF="$(cd "$(dirname "$ELF")" && pwd)/$(basename "$ELF")"
WIT="$(cd "$(dirname "$WIT")" && pwd)/$(basename "$WIT")"
[[ -f "$ELF" && -f "$WIT" ]] || { echo "ERROR: elf/witness not found" >&2; exit 1; }

# Find the runner binary wherever it is: explicit $RUNNER, the stripped copy we
# keep to save disk (~/sp1-runner-bin), or the normal cargo target path.
if [[ -n "${RUNNER:-}" && -x "${RUNNER:-}" ]]; then :
elif [[ -x "$HOME/sp1-runner-bin" ]]; then RUNNER="$HOME/sp1-runner-bin"
elif [[ -x "$HOME/sp1-runner/target/release/sp1-runner" ]]; then RUNNER="$HOME/sp1-runner/target/release/sp1-runner"
else echo "ERROR: runner not found (looked at \$RUNNER, ~/sp1-runner-bin, ~/sp1-runner/target/release/sp1-runner)" >&2; exit 1; fi
GATEWAY="${GATEWAY:-http://localhost:50061}"
# self-hosted gateway is AUTH_MODE=none, but the SDK still requires a key to exist.
NETWORK_PRIVATE_KEY="${NETWORK_PRIVATE_KEY:-0x0000000000000000000000000000000000000000000000000000000000000001}"

tag="$(basename "${WIT%.bin}")"
run="runs/${tag}-$(date -u +%Y%m%d-%H%M%SZ)"
mkdir -p "$run"

{ echo "tag=$tag mode=$MODE date=$(date -u)"
  echo "elf=$ELF witness=$WIT gateway=$GATEWAY"
  echo "--- cpu ---"
  lscpu 2>/dev/null | grep -iE 'model name|^socket|^core|^thread|^cpu\(s\)|^numa|max mhz' \
    || grep -m1 'model name' /proc/cpuinfo 2>/dev/null
  echo "nproc=$(nproc 2>/dev/null)"
  echo "--- mem / shm ---   (shm gates core-worker pipelining — see 03-start.sh cause #6)"
  free -h 2>/dev/null | head -2
  df -h /dev/shm 2>/dev/null
  echo "--- gpus ---"; nvidia-smi -L 2>/dev/null || echo "(no nvidia-smi)"; } > "$run/env.txt"

echo "== prove $tag ($MODE) via cluster gateway -> $run =="
# The runner (--features network) EXPLICITLY skips the SDK's pre-submit local CPU re-execution
# (skip_simulation + u64::MAX cycle/gas limits, set on the prove request — hosted() alone does NOT
# do it on the blocking API in sp1-sdk 6.2.4; see sp1-runner/src/main.rs). That local pass is
# pointless on a self-hosted/reserved cluster and used to add tens of seconds of box-CPU latency per
# proof before any GPU started. REBUILD the runner (./04-build-runner.sh) for this to take effect.

# Peak-resource sampler (host RAM + /dev/shm + GPU VRAM) DURING the proof. env.txt above is only a
# PRE-proof snapshot; this captures the real peak while proving. A background loop — managed by THIS
# script, nothing for the operator to launch in parallel — samples every 1s; after the runner exits
# we fold the max into report.json. VRAM = the busiest single GPU's memory.used at each tick (so a
# value near ~32 GB means at least one card is VRAM-pressured).
base_ram_mib=$(free -m 2>/dev/null | awk '/^Mem:/{print $3}')
_memsamp=$(mktemp)
( while :; do
    printf '%s %s %s\n' \
      "$(free -m 2>/dev/null | awk '/^Mem:/{print $3}')" \
      "$(df -m /dev/shm 2>/dev/null | awk 'END{print $3}')" \
      "$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | sort -rn | head -1)"
    sleep 1
  done ) > "$_memsamp" 2>/dev/null &
_memsamp_pid=$!

SP1_PROVER=network \
NETWORK_RPC_URL="$GATEWAY" \
NETWORK_PRIVATE_KEY="$NETWORK_PRIVATE_KEY" \
RUST_LOG="${RUST_LOG:-info}" \
  "$RUNNER" --elf "$ELF" --input "$WIT" --mode "$MODE" --skip-verify \
    --output "$run/proof.bin" --public-values "$run/pv.bin" \
    --vkey "$run/vkey.txt" --report "$run/report.json" 2>&1 | tee "$run/prove.log"
rc=${PIPESTATUS[0]}

# Stop the sampler, compute peaks, fold them into report.json + env.txt. free/df report MiB
# (1024²B); we also print GB (10⁹B). delta = peak − idle baseline = what the proof itself added.
kill "$_memsamp_pid" 2>/dev/null; wait "$_memsamp_pid" 2>/dev/null
peak_ram_mib=$(awk '{if($1>m)m=$1} END{print m+0}' "$_memsamp" 2>/dev/null)
peak_shm_mib=$(awk '{if($2>m)m=$2} END{print m+0}' "$_memsamp" 2>/dev/null)
peak_vram_mib=$(awk '{if($3>m)m=$3} END{print m+0}' "$_memsamp" 2>/dev/null)
rm -f "$_memsamp"
delta_ram_mib=$(( ${peak_ram_mib:-0} - ${base_ram_mib:-0} )); (( delta_ram_mib < 0 )) && delta_ram_mib=0
peak_ram_gib=$(awk "BEGIN{printf \"%.1f\", ${peak_ram_mib:-0}/1024}")
peak_ram_gb=$(awk "BEGIN{printf \"%.0f\", ${peak_ram_mib:-0}*1048576/1e9}")
delta_ram_gib=$(awk "BEGIN{printf \"%.1f\", $delta_ram_mib/1024}")
peak_shm_gib=$(awk "BEGIN{printf \"%.1f\", ${peak_shm_mib:-0}/1024}")
peak_vram_gib=$(awk "BEGIN{printf \"%.1f\", ${peak_vram_mib:-0}/1024}")
echo "Peak host RAM : ${peak_ram_mib:-?} MiB (${peak_ram_gib} GiB ≈ ${peak_ram_gb} GB) · Δ vs idle ${delta_ram_mib} MiB (${delta_ram_gib} GiB) · peak /dev/shm ${peak_shm_mib:-?} MiB (${peak_shm_gib} GiB)"
echo "Peak GPU VRAM : ${peak_vram_mib:-?} MiB (${peak_vram_gib} GiB) — busiest single GPU (32 GB/carte)"
echo "--- peak during proof: ram=${peak_ram_gib}GiB (Δ ${delta_ram_gib}GiB vs idle) · shm=${peak_shm_gib}GiB · vram=${peak_vram_gib}GiB ---" >> "$run/env.txt"
if command -v jq >/dev/null 2>&1 && [[ -f "$run/report.json" ]]; then
  _t=$(mktemp)
  jq --argjson pr "${peak_ram_mib:-0}" --argjson br "${base_ram_mib:-0}" --argjson dr "$delta_ram_mib" --argjson ps "${peak_shm_mib:-0}" --argjson pv "${peak_vram_mib:-0}" \
     '. + {peak_host_ram_mib:$pr, baseline_host_ram_mib:$br, delta_host_ram_mib:$dr, peak_dev_shm_mib:$ps, peak_gpu_vram_mib:$pv}' \
     "$run/report.json" > "$_t" 2>/dev/null && mv "$_t" "$run/report.json" || rm -f "$_t"
fi
# NB: --skip-verify so a flaky NETWORK verify can't discard a good proof (the
# runner verifies BEFORE saving). Verify the saved proof locally afterwards:
#   sp1-runner --elf rsp.elf --mode verify --proof <run>/proof.bin --public-values <run>/pv.bin

# Bundle the cluster-side logs into the run record. ~120 MB/run, so sweeps set BUNDLE_LOGS=0
# to skip it (tuning only needs report.json's prove_secs) — keeps the 32 GB disk from filling.
if [[ "${BUNDLE_LOGS:-1}" != 0 ]]; then
  echo "Bundling cluster logs into $run/ ..."
  for f in logs/api.log logs/coordinator.log logs/network-gateway.log logs/cpu-node-*.log logs/gpu*.log; do
    [[ -f "$f" ]] && cp -f "$f" "$run/" 2>/dev/null || true
  done
fi

if [[ "$rc" == 0 ]]; then
  echo "OK — proof saved: $run/proof.bin ($(wc -c < "$run/proof.bin" 2>/dev/null || echo '?') bytes)"
else
  echo ">>> runner exited $rc (proof NOT saved) — see $run/prove.log"
fi
echo "Run record: $run/   (copy back: scp -P <port> -r root@ssh.vast.ai:~/cluster-native/$run .)"
exit "$rc"
