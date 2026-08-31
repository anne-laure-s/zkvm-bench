#!/bin/bash
# run-r10.sh — build/gate/compare the r10 tip first, then extend and render the full series.
# Lives in the repo and NOT in /tmp: the previous driver was written to /tmp and macOS purged it
# between the launch and the wake-up, so the overnight run never started and nothing said so.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(cd "$HERE/../.." && pwd)"

usage() {
  cat <<'EOF'
usage: ./profiling/series/run-r10.sh [--nb-block N] [--skip-build] [--check]

  --nb-block N   representative nested sample of N canonical blocks (default: 200)
  --skip-build   reuse valid existing ELF files; build only missing/new commits
  --check        check every prerequisite and exit without building or measuring

This does not narrow the soundness gate or compare.py: both keep their full corpus.
Fresh-clone setup: profiling/series/RUN-R10.md
EOF
}

NB_BLOCK=200
SKIP_BUILD=0
CHECK_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --nb-block)
      [ -n "${2:-}" ] || { echo "--nb-block needs a value" >&2; exit 2; }
      NB_BLOCK="$2"; shift 2 ;;
    --nb-block=*) NB_BLOCK="${1#*=}"; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$NB_BLOCK" in
  ''|*[!0-9]*) echo "--nb-block must be a positive integer" >&2; exit 2 ;;
esac
[ "$NB_BLOCK" -gt 0 ] || { echo "--nb-block must be greater than zero" >&2; exit 2; }
[ "$NB_BLOCK" -le 200 ] || { echo "--nb-block $NB_BLOCK exceeds the canonical 200-block corpus" >&2; exit 2; }

# These defaults are portable and individually overridable. The build recipe expands
# `{toolchain}` to SERIES_TOOLCHAIN_DIR; it never records one developer's home directory.
MEASURE_GEN="$BENCH/guests/monad/gen/canonical-2026-08-25815000-25815199-d49075fa3/witnesses"
MONAD_TREE="${SERIES_MONAD:-$(cd "$BENCH/.." && pwd)/monad-series}"
SERIES_TOOLCHAIN_DIR="${SERIES_TOOLCHAIN_DIR:-${RISCV_TOOLCHAIN_DIR:-$HOME/.local/xPacks/zisk-dma-gcc-15.2.0}}"
SERIES_STOCK_TOOLCHAIN_DIR="${SERIES_STOCK_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
export SERIES_TOOLCHAIN_DIR SERIES_STOCK_TOOLCHAIN_DIR

preflight_error() { echo "preflight: ERROR: $*" >&2; PREFLIGHT_ERRORS=$((PREFLIGHT_ERRORS+1)); }
preflight() {
  PREFLIGHT_ERRORS=0
  command -v python3 >/dev/null 2>&1 || preflight_error "python3 is not installed"
  if [ ! -x "$HOME/.zisk/bin/ziskemu" ]; then
    preflight_error "no $HOME/.zisk/bin/ziskemu (install ZisK 1.1.0-alpha with ziskup)"
  else
    case $("$HOME/.zisk/bin/ziskemu" --version 2>&1) in
      *1.1.0-alpha*) ;;
      *) preflight_error "ziskemu is not the pinned 1.1.0-alpha runtime" ;;
    esac
  fi
  if [ ! -x "$HOME/.zisk/bin/cargo-zisk" ]; then
    preflight_error "no $HOME/.zisk/bin/cargo-zisk (install ZisK 1.1.0-alpha with ziskup)"
  else
    case $("$HOME/.zisk/bin/cargo-zisk" --version 2>&1) in
      *1.1.0-alpha*) ;;
      *) preflight_error "cargo-zisk is not the pinned 1.1.0-alpha runtime" ;;
    esac
  fi
  if [ ! -x "$SERIES_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++" ]; then
    preflight_error "no patched GCC 15.2.0 at $SERIES_TOOLCHAIN_DIR (set SERIES_TOOLCHAIN_DIR)"
  else
    case $("$SERIES_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++" --version 2>&1) in
      *15.2.0*) ;;
      *) preflight_error "patched compiler at $SERIES_TOOLCHAIN_DIR is not GCC 15.2.0" ;;
    esac
    "$SERIES_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++" \
        -mzisk-dma -E -x c++ /dev/null -o /dev/null >/dev/null 2>&1 || \
      preflight_error "GCC at $SERIES_TOOLCHAIN_DIR does not support -mzisk-dma"
  fi
  if [ ! -x "$SERIES_STOCK_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++" ]; then
    preflight_error "no stock GCC 15.2.0 at $SERIES_STOCK_TOOLCHAIN_DIR (set SERIES_STOCK_TOOLCHAIN_DIR)"
  else
    case $("$SERIES_STOCK_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++" --version 2>&1) in
      *15.2.0*) ;;
      *) preflight_error "stock compiler at $SERIES_STOCK_TOOLCHAIN_DIR is not GCC 15.2.0" ;;
    esac
  fi
  [ -f "$BENCH/guests/ziskethone/ziskethone.elf" ] || \
    preflight_error "missing tracked reference ELF guests/ziskethone/ziskethone.elf"

  missing_zeg=0; first_missing_zeg=""; block=25815000
  while [ "$block" -le 25815199 ]; do
    if [ ! -f "$BENCH/guests/ziskethone/fixtures/1-$block.bin" ] && \
       [ ! -f "$BENCH/guests/ziskethone/inputs/1-$block.bin" ]; then
      missing_zeg=$((missing_zeg+1)); [ -n "$first_missing_zeg" ] || first_missing_zeg="$block"
    fi
    block=$((block+1))
  done
  [ "$missing_zeg" = 0 ] || preflight_error "$missing_zeg/200 ZisKethone fixtures are missing (first: $first_missing_zeg)"
  if [ "$missing_zeg" = 0 ]; then
    zeg_digest=$(
      block=25815000
      while [ "$block" -le 25815199 ]; do
        f="$BENCH/guests/ziskethone/fixtures/1-$block.bin"
        [ -f "$f" ] || f="$BENCH/guests/ziskethone/inputs/1-$block.bin"
        shasum -a 256 "$f" | cut -d' ' -f1
        block=$((block+1))
      done | shasum -a 256 | cut -d' ' -f1
    )
    [ "$zeg_digest" = 184512672a39f45e51689b89a7204172040663df0c3525bc53fbc2609e72753b ] || \
      preflight_error "ZisKethone corpus digest differs from the canonical 25815000-25815199 set"
  fi

  if [ ! -d "$MEASURE_GEN" ]; then
    preflight_error "missing canonical Monad corpus $MEASURE_GEN"
  else
    witness_count=$(find -L "$MEASURE_GEN" -maxdepth 1 -type f -name '*.witness' | wc -l | tr -d ' ')
    root_count=$(find -L "$MEASURE_GEN" -maxdepth 1 -type f -name '*.post_state_root' | wc -l | tr -d ' ')
    [ "$witness_count" = 200 ] || preflight_error "canonical Monad corpus has $witness_count/200 witnesses"
    [ "$root_count" = 200 ] || preflight_error "canonical Monad corpus has $root_count/200 post-state roots"
    if [ "$witness_count" = 200 ] && [ "$root_count" = 200 ]; then
      monad_digest=$(
        cd "$MEASURE_GEN" &&
        find . -maxdepth 1 -type f \( -name '*.witness' -o -name '*.post_state_root' \) -print0 \
          | LC_ALL=C sort -z | xargs -0 shasum -a 256 | awk '{print $1}' \
          | shasum -a 256 | cut -d' ' -f1
      )
      [ "$monad_digest" = 5bc3267f24399a18b2554ca6e0d92a738bad5daa8502a7c5438e45d9de263e5f ] || \
        preflight_error "Monad corpus digest differs from canonical-2026-08-25815000-25815199-d49075fa3"
    fi

    if [ ! -e "$BENCH/guests/monad/fixtures" ]; then
      preflight_error "guests/monad/fixtures is absent (run guests/monad/use-gen zkvm-r8-canonical-25815000-25815199-0df7094a1)"
    fi
    bad_monad=0; first_bad_monad=""
    for witness in "$MEASURE_GEN"/*.witness; do
      [ -f "$witness" ] || continue
      block=$(basename "$witness" .witness)
      compare_witness="$BENCH/guests/monad/fixtures/$block.witness"
      if [ ! -f "$compare_witness" ] || ! cmp -s "$witness" "$compare_witness"; then
        bad_monad=$((bad_monad+1)); [ -n "$first_bad_monad" ] || first_bad_monad="$block"
      fi
    done
    [ "$bad_monad" = 0 ] || preflight_error "$bad_monad Monad compare fixtures differ or are missing (first: $first_bad_monad)"
  fi

  if [ ! -d "$MONAD_TREE/.git" ] && [ ! -f "$MONAD_TREE/.git" ]; then
    preflight_error "no dedicated Monad worktree at $MONAD_TREE"
  else
    TARGET_COMMIT=$(git -C "$MONAD_TREE" rev-parse --verify origin/al/zkvm-r10 2>/dev/null) || \
      preflight_error "origin/al/zkvm-r10 is unknown in $MONAD_TREE (fetch it first)"
    git -C "$MONAD_TREE" cat-file -e 3d237fe69^{commit} 2>/dev/null || \
      preflight_error "lineage base 3d237fe69 is absent from $MONAD_TREE"
    git -C "$MONAD_TREE" cat-file -e 142989e81^{commit} 2>/dev/null || \
      preflight_error "build compatibility commit 142989e81 is absent from $MONAD_TREE"
    # A rebase may remove the commit used as an @after anchor. Without this
    # check the tip silently falls back to the stock recipe, spends minutes
    # building and gating, then compare.py rejects it for lacking the official
    # profile. Resolve the sidecar exactly as the lineage builder does.
    if [ -n "${TARGET_COMMIT:-}" ]; then
      tip_short=$(git -C "$MONAD_TREE" log -1 --format='%h' "$TARGET_COMMIT")
      tip_recipe=$(grep -m1 "^$tip_short	" "$HERE/r10-buildenv.tsv" || true)
      if [ -z "$tip_recipe" ]; then
        while IFS=$'\t' read -r -a recipe_fields; do
          [ "${recipe_fields[0]:-}" = '@after' ] || continue
          anchor="${recipe_fields[1]:-}"; [ -n "$anchor" ] || continue
          anchor_full=$(git -C "$MONAD_TREE" rev-parse "$anchor" 2>/dev/null) || continue
          [ "$TARGET_COMMIT" != "$anchor_full" ] || continue
          git -C "$MONAD_TREE" merge-base --is-ancestor "$anchor" "$TARGET_COMMIT" 2>/dev/null || continue
          tip_recipe="${recipe_fields[*]:2}"
        done < "$HERE/r10-buildenv.tsv"
      fi
      case "$tip_recipe" in
        *MONAD_ZKVM_OFFICIAL_PROFILE=ON*) ;;
        *) preflight_error "r10-buildenv.tsv does not select MONAD_ZKVM_OFFICIAL_PROFILE=ON for tip ${TARGET_COMMIT:0:9} (an @after anchor may have been rebased away)" ;;
      esac
    fi
    dirty=$(git -C "$MONAD_TREE" status --porcelain 2>/dev/null | head -1)
    [ -z "$dirty" ] || preflight_error "$MONAD_TREE has uncommitted changes"
  fi

  for old in r8-zbkb/monad-r8-zbkb-zisk.elf r9-flatdirty/monad-r9-flatdirty-zisk.elf; do
    [ -f "$BENCH/guests/monad-variants/$old" ] || \
      echo "preflight: warning: guests/monad-variants/$old absent; that historical compare axis will be omitted" >&2
  done
  [ "$PREFLIGHT_ERRORS" = 0 ] || {
    echo "preflight: $PREFLIGHT_ERRORS error(s); see profiling/series/RUN-R10.md" >&2
    return 1
  }
  echo "preflight: OK — 200 paired inputs, both toolchains, emulator and Monad lineage available"
}

preflight || exit 2
[ "$CHECK_ONLY" = 0 ] || exit 0

# The canonical corpus is the same 200-block range compare.py measures. Smaller
# samples are stable hash-ranked prefixes, independent of block order; growing
# N therefore improves coverage while reusing every earlier row.
SAMPLE_DIR=$(mktemp -d); trap 'rm -rf "$SAMPLE_DIR"' EXIT
find -L "$MEASURE_GEN" -maxdepth 1 -type f -name '*.witness' | LC_ALL=C sort > "$SAMPLE_DIR/all"
TOTAL=$(wc -l < "$SAMPLE_DIR/all" | tr -d ' ')
[ "$NB_BLOCK" -le "$TOTAL" ] || {
  echo "--nb-block $NB_BLOCK exceeds the corpus ($TOTAL blocks)" >&2; exit 2; }
python3 "$HERE/select-blocks.py" --all "$SAMPLE_DIR/all" --count "$NB_BLOCK" \
  > "$SAMPLE_DIR/selected"
SELECTED=$(wc -l < "$SAMPLE_DIR/selected" | tr -d ' ')
[ "$SELECTED" = "$NB_BLOCK" ] || {
  echo "sample construction failed: requested $NB_BLOCK, selected $SELECTED" >&2; exit 2; }

LOG="$HERE/run-r10.log"
exec > >(tee -a "$LOG") 2>&1
if [ "$SKIP_BUILD" -eq 1 ]; then mode=" — incremental build reuse"; else mode=""; fi
echo "=== start $(date) — series sample $NB_BLOCK/$TOTAL blocks$mode"

# Freeze the remote-tracking target once; preflight resolved it before any expensive work.
echo "--- frozen target origin/al/zkvm-r10 at ${TARGET_COMMIT:0:9}"

# Build (or reuse in incremental mode) only the remote-tracking tip first. This
# one-row index lets gate + compare run before the historical lineage build.
# The full pass below consumes it as a trusted seed and does not build the tip twice.
MONAD="$MONAD_TREE" \
BRANCH="$TARGET_COMMIT" \
BUILDFIX=142989e81 \
REUSE_BUILDS="$SKIP_BUILD" \
REUSE_INDEX="$HERE/r10-index.tsv" \
ONLY_TIP=1 \
INDEX="$HERE/r10-tip-index.tsv" \
BUILDENV="$HERE/r10-buildenv.tsv" \
  "$HERE/series-build-lineage.sh" || { echo "BUILD STAGE FAILED"; exit 1; }

TIP_COMMIT=$(awk -F'\t' 'NF{print $2}' "$HERE/r10-tip-index.tsv")
TIP=$(awk -F'\t' 'NF{print $4}' "$HERE/r10-tip-index.tsv")
TIP_SUBJECT=$(awk -F'\t' 'NF{print $5}' "$HERE/r10-tip-index.tsv")
echo "--- tip: commit $TIP_COMMIT ($TIP_SUBJECT), elf $TIP"
[ -f "$HERE/elf/$TIP.elf" ] || { echo "no elf for tip"; exit 1; }

# The gate is the only thing standing between a broken tip and a published ratio, so its exit
# code is not swallowed into a neutral-looking line. A failure aborts before compare: an HTML
# report detached from its terminal warning would otherwise look publishable.
# gate-roots-record.sh, not gate-roots.sh: this run is unattended, and the plain gate keeps its
# per-block verdicts in a mktemp dir it deletes on exit — all that survives is one line in this
# log. A lever questioned next week cannot be re-checked against that. The TSV names the ELF
# sha, the corpus, and every block's two roots, so a disagreement can be located.
GATE=0; REUSE=1 GEN="$MEASURE_GEN" "$HERE/gate-roots-record.sh" "$HERE/elf/$TIP.elf" \
          "$HERE/r10-gate-$TIP.tsv" 6 || GATE=$?
if [ "$GATE" = 0 ]; then
  echo "--- gate OK"
else
  echo "--- GATE FAILED (rc=$GATE) — compare and historical builds not started" >&2
  exit "$GATE"
fi

# No repoint stage. r10tip-vs-ziskethone declares the tip-only index and compare.py
# resolves it from the index at run time, so the axis is at the tip this run just built by
# construction rather than by an edit somebody had to remember. The report stamps the sha it
# actually measured, so following the tip costs no attribution. Still assert it agrees with the
# tip computed above -- if they ever differ, the index and this driver disagree about what the
# lineage is, and every ratio below belongs to a different build than the gate just checked.
RESOLVED=$(cd "$BENCH/profiling" && python3 -c "
import sys; sys.path.insert(0, '.')
import importlib.util as u
sp = u.spec_from_file_location('c', 'compare.py'); m = u.module_from_spec(sp); sp.loader.exec_module(m)
print((m.resolve_tip('profiling/series/r10-tip-index.tsv', 'MONAD_ZKVM_OFFICIAL_PROFILE=ON')[0] or '').split('/')[-1].replace('.elf', ''))
" 2>/dev/null)
[ "$RESOLVED" = "$TIP" ] || { echo "TIP MISMATCH: driver says $TIP, compare.py resolves $RESOLVED"; exit 1; }
echo "--- axis follows the index, resolved $RESOLVED"

(cd "$BENCH/profiling" && \
  python3 compare.py --axis r10tip-vs-ziskethone --axis r9-vs-ziskethone --axis r8-vs-ziskethone \
    --block-min 25815000 --block-max 25815199 --families 12 --html results/compare.html) \
  || { echo "COMPARE FAILED — historical builds not started"; exit 1; }

# Now build the complete lineage. Incremental mode reuses every valid old row;
# full mode rebuilds history. Both consume the already gated tip from the seed.
MONAD="$MONAD_TREE" \
BRANCH="$TARGET_COMMIT" \
BUILDFIX=142989e81 \
REUSE_BUILDS="$SKIP_BUILD" \
REUSE_INDEX="$HERE/r10-index.tsv" \
SEED_INDEX="$HERE/r10-tip-index.tsv" \
INDEX="$HERE/r10-index.tsv" \
BUILDENV="$HERE/r10-buildenv.tsv" \
  "$HERE/series-build-lineage.sh" || { echo "LINEAGE BUILD STAGE FAILED"; exit 1; }
INDEX_ROWS=$(wc -l < "$HERE/r10-index.tsv" | tr -d ' ')
INDEX_OK=$(awk -F'\t' '$3=="OK"{n++} END{print n+0}' "$HERE/r10-index.tsv")
SERIES_TIP_COMMIT=$(awk -F'\t' 'NF{c=$2} END{print c}' "$HERE/r10-index.tsv")
SERIES_TIP=$(awk -F'\t' 'NF{h=$4} END{print h}' "$HERE/r10-index.tsv")
[ "$SERIES_TIP_COMMIT" = "$TIP_COMMIT" ] && [ "$SERIES_TIP" = "$TIP" ] || {
  echo "TIP MISMATCH after lineage: early=$TIP_COMMIT/$TIP series=$SERIES_TIP_COMMIT/$SERIES_TIP" >&2
  exit 1
}
echo "--- index: $INDEX_ROWS commits ($INDEX_OK OK), tip matches compare"

# Compare runs first and populates the shared content-addressed cache. Series
# imports compatible entries from it, then measures only lineage commits that
# compare has never seen.
if [ -f "$HERE/r10-measure.tsv" ]; then
  MEASURE_BEFORE=$(wc -l < "$HERE/r10-measure.tsv" | tr -d ' ')
else
  MEASURE_BEFORE=0
fi
BLOCKS_FILE="$SAMPLE_DIR/selected" INDEX="$HERE/r10-index.tsv" OUT="$HERE/r10-measure.tsv" \
  "$HERE/series-measure.sh" "$NB_BLOCK-block" 6 \
  || { echo "MEASUREMENT FAILED — series report not generated" >&2; exit 1; }
MEASURE_AFTER=$(wc -l < "$HERE/r10-measure.tsv" | tr -d ' ')
echo "--- measurement cache: $MEASURE_AFTER total rows ($((MEASURE_AFTER - MEASURE_BEFORE)) added this run)"

# --base is the BASE series-build-lineage.sh actually walked (its default), not a branch name:
# the page used to print `origin/sam/zkvm-zisk-sp1` unconditionally, and that ref has since moved.
(cd "$BENCH/profiling" && \
  python3 series/report.py --index r10-index.tsv --measure r10-measure.tsv \
    --branch al/zkvm-r10 --base 3d237fe69 --blocks-file "$SAMPLE_DIR/selected" \
    --out results/series-r10.html --no-sp1) \
  || { echo "SERIES REPORT FAILED" >&2; exit 1; }
echo "=== done $(date)"
