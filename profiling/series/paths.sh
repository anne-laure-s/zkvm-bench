# paths.sh — where things live, derived from this file. SOURCED, not executed.
#
# One definition per path, in one file. The absolute `/Users/<someone>/...` these replace were
# copied across fourteen scripts: unportable, and fourteen places to keep in step — the shape of
# drift the lineage builder was consolidated to avoid.
series_repo() {
    (cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
}

# The monad checkout. Resolves to the repo's own `vendor/monad`, a SHARED working clone -- which is
# why every caller claims it through series_tree_claim before touching it, the builders driving the
# tree with `git checkout -f`. For a lineage walked unattended, point MONAD at a worktree of its own.
series_monad_default() {
    printf '%s\n' "$(series_repo)/vendor/monad"
}

# A witness corpus, by generation. The generation stays named at each call site on purpose: WHICH
# corpus a gate ran against is part of its result, and burying it here would make "200 roots
# verified" unfalsifiable when another generation holds 504.
series_corpus() {
    printf '%s\n' "$(series_repo)/guests/monad/gen/$1/witnesses"
}
