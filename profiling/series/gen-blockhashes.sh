#!/bin/bash
# gen-blockhashes.sh [gen-name] [jobs] — write <block>.blockhash beside each witness of a corpus.
#
# From `guest: only expose blockhash as a public input` the guest emits the block hash where it
# used to emit the post-state root, so the gate and series-measure.sh -- both of which compare
# output[:32] against <block>.post_state_root -- reject every block of every commit after it. The
# guests are right and the reference is missing; this makes it.
#
# TWO INDEPENDENT SOURCES, and a disagreement is a hard failure rather than a preference:
#
#   ziskethone   a different guest, different codebase, different toolchain. It emits the block
#                hash as its own public output, so it is a witness to the value and not a copy of
#                our belief about it.
#   witness N+1  the header of the next block opens with parent_hash, which IS the hash of N. Read
#                straight out of the corpus, so it cannot drift from the blocks being measured.
#
# Deriving the reference from the guest under test would certify nothing: it would say the guest
# agrees with itself. The last block of a corpus has no successor, so it rests on ziskethone alone
# and the summary says how many blocks each source covered.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(cd "$HERE/../.." && pwd)"
GEN="${1:-$(basename "$(readlink "$BENCH/guests/monad/current" 2>/dev/null)")}"
JOBS="${2:-6}"
W="$BENCH/guests/monad/gen/$GEN/witnesses"
ZE="${ZE:-$BENCH/vendor/zisk-eth-client/bin/guests/stateless-validator-ziskethone/elf/zec-ziskethone.elf}"
EMU="${EMU:-$HOME/.zisk-1.2/bin/ziskemu}"
# parent_hash sits near the head of the successor's witness, but NOT at a fixed offset: the RLP
# prefix ahead of it changes length with the block number, which moved it by two bytes on 10 of
# 200 blocks. So the check is "ziskethone's hash occurs within the first PARENT_HASH_WINDOW bytes"
# -- an offset small enough that only the header can hold it, and no parse of our own.
PARENT_HASH_WINDOW="${PARENT_HASH_WINDOW:-64}"

[ -d "$W" ] || { echo "no such corpus: $W" >&2; exit 2; }
[ -f "$ZE" ] || { echo "no ziskethone ELF at $ZE" >&2; exit 2; }
[ -x "$EMU" ] || { echo "no ziskemu at $EMU" >&2; exit 2; }

RUN="$(mktemp -d)"; trap 'rm -rf "$RUN"' EXIT
export RUN W ZE EMU PARENT_HASH_WINDOW BENCH

one() {
    local b="$1" zeg out zh wh
    zeg="$BENCH/guests/ziskethone/fixtures/1-$b.bin"
    [ -f "$zeg" ] || zeg="$BENCH/guests/ziskethone/inputs/1-$b.bin"
    [ -f "$zeg" ] || { echo "NOINPUT $b"; return; }
    out="$RUN/$b.out"
    "$EMU" -e "$ZE" -i "$zeg" -o "$out" >/dev/null 2>&1
    zh=$(xxd -p -l32 "$out" 2>/dev/null | tr -d '\n')
    # 256 zero bytes is what ZisK leaves when a guest wrote nothing; it is not a hash.
    case "$zh" in ''|0000000000000000000000000000000000000000000000000000000000000000)
        echo "NORUN $b"; return ;; esac
    # The successor's parent_hash, when there is a successor.
    if [ -f "$W/$((b+1)).witness" ]; then
        off=$(python3 - "$W/$((b+1)).witness" "$zh" "$PARENT_HASH_WINDOW" <<'PY'
import binascii, sys
blob = open(sys.argv[1], 'rb').read(int(sys.argv[3]) + 32)
print(blob.find(binascii.unhexlify(sys.argv[2])))
PY
)
        # An EMPTY off is a broken check, not a passing one. The previous version exported the
        # wrong variable name, python raised, `[ "" -lt 0 ]` printed an error and evaluated false,
        # and all 199 blocks reported as cross-checked without a single comparison having run.
        case "$off" in ''|*[!0-9-]*) echo "CHECKFAIL $b (offset probe produced '$off')"; return ;; esac
        if [ "$off" -lt 0 ]; then
            echo "DISAGREE $b ziskethone=$zh not in the first $PARENT_HASH_WINDOW bytes of $((b+1))"; return
        fi
        echo "BOTH $b $zh @$off"
    else
        echo "ZEGONLY $b $zh"
    fi
}
export -f one

ls "$W"/*.witness 2>/dev/null | sed 's/.*\///;s/\.witness//' | sort -n > "$RUN/blocks"
n_in=$(wc -l < "$RUN/blocks" | tr -d ' ')
[ "$n_in" -gt 0 ] || { echo "corpus $W holds no .witness -- refusing" >&2; exit 3; }
echo "corpus $GEN: $n_in blocks, $JOBS at a time"
xargs -P "$JOBS" -I{} bash -c 'one "$@"' _ {} < "$RUN/blocks" > "$RUN/res"

bad=$(grep -cvE '^(BOTH|ZEGONLY) ' "$RUN/res" || true)
if [ "$bad" != 0 ]; then
    echo "REFUSING to write: $bad block(s) did not produce an agreed hash" >&2
    grep -vE '^(BOTH|ZEGONLY) ' "$RUN/res" | head -5 >&2
    exit 4
fi
# Written only once every block agreed: a half-written reference is worse than none, because the
# gate would pass on the blocks that have one and fail on the blocks that do not.
while read -r kind b h _off; do printf '0x%s\n' "$h" > "$W/$b.blockhash"; done < "$RUN/res"
echo "wrote $(grep -c . "$RUN/res") .blockhash files"
echo "  cross-checked against the next witness: $(grep -c '^BOTH ' "$RUN/res")"
echo "  ziskethone only (no successor):         $(grep -c '^ZEGONLY ' "$RUN/res")"
