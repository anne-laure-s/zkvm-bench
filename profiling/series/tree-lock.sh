# tree-lock.sh — claim a monad checkout for a series build. SOURCED, not executed.
#
# Three fragile things used to stand in for this, and each failed in its own way:
#
#   * `pgrep -f series-build` as "is a build running". A `-f` pattern matches any command line
#     carrying the string, including the shell that launched the watcher — so the check either
#     self-matched and blocked forever, or missed a build started under a different name.
#   * nothing at all against a SHARED checkout. All three builders drive the tree with
#     `git checkout -f`, which discards uncommitted work without asking. vendor/monad is a working
#     clone parked on someone's branch; a series build over it is data loss, not a race.
#   * `START=$(git rev-parse --abbrev-ref HEAD)` to remember where to put the tree back. On a tree
#     already in detached HEAD that yields the string "HEAD", and the restoring `git checkout HEAD`
#     is a no-op: the tree is left on the last commit of the series and the original is gone.
#
# The lock lives in the checkout's OWN git dir (per-worktree, never inside the working tree, so it
# cannot show up as a modification or be swept by `checkout -f -- .`).

# Path defaults (series_repo, series_monad_default, series_corpus) live in one file, next door.
. "$(dirname "${BASH_SOURCE[0]}")/paths.sh"

series_tree_lockfile() {
    local gd; gd=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
    printf '%s/zkvm-bench-series.lock\n' "$gd"
}

# series_tree_busy <dir> — 0 if a LIVE series build holds the lock. For watchers.
series_tree_busy() {
    local f pid; f=$(series_tree_lockfile "$1") || return 1
    [ -f "$f" ] || return 1
    pid=$(cut -d' ' -f1 "$f" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# series_tree_claim <dir> — take the lock, refuse a busy or dirty tree, remember HEAD, and install
# the release + restore trap. Sets SERIES_TREE_START. Exits the caller on refusal.
series_tree_claim() {
    local d="$1" f pid dirty
    [ -d "$d" ] || { echo "no such checkout: $d" >&2; exit 2; }
    f=$(series_tree_lockfile "$d") || { echo "not a git checkout: $d" >&2; exit 2; }

    if series_tree_busy "$d"; then
        pid=$(cut -d' ' -f1 "$f")
        echo "another series build owns $d (pid $pid, since $(cut -d' ' -f2- "$f"))" >&2
        echo "  wait for it, or point MONAD at a worktree of your own" >&2
        exit 3
    fi
    rm -f "$f"

    # The clobber guard. `git checkout -f` is coming; anything uncommitted here dies with it.
    dirty=$(git -C "$d" status --porcelain 2>/dev/null | head -5)
    if [ -n "$dirty" ]; then
        echo "$d has uncommitted changes, and this script drives it with \`git checkout -f\`:" >&2
        echo "$dirty" | sed 's/^/    /' >&2
        echo "  commit, stash, or point MONAD at a worktree of your own (see RUNBOOK § series)" >&2
        exit 4
    fi

    # A branch by name when there is one, the exact commit when the tree is detached.
    SERIES_TREE_START=$(git -C "$d" symbolic-ref --quiet --short HEAD 2>/dev/null) \
        || SERIES_TREE_START=$(git -C "$d" rev-parse HEAD)
    printf '%s %s\n' "$$" "$(date '+%Y-%m-%d %H:%M:%S')" > "$f"
    SERIES_TREE_DIR="$d" SERIES_TREE_LOCK="$f"
    export SERIES_TREE_DIR SERIES_TREE_LOCK SERIES_TREE_START
    trap 'series_tree_relinquish' EXIT
    echo "claimed $d (was on $SERIES_TREE_START)"
}

series_tree_relinquish() {
    [ -n "${SERIES_TREE_DIR:-}" ] || return 0
    git -C "$SERIES_TREE_DIR" checkout -f -q "$SERIES_TREE_START" 2>/dev/null \
        || echo "WARNING: could not restore $SERIES_TREE_DIR to $SERIES_TREE_START" >&2
    rm -f "$SERIES_TREE_LOCK"
}
