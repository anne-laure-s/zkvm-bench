#!/bin/bash
# watch-levers.sh — list commits on al/zkvm-r4-levers we have neither taken nor
# measured-and-rejected. Refuses to run while a series build owns the repo:
# checking out underneath one builds a tree that is not the lever, which once
# produced a confident +37%.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# The tree lock, not `pgrep -f series-build`: a `-f` pattern matches any command line carrying
# the string — including the shell that launched this watcher, which then blocked on itself —
# and missed a build started under another name. The lock is held by the builder that actually
# owns this checkout.
. "$HERE/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
if series_tree_busy "$MONAD"; then
  echo "a series build owns $MONAD — checking out underneath it would build a tree that is"
  echo "not the lever (that is where a confident +37% came from)"; exit 3
fi
cd "$MONAD"
HAVE=$(mktemp); trap 'rm -f "$HAVE"' EXIT
git log --format='%s' origin/sam/zkvm-zisk-sp1..al/zkvm-r4 > "$HAVE"
git log --reverse --format='%h %s' al/zkvm-r4..al/zkvm-r4-levers | while read -r sha subj; do
  grep -qxF "$subj" "$HERE/rejected.txt" && { echo "REJECTED $sha  $subj"; continue; }
  grep -qxF "$subj" "$HAVE"             && { echo "HAVE     $sha  $subj"; continue; }
  echo "CANDIDATE $sha  $subj"
done
