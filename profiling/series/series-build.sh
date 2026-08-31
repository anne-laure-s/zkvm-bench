#!/bin/bash
# series-build.sh — one ZisK ELF per commit of al/zkvm-r4 over Sam's base, into the series cache.
#
# ⚠️ SUPERSEDED by series-build-lineage.sh, which does this for ANY lineage and carries the two
# things this one cannot: a per-commit build environment (<lineage>-buildenv.tsv, without which a
# commit whose whole content is a build option reports as inert) and a per-lineage INDEX. This file
# is the r4-shaped special case it was generalised from; it is kept only because r4's index.tsv was
# built by it. The equivalent, and what to use instead:
#
#   MONAD=<a worktree of your own> BRANCH=al/zkvm-r4 BASE=origin/sam/zkvm-zisk-sp1 \
#   BUILDFIX=c846fd014 INDEX=index.tsv ./series-build-lineage.sh
#
# Nothing in the repo calls this script. Deleting it is safe once r4 needs no rebuild.
#
# Skips commits already present, so it resumes. Deduplicating by ELF sha is part of the result: an
# unchanged sha means the commit cannot have moved the guest.
set -uo pipefail
# Overridable, and it must be: this script drives the tree with `git checkout -f`, so it has
# to be able to run in a worktree of its own rather than the shared clone another session
# is using. The claim below refuses a shared or dirty tree outright.
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE=${BASE:-origin/sam/zkvm-zisk-sp1}
# Commits before this one do not build here: the guest CMakeLists queries
# libc/libgcc without -march and a multilib toolchain answers rv32.
BUILDFIX=c846fd014
mkdir -p "$HERE/elf"; touch "$HERE/index.tsv"
. "$HERE/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
export MONAD                        # build.sh / build-sp1.sh read it too
series_tree_claim "$MONAD"          # refuses a busy or dirty tree; restores HEAD on exit
cd "$MONAD"
i=$(wc -l < "$HERE/index.tsv" | tr -d ' ')
for c in $(git rev-list --reverse "$BASE"..al/zkvm-r4); do
  s=$(git log -1 --format='%h' "$c"); subj=$(git log -1 --format='%s' "$c")
  grep -q "	$s	" "$HERE/index.tsv" && continue
  i=$((i+1))
  git checkout -f -q --detach "$c" || { printf '%d\t%s\tCHECKOUT_FAIL\t\t%s\n' "$i" "$s" "$subj" >> "$HERE/index.tsv"; continue; }
  git merge-base --is-ancestor "$BUILDFIX" "$c" 2>/dev/null || git cherry-pick --no-commit "$BUILDFIX" >/dev/null 2>&1
  if ! "$HERE/build.sh" "tmp-$s" >/dev/null 2>&1; then
      git checkout -f -q -- . 2>/dev/null
      printf '%d\t%s\tBUILD_FAIL\t\t%s\n' "$i" "$s" "$subj" >> "$HERE/index.tsv"
      echo "[$i] $s BUILD_FAIL $subj"; continue
  fi
  git checkout -f -q -- . 2>/dev/null
  h=$(shasum -a256 "$HERE/elf/tmp-$s.elf" | cut -c1-16)
  mv "$HERE/elf/tmp-$s.elf" "$HERE/elf/$h.elf" 2>/dev/null || rm -f "$HERE/elf/tmp-$s.elf"
  printf '%d\t%s\tOK\t%s\t%s\n' "$i" "$s" "$h" "$subj" >> "$HERE/index.tsv"
  echo "[$i] $s $h $subj"
done
echo "distinct ELFs: $(ls "$HERE/elf" | wc -l | tr -d ' ')"
