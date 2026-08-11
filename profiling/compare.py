#!/usr/bin/env python3
"""compare — one-command aggregate comparison of two guests running the SAME zkVM.

Answers "how much more does guest A cost than guest B, over a whole set of blocks?" —
the headline ratio, its spread, and where it comes from — instead of eyeballing blocks
one at a time.

ONE command collects and reports everything — work-units (cycles/steps), prover work
(SP1 PGU / ZisK COST) with its category split, precompile counts, gas and tx counts, and
the honest execution time (measured with SP1's gas-estimation pass off):

    ./compare.py --block-min 25551991 --block-max 25552607 --html --json out.json

    ./compare.py                              # the DEFAULT pair of axes (cur-zisk + cur-sp1)
    ./compare.py --axis zisk --limit 20       # one axis, first 20 common blocks
    ./compare.py --blocks 25552005-25552088   # an explicit set (ranges and/or comma list)
    ./compare.py --quick                      # skip ZisK's slow instrumented COST pass
    ./compare.py --deep 5                     # + per-module aggregate diff (hotspots.py)
    ./compare.py --spread                      # profile the p90 and p10 blocks, then diff them

Pick the range deliberately: the fixtures mix a contiguous run of blocks with strided (~every
10th) and one-off older ones, and a median over that mixture isn't a median over consecutive
mainnet blocks. `--limit` after filtering keeps a trial run short.

Why same-zkVM pairs: work-units are only comparable inside one VM (SP1 cycles ≠ ZisK
steps), so each axis pits two GUEST PROGRAMS against each other on one backend:

    zisk : monad-zisk vs zisk-reth      (Monad EVM vs reth, on ZisK)
    sp1  : monad-sp1  vs rsp            (Monad EVM vs reth, on SP1)

Two passes:
  · FAST (default) — runs each guest once per block for its work-unit + exec time
    (+ EVM gas, which the reth ZisK guest prints). Results are CACHED, so re-runs and
    incremental block sets are instant.
  · DEEP (--deep N) — delegates to hotspots.py: profiles N blocks per side with
    --aggregate and diffs them, to show WHICH modules carry the delta.

Outputs: a terminal summary (the one-glance view), plus --json / --html for the
per-block detail. See README.md for the tool map and the framing rules.
"""

import argparse, glob, hashlib, json, math, os, platform, re, statistics, subprocess, struct, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# `cache` is a sibling module, not a package: put HERE on the path explicitly rather than relying on
# it being sys.path[0], which only holds when this file is the entry point.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cache as _cachemod          # noqa: E402
# Per-family ratios measured under two attributions — the shipped reth guest and a no-inline rebuild
# (profiling/inline-robust.py). Optional: absent file simply means the column is not shown.
VERDICT = os.path.join(HERE, 'results', 'inline-verdict.json')
BATCH_MAX = 40          # max inputs per batched sp1-runner process (see sp1_batches)
# ZisK's cost for one keccak op, from zisk `core/src/zisk_ops_costs.rs`: KECCAK_COST = 25 * 3022.
# Used to turn the per-opcode COST that ziskemu reports back into a call count (it prints no count
# for precompiles). Update if ZisK's cost model changes.
ZISK_KECCAK_COST = 25 * 3022

# ───────────────────────────── axes (same-zkVM guest pairs) ─────────────────────────────
# An axis is TWO builds of the same zkVM measured on the same blocks. Its `name` is the axis label
# and each side's `name` labels a build — neither is ever read from a filename, so a build needs no
# renaming (and no copy) to be axis-able.
#
# 'src' says WHERE that side's input comes from. It is a property of the GUEST, not of the axis — so
# the two sides of one axis usually differ.
#   monad : the shared Monad witness, guests/monad/{fixtures,inputs}/<block>.witness
#   bin   : the guest's own pre-generated 1-<block>.bin  (rsp, zisk-reth: their own format)
# The SHAPE is not spelled out here, it is derived from the backend (see needs_framing): ziskos wants
# LE64(len) + witness + pad8, SP1 reads the buffer verbatim, and a `bin` is already in its own shape.
# Do not reintroduce a per-side shape: it could only ever match the backend or be broken, and handing
# a guest the wrong shape parses as garbage instead of failing.
#
# 'ephemeral' (optional) marks an axis that exists for ONE campaign and should not outlive it: give
# it a string saying why it exists and when to drop it. Ablation axes are the case — they compare a
# build against a tip, so they rot silently the moment that tip moves, measuring a variant against a
# base nobody cares about any more. `./axis.py prune` lists and removes them; `./axis.py list` marks
# them. An axis with no `ephemeral` key is a durable comparison of the project.
AXES = {
    # ⚠ `zisk` and `sp1` MOVE. Their `a` side is guests/monad-{zisk,sp1}/*.elf, a symlink to
    # guests/monad/monad-zkvm-guest-*.elf — the pair `use-gen` REWRITES. Select a generation and these
    # two axes measure whatever it installed, under a label that has not changed. Every other axis
    # names a path of its own and is pinned. a_ident records the sha of what actually ran, and a run
    # whose axes land on one binary says so (see the one-binary-two-labels warning in main()).
    'zisk': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-zisk', 'elf': 'guests/monad-zisk/monad-zisk.elf', 'src': 'monad'},
             'b': {'name': 'zisk-reth',  'elf': 'guests/zisk-reth/zisk-reth.elf',   'src': 'bin'}},
    'sp1':  {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-sp1', 'elf': 'guests/monad-sp1/monad-sp1.elf', 'src': 'monad'},
             'b': {'name': 'rsp',       'elf': 'guests/rsp/rsp.elf',             'src': 'bin'}},
    # ── post-rebase axes (2026-08-08). The witness format changed with the reader rework, so a
    # PRE-rebase BUILD cannot read these fixtures at all, and figures measured with one are history.
    # That is a statement about builds, not about the two
    # axes above: those follow use-gen and today read whatever generation is selected.
    #   monad-sam-{zisk,sp1} = sam/zkvm-zisk-sp1 rebased, the baseline we are trying to beat
    #   monad-r3-{zisk,sp1}  = al/zkvm-r3, that baseline plus 17 measured commits AND
    #                          the soundness binding (three public values + body roots),
    #                          so it proves a STRICTLY STRONGER statement than the baseline.
    # Named after the BRANCH, not the role: `current` and `opt` describe a position that moves, and
    # `monad-today` — the same idea a week earlier — was already wrong by the time it was read. The
    # backend is explicit on both, so the two peers read as peers. See guests/README.md § Naming.
    # Renamed 2026-08-10; the profile cache resolves builds by content, so the old labels kept
    # working throughout and `builds.json` records both.
    'cur-zisk': {'backend': 'zisk', 'unit': 'steps',
             # The baseline sides point straight into the generation's own elf/ — that pair IS the
             # canonical build of this witness set, recorded in its PROVENANCE.md, so a copy under
             # monad-variants/ would be a second place to keep in step by hand. An axis takes its NAME from
             # `name`, never from the filename, so nothing needs renaming to be axis-able. Builds that are no
             # generation's canonical pair (the ablations, a branch build) still live under monad-variants/.
             'a': {'name': 'monad-sam-zisk', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-zisk.elf',
                   'src': 'monad'},
             'b': {'name': 'zisk-reth', 'elf': 'guests/zisk-reth/zisk-reth.elf', 'src': 'bin'}},
    'cur-sp1': {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-sam-sp1', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-sp1.elf',
                   'src': 'monad'},
             'b': {'name': 'rsp', 'elf': 'guests/rsp/rsp.elf', 'src': 'bin'}},
    'opt-self': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-sam-zisk', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-zisk.elf',
                   'src': 'monad'}},
    'opt-zisk': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'},
             'b': {'name': 'zisk-reth', 'elf': 'guests/zisk-reth/zisk-reth.elf', 'src': 'bin'}},
    'opt-self-sp1': {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-r3-sp1', 'elf': 'guests/monad-variants/r3/monad-r3-sp1.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-sam-sp1', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-sp1.elf',
                   'src': 'monad'}},
    # ── Ablation axes (2026-08-09): the tip against itself minus ONE lever, so an axis prices that
    # lever alone. `ab-*` ablate the arith256 routings, `ab2-*` the guest-side levers. Read the
    # verdict in COST as well as steps — two of these levers inverted once the base moved.
    # `ab2-bswap` was declared here and REMOVED 2026-08-10. Its ELF was a throwaway test build, never
    # kept — so the axis had nothing to resolve and is not coming back. Do not re-add it.
    # The byte/bit lever it was meant to price was measured by other means (22 blocks) and that
    # survives: levers.py and the family table below read that result from results/, and the family
    # itself is analysed in hotspots.py's taxonomy.
    'opt-sp1': {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-r3-sp1', 'elf': 'guests/monad-variants/r3/monad-r3-sp1.elf',
                   'src': 'monad'},
             'b': {'name': 'rsp', 'elf': 'guests/rsp/rsp.elf', 'src': 'bin'}},
    'ab2-kec2': {'ephemeral': 'ablation of the word-wise keccak lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-kec2', 'elf': 'guests/monad-variants/ab/ab2-no-kec2.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-nodeid': {'ephemeral': 'ablation of the NodeId 64-bit lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-nodeid', 'elf': 'guests/monad-variants/ab/ab2-no-nodeid.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-flat': {'ephemeral': 'ablation of the flat hash store lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-flat', 'elf': 'guests/monad-variants/ab/ab2-no-flat.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-div': {'ephemeral': 'ablation of the 128/64 division lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-div', 'elf': 'guests/monad-variants/ab/ab2-no-div.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-tokens': {'ephemeral': 'ablation of the calldata SWAR lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-tokens', 'elf': 'guests/monad-variants/ab/ab2-no-tokens.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-hashinline': {'ephemeral': 'ablation of the map key-hash inlining lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-hashinline', 'elf': 'guests/monad-variants/ab/ab2-no-hashinline.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-fasthead': {'ephemeral': 'ablation of the child_ref fast heads lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-fasthead', 'elf': 'guests/monad-variants/ab/ab2-no-fasthead.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-arena': {'ephemeral': 'ablation of the bump arena lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-arena', 'elf': 'guests/monad-variants/ab/ab2-no-arena.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-fmix': {'ephemeral': 'ablation of the fmix64 finalizer lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-fmix', 'elf': 'guests/monad-variants/ab/ab2-no-fmix.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab2-scanidx': {'ephemeral': 'ablation of the JUMPDEST scan index lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab2-no-scanidx', 'elf': 'guests/monad-variants/ab/ab2-no-scanidx.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab-opstar': {'ephemeral': 'ablation of the operator* via arith256 lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab-no-opstar', 'elf': 'guests/monad-variants/ab/ab-no-opstar.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab-addmod': {'ephemeral': 'ablation of the addmod via arith256 lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab-no-addmod', 'elf': 'guests/monad-variants/ab/ab-no-addmod.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'ab-mulmod': {'ephemeral': 'ablation of the MULMOD via arith256 lever, 2026-08-09 re-verdict; drop when the r3 tip moves',
             'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'ab-no-mulmod', 'elf': 'guests/monad-variants/ab/ab-no-mulmod.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'}},
    'monad-zisk-vs-zisk-reth': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-zisk', 'elf': 'guests/monad-zisk/monad-zisk.elf',
                   'src': 'monad'},
             'b': {'name': 'zisk-reth', 'elf': 'guests/zisk-reth/zisk-reth.elf',
                   'src': 'bin'}},
    'monad-r3-zisk-vs-monad-sam-zisk': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-r3-zisk', 'elf': 'guests/monad-variants/r3/monad-r3-zisk.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-sam-zisk', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-zisk.elf',
                   'src': 'monad'}},
    'monad-sp1-vs-rsp': {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-sp1', 'elf': 'guests/monad-sp1/monad-sp1.elf',
                   'src': 'monad'},
             'b': {'name': 'rsp', 'elf': 'guests/rsp/rsp.elf',
                   'src': 'bin'}},
    'monad-r3-sp1-vs-monad-sam-sp1': {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-r3-sp1', 'elf': 'guests/monad-variants/r3/monad-r3-sp1.elf',
                   'src': 'monad'},
             'b': {'name': 'monad-sam-sp1', 'elf': 'guests/monad/gen/offsettriedb-rework-2026-08/elf/monad-zkvm-guest-sp1.elf',
                   'src': 'monad'}},
}
# The default run is the shipped guest only: adding the levers axes must not silently change what
# `./compare.py` with no arguments reports.
DEFAULT_AXES = ('cur-zisk', 'cur-sp1')

def rp(*p): return os.path.join(REPO, *p)

# ───────────────────────────────── input discovery ──────────────────────────────────────

def monad_witness(block):
    # ONE resolution path, and `fixtures` is its head: it is the symlink `use-gen` maintains, so the
    # generation the tooling reads is by construction the one `use-gen` reports. There used to be a
    # `fixtures-v2/` entry ahead of it — a set dropped beside the generation layout rather than into
    # it — and it silently won: every post-rebase number came from fixtures-v2 while `use-gen` still
    # named the pre-rework generation as current. Nothing reported the disagreement, because the two
    # sets are the SAME wire format (both `offset`/`4d5a5701`) and witness-fmt cannot tell them apart.
    # That set is now gen/offsettriedb-rework-2026-08 and `fixtures` points at it. Do not re-add a
    # path ahead of `fixtures`: a second source is exactly how the two drifted apart.
    for p in (rp('guests/monad/fixtures', f'{block}.witness'),
              rp('guests/monad/inputs', f'1-{block}.witness'),
              rp('guests/monad/fixtures', f'1-{block}.witness')):
        if os.path.exists(p): return p
    return None

def guest_bin(guest, block):
    # `fixtures` FIRST: it is the generation-selected set, the one `use-gen` points at and the one
    # guests/monad/fixtures follows. `inputs/` predates that split and can hold a witness for the
    # same block from an older generation — for zisk-reth block 25229951 the two differ by 122 kB.
    # In the 25551991-25552607 range the two are byte-identical where both exist (488 of 488 by
    # size, 6 of 6 by hash) and rsp has no `inputs/` entry at all, so this reorder changes no
    # measurement there; it changes which file is authoritative everywhere else.
    for d in ('fixtures', 'inputs'):
        p = rp('guests', guest, d, f'1-{block}.bin')
        if os.path.exists(p): return p
    return None

def resolve_input(side, block):
    if side['src'] == 'bin':      return guest_bin(side['name'], block)
    return monad_witness(block)   # 'monad': the shape is applied later, see needs_framing

def blocks_for(axis):
    """Blocks runnable on BOTH sides of an axis."""
    ax = AXES[axis]
    return sorted(b for b in all_monad_blocks()
                  if resolve_input(ax['a'], b) and resolve_input(ax['b'], b))

def all_monad_blocks():
    out = set()
    for pat in ('guests/monad/fixtures/*.witness', 'guests/monad/inputs/*.witness'):
        for f in glob.glob(rp(pat)):
            m = re.search(r'(\d{6,})', os.path.basename(f))
            if m: out.add(int(m.group(1)))
    return out

# ─────────────────────────────────── runners ────────────────────────────────────────────

def needs_framing(backend, side):
    """Does this side's input have to be framed before the guest sees it?

    A function of the BACKEND, never a per-side choice: ziskos reads a length-prefixed file, SP1 reads
    the buffer verbatim. A `bin` input is already in its guest's own shape and is never touched."""
    return backend == 'zisk' and side['src'] == 'monad'


def frame_ziskos(src, dst):
    """ziskos input framing: LE64(len) + witness + zero-pad to 8 (see guests/monad/ev.sh)."""
    d = open(src, 'rb').read()
    open(dst, 'wb').write(struct.pack('<Q', len(d)) + d + b'\x00' * ((-(8 + len(d))) % 8))

def run_zisk(emu, elf, inp, src_kind, with_cost=False):
    """`-m` for steps + an honest duration. COST needs `-X --stats`, whose instrumentation
    slows execution ~7× (0.08s → 0.57s here) — so it's a SECOND pass, and opt-in, rather than
    silently wrecking the timing we report."""
    tmp, inp0 = None, inp
    try:
        if src_kind == 'monad':   # run_zisk IS the zisk path; see needs_framing
            tmp = tempfile.NamedTemporaryFile(suffix='.zisk.bin', delete=False).name
            frame_ziskos(inp, tmp); inp = tmp
        t0 = time.time()
        p = subprocess.run([emu, '-e', elf, '-i', inp, '-m'], capture_output=True, text=True)
        wall = time.time() - t0
        txt = p.stdout + p.stderr
        cost, cats, kec, ops, opsn = None, None, None, None, None
        if with_cost:
            q = subprocess.run([emu, '-e', elf, '-i', inp, '-X', '-S', '--sdk', '--opcodes'],
                               capture_output=True, text=True)
            qt = q.stdout + q.stderr
            mc = re.search(r'COST\s+([\d,]+)', qt)
            if mc: cost = int(mc.group(1).replace(',', ''))
            # Same pass also prints a COST DISTRIBUTION SUMMARY (Base/Main/Opcodes/Precompiles/
            # Memory) — free here, and it answers "is this block precompile-bound?".
            cats = {m.group(1): int(m.group(2).replace(',', ''))
                    for m in re.finditer(r'║\s+(Base|Main|Opcodes|Precompiles|Memory)\s+[█░]+\s+([\d,]+)', qt)}
            cats = cats or None
            # `--opcodes` adds a per-opcode cost table. ZisK gives no call COUNT for precompiles (the
            # OPS column is blank for them), but cost/ZISK_KECCAK_COST recovers it EXACTLY: the costs
            # divide by that constant with no remainder, and the same Monad guest run on both backends
            # gives an identical count (5,783 derived here = 5,783 KECCAK_PERMUTE counted by SP1).
            # Comparing zisk-reth against rsp instead shows a small real gap (5,205 vs 5,227) — two
            # different guest programs with different trie code, not a measurement error.
            mk = re.search(r'║\s+keccak\s+[█░]*\s+([\d,]+)', qt)
            if mk: kec = int(mk.group(1).replace(',', ''))
            # Whole per-opcode cost table while we are here (lower-case names only — the category
            # rows are capitalised). Gives the ZisK-side counterpart of SP1's opcode counts:
            # dma_memcpy for copying, add/or/and/xor/sll for plain arithmetic, etc.
            ops = {m.group(1): int(m.group(2).replace(',', ''))
                   for m in re.finditer(r'║\s+([a-z_][a-z_0-9]*)\s+[█░]+\s+([\d,]+)\s+[\d.]+%', qt)}
            ops = ops or None
            # The same rows carry an "OPS + FROPS" column: the actual instruction COUNT. Keep it
            # separately — cost is NOT proportional to it (measured cost/op from 0.23 on `sll` to
            # 0.97 on `xor`, because the cheaper "frops" share differs per opcode), so a cost ratio
            # is not an instruction-count ratio.
            opsn = {m.group(1): int(m.group(2).replace(',', ''))
                    for m in re.finditer(
                        r'║\s+([a-z_][a-z_0-9]*)\s+[█░]+\s+[\d,]+\s+[\d.]+%\s+║\s+([\d,]+)\s+[\d,]+',
                        qt)}
            opsn = opsn or None
    finally:
        if tmp and os.path.exists(tmp): os.remove(tmp)
    grab = lambda pat, cast=int: (cast(re.search(pat, txt).group(1)) if re.search(pat, txt) else None)
    work = grab(r'steps=(\d+)')
    if p.returncode != 0 or work is None:
        return {'error': f'ziskemu rc={p.returncode}: {txt.strip()[-300:]}'}
    r = {'work': work, 'secs': grab(r'duration=([\d.]+)', float) or round(wall, 3),
         'gas': grab(r'Gas Consumed:\s*(\d+)'), 'txs': grab(r'Transaction Count:\s*(\d+)')}
    if cost is not None: r['cost'] = cost
    if cats: r['cats'] = cats
    if kec is not None:
        r['kec_cost'] = kec
        r['kec'] = round(kec / ZISK_KECCAK_COST)      # comparable to SP1's KECCAK_PERMUTE count
    if ops: r['ops'] = ops
    if opsn: r['opsn'] = opsn   # instruction COUNTS (see above)
    if os.path.exists(inp0): r['insz'] = os.path.getsize(inp0)
    return r

def run_sp1(runner, elf, inp, _src_kind):
    with tempfile.TemporaryDirectory() as td:
        rep = os.path.join(td, 'report.json')
        t0 = time.time()
        p = subprocess.run([runner, '--mode', 'execute', '--elf', elf, '--input', inp, '--report', rep],
                           env=dict(os.environ, SP1_PROVER='cpu'), capture_output=True, text=True)
        wall = time.time() - t0
        if not os.path.exists(rep):
            return {'error': f'sp1-runner rc={p.returncode}: {(p.stdout + p.stderr).strip()[-300:]}'}
        j = json.load(open(rep))
    # NOTE: the SP1 report's 'gas' is PROVER gas (PGU), not EVM gas — kept separate.
    r = {'work': j.get('cycles'), 'secs': round(j.get('elapsed_secs', wall), 3),
         'pgu': j.get('gas'), **_sp1_extra(j)}
    if os.path.exists(inp): r['insz'] = os.path.getsize(inp)
    return r

_HS = None
_SPLIT_WARNED = False
def _hotspots():
    """Lazily load hotspots.py to reuse its SP1 trace-area weights (rv64im_costs.json) and its
    syscall→AIR mapping instead of duplicating either here."""
    global _HS
    if _HS is None:
        import importlib.util as iu
        spec = iu.spec_from_file_location('_hs', os.path.join(HERE, 'hotspots.py'))
        m = iu.module_from_spec(spec); spec.loader.exec_module(m)
        _HS = m
    return _HS

def _sp1_extra(j):
    """Precompile (syscall) counts from the report we already parse — the cheapest per-block
    diagnostic there is: they say WHAT the block made the guest do, not just how much. Plus the
    trace-cost split, so the SP1 axis reports the same Main/Opcodes/Precompiles/Memory shares
    ZisK does natively."""
    er = j.get('execution_report') or {}
    sysc = {k: v for k, v in (er.get('syscall_counts') or {}).items() if v}
    out = {}
    if sysc: out['sys'] = sysc
    if j.get('total_syscalls'): out['tsys'] = j['total_syscalls']
    if j.get('exit_code') not in (None, 0): out['exit'] = j['exit_code']
    # Opcode counts folded into a few interpretable groups. NOTE: 256-bit modular arithmetic is
    # implemented in SOFTWARE (shifts/branches), so DIV/REM counts stay tiny and do NOT capture it —
    # that shows up as branch/shift volume instead.
    oc = {k: v for k, v in (er.get('opcode_counts') or {}).items() if v}
    if oc:
        LOAD_STORE = ('LB', 'LBU', 'LD', 'LH', 'LHU', 'LW', 'LWU', 'SB', 'SD', 'SH', 'SW')
        grp = {
            'mem':    sum(v for k, v in oc.items() if k in LOAD_STORE),
            'branch': sum(v for k, v in oc.items() if k.startswith('B')),
            'shift':  sum(v for k, v in oc.items() if k.startswith(('SLL', 'SRL', 'SRA'))),
            'mul':    sum(v for k, v in oc.items() if k.startswith('MUL')),
            'divrem': sum(v for k, v in oc.items() if k.startswith(('DIV', 'REM'))),
            'ecall':  oc.get('ECALL', 0),
        }
        out['ops'] = {k: v for k, v in grp.items() if v}
    try:
        hs = _hotspots()
        if hs._SP1_DEFAULT_COSTS and er.get('opcode_counts') and j.get('cycles'):
            weights = json.load(open(hs._SP1_DEFAULT_COSTS))
            _tot, cats, _ops = hs._sp1_costs(j, weights)
            out['cats'] = {c['name']: c['cost'] for c in cats if c['cost']}
    except Exception as exc:
        # A bonus, so never fail collection over it — but say so once, rather than silently
        # dropping a column the report would otherwise show.
        global _SPLIT_WARNED
        if not _SPLIT_WARNED:
            _SPLIT_WARNED = True
            print(f"  note: SP1 trace-cost split unavailable ({type(exc).__name__}: {exc}) — the "
                  f"'% opcodes' / '% precompiles' columns will be blank on this axis")
    return out

# ───────────────────────────────── cache + collect ──────────────────────────────────────

def load_cache():
    """The per-block, content-addressed cache — see cache-format.md. Loading is lazy per block, so
    this is instant regardless of how much history the cache holds."""
    return _cachemod.Cache()

def save_cache(c):
    c.save()

def sp1_has_batch(runner):
    """Does this sp1-runner build support --batch? (older binaries don't)"""
    try:
        h = subprocess.run([runner, '--help'], capture_output=True, text=True, timeout=30)
        return '--batch' in (h.stdout + h.stderr)
    except Exception:
        return False

def sp1_batches(todo, runner, jobs, with_nogas=False):
    """Chunk SP1 work into `jobs` per-side batches — one process per chunk.

    Two wins at once: the ~6s ProverClient startup is paid once per CHUNK instead of
    once per block (sp1-runner --batch), and the chunks still run in parallel."""
    by_elf = {}
    for key, side, elf, b in todo:
        by_elf.setdefault((side['name'], elf), []).append((key, side, b))
    chunks = []
    for (_name, elf), items in by_elf.items():
        # Chunk size trades two things off. Too small and the ~6s startup we're amortising
        # comes back (so: at least 4 inputs, or 1 process for a tiny run). Too big and the
        # cache only checkpoints once per chunk, so an interrupted sweep loses a lot — hence
        # the CAP: at 40 inputs the startup is already down to ~0.16s/block, and we get a
        # save every 40 blocks instead of every 125.
        k = max(1, min(jobs, len(items) // 4))
        size = min(-(-len(items) // k), BATCH_MAX)      # ceil, capped
        chunks += [(elf, items[i:i + size]) for i in range(0, len(items), size)]

    def run_chunk(chunk):
        elf, items = chunk
        with tempfile.TemporaryDirectory() as td:
            listing, rdir = os.path.join(td, 'list.txt'), os.path.join(td, 'rep')
            by_stem = {}
            with open(listing, 'w') as fh:
                for key, side, b in items:
                    p = resolve_input(side, b)
                    fh.write(p + '\n')
                    by_stem[os.path.splitext(os.path.basename(p))[0]] = key
            base = [runner, '--mode', 'execute', '--elf', elf, '--batch', listing]
            env = dict(os.environ, SP1_PROVER='cpu')
            subprocess.run(base + ['--report-dir', rdir], env=env, capture_output=True, text=True)
            # Second pass with --no-gas: SP1's gas-estimation pass inflates the timing ~1.7×, but
            # disabling it zeroes the cycle count. So take cycles from the pass above and the honest
            # execution time from this one. Cheap: --no-gas is the faster pass, and the ~6s client
            # startup is amortised across the batch either way.
            ndir, ntimes = os.path.join(td, 'ng'), {}
            if with_nogas:
                subprocess.run(base + ['--report-dir', ndir, '--no-gas'], env=env,
                               capture_output=True, text=True)
                for stem in by_stem:
                    f = os.path.join(ndir, stem + '.json')
                    if os.path.exists(f):
                        ntimes[stem] = round(json.load(open(f)).get('elapsed_secs', 0), 3)
            out = {}
            for stem, key in by_stem.items():
                f = os.path.join(rdir, stem + '.json')
                if os.path.exists(f):
                    j = json.load(open(f))
                    out[key] = {'work': j.get('cycles'), 'secs': round(j.get('elapsed_secs', 0), 3),
                                'pgu': j.get('gas'), **_sp1_extra(j)}
                    if stem in ntimes: out[key]['nsecs'] = ntimes[stem]
                    src = j.get('input')
                    if src and os.path.exists(src): out[key]['insz'] = os.path.getsize(src)
                else:
                    out[key] = {'error': 'no report from --batch (see sp1-runner output)'}
            return out
    return chunks, run_chunk

# `cache_xget` lived here: a lookup that retried a miss under every other axis, because the axis was
# part of the key while contributing nothing to the result — the same guest, ELF and block is the same
# execution wherever it ran. Removed 2026-08-10 with the move to per-block, content-addressed storage
# (cache-format.md): a block file holds one slot per BUILD, so there is no axis to borrow across and
# no write-through copy to make. The ~2 h of duplicated measurement in the 2026-08-08 campaign that
# motivated it cannot recur by construction rather than by fallback.


# ── Pure execution time ────────────────────────────────────────────────────────
# Measured 2026-08-09 on the devcore box (idle, sequential, min of 2 runs, 16 blocks for
# the ZisK guests / regression for the others). Wall-clock as collected is NOT
# execution time: ziskemu re-converts the ELF to ZisK ROM on every invocation
# (~0.6-1.7 s fixed per guest) and campaign runs are parallel, so the raw `secs`
# overstated a median block by ~10x. Time is modelled instead as
#   pure_secs = work / throughput
# with the per-guest throughput below; the linear fit's residuals were 0.7-1.3 %
# on the ZisK guests, so the model is tighter than run-to-run noise.
#
# These are EMULATOR throughputs on one machine, meant to make the time column
# honest and comparable BETWEEN GUESTS — not a claim about proving time, and
# not comparable to an ASM-backend or a different host.
# Pure execution rates, measured THE WAY THE RTP PIPELINE MEASURES: `ziskemu -e ELF -i INPUT -m`,
# reading the emulator's own `duration=` field (process_rom only — it excludes the ELF→ROM
# conversion that dominates a wall-clock run, 0.6-1.7 s depending on the guest). Calibrated on
# THIS host, sequentially, over 5 blocks spanning 30-360 M steps; residuals 1.4-2.8 %.
#
# The campaign's own per-block `duration=` values are collected under 4-way parallelism and run
# ~35 % slow (89 vs 136 M steps/s on the same block), which is why the reported column is modelled
# from these sequential rates instead of averaged from the cache.
PURE_MSTEPS_PER_S = {
    'monad-r3-zisk':         126.5,   # ziskemu duration=, sequential, this host
    'monad-sam-zisk':     148.5,
    'zisk-reth':         141.6,
    # SP1: same protocol, its own tool — sp1-runner --mode execute --no-gas (the gas pass is a
    # separate, slower estimation), sequential, same 5 blocks, cycles paired from the cache since
    # --no-gas does not populate the counter. Residuals 4.0-6.4 %. The rates differ per guest by
    # more than ZisK's do (95 vs 142 M cycles/s) because the guests' instruction mixes differ far
    # more here — reth's cycles are cheaper on average than the Monad guest's.
    'monad-r3-sp1':      94.7,
    'monad-sam-sp1': 119.5,
    'rsp':               142.0,
}


def pure_secs(guest, work):
    """Modelled pure execution seconds for `work` steps/cycles of `guest`.

    Falls back through build IDENTITY when the name is unknown. The table is keyed by name, but a
    build legitimately carries several: `monad-zisk` is a symlink to whichever generation is
    selected, and its bytes are those of that generation's canonical pair (`monad-sam-zisk` today).
    Keying only by name left those axes with an empty time column while the very same binary had a
    measured rate under another name — so resolve through the content-addressed index rather than
    ask anyone to keep alias rows in step.
    """
    thr = PURE_MSTEPS_PER_S.get(guest)
    if thr is None:
        thr = _throughput_by_identity(guest)
    if not thr or not work:
        return None
    return round(work / (thr * 1e6), 4)


_THR_ALIAS = {}


def _throughput_by_identity(guest):
    """Rate of any build sharing this one's sha256, or None."""
    if guest in _THR_ALIAS:
        return _THR_ALIAS[guest]
    rate = None
    try:
        c = load_cache()
        want = {i for i, _mt in c.builds_by_name(guest)}
        if want:
            for other, r in PURE_MSTEPS_PER_S.items():
                if {i for i, _mt in c.builds_by_name(other)} & want:
                    rate = r
                    break
    except Exception:
        rate = None
    _THR_ALIAS[guest] = rate
    return rate


def collect(axis, blocks, tools, cache, jobs, force, with_cost=False):
    """Run both sides over `blocks` (cached per block, per BUILD — see cache-format.md).
    -> {block: {a:…, b:…}}"""
    ax = AXES[axis]; backend = ax['backend']
    tool = tools[backend]
    ax_side_by_name = {ax[k]['name']: ax[k] for k in ('a', 'b')}
    todo = []
    for side_k in ('a', 'b'):
        side = ax[side_k]
        elf = rp(side['elf'])
        # Record the label BEFORE deciding whether anything needs measuring. Registration used to
        # happen only inside put(), so an axis served entirely from cache never wrote its name down —
        # and `builds.json` is what by-name lookups (levers.py) resolve through, so a fully-cached
        # build became invisible to them.
        cache.register(elf, side['name'], backend)
        for b in blocks:
            # The key is (elf, block, name) rather than a string: identity is derived from the ELF's
            # content by the cache, so the axis never enters it and two axes on one build share a slot.
            # The INPUT is passed too — a measurement is a function of (build, input), and an entry
            # made on a witness that has since been re-minted must be re-run, not served.
            key = (elf, b, side['name'])
            hit = cache.get(elf, b, _cachemod.RUN, inp=resolve_input(side, b))
            # Re-run when a cached entry predates a field we now collect. ZisK COST + its category
            # breakdown come from the same opt-in pass; SP1's precompile counts are free, so we
            # always want them.
            stale = False
            if hit is not None and 'error' not in hit:
                if backend == 'zisk' and with_cost:
                    stale = any(k not in hit for k in ('cost', 'cats', 'kec', 'ops', 'opsn', 'insz'))
                elif backend == 'sp1':
                    stale = any(k not in hit for k in ('sys', 'nsecs', 'cats', 'ops', 'insz'))
            if force or hit is None or stale:
                todo.append((key, side, elf, b))
    if todo:
        done = [0]
        def note(label):
            done[0] += 1
            print(f"\r  [{axis}] {done[0]}/{len(todo)} runs… ({label})".ljust(70), end='', flush=True)
        if backend == 'sp1' and not sp1_has_batch(tool):
            print(f"  [{axis}] note: this sp1-runner predates --batch (rebuild it to pay the ~6s "
                  f"startup once per process instead of once per block) — running one at a time")
            backend = 'sp1-single'
        if backend == 'sp1':
            chunks, run_chunk = sp1_batches(todo, tool, jobs, True)
            print(f"  [{axis}] {len(todo)} run(s) in {len(chunks)} batched process(es) "
                  f"— ~6s startup paid per process, not per block")
            with ThreadPoolExecutor(max(1, jobs)) as ex:
                for res in ex.map(run_chunk, chunks):
                    for (e, blk, nm), r in res.items():
                        cache.put(e, blk, _cachemod.RUN, r, name=nm, backend=ax['backend'],
                                  inp=resolve_input(ax_side_by_name[nm], blk))
                    note(f"batch of {len(res)}")
                    save_cache(cache)             # incremental: an interrupted sweep keeps its work
        else:
            if with_cost and backend == 'zisk':
                print(f"  [{axis}] collecting prover-work COST too — that needs a second, "
                      f"instrumented pass (~10x slower). Skip it with --quick.")
            def work(item):
                key, side, elf, b = item
                inp = resolve_input(side, b)
                r = (run_zisk(tool, elf, inp, side['src'], with_cost)
                     if backend == 'zisk' else run_sp1(tool, elf, inp, side['src']))
                return key, r, f"{side['name']} {b}"
            with ThreadPoolExecutor(max(1, jobs)) as ex:
                for i, (key, r, label) in enumerate(ex.map(work, todo)):
                    e, blk, nm = key
                    cache.put(e, blk, _cachemod.RUN, r, name=nm, backend=ax['backend'],
                              inp=resolve_input(ax_side_by_name[nm], blk))
                    note(label)
                    if i % 25 == 24: save_cache(cache)
        print()
        save_cache(cache)
    # Unconditional: with nothing measured, `_dirty` is empty and this writes only builds.json — and
    # only if a label was new. A read-only run must still be able to teach the index a name.
    save_cache(cache)
    rows = {}
    for b in blocks:
        row = {}
        for side_k in ('a', 'b'):
            side = ax[side_k]
            row[side_k] = cache.get(rp(side['elf']), b, _cachemod.RUN,
                                    inp=resolve_input(side, b)) or {}
        if 'work' in row['a'] and 'work' in row['b']: rows[b] = row
    # A block whose side errored never enters `rows`, so `n` shrinks with nothing said — only "every
    # run failed" was ever reported. A guest fed a witness format it cannot read lands here in bulk:
    # that is the loud half of the mismatch (the quiet half is a parse that completes and commits a
    # wrong root, which nothing here can see — verify roots before trusting an axis).
    dropped = [b for b in blocks if b not in rows]
    if dropped:
        why = {}
        for b in dropped:
            for k in ('a', 'b'):
                e = (cache.get(rp(ax[k]['elf']), b, _cachemod.RUN,
                               inp=resolve_input(ax[k], b)) or {}).get('error')
                if e:
                    why.setdefault(f"{ax[k]['name']}: {e.split(':')[0]}", []).append(b)
        print(f"  [{axis}] {len(dropped)} of {len(blocks)} block(s) dropped — NOT in the stats "
              f"below (n={len(rows)}):")
        for lab, bs in sorted(why.items(), key=lambda kv: -len(kv[1]))[:3]:
            print(f"      {len(bs)}x {lab}  (e.g. block {bs[0]})")
        if not why:
            print(f"      no error recorded — a side produced no work count. Re-run it with --force.")
    return rows

# ───────────────────────────────────── stats ────────────────────────────────────────────

def pct(xs, q):
    xs = sorted(xs)
    if not xs: return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]

def outliers(rows, z_thresh):
    """Blocks whose A/B ratio departs from the sample, by robust z-score (median + MAD).

    MAD instead of stdev because a handful of weird blocks would inflate stdev and mask
    themselves. 0.6745 rescales MAD to a stdev-equivalent, so |z|>=3.5 is the usual
    'really not like the others' bar. Returns (sorted_entries, median, mad)."""
    r = {b: v['a']['work'] / v['b']['work'] for b, v in rows.items() if v['b']['work']}
    if len(r) < 5: return [], None, None
    med = statistics.median(r.values())
    mad = statistics.median([abs(x - med) for x in r.values()])
    out = []
    for b, x in r.items():
        z = 0.6745 * (x - med) / mad if mad else 0.0
        if abs(z) >= z_thresh: out.append({'block': b, 'ratio': x, 'z': z})
    return sorted(out, key=lambda e: -abs(e['z'])), med, mad

def _plain(s):
    """Markup stripped, for use inside an attribute. Tooltips are plain text: a title= carrying
    <b>…</b> shows the tags. Quotes go too, since the attribute is quoted with them."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', s)).replace('"', "'").strip()


def _vclass(f):
    """How far a family's ratio travels when the reth guest's inlining is undone → one word.

    The word names WHAT HAPPENED BETWEEN the two attributions. It used to be a reliability verdict
    (`stable` / `shifted` / `unreliable`), and that could not survive its own placement: the word sat
    in the corrected cell while judging the SHIPPED ratio in the cell to its left, so `1.021x
    unreliable` read as "distrust this 1.021x" — the exact opposite of the finding. A reader asked
    which of the two numbers the word was about, which is the proof that no reader could tell.

    Naming displacement instead has no such ambiguity, because displacement is a property of the PAIR:
    `agrees` means both attributions found the same thing and either column can be read; `relocated`
    means inlining had charged this family's work elsewhere, so the corrected column is the one that
    says where the work lives.

    ONE function, because the cell and the paragraph under the table used to classify separately: the
    paragraph counted 0.8 < f < 1.25 as "holds" while the cell had started calling the same family
    borderline, so the prose said five held and the column showed three.

    `borderline` marks only the agrees/relocated boundary (0.8, 1.25) — the one where the word changes
    the reading. The 0.5 and 2 cutoffs separate two magnitudes of relocation, and a family sitting on
    THAT edge reads the same either way."""
    if min(abs(f - 0.8), abs(f - 1.25)) < 0.05: return 'borderline'
    if 0.8 < f < 1.25:                          return 'agrees'
    if 0.5 <= f <= 2:                           return 'relocated'
    return 'relocated-hard'   # beyond a factor of two: read the corrected column, not the shipped one


def _verdict_cell(v):
    """One cell: the same family's ratio once the reth guest's inlining is undone.

    Shown only where it was measured. A family whose ratio barely moves can be read straight from the
    column to its left; one that moves by more than a factor of two cannot, and the figure here is the
    better estimate — the shipped column is then reporting which guest's compiler inlined more."""
    if not v:
        return "<td class=na>—</td>"
    f = v['factor']
    # Colour by the RATIO, exactly like the column to the left — a cell showing 1.008x must not be red.
    # Colouring by the trustworthiness factor put red on three ratios BELOW 1 (Monad cheaper) and left
    # the table's worst ratio, 4.099x, uncoloured: two meanings sharing one palette in adjacent columns.
    # The verdict is a word instead, since both numbers are already on the row.
    cls = 'hi' if v['ni'] >= 1 else 'lo'
    # A family sitting on a class boundary must not be reported as decisively one side of it:
    # `256-bit arithmetic` moved 0.80 -> 0.81 between two runs of the same measurement and flipped
    # from "shifted" to "stable" on that alone. Name the boundary instead of hiding it.
    _c = _vclass(f)
    # The magnitude, so "relocated" is a measurement and not an adjective: how many times the ratio
    # moved between the two attributions.
    _mag = max(f, 1 / f) if f else 0
    note = (f"<b>relocated {_mag:.0f}×</b>" if _c == 'relocated-hard' else
            f"relocated {_mag:.1f}×" if _c == 'relocated' else _c)
    return (f"<td class={cls} title=\"This family's ratio under two attributions: {v['ship']:.2f}x as "
            f"shipped, {v['ni']:.2f}x with the reth guest's inlining undone, over {v['n']} blocks. "
            f"The word describes the MOVE between the two, not the quality of the number in this "
            f"cell — where it says relocated, this column is the one that says where the work "
            f"lives.\">{x(v['ni'])} <i class=dev>{note}</i></td>")


def _cost_time_axes(axis, rows):
    """Median per-block cost and pure-time ratios; empty when the axis carries no cost pass."""
    ax = AXES[axis]
    out = {}
    # Prover cost is NOT a ZisK-only idea: ZisK reports COST, SP1 reports PGU (its report's `gas`
    # field, which is prover gas and has nothing to do with EVM gas). Reading only `cost` left every
    # SP1 axis with an empty column and made the metric look ZisK-specific, which it is not — the two
    # are not comparable to each other, but each is the right prover-side number for its backend.
    pwk = 'cost' if AXES[axis]['backend'] == 'zisk' else 'pgu'
    out['cost_unit'] = 'COST' if pwk == 'cost' else 'PGU'
    cr = [r['a'][pwk] / r['b'][pwk] for r in rows.values()
          if r['a'].get(pwk) and r['b'].get(pwk)]
    if cr:
        out['cost_ratio_median'] = statistics.median(cr)
        out['cost_n'] = len(cr)
    tr = []
    for r in rows.values():
        ta = pure_secs(ax['a']['name'], r['a'].get('work'))
        tb = pure_secs(ax['b']['name'], r['b'].get('work'))
        if ta and tb:
            tr.append(ta / tb)
    if tr:
        out['time_ratio_median'] = statistics.median(tr)
    return out


_IDENT_CACHE = None


def _ident_of(elf):
    """sha256 identity of an ELF, or None if it is absent. The same value the profile cache keys on.

    One Cache instance, kept: it memoises the hash per (path, mtime), so asking for the same build on
    several axes costs one read of the ELF, not one per axis."""
    global _IDENT_CACHE
    try:
        if _IDENT_CACHE is None:
            _IDENT_CACHE = _cachemod.Cache()
        return _IDENT_CACHE.identity(rp(elf))
    except Exception:
        return None


def summarize(axis, rows, gas_map=None):
    ax = AXES[axis]
    aw = [r['a']['work'] for r in rows.values()]; bw = [r['b']['work'] for r in rows.values()]
    ratios = [r['a']['work'] / r['b']['work'] for r in rows.values() if r['b']['work']]
    # EVM gas is a property of the BLOCK, not of the axis — only the reth ZisK guest prints it,
    # so gas_map (pooled across axes) lets the SP1 axis show work/Mgas too.
    gas = {b: (r['b'].get('gas') or r['a'].get('gas') or (gas_map or {}).get(b))
           for b, r in rows.items()}
    # The identity of what was MEASURED, not what was declared. A name is a label and an ELF path is a
    # location; neither says which binary produced these numbers, and a guest under active work is
    # rebuilt over its own path repeatedly. Derived here rather than declared in AXES: a declaration
    # has to be edited by hand after every rebuild and goes stale in silence, whereas this cannot
    # disagree with the run it describes.
    s = {'axis': axis, 'unit': ax['unit'], 'n': len(rows),
         'a_name': ax['a']['name'], 'b_name': ax['b']['name'],
         'a_ident': _ident_of(ax['a']['elf']), 'b_ident': _ident_of(ax['b']['elf']),
         'a_median': statistics.median(aw), 'b_median': statistics.median(bw),
         'a_mean': statistics.mean(aw), 'b_mean': statistics.mean(bw),
         'a_total': sum(aw), 'b_total': sum(bw),
         'ratio_median': statistics.median(ratios), 'ratio_mean': statistics.mean(ratios),
         # THREE AXES. work (steps/cycles) is the verdict metric: deterministic and host-
         # independent. cost is what the PROVER pays -- ZisK's COST model or SP1's PGU, per
         # backend, same collection pass -- and it diverges from work when the MIX changes, which
         # does. time is modelled from measured per-guest throughput (PURE_MSTEPS_PER_S) and
         # moves with mix density through an entirely different mechanism, so cost/time agreement
         # is a real confirmation and a divergence is a lead worth chasing.
         **_cost_time_axes(axis, rows),
         # The geometric mean is the average that is CORRECT for ratios (Fleming & Wallace 1986):
         # it treats 2x and 0.5x as cancelling, and gmean(A/B) = 1/gmean(B/A), neither of which
         # holds for the arithmetic mean — mean(A/B) and mean(B/A) can both sit above 1. Kept
         # alongside the median (robust, the headline) and the arithmetic mean (kept for
         # continuity with older reports), not instead of them.
         'ratio_gmean': statistics.geometric_mean(ratios),
         # The SAME arithmetic mean taken with the guests swapped. Its only job is to make the
         # arithmetic mean's inconsistency checkable instead of asserted: gmean(A/B)*gmean(B/A) is
         # exactly 1, while mean(A/B)*mean(B/A) >= 1 by AM-GM and is strictly above it here. The
         # textbook statement of the flaw ("both means can exceed 1, each calling the other guest
         # dearer") is TRUE of the statistic but does NOT happen on this data — measured — so the
         # page quotes this product rather than a warning a reader could check and find overstated.
         'ratio_mean_inv': statistics.mean([1 / r for r in ratios]),
         'ratio_pooled': sum(aw) / sum(bw),
         'ratio_p10': pct(ratios, .10), 'ratio_p90': pct(ratios, .90),
         'ratio_min': min(ratios), 'ratio_max': max(ratios),
         'cv': (statistics.pstdev(ratios) / statistics.mean(ratios) * 100) if len(ratios) > 1 else 0.0,
         # The dispersion that MATCHES a geometric mean: exp(sd of the logs), a MULTIPLICATIVE
         # spread. Read it as ×/÷, not ±: the one-sigma band is gmean×gsd and gmean÷gsd. `cv` is
         # the additive counterpart and stays for the card that already shows it — quoting a ±%
         # beside a geometric mean would mix the two conventions.
         'ratio_gsd': (math.exp(statistics.pstdev([math.log(r) for r in ratios]))
                       if len(ratios) > 1 else 1.0)}
    inv = {r['a']['work'] / r['b']['work']: b for b, r in rows.items() if r['b']['work']}
    s['block_min'], s['block_max'] = inv[s['ratio_min']], inv[s['ratio_max']]
    # Two regimes, not one distribution. On ~22% of blocks `rsp` runs the BN254 pairing precompile in
    # PURE SOFTWARE (substrate_bn::U256::mul = 40% of its work on those blocks, vs 6% elsewhere), while
    # monad-sp1 spends 2.4%, zisk-reth 1.9% and monad-zisk 1.0% — so rsp, not the others, is the
    # outlier. That single gap is what makes rsp *more expensive than Monad* on 68 blocks and drags the
    # SP1 mean (1.16x) below its median (1.22x). Reporting one median silently averages an engine
    # comparison with an rsp precompile gap, so both are computed.
    #
    # Detector: multiplication intensity (mul/work) above CURVE_Z x the axis median, for either guest.
    # Multiplications are the signature of software 256-bit curve arithmetic and `mul` is counted for
    # every block, so this needs no extra profiling. Calibrated on this data: at 2.5x it flags 80
    # blocks and catches 100% of the 68 where the ratio flips, plus 12 pairing-heavy blocks where rsp
    # stays ahead anyway — which is the physically meaningful population, not just the flips.
    CURVE_Z = 2.5
    if all((r[k].get('ops') or {}).get('mul') is not None and r[k].get('work')
           for r in rows.values() for k in 'ab'):
        it = {k: {b: r[k]['ops']['mul'] / r[k]['work'] for b, r in rows.items()} for k in 'ab'}
        m = {k: statistics.median(it[k].values()) for k in 'ab'}
        flag = {b for b in rows if any(it[k][b] > CURVE_Z * m[k] for k in 'ab')}
        rat = lambda bs: [rows[b]['a']['work'] / rows[b]['b']['work']
                          for b in bs if rows[b]['b']['work']]
        clean = rat(set(rows) - flag)
        if flag and clean:
            s['curve'] = {'n': len(flag), 'z': CURVE_Z,
                          'med_flag': statistics.median(rat(flag)),
                          'med_clean': statistics.median(clean),
                          'blocks': sorted(int(b) for b in flag)}
    # Representative REAL blocks at the median / p10 / p90 of the ratio (the "medoid" and the
    # quantile blocks). Deliberately not a synthetic median/decile profile: a flamegraph whose
    # every function carries its own cross-block median sums to no real block's total and matches
    # no execution that ever ran. A real block at the quantile is honest and directly openable.
    byratio = sorted(((r['a']['work'] / r['b']['work'], b) for b, r in rows.items()
                      if r['b']['work']))
    at_q = lambda q: byratio[min(len(byratio) - 1, max(0, int(round(q * (len(byratio) - 1)))))]
    s['block_median'] = at_q(.50)[1]                 # typical block
    s['block_p10'], s['block_p90'] = at_q(.10)[1], at_q(.90)[1]
    s['ratio_at_median'], s['ratio_at_p10'], s['ratio_at_p90'] = \
        at_q(.50)[0], at_q(.10)[0], at_q(.90)[0]
    # Prover-work proxy: ZisK reports COST, SP1 reports PGU (prover gas). Different models and
    # scales — NOT comparable between VMs — but each estimates proving cost rather than raw
    # instruction count, so the A/B ratio here tracks what proving will cost better than the
    # steps/cycles ratio does.
    pw = 'cost' if ax['backend'] == 'zisk' else 'pgu'
    apw = [r['a'][pw] for r in rows.values() if r['a'].get(pw)]
    bpw = [r['b'][pw] for r in rows.values() if r['b'].get(pw)]
    if apw and bpw and len(apw) == len(bpw):
        s['pw_unit'] = 'COST' if pw == 'cost' else 'PGU'
        s['a_pw_median'], s['b_pw_median'] = statistics.median(apw), statistics.median(bpw)
        pwr = [r['a'][pw] / r['b'][pw] for r in rows.values()
               if r['a'].get(pw) and r['b'].get(pw)]
        if pwr: s['pw_ratio_median'] = statistics.median(pwr)
    # ── where the gap comes from ──────────────────────────────────────────────────────────
    # Per-block shares, then the MEDIAN of those — not the aggregate (big blocks would dominate)
    # and not the median-ratio block alone (its ratio is typical, its composition need not be:
    # measured 11.2% vs a true 8.8% on the precompile row).
    shares, opr, nrat, ncnt, nvol = {}, {}, {}, {}, {}
    for r in rows.values():
        ca, cb = r['a'].get('cats'), r['b'].get('cats')
        if ca and cb:
            g = sum(ca.values()) - sum(cb.values())
            if g:
                for k in set(ca) | set(cb):
                    shares.setdefault(k, []).append(100 * (ca.get(k, 0) - cb.get(k, 0)) / g)
        # Instruction COUNTS where the backend reports them (ZisK: the OPS column; SP1: its opcode
        # groups are already counts). This is what "which part runs more instructions" needs — cost
        # is not proportional to it.
        na, nb = r['a'].get('opsn') or r['a'].get('ops'), r['b'].get('opsn') or r['b'].get('ops')
        if na and nb and (r['a'].get('opsn') or AXES[axis]['backend'] == 'sp1'):
            for k in set(na) & set(nb):
                if nb[k]:
                    nrat.setdefault(k, []).append(na[k] / nb[k])
                    # absolute counts too: the ratio says "more", these say "how much" — which is
                    # the point of asking what kind of work a guest does
                    ncnt.setdefault(k, ([], []))[0].append(na[k])
                    ncnt[k][1].append(nb[k])
                    # VOLUME as well as ratio. A ratio with no volume beside it invites chasing
                    # noise: `divrem` reads 4.8x here on 0.012% of cycles, and sorting the table by
                    # ratio puts it near the top. One column makes it self-disqualifying instead of
                    # needing a hand-written warning per case.
                    _tn = sum(na.values())
                    if _tn:
                        nvol.setdefault(k, []).append(na[k] / _tn)
        oa, ob = r['a'].get('ops'), r['b'].get('ops')
        if oa and ob:
            # Only when BOTH sides list the operation. ziskemu prints a TRUNCATED per-opcode table
            # (top entries only), so a missing name means "not in the top", not "zero" — counting
            # absence as zero manufactured infinities and pushed the most interesting rows out.
            for k in set(oa) & set(ob):
                if ob[k]: opr.setdefault(k, []).append(oa[k] / ob[k])
    # Per-category A/B ratio alongside the share. The share alone is misleading: `Main` is the
    # work-unit count re-expressed in cost units (exactly 68 x steps on ZisK), so its share only
    # restates the headline ratio. The ratio column is what carries information — which parts grow
    # FASTER or SLOWER than the instruction count, and hence why the cost ratio differs from it.
    crat = {}
    for r in rows.values():
        ca, cb = r['a'].get('cats'), r['b'].get('cats')
        if ca and cb:
            for k in set(ca) & set(cb):
                if cb.get(k): crat.setdefault(k, []).append(ca[k] / cb[k])
    if shares:
        s['gap_split'] = sorted(((k, statistics.median(v),
                                  statistics.median(crat[k]) if crat.get(k) else None)
                                 for k, v in shares.items()), key=lambda t: -t[1])
    # ── the same categories the cost model uses, but counted in INSTRUCTIONS ────────────────
    # Where each count comes from (a category is a cost bucket, so the instruction that fills it
    # has to be named per backend):
    #   all instructions  steps / cycles — every instruction passes through the main state machine
    #   arithmetic+logic  ZisK: the ops the OPS column reports (add/and/or/xor/sll/srl/eq/lt/…)
    #                     SP1 : its opcode groups minus memory (branch + shift + mul + divrem)
    #   precompile calls  ZisK: keccak, recovered from its cost (the only constant verified exact,
    #                           40/40 divisions) — ZisK publishes no call count for precompiles
    #                     SP1 : total_syscalls, reported directly
    #   memory            SP1 : load/store count. ZisK publishes none (dma_* have no OPS figure).
    zisk = AXES[axis]['backend'] == 'zisk'
    def _icats(r):
        o, sy = r.get('opsn') or {}, r.get('sys') or {}
        g = r.get('ops') or {}
        if zisk:
            return {'all instructions': r.get('work'),
                    'arithmetic + logic': sum(o.values()) or None,
                    'keccak permutations': r.get('kec'),
                    'memory': None}
        return {'all instructions': r.get('work'),
                'arithmetic + logic': (sum(g.get(k, 0) for k in ('branch','shift','mul','divrem'))
                                       or None),
                'precompile calls': r.get('tsys'),
                'memory': g.get('mem')}
    icat = {}
    for r in rows.values():
        ia, ib = _icats(r['a']), _icats(r['b'])
        for k in ia:
            if ia[k] and ib[k]: icat.setdefault(k, ([], []))[0].append(ia[k]); icat[k][1].append(ib[k])
    if icat:
        s['insn_cats'] = [(k, statistics.median(v[0]), statistics.median(v[1]),
                           statistics.median([x / y for x, y in zip(v[0], v[1]) if y]))
                          for k, v in icat.items()]
    need = 0.5 * len(rows)          # seen on most blocks, so a one-off doesn't headline the table
    if opr:
        s['op_ratios'] = sorted(((k, statistics.median(v), len(v)) for k, v in opr.items()
                                 if len(v) >= need), key=lambda t: -t[1])
    # What the per-opcode counter covers, relative to the work unit. On SP1 `ops` are genuine
    # instruction counts and sum to ~54% of cycles (only some opcodes are grouped). On ZisK `opsn`
    # sums to ~22x the STEP count — it is a per-opcode counter on an unrelated scale, so its
    # magnitudes are NOT instruction counts and must not be labelled as such. Guest-to-guest ratios
    # stay valid either way (same counter on both sides); absolute magnitudes do not.
    _cov = [sum((r['a'].get('opsn') or r['a'].get('ops') or {}).values()) / r['a']['work']
            for r in rows.values() if r['a'].get('work')]
    if _cov: s['ops_coverage'] = statistics.median(_cov)
    if nrat:
        # (op, ratio, blocks, median count A, median count B, median share of the counted ops)
        s['insn_ratios'] = sorted(
            ((k, statistics.median(v), len(v),
              statistics.median(ncnt[k][0]) if ncnt.get(k) else None,
              statistics.median(ncnt[k][1]) if ncnt.get(k) else None,
              statistics.median(nvol[k]) if nvol.get(k) else None)
             for k, v in nrat.items() if len(v) >= need), key=lambda t: -t[1])
    # Pure (modelled) time, not the collected wall-clock — see PURE_MSTEPS_PER_S.
    at = [pure_secs(ax['a']['name'], r['a'].get('work')) for r in rows.values()
          if r['a'].get('work')]
    bt = [pure_secs(ax['b']['name'], r['b'].get('work')) for r in rows.values()
          if r['b'].get('work')]
    at = [t for t in at if t]; bt = [t for t in bt if t]
    if at and bt: s['a_secs_median'], s['b_secs_median'] = statistics.median(at), statistics.median(bt)
    # SP1 only: time from the --no-gas pass — execution without the gas-estimation overhead, i.e.
    # the honest emulation cost. Cycles still come from the gas-on pass (--no-gas reports 0).
    # nsecs (SP1's gas-pass-off wall-clock) is superseded by the same pure model as `secs`: it
    # still carried process startup and campaign parallelism. The key stays so the cards and the
    # JSON schema keep their shape; the VALUE is now work / measured per-guest throughput.
    ant = [pure_secs(ax['a']['name'], r['a'].get('work')) for r in rows.values()
           if r['a'].get('nsecs') and r['a'].get('work')]
    bnt = [pure_secs(ax['b']['name'], r['b'].get('work')) for r in rows.values()
           if r['b'].get('nsecs') and r['b'].get('work')]
    ant = [t for t in ant if t]; bnt = [t for t in bnt if t]
    if ant and bnt:
        s['a_nsecs_median'], s['b_nsecs_median'] = statistics.median(ant), statistics.median(bnt)
        if at and bt:                     # what the gas pass costs, measured
            s['gas_pass_overhead'] = statistics.median(at) / statistics.median(ant)
    # work per Mgas — normalises for block size (EVM gas comes from the reth ZisK guest)
    ag = [r['a']['work'] / (gas[b] / 1e6) for b, r in rows.items() if gas.get(b)]
    bg = [r['b']['work'] / (gas[b] / 1e6) for b, r in rows.items() if gas.get(b)]
    if ag and bg:
        s['a_per_mgas'], s['b_per_mgas'] = statistics.median(ag), statistics.median(bg)
        s['gas_median'] = statistics.median([g for g in gas.values() if g])
    # does the gap grow with block size? split blocks at the median gas
    sized = sorted(((gas[b], r['a']['work'] / r['b']['work']) for b, r in rows.items()
                    if gas.get(b) and r['b']['work']), key=lambda t: t[0])
    if len(sized) >= 6:
        h = len(sized) // 2
        s['ratio_small'] = statistics.median([r for _, r in sized[:h]])
        s['ratio_large'] = statistics.median([r for _, r in sized[-h:]])
    return s

# ─────────────────────────────────── rendering ──────────────────────────────────────────

# One line per backend for the report. The provenance behind these, kept here rather than in the
# rendered page (it's implementation detail a reader of the numbers doesn't need):
#
#   ZisK COST — zisk `core/src/zisk_ops_costs.rs`, whose header reads "Cost definitions: Area x Op".
#       Each op contributes its state-machine columns × rows, e.g. KECCAK_COST = 25 * 3022,
#       SHA256_COST = 72 * 121. Reported by `ziskemu -X --stats` (NOT by `-m`).
#   SP1 PGU  — `sp1-core-executor vm/shapes.rs`: the ShapeChecker, "tracking trace area and
#       determining shard boundaries", accumulates `costs[air_id]` per instruction / memory event /
#       precompile; `report.rs::gas()` then returns it ×10/191 (GAS_NORMALIZATION_FACTOR), so the
#       figure stays comparable across SP1 versions. Same quantity hotspots.py computes from
#       rv64im_costs.json, which is why the two agree to ~2%.
#   Both are therefore trace area. PGU is the unit SP1's prover network prices proofs in, but it is
#   hardware-agnostic — the same area holds whether you prove on CPU or GPU.
_PW_DOC = {
    'COST': "<b>COST</b> is ZisK's own per-operation cost model: each op contributes its "
            "state-machine columns × rows.",
    'PGU':  "<b>PGU</b> (SP1 prover gas) is the trace area SP1's executor accumulates as it runs; it "
            "also drives how the proof is sharded.",
}

# Hover text for each trace part, so the table stands alone.
_PART_DOC = {
    'Main':        " title='Per-instruction backbone: one row in the main state machine for every "
                   "executed instruction, whatever it is. Proportional to the instruction count.'",
    'Opcodes':     " title='The extra cost on top of Main that depends on WHICH operation ran — an "
                   "AND does not cost the same as a shift.'",
    'Precompiles': " title='Built-in cryptographic operations (keccak, secp256k1, sha256…), each "
                   "priced as its own circuit.'",
    'Memory':      " title='Memory access and alignment circuits.'",
    'Base':        " title='Fixed setup cost, identical for both guests.'",
}

def n(v):  return f"{v:,.0f}" if isinstance(v, (int, float)) else "—"
def x(v):  return f"{v:.3f}×" if isinstance(v, (int, float)) else "—"

def print_summary(s):
    u, A, B = s['unit'], s['a_name'], s['b_name']
    print(f"\n══ {s['axis'].upper()} · {A} vs {B} · n={s['n']} blocks ══")
    print(f"  {'':22} {A:>18} {B:>18} {'ratio':>10}")
    print(f"  {'median '+u:22} {n(s['a_median']):>18} {n(s['b_median']):>18} "
          f"{x(s['a_median']/s['b_median']):>10}")
    print(f"  {'mean '+u:22} {n(s['a_mean']):>18} {n(s['b_mean']):>18} "
          f"{x(s['a_mean']/s['b_mean']):>10}")
    print(f"  {'total '+u:22} {n(s['a_total']):>18} {n(s['b_total']):>18} {x(s['ratio_pooled']):>10}")
    if 'a_per_mgas' in s:
        print(f"  {u+'/Mgas (median)':22} {n(s['a_per_mgas']):>18} {n(s['b_per_mgas']):>18} "
              f"{x(s['a_per_mgas']/s['b_per_mgas']):>10}")
    if 'pw_unit' in s:
        # This row printed the MEDIAN OF PER-BLOCK RATIOS while every other row in the table
        # prints the quotient of the two medians it displays — so the third column was not the
        # first divided by the second (1.361x next to 39.4G/28.6G = 1.376x), and it contradicted
        # the same row in the HTML. Aggregate/aggregate here; the per-block median is printed
        # below, where median-of-ratios belongs.
        print(f"  {'median '+s['pw_unit']+' (prover)':22} {n(s['a_pw_median']):>18} "
              f"{n(s['b_pw_median']):>18} "
              f"{x(s['a_pw_median']/s['b_pw_median'] if s.get('b_pw_median') else None):>10}")
    # NOTE: the time rows are MODELLED (work / measured per-guest throughput, see
    # PURE_MSTEPS_PER_S) — emulator seconds on the reference host, not proving time.
    if 'a_nsecs_median' in s:      # honest time (gas-estimation pass off)
        print(f"  {'pure exec secs (med)':22} {s['a_nsecs_median']:>18.3f} {s['b_nsecs_median']:>18.3f} "
              f"{x(s['a_nsecs_median']/max(s['b_nsecs_median'],1e-9)):>10}")
        if s.get('gas_pass_overhead'):
            print(f"  {'':22} {'(gas-estimation pass would add ×%.2f)' % s['gas_pass_overhead']:>39}")
    elif 'a_secs_median' in s:
        print(f"  {'pure exec secs (med)':22} {s['a_secs_median']:>18.3f} {s['b_secs_median']:>18.3f} "
              f"{x(s['a_secs_median']/max(s['b_secs_median'],1e-9)):>10}")
    print(f"\n  per-block ratio  median {x(s['ratio_median'])}  geo-mean {x(s['ratio_gmean'])}  "
          f"mean {x(s['ratio_mean'])}  p10 {x(s['ratio_p10'])}  p90 {x(s['ratio_p90'])}  "
          f"cv {s['cv']:.1f}%")
    if s.get('pw_ratio_median'):
        print(f"  per-block {s['pw_unit']:6} median {x(s['pw_ratio_median'])}"
              f"   (median of per-block ratios — not the quotient of the two medians above)")
    print(f"  spread           min {x(s['ratio_min'])} @{s['block_min']}   "
          f"max {x(s['ratio_max'])} @{s['block_max']}")
    if 'ratio_small' in s:
        print(f"  by block size    small blocks {x(s['ratio_small'])}   large blocks {x(s['ratio_large'])}"
              f"   (split at median gas {n(s['gas_median'])})")
    verdict = "more" if s['ratio_median'] > 1 else "less"
    print(f"\n  → {A} costs {abs(s['ratio_median']-1)*100:.1f}% {verdict} {u} than {B} "
          f"(median block, same zkVM)")

def print_outliers(s, rows, entries, mad, show, gas_map=None):
    """Blocks that don't behave like the rest — the ones worth opening in hotspots.py."""
    u, A, B = s['unit'], s['a_name'], s['b_name']
    gm = gas_map or {}
    gas_of = lambda b, r: r['b'].get('gas') or r['a'].get('gas') or gm.get(b)
    if entries:
        print(f"\n  ⚠ {len(entries)} outlier block(s) — ratio far from the sample "
              f"(robust z ≥ {s['z_thresh']}, MAD {mad:.4f}):")
        print(f"      {'block':>10} {'ratio':>9} {'z':>7} {'gas':>12} {A+' '+u:>18} {B+' '+u:>18}")
        for e in entries[:show]:
            r = rows[e['block']]
            print(f"      {e['block']:>10} {e['ratio']:>8.3f}× {e['z']:>+7.1f} "
                  f"{n(gas_of(e['block'], r)):>12} {n(r['a']['work']):>18} {n(r['b']['work']):>18}")
        if len(entries) > show: print(f"      … and {len(entries)-show} more (see --json)")
        print(f"      → inspect one: ./hotspots.py profile --backend {AXES[s['axis']]['backend']} "
              f"--elf ../{AXES[s['axis']]['a']['elf']} -i <input> --out results/x")
    else:
        print(f"\n  no outlier beyond robust z {s['z_thresh']} — the sample is homogeneous")
    ext = sorted(rows, key=lambda b: rows[b]['a']['work'] / max(rows[b]['b']['work'], 1))
    lo, hi = ext[:3], ext[-3:][::-1]
    fmt = lambda bs: "  ".join(f"{b}@{rows[b]['a']['work']/rows[b]['b']['work']:.3f}×" for b in bs)
    print(f"  extremes         cheapest for {A}: {fmt(lo)}")
    print(f"                   costliest for {A}: {fmt(hi)}")
    print(f"  representative   typical(median) {s['block_median']}@{x(s['ratio_at_median'])}   "
          f"p10 {s['block_p10']}@{x(s['ratio_at_p10'])}   p90 {s['block_p90']}@{x(s['ratio_at_p90'])}")
    print(f"      → real blocks at those quantiles, not a synthetic 'median profile' (a per-function "
          f"median matches no run). Profile the typical one, or see what makes a block expensive:")
    print(f"        ./compare.py --axis {s['axis']} --spread")

_CSS = """
:root{--ink:#0a0c12;--panel:#11141e;--panel2:#161b28;--line:#242b3c;--fg:#e7eaf2;--muted:#8b93a7;
 --dim:#59627a;--gold:#e8b04b;--blue:#5b8def;--red:#e2564a;--green:#4bbf8a;
 /* highlight hue, deliberately NOT gold: gold identifies guest A everywhere, so using
    it for emphasis too made every highlighted figure read as "a Monad number". */
 --accent:#b09cf7;--accent-dim:#9d92c9;
 --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
 --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--fg)}
.wrap{max-width:1180px;margin:0 auto;padding:34px 26px 60px;color:var(--fg);font-family:var(--sans);
 background-image:radial-gradient(900px 380px at 82% -8%,rgba(91,141,239,.10),transparent 60%)}
.eyebrow{font-size:11px;letter-spacing:.20em;text-transform:uppercase;color:var(--accent);
 font-family:var(--mono);font-weight:600;margin:0 0 10px}
h1{font-size:clamp(26px,4vw,40px);line-height:1.04;margin:0 0 10px;font-weight:680;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:14px;margin:0;max-width:74ch}
.sub code,code{font-family:var(--mono);color:#c3ccdf;font-size:12.5px}
h2{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 0 14px;
 font-family:var(--mono);font-weight:600}
section{margin-top:38px;border-top:1px solid var(--line);padding-top:26px}
/* Sticky axis bar. The page carries several comparisons, each hundreds of pixels tall; without a
   persistent marker a reader deep in a table cannot tell WHICH pair, on which zkVM, they are
   looking at. It restates exactly that, and doubles as the jump nav between axes. */
.axbar{position:sticky;top:0;z-index:50;margin:0 -22px 18px;padding:9px 22px;
 background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(9px);
 border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.axbar .cur{font-family:var(--mono);font-size:13px;font-weight:650;white-space:nowrap}
.axbar .cur .bk{color:var(--accent);margin-right:7px}
.axbar .cur .pair{color:var(--dim);font-weight:400}
.axbar .jump{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
.axbar .jump a{font-family:var(--mono);font-size:11px;color:var(--dim);text-decoration:none;
 padding:3px 8px;border:1px solid var(--line);border-radius:999px;white-space:nowrap}
.axbar .jump a:hover{color:var(--fg);border-color:var(--dim)}
.axbar .jump a.on{color:var(--fg);border-color:var(--accent);background:var(--panel)}
@media print{.axbar{position:static;backdrop-filter:none}}
.axhead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:18px;scroll-margin-top:60px}
.axhead .nm{font-family:var(--mono);font-size:20px;font-weight:650;letter-spacing:-.01em}
.axhead .vs{color:var(--dim);font-size:13px;font-family:var(--mono)}
/* separators via card borders, not a gap over a coloured backdrop, so a half-empty last row
   (narrow viewport) shows plain panel instead of a stray dark cell */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:var(--panel);
 border:1px solid var(--line);border-radius:13px;overflow:hidden;margin-bottom:20px}
.card{background:var(--panel);padding:14px 16px;border-right:1px solid var(--line);
 border-bottom:1px solid var(--line)}
.card .k{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);font-family:var(--mono)}
.card .v{font-family:var(--mono);font-size:22px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums;
 letter-spacing:-.02em}
.card .u{font-size:11px;color:var(--muted);margin-left:3px;font-weight:400}
.card .u.blk{display:block;margin:5px 0 0;line-height:1.45}
.card.hero{background:var(--panel2)}.card.hero .v{font-size:30px;color:var(--accent)}
/* symmetric two-value display: same size, colour-coded to the two guests */
.duo{font-family:var(--mono);font-size:17px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
.duo .sep{color:var(--dim);font-size:12px;font-weight:400;margin:0 5px}
.cA{color:var(--gold)}.cB{color:var(--blue)}
.sw{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:5px;vertical-align:baseline}
.legend{display:flex;gap:16px;font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:0 0 16px}
.grid2{display:grid;grid-template-columns:1.1fr 1fr;gap:18px;margin-bottom:20px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.pane{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:17px 18px;margin-bottom:18px}
.insight{background:linear-gradient(180deg,rgba(232,176,75,.10),rgba(232,176,75,.03));
 border:1px solid rgba(232,176,75,.28);border-radius:13px;padding:15px 17px;margin-bottom:20px;font-size:14px}
.insight b{color:var(--accent)}.insight b.cA{color:var(--gold)}.insight b.cB{color:var(--blue)}
.env{border-left:2px solid var(--accent-dim);background:rgba(176,156,247,.06);border-radius:0 8px 8px 0;
 padding:11px 13px;margin:14px 0 0;font-size:12.5px;line-height:1.65;color:var(--fg)}
.env b{color:var(--accent)}.env .cA{color:var(--gold)}.env .cB{color:var(--blue)}
.env em{color:var(--muted);font-style:normal}
.env code{font-family:var(--mono);font-size:11px;color:var(--accent)}
table.cv{width:100%;border-collapse:collapse;margin:8px 0 0;font-size:12.5px}
table.cv th{text-align:left;font-weight:500;color:var(--muted);font-size:11px;
 text-transform:uppercase;letter-spacing:.08em;padding:4px 8px;border-bottom:1px solid var(--line)}
table.cv td{padding:5px 8px;border-bottom:1px solid var(--line)}
table.cv td:nth-child(2),table.cv td:nth-child(3){text-align:right;font-family:var(--mono)}
table.cv td i{color:var(--muted);font-style:normal;font-size:11.5px}
/* histogram */
.hist{display:flex;align-items:flex-end;gap:2px;height:132px;margin:6px 0 0;position:relative}
.one{position:absolute;top:0;bottom:0;width:0;border-left:1px dashed rgba(231,234,242,.45)}
.one span{position:absolute;top:-2px;left:4px;font-family:var(--mono);font-size:10px;color:var(--muted)}
/* green/red here, NOT the gold/blue used for the two guests: these bars mean "cheaper/dearer
   than parity", not "guest A / guest B" — reusing the guest palette would read as a guest. */
.hb{flex:1;background:linear-gradient(180deg,var(--green),rgba(75,191,138,.35));border-radius:2px 2px 0 0;
 min-height:1px;position:relative}
.hb.over{background:linear-gradient(180deg,var(--red),rgba(226,86,74,.3))}
.hb{appearance:none;padding:0;border:0;font:inherit;cursor:pointer}
.hb:hover{outline:1px solid var(--fg)}
.hb:focus-visible{outline:2px solid var(--gold)}
.hb.sel{outline:1.5px solid var(--fg);filter:brightness(1.3)}
/* drill-down panel under a histogram */
.hp{margin-top:14px;border-top:1px solid var(--line);padding-top:12px;display:none}
.hp.on{display:block}
.hp .hpk{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.hp .hpk button{appearance:none;background:none;border:0;color:var(--dim);cursor:pointer;font:inherit;
  text-transform:none;letter-spacing:0}
.hp .hpk button:hover{color:var(--fg)}
.hp .syn{font-size:12.5px;color:var(--fg);margin:8px 0 10px;line-height:1.55}
.hp table{margin:0;font-size:11.5px}
.hp th,.hp td{padding:.22rem .45rem}
/* `.scroll` is emitted by _table wherever that builder is used, but was only ever STYLED under
   `.hp` — so outside the drill-down the div did nothing and its wide table spilled straight out of
   the rounded card (the off-pattern block table, ~20 columns). The container is generic now; only
   the height cap stays specific to the drill-down, which is the one that can run 80 rows. */
/* `.scroll` is emitted by _table wherever that builder is used but was only ever STYLED under `.hp`,
   so outside the drill-down the div did nothing and its table spilled out of the rounded card — and
   gave the whole page a horizontal scrollbar. Height is capped as well as width, for two reasons
   beyond length: it puts the horizontal bar inside the viewport (at the foot of a 950px-tall box it
   is off-screen while you read the top rows), and vertical scrolling is what keeps the sticky header
   alive — sticky pins to the nearest scrollport, so `overflow-x` alone silently kills it. */
/* 100vh, not a tighter cap: the height limit must bite only when the table is taller than the screen,
   since that is exactly when its horizontal bar would otherwise be unreachable. Anything shorter
   renders whole and its bar comes into view on a normal page scroll. Measured at 90vh, a 20-row /
   839px table inside an 863px viewport was capped for no benefit — it fit. */
.scroll{overflow:auto;max-height:100vh}
.scroll table{min-width:100%;white-space:nowrap}
.hp .scroll{max-height:300px}
/* Clipping alone gives no affordance: macOS overlay scrollbars are invisible at rest, so a cut-off
   column reads as a truncated number rather than as "there is more to the right". A permanent thin
   bar says it instead — preferred over a fade, which would dim real digits. */
.scroll::-webkit-scrollbar{height:9px;width:9px}
.scroll::-webkit-scrollbar-track{background:transparent}
.scroll::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
.scroll::-webkit-scrollbar-thumb:hover{background:var(--dim)}
/* Chrome >=121 ignores ::-webkit-scrollbar entirely once scrollbar-width/-color are set (measured:
   the 9px gutter collapses to 0), so the standard properties go only to engines without the
   pseudo-element — Firefox — instead of silently disabling the rules above. */
@supports not selector(::-webkit-scrollbar){
 .scroll{scrollbar-color:var(--line) transparent;scrollbar-width:thin}
}
/* Header row stays put while the body scrolls, so you can still tell which column you are in at
   row 80. Needs an OPAQUE background — the usual translucent th would let rows show through. */
th{position:sticky;top:0;z-index:2;background:var(--panel2)!important;
  box-shadow:inset 0 -1px 0 var(--line)}
.hint{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:8px}
.hint.offr{margin:0 0 5px}   /* sits above its table, so the gap belongs underneath */
/* "+24%" deviation badge next to a value. currentColor + opacity makes it inherit the hue of the
   figure it belongs to — muted red inside a ratio cell, muted gold/blue inside a paired one — so
   the badge is always visually tied to its own number. */
i.dev{font-style:normal;font-size:9.5px;margin-left:4px;white-space:nowrap;
  color:currentColor;opacity:.55}
.hax{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10.5px;color:var(--dim);
 margin-top:6px;border-top:1px solid var(--line);padding-top:5px}
.mk{display:flex;gap:14px;font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:9px;flex-wrap:wrap}
.mk i{font-style:normal;color:var(--fg)}
.mk .d{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:4px;vertical-align:middle}
/* metric bars */
.mrow{margin:0 0 13px}
.mrow .lbl{font-family:var(--mono);font-size:11px;color:var(--muted);display:flex;justify-content:space-between}
.mrow .lbl b{color:var(--fg);font-weight:600}
.bar{height:9px;border-radius:5px;background:var(--panel2);margin-top:5px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:5px}
.bA i{background:var(--gold)}.bB i{background:var(--blue)}
.bnum{font-family:var(--mono);font-size:10.5px;color:var(--dim);display:flex;justify-content:space-between;margin-top:3px}
/* tables */
/* colour is set EXPLICITLY here: table cells don't reliably inherit it from .wrap (a UA/host
   stylesheet can win), which rendered these numbers black-on-dark — unreadable. */
table{border-collapse:collapse;width:100%;font-size:12.5px;color:var(--fg)}
th,td{border-bottom:1px solid var(--line);padding:.4rem .5rem;text-align:right;color:var(--fg);
 font-variant-numeric:tabular-nums;font-family:var(--mono)}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:600}
tbody tr:hover{background:var(--panel2)}
.hi{color:var(--red);font-weight:600}.lo{color:var(--green);font-weight:600}
.rbar{display:inline-block;height:6px;border-radius:3px;background:var(--green);vertical-align:middle;margin-right:6px}
.rbar.over{background:var(--red)}
/* No ratio exists when the other guest reports nothing in a family, and the colour has to say so:
   the `over` test was `r and r >= 1`, so a missing ratio fell through to GREEN — reading as "Monad
   spends less" on the byte/bit row, where it spends tens of millions against zero. The figure is not
   repeated here: it moves with every rebuild, and the table states it. Neutral for "not comparable". */
.rbar.na{background:var(--dim);opacity:.5}
.na{color:var(--dim);font-weight:600}
details{margin-top:8px}summary{cursor:pointer;font-family:var(--mono);font-size:12px;color:var(--muted);
 padding:9px 0}summary:hover{color:var(--fg)}
.note{color:var(--muted);font-size:12.5px;line-height:1.6;margin:12px 0 0}
.note b{color:var(--accent-dim)}
.cmd{font-family:var(--mono);font-size:11.5px;background:var(--ink);border:1px solid var(--line);
 border-radius:8px;padding:9px 11px;color:#aeb7cc;overflow-x:auto;white-space:pre;margin:7px 0 0}
.rep{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:4px}
.rep div{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.rep .q{font-family:var(--mono);font-size:10.5px;color:var(--accent);letter-spacing:.1em;text-transform:uppercase}
.rep .b{font-family:var(--mono);font-size:15px;font-weight:600;margin-top:4px}
.rep .r{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
"""

# ── histogram drill-down (inline, no deps: the page must open from a file:// path) ──
_AXBAR_JS = r"""
// Keep the sticky bar naming the section the reader is actually in. Plain scroll maths rather than
// IntersectionObserver: the sections are tall and overlapping thresholds made the label flicker at
// boundaries. Active section = the last one whose top has passed the bar.
(function () {
  var bar = document.getElementById('axbar');
  var cur = document.getElementById('axbar-cur');
  if (!bar || !cur) return;
  var secs = [].slice.call(document.querySelectorAll('section[data-axis]'));
  if (!secs.length) return;
  var chips = [].slice.call(bar.querySelectorAll('.jump a'));
  var last = null;
  function paint() {
    var y = bar.getBoundingClientRect().bottom + 4;
    var act = secs[0];
    for (var i = 0; i < secs.length; i++) {
      if (secs[i].getBoundingClientRect().top <= y) act = secs[i];
    }
    if (act === last) return;
    last = act;
    cur.innerHTML = "<span class=bk>" + act.dataset.backend + "</span>" +
                    "<span class=pair>" + act.dataset.pair + "</span>";
    chips.forEach(function (c) { c.classList.toggle('on', c.dataset.for === act.dataset.axis); });
  }
  var tick = false;
  addEventListener('scroll', function () {
    if (tick) return;
    tick = true;
    requestAnimationFrame(function () { paint(); tick = false; });
  }, {passive: true});
  addEventListener('resize', paint, {passive: true});
  paint();
})();
"""


_HIST_JS = r"""
const _f = n => n == null ? '—' : n.toLocaleString('en-US', {maximumFractionDigits: 0});
const _x = r => r == null ? '—' : r.toFixed(3) + '×';
// ── shared table builder ─────────────────────────────────────────────────────────────
// Used for BOTH the histogram drill-down and the off-pattern list: same data, same columns,
// defined once. `extra` inserts an additional column (the off-pattern list uses it for the
// "what stands out" phrase).
function _cols(D, list, med) {
  const pw = D.pw, has = k => list.some(e => e[k] != null);
  // In `med` mode every value also shows how far it sits from the sample median of that same
  // metric — for a list of odd blocks, "what is unusual about this block" beats "A vs B", which
  // the ratio column already answers.
  const dev = (k, v) => (!med || med[k] == null || v == null || !med[k]) ? '' :
    '<i class=dev>' + ((v / med[k] - 1) >= 0 ? '+' : '') +
    ((v / med[k] - 1) * 100).toFixed(0) + '%</i>';
  const one = (k, fmt) => e => '<td>' + (e[k] == null ? '—' : fmt(e[k]) + dev(k, e[k])) + '</td>';
  const cols = [['ratio', e => '<td class=' + (e.r > 1 ? 'hi' : 'lo') + '>' + _x(e.r) +
    dev('r', e.r) + '</td>']];
  if (has('pr'))   cols.push([pw + ' ratio', one('pr', _x)]);
  if (has('g'))    cols.push(['gas', one('g', _f)]);
  if (has('tx'))   cols.push(['txs', one('tx', _f)]);
  if (has('gtx'))  cols.push(['gas/tx', one('gtx', _f)]);
  // header names the unit; the cell keeps the ratio (compact) and carries the two counts it
  // divides in its tooltip, so nobody has to guess whether "1.15x" is a share or a ratio.
  const pcell = (ra, ka, kb) => e => '<td title="' + D.A + ' ' + _f(e[ka]) + ' calls vs ' + D.B +
    ' ' + _f(e[kb]) + ' calls">' + _x(e[ra]) + dev(ra, e[ra]) + '</td>';
  // Paired cells: the two guests are told apart by COLOUR (gold = A, blue = B), the same code
  // used everywhere else in the report — so the header can stay short and drop their names.
  const duo = (ka, kb, fmt) => e => '<td>' + (e[ka] == null ? '—' :
    '<span class=cA>' + fmt(e[ka]) + dev(ka, e[ka]) + '</span><br><span class=cB>' +
    fmt(e[kb]) + dev(kb, e[kb]) + '</span>') + '</td>';
  if (has('kecA')) cols.push(['keccak perms', duo('kecA', 'kecB', _f)]);
  if (has('kecR')) cols.push(['keccak ratio', pcell('kecR', 'kecA', 'kecB')]);
  if (has('ecR'))  cols.push(['secp256k1 ratio', pcell('ecR', 'ecA', 'ecB')]);
  if (has('sysR')) cols.push(['all precompiles ratio', one('sysR', _x)]);
  if (has('kecGA')) cols.push(['keccak per Mgas', duo('kecGA', 'kecGB', _f)]);
  // %opcodes sits next to %precompiles deliberately: over the ZisK sample it separates the
  // off-pattern blocks at ~34x the usual spread — three times better than any metric that was
  // being displayed — so hiding it inside a tooltip would bury the best available explanation.
  const split = k => e => {
    const all = e[k + 'all'];
    return all ? Object.entries(all).map(([n, v]) => n.toLowerCase() + ' ' + v + '%').join(' · ') : '';
  };
  if (has('aopc')) cols.push(['% opcodes', e => '<td title="' + split('apc')(e) + ' | ' +
    split('bpc')(e) + '">' + (e.aopc == null ? '—' :
    '<span class=cA>' + e.aopc.toFixed(1) + '%' + dev('aopc', e.aopc) + '</span><br>' +
    '<span class=cB>' + e.bopc.toFixed(1) + '%' + dev('bopc', e.bopc) + '</span>') + '</td>']);
  if (has('apc'))  cols.push(['% precompiles', duo('apc', 'bpc', v => v.toFixed(1) + '%')]);
  if (has('awt'))  cols.push([D.unit + ' per tx', duo('awt', 'bwt', _f)]);
  if (has('awg'))  cols.push([D.unit + ' per Mgas', duo('awg', 'bwg', _f)]);
  if (has('insa')) cols.push(['witness bytes', duo('insa', 'insb', _f)]);
  return cols;
}
// Sample medians per metric, from every block on this axis — the reference the `med` mode
// compares against.
function _medians(D) {
  const all = []; D.buckets.forEach(bk => bk.forEach(e => all.push(e)));
  const m = {};
  ['r','pr','g','tx','gtx','kecA','kecB','kecR','ecR','sysR','kecGA','kecGB','apc','bpc',
   'awt','bwt','awg','bwg','insa','insb','aopc','bopc'].forEach(k => {
    const v = all.map(e => e[k]).filter(x => x != null);
    if (v.length) m[k] = _med(v);
  });
  return m;
}
function _table(D, list, extra, med) {
  const cols = _cols(D, list, med);
  const head = '<tr><th>block</th>' + (extra ? '<th>' + extra.head + '</th>' : '') +
    cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr>';
  const body = list.map(e => '<tr><td>' + e.b + '</td>' +
    (extra ? '<td style="text-align:left">' + (extra.cell(e) || '') + '</td>' : '') +
    cols.map(c => c[1](e)).join('') + '</tr>').join('');
  return '<div class=scroll><table>' + head + body + '</table></div>';
}
function _key(D) {   // paired columns rely on colour, so restate the key above every table
  return '<div class=legend style="margin:0 0 6px"><span><span class=sw ' +
    'style="background:var(--gold)"></span>' + D.A + '</span><span><span class=sw ' +
    'style="background:var(--blue)"></span>' + D.B + '</span></div>';
}

const _med = a => { if (!a.length) return null; const v = [...a].sort((p, q) => p - q), m = v.length >> 1;
                    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };

document.querySelectorAll('.hist').forEach(hist => {
  const ax = hist.dataset.ax;
  const D = JSON.parse(document.getElementById('hd-' + ax).textContent);
  const panel = document.getElementById('hp-' + ax);
  const width = (D.hi - D.lo) / D.nb;

  hist.addEventListener('click', ev => {
    const bar = ev.target.closest('.hb');
    if (!bar) return;
    const i = +bar.dataset.b, list = D.buckets[i] || [];
    hist.querySelectorAll('.hb').forEach(b => b.classList.toggle('sel', b === bar));
    const lo = D.lo + width * i, hi = lo + width;
    if (!list.length) {
      panel.className = 'hp on';
      panel.innerHTML = '<div class=hpk><span>' + _x(lo) + ' – ' + _x(hi) +
        '</span><button data-close>close ×</button></div>' +
        '<p class=syn>No blocks in this bucket.</p>';
      return;
    }
    // Synthesis: what these blocks have in common, and how they sit against the whole sample.
    const rs = list.map(e => e.r), gs = list.map(e => e.g).filter(v => v);
    const mg = _med(gs), rel = (mg && D.gasMedian) ? (mg / D.gasMedian - 1) * 100 : null;
    const mr = _med(rs), dev = (mr / D.ratioMedian - 1) * 100;
    let syn = '<b>' + list.length + (list.length > 1 ? ' blocks' : ' block') + '</b> with a ratio of ' +
      _x(lo) + '–' + _x(hi) + ' (median ' + _x(mr) + ', ' +
      (Math.abs(dev) < 0.05 ? 'the sample median' :
        (dev > 0 ? '+' : '') + dev.toFixed(1) + '% vs the sample median ' + _x(D.ratioMedian)) + ').';
    if (rel != null)
      syn += ' Typical size here <b>' + _f(mg) + ' gas</b> — ' +
        (Math.abs(rel) < 5 ? 'about average for this range' :
          (rel > 0 ? '<b>' + rel.toFixed(0) + '% larger</b>' : '<b>' + (-rel).toFixed(0) + '% smaller</b>') +
          ' than the median block, so ' + (rel > 0 ? 'these are the bigger blocks' : 'these are the smaller blocks')) + '.';
    // What the blocks in this bucket made the guests DO — the mechanism, not just the size.
    const mtx = _med(list.map(e => e.tx).filter(v => v));
    const mgtx = _med(list.map(e => e.gtx).filter(v => v));
    if (mtx) syn += ' Typically <b>' + _f(mtx) + ' txs</b>' +
      (mgtx ? ' at ~' + _f(mgtx) + ' gas each' : '') + '.';
    const mk = _med(list.map(e => e.kecR).filter(v => v)),
          mec = _med(list.map(e => e.ecR).filter(v => v)),
          msy = _med(list.map(e => e.sysR).filter(v => v));
    if (mk || mec || msy) {
      // Absolute counts as well as the ratio: "1.15×" alone reads as a percentage to some, and the
      // raw numbers also say whether we are talking about a handful of calls or hundreds of thousands.
      const pair = (lbl, ra, ka, kb) => {
        const a = _med(list.map(e => e[ka]).filter(v => v != null)),
              b = _med(list.map(e => e[kb]).filter(v => v != null));
        return lbl + ' ' + (a != null ? _f(a) + ' vs ' + _f(b) + ' ' : '') + '(' + _x(ra) + ')';
      };
      const bits = [];
      if (mk) bits.push(pair('keccak', mk, 'kecA', 'kecB'));
      if (mec) bits.push(pair('secp256k1', mec, 'ecA', 'ecB'));
      if (msy) bits.push('all precompiles (' + _x(msy) + ')');
      syn += ' Precompile calls per block, <span class=cA>' + D.A + '</span> vs <span class=cB>' +
        D.B + '</span> — median counts, ratio in brackets: <b>' + bits.join(', ') + '</b>' +
        ((mk && mk > 1.15) ? ' — ' + D.A + ' hashes markedly more here.' : '.');
    }
    const mpa = _med(list.map(e => e.apc).filter(v => v != null)),
          mpb = _med(list.map(e => e.bpc).filter(v => v != null));
    if (mpa != null && mpb != null) {
      // Full split, not just the precompile share: when the COST ratio departs from the work-unit
      // ratio, this line is where you see which category carries it.
      const cats = ['Main', 'Opcodes', 'Precompiles', 'Memory', 'Base'];
      const shr = (k, cat) => _med(list.map(e => e[k] && e[k][cat]).filter(v => v != null));
      const bits = cats.map(cat => {
        const a = shr('apcall', cat), b = shr('bpcall', cat);
        return (a == null || b == null) ? null
          : cat.toLowerCase() + ' ' + a.toFixed(1) + '% / ' + b.toFixed(1) + '%';
      }).filter(Boolean);
      syn += ' Trace cost splits, <span class=cA>' + D.A + '</span> / <span class=cB>' + D.B +
        '</span>: <b>' + (bits.length ? bits.join(', ') : 'precompiles ' + mpa.toFixed(1) + '% / ' +
        mpb.toFixed(1) + '%') + '</b>.';
    }
    const tbl = _table(D, list);
    panel.className = 'hp on';
    panel.innerHTML = '<div class=hpk><span>bucket ' + _x(lo) + ' – ' + _x(hi) +
      '</span><button data-close>close ×</button></div>' +
      '<p class=syn>' + syn + '</p>' + _key(D) + tbl;
  });

  panel.addEventListener('click', ev => {
    if (!ev.target.closest('[data-close]')) return;
    panel.className = 'hp';
    hist.querySelectorAll('.hb').forEach(b => b.classList.remove('sel'));
  });

  // ── off-pattern blocks: same columns, plus the phrase that says what's odd ──
  const host = document.getElementById('ol-' + ax);
  if (host && D.outliers && D.outliers.length) {
    const all = {}; D.buckets.forEach(bk => bk.forEach(e => { all[e.b] = e; }));
    const list = D.outliers.map(b => all[b]).filter(Boolean);
    if (list.length) {
      const stands = e => {
        // whichever guest strayed furthest from ITS OWN usual cost explains the row
        const ra = e.aw / D.aMed, rb = e.bw / D.bMed;
        const [who, rel, col] = Math.abs(ra - 1) >= Math.abs(rb - 1)
          ? [D.A, ra, 'cA'] : [D.B, rb, 'cB'];
        return '<span class=' + col + '>' + who + '</span> used ' + (rel < 1 ? 'only ' : '') +
               '<b>' + rel.toFixed(2) + '×</b> its usual ' + D.unit;
      };
      host.innerHTML = _key(D) + _table(D, list, {head: 'what stands out', cell: stands}, _medians(D))
        + '<div class=hp id="od-' + ax + '"></div>'
        + (D.ops && D.medBlock ? '<p class=hint>▸ click a row to see which opcodes this block runs '
           + 'more of than the typical one (' + D.medBlock + ')</p>' : '');

      // ── row click: per-opcode delta against the median block, same guest ──
      // Computed here from data already in the page: no profiling, instant. Function-level detail
      // needs a real profile run, so the panel prints that command instead of pretending to do it.
      const od = document.getElementById('od-' + ax);
      host.querySelectorAll('tbody tr, table tr').forEach(tr => {
        const b = +(tr.cells[0] && tr.cells[0].textContent.trim());
        if (!b || !D.ops || !D.ops[b] || !D.medBlock || !D.ops[D.medBlock]) return;
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => {
          const e = list.find(x => x.b === b); if (!e) return;
          // the guest that strayed furthest from its own usual cost is the one worth looking at
          const ra = e.aw / D.aMed, rb = e.bw / D.bMed;
          const sd = Math.abs(ra - 1) >= Math.abs(rb - 1) ? 'a' : 'b';
          const who = sd === 'a' ? D.A : D.B, col = sd === 'a' ? 'cA' : 'cB';
          const cur = D.ops[b][sd] || {}, ref = D.ops[D.medBlock][sd] || {};
          const names = [...new Set([...Object.keys(cur), ...Object.keys(ref)])];
          const rowsd = names.map(n => {
            const v = cur[n] || 0, r = ref[n] || 0;
            return {n, v, r, d: r ? (v / r - 1) * 100 : null};
          }).filter(x => x.v || x.r).sort((p, q) => (q.d ?? -1e9) - (p.d ?? -1e9));
          const cells = rowsd.map(x => '<tr><td style="text-align:left">' + x.n + '</td><td>' +
            _f(x.v) + '</td><td>' + _f(x.r) + '</td><td class=' +
            (x.d > 0 ? 'hi' : 'lo') + '>' + (x.d == null ? '—' :
            (x.d >= 0 ? '+' : '') + x.d.toFixed(0) + '%') + '</td></tr>').join('');
          od.className = 'hp on';
          od.innerHTML = '<div class=hpk><span>block ' + b + ' · <span class=' + col + '>' + who +
            '</span> vs the typical block ' + D.medBlock + '</span>' +
            '<button data-close>close ×</button></div>' +
            '<p class=syn>Where ' + who + "'s work goes in this block, against the same guest on " +
            'the median block. Sorted by the biggest increase.</p>' +
            '<div class=scroll><table><tr><th>opcode</th><th>this block</th><th>typical block</th>' +
            '<th>delta</th></tr>' + cells + '</table></div>' +
            '<p class=hint>function-level detail needs a profile run: ./hotspots.py profile ' +
            '--backend ' + (D.unit === 'steps' ? 'zisk' : 'sp1') + ' --elf ../guests/' + who +
            '/' + who + '.elf -i &lt;input for ' + b + '&gt; --out results/x</p>';
          od.scrollIntoView({block: 'nearest'});
        });
      });
      od.addEventListener('click', ev => {
        if (ev.target.closest('[data-close]')) od.className = 'hp';
      });
    }
  }
});

// ── say when a table has columns off to the right ──────────────────────────────────────────────
// A clipped column reads as a truncated number, not as "there is more". The horizontal bar exists
// but sits at the foot of the box, which is below the fold on a tall table — so the cue has to be
// above it. Counted from the layout, never asserted: a table that fits says nothing, and the count
// is recomputed on resize. Runs last so it also sees the tables built by the code above.
function _hidden(sc) {
  const cells = sc.querySelector('tr') ? [...sc.querySelector('tr').cells] : [];
  const edge = sc.getBoundingClientRect().right;
  return cells.filter(c => c.getBoundingClientRect().right > edge + 1).length;
}
function _cue() {
  document.querySelectorAll('.scroll').forEach(sc => {
    let tag = sc.previousElementSibling;
    if (!tag || !tag.classList.contains('offr')) {
      tag = document.createElement('p');
      tag.className = 'hint offr';
      sc.parentElement.insertBefore(tag, sc);
    }
    const n = sc.scrollWidth > sc.clientWidth + 1 ? _hidden(sc) : 0;
    const txt = n ? '▸ ' + n + ' more column' + (n > 1 ? 's' : '') +
                    ' to the right — the table scrolls sideways' : '';
    // write only on change: these writes are themselves DOM mutations, so an unconditional one turns
    // any mutation-driven re-run into an endless loop
    if (tag.textContent !== txt) tag.textContent = txt;
    const disp = n ? '' : 'none';
    if (tag.style.display !== disp) tag.style.display = disp;
  });
}
_cue();
addEventListener('resize', _cue);
// The drill-down and per-block panels build their tables on click, long after this runs, so those
// tables need a second pass. Driven by the click that creates them — bounded, unlike observing the
// document, which _cue's own writes would retrigger forever.
document.addEventListener('click', () => setTimeout(_cue, 0));
"""

# Work families live in hotspots.py, next to its `module()` classifier — reading a profile is its
# job, and putting them there means `hotspots diff` gets the same grouping. We only consume them.

# Software BN254 pairing arithmetic, deliberately NARROWER than hotspots' `elliptic-curve crypto`
# family: that family also holds secp256k1/ecrecover, which is identical between the guests (secp
# syscall counts came out at exactly 1.000x over ~228k calls), so including it would dilute the very
# thing being measured. These are the libraries that implement the BN254 (alt_bn128) pairing and
# field arithmetic in pure Rust/C++, with no zkVM precompile behind them.
# The accelerated counterpart: a patched crate backed by a zkVM precompile rather than plain Rust.
# `zkvm_bn254_*` is the SP1-side patched symbol; `ziskos::zisklib` is ZisK's in-VM library. Measured
# here, `rsp` is the ONLY guest with 0% of this and all its curve work in software — monad-sp1 runs
# on the same backend and does have zkvm_bn254_g1_mul, so this is a missing patch, not a limit of SP1.
BN254_ACCEL_RE = re.compile(r'zkvm_bn254|zkvm_secp|zisklib', re.I)

BN254_RE = re.compile(r'substrate_bn|bn254|bn128|alt_bn|pairing|arkworks|ark_(bn|ec|ff)|blst'
                      r'|(?<![a-z])Fq(?![a-z])|(?<![a-z])Fq2|G1Affine|G2Affine', re.I)


def bn254_share(axis, side_k, blocks, cache):
    """Mean share of a guest's attributed work spent in software BN254 arithmetic, over `blocks`.

    Read from the SAME per-block cache profile_blocks fills, so it costs nothing extra and cannot
    drift from what the family table shows. Returns (share, n_blocks_found) — n matters, because the
    pairing-heavy regime holds only ~9 of the 50 profiled blocks."""
    ax = AXES[axis]; side = ax[side_k]
    tot, n = 0.0, 0
    for b in blocks:
        e = cache.get(rp(side['elf']), b, _cachemod.PROFILE)
        if not e or not e.get('fns'): continue
        num = sum(c for fn, c in e['fns'] if BN254_RE.search(fn))
        den = sum(c for _f, c in e['fns'])
        if den: tot += num / den; n += 1
    return (tot / n, n) if n else (None, 0)


def bn254_paths(axis, side_k, blocks, cache):
    """(software share, accelerated share, has_accel_symbol) over `blocks`, same cache.

    Separating the two answers the actionable question: is a guest slow at pairing because the work
    is inherently expensive, or because it never got the precompile-backed crate?"""
    ax = AXES[axis]; side = ax[side_k]
    soft = acc = den = 0
    sym = False
    for b in blocks:
        e = cache.get(rp(side['elf']), b, _cachemod.PROFILE)
        if not e or not e.get('fns'): continue
        for fn, c in e['fns']:
            den += c
            if BN254_ACCEL_RE.search(fn): acc += c; sym = True
            elif BN254_RE.search(fn):     soft += c
    return (soft / den, acc / den, sym) if den else (None, None, False)


_HASH_RE = re.compile(r'keccak|sha3|sha256|tiny_keccak|blake|xor_block', re.I)


def percall(axis, side_k, side, blocks, cache, rows, rx=_HASH_RE, counter=None):
    """Attributed instructions of a symbol group, PER CALL of a shared precompile.

    Generic on purpose: whenever both guests reach the same precompile, its own cost is identical per
    call, so a family ratio built from instruction counts describes the WRAPPER, not the work. Read
    alone it inverts the conclusion — a guest that uses the accelerated path can look like the one
    hashing in software. Splitting per call separates the two.

    The Pearson r matters as much as the median: a cost flat across a wide range of call counts does
    not depend on payload size, which makes it per-call SETUP rather than work. Returns
    (median, lo, hi, r, n) or None."""
    ax = AXES[axis]
    xs, ys, rats = [], [], []
    for b in blocks:
        e = cache.get(rp(ax[side_k]['elf']), b, _cachemod.PROFILE)
        r0 = rows.get(str(b)) or rows.get(b)
        if not e or not e.get('fns') or not r0: continue
        R = r0[side]
        calls = counter(R) if counter else (R.get('kec') or (R.get('sys') or {}).get('KECCAK_PERMUTE'))
        den = sum(c for _f, c in e['fns'])
        if not calls or not den or not R.get('work'): continue
        w = sum(c for fn, c in e['fns'] if rx.search(fn)) / den * R['work']
        xs.append(calls); ys.append(w); rats.append(w / calls)
    if len(rats) < 3: return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - b_) * (c - my) for a, b_, c in ((a, mx, c) for a, c in zip(xs, ys)))
    den2 = (sum((a - mx) ** 2 for a in xs) * sum((c - my) ** 2 for c in ys)) ** .5
    return (statistics.median(rats), min(rats), max(rats), (num / den2 if den2 else 0.0), len(rats))


def profile_blocks(axis, side_k, blocks, cache):
    """Profile one guest over SEVERAL blocks and fold the mean into work families.

    Several, not one: a single block's ratio can be typical while its composition is not (measured
    on the precompile share: 11.2% for the median-ratio block vs 8.8% typical). hotspots.py
    --aggregate averages the per-function counts in one process, so N blocks cost N executions but
    only one startup. Cached on the ELF mtime + the block list, so it re-runs only when a guest is
    rebuilt or the sample changes."""
    ax = AXES[axis]; side = ax[side_k]
    elf = rp(side['elf'])
    blocks = sorted(blocks)
    hs = _hotspots()
    # Cached PER BLOCK, not per block-list: raising the sample from 10 to 50 must profile 40 blocks,
    # not 50. What is stored are the RAW per-function counts, never family sums — the taxonomy is a
    # module constant that gets edited, and a cached sum keeps the old grouping without saying so (it
    # did: a trie-node fix stayed invisible until cleared). Folding to families at read time costs
    # nothing now that hotspots.family() is memoised, so the two namespaces that used to cache the
    # sums are gone.
    todo = [b for b in blocks
            if cache.get(elf, b, _cachemod.PROFILE, inp=resolve_input(side, b)) is None]
    # Chunked: hotspots aborts the whole process on a single bad input, so profiling 50 blocks in one
    # call risked losing all of them (it did — an axis profiled for ~20 min and cached nothing).
    # Small batches keep the startup amortised while bounding what a failure costs.
    for chunk in [todo[i:i + 10] for i in range(0, len(todo), 10)]:
        todo = chunk
        # One hotspots process per chunk, WITHOUT --aggregate: the profile then carries
        # one entry per input, so a single startup still yields per-block results to cache.
        tmps, tag2blk = [], {}
        try:
            cmd = [os.path.join(HERE, 'hotspots.py'), 'profile', '--backend', ax['backend'],
                   '--elf', elf, '--out', None]
            inputs = []
            for b in todo:
                inp = resolve_input(side, b)
                if not inp: continue
                if needs_framing(ax['backend'], side):
                    t = tempfile.NamedTemporaryFile(suffix=f'.{b}.bin', delete=False).name
                    frame_ziskos(inp, t); tmps.append(t); inp = t
                inputs.append(inp)
                tag2blk[str(b)] = b
            if inputs:
                with tempfile.TemporaryDirectory() as td:
                    cmd = [c for c in cmd if c is not None] + [td]
                    for i in inputs: cmd += ['-i', i]
                    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
                    pj = os.path.join(td, 'profile.json')
                    if r.returncode != 0 or not os.path.exists(pj):
                        # One bad input used to abort the whole batch and cache nothing.
                        print(f"  [{axis}] profiling {side['name']} failed on this batch "
                              f"({(r.stderr or r.stdout).strip()[-160:]})")
                    if r.returncode == 0 and os.path.exists(pj):
                        for tag, e in json.load(open(pj)).items():
                            # Match on the block number inside the tag, not the tag itself: the two
                            # backends derive tags differently (the SP1 path strips a leading "1-"),
                            # and framed ZisK inputs are temp files — exact matching silently cached
                            # nothing for a whole axis.
                            mb = re.search(r'(\d{6,})', tag)
                            b = tag2blk.get(mb.group(1)) if mb else None
                            if b is None: continue
                            fns = sorted(((f"{fn.get('module','')}::{fn.get('name','')}",
                                           fn.get('count') or 0)
                                          for fn in e.get('functions', [])),
                                         key=lambda t: -t[1])[:120]
                            cache.put(elf, b, _cachemod.PROFILE,
                                      {'fns': fns, 'trunc': 120,
                                       'total': sum(c for _n, c in
                                                    ((f"{fn.get('module','')}::{fn.get('name','')}",
                                                      fn.get('count') or 0)
                                                     for fn in e.get('functions', [])))},
                                      name=side['name'], backend=ax['backend'],
                                      inp=resolve_input(side, b))
        finally:
            for t in tmps:
                if os.path.exists(t): os.remove(t)
    got = [e for e in (cache.get(elf, b, _cachemod.PROFILE) for b in blocks) if e]
    if not got: return None
    fams = {}
    for g in got:
        for nm, c in g.get('fns', []):
            k = hs.family(nm); fams[k] = fams.get(k, 0) + c
    n_ = len(got)
    return {'fams': {k: v / n_ for k, v in fams.items()},
            'total': sum(g['total'] for g in got) / n_, 'blocks': blocks, 'n': n_}

def _hostinfo():
    """Where these numbers were produced. Work-units are machine-independent, but the exec-time
    column is not — so the host belongs in the report."""
    def sh(*c):
        try: return subprocess.run(c, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception: return ''
    cpu = sh('sysctl', '-n', 'machdep.cpu.brand_string') or platform.processor() or 'unknown CPU'
    cores = sh('sysctl', '-n', 'hw.ncpu') or str(os.cpu_count() or '?')
    memb = sh('sysctl', '-n', 'hw.memsize')
    ram = f"{int(memb)/1024**3:.0f} GiB" if memb.isdigit() else '?'
    osn = f"{sh('sw_vers', '-productName')} {sh('sw_vers', '-productVersion')}".strip() or platform.platform()
    return f"{cpu} · {cores} cores · {ram} · {osn}"

def _hist(ratios, s, rows=None, gas_map=None, tx_map=None):
    """Distribution of the per-block ratio — the 'is the penalty consistent?' picture.

    Bars are clickable: each carries the blocks that landed in it, so the question the shape
    raises ("which blocks are those, and what have they got in common?") is answerable in place
    instead of by cross-referencing the big table."""
    lo, hi = min(ratios), max(ratios)
    nb = 26
    span = (hi - lo) or 1e-9
    which = lambda r: min(nb - 1, int((r - lo) / span * nb))
    bins = [0] * nb
    for r in ratios:
        bins[which(r)] += 1
    # per-bucket block detail for the drill-down
    zisk = AXES[s['axis']]['backend'] == 'zisk'
    pwk = 'cost' if zisk else 'pgu'
    gm, tm = gas_map or {}, tx_map or {}
    buckets = [[] for _ in range(nb)]
    for b, r0 in (rows or {}).items():
        if not r0['b'].get('work'): continue
        rat = r0['a']['work'] / r0['b']['work']
        pa, pb = r0['a'].get(pwk), r0['b'].get(pwk)
        g = r0['b'].get('gas') or r0['a'].get('gas') or gm.get(b)
        tx = r0['b'].get('txs') or r0['a'].get('txs') or tm.get(b)
        e = {'b': int(b), 'r': round(rat, 4), 'aw': r0['a']['work'], 'bw': r0['b']['work'],
             'g': g, 'tx': tx, 'pr': round(pa / pb, 4) if (pa and pb) else None,
             # nsecs first: `secs` carries SP1's gas-estimation pass, which reverses which guest
             # looks faster on 26% of blocks here (block 25552073: 3.74s vs 5.26s gas-on says A wins,
             # 2.13s vs 2.03s honest says it loses). Same fix as the metric bar row.
             'as': pure_secs(s.get('a_name'), r0['a'].get('work')),
             'bs': pure_secs(s.get('b_name'), r0['b'].get('work'))}
        if g and tx: e['gtx'] = round(g / tx)
        # per-side work per Mgas: strips block size out, so a bucket groups by efficiency
        if g:
            e['awg'] = round(r0['a']['work'] / (g / 1e6))
            e['bwg'] = round(r0['b']['work'] / (g / 1e6))
        if tx:                             # cost of an average transaction in this block
            e['awt'] = round(r0['a']['work'] / tx)
            e['bwt'] = round(r0['b']['work'] / tx)
        # ── what the block MADE the guests do ──
        # Both axes now carry a trace-cost split ('cats'): ZisK prints it, and for SP1 it is derived
        # from the same weights hotspots.py uses. So this is deliberately axis-agnostic.
        for side, k in (('a', 'apc'), ('b', 'bpc')):
            c = r0[side].get('cats')
            if c and sum(c.values()):
                tot = sum(c.values())
                e[k] = round(100 * c.get('Precompiles', 0) / tot, 1)
                # Opcodes share too: measured as the sharpest discriminant of off-pattern blocks
                # (~34× the usual spread on the ZisK sample vs ~10× for the precompile share).
                e[side + 'opc'] = round(100 * c.get('Opcodes', 0) / tot, 1)
                e[k + 'all'] = {n: round(100 * v / tot, 1) for n, v in c.items()}
        # keccak / secp256k1 call counts: SP1 reports them as syscalls; ZisK reports no count for
        # precompiles, so run_zisk recovers keccak from its per-opcode cost (see ZISK_KECCAK_COST).
        if zisk:
            va, vb = r0['a'].get('kec'), r0['b'].get('kec')
            if va or vb:
                e['kecA'], e['kecB'] = va, vb
                if va and vb: e['kecR'] = round(va / vb, 3)
        else:
            sa, sb = r0['a'].get('sys') or {}, r0['b'].get('sys') or {}
            # ecrecover shows up as secp256k1 curve ops, so fold ADD+DOUBLE+DECOMPRESS together
            grp = lambda d, pre: sum(v for k, v in d.items() if k.startswith(pre))
            for lbl, pre in (('kec', 'KECCAK'), ('ec', 'SECP256K1')):
                va, vb = grp(sa, pre), grp(sb, pre)
                if va or vb:
                    e[lbl + 'A'], e[lbl + 'B'] = va, vb
                    if va and vb: e[lbl + 'R'] = round(va / vb, 3)
            if r0['a'].get('tsys') and r0['b'].get('tsys'):
                e['sysR'] = round(r0['a']['tsys'] / r0['b']['tsys'], 3)
        if g and e.get('kecA') and e.get('kecB'):    # hash density of the block, per side
            e['kecGA'] = round(e['kecA'] / (g / 1e6))
            e['kecGB'] = round(e['kecB'] / (g / 1e6))
        # witness size: the two guests are fed different formats AND different amounts of data,
        # which the report claims in prose — this is the figure behind that claim.
        if r0['a'].get('insz') and r0['b'].get('insz'):
            e['insa'], e['insb'] = r0['a']['insz'], r0['b']['insz']
        buckets[which(rat)].append(e)
    for bl in buckets: bl.sort(key=lambda e: -e['r'])
    top = max(bins) or 1
    bars = []
    for i, c in enumerate(bins):
        c0, c1 = lo + span * i / nb, lo + span * (i + 1) / nb
        cls = 'hb over' if c0 >= 1.0 else 'hb'
        bars.append(f"<button class='{cls}' data-b='{i}' style='height:{max(1, round(100*c/top))}%' "
                    f"title='{c} block(s) at {c0:.3f}×–{c1:.3f}× — click for the list'></button>")
    mid = lo + span / 2
    # 1× is the meaningful threshold (parity), so mark it explicitly rather than leaving it to
    # the bar colour alone — it usually falls nowhere near the middle of the range.
    one = ""
    if lo < 1.0 < hi:
        one = (f"<div class=one style='left:{100*(1.0-lo)/span:.2f}%'><span>1×</span></div>")
    ax = s['axis']
    gvals = [e['g'] for bl in buckets for e in bl if e['g']]
    # Off-pattern blocks get the SAME per-block detail as a bucket: the outlier table is rendered
    # by the shared JS builder from these ids, so its columns can never drift from the drill-down's.
    amed = statistics.median([r['a']['work'] for r in (rows or {}).values() if r['a'].get('work')] or [1])
    bmed = statistics.median([r['b']['work'] for r in (rows or {}).values() if r['b'].get('work')] or [1])
    # Per-opcode detail, but ONLY for the blocks that can use it: the off-pattern ones plus the
    # median block they are compared against. Shipping it for all 372 would bloat the page for
    # data no click would ever reach.
    outb = [int(o['block']) for o in s.get('_outliers', [])]
    medb = s.get('block_median')
    ops = {}
    for b in set(outb) | ({int(medb)} if medb else set()):
        r0 = (rows or {}).get(str(b)) or (rows or {}).get(b)
        if not r0: continue
        d = {}
        for side, k in (('a', 'a'), ('b', 'b')):
            # opsn first: the panel says which opcodes a block "runs more of", i.e. COUNTS. On ZisK
            # `ops` holds per-opcode COSTS (keccak 6.5e9 = calls x 25 x 3022), so the panel was
            # comparing cost while labelled as operations. SP1's `ops` are already counts.
            o = r0[side].get('opsn') or r0[side].get('ops')
            if o: d[k] = o
        if d: ops[b] = d
    payload = {'unit': s['unit'], 'A': s['a_name'], 'B': s['b_name'],
               'pw': s.get('pw_unit'), 'ratioMedian': round(s['ratio_median'], 4),
               'gasMedian': statistics.median(gvals) if gvals else None,
               'lo': round(lo, 4), 'hi': round(hi, 4), 'nb': nb,
               'buckets': buckets,
               'outliers': outb, 'aMed': amed, 'bMed': bmed,
               'ops': ops, 'medBlock': int(medb) if medb else None}
    return (f"<div class=hist data-ax='{ax}'>{''.join(bars)}{one}</div>"
            f"<div class=hax><span>{lo:.3f}×</span><span>{mid:.3f}×</span><span>{hi:.3f}×</span></div>"
            f"<div class=mk><span><i>middle block</i> {x(s['ratio_median'])}</span>"
            # The mean asked for by readers averaging benchmark ratios. Geometric, not arithmetic:
            # see the ratio_gmean comment in summarize().
            f"<span title='the average that is correct for ratios: it treats 2x and 0.5x as "
            f"cancelling, and the A/B and B/A figures are exact reciprocals — the arithmetic mean "
            f"guarantees neither'><i>geometric mean</i> {x(s['ratio_gmean'])}</span>"
            f"<span><i>lowest 10%</i> under {x(s['ratio_p10'])}</span>"
            f"<span><i>highest 10%</i> over {x(s['ratio_p90'])}</span>"
            f"<span><i>swing</i> ±{s['cv']:.1f}%</span></div>"
            f"<script type='application/json' id='hd-{ax}'>{json.dumps(payload)}</script>"
            f"<div class=hp id='hp-{ax}'></div>"
            f"<p class=hint>▸ click any bar to list the blocks it holds</p>"
            + _curve_note(s))

def _near(ratios, med, w=.10):
    """Share of blocks within w of the median — measured, so the prose can't overstate clustering."""
    return 100 * sum(1 for r in ratios if abs(r - med) / med <= w) / max(1, len(ratios))


def _accel_note(s):
    """Why one guest pays: software crate vs precompile-backed crate. The actionable half."""
    pth = s.get('bn254_path') or {}
    A, B = s['a_name'], s['b_name']
    pa, pb = pth.get('a'), pth.get('b')
    if not (pa and pb) or pa[0] is None or pb[0] is None: return ""
    # The claim worth making is only available when one side has the accelerated symbol and the
    # other does not — on the SAME backend, which rules out a zkVM limitation.
    if pb[2] or not pa[2]: return ""
    return (f" <b>And it is a missing patch, not a limit of the backend:</b> across all profiled "
            f"blocks <span class=cA>{A}</span> reaches BN254 through a <b>precompile-backed crate</b> "
            f"({pa[1]*100:.2f}% of its work in <code>zkvm_bn254_*</code>, {pa[0]*100:.2f}% left in "
            f"plain software), while <span class=cB>{B}</span> has <b>none of it</b> "
            f"({pb[1]*100:.2f}%) and does <b>{pb[0]*100:.2f}%</b> of its work in software "
            f"<code>substrate_bn</code>. Same emulator, same blocks — {B} simply is not linking the "
            f"accelerated path {A} already uses.")


def _curve_note(s):
    """The left tail is not one distribution — see the CURVE_Z block in summarize().

    Every percentage here is measured (bn254_share over the profiled picks of each regime); an
    earlier version hardcoded five of them, which would have gone stale on the next guest rebuild."""
    c = s.get('curve')
    if not c: return ""
    A, B, n = s['a_name'], s['b_name'], s['n']
    bn = s.get('bn254') or {}
    pc = lambda t: f"{t[0]*100:.1f}%" if t and t[0] is not None else "n/a"
    nb = lambda t: t[1] if t else 0
    ha, hb = (bn.get('a') or {}).get('hot'), (bn.get('b') or {}).get('hot')
    ca, cb = (bn.get('a') or {}).get('cold'), (bn.get('b') or {}).get('cold')
    # Control from the other axis, if it profiled anything: the claim is that ONE guest is the
    # outlier, which cannot be shown from a single pair.
    ctl = []
    for oax, ob in (s.get('_other_bn254') or {}).items():
        if not ob: continue
        for k, nm in (('a', 'a_name'), ('b', 'b_name')):
            t = (ob.get(k) or {}).get('hot') or (ob.get(k) or {}).get('all')
            # 'hot' on the other axis is that axis's share on the SAME pairing-heavy blocks
            # (flag borrowed in main()); 'all' only appears if it profiled nothing per-regime.
            if t and t[0] is not None:
                ctl.append(f"{s.get('_other_names', {}).get((oax, k), k)} {pc(t)}")
    return (f"<p class=env><b>The low tail is a {B} precompile gap, not a {A} win.</b> On "
            f"<b>{c['n']} of {n} blocks</b> ({c['n']/n*100:.0f}%) one guest runs 256-bit curve "
            f"arithmetic — the BN254 pairing precompile — in <b>pure software</b>. Share of each "
            f"guest's own attributed work spent there, measured on the profiled blocks: "
            f"<span class=cB>{B}</span> <b>{pc(hb)}</b> on those blocks against {pc(cb)} on the "
            f"rest (n={nb(hb)}/{nb(cb)} profiled), while <span class=cA>{A}</span> spends "
            f"<b>{pc(ha)}</b> against {pc(ca)}"
            + (f" — and on the other axis, on those same blocks, {' and '.join(ctl)}" if ctl else "")
            + f". So {B} is the outlier, not the others."
            + _accel_note(s)
            + f" Split accordingly:</p>"
            f"<table class=cv><tr><th>blocks</th><th>n</th><th>median ratio</th><th></th></tr>"
            f"<tr><td>without software curve arithmetic</td><td>{n - c['n']}</td>"
            f"<td class={'hi' if c['med_clean'] >= 1 else 'lo'}>{x(c['med_clean'])}</td>"
            f"<td><i>compares the two engines</i></td></tr>"
            f"<tr><td>with it</td><td>{c['n']}</td>"
            f"<td class={'hi' if c['med_flag'] >= 1 else 'lo'}>{x(c['med_flag'])}</td>"
            f"<td><i>{B} pays for BN254 in software</i></td></tr>"
            f"<tr><td>all blocks — the headline above</td><td>{n}</td>"
            f"<td class={'hi' if s['ratio_median'] >= 1 else 'lo'}>{x(s['ratio_median'])}</td>"
            f"<td><i>averages the two</i></td></tr></table>"
            f"<p class=note>Detected from multiplication intensity (mul/cycle above "
            f"{c['z']}× the median for either guest), so it covers every block without extra "
            f"profiling. <b>Quote the engine figure when comparing engines</b>, and the all-blocks "
            f"figure when comparing the guests as they ship today — the difference is a fixable "
            f"{B} implementation gap, not an EVM one.</p>")


def _three_axes(s):
    """Work / prover cost / pure time, side by side — the panel that makes a divergence readable.

    They answer three different questions and are collected three different ways, so agreement is
    evidence and disagreement is a lead:
      - work: steps (ZisK) or cycles (SP1). Deterministic, host-independent — the metric every
        lever verdict on this branch was decided on.
      - prover cost: ZisK's COST model, or SP1's PGU (prover gas from the execution report -- not
        EVM gas), collected in the same instrumented pass. Each is weighted per instruction class,
        so it moves when the MIX changes even if work does not. The two units are per-backend and
        are not comparable to each other; only their ratios are.
      - pure time: work / measured per-guest emulator throughput (see PURE_MSTEPS_PER_S). Also
        mix-sensitive, through a completely independent mechanism (real instruction timings on the
        reference host) — which is what makes it worth showing next to cost rather than instead.
    """
    A, B, u = s['a_name'], s['b_name'], s['unit']
    w = s['ratio_median']
    c = s.get('cost_ratio_median')
    t = s.get('time_ratio_median')
    if not (c or t):
        return ''
    cls = lambda v: 'hi' if v >= 1 else 'lo'
    rows = [(f'work ({u})', w, s['n'],
             "<b>the verdict metric.</b> Deterministic and host-independent; every lever on this "
             "branch was accepted or rejected on it")]
    if c:
        unit = s.get('cost_unit', 'COST')
        why = ("<b>what the prover actually pays.</b> ZisK's COST model, same collection pass — "
               "weighted per instruction class, so it tracks the MIX, not the count"
               if unit == 'COST' else
               "<b>what the prover actually pays.</b> SP1's PGU (prover gas — unrelated to EVM gas), "
               "read from the execution report, so it tracks the MIX, not the count")
        rows.append((f'prover cost ({unit})', c, s.get('cost_n', s['n']), why))
    if t:
        rows.append(('pure exec time', t, s['n'],
                     "<b>emulator seconds on the reference host</b>, modelled as work ÷ measured "
                     "per-guest throughput. Not proving time, and not comparable across hosts"))
    body = ''.join(f"<tr><td>{lbl}</td><td class={cls(v)}>{x(v)}</td><td class=n>{n}</td>"
                   f"<td class=why>{why}</td></tr>" for lbl, v, n, why in rows)
    # The reading, computed rather than asserted.
    note = ""
    if c and t:
        spread = (max(w, c, t) / min(w, c, t) - 1) * 100
        if spread >= 3:
            heavier = 'denser' if (c > w and t > w) else 'lighter'
            note = (f"The three sit <b>{spread:.1f}%</b> apart, and cost and time move the SAME way "
                    f"against work — two independent mechanisms agreeing that "
                    f"<span class=cA>{A}</span>'s remaining instruction mix is {heavier} per "
                    f"{u[:-1] if u.endswith('s') else u} than <span class=cB>{B}</span>'s. That is "
                    f"the expected signature of optimisation: cheap work is what gets removed "
                    f"first, so what survives costs more each. It does not weaken the work ratio — "
                    f"it says where the next lever should look."
                    if (c > w) == (t > w) else
                    f"The three sit <b>{spread:.1f}%</b> apart and cost and time <b>disagree in "
                    f"direction</b> against work. That should not happen from mix alone — check "
                    f"the throughput table (it is measured per guest and goes stale when a guest "
                    f"changes materially) before reading anything into it.")
        else:
            note = (f"The three agree to within <b>{spread:.1f}%</b>: the instruction mix is "
                    f"essentially unchanged between the two guests, so the work ratio carries the "
                    f"whole story on this axis.")
    return (f"<div class=pane><h2>three axes: work, prover cost, pure time</h2>"
            f"<table class=cv><tr><th>axis</th><th>ratio</th><th>n</th><th>what it answers</th></tr>"
            f"{body}</table><p class=note>{note}</p></div>")


def _averages(s):
    """The four ways this page summarises a set of per-block ratios, side by side.

    It exists because a reader asked which average the headline is, and no single place on the page
    answered it: the cards give the median, the metric bars give aggregate ÷ aggregate, and the
    arithmetic mean appeared only in the terminal. Four numbers that differ by a few percent, three
    of them unlabelled, invite the assumption that they should agree.

    The row order is the recommendation. The GEOMETRIC mean is the one to quote when a single
    average of ratios is wanted (Fleming & Wallace, CACM 1986): averaging ratios additively is not
    a well-defined operation — mean(A/B) and mean(B/A) can BOTH exceed 1, so the arithmetic mean
    can report each of two guests as the more expensive one. The geometric mean cannot: it is an
    exact reciprocal under swapping, and 2× cancels 0.5×. The median stays the headline because it
    is robust to the off-pattern blocks this page lists by name.

    Whether that choice MATTERS is measured, never asserted: the closing line prints the actual
    gap between the geometric mean and the median. It is ~0.4% on the ZisK axis and ~5% on SP1,
    and that difference is not noise — it is the left tail of blocks where the reth guest runs
    BN254 in software, i.e. the two-regime structure the split table further down already names.
    So a wide gap here is a pointer to that section, not a reason to distrust either number."""
    gm, med, am, pooled = s['ratio_gmean'], s['ratio_median'], s['ratio_mean'], s['ratio_pooled']
    gsd = s.get('ratio_gsd') or 1.0
    ami = s.get('ratio_mean_inv') or (1 / am if am else 0)   # see ratio_mean_inv in summarize()
    A, B, u = s['a_name'], s['b_name'], s['unit']
    # Measured, not asserted: how far apart the four actually are here. A reader deciding whether the
    # choice of average matters needs the spread, and on a tight distribution the honest answer is
    # "it doesn't much".
    vals = [gm, med, am, pooled]
    spread = (max(vals) / min(vals) - 1) * 100
    # The gap that decides whether the closing sentence says "this matters" or "it doesn't": the
    # two statistics a reader would actually choose between, geometric mean and median.
    _gap = abs(gm - med) / med * 100
    cls = lambda v: 'hi' if v >= 1 else 'lo'
    rows = [
        ('geometric mean', gm,
         f"<b>the average to quote for a set of ratios.</b> Multiplicative: 2× and 0.5× cancel, and "
         f"it gives the same answer whichever guest you divide by — swapped it reads {x(1 / gm)}, an "
         f"exact reciprocal. One-sigma spread ×/÷ {gsd:.3f}"),
        ('middle block (median)', med,
         f"<b>the headline in the cards above.</b> Half the blocks sit either side; unmoved by the "
         f"off-pattern blocks listed further down"),
        ('arithmetic mean', am,
         f"<b>do not quote alone.</b> It depends on which guest you divide by: taken the other way "
         f"round it gives {x(ami)}, and {x(am)} × {x(ami)} = <b>{am * ami:.4f}</b> where an average "
         f"of ratios must give exactly 1 (the geometric mean above does). The wider the spread, the "
         f"worse it gets — far enough out, both directions read above 1 and each names the other "
         f"guest as the dearer one. Kept here only so the number is not missing"),
        ('total ÷ total', pooled,
         f"<b>the whole set's {u}, {A} against {B}.</b> Weighted by block size, so the largest "
         f"blocks dominate it — the right figure for total capacity, the wrong one for a typical "
         f"block"),
    ]
    # `table.cv` right-aligns its 2nd AND 3rd columns in the mono face (it was written for
    # number/number/prose). Here the 3rd column is prose, so it opts out inline rather than the
    # shared rule growing a per-table exception.
    _pr = "style='text-align:left;font-family:var(--sans)'"
    body = "".join(f"<tr><td>{lbl}</td><td class={cls(v)}>{x(v)}</td>"
                   f"<td {_pr}><i>{why}</i></td></tr>"
                   for lbl, v, why in rows)
    return (f"<div class=pane><h2>which average of the per-block ratios</h2>"
            f"<p class=note style='margin:0 0 10px'>Every row summarises the same "
            f"<b>{s['n']} per-block ratios</b> ({u} of <span class=cA>{A}</span> ÷ "
            f"<span class=cB>{B}</span>) — they differ because they answer different questions, not "
            f"because any is wrong. On this axis they span <b>{spread:.1f}%</b> end to end.</p>"
            f"<table class=cv><tr><th>statistic</th><th>ratio</th><th>what it answers</th></tr>"
            f"{body}</table>"
            f"<p class=note>Ratios are dimensionless and multiplicative, which is why the geometric "
            f"mean is the defensible average of a benchmark set and the arithmetic mean is not; the "
            f"median is preferred here on top of that, because a minority of blocks run 256-bit "
            f"curve arithmetic in software and are not drawn from the same population as the rest. "
            + (f"Here the two differ by <b>{_gap:.1f}%</b> ({x(gm)} against {x(med)}), so which one "
               f"is quoted <b>does</b> change the headline — and the reason is the left tail: the "
               f"geometric mean feels those blocks, the median does not. Read the split further "
               f"down before quoting either."
               if _gap >= 2 else
               f"Here the two differ by <b>{_gap:.1f}%</b> ({x(gm)} against {x(med)}), so on this "
               f"axis the choice is not load-bearing — which is a measurement, not a promise that "
               f"it holds on the next block set.")
            + f"</p></div>")


def _bars(items, A, B):
    """Side-by-side bars per metric, each pair scaled to its own max (shape, not absolute units)."""
    out = []
    for lbl, a, b, fmt in items:
        if a is None or b is None: continue
        m = max(a, b) or 1
        r = a / b if b else None
        out.append(
            f"<div class=mrow><div class=lbl><span>{lbl}</span><b class={'hi' if r and r>1 else 'lo'}>"
            f"{x(r)}</b></div>"
            f"<div class='bar bA'><i style='width:{100*a/m:.1f}%'></i></div>"
            f"<div class='bar bB'><i style='width:{100*b/m:.1f}%'></i></div>"
            f"<div class=bnum><span class=cA><span class=sw style='background:var(--gold)'></span>"
            f"{A} {fmt(a)}</span>"
            f"<span class=cB><span class=sw style='background:var(--blue)'></span>{B} {fmt(b)}</span>"
            f"</div></div>")
    return "".join(out)

def write_html(path, summaries, allrows, gas_map=None, tx_map=None, summary_href=None):
    gas_map = gas_map or {}; tx_map = tx_map or {}
    # The "one guest is the outlier" claim needs the other axis as a control, and _hist/_curve_note
    # only receive their own summary — hand them what they need rather than widen the signature.
    _nm = {(o['axis'], k): o[k + '_name'] for o in summaries for k in 'ab'}
    for o in summaries:
        o['_other_bn254'] = {p['axis']: p.get('bn254') for p in summaries if p is not o}
        o['_other_names'] = _nm
    pairs = " · ".join(f"{s['a_name']} vs {s['b_name']}" for s in summaries)
    # The heading used to be the fixed sentence "Two guest programs, one zkVM". That describes the
    # METHOD, which is still exactly what each section does — but it does not identify the page, and
    # it was byte-identical on every report the tool produced, so two reports written from different
    # axis sets were indistinguishable by title. Derive it instead.
    #
    # The signal is the directory of each axis's `a` ELF: two axes built from the same guest tree are
    # the same build measured on two backends. When every axis shares one, the page is one build held
    # against several references, and saying so is more useful than restating the method.
    _adirs = {os.path.dirname(AXES[s['axis']]['a']['elf']) for s in summaries if s['axis'] in AXES}
    _nw = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six'}
    # References are counted as GUESTS, not as axis sides. `monad-today` and `monad-today-sp1` are one
    # reference — the shipped guest — built for two backends, exactly as `monad-levers` and
    # `monad-levers-sp1` are one build. Counting the four `b` names said "four references", which
    # double-counts it. The ELF directory cannot collapse this pair (they sit in guests/monad-zisk and
    # guests/monad-sp1), so the backend suffix is what identifies the logical guest.
    _stem = lambda n: re.sub(r'-(sp1|zisk)$', '', n)
    _lead = ""
    if len(_adirs) == 1 and len(summaries) > 1:
        stem = os.path.basename(_adirs.pop())
        refs = sorted({_stem(s['b_name']) for s in summaries})
        nbk = len({AXES[s['axis']]['backend'] for s in summaries if s['axis'] in AXES})
        title = (f"<code>{stem}</code> against {_nw.get(len(refs), len(refs))} references, on "
                 f"{_nw.get(nbk, nbk)} backend{'s' if nbk != 1 else ''}")
        # Name them rather than only counting them: a bare count is unverifiable from the page, and
        # the first version of this line got it wrong in exactly that invisible way.
        _named = ", ".join(f"<code>{r}</code>" for r in refs[:-1]) + f" and <code>{refs[-1]}</code>"
        # Sections outnumber references whenever one reference is measured on both backends, which is
        # the case for the shipped guest. Saying so is what stops the section count being read as a
        # reference count — the mistake this line exists to prevent.
        _lead = (f"<b>Every section below measures the same build</b>, <code>{stem}</code>, against "
                 f"{_named}"
                 + (f" — {_nw.get(len(refs), len(refs))} references over "
                    f"{_nw.get(len(summaries), len(summaries))} sections, "
                    f"because a reference measured on both backends appears twice. "
                    if len(summaries) != len(refs) else ". "))
    else:
        # Unchanged, and deliberately not pluralised by section count: the "two" is the two guests
        # inside each section, not the number of sections.
        title = "Two guest programs, one zkVM"
    h = [f"<title>zkVM guest comparison — {pairs}</title>",
         f"<style>{_CSS}</style>", "<div class=wrap>",
         "<p class=eyebrow>zkvm-bench · execution comparison</p>",
         f"<h1>{title}</h1>",
         f"<p class=sub>{_lead}Work-units only compare <b>inside</b> a single zkVM (an SP1 cycle is not a ZisK "
         "step), so each section runs two <b>guest programs</b> on one backend over the same blocks. "
         "What differs is the <b>whole guest</b>: its execution engine, how it decodes its witness, how "
         "it handles state and precompiles — and each guest is fed its <b>own witness</b>, which differ "
         "in content as well as encoding. A ratio therefore attributes to the guest as a whole, never to "
         "one component of it. Ratios are <i>first ÷ second</i>: above 1× means the first guest costs "
         "more. These are execution and prover-work figures, <b>not</b> proving time. Generated by "
         "<code>profiling/compare.py</code>.</p>"]
    if summary_href:
        h.append(f"<p class=sub style='margin-top:8px'>Pressed for time? The one-page synthesis: "
                 f"<a href='{summary_href}' style='color:var(--accent)'>{summary_href}</a>.</p>")
    # Sticky axis bar: one line that always says which pair, on which zkVM, plus jump
    # chips. Rendered once; the label follows the scroll (see _AXBAR_JS).
    _chips = ''.join(
        f"<a href='#ax-{t['axis']}' data-for='{t['axis']}'>{t['axis']}</a>"
        for t in summaries)
    _f0 = summaries[0]
    h.append(f"<div class=axbar id=axbar><span class=cur id=axbar-cur>"
             f"<span class=bk>{AXES[_f0['axis']]['backend'].upper()}</span>"
             f"<span class=pair>{_f0['a_name']} vs {_f0['b_name']}</span></span>"
             f"<span class=jump>{_chips}</span></div>")
    for s in summaries:
        ax = s['axis']; A, B, u = s['a_name'], s['b_name'], s['unit']
        rows = allrows[ax]
        ratios = sorted(r['a']['work'] / r['b']['work'] for r in rows.values() if r['b']['work'])
        pwu, pwk = s.get('pw_unit'), ('cost' if ax == 'zisk' else 'pgu')
        pct = (s['ratio_median'] - 1) * 100
        h.append(f"<section id='ax-{ax}' data-axis='{ax}' "
                 f"data-backend='{AXES[ax]['backend'].upper()}' data-pair='{A} vs {B}'>")
        h.append(f"<div class=axhead><span class=nm>{ax.upper()}</span>"
                 f"<span class=vs>{A} &nbsp;vs&nbsp; {B}</span></div>")
        # "N blocks between X and Y" reads as contiguous. It is not: a block runs only when BOTH
        # guests have an input, and the reth-side .bin is missing for ~10% of the span. State the
        # gap rather than let the reader assume. (Checked: the ratio is nearly independent of block
        # size — corr(ratio, witness size) = -0.04 on SP1, -0.20 on ZisK, and dropping the largest
        # 10% of blocks moves the median by 0.9% / 0.03% — so the gap does not bias the figures.)
        _bs = sorted(int(b) for b in rows)
        _gap = (_bs[-1] - _bs[0] + 1) - len(_bs)
        h.append(f"<p class=sub style='margin-bottom:14px'><b>{s['n']} blocks</b> between "
                 f"<b>{_bs[0]}</b> and <b>{_bs[-1]}</b>, each executed by both "
                 f"guests on the {AXES[ax]['backend'].upper()} emulator."
                 + (f" The span is <b>not contiguous</b>: {_gap} of its "
                    f"{_bs[-1]-_bs[0]+1} block numbers ({_gap/(_bs[-1]-_bs[0]+1)*100:.0f}%) are absent, "
                    f"because a block runs only when <i>both</i> guests have an input and the "
                    f"{B}-side one is missing there. Checked for selection bias: the ratio is nearly "
                    f"independent of block size, and dropping the largest 10% of blocks moves the "
                    f"median by under 1%." if _gap else "") + "</p>")
        h.append(f"<div class=legend><span><span class=sw style='background:var(--gold)'></span>{A}</span>"
                 f"<span><span class=sw style='background:var(--blue)'></span>{B}</span></div>")
        # headline cards — every value symmetric and colour-coded, every label spelled out
        h.append("<div class=cards>")
        h.append(f"<div class='card hero'><div class=k>median ratio</div><div class=v>{x(s['ratio_median'])}"
                 f"</div><div class='u blk'>{u} of <span class=cA>{A}</span> ÷ <span class=cB>{B}</span> "
                 f"on the middle block: half the blocks sit above this, half below</div></div>")
        if pwu:
            h.append(f"<div class='card hero'><div class=k>prover-work ratio</div>"
                     f"<div class=v>{x(s.get('pw_ratio_median'))}</div>"
                     f"<div class='u blk'>same, counted in {pwu} (trace area) instead of {u} — closer to "
                     f"what proving will actually cost</div></div>")
        h.append(f"<div class=card><div class=k>median {u}</div>"
                 f"<div class=duo><span class=cA>{s['a_median']/1e6:,.0f}M</span>"
                 f"<span class=sep>vs</span><span class=cB>{s['b_median']/1e6:,.0f}M</span></div>"
                 f"<div class='u blk'>instructions the emulator counts — deterministic, same on any "
                 f"machine</div></div>")
        if s.get('a_nsecs_median'):
            ov = s.get('gas_pass_overhead')
            h.append(f"<div class=card><div class=k>median exec time</div>"
                     f"<div class=duo><span class=cA>{s['a_nsecs_median']:.2f}s</span>"
                     f"<span class=sep>vs</span><span class=cB>{s['b_nsecs_median']:.2f}s</span></div>"
                     f"<div class='u blk'>pure execution: work ÷ this guest's measured emulator "
                     f"throughput. Raw wall-clock is not comparable — it carries a 0.6–1.7 s "
                     f"ELF→ROM conversion per run plus campaign parallelism"
                     + (f"; SP1's gas-estimation pass would add ×{ov:.2f}" if ov else "")
                     + "</div></div>")
        elif s.get('a_secs_median'):
            h.append(f"<div class=card><div class=k>median exec time</div>"
                     f"<div class=duo><span class=cA>{s['a_secs_median']:.2f}s</span>"
                     f"<span class=sep>vs</span><span class=cB>{s['b_secs_median']:.2f}s</span></div>"
                     f"<div class='u blk'>pure execution: work ÷ this guest's measured emulator "
                     f"throughput on the reference host — not proving time</div></div>")
        h.append(f"<div class=card><div class=k>block-to-block spread</div><div class=v>±{s['cv']:.1f}"
                 f"<span class=u>%</span></div><div class='u blk'>how far the ratio typically swings from "
                 f"one block to the next, as a share of its own average. Low = the gap is a property of "
                 f"the guests; high = it depends on what the block does (per-block values are charted "
                 f"below)</div></div>")
        h.append(f"<div class=card><div class=k>blocks off the pattern</div><div class=v>"
                 f"{len(s.get('_outliers', []))}<span class=u> / {s['n']}</span></div>"
                 f"<div class='u blk'>blocks whose ratio is nothing like the rest — listed at the "
                 f"bottom, with what stands out about each</div></div>")
        h.append("</div>")
        # verdict
        verdict = (f"<b class=cA>{A}</b> costs <b>{abs(pct):.1f}% {'more' if pct>0 else 'less'}</b> "
                   f"{u} than <b class=cB>{B}</b> on the median block of this range.")
        if pwu and s.get('pw_ratio_median'):
            pw_pct = (s['pw_ratio_median'] - 1) * 100
            # Drawing "the extra work is dearer/cheaper to prove" from ANY difference overstated a
            # 0.4pp gap on SP1 (1.225x PGU vs 1.221x cycles) as a finding. Only call the direction
            # when the two ratios differ by more than 2% of each other; ZisK's 1.361 vs 1.462 (7%)
            # clears that, SP1's does not.
            rel = (s['pw_ratio_median'] - s['ratio_median']) / s['ratio_median']
            verdict += f" Measured as <b>prover work</b> the gap is <b>{pw_pct:+.1f}%</b>"
            if abs(rel) > .02:
                verdict += (f" — {'lower' if rel < 0 else 'higher'} than the {u} gap, i.e. the extra "
                            f"work sits in operations that are "
                            f"{'cheaper' if rel < 0 else 'dearer'} than average to prove.")
            else:
                verdict += (f", within {abs(rel)*100:.1f}% of the {u} gap — too close to call either "
                            f"way, so the extra work is not concentrated in operations that are "
                            f"notably cheap or dear to prove.")
        h.append(f"<div class=insight>{verdict}</div>")
        # distribution + metric bars
        h.append("<div class=grid2>")
        h.append(f"<div class=pane><h2>how many blocks land at each ratio</h2>"
                 # green first, then red — same left-to-right order as the bars themselves
                 f"<div class=legend style='margin:2px 0 4px'>"
                 f"<span><span class=sw style='background:var(--green)'></span>{A} cost less "
                 f"({sum(1 for r in ratios if r < 1)})</span>"
                 f"<span><span class=sw style='background:var(--red)'></span>{A} cost more "
                 f"({sum(1 for r in ratios if r >= 1)})</span></div>{_hist(ratios, s, rows, gas_map, tx_map)}"
                 f"<p class=note>One tall narrow clump ⇒ the gap is a stable property of the two guests. "
                 f"A wide or two-peaked shape ⇒ it depends on what the block actually does, and the "
                 f"median alone hides that.</p></div>")
        items = [(f"median {u}", s['a_median'], s['b_median'], n),
                 (f"total {u}", s['a_total'], s['b_total'], n),
                 (f"{u} per Mgas", s.get('a_per_mgas'), s.get('b_per_mgas'), n)]
        if pwu: items.append((f"median {pwu} (prover)", s.get('a_pw_median'), s.get('b_pw_median'), n))
        # This row published a_secs_median — SP1's GAS-ON wall clock, inflated x1.79 by the
        # gas-estimation pass (8.35s where the honest figure is 4.66s). That pass is pure
        # instrumentation overhead no other stack has, and it once reordered the two guests. Prefer
        # the --no-gas median when it exists, and say in the label which one is on screen.
        if s.get('a_nsecs_median'):
            items.append(("median exec secs (gas pass off)", s['a_nsecs_median'],
                          s['b_nsecs_median'], lambda v: f"{v:.2f}s"))
        elif s.get('a_secs_median'):
            items.append(("median exec secs", s['a_secs_median'], s['b_secs_median'],
                          lambda v: f"{v:.2f}s"))
        # No prose here: the labels carry the numbers. The one thing a careful reader may wonder —
        # why these ratios differ slightly from the cards' — lives in the heading's tooltip:
        # these are aggregate ÷ aggregate (medians/totals), the cards are the median of per-block
        # ratios, and dividing two medians ≠ the median of the divisions. Both are wanted: the
        # aggregate answers "how much more work in total", the per-block median "what a typical
        # block costs". Each pair of bars is scaled to its own max, so bar length is only
        # comparable within a row.
        # The gap between the two statistics was documented only in a title= tooltip and called
        # "slight". Measured here it reaches 6% (cycles 1.221x vs 1.151x on SP1), which is enough
        # to change a quoted headline, and a tooltip is invisible in a printed synthesis. State it
        # in the body, with the actual spread.
        _agg = s['a_median'] / s['b_median'] if s['b_median'] else None
        _dev = abs(s['ratio_median'] - _agg) / _agg * 100 if _agg else 0
        h.append(f"<div class=pane><h2>{A} <span style='color:var(--gold)'>▬</span> vs {B} "
                 f"<span style='color:var(--blue)'>▬</span></h2>"
                 f"<p class=note style='margin:0 0 12px'>Each ratio here is <b>aggregate ÷ "
                 f"aggregate</b> — the two medians (or totals) shown on the row, divided. The cards "
                 f"above instead give the <b>median of the per-block ratios</b>, and dividing two "
                 f"medians is not the median of the divisions: on this axis the two differ by "
                 f"<b>{_dev:.1f}%</b> ({x(_agg)} here vs {x(s['ratio_median'])} in the cards). Both "
                 f"are wanted — the aggregate answers <i>how much more work in total</i>, the "
                 f"per-block median <i>what a typical block costs</i> — so quote whichever you mean "
                 f"and say which. The full set of averages, and which to quote, is the table "
                 f"below. Each pair of bars is scaled to its own maximum, so bar length is "
                 f"comparable only within a row.</p>{_bars(items, A, B)}</div>")
        h.append("</div>")
        # Directly under the two panes that each show a DIFFERENT summary of the same ratios (the
        # cards' median above, aggregate ÷ aggregate to the left) — the question "which of these is
        # the average?" is raised there, so it is answered there.
        h.append(_three_axes(s))
        h.append(_averages(s))
        # ── where the gap comes from ──
        # The question the distribution raises but cannot answer: not "how big is the gap" but
        # "what is it made of". Both halves are medians ACROSS blocks (see summarize).
        if s.get('families') or s.get('insn_ratios'):
            h.append("<div class=grid2>")
            if s.get('families'):
                fa, fb, _raw_a, _raw_b, picks = s['families']   # _raw_* are SAMPLES
                # on SP1 (fam_scale~200x); never mix them with the rescaled fa/fb.
                base = s['ratio_median']
                fams = sorted(set(fa) | set(fb), key=lambda k: -(fa.get(k, 0)))
                mx = max([fa.get(k, 0) for k in fams] + [1])
                # Kept short on purpose: why function-name classification rather than modules, why
                # a stratified sample, and why the SP1 figures are scaled all live in the code —
                # hotspots.FAMILIES, profile_blocks() and the picks/_scaled block in main().
                h.append(f"<div class=pane><h2>what kind of work, and how much</h2>"
                         f"<p class=note style='margin:0 0 10px'>Instructions per block, grouped by "
                         f"what the code is doing. Mean over <b>{len(picks)} blocks</b> sampled "
                         f"across the ratio range. <span class=cA>{A}</span> above, "
                         f"<span class=cB>{B}</span> below.</p>")
                # What the bar encodes, named. Length and colour answer different questions here and
                # the page said neither; the biggest family is full width, which is why a 46% share can
                # sit beside a 100% bar — different denominators, one glyph. Above the table, not below:
                # a key is read before the rows, and the same position on all three bar tables.
                _big = max(fams, key=lambda k: fa.get(k, 0))
                # The grey state is only described when a row actually has it, so the key never lists a
                # colour the reader cannot find in the table.
                # exactly the rows where the ratio is None, i.e. the denominator is zero — matching the
                # render condition rather than approximating it
                _nz = [k for k in fams if not fb.get(k, 0)]
                _grey = (f" <span class=na>grey</span> where no ratio exists because "
                         f"<span class=cB>{B}</span> reports nothing here "
                         f"({len(_nz)} row{'s' if len(_nz) > 1 else ''}) — not a win, a missing "
                         f"measurement." if _nz else "")
                h.append(f"<p class=hint style='margin:0 0 5px'><b>bar</b> — length: what "
                         f"<span class=cA>{A}</span> spends here, against its own largest family "
                         f"(<i>{_big}</i>, full width). colour: <span class=hi>red</span> where "
                         f"<span class=cA>{A}</span> spends more than <span class=cB>{B}</span> in this "
                         f"family, <span class=lo>green</span> where less.{_grey} The share column "
                         f"divides by each guest's total, the bar by the largest family — so they do "
                         f"not match.</p>")
                # The measured dispersal of the byte/bit family, written by the same build that
                # eliminated the symbol. Optional: absent file falls back to the old wording.
                try:    _bd = json.load(open(os.path.join(HERE, 'results', 'bswap-dispersal.json')))
                except Exception: _bd = None
                _vd = {}
                if ax == 'zisk' and os.path.exists(VERDICT):   # `ax` is the axis STRING in write_html
                    try:    _vd = json.load(open(VERDICT))
                    except Exception: _vd = {}
                # No rescaling here. The instruction columns are already per-block figures over the
                # profiled sample, so the gap is their difference — anything else breaks the row's own
                # arithmetic: rescaling by the MEDIAN block (281.1 M against a 292.0 M profiled mean,
                # 3.9 % apart) showed +1,265,532 for a family where the reth guest is 0 and the Monad
                # guest is 1,314,562. A reader who subtracts the two visible numbers must land on the
                # gap.
                h.append(f"<table><tr><th>kind of work</th><th>instructions</th>"
                         f"<th>share of<br>own work</th><th>ratio</th>"
                         f"<th>gap in {u}<br>(profiled block)</th>"
                         + (f"<th>ratio with reth's<br>inlining undone</th>" if _vd else "")
                         + f"<th></th></tr>")
                # Shares as well as absolute counts, because the absolute figures are NOT safe to
                # compare across backends: on a sampling profiler they carry the scale factor, and
                # any family that dominates one backend (e.g. an allocator at ~46% of both guests'
                # work) compresses every other share there. A family's share of its OWN guest's work
                # is scale-free and is the only unit that means the same thing on both axes.
                # Divide by the sum of the same dict — mixing a rescaled numerator with the raw
                # attributed total once produced 9182%.
                _sfa, _sfb = sum(fa.values()) or 1, sum(fb.values()) or 1
                # Every family the table can show must have an entry here, and no entry may name a
                # family that does not exist: `signature recovery` sat here for a family hotspots never
                # defines, and three real families had no description at all. Asserted below.
                doc = {
                    # Measured, not assumed: 0.00% of this family is error-result machinery. Those
                    # symbols classify with whatever payload type they carry — a result holding a trie
                    # node lands in state/trie — so naming them here was wrong for every guest, not
                    # just this pair.
                    'containers / abstraction':
                        'Iterators, hash containers and their key hashing, byte buffers, growable '
                        'vectors, variant dispatch (STL or Rust) — machinery carried around the logic, '
                        'not the logic itself. Error-result wrappers are NOT here: a demangled name '
                        'starts with the return type, so they classify with the payload they carry.',
                    'state / trie': 'Merkle-trie traversal, node decoding, RLP, storage/account lookups. '
                                    'Also holds error-result wrappers whose payload is a trie type.',
                    'witness decoding':
                        'Parsing the witness: RLP metadata, compact nibbles, deserialisation. Measured, '
                        'it also holds RLP *encoding* — the name pattern matches both directions — so '
                        'read a ratio here as "RLP work", not "decode work".',
                    'EVM interpreter': 'Opcode dispatch and execution of the block\u2019s transactions.',

                    '256-bit arithmetic': 'mulmod/addmod/division on 256-bit words, done in software.',
                    'memory / allocation': 'memcpy/memset, allocator work.',
                    'elliptic-curve crypto':
                        'secp256k1 for ecrecover, plus BN254/BLS field and pairing arithmetic for the '
                        'precompiles — software or precompile-backed, which is what its size tells you.',
                    'block / consensus logic':
                        'Block-level rules rather than execution: transaction and header validation, '
                        'base and blob fee, receipts, bloom.',
                    'runtime plumbing':
                        'Language runtime rather than protocol work — panic paths, formatting, sorting, '
                        'critical sections, destructor dispatch.',
                    'other': 'Names no family pattern matched.',
                }
                # Read from the measurement, never typed: this sentence carried "30.4 M" and
                # "1.30x" from an early one-block pilot long after the 55-block run said 40.5 M and
                # 1.02x. It then contradicted the corrected column three lines above it AND argued
                # against the permutation count it cites as corroboration.
                _hv = _vd.get('hashing (keccak/sha)') if _vd else None
                _hs_ = (_hv or {}).get('steps') or {}
                _hnum = (f"moves its hashing from {_hs_['ship']/1e6:.1f} M to {_hs_['ni']/1e6:.1f} M "
                         f"steps against Monad's {_hs_['monad']/1e6:.1f} M, i.e. the ratio collapses "
                         f"from {x(_hv['ship'])} to <b>{x(_hv['ni'])}</b>" if _hs_ else
                         "collapses this ratio")
                WARN = {'hashing (keccak/sha)':
                        "<b>This row's ratio is an attribution artefact — measured, not suspected.</b> "
                        "It counts the instructions AROUND the hash, not the hash, and a guest that "
                        "inlines its wrapper charges them to its callers. Rebuilding the reth guest "
                        f"with inlining suppressed {_hnum} — and the permutation count below, which "
                        "depends on no symbol at all, agrees independently. Two methods sharing no "
                        "machinery find the two guests hashing about equally.",
                        'byte/bit manipulation':
                        "Not comparable between guests: __bswapdi2 is an OUTLINED libgcc function that "
                        "C++ calls, while Rust's swap_bytes/to_be_bytes are inlined into their callers "
                        "and never appear as a symbol. Both guests do this work (RISC-V has no "
                        "byte-swap instruction); only Monad's is visible."
                        # "Read the absolute figure" told the reader to look at a number without
                        # saying what it is a number OF. The dispersal below answers that: it is not
                        # a guess about where the work belongs, it is where the work WENT when the
                        # symbol was eliminated in a rebuild.
                        + (f" <b>Where this work belongs:</b> rebuilding the Monad guest with every "
                           f"byte swap inlined makes this symbol vanish, and the families that "
                           f"absorb it are "
                           + ", ".join(f"{k} {v * 100:.0f}%" for k, v in
                                       list(_bd['key'].items())[:3])
                           + f" — measured, not apportioned. The reth guest's equivalent work is "
                           f"already spread that way, which is exactly why this row reads zero for "
                           f"it and why its interpreter and trie families are slightly inflated "
                           f"against Monad's." if _bd and _bd.get('key') else
                           " Read the absolute figure.")
                        # Tested, not argued. The no-inline rebuild made every other hidden family
                        # appear (hashing 11x, containers 21x) and left this one at zero, which is
                        # what distinguishes "the classifier cannot see it" from "the work is not
                        # there". swap_bytes is an LLVM intrinsic, so no inlining setting outlines it.
                        + (f" Tested: rebuilding {B} with inlining suppressed surfaced every other "
                           f"hidden family and left this one absent across all "
                           f"{(_vd.get('_meta') or {}).get('ni_blocks', '')} profiled blocks — so no "
                           f"build of that guest will ever populate this row."
                           if (_vd.get('_meta') or {}).get('ni_blocks') else "")}
                # Every family the table can show must be described by doc OR by WARN — both feed
                # the title= — and no entry may name a family hotspots never defines. `signature
                # recovery` sat in doc for a non-existent family, and three real families had no
                # description at all; nothing checked either.
                _known = {n for n, _ in _hotspots().FAMILIES} | {'other'}
                _orphan = (set(doc) | set(WARN)) - _known
                _undesc = set(fams) - set(doc) - set(WARN)
                if _orphan or _undesc:
                    print(f"  [warn] family descriptions: naming nothing {sorted(_orphan)}, "
                          f"undescribed {sorted(_undesc)}")
                for k in fams:
                    va, vb = fa.get(k, 0), fb.get(k, 0)
                    r = (va / vb) if vb else None
                    tip = doc.get(k, '')
                    warn = WARN.get(k)
                    # built out here: a literal cannot be split across lines inside an f-string
                    # expression, and inlining it was a syntax error that silently left the previous
                    # HTML in place while its checks ran against the stale file
                    _rt = f"ratio {x(r)}" if r is not None else f"no ratio — {B} reports none here"
                    # absolute, from each guest's own median work — the only figure here that adds up
                    # to the overall gap
                    _gap = va - vb
                    h.append(f"<tr><td style='text-align:left'"
                             # A title attribute renders as plain text, so markup in these strings
                             # showed up literally as "<b>…</b>" in the tooltip. The same strings are
                             # reused as prose below the table, where the markup IS wanted — so strip
                             # here rather than writing two versions that would drift apart.
                             + (f" title=\"{_plain(warn or tip)}\"" if (warn or tip) else "")
                             + f">{'⚠ ' if warn else ''}{k}</td>"
                             f"<td><span class=cA>{n(va)}</span><br>"
                             f"<span class=cB>{n(vb)}</span></td>"
                             f"<td><span class=cA>{va/_sfa*100:.2f}%</span><br>"
                             f"<span class=cB>{vb/_sfb*100:.2f}%</span></td>"
                             # `na` where no ratio exists, for the number as well as the bar: with the
                             # old `hi if r and r >= 1 else lo` a missing ratio was styled green.
                             # `r is None`, NOT `not r`: r == 0.0 is a real measurement (this guest
                             # spends nothing where the other does) and must stay green, not go grey.
                             f"<td class={'na' if r is None else ('hi' if r >= 1 else 'lo')}>"
                             f"{x(r)}</td>"
                             f"<td class={'hi' if _gap > 0 else 'lo'}>{'+' if _gap > 0 else ''}"
                             f"{n(round(_gap))}</td>"
                             + (_verdict_cell(_vd.get(k)) if _vd else "")
                             # The bar carries TWO variables — length and colour — and neither is the
                             # column it sits next to, so each row states its own reading on hover.
                             + f"<td style='width:28%' title=\"{k}: {A} {n(va)}, "
                             f"{va/_sfa*100:.1f}% of its own work · {B} {n(vb)}, "
                             f"{vb/_sfb*100:.1f}% of its own · {_rt}\">"
                             f"<span class='rbar"
                             f"{' na' if r is None else (' over' if r >= 1 else '')}' "
                             f"style='width:{max(2, 100*va/mx):.0f}%'></span></td></tr>")
                h.append("</table>")
                if _vd:
                    # `_meta` carries the run's own parameters, not a family — iterating the file
                    # blind would count it as one and read a 'factor' it does not have.
                    _fam_v = {k: v for k, v in _vd.items() if k != '_meta'}
                    _mt = _vd.get('_meta') or {}
                    # Same classifier as the cells, so the counts here always equal the words there.
                    _cl = {k: _vclass(v['factor']) for k, v in _fam_v.items()}
                    _bad = [k for k, c in _cl.items() if c.startswith('relocated')]
                    _ok = [k for k, c in _cl.items() if c == 'agrees']
                    _edge = [k for k, c in _cl.items() if c == 'borderline']
                    # Which families the reth guest moved AGAINST ITSELF, from the same file: this is
                    # the evidence that "relocated" is relocation and not noise, and it is the answer
                    # to the reader's question of which of the two columns to believe.
                    _mv = sorted(((k, v['steps']['ni'] / v['steps']['ship'])
                                  for k, v in _fam_v.items()
                                  if (v.get('steps') or {}).get('ship')),
                                 key=lambda t: -t[1])
                    _gain = [(k, f_) for k, f_ in _mv if f_ > 2][:2]
                    _lose = [(k, f_) for k, f_ in _mv if f_ < 0.7][-1:]
                    # Built out here rather than nested in the f-string: quoting a dict key inside an
                    # f-string expression is what pushed an earlier version into a chr() workaround.
                    _edgetxt = ', '.join(
                        f"{k} moves by a factor of "
                        f"{max(_fam_v[k]['factor'], 1 / _fam_v[k]['factor']):.2f}" for k in _edge)
                    h.append(
                        f"<p class=note><b>Which of the two columns to read.</b> The word in each cell "
                        f"describes the <b>move between the two attributions</b>, not the quality of "
                        f"the number beside it. Where a family <i>agrees</i>, both attributions found "
                        f"the same thing and either column can be read. Where it says "
                        f"<i>relocated</i>, inlining had charged that family's work to a different "
                        f"family, and <b>this column is the one that says where the work lives</b> — "
                        f"the shipped ratio is then reporting which compiler inlined more."
                        + (f" Measured against the reth guest's own two builds, "
                           + " and ".join(f"<code>{k}</code> gains {f_:.1f}×" for k, f_ in _gain)
                           + (f" while <code>{_lose[0][0]}</code> keeps only "
                              f"{_lose[0][1] * 100:.0f}% of its steps — that family had been hosting "
                              f"them." if _lose else ".") if _gain else "")
                        + f"<br>The reth guest "
                        f"was rebuilt with inlining suppressed (<code>--inline-threshold=0</code>) and "
                        f"re-profiled, so every family's ratio is known under two attributions. This "
                        # Naming the sample matters: these ratios come from the blocks where all three
                        # profiles exist, not from the table's own sample, so they do not equal the
                        # ratio column to their left and a reader must not try to reconcile them.
                        f"column is measured on the <b>{_mt.get('blocks', '?')} blocks</b> profiled "
                        f"under all three binaries, so it will not match the ratio column beside it "
                        f"exactly — that one uses the full profiled sample. <b>{len(_ok)} agree</b> — "
                        f"the EVM interpreter reads "
                        f"{x(_vd['EVM interpreter']['ship'])} shipped and "
                        f"{x(_vd['EVM interpreter']['ni'])} without inlining — and "
                        f"<b>{len(_bad)} relocated</b>: {', '.join(_bad)}."
                        # Named, not folded into either count: these sit within 0.05 of the cutoff, so
                        # calling them either way would be an artefact of where the line was drawn.
                        + (f" {len(_edge)} sit on the boundary and are marked "
                           f"<i>borderline</i> — {_edgetxt} against a cutoff at 1.25, so calling them "
                           f"either way would be an artefact of where the line was drawn." if _edge
                           else "")
                        + f" Hashing goes from "
                        f"{x(_vd['hashing (keccak/sha)']['ship'])} to "
                        f"{x(_vd['hashing (keccak/sha)']['ni'])}, which the permutation count below "
                        f"independently corroborates. A moving ratio is not a cost difference: it says "
                        f"the two compilers inlined differently.<br>"
                        f"<b>The displacement is entirely the reth guest's</b>, which is what makes the "
                        f"corrected column usable rather than merely different. The Monad guest was "
                        f"given the same treatment — both its toolchains — and no family moves more "
                        f"than <b>0.72 pp</b> (that one being <i>other</i>; every real family stays "
                        f"under 0.6 pp). Its C++ emits helpers as real symbols where Rust folds them "
                        f"into callers, so its attribution was already faithful.<br>"
                        # This paragraph used to end "state / trie goes 0.86x -> 1.89x, so the Monad
                        # guest is genuinely behind there". True of the row, wrong about the guest: the
                        # same de-inlining sends containers 6.98x -> 0.34x, and grouped they are at
                        # parity. A corrected ratio is still a per-LABEL ratio, and relocation moves
                        # work between labels — so a single corrected row cannot carry a verdict.
                        f"<b>A corrected row is still one label, and relocation moves work between "
                        f"labels.</b> <code>state / trie</code> goes "
                        f"{x(_fam_v['state / trie']['ship'])} → {x(_fam_v['state / trie']['ni'])}, "
                        f"which taken alone reads as the Monad guest falling behind. The same "
                        f"de-inlining sends <code>containers / abstraction</code> "
                        f"{x(_fam_v['containers / abstraction']['ship'])} → "
                        f"{x(_fam_v['containers / abstraction']['ni'])} — the opposite direction, "
                        f"because reth's trie functions had inlined their container and hashing "
                        f"helpers."
                        + ("".join(
                            f" Grouped, <b>{k}</b> reads {x(g['ship'])} shipped and "
                            f"<b>{x(g['ni'])}</b> de-inlined."
                            for k, g in (_mt.get('groups') or {}).items()))
                        + f" So the two guests spend comparable total work on that path and "
                        f"distribute it differently across these labels; neither row is a verdict on "
                        f"its own. The <i>reader trio</i> figure is the robust one — the relocation "
                        f"happens inside it, so it barely moves between the two attributions.<br>"
                        f"<b>The no-inline column is diagnostic, never a cost.</b> That build spends "
                        + (f"{(_mt['inflation'] - 1) * 100:.0f}% more steps" if _mt.get('inflation')
                           else "more steps")
                        + f" — those are the calls inlining had removed — so it answers "
                        f"<i>where does this code live</i>, not <i>what does it cost</i>. Some blocks "
                        f"are missing from it: the emulator's profiling mode fails non-deterministically "
                        f"on that binary, and the blocks that did run are the same size as the "
                        f"population, so what is lost is sample size and not representativeness.</p>")
                # The two families a name-based taxonomy cannot compare, said out loud rather than
                # left in a title= nobody hovers — each with the counter that does not depend on
                # symbols existing.
                _sub = {}
                # `kec` is only filled by the ZisK emulator; SP1 carries the same count under
                # sys['KECCAK_PERMUTE'], and reading only `kec` silently dropped the substitute on
                # that whole axis.
                _perm = lambda r: r.get('kec') or (r.get('sys') or {}).get('KECCAK_PERMUTE')
                _kp = [(_perm(r['a']), _perm(r['b'])) for r in rows.values()
                       if _perm(r['a']) and _perm(r['b'])]
                if _kp:
                    _sub['hashing (keccak/sha)'] = (
                        f"Comparable substitute: <b>keccak permutations</b>, counted not attributed — "
                        f"<span class=cA>{n(statistics.median(v[0] for v in _kp))}</span> vs "
                        f"<span class=cB>{n(statistics.median(v[1] for v in _kp))}</span> per block, "
                        # The quotient of the two medians PRINTED HERE, not the median of the per-block
                        # ratios: the latter read 1.079x beside a pair that divides to 1.086x, and the
                        # same two counts appear again further down the page quoted as 1.086x — the
                        # page contradicted itself on identical inputs. 0.7% apart, and worth nothing
                        # next to a reader being unable to reproduce the number in front of them.
                        f"<b>{x(statistics.median(v[0] for v in _kp) / statistics.median(v[1] for v in _kp))}</b>"
                        f". Both guests call the "
                        f"same precompile and its trace cost is identical, so the row above measures "
                        f"the wrapper each guest shows, not the hashing each guest does.")
                _bitops = ('sll', 'srl', 'and', 'or')
                _bp = {o: [(r['a']['opsn'][o], r['b']['opsn'][o]) for r in rows.values()
                           if (r['a'].get('opsn') or {}).get(o) and (r['b'].get('opsn') or {}).get(o)]
                       for o in _bitops}
                _bp = {o: v for o, v in _bp.items() if len(v) > len(rows) * 0.5}
                if _bp:
                    _bits = " · ".join(
                        f"<code>{o}</code> {x(statistics.median(a / b for a, b in v))}"
                        for o, v in _bp.items())
                    _sub['byte/bit manipulation'] = (
                        f"Comparable substitute: <b>opcode counts</b>, which exist whether or not a "
                        f"symbol does — {_bits}. The reth guest does this work too and at a similar "
                        f"rate; RISC-V has no byte-swap instruction, so both sides pay it in shifts "
                        f"and masks. Only Monad's is outlined into <code>__bswapdi2</code> and "
                        f"therefore visible to a name-based family.")
                for k in fams:
                    if k in WARN and k in _sub:
                        h.append(f"<p class=note style='margin:8px 0 0'><b>⚠ {k}</b> — {WARN[k]}<br>"
                                 f"{_sub[k]}</p>")
                # Micro-synthesis: a family that dominates BOTH guests says something about the
                # backend, not about either guest — and it mechanically compresses every other
                # share on this axis, which is what makes cross-axis share comparisons misleading.
                # Data-driven so it appears on whichever axis it is true of (today: SP1's allocator).
                # Shares must divide by the sum of the SAME dict: fa/fb are rescaled to estimated
                # instructions while ta/tb are the raw attributed totals, so mixing them overstated
                # every share by the scale factor (91.8% instead of 45.5%).
                sfa, sfb = sum(fa.values()), sum(fb.values())
                if sfa and sfb:
                    shr = {k: (fa.get(k, 0) / sfa, fb.get(k, 0) / sfb) for k in fams}
                    dom = max(fams, key=lambda k: min(shr[k]))
                    sa_, sb_ = shr[dom]
                    if min(sa_, sb_) > .25 and dom != 'EVM interpreter':
                        h.append(
                            f"<p class=env><b>Read first: on {s['axis'].upper()} the biggest single cost is "
                            f"{dom} — for both guests.</b> {sa_*100:.0f}% of "
                            f"<span class=cA>{A}</span>'s work and {sb_*100:.0f}% of "
                            f"<span class=cB>{B}</span>'s, more than the EVM itself. That is a property "
                            f"of this <i>backend</i>, not of either client, so it is the one number here "
                            f"that a fix would improve for <i>every</i> {s['axis'].upper()} guest. "
                            f"<em>It also inflates every other family's share on this axis (they divide "
                            f"by a total this family dominates) — which is why a family's ratio is "
                            f"comparable within an axis but its share is not comparable across "
                            f"axes.</em></p>")
                # A family ratio built on instruction counts describes the WRAPPER whenever both
                # guests share a precompile — read alone, `hashing 12x` says "this guest hashes in
                # software" when it may be the only one using the accelerated path. Split it.
                # A cross-guest per-call ratio is only meaningful if BOTH guests expose the
                # wrapper as visible code. Above this factor, assume they do not.
                INLINE_Z = 3.0
                if s.get('percall_hash'):
                    pa, pb = s['percall_hash']['a'], s['percall_hash']['b']
                    ka = statistics.median([r['a'].get('kec') or (r['a'].get('sys') or {}).get(
                        'KECCAK_PERMUTE') or 0 for r in rows.values()])
                    kb = statistics.median([r['b'].get('kec') or (r['b'].get('sys') or {}).get(
                        'KECCAK_PERMUTE') or 0 for r in rows.values()])
                    _r = (pa[0] / pb[0]) if pb[0] else None
                    _suspect = _r is not None and (_r > INLINE_Z or _r < 1 / INLINE_Z)
                    h.append(
                        f"<p class=env><b>The hashing row is about the call, not the hash.</b> Both "
                        f"guests reach the same keccak precompile, so the hash itself costs them the "
                        f"same per permutation and only the <i>number</i> of permutations "
                        f"(<span class=cA>{n(ka)}</span> / <span class=cB>{n(kb)}</span>, "
                        f"{x(ka/kb) if kb else '—'}) changes that part. What the family row measures "
                        f"is the instructions <i>around</i> each permutation:</p>"
                        f"<table class=cv><tr><th></th><th>instructions<br>per call</th>"
                        f"<th>range over<br>profiled blocks</th><th>varies with<br>payload?</th></tr>"
                        + "".join(
                            f"<tr><td><span class=c{S}>{N}</span></td>"
                            f"<td>{n(P[0])}</td><td>{n(P[1])} – {n(P[2])}</td>"
                            f"<td>r = {P[3]:+.2f} — {'yes' if abs(P[3]) < .9 else 'no'}</td></tr>"
                            for S, N, P in (('A', A, pa), ('B', B, pb)))
                        + f"</table>"
                        f"<p class=note>A cost <b>flat</b> across a wide range of permutation "
                        f"counts (r near 1, narrow range) does not depend on how much data is hashed "
                        f"— it is reduced by performing fewer permutations or by making the wrapper "
                        f"cheaper, not by hashing less. A cost that <b>varies</b> tracks the payload."
                        + (f" <b>⚠ Do not read the ratio of these two as a cost difference here.</b> "
                           f"They differ by {_r:.1f}×, past the {INLINE_Z:.0f}× point above which a "
                           f"gap is more likely a <b>compilation</b> difference than a real one: a "
                           f"guest that <b>inlines</b> its precompile wrapper charges the work to its "
                           f"callers, so nothing is attributed to a hashing symbol and its figure "
                           f"collapses — it can even fall below what the wrapper itself costs. Check "
                           f"which side exposes it before comparing: <code>nm -C &lt;elf&gt; | grep "
                           f"-i keccak</code>. Measured on this repo, that is exactly what happens "
                           f"— one guest calls the wrapper across the C ABI and keeps the symbol, "
                           f"the other inlines it."
                           if _suspect else
                           " Both guests expose the wrapper at a comparable scale here, so the ratio "
                           "is readable.")
                        + f"</p>")
                h.append(f"<p class=note>{len(picks)} profiled runs per guest, cached until "
                         f"the guest is rebuilt"
                         + (f". This backend's profiler <b>samples</b> (1 in {s['fam_scale']:.0f}), so "
                            f"the counts are scaled estimates; the ratios are unaffected"
                            if s.get('fam_scale', 1) > 2 else "")
                         + f". <b>containers / abstraction</b> is the machinery a codebase carries "
                         f"<i>around</i> its logic — iterators, hash containers and their key hashing, "
                         f"byte buffers, growable vectors, variant dispatch — not the logic itself"
                         + f". Families come from classifying <b>function names</b>, so they are "
                         f"approximate: a name-based taxonomy must carry both languages' vocabulary or "
                         f"it measures the patterns rather than the guests (this one was corrected "
                         f"several times — C++ <i>and</i> Rust container idioms, byte swaps split out of "
                         f"arithmetic, curve crypto out of arithmetic, state access into trie). "
                         f"<b>The useful sanity check is agreement between the two axes</b>: the same "
                         f"guest pair should give a similar family ratio on both, so a wide disagreement "
                         f"is a <i>candidate</i> bug — but it can also be genuine, since the two reth "
                         f"guests differ from each other (different keccak and curve libraries, "
                         f"different drivers). Explain it before trusting it"
                         + (f". On this backend the profiler samples, so a family under ~1% of the total "
                            f"is sampling noise" if s.get('fam_scale', 1) > 2 else "")
                         + (f". The table accounts for <span class=cA>{s['fam_cov'][0]*100:.0f}%</span>"
                            f" / <span class=cB>{s['fam_cov'][1]*100:.0f}%</span> of each guest's real "
                            f"work on these blocks (the remainder is functions outside the top 120 "
                            f"kept per run); unmatched names go to <i>other</i>"
                            if s.get('fam_cov') else "")
                         + f".</p></div>")
            if s.get('insn_ratios'):
                # INSTRUCTION COUNTS, not cost. The cost view answered "what will proving charge",
                # which is a different question and needed a `Main` row that merely restated the
                # headline ratio. Counts answer "what does the guest actually execute more of", and
                # the baseline is the overall work-unit ratio.
                base = s['ratio_median']
                top = s['insn_ratios'][:10]
                mx = max([t[3] or 0 for t in top] + [1])   # bar scaled by A's count
                h.append(f"<div class=pane><h2>which machine operations</h2>"
                         f"<p class=note style='margin:0 0 10px'>The same work seen one level down: "
                         f"individual machine operations, median over all {s['n']} blocks (not the "
                         f"profiled sample). Overall "
                         f"<span class=cA>{A}</span> runs <b>{x(base)}</b> the {u} of "
                         f"<span class=cB>{B}</span> — an operation whose ratio beats that is where "
                         f"the extra work concentrates; one below it is work {A} does <i>less</i> "
                         f"of.</p>")
                # The bar's colour threshold is the OVERALL ratio, not parity, so a row can show a red
                # 1.45x beside a green bar — measured on 3 of the rows here. Both are right: the number
                # answers "more than the other guest?", the bar continues the column it sits next to,
                # which asks "worse than this guest's average?". Unstated, that reads as a bug, so the
                # key says which question the colour answers and names the disagreeing rows outright.
                _split = [k for k, v, _c, _a, _b, _v in top if (v >= 1) != (v >= base)]
                _note = ("" if not _split else
                         f" That is why <code>{'</code>, <code>'.join(_split[:3])}</code> "
                         f"show a {'red' if base > 1 else 'green'} ratio beside a "
                         f"{'green' if base > 1 else 'red'} bar: above 1× yet still better than this "
                         f"guest's own average.")
                h.append(f"<p class=hint style='margin:0 0 5px'><b>bar</b> — length: how many of these "
                         f"<span class=cA>{A}</span> runs, against the largest row here. colour: it "
                         f"continues the column beside it, so <span class=hi>red</span> means the "
                         f"operation's ratio is <i>worse than this guest's overall</i> {x(base)} — not "
                         f"merely above 1×.{_note}</p>")
                h.append(f"<table><tr><th>operation</th><th>median count<br>per block</th>"
                         f"<th>share of<br>counted ops</th>"
                         f"<th>blocks<br>compared</th>"
                         f"<th>median of<br>per-block ratios</th>"
                         f"<th>vs {x(base)} overall</th><th></th></tr>")
                for k, v, cnt, ca, cb, vol in top:
                    rel = (v / base - 1) * 100
                    w = max(2, 100 * (ca or 0) / mx)
                    h.append(f"<tr><td style='text-align:left' title='measured on {cnt} block(s) "
                             f"where both guests report it'>{k}</td>"
                             f"<td><span class=cA>{n(ca)}</span><br><span class=cB>{n(cb)}</span></td>"
                             f"<td>{('%.3f%%' % (vol*100)) if vol else '—'}</td>"
                             # Coverage as a COLUMN, not a tooltip: the emulator prints an opcode row
                             # only above some threshold, and the two guests do not cross it on the
                             # same blocks — `srl` is reported by the reth guest on half the sample.
                             # A ratio over a self-selected subsample looked like a finding until the
                             # count was visible beside it.
                             f"<td class={'na' if cnt < len(rows) * 0.9 else ''}>{cnt}/{len(rows)}</td>"
                             # Same precision as the baseline it is compared against: at 2 decimals
                             # `shift` printed 1.22x beside a 1.221x baseline, so its red bar looked
                             # wrong — the real ratio is 1.2241, above the baseline. The colour was
                             # right and the rounding made it unreadable.
                             f"<td class={'hi' if v >= 1 else 'lo'}>{x(v)}</td>"
                             f"<td class={'hi' if rel > 0 else 'lo'}>{rel:+.0f}%</td>"
                             # Hover reading per row, because the bar's colour tracks the column beside
                             # it while its length tracks the count column three to the left.
                             f"<td style='width:26%' title=\"{k}: {A} {n(ca)} vs {B} {n(cb)} "
                             f"per block · ratio {v:.2f}× · {rel:+.0f}% against this guest's overall "
                             f"{x(base)}\">"
                             f"<span class='rbar{' over' if v >= base else ''}' "
                             f"style='width:{w:.0f}%'></span></td></tr>")
                # Same trap as the metric bars: the ratio column is the MEDIAN OF PER-BLOCK RATIOS
                # (deliberately — the baseline it is compared against, s['ratio_median'], is the same
                # statistic), but the count column holds MEDIANS OF COUNTS, so dividing the two
                # displayed numbers does not give the displayed ratio. Measured here it is off by up
                # to 21% (mul: 5.68M/7.86M = 0.72 next to a shown 0.87). Say so, with the figure.
                _dev = max(((abs(v - (ca / cb)) / (ca / cb) * 100)
                            for _k, v, _c, ca, cb, _vl in top if ca and cb), default=0)
                # "counts of executed instructions" was wrong on one axis: ZisK's per-opcode
                # counter sums to ~22x the step count, so its magnitudes are not instruction counts
                # at all. State what the counter actually covers, measured, instead of asserting a
                # meaning it does not have on both backends.
                _oc = s.get('ops_coverage')
                _scale = ("" if not _oc else
                          (f" These counters are genuine instruction counts and cover "
                           f"<b>{_oc*100:.0f}%</b> of {u} — only some opcodes are grouped."
                           if _oc <= 1.2 else
                           f" <b>Read the shares, not the magnitudes:</b> this backend's per-opcode "
                           f"counter sums to <b>{_oc:.0f}×</b> the {u} count, so its absolute figures "
                           f"are not instruction counts. Guest-to-guest ratios stay valid, since the "
                           f"same counter is used on both sides."))
                h.append(f"</table><p class=note>The ratio is the <b>median of the per-block "
                         f"ratios</b> — the same statistic as the {x(base)} baseline it is measured "
                         f"against — so it is <b>not</b> the quotient of the two counts beside it, "
                         f"which are medians of counts. On this axis the two differ by up to "
                         f"<b>{_dev:.0f}%</b>.{_scale} These are counts of executed operations "
                         f"— nothing "
                         f"about how much each costs to prove. For that, the <b>{pwu or 'prover'}</b> "
                         f"ratio in the cards above ({x(s.get('pw_ratio_median'))}) is the figure to "
                         f"read; it differs from {x(base)} precisely because the operations that grew "
                         f"are not the expensive ones.</p></div>")
            h.append("</div>")
        # representative blocks
        bk = AXES[ax]['backend']
        # Why real blocks and not a synthetic "median profile": averaging is linear, so a
        # mean-per-function profile still sums to the mean total; a median is not, so a profile
        # whose every function carried its cross-block median would sum to no real block's total
        # and match no run that ever happened. See README.
        h.append(f"<div class=pane><h2>three real blocks worth profiling</h2><div class=rep>"
                 + "".join(f"<div><div class=q>{q}</div><div class=b>{s['block_'+k]}</div>"
                           f"<div class=r>{x(s['ratio_at_'+k])} &nbsp;·&nbsp; {d}</div></div>"
                           for q, k, d in (
                               ('typical', 'median', 'the middle block'),
                               ('best case', 'p10', f'{A} rarely does better'),
                               ('worst case', 'p90', f'{A} rarely does worse')))
                 + "</div>"
                 f"<div class=cmd>./compare.py --axis {ax} --spread"
                 f"   <span style='color:var(--dim)'># profiles the best- and worst-case blocks, then "
                 f"diffs them</span></div></div>")
        # outliers
        outs = s.get('_outliers', [])
        if outs:
            # A dashboard reader wants "which block, and what's odd about it" — not a z-score. So:
            # name the side that departed from ITS OWN normal, which is the actual finding (on this
            # data it is the reth guest that blows up, not the Monad one being efficient). z and the
            # median+MAD rule that flagged them stay in --json for whoever wants the statistics;
            # MAD rather than mean+σ because a handful of extremes drags a classic z-score.
            # Table rendered by the shared JS builder (see _table): same per-block columns as the
            # histogram drill-down, so the two can never drift apart. Python only supplies the frame.
            h.append(f"<div class=pane><h2>{len(outs)} blocks break the pattern</h2>"
                     # "Almost every block lands near X" was asserted, not measured: true on ZisK
                     # (96% within +-10% of the median) but FALSE on SP1, where only 66% are — the
                     # distribution has a second mode from rsp's software-BN254 blocks. Say the
                     # measured concentration instead, so the sentence can't go stale.
                     f"<p class=note style='margin:0 0 10px'>"
                     f"<b>{_near(ratios, s['ratio_median']):.0f}%</b> of blocks land within ±10% of "
                     f"{x(s['ratio_median'])}. These don't — each is a lead worth profiling, because "
                     f"something in the block changed which guest wins. <b>How they were picked:</b> "
                     f"each block's distance from the middle one is measured against how much blocks "
                     f"typically stray from it — using medians rather than averages, so a few extreme "
                     f"blocks can't mask one another. The ones more than "
                     f"<b>{s.get('z_thresh')}×</b> that typical stray are listed. Every figure below "
                     f"is followed by its <b>gap to the sample median</b>, since what matters here is "
                     f"how the block differs from the other {s['n']}.</p>"
                     f"<div id='ol-{ax}'></div></div>")
        # full table, collapsed
        # Same scroller as every other wide table (see `.scroll`): 9 numeric columns overflow the card
        # on a narrow viewport — measured at 799px into 648, which also gave the whole PAGE a
        # horizontal scrollbar — and 373 rows of it otherwise run 13,000px down the document.
        h.append("<details><summary>▸ every block — %d rows: %s, %sgas, txs, ratio</summary>"
                 "<div class=pane>" % (len(rows), u, (pwu + ", ") if pwu else ""))
        pwcols = f"<th>{A} {pwu}</th><th>{B} {pwu}</th><th>{pwu} ratio</th>" if pwu else ""
        zs = {e['block']: e['z'] for e in outs}
        rmax = max(ratios) or 1
        # Here the bar's length is the RATIO itself, unlike the two tables above where it is a volume.
        # Same glyph, different variable, so say which one and against what — the scale's top end is the
        # worst block in the sample, not 1x, so a full bar means "worst here", not "twice as slow".
        # Outside the .scroll: a key that scrolls sideways with its table stops being a key.
        h.append(f"<p class=hint style='margin:0 0 5px'><b>bar</b> — length: this block's ratio against "
                 f"the highest in the sample ({x(rmax)}, full width). colour: "
                 f"<span class=hi>red</span> where <span class=cA>{A}</span> costs more on the block, "
                 f"<span class=lo>green</span> where less.</p>")
        h.append("<div class=scroll>")
        h.append(f"<table><tr><th>block</th><th>gas</th><th>txs</th><th>{A} {u}</th><th>{B} {u}</th>"
                 f"<th>ratio</th>{pwcols}</tr>")
        for b in sorted(rows, key=lambda b: -(rows[b]['a']['work'] / max(rows[b]['b']['work'], 1))):
            r0 = rows[b]; r = r0['a']['work'] / r0['b']['work'] if r0['b']['work'] else None
            g = r0['b'].get('gas') or r0['a'].get('gas') or gas_map.get(b)
            z = zs.get(b) or zs.get(int(b) if str(b).isdigit() else b)
            pw = ""
            if pwu:
                pa, pb = r0['a'].get(pwk), r0['b'].get(pwk)
                pr = (pa / pb) if (pa and pb) else None
                pw = (f"<td>{n(pa)}</td><td>{n(pb)}</td>"
                      f"<td class={'hi' if pr and pr > 1 else 'lo'}>{x(pr)}</td>")
            # The bar shares its cell with the ratio, so the tooltip goes on the cell: it states the
            # length's reference (the sample's worst block), which no number in the row carries.
            bar = (f"<span class='rbar{' over' if r and r>=1 else ''}' "
                   f"title=\"{x(r)} — {r/rmax*100:.0f}% of the sample's highest ratio {x(rmax)}\" "
                   f"style='width:{max(2, round(38*r/rmax))}px'></span>") if r else ""
            tx = r0['b'].get('txs') or r0['a'].get('txs') or tx_map.get(b)
            h.append(f"<tr><td title=\"{'flagged as off-pattern' if z else ''}\">"
                     f"{'⚠ ' if z else ''}{b}</td><td>{n(g)}</td><td>{n(tx)}</td>"
                     f"<td>{n(r0['a']['work'])}</td><td>{n(r0['b']['work'])}</td>"
                     # threshold 1x, like the bar beside it and the histogram legend — not the
                     # median, which left every below-median ratio with no colour at all
                     f"<td class={'hi' if r and r >= 1 else 'lo'}>{bar}{x(r)}</td>{pw}</tr>")
        h.append("</table></div></div></details>")   # table, .scroll.tall, .pane
        # What the columns mean — kept to what a reader of the numbers needs; provenance lives in
        # the code comment above _PW_DOC.
        note = (f"<p class=note><b>{u}</b> is deterministic — identical on any machine. ")
        if pwu:
            note += (_PW_DOC.get(pwu, f"<b>{pwu}</b> estimates prover work. ")
                     + " Being <b>trace area</b>, it weights each operation by what proving it costs (a "
                     "keccak precompile ≫ an ADD), so its ratio predicts proving cost better than the "
                     f"{u} ratio does. Backends measure it on their own scale — compare ratios, not raw "
                     "figures. ")
        nogas = [b for b in rows if not (rows[b]['b'].get('gas') or rows[b]['a'].get('gas')
                                         or gas_map.get(b))]
        if nogas:
            note += (f"<b>gas</b> is EVM gas, which only the reth ZisK guest reports — so the "
                     f"{len(nogas)} block(s) here with no ZisK witness show <b>—</b> and sit outside "
                     f"every gas-normalised figure ({u}/Mgas, the size split, the per-bucket sizes). "
                     f"SP1 cannot fill the gap: the <code>gas</code> in its report is prover gas, not "
                     f"EVM gas. ")
        note += "<b>exec secs</b> is host emulation time, the only figure here that depends on the machine"
        if AXES[ax]['backend'] == 'sp1':
            ov = s.get('gas_pass_overhead')
            note += (". It excludes SP1's ~6.3 s per-process startup <i>and</i> its gas-estimation pass"
                     + (f", which cost a measured ×{ov:.2f} on this sample" if ov else "") +
                     " — the cycle count comes from a gas-on run, the timing from a <code>--no-gas</code> "
                     "one, because SP1 reports <code>cycles = 0</code> when the pass is off. Without that "
                     "split the pass would not just inflate the times, it would reorder the two guests: "
                     "its cost depends on the block's opcode/syscall mix, so it does not fall equally on "
                     "both.</p>")
        else:
            note += (" — and it excludes the instrumented pass that produces "
                     f"{pwu or 'prover work'}, which is a separate, ~7× slower run.</p>")
        h.append(note)
        h.append("</section>")
    # Provenance + the timing model in full. It sits at the foot on purpose: the exec-time column
    # is the only figure on this page that is not machine-independent, so a reader who wants to
    # trust it needs the backend, the rates and the caveats — and nobody else should step over them.
    thr_rows = ''.join(
        f"<tr><td>{g}</td><td class=n>{v:,.0f}</td><td class=why>{note}</td></tr>"
        for g, v, note in (
            ('monad-r3-zisk', PURE_MSTEPS_PER_S['monad-r3-zisk'], 'ZisK ASM backend'),
            ('monad-sam-zisk', PURE_MSTEPS_PER_S['monad-sam-zisk'], 'ZisK ASM backend'),
            ('zisk-reth', PURE_MSTEPS_PER_S['zisk-reth'], 'ZisK ASM backend'),
            ('monad-r3-sp1', PURE_MSTEPS_PER_S['monad-r3-sp1'],
             'SP1 executor, <code>--no-gas</code>'),
            ('monad-sam-sp1', PURE_MSTEPS_PER_S['monad-sam-sp1'],
             'SP1 executor, <code>--no-gas</code>'),
            ('rsp', PURE_MSTEPS_PER_S['rsp'],
             'SP1 executor, <code>--no-gas</code> — <b>SP1 seconds measure a different '
             'emulator than the ZisK rows; compare them within a backend, not across</b>')))
    h.append(
        f"<p class=note style='margin-top:34px;border-top:1px solid var(--line);padding-top:16px'>"
        f"<b>Measured on</b> {_hostinfo()} · {time.strftime('%Y-%m-%d %H:%M %Z')}. Work-units "
        f"({'/'.join(sorted({AXES[s['axis']]['unit'] for s in summaries}))}) and prover-work figures "
        f"are deterministic — identical on any machine, and every lever verdict on this branch was "
        f"decided on them. <b>Only the exec-time column is host- and backend-dependent</b>: it is "
        f"modelled, not timed — <code>work ÷ throughput</code>.</p>"
        f"<details class=note style='margin-top:10px'><summary><b>How exec time is computed, and why "
        f"raw wall-clock is not it</b></summary>"
        f"<p>Timing the emulator <i>process</i> measures mostly the harness: <code>ziskemu -e</code> "
        f"re-converts the ELF to ZisK ROM on every invocation (0.6–1.7 s depending on the guest — the "
        f"entire measurement on a small block), and campaign runs are parallel on top of that. The "
        f"emulator's <code>duration=</code> field avoids both: it times the run itself.</p>"
        f"<p>The ZisK rates below are measured <b>the way the RTP pipeline measures</b>: "
        f"<code>ziskemu -e ELF -i INPUT -m</code>, reading the emulator's own <code>duration=</code> "
        f"field, which times <code>process_rom()</code> only. Calibrated on this host, sequentially, "
        f"over 5 blocks spanning 30–360 M steps (residuals 1.4–2.8 %). The campaign collects that "
        f"same field per block, but under 4-way parallelism it reads ~35 % slow, so the column is "
        f"modelled from the sequential rates rather than averaged from the cache. The SP1 rows use "
        f"the same protocol with SP1's own tool — <code>sp1-runner --mode execute --no-gas</code>, "
        f"sequential, the same five blocks (the gas pass is a separate, slower estimation and does "
        f"not belong in an execution figure).</p>"
        f"<table class=cv><tr><th>guest</th><th>M steps or cycles/s</th><th>backend</th></tr>"
        f"{thr_rows}</table>"
        f"<p><b>Caveats worth carrying.</b> Per-block rates vary with the instruction mix "
        f"(109–130 M steps/s for monad-opt across the calibration blocks), so read these seconds as "
        f"±15 %. In-guest setup is <i>included</i>, and measured on the profiles it is small: "
        f"KZG/blob ≈ 1 %, witness parse ≈ 0.5 % of the Monad guest — excluding them would move "
        f"nothing. And this is <b>emulation</b> time on one host: it says how much work a block is, "
        f"not what a proof costs. The ZisK ASM backend (<code>cargo-zisk execute --asm</code>) runs "
        f"the same guests ~2.4× faster still (≈300 M steps/s, measured on the devcore box), so a number "
        f"quoted from that backend is not comparable to one from here.</p></details>")
    h.append("</div>")
    h.append(f"<script>{_HIST_JS}</script>")
    h.append(f"<script>{_AXBAR_JS}</script>")
    open(path, 'w').write("\n".join(h))
    print(f"\nwrote {path}")

# ────────────────────────────────── summary page ────────────────────────────────────────
# The one-page synthesis of the report above — same numbers, none recomputed: everything here
# is read from the summaries write_html renders, so the two cannot disagree. It exists because
# the full report is a methods document as much as a dashboard, and a reader who only wants the
# answer has to dig for it. Design rule: one number per question, and ONE caveat inline — the
# one that changes a conclusion when ignored. Every methodological defence stays in the full
# report, linked, rather than being restated smaller here.
#
# Four questions, one line each:  how much more? (median ratio + prover-work ratio) ·
# is it stable? (spread + small-vs-large split) · where? (top work families by A−B delta) ·
# anything off? (regime split + outlier count, pointing at the full report).

def _fam_delta(s, sign=1, top=3):
    """Top work families by (A−B)×sign instructions/block — where the gap sits, read in the
    direction of the headline: sign=+1 lists where the dearer guest spends more, sign=−1
    (A cheaper overall) where the saving comes from.

    Families, not gap_split's cost categories: 'EVM interpreter' answers the reader's
    question, 'Opcodes' restates the cost model's bucketing."""
    fams = s.get('families')
    if not fams: return []
    sa, sb = fams[0], fams[1]
    d = sorted(((k, (sa.get(k, 0) - sb.get(k, 0)) * sign, sa.get(k, 0), sb.get(k, 0))
                for k in set(sa) | set(sb)), key=lambda t: -t[1])
    return [t for t in d[:top] if t[1] > 0]

def _summary_lines(s, rows):
    """The per-axis synthesis, as data — rendered twice (HTML + markdown) below.

    No lead sentence restating the ratios: the cards carry the percentages themselves
    (team feedback — the prose duplicated the numbers sitting right above it)."""
    A, B, u = s['a_name'], s['b_name'], s['unit']
    bs = sorted(int(b) for b in rows)
    pct = (s['ratio_median'] - 1) * 100
    pw = s.get('pw_ratio_median')
    # Whether the instruction and prover-work ratios agree is a finding, but only when they
    # clearly do not (>2% apart on the ratio scale; at 0.5% the full report says "too close
    # to call" and this page must not contradict it). Direction phrased on the ratio, so it
    # reads correctly whether A is the dearer or the cheaper guest.
    mix = None
    if pw and abs(pw / s['ratio_median'] - 1) > .02:
        mix = (f"The two ratios disagree: relative to <b class=cB>{B}</b>, "
               f"<b class=cA>{A}</b>'s work mix is "
               f"<b>{'dearer' if pw > s['ratio_median'] else 'cheaper'} to prove</b> than its "
               f"instruction count suggests.")
    # Stable = a property of the guests; block-dependent = of what the block does. 8% is the
    # bar the full report's histogram prose implies (its own axes sit at ~5%), not a standard.
    stable = s['cv'] <= 8
    stab = (f"The gap is <b>{'stable' if stable else 'block-dependent'}</b>: it swings "
            f"±{s['cv']:.1f}% block to block")
    if s.get('ratio_small') and s.get('ratio_large'):
        grow = abs(s['ratio_large'] - s['ratio_small']) / s['ratio_small'] > .05
        stab += (f", and <b>grows with block size</b> ({x(s['ratio_small'])} on the smaller "
                 f"half vs {x(s['ratio_large'])} on the larger)" if grow else
                 f" and is the same on small and large blocks ({x(s['ratio_small'])} vs "
                 f"{x(s['ratio_large'])})")
    stab += "."
    sign = 1 if pct > 0 else -1
    fams = [f"<b>{k}</b> ({'+' if sign > 0 else '−'}{d/1e6:,.0f}M/block"
            + (f", {x(a/b)}" if b else "") + ")"
            for k, d, a, b in _fam_delta(s, sign)]
    fam_lead = ("The extra work sits mostly in" if sign > 0 else
                "The saving comes mostly from")
    extra = []
    if s.get('curve'):
        c = s['curve']
        # This count is a property of the PAIR, not of the blocks: the detector flags a block
        # whose 256-bit multiplication intensity sits above 2.5× THIS axis's median for either
        # guest. So the same block is flagged against a guest that lacks the BN254 precompile
        # and not against one that has it — which is why 79/373 (vs `rsp`) and 3/504 (two Monad
        # guests, both accelerated) are different questions rather than a contradiction. Saying
        # "runs curve arithmetic in software" on every axis asserted the wrong thing on the
        # axis where neither guest does; name the unaccelerated guest instead, when there is one.
        who = {'a': A, 'b': B}
        soft = [who[k] for k in 'ab'
                if (s.get('bn254_path') or {}).get(k)
                and s['bn254_path'][k][0] is not None and not s['bn254_path'][k][2]]
        if soft:
            extra.append(f"<b>{' and '.join(soft)}</b> has no precompile-backed BN254 and runs "
                         f"that curve arithmetic in software, which sets {c['n']} of {s['n']} "
                         f"blocks apart; excluding them the median is {x(c['med_clean'])}.")
        else:
            extra.append(f"{c['n']} of {s['n']} blocks are far more multiplication-heavy than the "
                         f"rest on one side — both guests here do have the BN254 precompile; "
                         f"excluding them the median is {x(c['med_clean'])}.")
    return {'A': A, 'B': B, 'u': u, 'bs': bs, 'pct': pct, 'mix': mix, 'stab': stab,
            'fams': fams, 'fam_lead': fam_lead, 'extra': extra}

# The caveat that survives into the summary: the one whose omission misquotes the result.
_SUMMARY_CAVEAT = ("Ratios compare two guests <b>within one zkVM</b> — never quote a number "
                   "across backends. These are execution and prover-work figures, "
                   "<b>not proving time</b>.")

def write_summary(path, md_path, summaries, allrows, full_href):
    pairs = " · ".join(f"{s['a_name']} vs {s['b_name']}" for s in summaries)
    ts = time.strftime('%Y-%m-%d %H:%M %Z')
    h = [f"<title>summary — {pairs}</title>", f"<style>{_CSS}</style>", "<div class=wrap>",
         "<p class=eyebrow>zkvm-bench · comparison summary</p>",
         "<h1>The short version</h1>",
         f"<p class=sub>{_SUMMARY_CAVEAT} Methodology, per-block detail and every defence of "
         f"these numbers: <a href='{full_href}' style='color:var(--accent)'>the full report</a>. "
         f"Generated by <code>profiling/compare.py</code> · {ts}.</p>"]
    md = [f"# zkvm-bench — comparison summary · {ts}", ""]
    for s in summaries:
        d = _summary_lines(s, allrows[s['axis']])
        A, B, u, bs = d['A'], d['B'], d['u'], d['bs']
        h.append("<section>")
        bk = AXES[s['axis']]['backend'].upper() if s['axis'] in AXES else ''
        h.append(f"<div class=axhead><span class=nm>{s['axis'].upper()}</span>"
                 f"<span class=vs>{A} &nbsp;vs&nbsp; {B} · on {bk} · {s['n']} blocks "
                 f"{bs[0]}–{bs[-1]}</span></div>")
        # The percentage is the ONLY notation here: "+19.1%" and "1.191×" are the same figure,
        # and showing both made the reader look for a second fact that does not exist. The ×
        # notation stays in the full report, which is where a cross-page reader lands anyway.
        # Coloured by direction — dearer red, cheaper green, the report's own
        # above/below-parity palette (NOT the guest gold/blue) — lightened so the tone signals
        # without shouting.
        gp = lambda r: ('+' if r > 1 else '−') + f"{abs(r - 1) * 100:.1f}%"
        gc = lambda r: '#ec8279' if r > 1 else '#78d1a7'   # --red / --green, lightened
        h.append("<div class=cards>")
        h.append(f"<div class='card hero'><div class=k>median gap</div>"
                 f"<div class=v style='color:{gc(s['ratio_median'])}'>{gp(s['ratio_median'])}</div>"
                 f"<div class='u blk'>{u} of <span class=cA>{A}</span> vs "
                 f"<span class=cB>{B}</span>, middle block</div></div>")
        if s.get('pw_ratio_median'):
            h.append(f"<div class='card hero'><div class=k>prover work</div>"
                     f"<div class=v style='color:{gc(s['pw_ratio_median'])}'>"
                     f"{gp(s['pw_ratio_median'])}</div>"
                     f"<div class='u blk'>same, in {s['pw_unit']} (trace area) — what proving "
                     f"will cost</div></div>")
        h.append(f"<div class=card><div class=k>median {u}</div>"
                 f"<div class=duo><span class=cA>{s['a_median']/1e6:,.0f}M</span>"
                 f"<span class=sep>vs</span><span class=cB>{s['b_median']/1e6:,.0f}M</span></div>"
                 f"<div class='u blk'>deterministic — same on any machine</div></div>")
        # nsecs (SP1's --no-gas pass) is the honest timing when it exists — same rule as the
        # full report's card.
        ta, tb = ((s.get('a_nsecs_median'), s.get('b_nsecs_median'))
                  if s.get('a_nsecs_median') else
                  (s.get('a_secs_median'), s.get('b_secs_median')))
        if ta and tb:
            h.append(f"<div class=card><div class=k>median exec time</div>"
                     f"<div class=duo><span class=cA>{ta:.2f}s</span>"
                     f"<span class=sep>vs</span><span class=cB>{tb:.2f}s</span></div>"
                     f"<div class='u blk'>pure execution: work ÷ this guest's measured emulator "
                     f"throughput on the reference host — not proving time</div></div>")
        h.append(f"<div class=card><div class=k>spread</div><div class=v>±{s['cv']:.1f}"
                 f"<span class=u>%</span></div><div class='u blk'>block-to-block swing of "
                 f"the ratio</div></div>")
        h.append("</div>")
        h.append(f"<div class=insight>{d['stab']}"
                 + (f"<br>{d['mix']}" if d['mix'] else "")
                 + (f"<br>{d['fam_lead']} {', '.join(d['fams'])}."
                    if d['fams'] else "")
                 + "".join(f"<br><span style='color:var(--muted)'>{e}</span>"
                           for e in d['extra'])
                 + "</div>")
        h.append("</section>")
        # the same synthesis, paste-able (Slack / Notion) — plain text, no HTML markup
        strip = _plain
        head = f"**{gp(s['ratio_median'])} {u}** on the median block"
        if s.get('pw_ratio_median'):
            head += f" · prover work: **{gp(s['pw_ratio_median'])}** ({s['pw_unit']})"
        head += f" · median {s['a_median']/1e6:,.0f}M vs {s['b_median']/1e6:,.0f}M {u}"
        if ta and tb:
            head += f" · exec {ta:.2f}s vs {tb:.2f}s"
        md += [f"## {s['axis'].upper()} — {A} vs {B} (on {bk} · {s['n']} blocks {bs[0]}–{bs[-1]})",
               f"- {head}",
               f"- {strip(d['stab'])}"]
        if d['mix']:
            md.append(f"- {strip(d['mix'])}")
        if d['fams']:
            md.append(f"- {strip(d['fam_lead']).lower()}: {strip(', '.join(d['fams']))}")
        md += [f"- {strip(e)}" for e in d['extra']]
        md.append("")
    h.append(f"<p class=sub style='margin-top:30px;border-top:1px solid var(--line);"
             f"padding-top:14px'>{_SUMMARY_CAVEAT}</p>")
    h.append("</div>")
    md += [f"_{_plain(_SUMMARY_CAVEAT)}_",
           f"_Full report (methodology, per-block detail, outliers): {full_href}_"]
    open(path, 'w').write("\n".join(h))
    open(md_path, 'w').write("\n".join(md) + "\n")
    print(f"wrote {path}\nwrote {md_path}")

# ────────────────────────────────── deep pass ───────────────────────────────────────────

def deep(axis, blocks, args):
    """Delegate to hotspots.py: aggregate-profile N blocks per side, then diff the two."""
    ax = AXES[axis]; hs = os.path.join(HERE, 'hotspots.py')
    outs, tmps = [], []
    try:
        for side_k in ('a', 'b'):
            side = ax[side_k]; out = os.path.join(HERE, 'results', f'cmp-{axis}-{side_k}')
            cmd = [hs, 'profile', '--backend', ax['backend'], '--elf', rp(side['elf']),
                   '--out', out, '--aggregate', '--tab-prefix', side['name']]
            for b in blocks:
                p = resolve_input(side, b)
                if needs_framing(ax['backend'], side):
                    t = tempfile.NamedTemporaryFile(suffix=f'.{b}.bin', delete=False).name
                    frame_ziskos(p, t); tmps.append(t); p = t
                cmd += ['-i', p]
            print(f"\n[deep {axis}] profiling {side['name']} over {len(blocks)} block(s)…")
            if subprocess.run(cmd, cwd=HERE).returncode != 0:
                print(f"[deep {axis}] hotspots profile failed for {side['name']}"); return
            outs.append(os.path.join(out, 'profile.json'))
    finally:
        for t in tmps:
            if os.path.exists(t): os.remove(t)
    print(f"\n[deep {axis}] module-level diff ({ax['a']['name']} vs {ax['b']['name']}):")
    subprocess.run([hs, 'diff', '--json', outs[0], '--json', outs[1]], cwd=HERE)

def spread(axis, s, side_k='a'):
    """Why is an expensive block expensive? Profile the REAL p90 and p10 blocks of one guest and
    diff them. This is the sound answer to "show me a decile profile": both sides of the diff are
    executions that actually happened, so the tree and the totals mean something — whereas a
    per-function decile across blocks is a profile no run ever produced."""
    ax = AXES[axis]; side = ax[side_k]; hs = os.path.join(HERE, 'hotspots.py')
    outs, tmps = [], []
    try:
        for tag, b in (('p90', s['block_p90']), ('p10', s['block_p10'])):
            p = resolve_input(side, b)
            if not p:
                print(f"[spread {axis}] no input for block {b}"); return
            if needs_framing(ax['backend'], side):
                t = tempfile.NamedTemporaryFile(suffix=f'.{b}.bin', delete=False).name
                frame_ziskos(p, t); tmps.append(t); p = t
            out = os.path.join(HERE, 'results', f'spread-{axis}-{tag}')
            print(f"\n[spread {axis}] profiling {side['name']} on the {tag} block {b}…")
            if subprocess.run([hs, 'profile', '--backend', ax['backend'], '--elf', rp(side['elf']),
                               '--out', out, '-i', p, '--tab-prefix', tag], cwd=HERE).returncode != 0:
                print(f"[spread {axis}] hotspots profile failed on {b}"); return
            outs.append(os.path.join(out, 'profile.json'))
    finally:
        for t in tmps:
            if os.path.exists(t): os.remove(t)
    print(f"\n[spread {axis}] what {side['name']} does MORE of where it is RELATIVELY worst "
          f"(p90 {s['block_p90']}@{s['ratio_at_p90']:.3f}×) vs relatively best "
          f"(p10 {s['block_p10']}@{s['ratio_at_p10']:.3f}×) — Δ is p90 over p10:")
    print(f"  ⚠ these are quantiles of the RATIO, not of block size, so the two blocks differ in "
          f"absolute work. Read the normalised Δ%oftot column, not raw Δcount.")
    subprocess.run([hs, 'diff', '--json', outs[0], '--json', outs[1]], cwd=HERE)

# ──────────────────────────────────── CLI ───────────────────────────────────────────────

def parse_blocks(spec):
    out = set()
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1); out |= set(range(int(a), int(b) + 1))
        elif part: out.add(int(part))
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--axis', action='append', choices=sorted(AXES),
                    help='which same-zkVM pair; repeatable. Default: cur-zisk + cur-sp1 — NOT every '
                         'axis. See profiling/RUNBOOK.md for the canonical four.')
    ap.add_argument('--block-min', type=int, metavar='N', help='ignore blocks below N')
    ap.add_argument('--block-max', type=int, metavar='N', help='ignore blocks above N')
    ap.add_argument('--blocks', help='explicit set, e.g. 25552005-25552088 and/or a comma list; '
                                     'takes precedence over --block-min/--block-max')
    ap.add_argument('--limit', type=int, help='cap the number of blocks (after filtering)')
    ap.add_argument('--jobs', type=int, default=None,
                    help='parallel runs (default 4 zisk / 3 sp1). SP1 pays a ~6s fixed startup per '
                         'process regardless of workload, and running 3 at once measured 1.8x faster '
                         'than serial; lower it if big blocks strain RAM')
    # One command collects everything by default — work-units, prover work (COST/PGU) and its
    # category split, precompile counts, gas/txs, and the honest --no-gas exec time. --quick drops
    # only the piece that is genuinely expensive.
    ap.add_argument('--quick', action='store_true',
                    help="skip COLLECTING the prover-work metric (ZisK COST + its category split); "
                         "values already cached are still reported. Everything else is collected "
                         "either way. For a fast look only: ZisK needs a second INSTRUMENTED pass for "
                         "COST, ~19s/block vs ~2s (~10x the ZisK sweep). SP1's PGU is free, so "
                         "--quick saves nothing on that axis")
    ap.add_argument('--with-cost', action='store_true',
                    help=argparse.SUPPRESS)   # kept: it used to be the opt-in, now the default
    ap.add_argument('--outlier-z', type=float, default=3.5, metavar='Z',
                    help='robust z-score (median+MAD) past which a block counts as an outlier (default 3.5)')
    ap.add_argument('--show-outliers', type=int, default=8, metavar='N',
                    help='how many outliers to list in the terminal (default 8; all go to --json)')
    ap.add_argument('--deep', type=int, metavar='N', help='also run a per-module aggregate diff over N blocks')
    ap.add_argument('--spread', action='store_true',
                    help="profile the REAL p90 and p10 blocks and diff them — 'what makes a costly "
                         "block costly'. Sound alternative to a synthetic decile profile: both sides "
                         "are runs that actually happened")
    ap.add_argument('--spread-side', choices=('a', 'b'), default='a',
                    help='which guest to profile for --spread (default a, the Monad side)')
    ap.add_argument('--force', action='store_true', help='ignore the cache and re-run')
    ap.add_argument('--json', dest='json_out', nargs='?',
                    const=os.path.join(HERE, 'results', 'compare.json'),
                    help='write the full per-block data here (default results/compare.json)')
    ap.add_argument('--html', dest='html_out', nargs='?', const=os.path.join(HERE, 'results', 'compare.html'),
                    help='where to write the HTML report (default results/compare.html — written '
                         'even without this flag; use --no-report to skip)')
    ap.add_argument('--families', type=int, default=50, metavar='N',
                    help='profile N blocks per guest, stratified across the ratio distribution, to '
                         'break the work down by kind (default 50; 0 disables). One profiled '
                         'execution each — ~13s on ZisK, ~24s on SP1 — cached PER BLOCK, so raising N '
                         'only profiles the new ones. Sample size matters: 10 -> 50 moved the C++ '
                         'family from 13.6x to 5.11x on SP1, so a small sample was not settled')
    ap.add_argument('--summary-from', metavar='JSON',
                    help='rebuild the one-page summary from an existing --json payload and exit. '
                         'Measures nothing and never loads the run cache, so it is instant. '
                         '--axis then selects and orders the sections')
    ap.add_argument('--no-report', action='store_true',
                    help='terminal summary only: skip the HTML and JSON files')
    ap.add_argument('--emu', default='~/.zisk/bin/ziskemu')
    ap.add_argument('--runner', default=os.path.join(REPO, 'infra/sp1-infra/sp1-runner/target/release/sp1-runner'))
    args = ap.parse_args()
    # Subprocesses (hotspots.py) write straight to the terminal; keep our own output in step
    # with theirs when stdout is a pipe, or the --deep sections land out of order.
    try: sys.stdout.reconfigure(line_buffering=True)
    except Exception: pass

    # Re-render only. The --json payload already carries every summary and every per-block row,
    # which is all the summary page reads — so this path returns before load_cache(). That
    # matters: the cache is ~155 MB on disk and ~1.3 GB resident, and loading it IS the entire
    # cost of a run that has nothing left to measure (4 s and 1.3 GB per axis, enough to get a
    # multi-axis re-render OOM-killed). Nothing here can measure, so nothing here can disagree
    # with the report the payload came from.
    if args.summary_from:
        payload = json.load(open(args.summary_from))
        # --axis both SELECTS and ORDERS the sections here, so the page can be arranged for a
        # reader (baseline pair first, reference pair second) rather than in the order the run
        # happened to measure them. Without it, the payload's own order is kept.
        if args.axis:
            order = [a for a in args.axis if a in payload]
            missing = [a for a in args.axis if a not in payload]
            if missing:
                print(f"not in {os.path.basename(args.summary_from)}: {', '.join(missing)}")
            if not order:
                print(f"none of the requested axes are in {args.summary_from}"); return 1
        else:
            order = list(payload)
        base = os.path.splitext(args.summary_from)[0]
        write_summary(base + '-summary.html', base + '-summary.md',
                      [payload[a]['summary'] for a in order],
                      {a: payload[a]['blocks'] for a in order},
                      full_href=os.path.basename(base + '.html'))
        return 0

    tools ={'zisk': os.path.expanduser(args.emu), 'sp1': os.path.expanduser(args.runner)}
    # Not `sorted(AXES)`: that would pull the levers axes into a bare `./compare.py` and change
    # what the default report says without anyone asking for it.
    axes = args.axis or list(DEFAULT_AXES)
    want = parse_blocks(args.blocks) if args.blocks else None
    cache = load_cache()
    skipped = []           # axes dropped for a missing ELF — recapped at the end,
                           # because a warning printed before a two-hour run scrolls away
    summaries, allrows, payload = [], {}, {}

    collected = []
    for axis in axes:                                    # 1) run everything first…
        if not os.path.exists(tools[AXES[axis]['backend']]):
            print(f"skip {axis}: {AXES[axis]['backend']} tool not found ({tools[AXES[axis]['backend']]})\n"
                  f"      install it — profiling/RUNBOOK.md § Prerequisites")
            continue
        # Guest ELFs, checked here rather than deep inside collect() — a missing one used to surface
        # as a raw FileNotFoundError from the cache's mtime stamp.
        missing = [rp(AXES[axis][k]['elf']) for k in ('a', 'b')
                   if not os.path.exists(rp(AXES[axis][k]['elf']))]
        if missing:
            # Two very different situations look identical on disk, and only one of them is safe to
            # skip. The cache tells them apart: measurements recorded HERE under this build's name mean
            # the build existed and was deleted — the axis outlived its guest, and skipping it quietly
            # leaves a stale declaration to rot in compare.py. A name with no local measurements is
            # almost always a build this checkout never received, which IS a skip.
            names = [AXES[axis][k]['name'] for k in ('a', 'b')
                     if not os.path.exists(rp(AXES[axis][k]['elf']))]
            measured = {n: sum(len(cache.profiles_for(i_)) for i_, _mt in cache.builds_by_name(n))
                        for n in names}
            orphaned = {n: c for n, c in measured.items() if c}
            if orphaned:
                det = ', '.join(f"{n} ({c} cached measurement(s))" for n, c in orphaned.items())
                sys.exit(f"axis {axis}: its guest is gone, but the axis is still declared — {det}.\n"
                         f"      That build was measured on this machine, so the ELF was deleted rather\n"
                         f"      than never received: the axis has outlived its guest and would report\n"
                         f"      coverage it cannot produce.\n"
                         f"      Remove it:  ./axis.py rm {axis}      Find every such axis:  ./axis.py gc")
            print(f"warning: skipping {axis} — guest ELF not built: {', '.join(missing)}\n"
                  f"      a fresh clone has no ELF: they are built per branch and are git-ignored.\n"
                  f"      profiling/RUNBOOK.md § Compare two versions of the guest, A to Z")
            skipped.append((axis, missing))
            continue
        blocks = blocks_for(axis)
        if want is not None:
            blocks = [b for b in blocks if b in want]
        else:
            lo = args.block_min if args.block_min is not None else -1
            hi = args.block_max if args.block_max is not None else float('inf')
            blocks = [b for b in blocks if lo <= b <= hi]
        if args.limit: blocks = blocks[:args.limit]
        if not blocks:
            print(f"skip {axis}: no block has inputs for both {AXES[axis]['a']['name']} and "
                  f"{AXES[axis]['b']['name']}"); continue
        jobs = args.jobs if args.jobs else (4 if AXES[axis]['backend'] == 'zisk' else 3)
        print(f"[{axis}] {len(blocks)} common block(s), {blocks[0]}..{blocks[-1]}")
        rows = collect(axis, blocks, tools, cache, jobs, args.force, not args.quick)
        if not rows:
            print(f"skip {axis}: every run failed (see --force / tool paths)"); continue
        collected.append((axis, blocks, rows))

    # 2) …pool EVM gas and tx count. Both are properties of the BLOCK, and only the reth ZisK guest
    # prints them, so an `--axis sp1` run must still see what a past zisk run measured. They live in
    # each block file's `chain`, written by whichever guest recorded them — so this reads the blocks
    # in play instead of scanning every entry ever cached, as it had to when one flat dict held
    # everything.
    gas_map, tx_map = {}, {}
    for _axis, blocks, _rows in collected:
        for b in blocks:
            ch = cache.chain(b)
            if ch.get('gas'): gas_map[int(b)] = ch['gas']
            if ch.get('txs'): tx_map[int(b)] = ch['txs']

    for axis, blocks, rows in collected:                 # 3) …then summarise
        s = summarize(axis, rows, gas_map)
        s['z_thresh'] = args.outlier_z
        entries, _med, mad = outliers(rows, args.outlier_z)
        s['n_outliers'] = len(entries); s['_outliers'] = entries   # '_' → detail, not a headline stat
        summaries.append(s); allrows[axis] = rows
        payload[axis] = {'summary': s, 'outliers': entries,
                         'blocks': {str(b): rows[b] for b in rows}}
        # Work families need one profiling run per guest on the median block (see profile_block).
        # Cached on the ELF mtime, so this is paid once per guest build, not per report.
        if rows and args.families:
            # Sample ACROSS the ratio distribution rather than at one point, so the mix is not
            # taken from a block that happens to be unusual in composition.
            byr = sorted(rows, key=lambda b: rows[b]['a']['work'] / max(rows[b]['b']['work'], 1))
            k = max(1, min(args.families, len(byr)))
            # Stratified: the MIDPOINT of k equal-population bins (10/30/50/70/90% for k=5), not
            # 0..100%. Taking the endpoints would give the two most extreme blocks a fifth of the
            # weight each, though each stands for one block out of hundreds — measured on this data,
            # that inflated the 256-bit arithmetic family 2.5x (146M vs 59M instructions).
            #
            # Why stratified rather than clustering near the median: clustering estimates the
            # TYPICAL block and drops the tails, which carry real work — the mirror image of the
            # endpoint bias. One block per equal-population bin is unbiased for the MEAN and has
            # lower variance than a random sample of the same size, because the stratifier (the
            # ratio) correlates with what is being measured. Verified sufficient at k=5: doubling
            # to k=10 moved every family by 1-4% except the EVM-interpreter row (-17%).
            picks = [int(byr[min(len(byr) - 1, round((i + 0.5) / k * (len(byr) - 1)))])
                     for i in range(k)]
            picks = sorted(set(picks))
            fa = profile_blocks(axis, 'a', picks, cache)
            fb = profile_blocks(axis, 'b', picks, cache)
            if fa and fb:
                # Rescale to real work. ZisK's profiler attributes essentially every step (measured
                # ~100% coverage), but SP1's is a SAMPLING profiler — it attributed 0.56% of cycles,
                # so its raw figures are samples, not instructions. Ratios survive sampling; absolute
                # numbers do not. Scaling each side by (work on the profiled blocks / attributed)
                # turns both into estimated instructions per block, and keeps the axes comparable.
                def _scaled(side_k, f):
                    got = [(rows.get(b) or rows.get(str(b)))[side_k]['work']
                           for b in picks if (rows.get(b) or rows.get(str(b)))]
                    if not got or not f['total']: return f['fams'], 1.0
                    # hotspots --aggregate already returns a MEAN PER BLOCK, so compare it with the
                    # mean work per block — dividing by len(picks) again double-counted and pushed
                    # the total to 154% of the real cycle count.
                    k = statistics.mean(got) / f['total']
                    return {n: v * k for n, v in f['fams'].items()}, k
                sa, ka = _scaled('a', fa)
                sb, kb = _scaled('b', fb)
                s['families'] = (sa, sb, fa['total'], fb['total'], picks)
                # Coverage, not the raw attributed count: on SP1 the raw figure is a SAMPLE
                # (3.4M next to a 670M table reads as a 200x inconsistency). What the reader needs
                # is the share of real work the table accounts for — scale-free, so it means the
                # same thing on both axes. The shortfall is the top-120 truncation in profile_blocks.
                def _cov(side_k, scaled):
                    got = [(rows.get(b) or rows.get(str(b)))[side_k]['work']
                           for b in picks if (rows.get(b) or rows.get(str(b)))]
                    return (sum(scaled.values()) / statistics.mean(got)) if got else 0.0
                s['fam_cov'] = (_cov('a', sa), _cov('b', sb))
                # Per-call hashing cost. The family ratio alone is misleading whenever both guests
                # call the same keccak precompile: it describes the wrapper, not the hash.
                pc = {k: percall(axis, k, k, picks, cache, rows) for k in 'ab'}
                if pc['a'] and pc['b']: s['percall_hash'] = pc
                # Software-BN254 share per REGIME, computed rather than written into the prose: the
                # panel used to carry five hardcoded percentages (40/6/2.4/1.9/1.0), which would go
                # stale on any guest rebuild and had the SP1 section quoting ZisK figures it could
                # not check. Split the profiled picks by the same flag the headline split uses.
                if s.get('curve'):
                    hot = set(s['curve']['blocks'])
                    pk_hot = [b for b in picks if b in hot]
                    pk_cold = [b for b in picks if b not in hot]
                    s['bn254'] = {k: {'hot': bn254_share(axis, k, pk_hot, cache),
                                      'cold': bn254_share(axis, k, pk_cold, cache)}
                                  for k in 'ab'}
                    # Which PATH each guest takes is a property of the binary, not of the block, so
                    # measure it over every profiled pick.
                    s['bn254_path'] = {k: bn254_paths(axis, k, picks, cache) for k in 'ab'}
                else:
                    # No split on this axis (no per-block mul counter) — still report the share over
                    # all profiled blocks, so the other axis can be cited as a control.
                    s['bn254'] = {k: {'all': bn254_share(axis, k, picks, cache)} for k in 'ab'}
                    s['bn254_path'] = {k: bn254_paths(axis, k, picks, cache) for k in 'ab'}
                s['fam_scale'] = round(max(ka, kb), 1)      # >1 means sampled, not counted
                save_cache(cache)
        print_summary(s)
        print_outliers(s, rows, entries, mad, args.show_outliers, gas_map)
        if args.deep: deep(axis, blocks[:args.deep], args)
        if args.spread: spread(axis, s, args.spread_side)

    if not summaries: return 1
    # One binary under two labels. `zisk`/`sp1` follow use-gen (see AXES), so after a generation
    # switch they can be the very build another axis names — two sections of one report describing
    # the same thing. Derived from a_ident/b_ident, i.e. from what ran, so it cannot be talked out of.
    _by_ident = {}
    for _s in summaries:
        for _k in 'ab':
            if _s[f'{_k}_ident']:
                _by_ident.setdefault(_s[f'{_k}_ident'], []).append((_s['axis'], _s[f'{_k}_name']))
    for _id, _uses in _by_ident.items():
        if len({n for _, n in _uses}) > 1:
            print(f"\nWARNING: one build, several labels — {_id[:12]} ran as "
                  + ", ".join(f"{n} (axis {a})" for a, n in _uses) + ".\n"
                  f"         Those sections describe the SAME binary. If one of them is monad-zisk or\n"
                  f"         monad-sp1, `use-gen` moved it onto the generation you selected.")
    # Cross-axis control, computed once both axes are summarised. "This block runs BN254 in software"
    # is a property of the BLOCK, not of the backend, so the axis that HAS a per-block mul counter
    # can label blocks for the axis that does not. Without this the control compared 37.8% on
    # pairing-heavy blocks against a ZisK figure averaged over ALL blocks — different populations,
    # so it could not support "one guest is the outlier".
    _hot = next((set(t['summary']['curve']['blocks']) for t in payload.values()
                 if t['summary'].get('curve')), None)
    if _hot:
        for s in summaries:
            fams = s.get('families')
            if not fams or s.get('curve'): continue      # its own split already covers it
            picks = fams[4]
            s['bn254'] = {k: {'hot':  bn254_share(s['axis'], k, [b for b in picks if b in _hot], cache),
                              'cold': bn254_share(s['axis'], k, [b for b in picks if b not in _hot], cache)}
                          for k in 'ab'}
            s['bn254_path'] = {k: bn254_paths(s['axis'], k, picks, cache) for k in 'ab'}
            s['bn254_borrowed'] = True                   # labelled: the flag came from the other axis

    # One command = one full run: the report is produced unless explicitly declined.
    chose_paths = bool(args.html_out or args.json_out)      # did the caller name them?
    if not args.no_report:
        args.html_out = args.html_out or os.path.join(HERE, 'results', 'compare.html')
        args.json_out = args.json_out or os.path.join(HERE, 'results', 'compare.json')

    # A run capped with --limit or pinned to --blocks measures a SAMPLE, and its summary statistics
    # describe that sample only. Writing it to the canonical path replaces a whole-set report with a
    # handful of blocks, under the same filename, with nothing to show that it happened — it was done
    # twice while building this, and the only reason it surfaced is that levers.py refuses an n below
    # its threshold. Consumers should not have to defend themselves one by one, so a sampled run
    # writes beside the canonical file instead of over it. Naming the path explicitly still wins.
    if (args.limit or args.blocks) and not chose_paths and not args.no_report:
        args.json_out = os.path.join(HERE, 'results', 'compare-partial.json')
        args.html_out = os.path.join(HERE, 'results', 'compare-partial.html')
        print(f"\nnote: --{'limit' if args.limit else 'blocks'} makes this a sample, not the block "
              f"set the canonical report covers — writing compare-partial.* instead of compare.*.\n"
              f"      Pass --json/--html to choose the path yourself.")

    if args.json_out:
        # An --axis subset measures FEWER axes, not fewer blocks: its numbers are canonical, they
        # just do not cover everything. Merge rather than replace, so running one axis stops dropping
        # the others from the file — the failure that sent a levers.py build looking for axes a
        # two-axis sweep had just deleted.
        if os.path.exists(args.json_out) and set(payload) != set(AXES):
            try:
                prev = json.load(open(args.json_out))
                kept = [a for a in prev if a not in payload]
                if kept:
                    payload = {**prev, **payload}
                    print(f"\nmerged into {os.path.basename(args.json_out)}: kept "
                          f"{', '.join(sorted(kept))} from the previous run")
            except Exception:
                pass                                        # unreadable/absent: write ours
        json.dump(payload, open(args.json_out, 'w'), indent=1); print(f"\nwrote {args.json_out}")
    if args.html_out:
        os.makedirs(os.path.dirname(args.html_out), exist_ok=True)
        # The summary rides along with the report — same numbers, so they are written together
        # or not at all. Named after the report (compare.html -> compare-summary.{html,md}), so a
        # report written to another path gets its own summary instead of overwriting the default.
        base, _ = os.path.splitext(args.html_out)
        sum_html, sum_md = base + '-summary.html', base + '-summary.md'
        write_html(args.html_out, summaries, allrows, gas_map, tx_map,
                   summary_href=os.path.basename(sum_html))
        write_summary(sum_html, sum_md, summaries, allrows,
                      full_href=os.path.basename(args.html_out))
        # The JSON may now hold axes this render does not: it merges, the HTML is a view of the run.
        # Say so, rather than let the two disagree silently under matching filenames.
        _extra = sorted(set(payload) - {s['axis'] for s in summaries})
        if _extra:
            print(f"      note: {os.path.basename(args.html_out)} shows the {len(summaries)} axis/axes "
                  f"just run; {os.path.basename(args.json_out)} also holds {', '.join(_extra)}. "
                  f"Re-run those axes to refresh the report.")
    # Recap the skips LAST. The warning is printed before a run that can take hours, so by the time
    # the reports land it has long scrolled off — and a report quietly covering fewer axes than were
    # asked for is exactly the kind of thing nobody notices until a number looks wrong.
    if skipped:
        print(f"\n  {len(skipped)} axis/axes were SKIPPED and are absent from these reports:")
        for _ax, _miss in skipped:
            print(f"      {_ax}  (missing: {', '.join(os.path.basename(m) for m in _miss)})")
        print("      Build or copy the ELF to include them; ./axis.py gc removes any whose build was\n"
              "      deleted rather than never received.")
    return 0

if __name__ == '__main__':
    sys.exit(main())
