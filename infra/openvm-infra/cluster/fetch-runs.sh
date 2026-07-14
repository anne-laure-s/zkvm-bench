#!/usr/bin/env bash
# fetch-runs.sh — pull the box's cluster/runs/ records (report.json · proof.json · worker-*.log ·
# aggregate.log · gpu-util.csv · timing.txt · env.txt) back to the Mac for analysis.
#
#   ./fetch-runs.sh user@host [port] [remote_runs_dir]
#
# The remote runs dir depends on WHERE openvm-infra is checked out on the box. submit.sh prints its
# absolute runs path ("OK — run record: … <that path>") — pass it as arg 3. If omitted, we probe a
# few common locations and tell you if nothing matched (so a wrong path never fails silently).
set -uo pipefail
REMOTE="${1:?usage: ./fetch-runs.sh user@host [port] [remote_runs_dir]}"
PORT="${2:-22}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/results"
mkdir -p "$DEST"

# Candidate remote dirs: the explicit arg, else a few likely checkout locations.
if [[ -n "${3:-}" ]]; then
  cands=("$3")
else
  cands=(
    "/workspace/openvm/openvm-infra/cluster/runs"
    "\$HOME/openvm-infra/cluster/runs"
    "/workspace/openvm-infra/cluster/runs"
    "\$HOME/infra/openvm-infra/cluster/runs"
  )
fi

# Resolve the first candidate that exists AND is non-empty on the box (expand \$HOME remotely).
RDIR=""
for c in "${cands[@]}"; do
  found="$(ssh -p "$PORT" "$REMOTE" "d=\"$c\"; [ -d \"\$d\" ] && [ -n \"\$(ls -A \"\$d\" 2>/dev/null)\" ] && echo \"\$d\"" 2>/dev/null || true)"
  [[ -n "$found" ]] && { RDIR="$found"; break; }
done

if [[ -z "$RDIR" ]]; then
  echo "ERROR: no non-empty runs dir found on $REMOTE." >&2
  echo "Tried: ${cands[*]}" >&2
  echo "Pass the exact path submit.sh printed:  ./fetch-runs.sh $REMOTE $PORT <remote_runs_dir>" >&2
  exit 1
fi

echo "Fetching $REMOTE:$RDIR/ -> $DEST/"
rsync -az -e "ssh -p $PORT" "$REMOTE:$RDIR/" "$DEST/" || scp -rP "$PORT" "$REMOTE:$RDIR/*" "$DEST/"
echo "Done. Local runs:"
ls -1 "$DEST" | sed 's/^/  /'
