#!/bin/bash
# series-build-parallel.sh — walk one lineage across several worktrees at once.
#
# Commits of a lineage are INDEPENDENT builds: commit N's ELF does not consume commit N-1's. The
# only thing that ties the walk together is the incremental build cache, and that is a performance
# effect, not a correctness one. So the walk parallelises -- but a worktree holds one commit at a
# time, so the unit of parallelism is a worktree, not a process.
#
# Each worker gets a CONTIGUOUS slice and walks it in order. Round-robin would balance the load
# better and would be much slower: adjacent commits touch few files, which is the whole reason a
# build here is ~15 s rather than several minutes, and a worker hopping between distant commits
# would rebuild the world every time. Contiguity buys the cache; the slices are equal in commit
# count, and over a dozen commits each the per-commit variance evens out.
#
# The workers are the SAME script that walks a whole lineage, invoked on a range. That is what a
# slice is, so there is no second implementation of the build, the sidecar, the buildfix, the
# gate or the resume checkpoint to keep in step with the first -- the three hooks it needed are
# I_OFFSET, ANCHOR_RANGE and PARTIAL.
#
#   JOBS              worktrees to build in (default 4; 1 delegates to the serial walk unchanged)
#   SERIES_WORKERS    where the extra worktrees live (default: <monad tree>-w2, -w3, ...)
#
# Everything else -- BRANCH, BASE, BUILDFIX, BUILDENV, INDEX, REUSE_BUILDS, REUSE_INDEX, GATE_GEN,
# MONAD -- means what it means for series-build-lineage.sh, and is passed through.
#
# Worker 1 builds in $MONAD itself, so JOBS=1 and JOBS=4 differ only in the extra trees. The extra
# trees are PERSISTENT: provisioning one costs a submodule checkout, which is minutes, and paying
# that on every run would eat the gain it buys. They are reused as they are left, which also means
# a worker starts its next run near the slice it ended on -- warm.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/tree-lock.sh"
. "$HERE/subject-ref.sh"

BRANCH="${BRANCH:?BRANCH is required}"
BASE="${BASE:-3d237fe69}"
MONAD="${MONAD:-$(series_monad_default)}"
INDEX="${INDEX:-$HERE/$(basename "$BRANCH" | sed 's/^zkvm-//')-index.tsv}"
JOBS="${JOBS:-4}"
SERIES_WORKERS="${SERIES_WORKERS:-$MONAD}"   # worker k > 1 lives at <this>-w<k>
PROVISION_ONLY="${PROVISION_ONLY:-0}"

[ -d "$MONAD" ] || { echo "no such checkout: $MONAD" >&2; exit 2; }
# A worker worktree is created AT the base, so the base has to be a commit by the time it gets
# here. run-r10.sh resolves `subject:<text>` in its preflight; a direct caller that does not would
# otherwise meet this as a `git worktree add` failure naming a ref nobody wrote.
git -C "$MONAD" rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || {
  echo "BASE must name a commit here, not a search: $BASE" >&2; exit 2; }
COMMITS=$(git -C "$MONAD" rev-list --reverse "$BASE".."$BRANCH") \
  || { echo "cannot walk $BASE..$BRANCH in $MONAD" >&2; exit 2; }
[ -n "$COMMITS" ] || { echo "$BASE..$BRANCH contains no commits" >&2; exit 2; }
set -- $COMMITS; N=$#
# Where this walk sits in its lineage. Zero for a whole lineage; a window of one (run-r10.sh --last)
# numbers its rows the way the lineage does, so the slices below add it to their own offset.
OFFSET0="${I_OFFSET:-0}"

# One worker per slice, and a slice worth having. Splitting 5 commits four ways pays four cold
# starts to save two builds.
MIN_SLICE="${MIN_SLICE:-4}"
W="$JOBS"
[ "$W" -ge 1 ] || W=1
while [ "$W" -gt 1 ] && [ $((N / W)) -lt "$MIN_SLICE" ]; do W=$((W-1)); done

if [ "$W" -le 1 ] && [ "$PROVISION_ONLY" = 0 ]; then
  [ "$JOBS" -le 1 ] || echo "only $N commit(s) to build — walking them serially"
  # export, not `env A=B`: every other input (BUILDFIX, BUILDENV, REUSE_BUILDS, GATE_GEN, the ZisK
  # pins) reaches the walk through the environment, and an explicit assignment list is a list that
  # will one day be missing the input someone just added.
  export MONAD BRANCH BASE INDEX
  exec "$HERE/series-build-lineage.sh"
fi

# The anchors the sidecar resolves against belong to the LINEAGE, not to any slice. Computed here
# once, exactly as the serial walk computes it, and handed to every worker. Overridable for a run
# over a WINDOW of a lineage, whose own base sits after the anchors.
if [ -z "${ANCHOR_RANGE:-}" ]; then
  if git -C "$MONAD" rev-parse --verify "$BASE^{commit}" >/dev/null 2>&1 && \
     git -C "$MONAD" rev-parse --verify "$BASE^^{commit}" >/dev/null 2>&1; then
      ANCHOR_RANGE="$BASE^..$BRANCH"
  else
      ANCHOR_RANGE="$BRANCH"
  fi
fi

# ── worker worktrees ─────────────────────────────────────────────────────────────────────────────
worker_tree() { [ "$1" = 1 ] && printf '%s' "$MONAD" || printf '%s-w%s' "$SERIES_WORKERS" "$1"; }

provision() {
    local k="$1" W_DIR; W_DIR=$(worker_tree "$k")
    [ "$k" = 1 ] && return 0
    if [ -e "$W_DIR/.git" ]; then return 0; fi
    echo "--- provisioning worker $k at $W_DIR (one-time)"
    git -C "$MONAD" worktree add --detach -f "$W_DIR" "$BASE" >/dev/null 2>&1 || {
        echo "cannot create a worktree at $W_DIR" >&2; return 1; }
    # Borrow the superproject's own submodule objects instead of cloning them again. Every worker
    # is a worktree of ONE repository, so those objects are already on this disk; without this a
    # fresh worker spends minutes re-fetching third_party it is sitting next to.
    git -C "$W_DIR" config submodule.alternateLocation superproject
    git -C "$W_DIR" config submodule.alternateErrorStrategy info
    git -C "$W_DIR" submodule update --init --recursive --quiet || {
        echo "cannot initialise submodules in $W_DIR" >&2; return 1; }
    return 0
}

# A build tree is ~8 GB and a walk that runs out of disk half way leaves every worker's incremental
# state to rebuild from nothing. Cheap to check, expensive to discover.
need=0
for k in $(seq 2 "$W"); do [ -e "$(worker_tree "$k")/.git" ] || need=$((need+9)); done
if [ "$need" -gt 0 ]; then
    free=$(df -g "$MONAD" 2>/dev/null | awk 'NR==2{print $4}')
    case "$free" in ''|*[!0-9]*) free="" ;; esac
    if [ -n "$free" ] && [ "$free" -lt "$need" ]; then
        echo "need ~${need} GiB for the new worker tree(s), $free GiB free on $(dirname "$MONAD")" >&2
        exit 5
    fi
fi
_prov="$W"; [ "$PROVISION_ONLY" = 0 ] || _prov="$JOBS"
for k in $(seq 1 "$_prov"); do provision "$k" || exit 5; done
[ "$PROVISION_ONLY" = 0 ] || { echo "worker trees ready"; exit 0; }

# ── slices ───────────────────────────────────────────────────────────────────────────────────────
# Per-worker indexes live beside the ELF cache and not beside the published one: they are cache,
# like the resume checkpoint they drive, and a run interrupted half way must not leave the
# repository dirty. Their names are STABLE, so the resume checkpoint each worker keeps is found
# again by the same worker on the next run.
WDIR="$HERE/elf/.parallel"; mkdir -p "$WDIR"
BN=$(basename "$INDEX")
echo "lineage $BRANCH over $BASE: $N commits across $W worktree(s)"
pids=(); wlog=(); widx=(); wfrom=(); wto=()
start=0
for k in $(seq 1 "$W"); do
    # Distribute the remainder over the FIRST slices rather than piling it on the last one.
    cnt=$(( N / W )); [ "$k" -le $(( N % W )) ] && cnt=$((cnt+1))
    from=$((start+1)); to=$((start+cnt)); start=$to
    eval "slice_tip=\${$to}"
    if [ "$from" = 1 ]; then slice_base="$BASE"; else eval "slice_base=\${$((from-1))}"; fi
    idx="$WDIR/$BN.w$k"; log="$WDIR/$BN.w$k.log"
    widx+=("$idx"); wlog+=("$log"); wfrom+=("$from"); wto+=("$to")
    : > "$log"
    # I_OFFSET makes the worker number its rows the way the lineage numbers them, so the merge
    # below is a concatenation and never a renumbering.
    # GATE_PREFIX keeps every worker writing the per-sha gate record to the LINEAGE's name: the
    # record is keyed by the ELF sha and shared, so two commits that build the same bytes -- in
    # different slices or not -- cost one gate. Two workers reaching the same sha at the same
    # moment can interleave that file; gate-roots-record.sh validates a cached record before
    # trusting it, so the outcome is a re-gate, never a pass that was not earned.
    env MONAD="$(worker_tree "$k")" \
        BRANCH="$slice_tip" BASE="$slice_base" \
        INDEX="$idx" I_OFFSET="$((OFFSET0 + from - 1))" REUSE_FAMILY="$BN" \
        ANCHOR_RANGE="$ANCHOR_RANGE" \
        GATE_PREFIX="$(dirname "$INDEX")/$(basename "$INDEX" -index.tsv)" \
        GATE_JOBS="${GATE_JOBS:-$(( 6 / W < 1 ? 1 : 6 / W ))}" \
        PARTIAL=1 \
        "$HERE/series-build-lineage.sh" > "$log" 2>&1 &
    pids+=($!)
    echo "  worker $k: commits $from..$to in $(worker_tree "$k")"
done

# Workers write to their own logs and this relays them, because W processes echoing into one pipe
# tear each other's lines apart. Reading whole lines from separate files cannot.
#
# The relayed lines say what each worker has just done, not how far the walk has got, so a gauge
# over every slice follows them each time one moves. It counts, among the lines it has relayed,
# those that name an ELF, `[i] <commit> <elf>`, reused or built alike. A worker stops at its first
# failure, which names no ELF: its slice is marked stopped and the gauge stays short of 100 %.
relay() {
    local k n c d t alive final chunk done_all parts state last="" pct filled bar i
    while :; do
        # Read before the pass, not after it: the pass that ends the loop then starts once every
        # worker has exited, and relays their last lines.
        final=0; [ -f "$WDIR/.done" ] && final=1
        done_all=0; parts=""
        for k in $(seq 1 "$W"); do
            # Alive is sampled before the log is read: a worker found dead has written all it ever
            # will, so a slice still short after the read is one that stopped.
            alive=0; kill -0 "${pids[$((k-1))]}" 2>/dev/null && alive=1
            n=$(wc -l < "${wlog[$((k-1))]}" 2>/dev/null | tr -d ' '); n=${n:-0}
            eval "seen=\${seen_$k:-0}; d=\${done_$k:-0}"
            if [ "$n" -gt "$seen" ]; then
                chunk=$(sed -n "$((seen+1)),${n}p" "${wlog[$((k-1))]}")
                printf '%s\n' "$chunk" | sed "s/^/[w$k] /"
                c=$(printf '%s\n' "$chunk" | grep -cE '^\[[0-9]+\] [0-9a-f]{7,} [0-9a-f]{16}( |$)')
                d=$((d + ${c:-0}))
                eval "seen_$k=$n; done_$k=$d"
            fi
            t=$(( ${wto[$((k-1))]} - ${wfrom[$((k-1))]} + 1 ))
            state=""; [ "$alive" = 1 ] || [ "$d" -ge "$t" ] || state=" stopped"
            done_all=$((done_all + d)); parts="$parts  w$k $d/$t$state"
        done
        if [ "$parts" != "$last" ]; then
            pct=$((done_all * 100 / N)); filled=$((pct / 5)); bar=""; i=0
            while [ "$i" -lt 20 ]; do
                if [ "$i" -lt "$filled" ]; then bar="$bar#"; else bar="$bar-"; fi
                i=$((i+1))
            done
            printf 'progress [%s] %3d%% (%d/%d, %d left)%s\n' \
                   "$bar" "$pct" "$done_all" "$N" "$((N - done_all))" "$parts"
            last=$parts
        fi
        [ "$final" = 1 ] && break
        sleep 2
    done
}
rm -f "$WDIR/.done"
relay & relay_pid=$!

rc=0; failed=""
for k in $(seq 1 "$W"); do
    wait "${pids[$((k-1))]}" || { rc=1; failed="$failed $k"; }
done
: > "$WDIR/.done"; wait "$relay_pid" 2>/dev/null; rm -f "$WDIR/.done"

if [ -n "$failed" ]; then
    echo "worker(s)$failed failed — the lineage is incomplete and the index is NOT replaced" >&2
    for k in $failed; do
        echo "  worker $k covered commits ${wfrom[$((k-1))]}..${wto[$((k-1))]}; log: ${wlog[$((k-1))]}" >&2
        tail -4 "${wlog[$((k-1))]}" | sed 's/^/    /' >&2
    done
    exit 2
fi

# ── merge ────────────────────────────────────────────────────────────────────────────────────────
# Slices are contiguous and numbered from where they start, so the lineage is the concatenation in
# worker order. Sorted anyway: a merge that silently reordered a series would be invisible in the
# report and wrong in every ratio.
MERGED="$INDEX.tmp.$$"
trap 'rm -f "$MERGED"' EXIT
cat "${widx[@]}" | awk 'NF' | sort -n -k1,1 > "$MERGED"
rows=$(wc -l < "$MERGED" | tr -d ' ')
[ "$rows" = "$N" ] || {
    echo "merged index holds $rows rows for a $N-commit lineage — refusing to publish it" >&2
    exit 2
}
# The row numbers must run consecutively from the walk's first row, each once. A duplicate or a hole means two slices overlapped
# or one stopped short, and both produce a plausible-looking index that measures the wrong walk.
awk -v n="$N" -v off="$OFFSET0" 'NF{ if ($1 != NR + off) { print "row " NR " is numbered " $1; bad=1 } } END{ exit bad }' \
    "$MERGED" >&2 || { echo "refusing to publish a misnumbered index" >&2; exit 2; }
mv "$MERGED" "$INDEX" || { echo "cannot replace $INDEX" >&2; exit 2; }
trap - EXIT
echo "done: $N commits over $W worktree(s); index replaced"

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
