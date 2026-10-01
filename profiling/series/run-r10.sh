#!/bin/bash
# run-r10.sh — build/gate/compare the r10 tip first, then extend and render the full series.
# Lives in the repo and NOT in /tmp: the previous driver was written to /tmp and macOS purged it
# between the launch and the wake-up, so the overnight run never started and nothing said so.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BENCH="$(cd "$HERE/../.." && pwd)"

usage() {
  cat <<'EOF'
usage: ./profiling/series/run-r10.sh [--lineage NAME] [--branch REF] [--base REF]
                                     [--nb-block N] [--skip-build] [--skip-gate] [--fetch]
                                     [--build-jobs N] [--measure-jobs N] [--no-incremental]
                                     [--compare-only | --series-only | --last [N]] [--check]

  --lineage NAME  which lineage to drive (default: r10). One file per lineage in
                  lineages/NAME.conf: its branch, base, build fix, corpus and axis.
  --nb-block N    representative nested sample of N canonical blocks (default: 200)
  --skip-build    reuse valid existing ELF files; build only missing/new commits
  --branch REF    walk to this ref instead of the lineage's declared top. A stack that is being
                  restacked changes top often; this avoids editing the config for one run.
  --base REF      start the walk here. It is the series' DENOMINATOR: every ratio is measured
                  against this commit's build, and it must be an ancestor of the branch.
                  `subject:<text>` names it by subject, which a rebase preserves.
  --quick         measure steps only, on ZisK's cheap pass rather than the instrumented one that
                  also yields COST. About a third of the time (measured: 4 s vs 11 s over 12
                  blocks). The rows carry NA and a later full run fills exactly them in.
  --build-jobs N  build the lineage in N worktrees at once. Commits are independent builds, so the
                  walk parallelises; a worktree holds one commit at a time, so N worktrees is what
                  N-way means. Worker 1 is the series tree; workers 2..N are created beside it the
                  first time and kept, each ~8 GB, because provisioning one costs minutes.
  --no-incremental  run the guest build through `cargo-zisk build` rather than the cargo command it
                  wraps. cargo-zisk writes its linker script to a fresh mktemp path on every call
                  and that path is part of cargo's unit hash, so nothing is ever reused: measured,
                  an unchanged tree recompiles 112 crates. Incremental is the default and produces
                  a byte-identical ELF; this is the way back if a ZisK release moves.
  --measure-jobs N  emulators to run at once, for the series and for compare's COST pass (default
                  12); compare's timed pass keeps its own, smaller count. Measured here: 6 -> 1.00x, 12 -> 1.56x,
                  16 -> 1.79x, at ~0.4 GiB an emulator. Lower it while an SP1 campaign is running.
  --skip-gate     do not run the standalone root gate over the tip (~30 s). series-measure.sh
                  compares every measured block's public output against the same corpus reference
                  before it records a row, for every ELF of the walk, and a mismatch fails the run
                  -- so at --nb-block 200 the gate re-checks what the measurement already checks.
                  What it alone gives up: the per-block TSV that LOCATES a disagreement, and,
                  below 200 blocks, the roots of the blocks the sample leaves out.
  --fetch         update the worktree's remote-tracking refs first. A stack that grows between
                  runs otherwise fails preflight with "unknown ... (fetch it first)".
  --compare-only  build and gate the tip, run compare, stop. No lineage, no series.
  --series-only   build the lineage and render the series, skipping compare.
  --last [N]      what the last N commits bring (default 1). Builds and measures only the tip, the
                  N-1 commits below it, and the commit under those, which is the reference their
                  steps are measured from; prints each commit's step. Everything the lineage has
                  already built or measured is reused, and what this measures is not measured again
                  by the next full walk. Writes <lineage>-last-index.tsv and a -last page, so the
                  lineage's own index and page stay as they were; rows keep the lineage's numbers.
                  Implies --series-only and --skip-build.
  --check         check every prerequisite and exit without building or measuring

Both stop-early modes still build AND gate the tip: the series seeds from it, and the gate is
what stands between a broken guest and a published number. Neither is a way to skip it.

This does not narrow the soundness gate or compare.py: both keep their full corpus.
Fresh-clone setup: profiling/series/RUN-R10.md
EOF
}

LINEAGE=r10
NB_BLOCK=200
SKIP_BUILD=0
COMPARE_ONLY=0
SERIES_ONLY=0
DO_FETCH=0
BRANCH_EXPLICIT=0
SKIP_GATE=0
QUICK=0
CHECK_ONLY=0
LAST_SET=0
LAST_N=0
BASE_EXPLICIT=0
LINEAGE_OFFSET=0
LINEAGE_ANCHOR_RANGE=""
BUILD_JOBS="${LINEAGE_BUILD_JOBS:-1}"
MEASURE_JOBS="${LINEAGE_MEASURE_JOBS:-12}"
ZISK_INCREMENTAL="${ZISK_INCREMENTAL:-1}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --nb-block)
      [ -n "${2:-}" ] || { echo "--nb-block needs a value" >&2; exit 2; }
      NB_BLOCK="$2"; shift 2 ;;
    --nb-block=*) NB_BLOCK="${1#*=}"; shift ;;
    --lineage)
      [ -n "${2:-}" ] || { echo "--lineage needs a name" >&2; exit 2; }
      LINEAGE="$2"; shift 2 ;;
    --lineage=*) LINEAGE="${1#*=}"; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --branch)
      [ -n "${2:-}" ] || { echo "--branch needs a ref" >&2; exit 2; }
      LINEAGE_BRANCH="$2"; BRANCH_EXPLICIT=1; shift 2 ;;
    --branch=*) LINEAGE_BRANCH="${1#*=}"; BRANCH_EXPLICIT=1; shift ;;
    --base)
      [ -n "${2:-}" ] || { echo "--base needs a commit" >&2; exit 2; }
      LINEAGE_BASE="$2"; BASE_EXPLICIT=1; shift 2 ;;
    --base=*) LINEAGE_BASE="${1#*=}"; BASE_EXPLICIT=1; shift ;;
    --last)
      # The count is optional: `--last` alone is the tip's own commit.
      LAST_SET=1
      case "${2:-}" in
        ''|-*|*[!0-9]*) LAST_N=1; shift ;;
        *) LAST_N="$2"; shift 2 ;;
      esac ;;
    --last=*) LAST_SET=1; LAST_N="${1#*=}"; shift ;;
    --quick) QUICK=1; shift ;;
    --build-jobs)
      [ -n "${2:-}" ] || { echo "--build-jobs needs a number" >&2; exit 2; }
      BUILD_JOBS="$2"; shift 2 ;;
    --build-jobs=*) BUILD_JOBS="${1#*=}"; shift ;;
    --measure-jobs)
      [ -n "${2:-}" ] || { echo "--measure-jobs needs a number" >&2; exit 2; }
      MEASURE_JOBS="$2"; shift 2 ;;
    --measure-jobs=*) MEASURE_JOBS="${1#*=}"; shift ;;
    --no-incremental) ZISK_INCREMENTAL=0; shift ;;
    --skip-gate) SKIP_GATE=1; shift ;;
    --fetch) DO_FETCH=1; shift ;;
    --compare-only) COMPARE_ONLY=1; shift ;;
    --series-only) SERIES_ONLY=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$NB_BLOCK" in
  ''|*[!0-9]*) echo "--nb-block must be a positive integer" >&2; exit 2 ;;
esac
case "$BUILD_JOBS" in
  ''|*[!0-9]*|0) echo "--build-jobs must be a positive integer" >&2; exit 2 ;;
esac
case "$MEASURE_JOBS" in
  ''|*[!0-9]*|0) echo "--measure-jobs must be a positive integer" >&2; exit 2 ;;
esac
[ "$NB_BLOCK" -gt 0 ] || { echo "--nb-block must be greater than zero" >&2; exit 2; }
[ "$COMPARE_ONLY" = 0 ] || [ "$SERIES_ONLY" = 0 ] || {
  echo "--compare-only and --series-only exclude each other" >&2; exit 2; }
[ "$NB_BLOCK" -le 200 ] || { echo "--nb-block $NB_BLOCK exceeds the canonical 200-block corpus" >&2; exit 2; }
if [ "$LAST_SET" = 1 ]; then
  case "$LAST_N" in
    ''|*[!0-9]*|0) echo "--last must be a positive integer" >&2; exit 2 ;;
  esac
  [ "$COMPARE_ONLY" = 0 ] || { echo "--last and --compare-only exclude each other" >&2; exit 2; }
  [ "$BASE_EXPLICIT" = 0 ] || { echo "--last sets where the walk starts; drop --base" >&2; exit 2; }
  SERIES_ONLY=1
  SKIP_BUILD=1
fi

# ── Which lineage ────────────────────────────────────────────────────────────────────────────────
# Everything r10-specific lives in lineages/<name>.conf: the branch, where it starts, whether it
# needs a build-compatibility cherry-pick, its compare axis and its two output paths. A second
# lineage is a second config file, NOT a second copy of this driver -- two 300-line drivers
# differing on five values is the drift the lineage builder was consolidated to avoid.
#
# The FILE NAMES of a lineage's tables are derived from its name and not configurable: an index
# that does not say which lineage it belongs to is how two runtimes ended up in one table.
LINEAGE_CONF="$HERE/lineages/$LINEAGE.conf"
[ -f "$LINEAGE_CONF" ] || {
  echo "no lineage config at $LINEAGE_CONF" >&2
  echo "known lineages: $(ls "$HERE/lineages" 2>/dev/null | sed 's/\.conf$//' | tr '\n' ' ')" >&2
  exit 2
}
# Sourced AFTER the arguments are parsed, and every value in it is `${VAR:-default}`: so --branch
# and --base (and a plain environment variable) win over the file, and the file wins over nothing.
# shellcheck disable=SC1090
. "$LINEAGE_CONF"
. "$HERE/subject-ref.sh"
for _v in LINEAGE_BRANCH LINEAGE_BASE LINEAGE_AXIS LINEAGE_SERIES_OUT LINEAGE_COMPARE_OUT; do
  [ -n "${!_v:-}" ] || { echo "$LINEAGE_CONF does not set $_v" >&2; exit 2; }
done
LINEAGE_BUILDFIX="${LINEAGE_BUILDFIX:-}"          # empty is a valid answer: no fix needed
# The lineage-wide statistics page sits beside the series page unless the conf names one.
LINEAGE_META_OUT="${LINEAGE_META_OUT:-${LINEAGE_SERIES_OUT%.html}-meta.html}"
IDX="$HERE/$LINEAGE-index.tsv"
TIP_IDX="$HERE/$LINEAGE-tip-index.tsv"
MEASURE="$HERE/$LINEAGE-measure.tsv"
BUILDENV="$HERE/$LINEAGE-buildenv.tsv"
# --last writes a table of its own, named after the lineage like the others, and reads the
# lineage's as a seed: the lineage index is never replaced by a window of itself.
MAIN_IDX="$IDX"
if [ "$LAST_N" -gt 0 ]; then
  IDX="$HERE/$LINEAGE-last-index.tsv"
  LINEAGE_SERIES_OUT="${LINEAGE_SERIES_OUT%.html}-last.html"
fi
[ -f "$BUILDENV" ] || { echo "no build-env sidecar at $BUILDENV" >&2; exit 2; }

# These defaults are portable and individually overridable. The build recipe expands
# `{toolchain}` to SERIES_TOOLCHAIN_DIR; it never records one developer's home directory.
# The witness generation is a LINEAGE input, not a constant: a guest reads the wire format its
# own branch writes. The r10-zisk stack encodes the digest tag as 0xa0 rather than 0x04 -- one
# byte value, 83,231 times in a 6.5 MB witness, same content, same post-state root -- so it
# cannot read r10's corpus and r10 cannot read its.
MEASURE_GEN="$BENCH/guests/monad/gen/$LINEAGE_GEN/witnesses"
MONAD_TREE="${SERIES_MONAD:-$(cd "$BENCH/.." && pwd)/monad-series}"
SERIES_TOOLCHAIN_DIR="${SERIES_TOOLCHAIN_DIR:-${RISCV_TOOLCHAIN_DIR:-$HOME/.local/xPacks/zisk-dma-gcc-15.2.0}}"
SERIES_STOCK_TOOLCHAIN_DIR="${SERIES_STOCK_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"

# ── The ZisK release, pinned here and forced into every stage ────────────────────────────────────
# It is TWO inputs at once, and both have to move together or the run is incoherent:
#   BUILD       its Rust toolchain compiles the guest and its std, and its cargo-zisk writes the
#               linker script; build.sh takes both from ZISK_DIR. Measured at commit 08735d46e: the
#               1.2 SDK build runs +2.89 % steps and +1.66 % COST against the 1.1 SDK build of the
#               same source.
#   MEASUREMENT ziskemu's cost model. 1.2.0-alpha prices a keccak permutation at 25x1538 where
#               1.1.0-alpha charged 25x3022 -- every other category is unchanged to the byte -- so
#               the same ELF reports ~20 % less COST under it.
# Pinned rather than "whatever is on PATH" for the reason the compiler is: a runtime A/B must not be
# indistinguishable from a source change. Override BOTH together to move the pin.
SERIES_ZISK_DIR="${SERIES_ZISK_DIR:-$HOME/.zisk-1.3}"
SERIES_ZISK_VERSION="${SERIES_ZISK_VERSION:-1.3.1-alpha}"
# The ziskethone the compare measures against is built for a release too: the commit zisk-eth-client
# pins for named ZisK releases, built by its driver. Its record lists them, and the preflight refuses
# a pin outside them.
ZEG_RECORD="$BENCH/guests/zec-ziskethone/zec-ziskethone.build.json"
# One measurement cache per release, for every stage: profiling/cache.py keys on the ELF and not on the
# emulator, so series-measure and compare.py must read and publish into the root of the pin -- the
# shared default root holds rows priced by earlier releases.
export COMPARE_CACHE_ROOT="${COMPARE_CACHE_ROOT:-$BENCH/profiling/cache-zisk-$SERIES_ZISK_VERSION}"
# The names the stages read: build.sh takes ZISK_DIR, gate-roots*.sh and series-measure.sh take EMU.
ZISK_DIR="$SERIES_ZISK_DIR"
EMU="$SERIES_ZISK_DIR/bin/ziskemu"
export SERIES_TOOLCHAIN_DIR SERIES_STOCK_TOOLCHAIN_DIR SERIES_ZISK_DIR SERIES_ZISK_VERSION ZISK_DIR EMU
export ZISK_INCREMENTAL

preflight_error() { echo "preflight: ERROR: $*" >&2; PREFLIGHT_ERRORS=$((PREFLIGHT_ERRORS+1)); }

# A table carries the runtime it was produced under, in a stamp beside it. Refusing a mismatch is
# the whole point: the ELF sha changes with the SDK, so a bumped run APPENDS rather than colliding,
# and the table would silently hold two cost models -- a lineage whose COST column changes meaning
# halfway down, with nothing on the page to say where. Delete the stamp and the table together to
# start a runtime over; there is no in-place migration, because none of the old rows are valid.
runtime_stamp() {
  local f="$1" stamp="$1.runtime" had
  [ -s "$f" ] || { printf '%s\n' "$SERIES_ZISK_VERSION" > "$stamp"; return 0; }
  if [ ! -f "$stamp" ]; then
    preflight_error "$(basename "$f") has rows but no .runtime stamp — it predates the pin. If it is \
$SERIES_ZISK_VERSION data, run: echo $SERIES_ZISK_VERSION > $stamp"
    return 0
  fi
  had=$(cat "$stamp")
  [ "$had" = "$SERIES_ZISK_VERSION" ] || preflight_error \
    "$(basename "$f") was measured under ZisK $had, the pin is $SERIES_ZISK_VERSION — \
appending would mix two cost models in one table. Move it aside, or set SERIES_ZISK_VERSION=$had."
}
preflight() {
  PREFLIGHT_ERRORS=0
  # Opt-in, and only ever `fetch`: it moves refs/remotes and nothing else -- no working tree, no
  # local branch, no index. Not automatic, because a run that silently refreshed its own target
  # could measure a different tip than the one you last looked at.
  if [ "$DO_FETCH" = 1 ] && [ -e "$MONAD_TREE/.git" ]; then
    echo "--- fetching origin in $MONAD_TREE"
    git -C "$MONAD_TREE" fetch --quiet origin || preflight_error "git fetch origin failed"
  fi
  command -v python3 >/dev/null 2>&1 || preflight_error "python3 is not installed"
  # Both binaries of the SAME install, both asserted against the SAME pin: measuring with one
  # release and linking against another is the incoherent run this is here to refuse.
  for _b in ziskemu cargo-zisk; do
    if [ ! -x "$SERIES_ZISK_DIR/bin/$_b" ]; then
      preflight_error "no $SERIES_ZISK_DIR/bin/$_b (install ZisK $SERIES_ZISK_VERSION with ziskup)"
    else
      case $("$SERIES_ZISK_DIR/bin/$_b" --version 2>&1) in
        *"$SERIES_ZISK_VERSION"*) ;;
        *) preflight_error "$_b at $SERIES_ZISK_DIR is not the pinned $SERIES_ZISK_VERSION \
($("$SERIES_ZISK_DIR/bin/$_b" --version 2>&1 | head -1))" ;;
      esac
    fi
  done
  runtime_stamp "$IDX"
  runtime_stamp "$MEASURE"
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
  # Pinned by content and by release: an ELF other than the recorded one, or one built for another
  # ZisK, would compare the pinned guest against something else under the same label.
  zeg_check=$(python3 - "$ZEG_RECORD" "$BENCH" "$SERIES_ZISK_VERSION" <<'PY'
import hashlib, json, os, sys
record, bench, pin = sys.argv[1:4]
try:
    rec = json.load(open(record))
except (OSError, ValueError) as e:
    sys.exit(print(f"cannot read the ziskethone record {record}: {e}"))
elf = os.path.join(bench, rec.get("elf") or "")
if not os.path.isfile(elf):
    sys.exit(print(f"missing ziskethone reference ELF {rec.get('elf')}"))
if hashlib.sha256(open(elf, "rb").read()).hexdigest() != rec.get("elf_sha256"):
    sys.exit(print(f"{rec.get('elf')} is not the ELF its record names (sha256 differs)"))
if pin not in (rec.get("runtimes") or []):
    print(f"the ziskethone reference is built for ZisK {', '.join(rec.get('runtimes') or ['?'])}, "
          f"the pin is {pin}")
PY
)
  [ -z "$zeg_check" ] || preflight_error "$zeg_check"

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
      [ "$monad_digest" = "$LINEAGE_GEN_DIGEST" ] || \
        preflight_error "Monad corpus digest differs from $LINEAGE_GEN (got ${monad_digest:0:16}…, \
expected ${LINEAGE_GEN_DIGEST:0:16}…)"
    fi

    # compare.py resolves its Monad-side inputs through the `fixtures` symlink, which `use-gen`
    # maintains and which is GLOBAL. Two lineages reading two wire formats cannot both be measured
    # while it points one way, and a run that flipped it silently would corrupt whatever another
    # session was measuring. So: assert, name the command, and refuse -- never flip it here.
    if [ ! -e "$BENCH/guests/monad/fixtures" ]; then
      preflight_error "guests/monad/fixtures is absent (run guests/monad/use-gen $LINEAGE_GEN)"
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
    TARGET_COMMIT=$(git -C "$MONAD_TREE" rev-parse --verify "$LINEAGE_BRANCH" 2>/dev/null) || \
      preflight_error "$LINEAGE_BRANCH is unknown in $MONAD_TREE (fetch it first)"
    # The base may be given as `subject:<text>`, resolved inside the stack's own commits -- the
    # fork point from main is the widest range that is still the stack. A base named by sha is a
    # sha to fix after every restack; a base named by subject survives one.
    if [ -n "${TARGET_COMMIT:-}" ]; then
      _fork=$(git -C "$MONAD_TREE" merge-base origin/main "$TARGET_COMMIT" 2>/dev/null)
      _rb=$( cd "$MONAD_TREE" && resolve_ref "${_fork:-$LINEAGE_BASE}..$TARGET_COMMIT" "$LINEAGE_BASE" 2>&1 ) \
        && LINEAGE_BASE="$_rb" \
        || preflight_error "lineage base does not resolve: $_rb"
    fi
    # The official profile signs the ZisK release its source declares into the ELF. A tip declaring
    # another release than the pin still builds -- the toolchain is the pin's -- and would sign a
    # runtime it was not built with.
    if [ -n "${TARGET_COMMIT:-}" ]; then
      _rt=$(git -C "$MONAD_TREE" show "$TARGET_COMMIT:zkvm/guest/CMakeLists.txt" 2>/dev/null \
            | sed -n 's/.*set(_monad_zkvm_runtime_version "\([^"]*\)").*/\1/p' | head -1)
      [ -z "$_rt" ] || [ "$_rt" = "$SERIES_ZISK_VERSION" ] || preflight_error \
        "$LINEAGE_BRANCH declares ZisK $_rt in its official profile (zkvm/guest/CMakeLists.txt), the pin is $SERIES_ZISK_VERSION"
    fi
    git -C "$MONAD_TREE" cat-file -e "$LINEAGE_BASE^{commit}" 2>/dev/null || \
      preflight_error "lineage base $LINEAGE_BASE is absent from $MONAD_TREE"
    git -C "$MONAD_TREE" merge-base --is-ancestor "$LINEAGE_BASE" "${TARGET_COMMIT:-HEAD}" 2>/dev/null || \
      preflight_error "lineage base ${LINEAGE_BASE:0:9} is NOT an ancestor of $LINEAGE_BRANCH — \
the walk would be every commit the branch reaches without passing through it, which is not a lineage." 
    # Only when the lineage declares one. A stack rooted at a recent main needs no fix, and
    # demanding a commit it will never cherry-pick would fail a run that is perfectly buildable.
    [ -z "$LINEAGE_BUILDFIX" ] || git -C "$MONAD_TREE" cat-file -e "$LINEAGE_BUILDFIX^{commit}" 2>/dev/null || \
      preflight_error "build compatibility commit $LINEAGE_BUILDFIX is absent from $MONAD_TREE"
    # A rebase may remove the commit used as an @after anchor. Without this
    # check the tip silently falls back to the stock recipe, spends minutes
    # building and gating, then compare.py rejects it for lacking the official
    # profile. Resolve the sidecar exactly as the lineage builder does.
    if [ -n "${TARGET_COMMIT:-}" ]; then
      tip_short=$(git -C "$MONAD_TREE" log -1 --format='%h' "$TARGET_COMMIT")
      tip_recipe=$(grep -m1 "^$tip_short	" "$BUILDENV" || true)
      if [ -z "$tip_recipe" ]; then
        while IFS=$'\t' read -r -a recipe_fields; do
          case "${recipe_fields[0]:-}" in
            @after|@from) _pincl=0; [ "${recipe_fields[0]}" = '@after' ] || _pincl=1
                          _pref="${recipe_fields[1]:-}" ;;
            @after:subject|@from:subject)
                          _pincl=0; [ "${recipe_fields[0]}" = '@after:subject' ] || _pincl=1
                          _pref="subject:${recipe_fields[1]:-}" ;;
            *) continue ;;
          esac
          [ -n "${recipe_fields[1]:-}" ] || continue
          anchor_full=$( cd "$MONAD_TREE" && resolve_ref "$LINEAGE_BASE..$TARGET_COMMIT" "$_pref" 2>/dev/null ) || continue
          if [ "$TARGET_COMMIT" = "$anchor_full" ]; then
            [ "$_pincl" = 1 ] || continue
          else
            git -C "$MONAD_TREE" merge-base --is-ancestor "$anchor_full" "$TARGET_COMMIT" 2>/dev/null || continue
          fi
          tip_recipe="${recipe_fields[*]:2}"
        done < "$BUILDENV"
      fi
      case "$tip_recipe" in
        *MONAD_ZKVM_OFFICIAL_PROFILE=ON*) ;;
        *) preflight_error "$(basename "$BUILDENV") does not select MONAD_ZKVM_OFFICIAL_PROFILE=ON for tip ${TARGET_COMMIT:0:9} (an @after anchor may have been rebased away)" ;;
      esac
    fi
    # A STACK GROWS. Declaring its top by hand means the day a branch is pushed on top and this
    # config is not moved with it, the run measures the old top and reports success -- a plausible
    # number for the wrong tree, which is worse than an error. So: list the stack's refs and refuse
    # if any of them is strictly ahead of the declared top. Lineages that are a single branch leave
    # LINEAGE_STACK_REFS empty and skip this entirely.
    if [ -n "${LINEAGE_STACK_REFS:-}" ] && [ -n "${TARGET_COMMIT:-}" ]; then
      while read -r _ref; do
        [ -n "$_ref" ] || continue
        _rc=$(git -C "$MONAD_TREE" rev-parse --verify "$_ref" 2>/dev/null) || continue
        [ "$_rc" != "$TARGET_COMMIT" ] || continue
        # strictly ahead: contains the declared top, and is not contained by it
        git -C "$MONAD_TREE" merge-base --is-ancestor "$TARGET_COMMIT" "$_rc" 2>/dev/null || continue
        # A top given on the command line is a DECISION -- measuring one étage of a stack on
        # purpose is a normal thing to want. Only the config's default can be stale by oversight,
        # which is what this guard exists for: refuse the default, inform the explicit.
        if [ "$BRANCH_EXPLICIT" = 1 ]; then
          echo "note: $_ref is above the requested $LINEAGE_BRANCH — measuring the branch you named" >&2
        else
          preflight_error "$_ref is ahead of the declared top $LINEAGE_BRANCH — the stack has grown. \
Set LINEAGE_BRANCH=$_ref in $LINEAGE_CONF, or delete the branch if it is not part of the stack."
        fi
      done <<EOF
$(git -C "$MONAD_TREE" for-each-ref --format='%(refname:short)' "refs/remotes/$LINEAGE_STACK_REFS" 2>/dev/null)
EOF
    fi
    # TRACKED changes only. This guard exists because the builders drive the tree with
    # `git checkout -f`, which discards modifications to tracked files without asking -- that is
    # the work it must not destroy. Untracked files survive a checkout, so they are not at risk,
    # and blocking on them refuses a perfectly safe run: walking a lineage across commits whose
    # submodule set differs leaves an initialised submodule directory behind as untracked, every
    # time. Reported, because a build can still pick up a stale header from one.
    dirty=$(git -C "$MONAD_TREE" status --porcelain --untracked-files=no 2>/dev/null | head -1)
    [ -z "$dirty" ] || preflight_error "$MONAD_TREE has uncommitted changes to tracked files: $dirty"
    untracked=$(git -C "$MONAD_TREE" status --porcelain --untracked-files=normal 2>/dev/null \
                | grep -c '^??' || true)
    [ "$untracked" = 0 ] || echo "note: $MONAD_TREE holds $untracked untracked path(s) — a checkout \
leaves them in place; harmless unless a build reads one" >&2
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

# The window for --last: the tip and the N-1 commits below it, plus the commit under them as the
# reference. The reference must be one of the lineage's own commits, so every row written here is a
# row the full walk writes too, under the same number. The build recipe's anchors are resolved over
# the LINEAGE, not the window -- they sit near the lineage base, outside any short window, and an
# anchor that does not resolve is a hard failure.
if [ "$LAST_N" -gt 0 ]; then
  LAST_REF=$(git -C "$MONAD_TREE" rev-parse -q --verify "$TARGET_COMMIT~$LAST_N^{commit}") || {
    echo "--last $LAST_N: $LINEAGE_BRANCH has no commit $LAST_N below its tip" >&2; exit 2; }
  LAST_AVAIL=$(git -C "$MONAD_TREE" rev-list --count "$LINEAGE_BASE..$TARGET_COMMIT")
  if [ "$LAST_REF" = "$(git -C "$MONAD_TREE" rev-parse "$LINEAGE_BASE^{commit}")" ] || \
     ! git -C "$MONAD_TREE" merge-base --is-ancestor "$LINEAGE_BASE" "$LAST_REF"; then
    echo "--last $LAST_N reaches the lineage base: the lineage has $LAST_AVAIL commits, so at most --last $((LAST_AVAIL - 1))" >&2
    exit 2
  fi
  LAST_WALK=$(git -C "$MONAD_TREE" rev-list --count "$LAST_REF^..$TARGET_COMMIT")
  [ "$LAST_WALK" = "$((LAST_N + 1))" ] || {
    echo "--last $LAST_N: the window holds $LAST_WALK commits, not $((LAST_N + 1)) -- the tip's history is not linear" >&2
    exit 2; }
  LINEAGE_ANCHOR_RANGE="$LINEAGE_BASE^..$TARGET_COMMIT"
  LINEAGE_OFFSET=$((LAST_AVAIL - LAST_N - 1))
  LINEAGE_BASE=$(git -C "$MONAD_TREE" rev-parse "$LAST_REF^")
  echo "--- last $LAST_N: reference #$((LINEAGE_OFFSET + 1)) $(git -C "$MONAD_TREE" log -1 --format='%h %s' "$LAST_REF")"
  _k=$((LINEAGE_OFFSET + 1))
  for _c in $(git -C "$MONAD_TREE" rev-list --reverse "$LAST_REF..$TARGET_COMMIT"); do
    _k=$((_k + 1)); echo "    #$_k $(git -C "$MONAD_TREE" log -1 --format='%h %s' "$_c")"
  done
fi
[ "$CHECK_ONLY" = 0 ] || exit 0

# One run per table. The tree lock covers a build and nothing after it, so a second run of the same
# table could claim the tree while the first is measuring, then replace the index the first is about
# to render. --compare-only never writes the index and takes no lock.
RUN_LOCK=""
if [ "$COMPARE_ONLY" = 0 ]; then
  RUN_LOCK="$HERE/elf/.$(basename "$IDX").run.lock"
  _lock_pid=$(cut -d' ' -f1 "$RUN_LOCK" 2>/dev/null || true)
  if [ -n "$_lock_pid" ] && kill -0 "$_lock_pid" 2>/dev/null; then
    echo "another run is writing $(basename "$IDX") (pid $_lock_pid, since $(cut -d' ' -f2- "$RUN_LOCK")) -- wait for it" >&2
    exit 3
  fi
  printf '%s %s\n' "$$" "$(date '+%Y-%m-%d %H:%M:%S')" > "$RUN_LOCK"
fi

# The canonical corpus is the same 200-block range compare.py measures. Smaller
# samples are stable hash-ranked prefixes, independent of block order; growing
# N therefore improves coverage while reusing every earlier row.
SAMPLE_DIR=$(mktemp -d); trap 'rm -rf "$SAMPLE_DIR"; [ -z "$RUN_LOCK" ] || rm -f "$RUN_LOCK"' EXIT
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
[ "$LAST_N" -eq 0 ] || mode="$mode — last $LAST_N commit(s)"
echo "=== start $(date) — series sample $NB_BLOCK/$TOTAL blocks$mode"

# Freeze the remote-tracking target once; preflight resolved it before any expensive work.
echo "--- lineage $LINEAGE — frozen target $LINEAGE_BRANCH at ${TARGET_COMMIT:0:9}"

# Computed outside the environment-assignment chains below, where a comment or an `if` between two
# assignments would end the command and hand the builder none of it.
GATE_GEN_TIP="$MEASURE_GEN"
if [ "$SKIP_GATE" = 1 ]; then
  GATE_GEN_TIP=""
  echo "--- gate skipped (--skip-gate); every measured block's public output is still checked"
fi

# Build (or reuse in incremental mode) only the remote-tracking tip first. This
# one-row index lets gate + compare run before the historical lineage build.
# The full pass below consumes it as a trusted seed and does not build the tip twice.
#
# --series-only walks IN ORDER instead. Building the tip first exists to fail fast before a long
# lineage, which is the right trade when a compare follows; with no compare it inverts, because a
# tip that does not gate then blocks every earlier commit from ever being built -- and the earlier
# commits are exactly what tells you WHERE the tip broke. Measured on the r10-zisk keccak family:
# the tip gated 0/200, the run stopped, and the 28 commits that would have located the regression
# were never built. So in this mode the lineage runs first and the gate follows it.
if [ "$SERIES_ONLY" = 1 ]; then
  echo "--- series-only: walking the lineage in order; the tip is gated after it, not before"
else
MONAD="$MONAD_TREE" \
BRANCH="$TARGET_COMMIT" \
BASE="$LINEAGE_BASE" \
BUILDFIX="$LINEAGE_BUILDFIX" \
REUSE_BUILDS="$SKIP_BUILD" \
REUSE_INDEX="$([ -s "$IDX" ] && echo "$IDX" || echo "$TIP_IDX")" \
ONLY_TIP=1 \
INDEX="$TIP_IDX" \
BUILDENV="$BUILDENV" \
GATE_GEN="$GATE_GEN_TIP" \
  "$HERE/series-build-lineage.sh" || { echo "BUILD STAGE FAILED"; exit 1; }
fi

if [ "$SERIES_ONLY" != 1 ]; then
TIP_COMMIT=$(awk -F'\t' 'NF{print $2}' "$TIP_IDX")
TIP=$(awk -F'\t' 'NF{print $4}' "$TIP_IDX")
TIP_SUBJECT=$(awk -F'\t' 'NF{print $5}' "$TIP_IDX")
echo "--- tip: commit $TIP_COMMIT ($TIP_SUBJECT), elf $TIP"
[ -f "$HERE/elf/$TIP.elf" ] || { echo "no elf for tip"; exit 1; }

# ── the profile must be IN THE BINARY, not merely selected ───────────────────────────────────────
# The preflight checks that the sidecar SELECTS MONAD_ZKVM_OFFICIAL_PROFILE=ON. That is a statement
# about a table, not about a build. Measured on the r10-zisk stack: its build-support crate no
# longer reads MONAD_ZKVM_CMAKE_DEFINES, so `cmake` received only -DMONAD_ZKVM_GUEST_TARGET=zisk
# and every ELF was a default build -- no profile, no levers. Twenty-seven commits measured, the
# keccak memo reporting 0 because it was never compiled in, and nothing anywhere said so.
#
# The profile stamps MONAD_ZKVM_BUILD_COMMIT into the guest, so the commit's own sha is the trace:
# per-commit, unambiguous, and present in a working build (checked against r10's ELFs). Absent, the
# options did not reach the compiler however convincing the recipe looked.
if grep -q 'MONAD_ZKVM_OFFICIAL_PROFILE=ON' <<<"$(awk -F'\t' -v c="$TIP_COMMIT" '$2==c{print $6}' "$TIP_IDX")"; then
  TIP_FULL=$(git -C "$MONAD_TREE" rev-parse "$TIP_COMMIT" 2>/dev/null)
  # `grep -a` on the file, not `strings | grep`: strings is an extra dependency whose failure the
  # 2>/dev/null would have hidden, turning "the tool is missing" into "the profile is missing".
  if [ -z "$TIP_FULL" ]; then
    echo "cannot resolve $TIP_COMMIT in $MONAD_TREE — skipping the profile-stamp check" >&2
  elif ! LC_ALL=C grep -aq "$TIP_FULL" "$HERE/elf/$TIP.elf"; then
    echo "PROFILE NOT IN THE BINARY: $TIP.elf carries no trace of $TIP_COMMIT, though the recipe" >&2
    echo "  selects MONAD_ZKVM_OFFICIAL_PROFILE=ON. The build did not receive its -D options —" >&2
    echo "  check that zkvm/build-support still reads MONAD_ZKVM_CMAKE_DEFINES on this lineage." >&2
    echo "  Every ELF built this way is a DEFAULT build: no profile, no levers, and a lever's" >&2
    echo "  commit measures 0 because it was never compiled in." >&2
    exit 1
  fi
  echo "--- profile stamped: $TIP.elf carries ${TIP_COMMIT}"
fi

# The gate is the only thing standing between a broken tip and a published ratio, so its exit
# code is not swallowed into a neutral-looking line. A failure aborts before compare: an HTML
# report detached from its terminal warning would otherwise look publishable.
# gate-roots-record.sh, not gate-roots.sh: this run is unattended, and the plain gate keeps its
# per-block verdicts in a mktemp dir it deletes on exit — all that survives is one line in this
# log. A lever questioned next week cannot be re-checked against that. The TSV names the ELF
# sha, the corpus, and every block's two roots, so a disagreement can be located.
GATE=0
if [ "$SKIP_GATE" = 0 ]; then
  REUSE=1 GEN="$MEASURE_GEN" "$HERE/gate-roots-record.sh" "$HERE/elf/$TIP.elf" \
          "$HERE/$LINEAGE-gate-$TIP.tsv" "$MEASURE_JOBS" || GATE=$?
fi
if [ "$SKIP_GATE" = 1 ]; then
  :
elif [ "$GATE" = 0 ]; then
  echo "--- gate OK"
else
  echo "--- GATE FAILED (rc=$GATE) — compare and historical builds not started" >&2
  exit "$GATE"
fi
fi

# ── compare ──────────────────────────────────────────────────────────────────────────────────────
if [ "$SERIES_ONLY" = 1 ]; then
  echo "--- compare skipped (--series-only); $LINEAGE_COMPARE_OUT keeps whatever a previous run wrote"
else
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
print((m.resolve_tip('profiling/series/' + '$LINEAGE' + '-tip-index.tsv', 'MONAD_ZKVM_OFFICIAL_PROFILE=ON')[0] or '').split('/')[-1].replace('.elf', ''))
" 2>/dev/null)
[ "$RESOLVED" = "$TIP" ] || { echo "TIP MISMATCH: driver says $TIP, compare.py resolves $RESOLVED"; exit 1; }
echo "--- axis follows the index, resolved $RESOLVED"

# compare.py reads and publishes into COMPARE_CACHE_ROOT, the pin's root (set beside the pin).
(cd "$BENCH/profiling" && \
  python3 compare.py --axis "$LINEAGE_AXIS" \
    --emu "$EMU" --cost-jobs "$MEASURE_JOBS" \
    --block-min 25815000 --block-max 25815199 --families 12 --html "$LINEAGE_COMPARE_OUT") \
  || { echo "COMPARE FAILED — historical builds not started"; exit 1; }
fi

if [ "$COMPARE_ONLY" = 1 ]; then
  # Stop BEFORE the lineage. The tip is built, gated and compared; the series tables are untouched,
  # so a later full run resumes rather than restarts.
  echo "--- lineage, measurement and series skipped (--compare-only)"
  echo "=== done $(date)"
  exit 0
fi

# Now build the complete lineage. Incremental mode reuses every valid old row;
# full mode rebuilds history. Both consume the already gated tip from the seed.
#
# The lineage is not gated here, and does not need to be: series-measure.sh compares every measured
# block's public output against the same corpus reference before it records a row, for every ELF of
# the walk and not just the tip, and a mismatch fails the run. A standalone pass over each binary
# would re-check that at ~30 s a binary -- 45 min over a lineage this size -- and add only the
# per-block TSV. It does still cover the blocks a sample below 200 leaves out, so a run that wants
# that can set GATE_GEN for the builder by hand.
#
# One builder or the other, chosen here rather than inside the chain for the same reason: an `if`
# between environment assignments would end the command. The parallel driver delegates back to the
# serial walk whenever the slices would be too thin to be worth a worktree, so --build-jobs 1 and
# an unset flag take exactly the same path.
LINEAGE_BUILDER="$HERE/series-build-lineage.sh"
[ "$BUILD_JOBS" -le 1 ] || LINEAGE_BUILDER="$HERE/series-build-parallel.sh"
# A --last window seeds from the lineage's own index, so a commit the lineage already built is
# not built again, and reuses its own previous windows.
LINEAGE_SEED="$TIP_IDX"
LINEAGE_REUSE="$([ -s "$IDX" ] && echo "$IDX" || echo "$TIP_IDX")"
if [ "$LAST_N" -gt 0 ]; then
  LINEAGE_SEED="$MAIN_IDX"
  LINEAGE_REUSE="$([ -s "$IDX" ] && echo "$IDX" || echo "$MAIN_IDX")"
fi
MONAD="$MONAD_TREE" \
JOBS="$BUILD_JOBS" \
BRANCH="$TARGET_COMMIT" \
BASE="$LINEAGE_BASE" \
BUILDFIX="$LINEAGE_BUILDFIX" \
REUSE_BUILDS="$SKIP_BUILD" \
REUSE_INDEX="$LINEAGE_REUSE" \
SEED_INDEX="$LINEAGE_SEED" \
I_OFFSET="$LINEAGE_OFFSET" \
ANCHOR_RANGE="$LINEAGE_ANCHOR_RANGE" \
INDEX="$IDX" \
BUILDENV="$BUILDENV" \
  "$LINEAGE_BUILDER" || { echo "LINEAGE BUILD STAGE FAILED"; exit 1; }
INDEX_ROWS=$(wc -l < "$IDX" | tr -d ' ')
INDEX_OK=$(awk -F'\t' '$3=="OK"{n++} END{print n+0}' "$IDX")
SERIES_TIP_COMMIT=$(awk -F'\t' 'NF{c=$2} END{print c}' "$IDX")
SERIES_TIP=$(awk -F'\t' 'NF{h=$4} END{print h}' "$IDX")
# Only meaningful when a tip-first stage ran. --series-only has no earlier build to agree with,
# and an `&&`/`||` chain mixing the two cases is the kind of precedence puzzle that reads as a
# check while asserting something else -- so it is an `if`.
if [ "$SERIES_ONLY" != 1 ]; then
  if [ "$SERIES_TIP_COMMIT" != "$TIP_COMMIT" ] || [ "$SERIES_TIP" != "$TIP" ]; then
    echo "TIP MISMATCH after lineage: early=$TIP_COMMIT/$TIP series=$SERIES_TIP_COMMIT/$SERIES_TIP" >&2
    exit 1
  fi
  echo "--- index: $INDEX_ROWS commits ($INDEX_OK OK), tip matches compare"
else
  echo "--- index: $INDEX_ROWS commits ($INDEX_OK OK), tip ${SERIES_TIP_COMMIT:-?}"

fi

# The gate the tip-first path runs before compare. Here it runs after the walk, so a tip that does
# not reproduce the corpus roots is still reported -- and every earlier commit is on disk to bisect
# with. It does not abort: the rows are built and measurable, and refusing to render them would
# discard the evidence that locates the break.
if [ "$SERIES_ONLY" = 1 ] && [ "$SKIP_GATE" = 0 ] && [ -n "${SERIES_TIP:-}" ]; then
  GATE=0; REUSE=1 GEN="$MEASURE_GEN" "$HERE/gate-roots-record.sh" "$HERE/elf/$SERIES_TIP.elf" \
            "$HERE/$LINEAGE-gate-$SERIES_TIP.tsv" "$MEASURE_JOBS" || GATE=$?
  if [ "$GATE" = 0 ]; then
    echo "--- gate OK"
  else
    echo "--- GATE FAILED (rc=$GATE) on the tip — the series below is still written, so the" >&2
    echo "    earlier commits can locate the break; do not publish its ratios." >&2
  fi
fi

# Compare runs first and populates the shared content-addressed cache. Series
# imports compatible entries from it, then measures only lineage commits that
# compare has never seen.
if [ -f "$MEASURE" ]; then
  MEASURE_BEFORE=$(wc -l < "$MEASURE" | tr -d ' ')
else
  MEASURE_BEFORE=0
fi
BLOCKS_FILE="$SAMPLE_DIR/selected" INDEX="$IDX" OUT="$MEASURE" QUICK="$QUICK" \
  "$HERE/series-measure.sh" "$NB_BLOCK-block" "$MEASURE_JOBS" \
  || { echo "MEASUREMENT FAILED — series report not generated" >&2; exit 1; }
MEASURE_AFTER=$(wc -l < "$MEASURE" | tr -d ' ')
echo "--- measurement cache: $MEASURE_AFTER total rows ($((MEASURE_AFTER - MEASURE_BEFORE)) added this run)"

# --base is the BASE series-build-lineage.sh actually walked (its default), not a branch name:
# the page used to print `origin/sam/zkvm-zisk-sp1` unconditionally, and that ref has since moved.
(cd "$BENCH/profiling" && \
  python3 series/report.py --index "$(basename "$IDX")" --measure "$(basename "$MEASURE")" \
    --branch "${LINEAGE_BRANCH#origin/}" --base "$LINEAGE_BASE" --blocks-file "$SAMPLE_DIR/selected" \
    --out "$LINEAGE_SERIES_OUT" --no-sp1 $([ "$LAST_N" -eq 0 ] || echo --print-deltas)) \
  || { echo "SERIES REPORT FAILED" >&2; exit 1; }
# How the lineage's gain is spread across its commits. It needs the whole walk, so a --last window,
# two commits, has none. A failure here leaves the series page above complete, so it does not fail
# the run.
if [ "$LAST_N" -eq 0 ]; then
  (cd "$BENCH/profiling" && \
    python3 series/meta-report.py --index "$(basename "$IDX")" --measure "$(basename "$MEASURE")" \
      --monad "$MONAD_TREE" --lineage "$LINEAGE" --blocks-file "$SAMPLE_DIR/selected" \
      --out "$LINEAGE_META_OUT") \
    || echo "META REPORT FAILED — $LINEAGE_SERIES_OUT is complete; $LINEAGE_META_OUT was not written" >&2
fi
echo "=== done $(date)"
