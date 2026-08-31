#!/bin/bash
# fill-gaps.sh — measure the blocks that only SOME ELFs of the r10 table have, so the curve is one
# block set. 9 blocks sit at corpus indices 20/40/140/160/260/280/380/400/500 (offsets 16 and 20 of
# STRIDE 24, both only partly measured), and 45 of the 95 ELFs lack them. A ratio is still honest
# without this -- report.py intersects each ELF with the base -- but n then varies from 105 to 114
# and a Δ between two adjacent rows can be a step between medians taken on DIFFERENT sets.
#
# STRIDE=504 with OFF=<index> selects exactly that one witness, which is how this reuses
# series-measure.sh instead of restating its two ziskemu passes.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec >> "$HERE/fill-gaps.log" 2>&1
echo "=== start $(date)"
for i in 20 40 140 160 260 280 380 400 500; do
  STRIDE=504 INDEX="$HERE/r10-index.tsv" OUT="$HERE/r10-measure.tsv" \
    "$HERE/series-measure.sh" "$i" 6 || echo "index $i returned $?"
done
echo "--- measure now $(wc -l < "$HERE/r10-measure.tsv") rows"
cd "$HERE/.." && python3 series/report.py --index r10-index.tsv --measure r10-measure.tsv \
  --branch al/zkvm-r10 --out results/series-r10.html --no-sp1
echo "=== done $(date)"
