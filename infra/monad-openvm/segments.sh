#!/usr/bin/env bash
# segments.sh — STEP 0 of the monad-on-OpenVM GPU campaign: measure, on CPU, how many proving
# segments each block splits into, for BOTH arms (baseline vs powdr autoprecompiles).
#
# Why this exists: core proving is linear in segments, so segment count is what turns a block list
# into a GPU budget. Nothing else predicts it — witness size does not (a 7 MB Prague witness is
# mostly Merkle proof, not work), and neither do SP1 cycles. This is also a RESULT in its own right:
# the APC segment ratio across the block-size range, comparable to the published CPU pair on
# 22200003 (63 segments baseline / 43 with autoprecompiles).
#
#   ./segments.sh                                   # the 7-point stratified sample, both arms
#   BLOCKS="25552210" ARMS=vanilla ./segments.sh    # one block, one arm
#   APCS=32 ./segments.sh                           # a different autoprecompile count
#
# Costs CPU minutes, no GPU. Resume-able: a (block, arm, apcs, seg_mem_gib) row already recorded
# as ok is skipped, so re-running only fills the gaps.
#
# TWO INVARIANTS, or the comparison is meaningless:
#   * SEG_MEM_GIB is applied to BOTH arms (it sets the segment count directly), and recorded in
#     every row. Never compare rows that disagree on it.
#   * PROFILE_BLOCK is FIXED across all APC runs: the autoprecompile set is derived from the
#     profiled block, so profiling per-block would compare a different program per row. The driver
#     caches generate/select/setup under ARTIFACTS_DIR keyed on it; --input does not invalidate it.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# ── config (override via env) ────────────────────────────────────────────────
OPENVM_DIR="${OPENVM_DIR:-$REPO/monad/zkvm/openvm}"     # the guest package (holds openvm.toml)
SCRIPT_DIR="${SCRIPT_DIR:-$OPENVM_DIR/script}"          # the host driver crate
FIXTURES="${FIXTURES:-$REPO/guests/monad/fixtures}"     # <block>.witness + <block>.post_state_root
OUT="${OUT:-$HERE/results}"
CSV="${CSV:-$OUT/segments.csv}"
# Baseline arm input, produced by `cargo openvm build`. Where the CLI drops it is not documented
# (`--output-dir` has no stated default), and the driver's own default — `../openvm/release/…`
# relative to script/ — implies <pkg>/openvm/release/. So resolve rather than assume: the two known
# layouts first, then any .vmexe under the package. EXE= overrides all of it.
if [[ -z "${EXE:-}" ]]; then
  for cand in "$OPENVM_DIR/openvm/release/monad-zkvm-openvm.vmexe" \
              "$OPENVM_DIR/release/monad-zkvm-openvm.vmexe"; do
    [[ -f "$cand" ]] && { EXE="$cand"; break; }
  done
  [[ -z "${EXE:-}" ]] && EXE="$(find "$OPENVM_DIR" -name 'monad-zkvm-openvm.vmexe' -not -path '*/target/*' 2>/dev/null | head -1)"
  # Nothing built yet: keep the documented path so the preflight error names something actionable.
  EXE="${EXE:-$OPENVM_DIR/openvm/release/monad-zkvm-openvm.vmexe}"
fi
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$OUT/apc-artifacts}"        # APC stage cache (makes reruns cheap)

# The 7-point stratified sample over the monad-vs-rsp / monad-vs-zisk study set (see README).
BLOCKS="${BLOCKS:-25551992 25552236 25552361 25552210 25552250 25552179 25552051}"
ARMS="${ARMS:-vanilla apc}"
# 16, not the driver's default 32: the published CPU pair on 22200003 (1h48 -> 1h12) used 16, so
# this keeps the GPU numbers comparable to it. Sweep it deliberately, not by accident.
APCS="${APCS:-16}"
PROFILE_BLOCK="${PROFILE_BLOCK:-25552210}"   # p50 of the study set — fixed, see invariants above
SEG_MEM_GIB="${SEG_MEM_GIB:-}"               # empty = OpenVM default (15 GiB)

BIN_BASE="$SCRIPT_DIR/target/release/monad-zkvm-openvm-script"
BIN_APC="$SCRIPT_DIR/target/release/monad-zkvm-openvm-powdr"

# The APC arm compiles the guest ITSELF (cargo build -> build.rs -> cmake, -march=rv32im
# -mabi=ilp32), so the cross toolchain has to be reachable from here — not only when you ran
# `cargo openvm build` for the baseline arm. Both are plain env knobs:
#   RISCV_TOOLCHAIN_DIR — build-support asserts on it; category/core/toolchains/riscv64-elf.cmake
#     then requires <dir>/bin/riscv64-unknown-elf-gcc EXACTLY (symlink other prefixes to it).
#   CMAKE_PREFIX_PATH  — build-support reads it (default /usr) and passes it as a cmake variable,
#     because cmake-rs wipes it from the env; it is how find_package(Boost CONFIG) resolves.
#     Boost's include dir is added with -idirafter, so a Homebrew prefix cannot hijack the
#     cross-toolchain's own headers.
export RISCV_TOOLCHAIN_DIR="${RISCV_TOOLCHAIN_DIR:-$HOME/riscv_gcc_multilib}"
# cc-rs picks its flag DIALECT from the compiler family it thinks it will use — and cmake-rs feeds
# those flags to cmake as CMAKE_C_FLAGS, where they override the toolchain file's *_FLAGS_INIT.
# With no CC_<target> set, a macOS host assumes clang for riscv32im-risc0-zkvm-elf and emits
# `--target=riscv32`, which the cross GCC that riscv64-elf.cmake selects rejects outright
# ("unrecognized command-line option"). Naming the GNU cross compiler makes cc-rs take its Gnu
# branch and emit `-march=rv32im -mabi=ilp32` instead — byte-for-byte what a Linux host produces,
# so this is a portability fix, not a macOS-only hack.
# The APC arm shells out to `cargo metadata` and `cargo +<toolchain> build` to build the guest.
# Both resolve the binary from $CARGO, falling back to a PATH lookup — and a PATH lookup that works in
# your interactive shell does not always work in the driver's environment (the failure is an opaque
# `cargo metadata command failed: Io(NotFound)` panic). Pin it.
if [[ -z "${CARGO:-}" ]]; then
  CARGO="$(command -v cargo 2>/dev/null || true)"
  [[ -n "$CARGO" ]] && export CARGO
fi
: "${CC_riscv32im_risc0_zkvm_elf:=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-gcc}"
: "${CXX_riscv32im_risc0_zkvm_elf:=$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-g++}"
export CC_riscv32im_risc0_zkvm_elf CXX_riscv32im_risc0_zkvm_elf
if [[ -z "${CMAKE_PREFIX_PATH:-}" ]]; then
  [[ "$(uname -s)" == Darwin ]] && export CMAKE_PREFIX_PATH="$(brew --prefix 2>/dev/null || echo /opt/homebrew)" \
                               || export CMAKE_PREFIX_PATH="/usr"
fi

mkdir -p "$OUT/logs" "$ARTIFACTS_DIR"

# ── preflight ────────────────────────────────────────────────────────────────
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ -d "$OPENVM_DIR" ]] || fail "no OpenVM guest dir at $OPENVM_DIR (set OPENVM_DIR=)"
[[ -f "$OPENVM_DIR/openvm.toml" ]] || fail "no openvm.toml under $OPENVM_DIR"

want_arm() { [[ " $ARMS " == *" $1 "* ]]; }

if want_arm vanilla && [[ ! -x "$BIN_BASE" ]]; then
  if [[ "${BUILD:-0}" == 1 ]]; then
    echo "== building the baseline driver =="
    ( cd "$SCRIPT_DIR" && cargo build --release --bin monad-zkvm-openvm-script ) || fail "build failed"
  else
    fail "baseline driver not built. Run:
    cd $SCRIPT_DIR && cargo build --release --bin monad-zkvm-openvm-script
  (or re-run this script with BUILD=1)"
  fi
fi
# The guest cmake project add_subdirectory()s third_party/* out of the monad checkout. A fresh clone
# has them as empty submodule dirs, and cmake then fails with "does not contain a CMakeLists.txt"
# per dep — checked here because that failure surfaces 40 lines deep in a build-script panic.
MONAD_ROOT="$(cd "$OPENVM_DIR/../.." 2>/dev/null && pwd || echo "")"
if [[ -n "$MONAD_ROOT" && -d "$MONAD_ROOT/third_party" && ! -f "$MONAD_ROOT/third_party/immer/CMakeLists.txt" ]]; then
  echo "WARN: third_party submodules are not initialised in $MONAD_ROOT" >&2
  echo "      git -C $MONAD_ROOT submodule update --init --recursive \\" >&2
  echo "          third_party/{unordered_dense,cthash,immer,komihash,nlohmann_json,blst,evmc,ethash,intx,magic_enum,quill,c-kzg-4844}" >&2
  want_arm apc && fail "the APC arm builds the guest and cannot do it without them"
fi

# The rv32im/ilp32 cross toolchain: needed by the APC arm every run, and by `cargo openvm build`
# for the baseline arm. Checked here because the failure otherwise surfaces deep inside cmake, and
# because "has a RISC-V gcc" is not the same as "can emit rv32im/ilp32" — a rv64-only toolchain
# (the usual one) prints just ".;" for -print-multi-lib and cannot build this guest at all.
if want_arm apc || [[ "${CHECK_TOOLCHAIN:-1}" == 1 ]]; then
  gcc="$RISCV_TOOLCHAIN_DIR/bin/riscv64-unknown-elf-gcc"
  if [[ ! -x "$gcc" ]]; then
    echo "WARN: no $gcc" >&2
    echo "      riscv64-elf.cmake requires that exact prefix; symlink your toolchain's" >&2
    echo "      riscv-none-elf-* / riscv64-none-elf-* binaries to riscv64-unknown-elf-*." >&2
    want_arm apc && fail "the APC arm cannot build the guest without it"
  elif ! "$gcc" -print-multi-lib 2>/dev/null | grep -q 'rv32im/ilp32'; then
    echo "WARN: $gcc has no rv32im/ilp32 multilib (-print-multi-lib):" >&2
    "$gcc" -print-multi-lib 2>&1 | sed 's/^/        /' >&2
    want_arm apc && fail "this guest is rv32im/ilp32 only — a rv64-only toolchain cannot build it"
  fi
fi

if want_arm apc && [[ ! -x "$BIN_APC" ]]; then
  if [[ "${BUILD:-0}" == 1 ]]; then
    echo "== building the autoprecompile driver (large dep tree, expect a long first build) =="
    ( cd "$SCRIPT_DIR" && cargo build --release --features powdr --bin monad-zkvm-openvm-powdr ) || fail "build failed"
  else
    fail "autoprecompile driver not built. Run:
    cd $SCRIPT_DIR && cargo build --release --features powdr --bin monad-zkvm-openvm-powdr
  (or re-run this script with BUILD=1)"
  fi
fi
# The baseline arm consumes a transpiled guest; it is NOT built by the driver (the APC arm builds
# and transpiles its own, which is why only this arm needs the .vmexe).
if want_arm vanilla && [[ ! -f "$EXE" ]]; then
  fail "no transpiled guest at $EXE
  Build it with powdr's cargo-openvm fork (the .vmexe format is not compatible with openvm-org v2.0.1):
    cargo install --git https://github.com/powdr-labs/openvm.git --tag v2.0.0-beta.2-powdr.1 \\
        cargo-openvm --no-default-features --features parallel,jemalloc --locked
    cd $OPENVM_DIR && RISCV_TOOLCHAIN_DIR=~/riscv_gcc_multilib cargo openvm build"
fi
for b in $BLOCKS $PROFILE_BLOCK; do
  [[ -f "$FIXTURES/$b.witness" ]] || fail "missing witness: $FIXTURES/$b.witness"
done

[[ -f "$CSV" ]] || echo "block,arm,witness_bytes,instructions,segments,seg_mem_gib,apcs,profile_block,root_match,secs,peak_rss_gib,compile_secs,apc_setup_secs,status" > "$CSV"

# done <block> <arm> — already recorded ok for this exact configuration?
done_already() {
  python3 - "$CSV" "$1" "$2" "${SEG_MEM_GIB:-}" "$APCS" "$PROFILE_BLOCK" <<'PY'
import csv, sys
path, block, arm, mem, apcs, prof = sys.argv[1:7]
apcs = apcs if arm == "apc" else ""
prof = prof if arm == "apc" else ""
for r in csv.DictReader(open(path)):
    if (r["block"], r["arm"], r["seg_mem_gib"], r["apcs"], r["profile_block"], r["status"]) \
       == (block, arm, mem, apcs, prof, "ok"):
        sys.exit(0)
sys.exit(1)
PY
}

echo "== step 0: segment counts on CPU =="
echo "blocks    : $(echo $BLOCKS | wc -w | tr -d ' ') ($BLOCKS)"
echo "arms      : $ARMS      apcs=$APCS  profile_block=$PROFILE_BLOCK  seg_mem_gib=${SEG_MEM_GIB:-<openvm default 15>}"
echo "csv       : $CSV"
echo

for block in $BLOCKS; do
  wit="$FIXTURES/$block.witness"
  root="$FIXTURES/$block.post_state_root"
  wbytes="$(wc -c < "$wit" | tr -d ' ')"
  for arm in $ARMS; do
    if done_already "$block" "$arm"; then
      printf "%-10s %-8s already recorded, skip\n" "$block" "$arm"; continue
    fi
    log="$OUT/logs/$arm-$block.log"
    case "$arm" in
      vanilla)
        set -- "$BIN_BASE" --input "$wit" --exe "$EXE" --config "$OPENVM_DIR/openvm.toml" --mode segment
        [[ -n "$SEG_MEM_GIB" ]] && set -- "$@" --segment-memory-gib "$SEG_MEM_GIB"
        ;;
      apc)
        # --segments stops right after the metered execute, so this never builds a STARK.
        set -- "$BIN_APC" --input "$wit" --profile-input "$FIXTURES/$PROFILE_BLOCK.witness" \
               --guest "$OPENVM_DIR" --bin monad-zkvm-openvm --config "$OPENVM_DIR/openvm.toml" \
               --autoprecompiles "$APCS" --artifacts-dir "$ARTIFACTS_DIR" --segments
        [[ -n "$SEG_MEM_GIB" ]] && set -- "$@" --segment-memory-gib "$SEG_MEM_GIB"
        ;;
      *) fail "unknown arm '$arm' (vanilla|apc)" ;;
    esac
    printf "%-10s %-8s running... " "$block" "$arm"
    t0=$(python3 -c 'import time;print(time.time())')
    ( cd "$SCRIPT_DIR" && "$@" ) > "$log" 2>&1; rc=$?
    t1=$(python3 -c 'import time;print(time.time())')

    # Parsed into a file, not "$(python3 … <<PY …)": macOS bash 3.2 mis-parses a quote inside a
    # here-doc nested in a command substitution (it scans for the closing paren without honouring
    # the here-doc), so an apostrophe in the Python below would be a syntax error on the Mac.
    python3 - "$log" "$root" "$rc" "$t0" "$t1" > "$OUT/.row" <<'PY'
import os, re, sys
log, rootf, rc, t0, t1 = sys.argv[1:6]
txt = open(log, encoding="utf-8", errors="replace").read()
def grab(pat):
    m = re.search(pat, txt, re.M)
    return m.group(1) if m else ""
instr   = grab(r"^Instructions:\s+(\d+)")
segs    = grab(r"^Segments:\s+(\d+)")
out     = grab(r"^Output: 0x([0-9a-fA-F]+)").lower()
rss     = grab(r"^Peak RSS:\s+([\d.]+) GiB")
comp    = grab(r"^Compile \+ transpile:\s+([\d.]+) s")
setup   = grab(r"^APC generate \+ select \+ setup:\s+([\d.]+) s")
# Same verdict convention as guests/monad/ev.sh: the .post_state_root file is "0x<hex>" and the
# guest's public-value output is padded, so the root is checked as a substring.
verdict = "no-expected"
# A run that died before committing anything has no root to compare — saying MISMATCH there reads as
# "the guest computed the wrong answer", which is a very different and much scarier claim.
if not out:
    verdict = "n/a"
elif os.path.exists(rootf):
    h = "".join(c for c in open(rootf).read().strip().lower().removeprefix("0x")
                if c in "0123456789abcdef")
    if h:
        verdict = "PASS" if h in out else ("PASS(rev)" if h[::-1] in out else "MISMATCH")
status = "ok" if rc == "0" and segs else f"FAIL(rc={rc})"
print("|".join([instr, segs, verdict, f"{float(t1)-float(t0):.1f}", rss, comp, setup, status]))
PY
    IFS='|' read -r instr segs verdict secs rss comp setup status < "$OUT/.row"
    echo "$block,$arm,$wbytes,$instr,$segs,${SEG_MEM_GIB:-},$([[ $arm == apc ]] && echo "$APCS"),$([[ $arm == apc ]] && echo "$PROFILE_BLOCK"),$verdict,$secs,$rss,$comp,$setup,$status" >> "$CSV"
    printf "segments=%-5s instr=%-12s root=%-9s %ss  [%s]\n" "${segs:-?}" "${instr:-—}" "$verdict" "$secs" "$status"
    [[ "$status" == ok ]] || echo "   see $log" >&2
  done
done

# ── summary: the APC segment ratio, which is what step 0 is for ──────────────
echo
python3 - "$CSV" "$APCS" "${SEG_MEM_GIB:-}" <<'PY'
import csv, sys
rows = [r for r in csv.DictReader(open(sys.argv[1])) if r["status"] == "ok"]
apcs, mem = sys.argv[2], sys.argv[3]
cur = [r for r in rows if r["seg_mem_gib"] == mem and r["arm"] == "vanilla"] + \
      [r for r in rows if r["seg_mem_gib"] == mem and r["arm"] == "apc" and r["apcs"] == apcs]
by = {}
for r in cur:
    by.setdefault(int(r["block"]), {})[r["arm"]] = r
print(f"{'block':>9} {'witness MB':>10} {'segments':>9} {'seg APC':>8} {'ratio':>6} {'instr (M)':>10} {'root':>9}")
ratios = []
for b in sorted(by):
    v, a = by[b].get("vanilla"), by[b].get("apc")
    seg_v = int(v["segments"]) if v else None
    seg_a = int(a["segments"]) if a else None
    ratio = seg_v / seg_a if seg_v and seg_a else None
    if ratio: ratios.append(ratio)
    mb = int((v or a)["witness_bytes"]) / 1e6
    instr = f"{int(v['instructions'])/1e6:,.0f}" if v and v["instructions"] else "—"
    print(f"{b:>9} {mb:>10.2f} {seg_v if seg_v else '—':>9} {seg_a if seg_a else '—':>8} "
          f"{f'{ratio:.2f}x' if ratio else '—':>6} {instr:>10} {(v or a)['root_match']:>9}")
if ratios:
    ratios.sort()
    med = ratios[len(ratios)//2]
    print(f"\nAPC segment ratio (baseline/apc, apcs={apcs}): median {med:.2f}x  "
          f"min {min(ratios):.2f}x  max {max(ratios):.2f}x   over {len(ratios)} blocks")
    print("Published CPU pair on 22200003 for reference: 63 -> 43 segments (1.47x), 16 APCs.")
mism = [r["block"] for r in cur if r["root_match"] not in ("PASS", "PASS(rev)")]
if mism: print(f"\n⚠️  root not verified on: {' '.join(sorted(set(mism)))}")
print(f"\ncsv: {sys.argv[1]}")
PY
