# root-ref.sh — which public output a guest is expected to emit. SOURCED, not executed.
#
# `guest: only expose blockhash as a public input` changed write_output from the post-state root
# to the block hash. Both are correct answers, for different commits of the same lineage, and a
# check that knows only one of them rejects every block on one side of that commit -- which is how
# a correct guest came to read as 0/200 for two days.
#
# So the reference is a SET, and the caller is told which member matched. Not "try until something
# passes": each file is an independently established value (see gen-blockhashes.sh), and matching
# neither is still a failure. What this removes is the assumption that a lineage emits one thing.
#
#   root_match <witness-path> <got-hex>  -> prints the matching kind, or fails
root_match() {
    local w="$1" got="$2" want kind
    [ "${#got}" = 64 ] || return 1
    for kind in post_state_root blockhash; do
        [ -f "${w%.witness}.$kind" ] || continue
        want=$(sed 's/^0x//' "${w%.witness}.$kind" | tr -d '\n')
        [ "${#want}" = 64 ] || continue
        if [ "$got" = "$want" ]; then printf '%s\n' "$kind"; return 0; fi
    done
    return 1
}

# Which references exist for a witness at all, so "no reference" and "wrong value" stay distinct.
root_refs() {
    local w="$1" kind out=""
    for kind in post_state_root blockhash; do
        [ -f "${w%.witness}.$kind" ] && out="$out $kind"
    done
    printf '%s\n' "${out# }"
}
