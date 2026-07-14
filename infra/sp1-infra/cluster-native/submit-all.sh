#!/usr/bin/env bash
# submit-all.sh — prove EVERY raw witness (*.bin) in a directory, sequentially.
# Each proof uses the whole 16-GPU cluster, so we run them one at a time.
# Each produces its own run record (runs/<tag>-<ts>/) via submit.sh.
#
#   ./submit-all.sh <elf> <dir-with-witnesses> [prove-compressed|prove-core|prove-groth16]
#   ./submit-all.sh ~/rsp.elf ~/witnesses
set -uo pipefail
cd "$(dirname "$0")"

ELF="${1:?usage: ./submit-all.sh <elf> <dir> [mode]}"
DIR="${2:?usage: ./submit-all.sh <elf> <dir> [mode]}"
MODE="${3:-prove-compressed}"
[[ -d "$DIR" ]] || { echo "ERROR: not a directory: $DIR" >&2; exit 1; }

ok=(); fail=()
for w in "$DIR"/*.bin; do
  [[ -f "$w" ]] || continue
  case "$w" in *.pv.bin) continue ;; esac   # skip public-values, keep only witnesses
  echo
  echo "==================== $(basename "$w") ===================="
  if ./submit.sh "$ELF" "$w" "$MODE"; then ok+=("$(basename "${w%.bin}")"); else fail+=("$(basename "${w%.bin}")"); fi
done

echo
echo "==================== summary ===================="
echo "OK   (${#ok[@]}): ${ok[*]:-—}"
echo "FAIL (${#fail[@]}): ${fail[*]:-—}"
echo "Run records in: runs/   (copy back: scp -P <port> -r root@ssh.vast.ai:~/cluster-native/runs .)"
