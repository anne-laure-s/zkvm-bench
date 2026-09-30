# subject-ref.sh — resolve a commit by its SUBJECT, inside a range. SOURCED, not executed.
#
# A rebase rewrites every sha it touches and keeps every subject. Anchoring a build recipe or a
# lineage base on a sha therefore breaks on each restack, and it breaks QUIETLY where it matters
# most: an @after anchor whose commit no longer exists simply stops matching, so the recipe it
# carried is not applied and the guest builds with its options at their defaults. Measured on the
# r10-zisk stack: twenty-seven commits built without the official profile, the keccak memo
# reporting exactly 0 because it was never compiled in, and nothing in the run said so.
#
# The subject is the stable name of a commit across a rebase. It is not unique in principle, so
# this refuses an ambiguous match rather than taking the first: a recipe applied to the wrong
# commit is the failure it exists to prevent.
#
#   subject_ref <range> <text>   -> prints one full sha, or fails with a message on stderr
#
# `text` is matched as a fixed substring of the subject, so "start an audited ZisK 1.2 build
# profile" and a distinctive prefix of it behave the same.
subject_ref() {
    local range="$1" text="$2" hits n
    # Field 2 only, so a text that happens to be hex cannot match a commit id.
    hits=$(git log --format='%H%x09%s' "$range" 2>/dev/null | awk -F'\t' -v t="$text" 'index($2,t){print $1}')
    n=$(printf '%s\n' "$hits" | grep -c . || true)
    if [ "$n" = 0 ]; then
        echo "subject-ref: no commit in $range has a subject containing: $text" >&2
        return 1
    fi
    if [ "$n" != 1 ]; then
        echo "subject-ref: $n commits in $range match: $text" >&2
        git log --format='  %h %s' "$range" | awk -v t="$text" 'index($0,t)' >&2
        echo "  refusing to guess — make the text distinctive" >&2
        return 1
    fi
    printf '%s\n' "$hits"
}

# `subject:<text>` resolves through the above; anything else is handed to git as-is. One place
# decides what a reference may look like, so --base, the sidecar and the preflight cannot disagree.
resolve_ref() {
    local range="$1" ref="$2"
    case "$ref" in
        subject:*) subject_ref "$range" "${ref#subject:}" ;;
        *) git rev-parse --verify "$ref^{commit}" 2>/dev/null || {
               echo "subject-ref: not a commit: $ref" >&2; return 1; } ;;
    esac
}
