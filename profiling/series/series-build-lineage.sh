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
# the previous complete index and from every checkpoint and unmerged slice this
# table has left, whichever walk -- serial or parallel -- wrote them. The published
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
# The table this walk writes. A slice of a parallel walk indexes into a file of its own, so the driver
# names the table; a serial walk IS the table.
REUSE_FAMILY="${REUSE_FAMILY:-$(basename "$INDEX")}"
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
. "$HERE/subject-ref.sh"
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
  # Row numbers are the lineage's, not the walk's. series-build-parallel.sh hands each worker a
  # contiguous SLICE of one lineage, and a slice that renumbered from 1 would produce four indexes
  # all claiming row 1 -- merging them would then need a renumbering pass that could silently
  # reorder the series. Numbering from where the slice starts makes the merge a concatenation.
  i="${I_OFFSET:-0}"
fi
[ -n "$COMMITS" ] || { echo "$BASE..$BRANCH contains no commits" >&2; exit 2; }
# Anchors resolve over a range that INCLUDES the base. The walk itself does not build the base --
# `rev-list BASE..BRANCH` excludes it -- but a sidecar rule anchored on the base is the natural way
# to say "every commit of this lineage", and searching the exclusive range made that rule the one
# thing that could never resolve. A root commit has no parent, so fall back to the whole branch.
# Overridable, and a parallel walk must override it: a worker builds a SLICE of the lineage, and
# the sidecar anchors sit wherever the option was introduced -- usually near the lineage base, which
# is outside every slice but the first. Resolved against the slice alone they would not resolve at
# all, and an unresolvable anchor is a hard failure by design. The driver passes the whole lineage.
if [ -z "${ANCHOR_RANGE:-}" ]; then
  if git rev-parse --verify "$BASE^{commit}" >/dev/null 2>&1 && \
     git rev-parse --verify "$BASE^^{commit}" >/dev/null 2>&1; then
      ANCHOR_RANGE="$BASE^..$BRANCH"
  else
      ANCHOR_RANGE="$BRANCH"
  fi
fi
NEW_INDEX="$INDEX.tmp.$$"
cleanup_lineage() {
  [ -z "${NEW_INDEX:-}" ] || rm -f "$NEW_INDEX"
  [ -z "${RESUME_INDEX:-}" ] || rm -f "$RESUME_INDEX.tmp.$$"
  [ -z "${REUSE_POOL:-}" ] || rm -f "$REUSE_POOL"
  series_tree_relinquish
}
# series_tree_claim installed the checkout-restoration trap. Extend it instead
# of replacing it, so an interrupt removes the partial index and restores HEAD.
trap cleanup_lineage EXIT
: > "$NEW_INDEX"
[ "$REUSE_BUILDS" = 1 ] || rm -f "$RESUME_INDEX"
# Every build this table has recorded, whichever walk recorded it. A serial walk and each slice of a
# parallel one checkpoint to files of their own -- several writers on one checkpoint would drop each
# other's rows -- and a parallel walk whose merge never ran leaves its finished slices in .parallel/.
# Each of them is read by every walk of the table, so a serial run resumes what a parallel one built
# and the reverse. A row is used only after the checks below, so reading more of them never reuses a
# build it should not.
REUSE_POOL="$HERE/elf/.$(basename "$INDEX").pool.$$"
: > "$REUSE_POOL"
if [ "$REUSE_BUILDS" = 1 ]; then
  for f in "$RESUME_INDEX" "$HERE/elf/.$REUSE_FAMILY".resume "$HERE/elf/.$REUSE_FAMILY".w*.resume \
           "$HERE/elf/.parallel/$REUSE_FAMILY".w[0-9]* "$REUSE_INDEX"; do
    case "$f" in *.log) continue ;; esac
    [ -s "$f" ] && cat "$f" >> "$REUSE_POOL"
  done
fi
# Gate one ELF against the corpus, keyed by its sha. Empty GATE_GEN disables it, which is what a
# caller with no corpus (or one deliberately measuring an unverifiable arm) needs.
gate_elf() {
    local h="$1" s="$2" gate
    [ -n "${GATE_GEN:-}" ] || { printf '%s' '-'; return 0; }
    # Overridable because a parallel walk writes its rows to a per-worker index whose name would
    # derive four different gate-record names for one lineage -- and the record is keyed by the ELF
    # sha precisely so that every commit building the same bytes shares one verdict.
    gate="${GATE_PREFIX:-$(dirname "$INDEX")/$(basename "$INDEX" -index.tsv)}-gate-$h.tsv"
    if REUSE=1 GEN="$GATE_GEN" "$HERE/gate-roots-record.sh" "$HERE/elf/$h.elf" "$gate" \
           "${GATE_JOBS:-6}" >/dev/null 2>&1; then
        printf '%s' 'gate-ok'
    else
        echo "[$i] $s GATE_FAIL $h -- see $(basename "$gate")" >&2
        printf '%s' 'GATE_FAIL'
    fi
}

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
          # Four anchor forms, one resolver. `@after` excludes its own commit, `@from` includes it
          # -- a rule that starts AT the commit introducing an option is the common case and had
          # to be written as "the parent of", which is one more sha to rebase away. The `:subject`
          # variants name the commit by its subject, which a rebase preserves where it rewrites
          # every sha; an anchor that no longer resolves is a hard failure, never a silent skip.
          while IFS=$'\t' read -r -a bfields; do
              case "${bfields[0]:-}" in
                  @after|@from) _incl=0; [ "${bfields[0]}" = '@after' ] || _incl=1
                                _aref="${bfields[1]:-}" ;;
                  @after:subject|@from:subject)
                                _incl=0; [ "${bfields[0]}" = '@after:subject' ] || _incl=1
                                _aref="subject:${bfields[1]:-}" ;;
                  *) continue ;;
              esac
              [ -n "${bfields[1]:-}" ] || continue
              _anchor=$(resolve_ref "$ANCHOR_RANGE" "$_aref") || {
                  echo "BUILDENV anchor does not resolve in $BASE..$BRANCH: ${bfields[1]}" >&2
                  exit 2; }
              if [ "$c" = "$_anchor" ]; then
                  [ "$_incl" = 1 ] || continue
              else
                  git merge-base --is-ancestor "$_anchor" "$c" 2>/dev/null || continue
              fi
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
  # Every recorded build of this commit, the seed's first. One counts only if its ELF is still the file
  # it names and was built from the recipe this commit resolves to now; the first that does is used.
  candidates=""
  if [ -n "$SEED_INDEX" ] && [ -s "$SEED_INDEX" ]; then
      candidates=$(awk -F'\t' -v c="$s" '$2==c && $3=="OK"' "$SEED_INDEX")
  fi
  if [ "$REUSE_BUILDS" = 1 ] && [ -s "$REUSE_POOL" ]; then
      candidates="$candidates${candidates:+$'\n'}$(awk -F'\t' -v c="$s" '$2==c && $3=="OK"' "$REUSE_POOL")"
  fi
  h=""
  while IFS= read -r oldline; do
      [ -n "$oldline" ] || continue
      IFS=$'\t' read -r -a oldfields <<< "$oldline"
      _h="${oldfields[3]:-}"
      [ -n "$_h" ] && [ -f "$HERE/elf/$_h.elf" ] && [ "${oldfields[5]:-}" = "$expected_env" ] || continue
      [ "$(shasum -a256 "$HERE/elf/$_h.elf" | cut -c1-16)" = "$_h" ] || continue
      h="$_h"; break
  done <<< "$candidates"
  if [ -n "$h" ]; then
      printf '%d\t%s\tOK\t%s\t%s%s\n' "$i" "$s" "$h" "$subj" \
             "${benv[*]+${benv[*]:+$'\t'${benv[*]}}}" >> "$NEW_INDEX"
      echo "[$i] $s $h REUSED $(gate_elf "$h" "$s") $subj"
      reused=$((reused+1))
      checkpoint_lineage || { echo "cannot checkpoint $RESUME_INDEX" >&2; exit 2; }
      continue
  fi

  git checkout -f -q --detach "$c" || {
    echo "[$i] $s CHECKOUT_FAIL $subj" >&2
    exit 2
  }
  # A checkout moves the submodule POINTERS and leaves the submodule worktrees where the previous
  # commit left them, so walking a lineage compiles each commit's own sources against whatever
  # third_party the last one happened to check out. That is not a build failure you can read: the
  # error surfaces deep in a template instantiation, in a file the commit never touched.
  git submodule update --init --recursive --quiet || {
    echo "[$i] $s SUBMODULE_FAIL $subj" >&2
    git reset --hard -q "$c" 2>/dev/null || true
    exit 2
  }
  if [ -n "$BUILDFIX" ]; then
    if ! git merge-base --is-ancestor "$BUILDFIX" "$c" 2>/dev/null; then
      # Keep stderr: suppressing it reduced every conflict, stale lock or object
      # error to the same unexplained BUILDFIX_FAIL. Reset the index as well as
      # the worktree before leaving; --no-commit stages the applied patch.
      git cherry-pick --no-commit "$BUILDFIX" || {
        echo "[$i] $s BUILDFIX_FAIL $BUILDFIX" >&2
        git reset --hard -q "$c" 2>/dev/null || true
        exit 2
      }
      # The fix may carry a submodule bump of its own.
      git submodule update --init --recursive --quiet || true
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
      git reset --hard -q "$c" 2>/dev/null || true
      rm -f "$HERE/elf/tmp-$s.elf"
      echo "[$i] $s BUILD_FAIL $subj" >&2
      exit 2
  fi
  # A successful cherry-pick --no-commit leaves the fix staged. Reset both the
  # worktree and index before the next commit. `checkout -f --detach "$c"` is
  # not sufficient when HEAD is already $c: Git can leave the staged patch in
  # place, making the next cherry-pick fail on CMakeLists.txt.
  git reset --hard -q "$c" 2>/dev/null || {
    echo "[$i] $s CLEANUP_FAIL after build" >&2
    exit 2
  }
  # Name the build by the PROGRAM, not by the bytes of the file. Cargo's unit hash for a path
  # package includes that package's absolute path, so the same commit built in two worktrees --
  # which is what a parallel walk is -- yields two files differing only in rustc's codegen-unit
  # symbol names, none of it inside a PT_LOAD segment and none of it visible to the emulator.
  # Keyed by the file sha, those would be two cache entries and two measurement campaigns for one
  # program. --prefer keeps a name this lineage already uses when the cache knows the program under
  # several. See elf-id.py.
  _adopt=$("$HERE/elf-id.py" --dir "$HERE/elf" --adopt "$HERE/elf/tmp-$s.elf" \
           --prefer "$([ -s "$REUSE_INDEX" ] && echo "$REUSE_INDEX" || echo "$INDEX")") || {
      echo "[$i] $s ADOPT_FAIL $subj" >&2
      rm -f "$HERE/elf/tmp-$s.elf"
      exit 2
  }
  h=${_adopt%%	*}; _adopted=${_adopt##*	}
  # Gate HERE, next to the build that produced it, and keyed by the ELF's own sha. Gating only the
  # tip left every earlier commit unverified, so a lineage could be measured end to end with a
  # guest that stopped reproducing the corpus somewhere in the middle -- and nothing to say where.
  # The record is per-sha, so a commit that rebuilds an identical binary reuses the verdict and a
  # re-walk of an unchanged lineage costs nothing.
  _gv=$(gate_elf "$h" "$s")
  # $'\t' and not '\t': printf expands escapes in the FORMAT, never in a %s argument,
  # so the literal two characters would land in the subject column.
  printf '%d\t%s\tOK\t%s\t%s%s\n' "$i" "$s" "$h" "$subj" \
         "${benv[*]+${benv[*]:+$'\t'${benv[*]}}}" >> "$NEW_INDEX"
  _tag=""; [ "${_adopted:-}" = reused ] && _tag=" reused-elf"
  echo "[$i] $s $h$_tag${_gv:+ $_gv} $subj${benv[*]+${benv[*]:+  [env: ${benv[*]}]}}"
  rebuilt=$((rebuilt+1))
  checkpoint_lineage || { echo "cannot checkpoint $RESUME_INDEX" >&2; exit 2; }
done
mv "$NEW_INDEX" "$INDEX" || { echo "cannot replace $INDEX" >&2; exit 2; }
NEW_INDEX=""
rm -f "$RESUME_INDEX"
echo "done: $rebuilt rebuilt, $reused reused; index replaced"
# A slice is not a lineage: it has no tip of its own, and "every row OK" is a statement about the
# merged index. The driver makes it there, over all the slices at once.
[ "${PARTIAL:-0}" = 0 ] || exit 0
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
