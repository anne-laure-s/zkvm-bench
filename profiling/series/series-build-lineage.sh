#!/bin/bash
# series-build-lineage.sh — one ZisK ELF per commit of a lineage, into the
# series cache. Replaces the four per-lineage copies (r5, r6, r7, r8) that
# differed on three variables and would have drifted apart.
#
# Each lineage keeps its OWN index: two lineages measured under different ZisK
# runtimes are not comparable, and a shared table would invite exactly that
# comparison. The elf/ directory IS shared, because its key is the ELF sha,
# which already separates them.
#
# Full mode rebuilds every commit. REUSE_BUILDS=1 validates and reuses rows from
# the previous complete index and from a private resume checkpoint. The published
# index is still replaced only after the whole walk succeeds: an interruption
# keeps the last complete index while preserving verified progress for the next
# incremental run. The measurement table remains keyed by ELF sha and block.
#
#   BRANCH    lineage tip to walk (required)
#   INDEX     table to append to   (default: <lineage-name>-index.tsv)
#   BUILDFIX  commit to graft onto ancestors that predate it (required unless
#             every commit of the lineage builds on its own)
#   BASE      where the lineage starts (default: Sam's tip)
#   BUILDENV  sidecar table of per-commit build environments, default
#             <lineage-name>-buildenv.tsv. TAB-separated: the abbreviated commit, then
#             ONE ASSIGNMENT PER FIELD, passed to build.sh FOR THAT COMMIT ONLY.
#             One per field and read into an array, not a word-split string: a value
#             may contain spaces (EXTRA='-mzisk-dma -DMONAD_VM_TABLE_ARG'), and
#             `env $str` would hand the second word to env as the command to run.
#             A carry-forward rule has this shape:
#               @after<TAB><commit><TAB>ASSIGNMENT<TAB>ASSIGNMENT...
#             It applies to later descendants with no exact row. `{commit}` expands
#             to the full commit being built; `{toolchain}` expands to
#             SERIES_TOOLCHAIN_DIR (default ~/.local/xPacks/zisk-dma-gcc-15.2.0).
#             Exact rows always win.
#
# A commit whose whole content is a build option needs this or the series lies about it.
# 6f2a29d4d ("block mem* through ZisK's DMA precompile") is the case that forced it: the
# option is OFF for a stock compiler, so building it like its neighbours produced an ELF
# with the same step count and the same 48,750 dma_xmemcpy calls as the commit before —
# the series correctly reported "did not move the guest" about a commit worth -8.4 %.
# Applying the env globally is not the fix: earlier commits have no _zicsr in their own
# -march and never saw that toolchain.
#
# Commits before BUILDFIX do not build here: the guest CMakeLists queries
# libc/libgcc without -march and a multilib toolchain answers rv32. The fix is
# cherry-picked onto them rather than skipping those commits, so the series has
# no holes -- a hole reads as "did not move the guest", which is a different
# statement from "does not build".
set -uo pipefail
# Overridable: this script drives the tree with `git checkout -f`, so it must be able to run
# in a worktree of its own rather than the shared checkout another session may be using.
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${BASE:-3d237fe69}"
BRANCH="${BRANCH:?BRANCH is required, e.g. BRANCH=al/zkvm-r8}"
INDEX="${INDEX:-$HERE/$(basename "$BRANCH" | sed 's/^zkvm-//')-index.tsv}"
BUILDFIX="${BUILDFIX:-}"
BUILDENV="${BUILDENV:-$HERE/$(basename "$BRANCH" | sed 's/^zkvm-//')-buildenv.tsv}"
REUSE_BUILDS="${REUSE_BUILDS:-0}"
REUSE_INDEX="${REUSE_INDEX:-$INDEX}"
SEED_INDEX="${SEED_INDEX:-}"
ONLY_TIP="${ONLY_TIP:-0}"
SERIES_TOOLCHAIN_DIR="${SERIES_TOOLCHAIN_DIR:-${RISCV_TOOLCHAIN_DIR:-$HOME/.local/xPacks/zisk-dma-gcc-15.2.0}}"
SERIES_STOCK_TOOLCHAIN_DIR="${SERIES_STOCK_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
mkdir -p "$HERE/elf" "$(dirname "$INDEX")"
# A checkpoint is cache, not published provenance. Keep it beside the ignored
# ELFs rather than next to the tracked/final index, so an interrupted run does
# not dirty the repository. Every row is revalidated before reuse.
RESUME_INDEX="${RESUME_INDEX:-$HERE/elf/.$(basename "$INDEX").resume}"
. "$HERE/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
export MONAD                        # build.sh / build-sp1.sh read it too
series_tree_claim "$MONAD"          # refuses a busy or dirty tree; restores HEAD on exit
cd "$MONAD"
echo "lineage $BRANCH over $BASE -> $(basename "$INDEX")${BUILDFIX:+  (buildfix $BUILDFIX)}"
if [ "$ONLY_TIP" = 1 ]; then
  COMMITS=$(git rev-parse "$BRANCH") || { echo "cannot resolve $BRANCH" >&2; exit 2; }
  i=$(git rev-list --count "$BASE".."$BRANCH"); i=$((i-1))
else
  COMMITS=$(git rev-list --reverse "$BASE".."$BRANCH") \
    || { echo "cannot walk $BASE..$BRANCH" >&2; exit 2; }
  i=0
fi
[ -n "$COMMITS" ] || { echo "$BASE..$BRANCH contains no commits" >&2; exit 2; }
NEW_INDEX="$INDEX.tmp.$$"
cleanup_lineage() {
  [ -z "${NEW_INDEX:-}" ] || rm -f "$NEW_INDEX"
  [ -z "${RESUME_INDEX:-}" ] || rm -f "$RESUME_INDEX.tmp.$$"
  series_tree_relinquish
}
# series_tree_claim installed the checkout-restoration trap. Extend it instead
# of replacing it, so an interrupt removes the partial index and restores HEAD.
trap cleanup_lineage EXIT
: > "$NEW_INDEX"
[ "$REUSE_BUILDS" = 1 ] || rm -f "$RESUME_INDEX"
checkpoint_lineage() {
  [ "$REUSE_BUILDS" = 1 ] || return 0
  local tmp="$RESUME_INDEX.tmp.$$"
  # Preserve rows checkpointed by a previous attempt. Replacing the checkpoint
  # with the current prefix would erase its later rows as soon as row 1 was
  # reused. Duplicates are harmless: lookup takes the latest matching row.
  if [ -s "$RESUME_INDEX" ]; then cp "$RESUME_INDEX" "$tmp"; else : > "$tmp"; fi
  tail -n 1 "$NEW_INDEX" >> "$tmp" && mv "$tmp" "$RESUME_INDEX"
}
rebuilt=0; reused=0
for c in $COMMITS; do
  s=$(git log -1 --format='%h' "$c"); subj=$(git log -1 --format='%s' "$c")
  i=$((i+1))
  # Exact per-commit env wins. Otherwise the LAST matching @after rule is inherited.
  # The latter is what keeps r10's official profile on for every future commit: the
  # previous exact-only table silently built four new tips with every lever at its
  # default OFF, and compare reported +14.8 % COST under an "official profile" label.
  benv=()
  if [ -f "$BUILDENV" ]; then
      bline=$(grep -m1 "^$s	" "$BUILDENV" || true)
      if [ -n "$bline" ]; then
          IFS=$'\t' read -r -a bfields <<< "$bline"; benv=("${bfields[@]:1}")
      else
          while IFS=$'\t' read -r -a bfields; do
              [ "${bfields[0]:-}" = '@after' ] || continue
              [ -n "${bfields[1]:-}" ] || continue
              [ "$c" != "$(git rev-parse "${bfields[1]}" 2>/dev/null)" ] || continue
              git merge-base --is-ancestor "${bfields[1]}" "$c" 2>/dev/null || continue
              benv=("${bfields[@]:2}")
          done < "$BUILDENV"
      fi
      if [ "${#benv[@]}" -gt 0 ]; then
          full=$(git rev-parse "$c")
          for j in "${!benv[@]}"; do
              benv[$j]="${benv[$j]//\{commit\}/$full}"
              benv[$j]="${benv[$j]//\{toolchain\}/$SERIES_TOOLCHAIN_DIR}"
          done
      fi
  fi
  # Commits before the first sidecar row historically used build.sh's stock
  # GCC 15 default. Make that formerly implicit input explicit and overridable.
  if [ "${#benv[@]}" -eq 0 ]; then
      benv=("RISCV_TOOLCHAIN_DIR=$SERIES_STOCK_TOOLCHAIN_DIR")
  fi
  expected_env="${benv[*]-}"
  oldline=""
  if [ -n "$SEED_INDEX" ] && [ -s "$SEED_INDEX" ]; then
      oldline=$(awk -F'\t' -v c="$s" '$2==c && $3=="OK"{print; exit}' "$SEED_INDEX")
  fi
  if [ -z "$oldline" ] && [ "$REUSE_BUILDS" = 1 ] && [ -s "$RESUME_INDEX" ]; then
      oldline=$(awk -F'\t' -v c="$s" '$2==c && $3=="OK"{line=$0} END{if(line) print line}' "$RESUME_INDEX")
  fi
  if [ -z "$oldline" ] && [ "$REUSE_BUILDS" = 1 ] && [ -s "$REUSE_INDEX" ]; then
      oldline=$(awk -F'\t' -v c="$s" '$2==c && $3=="OK"{print; exit}' "$REUSE_INDEX")
  fi
  if [ -n "$oldline" ]; then
      IFS=$'\t' read -r -a oldfields <<< "$oldline"
      h="${oldfields[3]:-}"; oldenv="${oldfields[5]:-}"
      if [ -n "$h" ] && [ -f "$HERE/elf/$h.elf" ] && [ "$oldenv" = "$expected_env" ]; then
          actual_h=$(shasum -a256 "$HERE/elf/$h.elf" | cut -c1-16)
      else
          actual_h=""
      fi
      if [ "$actual_h" = "$h" ]; then
          printf '%d\t%s\tOK\t%s\t%s%s\n' "$i" "$s" "$h" "$subj" \
                 "${benv[*]+${benv[*]:+$'\t'${benv[*]}}}" >> "$NEW_INDEX"
          echo "[$i] $s $h REUSED $subj"
          reused=$((reused+1))
          checkpoint_lineage || { echo "cannot checkpoint $RESUME_INDEX" >&2; exit 2; }
          continue
      fi
  fi

  git checkout -f -q --detach "$c" || {
    echo "[$i] $s CHECKOUT_FAIL $subj" >&2
    exit 2
  }
  if [ -n "$BUILDFIX" ]; then
    if ! git merge-base --is-ancestor "$BUILDFIX" "$c" 2>/dev/null; then
      # Keep stderr: suppressing it reduced every conflict, stale lock or object
      # error to the same unexplained BUILDFIX_FAIL. Reset the index as well as
      # the worktree before leaving; --no-commit stages the applied patch.
      git cherry-pick --no-commit "$BUILDFIX" || {
        echo "[$i] $s BUILDFIX_FAIL $BUILDFIX" >&2
        git checkout -f -q --detach "$c" 2>/dev/null || true
        exit 2
      }
    fi
  fi
  # bash 3.2 (what macOS ships) treats "${arr[@]}" on an EMPTY array as unbound under `set -u`,
  # so the no-sidecar path died with `benv[@]: unbound variable` right after the one commit that
  # had an entry. The +expansion form is the portable guard.
  # Do not let flags exported by the caller become undeclared build inputs.
  # The sidecar adds back the exact overrides required by this commit; commits
  # with no row get the explicit stock-toolchain fallback above.
  if ! env -u MONAD_ZKVM_CMAKE_DEFINES -u MONAD_ZKVM_GIT_COMMIT \
      -u RISCV_TOOLCHAIN_DIR -u MARCH -u EXTRA \
      FORCE_REBUILD=1 ${benv[@]+"${benv[@]}"} \
      "$HERE/build.sh" "tmp-$s"; then
      git checkout -f -q --detach "$c" 2>/dev/null || true
      rm -f "$HERE/elf/tmp-$s.elf"
      echo "[$i] $s BUILD_FAIL $subj" >&2
      exit 2
  fi
  # A successful cherry-pick --no-commit leaves the fix staged. Reset both the
  # worktree and index before the next commit; resetting only `-- .` left that
  # staged state behind and made later failures depend on the previous build.
  git checkout -f -q --detach "$c" 2>/dev/null || {
    echo "[$i] $s CLEANUP_FAIL after build" >&2
    exit 2
  }
  h=$(shasum -a256 "$HERE/elf/tmp-$s.elf" | cut -c1-16)
  mv "$HERE/elf/tmp-$s.elf" "$HERE/elf/$h.elf" 2>/dev/null || rm -f "$HERE/elf/tmp-$s.elf"
  # $'\t' and not '\t': printf expands escapes in the FORMAT, never in a %s argument,
  # so the literal two characters would land in the subject column.
  printf '%d\t%s\tOK\t%s\t%s%s\n' "$i" "$s" "$h" "$subj" \
         "${benv[*]+${benv[*]:+$'\t'${benv[*]}}}" >> "$NEW_INDEX"
  echo "[$i] $s $h $subj${benv[*]+${benv[*]:+  [env: ${benv[*]}]}}"
  rebuilt=$((rebuilt+1))
  checkpoint_lineage || { echo "cannot checkpoint $RESUME_INDEX" >&2; exit 2; }
done
mv "$NEW_INDEX" "$INDEX" || { echo "cannot replace $INDEX" >&2; exit 2; }
NEW_INDEX=""
rm -f "$RESUME_INDEX"
echo "done: $rebuilt rebuilt, $reused reused; index replaced"
FAILED_ROWS=$(awk -F'\t' '$3!="OK"{n++} END{print n+0}' "$INDEX")
[ "$FAILED_ROWS" = 0 ] || {
  echo "$FAILED_ROWS lineage commit(s) failed — refusing to measure a partial series" >&2
  awk -F'\t' '$3!="OK"{print "  " $2 " " $3 " " $5}' "$INDEX" | head -10 >&2
  exit 1
}
TIP_STATUS=$(awk -F'\t' 'NF{status=$3; commit=$2} END{print status, commit}' "$INDEX")
case "$TIP_STATUS" in
  OK\ *) ;;
  *) echo "lineage tip did not build ($TIP_STATUS) — refusing to expose an older OK as the tip" >&2
     exit 1 ;;
esac
