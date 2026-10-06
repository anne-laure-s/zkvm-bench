#!/usr/bin/env bash
# summary.sh — turn a results.csv from t1…t5 into the two tables you actually compare.
#
#   bash tests/summary.sh <results.csv>            # one run
#   bash tests/summary.sh ~/tests/*/results.csv    # several runs at once (configs must be uniquely named)
#
# Derived numbers live HERE and not in the CSV on purpose: fixing the arithmetic must never mean
# re-running a 45-minute benchmark. The CSV keeps only what was measured.
#
# MEDIAN, not mean, per (config, block): on a shared host a single neighbour spike moves a mean and
# leaves a median alone. With PASSES=2 the "median" is the lower of the two, which is the right
# reading — the faster pass is the one less contaminated by someone else's work.
#
# THE PER-CONFIG ROLLUP USES ONLY BLOCKS COMMON TO EVERY CONFIG. Throughput rises with block size
# (fixed overhead amortises), so an arm that skipped a block — missing witness, a failed pass — would
# otherwise be ranked on an easier mix and could "win" while being identical or slower block for
# block. That is not a hypothetical: it is the first thing that happened when this script was tested.
#
# awk only (no gawk asort, no python): mawk is what Ubuntu ships.
set -uo pipefail

[[ $# -ge 1 ]] || { echo "usage: $0 <results.csv> [more.csv …]" >&2; exit 2; }
TMP="$(mktemp)"; trap 'rm -f "$TMP" "$TMP".*' EXIT
for f in "$@"; do [[ -f "$f" ]] && tail -n +2 "$f"; done | grep -v '^[[:space:]]*$' > "$TMP"
[[ -s "$TMP" ]] || { echo "(no data rows)"; exit 0; }

FAILED="$(awk -F, '$18!=0' "$TMP" | wc -l | tr -d ' ')"
TOTAL="$(wc -l < "$TMP" | tr -d ' ')"

# ── per (config, block) medians ────────────────────────────────────────────────────────────────
# Sorted by config, block, then wall_secs ascending → the median is a positional pick in awk.
awk -F, '$18==0' "$TMP" | sort -t, -k2,2 -k4,4 -k6,6n | awk -F, -v meta="$TMP.meta" '
{
  k = $2 SUBSEP $4
  if (!(k in n)) { cfg[k]=$2; tag[k]=$4; ms[k]=$5 }
  n[k]++; v[k, n[k]] = $6
  ex[k]+=$7; co[k]+=$8; inn[k]+=$9; fin[k]+=$10; if ($11!="") { mhz[k]+=$11; mhzn[k]++ }
  g[$2]=$13; nl[$2]=$14; nt[$2]=$15; st[$2]=$16; cap[$2]=$17
}
END {
  for (k in n) {
    med = v[k, int((n[k]+1)/2)] + 0
    mms = (ms[k]=="" ? 0 : ms[k]/1e6)
    mps = (mms > 0 && med > 0) ? mms/med : 0
    per = (mps > 0 && g[cfg[k]] > 0) ? mps/g[cfg[k]] : 0
    printf "%s\t%s\t%.0f\t%d\t%.2f\t%.2f\t%.3f\t%.0f\t%.0f\t%.0f\t%.0f\t%.0f\n",
      cfg[k], tag[k], mms, n[k], med, mps, per,
      ex[k]/n[k], co[k]/n[k], inn[k]/n[k], fin[k]/n[k], (mhzn[k] ? mhz[k]/mhzn[k] : 0)
  }
  for (c in g) printf "%s\t%s\t%s\t%s\t%s\t%s\n", c, g[c], nl[c], nt[c], (st[c]==""?"-":st[c]), (cap[c]==""?"-":cap[c]) > meta
}' | sort -k1,1 -k3,3g > "$TMP.tags"

# ── per-config rollup, restricted to the common block set ──────────────────────────────────────
awk -v meta="$TMP.meta" '
{ cfgs[$1]=1; tags[$2]=1; seen[$1 SUBSEP $2]=1; sec[$1 SUBSEP $2]=$5; msb[$1 SUBSEP $2]=$3; mhz[$1 SUBSEP $2]=$12 }
END {
  nc = 0; for (c in cfgs) nc++
  ncommon = 0; nskipped = 0
  for (t in tags) { k = 0; for (c in cfgs) if ((c SUBSEP t) in seen) k++
                    if (k == nc) { common[t]=1; ncommon++ } else { skipped[t]=1; nskipped++ } }
  while ((getline line < meta) > 0) { split(line, m, "\t"); G[m[1]]=m[2]; NL[m[1]]=m[3]; NT[m[1]]=m[4]; ST[m[1]]=m[5]; CAP[m[1]]=m[6] }
  for (c in cfgs) {
    ss = 0; mm = 0; nb = 0; hz = 0; hn = 0
    for (t in common) { ss += sec[c SUBSEP t]; mm += msb[c SUBSEP t]; nb++
                        if (mhz[c SUBSEP t] > 0) { hz += mhz[c SUBSEP t]; hn++ } }
    mps = (ss > 0) ? mm/ss : 0
    per = (mps > 0 && G[c] > 0) ? mps/G[c] : 0
    # FIELD ORDER — the sort key and the "best" pick below depend on it, so it is spelled out:
    #   1 config  2 gpus  3 numa_local/total  4 streams  5 cap  6 blocks  7 total_s
    #   8 Msteps/s  9 /GPU  10 asm_MHz
    # An empty cell would make `column -t` shift every following column left, so blanks become "-".
    printf "%s\t%s\t%s/%s\t%s\t%s\t%d\t%.2f\t%.2f\t%.3f\t%.0f\n",
      c, (G[c]==""?"-":G[c]), (NL[c]==""?"-":NL[c]), (NT[c]==""?"-":NT[c]),
      (ST[c]==""?"-":ST[c]), (CAP[c]==""?"-":CAP[c]), nb, ss, mps, per, (hn ? hz/hn : 0)
  }
  if (nskipped > 0) { s=""; for (t in skipped) s = s " " t
                      printf "SKIPPED\t%s\n", s }
}' "$TMP.tags" > "$TMP.cfgraw"

# Column 9 is /GPU (see the FIELD ORDER comment above). Sorting or ranking on 10 would order by
# asm_MHz while the header claims throughput — which inverts the ranking whenever the two disagree,
# and silently agrees whenever they happen to correlate. That bug shipped once; hence the comment.
PERGPU_COL=9
grep -v '^SKIPPED' "$TMP.cfgraw" | sort -k${PERGPU_COL},${PERGPU_COL}gr > "$TMP.cfg"
SKIPPED="$(grep '^SKIPPED' "$TMP.cfgraw" | cut -f2- || true)"

echo "════════ per config — common blocks only (sorted by throughput per GPU, best first) ════════"
{ printf 'config\tgpus\tnuma_local\tstreams\tcap\tblocks\ttotal_s\tMsteps/s\t/GPU\tasm_MHz\n'
  cat "$TMP.cfg"; } | column -t -s $'\t'

BEST="$(awk -v c="$PERGPU_COL" 'NR==1{print $c}' "$TMP.cfg")"
if [[ -n "${BEST:-}" && "$(wc -l < "$TMP.cfg")" -gt 1 ]]; then
  echo
  echo "  slower than the best config, per-GPU:"
  awk -v b="$BEST" -v c="$PERGPU_COL" 'NR>1 { printf "    %-16s %6.2fx\n", $1, (b>0 && $c>0 ? b/$c : 0) }' "$TMP.cfg"
fi
[[ -n "$SKIPPED" ]] && {
  echo
  echo "  ⚠️  blocks NOT common to every config, excluded from the rollup:$SKIPPED"
  echo "      (they are still in the per-block table below — compare those rows directly)"
}

echo
echo "════════ per config × block (median of passes) ════════"
{ printf 'config\tblock\tMsteps\tn\tmedian_s\tMsteps/s\t/GPU\texec_ms\tcontrib_ms\tinner_ms\tfinal_ms\tasm_MHz\n'
  cat "$TMP.tags"; } | column -t -s $'\t'

echo
# Reference points, so a number does not have to be carried in someone's head to be meaningful.
echo "════════ reference points ════════"
echo "  ZisK's live ethproofs cluster (Zisk 16x5090, checked 2026-08-13) : 8.2 s avg / mainnet block"
echo "  our 16-GPU fit (RTP-FINDINGS.md)  : prove_secs = 2.12 + 0.0364 x Msteps  → 1.72 Msteps/s/GPU"
echo "  mainnet distribution, zisk-reth   : median 227 Msteps · mean 244 · p90 388 · max 666"
echo "  ZisK's published asm trace speed  : 1500 MHz   (we measure 719-809)"
[[ "$FAILED" != 0 ]] && echo "  ⚠️  $FAILED of $TOTAL rows had rc != 0 and were EXCLUDED — check the per-block .log files"
exit 0
