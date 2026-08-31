#!/bin/bash
# series-build-sp1.sh — build the SP1 guest only for the commits the question
# needs. The question is "which commits are inert on BOTH backends", and a
# commit that moved the ZisK ELF is not inert whatever SP1 does — so only the
# ZisK-grey commits and their predecessors need an SP1 build. That is 23 of 57
# here, and each build is 80-120s.
#
# Not wiping the cmake build dir: measured, two different commits give two
# different ELFs without it. The wipe is needed when only the ENV changes
# (a -mtune flag cargo does not fingerprint), not when the sources do.
set -uo pipefail
# Overridable, and it must be: this script drives the tree with `git checkout -f`, so it has
# to be able to run in a worktree of its own rather than the shared clone another session
# is using. The claim below refuses a shared or dirty tree outright.
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/tree-lock.sh"
OUTD="${OUTD:-$(series_repo)/profiling/series-sp1}"
export OUTD          # build-sp1.sh writes here too
BUILDFIX=c846fd014
mkdir -p "$OUTD/elf"; touch "$OUTD/index.tsv"
MONAD="${MONAD:-$(series_monad_default)}"
export MONAD                        # build.sh / build-sp1.sh read it too
series_tree_claim "$MONAD"          # refuses a busy or dirty tree; restores HEAD on exit
cd "$MONAD"
i=$(wc -l < "$OUTD/index.tsv" | tr -d ' ')
while read -r s; do
  [ -n "$s" ] || continue
  grep -q "	$s	" "$OUTD/index.tsv" && continue
  subj=$(git log -1 --format='%s' "$s")
  i=$((i+1))
  git checkout -f -q --detach "$s" || { echo "CHECKOUT_FAIL $s"; continue; }
  git merge-base --is-ancestor "$BUILDFIX" "$s" 2>/dev/null || git cherry-pick --no-commit "$BUILDFIX" >/dev/null 2>&1
  if ! "$HERE/build-sp1.sh" "tmp-$s" >/dev/null 2>&1; then
      git checkout -f -q -- . 2>/dev/null
      printf '%d\t%s\tBUILD_FAIL\t\t%s\n' "$i" "$s" "$subj" >> "$OUTD/index.tsv"
      echo "BUILD_FAIL $s"; continue
  fi
  git checkout -f -q -- . 2>/dev/null
  h=$(shasum -a256 "$OUTD/elf/tmp-$s.elf" | cut -c1-16)
  mv "$OUTD/elf/tmp-$s.elf" "$OUTD/elf/$h.elf" 2>/dev/null || rm -f "$OUTD/elf/tmp-$s.elf"
  printf '%d\t%s\tOK\t%s\t%s\n' "$i" "$s" "$h" "$subj" >> "$OUTD/index.tsv"
  echo "[$i] $s $h $subj"
done < "$HERE/sp1-needed.txt"
echo "done: $(grep -c OK "$OUTD/index.tsv") entries"
