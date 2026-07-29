#!/usr/bin/env python3
"""compare — one-command aggregate comparison of two guests running the SAME zkVM.

Answers "how much more does guest A cost than guest B, over a whole set of blocks?" —
the headline ratio, its spread, and where it comes from — instead of eyeballing blocks
one at a time.

ONE command collects and reports everything — work-units (cycles/steps), prover work
(SP1 PGU / ZisK COST) with its category split, precompile counts, gas and tx counts, and
the honest execution time (measured with SP1's gas-estimation pass off):

    ./compare.py --block-min 25551991 --block-max 25552607 --html --json out.json

    ./compare.py                              # every axis, every common block
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

import argparse, glob, hashlib, json, os, platform, re, statistics, subprocess, struct, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CACHE = os.path.join(HERE, 'results', 'compare-cache.json')
BATCH_MAX = 40          # max inputs per batched sp1-runner process (see sp1_batches)
# ZisK's cost for one keccak op, from zisk `core/src/zisk_ops_costs.rs`: KECCAK_COST = 25 * 3022.
# Used to turn the per-opcode COST that ziskemu reports back into a call count (it prints no count
# for precompiles). Update if ZisK's cost model changes.
ZISK_KECCAK_COST = 25 * 3022

# ───────────────────────────── axes (same-zkVM guest pairs) ─────────────────────────────
# 'src' tells how to build that guest's input for the block:
#   monad-raw   : guests/monad/**/<block>.witness            (SP1 reads it verbatim)
#   monad-framed: same witness, framed LE64(len)+witness+pad8 (what ziskos expects)
#   bin         : the guest's own pre-generated 1-<block>.bin
AXES = {
    'zisk': {'backend': 'zisk', 'unit': 'steps',
             'a': {'name': 'monad-zisk', 'elf': 'guests/monad-zisk/monad-zisk.elf', 'src': 'monad-framed'},
             'b': {'name': 'zisk-reth',  'elf': 'guests/zisk-reth/zisk-reth.elf',   'src': 'bin'}},
    'sp1':  {'backend': 'sp1', 'unit': 'cycles',
             'a': {'name': 'monad-sp1', 'elf': 'guests/monad-sp1/monad-sp1.elf', 'src': 'monad-raw'},
             'b': {'name': 'rsp',       'elf': 'guests/rsp/rsp.elf',             'src': 'bin'}},
}

def rp(*p): return os.path.join(REPO, *p)

# ───────────────────────────────── input discovery ──────────────────────────────────────

def monad_witness(block):
    for p in (rp('guests/monad/fixtures', f'{block}.witness'),
              rp('guests/monad/inputs', f'1-{block}.witness'),
              rp('guests/monad/fixtures', f'1-{block}.witness')):
        if os.path.exists(p): return p
    return None

def guest_bin(guest, block):
    for d in ('inputs', 'fixtures'):
        p = rp('guests', guest, d, f'1-{block}.bin')
        if os.path.exists(p): return p
    return None

def resolve_input(side, block):
    if side['src'] == 'bin':      return guest_bin(side['name'], block)
    return monad_witness(block)   # monad-raw / monad-framed both start from the witness

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
        if src_kind == 'monad-framed':
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
    try:    return json.load(open(CACHE))
    except Exception: return {}

def save_cache(c):
    """Merge-then-write, atomically: two compare.py runs (say one per axis) would otherwise
    clobber each other's results, and a kill mid-write would leave a truncated cache."""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    merged = load_cache(); merged.update(c)
    tmp = CACHE + '.tmp'
    with open(tmp, 'w') as fh: json.dump(merged, fh)
    os.replace(tmp, CACHE)

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

def collect(axis, blocks, tools, cache, jobs, force, with_cost=False):
    """Run both sides over `blocks` (cached by axis/guest/block + ELF mtime). -> {block: {a:…, b:…}}"""
    ax = AXES[axis]; backend = ax['backend']
    tool = tools[backend]
    todo = []
    for side_k in ('a', 'b'):
        side = ax[side_k]
        elf = rp(side['elf']); stamp = int(os.path.getmtime(elf))
        for b in blocks:
            key = f"{axis}/{side['name']}/{b}/{stamp}"
            hit = cache.get(key)
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
                    cache.update(res); note(f"batch of {len(res)}")
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
                    cache[key] = r; note(label)
                    if i % 25 == 24: save_cache(cache)
        print()
        save_cache(cache)
    rows = {}
    for b in blocks:
        row = {}
        for side_k in ('a', 'b'):
            side = ax[side_k]; stamp = int(os.path.getmtime(rp(side['elf'])))
            row[side_k] = cache.get(f"{axis}/{side['name']}/{b}/{stamp}", {})
        if 'work' in row['a'] and 'work' in row['b']: rows[b] = row
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

def summarize(axis, rows, gas_map=None):
    ax = AXES[axis]
    aw = [r['a']['work'] for r in rows.values()]; bw = [r['b']['work'] for r in rows.values()]
    ratios = [r['a']['work'] / r['b']['work'] for r in rows.values() if r['b']['work']]
    # EVM gas is a property of the BLOCK, not of the axis — only the reth ZisK guest prints it,
    # so gas_map (pooled across axes) lets the SP1 axis show work/Mgas too.
    gas = {b: (r['b'].get('gas') or r['a'].get('gas') or (gas_map or {}).get(b))
           for b, r in rows.items()}
    s = {'axis': axis, 'unit': ax['unit'], 'n': len(rows),
         'a_name': ax['a']['name'], 'b_name': ax['b']['name'],
         'a_median': statistics.median(aw), 'b_median': statistics.median(bw),
         'a_mean': statistics.mean(aw), 'b_mean': statistics.mean(bw),
         'a_total': sum(aw), 'b_total': sum(bw),
         'ratio_median': statistics.median(ratios), 'ratio_mean': statistics.mean(ratios),
         'ratio_pooled': sum(aw) / sum(bw),
         'ratio_p10': pct(ratios, .10), 'ratio_p90': pct(ratios, .90),
         'ratio_min': min(ratios), 'ratio_max': max(ratios),
         'cv': (statistics.pstdev(ratios) / statistics.mean(ratios) * 100) if len(ratios) > 1 else 0.0}
    inv = {r['a']['work'] / r['b']['work']: b for b, r in rows.items() if r['b']['work']}
    s['block_min'], s['block_max'] = inv[s['ratio_min']], inv[s['ratio_max']]
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
    shares, opr, nrat, ncnt = {}, {}, {}, {}
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
                    'keccak calls': r.get('kec'),
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
    if nrat:
        # (op, ratio, blocks, median count A, median count B) — quantity as well as ratio
        s['insn_ratios'] = sorted(
            ((k, statistics.median(v), len(v),
              statistics.median(ncnt[k][0]) if ncnt.get(k) else None,
              statistics.median(ncnt[k][1]) if ncnt.get(k) else None)
             for k, v in nrat.items() if len(v) >= need), key=lambda t: -t[1])
    at = [r['a'].get('secs') for r in rows.values() if r['a'].get('secs')]
    bt = [r['b'].get('secs') for r in rows.values() if r['b'].get('secs')]
    if at and bt: s['a_secs_median'], s['b_secs_median'] = statistics.median(at), statistics.median(bt)
    # SP1 only: time from the --no-gas pass — execution without the gas-estimation overhead, i.e.
    # the honest emulation cost. Cycles still come from the gas-on pass (--no-gas reports 0).
    ant = [r['a']['nsecs'] for r in rows.values() if r['a'].get('nsecs')]
    bnt = [r['b']['nsecs'] for r in rows.values() if r['b'].get('nsecs')]
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
        print(f"  {'median '+s['pw_unit']+' (prover)':22} {n(s['a_pw_median']):>18} "
              f"{n(s['b_pw_median']):>18} {x(s.get('pw_ratio_median')):>10}")
    if 'a_nsecs_median' in s:      # honest time (gas-estimation pass off)
        print(f"  {'exec secs (median)':22} {s['a_nsecs_median']:>18.3f} {s['b_nsecs_median']:>18.3f} "
              f"{x(s['a_nsecs_median']/max(s['b_nsecs_median'],1e-9)):>10}")
        if s.get('gas_pass_overhead'):
            print(f"  {'':22} {'(gas-estimation pass would add ×%.2f)' % s['gas_pass_overhead']:>39}")
    elif 'a_secs_median' in s:
        print(f"  {'exec secs (median)':22} {s['a_secs_median']:>18.3f} {s['b_secs_median']:>18.3f} "
              f"{x(s['a_secs_median']/max(s['b_secs_median'],1e-9)):>10}")
    print(f"\n  per-block ratio  median {x(s['ratio_median'])}  mean {x(s['ratio_mean'])}  "
          f"p10 {x(s['ratio_p10'])}  p90 {x(s['ratio_p90'])}  cv {s['cv']:.1f}%")
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
.axhead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:18px}
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
.hp .scroll{max-height:300px;overflow:auto}
.hp .scroll table{min-width:100%;white-space:nowrap}
/* Header row stays put while the body scrolls, so you can still tell which column you are in at
   row 80. Needs an OPAQUE background — the usual translucent th would let rows show through. */
th{position:sticky;top:0;z-index:2;background:var(--panel2)!important;
  box-shadow:inset 0 -1px 0 var(--line)}
.hint{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:8px}
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
  if (has('kecA')) cols.push(['keccak calls', duo('kecA', 'kecB', _f)]);
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
"""

# Work families live in hotspots.py, next to its `module()` classifier — reading a profile is its
# job, and putting them there means `hotspots diff` gets the same grouping. We only consume them.

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
    # not 50. The classification is part of each key — without it, editing hotspots' FAMILIES would
    # silently keep the old grouping (it did: a trie-node fix stayed invisible until cleared).
    fv = hashlib.sha1(repr(hs.FAMILIES).encode()).hexdigest()[:8]
    stamp = int(os.path.getmtime(elf))
    k1 = lambda b: f"fam1/{axis}/{side['name']}/{b}/{stamp}/{fv}"
    todo = [b for b in blocks if k1(b) not in cache]
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
                if side['src'] == 'monad-framed':
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
                            fams, tot = {}, 0
                            for fn in e.get('functions', []):
                                c = fn.get('count') or 0; tot += c
                                fam = hs.family(f"{fn.get('module','')}::{fn.get('name','')}")
                                fams[fam] = fams.get(fam, 0) + c
                            cache[k1(b)] = {'fams': fams, 'total': tot}
        finally:
            for t in tmps:
                if os.path.exists(t): os.remove(t)
    got = [cache[k1(b)] for b in blocks if k1(b) in cache]
    if not got: return None
    fams = {}
    for g in got:
        for k, v in g['fams'].items(): fams[k] = fams.get(k, 0) + v
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
             'as': r0['a'].get('secs'), 'bs': r0['b'].get('secs')}
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
            o = r0[side].get('ops')
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
            f"<span><i>lowest 10%</i> under {x(s['ratio_p10'])}</span>"
            f"<span><i>highest 10%</i> over {x(s['ratio_p90'])}</span>"
            f"<span><i>swing</i> ±{s['cv']:.1f}%</span></div>"
            f"<script type='application/json' id='hd-{ax}'>{json.dumps(payload)}</script>"
            f"<div class=hp id='hp-{ax}'></div>"
            f"<p class=hint>▸ click any bar to list the blocks it holds</p>")

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

def write_html(path, summaries, allrows, gas_map=None, tx_map=None):
    gas_map = gas_map or {}; tx_map = tx_map or {}
    pairs = " · ".join(f"{s['a_name']} vs {s['b_name']}" for s in summaries)
    h = [f"<title>zkVM guest comparison — {pairs}</title>",
         f"<style>{_CSS}</style>", "<div class=wrap>",
         "<p class=eyebrow>zkvm-bench · execution comparison</p>",
         "<h1>Two guest programs, one zkVM</h1>",
         "<p class=sub>Work-units only compare <b>inside</b> a single zkVM (an SP1 cycle is not a ZisK "
         "step), so each section runs two <b>guest programs</b> on one backend over the same blocks. "
         "What differs is the <b>whole guest</b>: its execution engine, how it decodes its witness, how "
         "it handles state and precompiles — and each guest is fed its <b>own witness</b>, which differ "
         "in content as well as encoding. A ratio therefore attributes to the guest as a whole, never to "
         "one component of it. Ratios are <i>first ÷ second</i>: above 1× means the first guest costs "
         "more. These are execution and prover-work figures, <b>not</b> proving time. Generated by "
         "<code>profiling/compare.py</code>.</p>"]
    for s in summaries:
        ax = s['axis']; A, B, u = s['a_name'], s['b_name'], s['unit']
        rows = allrows[ax]
        ratios = sorted(r['a']['work'] / r['b']['work'] for r in rows.values() if r['b']['work'])
        pwu, pwk = s.get('pw_unit'), ('cost' if ax == 'zisk' else 'pgu')
        pct = (s['ratio_median'] - 1) * 100
        h.append("<section>")
        h.append(f"<div class=axhead><span class=nm>{ax.upper()}</span>"
                 f"<span class=vs>{A} &nbsp;vs&nbsp; {B}</span></div>")
        h.append(f"<p class=sub style='margin-bottom:14px'><b>{s['n']} blocks</b> between "
                 f"<b>{min(rows, key=int)}</b> and <b>{max(rows, key=int)}</b>, each executed by both "
                 f"guests on the {AXES[ax]['backend'].upper()} emulator.</p>")
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
                     f"<div class='u blk'>pure emulation on this host, measured with SP1's "
                     f"gas-estimation pass switched off"
                     + (f" (it would add ×{ov:.2f})" if ov else "") + "</div></div>")
        elif s.get('a_secs_median'):
            h.append(f"<div class=card><div class=k>median exec time</div>"
                     f"<div class=duo><span class=cA>{s['a_secs_median']:.2f}s</span>"
                     f"<span class=sep>vs</span><span class=cB>{s['b_secs_median']:.2f}s</span></div>"
                     f"<div class='u blk'>pure emulation on this host</div></div>")
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
            gap = "lower than" if s['pw_ratio_median'] < s['ratio_median'] else "higher than"
            verdict += (f" Measured as <b>prover work</b> the gap is <b>{pw_pct:+.1f}%</b> — {gap} the "
                        f"{u} gap, i.e. the extra work sits in operations that are "
                        f"{'cheaper' if s['pw_ratio_median'] < s['ratio_median'] else 'dearer'} than "
                        f"average to prove.")
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
        if s.get('a_secs_median'):
            items.append(("median exec secs", s.get('a_secs_median'), s.get('b_secs_median'),
                          lambda v: f"{v:.2f}s"))
        # No prose here: the labels carry the numbers. The one thing a careful reader may wonder —
        # why these ratios differ slightly from the cards' — lives in the heading's tooltip:
        # these are aggregate ÷ aggregate (medians/totals), the cards are the median of per-block
        # ratios, and dividing two medians ≠ the median of the divisions. Both are wanted: the
        # aggregate answers "how much more work in total", the per-block median "what a typical
        # block costs". Each pair of bars is scaled to its own max, so bar length is only
        # comparable within a row.
        h.append(f"<div class=pane><h2 title=\"Each pair is scaled to its own maximum, so compare bar "
                 f"length only within a row. These ratios are aggregate ÷ aggregate (of medians or "
                 f"totals); the cards above show the median of the per-block ratios, which differs "
                 f"slightly — dividing two medians is not the median of the divisions.\">"
                 f"{A} <span style='color:var(--gold)'>▬</span> vs {B} "
                 f"<span style='color:var(--blue)'>▬</span></h2>{_bars(items, A, B)}</div>")
        h.append("</div>")
        # ── where the gap comes from ──
        # The question the distribution raises but cannot answer: not "how big is the gap" but
        # "what is it made of". Both halves are medians ACROSS blocks (see summarize).
        if s.get('families') or s.get('insn_ratios'):
            h.append("<div class=grid2>")
            if s.get('families'):
                fa, fb, ta, tb, picks = s['families']
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
                         f"<span class=cB>{B}</span> below.</p>"
                         f"<table><tr><th>kind of work</th><th>instructions</th><th>ratio</th>"
                         f"<th></th></tr>")
                doc = {
                    'C++ abstraction layer':
                        'Boost.Outcome result wrappers, STL iterators and containers, hash maps, '
                        'smart-pointer destructors — the machinery a C++ codebase carries around its '
                        'own logic. Not the logic itself.',
                    'state / trie': 'Merkle-trie traversal, node decoding, RLP, storage/account lookups.',
                    'EVM interpreter': 'Opcode dispatch and execution of the block\u2019s transactions.',
                    'hashing (keccak/sha)': 'Keccak/SHA rounds, whether a precompile or a software routine.',
                    '256-bit arithmetic': 'mulmod/addmod/division on 256-bit words, done in software.',
                    'memory / allocation': 'memcpy/memset, allocator work.',
                    'signature recovery': 'ecrecover: secp256k1 curve operations.',
                    'other': 'Names no family pattern matched.',
                }
                for k in fams:
                    va, vb = fa.get(k, 0), fb.get(k, 0)
                    r = (va / vb) if vb else None
                    tip = doc.get(k, '')
                    h.append(f"<tr><td style='text-align:left'"
                             + (f" title=\"{tip}\"" if tip else "") + f">{k}</td>"
                             f"<td><span class=cA>{n(va)}</span><br>"
                             f"<span class=cB>{n(vb)}</span></td>"
                             f"<td class={'hi' if r and r >= 1 else 'lo'}>{x(r)}</td>"
                             f"<td style='width:28%'><span class='rbar"
                             f"{' over' if r and r >= 1 else ''}' "
                             f"style='width:{max(2, 100*va/mx):.0f}%'></span></td></tr>")
                h.append(f"</table><p class=note>{len(picks)} profiled runs per guest, cached until "
                         f"the guest is rebuilt"
                         + (f". This backend's profiler <b>samples</b> (1 in {s['fam_scale']:.0f}), so "
                            f"the counts are scaled estimates; the ratios are unaffected"
                            if s.get('fam_scale', 1) > 2 else "")
                         + f". <b>C++ abstraction layer</b> is Boost.Outcome result wrappers, STL "
                         f"iterators and hash containers, smart-pointer destructors — the machinery a "
                         f"C++ codebase carries around its logic, not the logic itself"
                         + f". Attributed: <span class=cA>{n(ta)}</span> / "
                         f"<span class=cB>{n(tb)}</span>; unmatched names go to <i>other</i>.</p></div>")
            if s.get('insn_ratios'):
                # INSTRUCTION COUNTS, not cost. The cost view answered "what will proving charge",
                # which is a different question and needed a `Main` row that merely restated the
                # headline ratio. Counts answer "what does the guest actually execute more of", and
                # the baseline is the overall work-unit ratio.
                base = s['ratio_median']
                top = s['insn_ratios'][:10]
                mx = max([t[3] or 0 for t in top] + [1])
                h.append(f"<div class=pane><h2>what kind of work, and how much of it</h2>"
                         f"<p class=note style='margin:0 0 10px'>Instructions executed per block "
                         f"(median over the {s['n']} blocks), by operation. Overall "
                         f"<span class=cA>{A}</span> runs <b>{x(base)}</b> the {u} of "
                         f"<span class=cB>{B}</span> — an operation whose ratio beats that is where "
                         f"the extra work concentrates; one below it is work {A} does <i>less</i> "
                         f"of.</p>"
                         f"<table><tr><th>operation</th><th>per block</th><th>ratio</th>"
                         f"<th>vs {x(base)} overall</th><th></th></tr>")
                for k, v, cnt, ca, cb in top:
                    rel = (v / base - 1) * 100
                    w = max(2, 100 * (ca or 0) / mx)
                    h.append(f"<tr><td style='text-align:left' title='measured on {cnt} block(s) "
                             f"where both guests report it'>{k}</td>"
                             f"<td><span class=cA>{n(ca)}</span><br><span class=cB>{n(cb)}</span></td>"
                             f"<td class={'hi' if v >= 1 else 'lo'}>{v:.2f}×</td>"
                             f"<td class={'hi' if rel > 0 else 'lo'}>{rel:+.0f}%</td>"
                             f"<td style='width:26%'><span class='rbar{' over' if v >= base else ''}' "
                             f"style='width:{w:.0f}%'></span></td></tr>")
                h.append(f"</table><p class=note>These are counts of executed instructions — nothing "
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
                     f"<p class=note style='margin:0 0 10px'>Almost every block lands near "
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
        h.append("<details><summary>▸ every block — %d rows: %s, %sgas, txs, ratio</summary>"
                 "<div class=pane>" % (len(rows), u, (pwu + ", ") if pwu else ""))
        pwcols = f"<th>{A} {pwu}</th><th>{B} {pwu}</th><th>{pwu} ratio</th>" if pwu else ""
        h.append(f"<table><tr><th>block</th><th>gas</th><th>txs</th><th>{A} {u}</th><th>{B} {u}</th>"
                 f"<th>ratio</th>{pwcols}</tr>")
        zs = {e['block']: e['z'] for e in outs}
        rmax = max(ratios) or 1
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
            bar = (f"<span class='rbar{' over' if r and r>=1 else ''}' "
                   f"style='width:{max(2, round(38*r/rmax))}px'></span>") if r else ""
            tx = r0['b'].get('txs') or r0['a'].get('txs') or tx_map.get(b)
            h.append(f"<tr><td title=\"{'flagged as off-pattern' if z else ''}\">"
                     f"{'⚠ ' if z else ''}{b}</td><td>{n(g)}</td><td>{n(tx)}</td>"
                     f"<td>{n(r0['a']['work'])}</td><td>{n(r0['b']['work'])}</td>"
                     # threshold 1x, like the bar beside it and the histogram legend — not the
                     # median, which left every below-median ratio with no colour at all
                     f"<td class={'hi' if r and r >= 1 else 'lo'}>{bar}{x(r)}</td>{pw}</tr>")
        h.append("</table></div></details>")
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
    h.append(f"<p class=note style='margin-top:34px;border-top:1px solid var(--line);padding-top:16px'>"
             f"<b>Measured on</b> {_hostinfo()} · {time.strftime('%Y-%m-%d %H:%M %Z')}. Work-units "
             f"({'/'.join(sorted({AXES[s['axis']]['unit'] for s in summaries}))}) and prover-work figures "
             f"are deterministic — identical on any machine. Only the exec-time column depends on this "
             f"host.</p>")
    h.append("</div>")
    h.append(f"<script>{_HIST_JS}</script>")
    open(path, 'w').write("\n".join(h))
    print(f"\nwrote {path}")

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
                if side['src'] == 'monad-framed':
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
            if side['src'] == 'monad-framed':
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
                    help='which same-zkVM pair (default: all)')
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
    ap.add_argument('--families', type=int, default=10, metavar='N',
                    help='profile N blocks per guest, stratified across the ratio distribution, to '
                         'break the work down by kind (default 10; 0 disables). One profiled '
                         'execution each — ~13s on ZisK, ~24s on SP1 — cached until the guest ELF '
                         'changes. Measured: 5 -> 10 moved every family by 1-4%% except the '
                         'EVM-interpreter row (-17%%), so 10 is the safer default')
    ap.add_argument('--no-report', action='store_true',
                    help='terminal summary only: skip the HTML and JSON files')
    ap.add_argument('--emu', default='~/.zisk/bin/ziskemu')
    ap.add_argument('--runner', default=os.path.join(REPO, 'infra/sp1-infra/sp1-runner/target/release/sp1-runner'))
    args = ap.parse_args()
    # Subprocesses (hotspots.py) write straight to the terminal; keep our own output in step
    # with theirs when stdout is a pipe, or the --deep sections land out of order.
    try: sys.stdout.reconfigure(line_buffering=True)
    except Exception: pass

    tools ={'zisk': os.path.expanduser(args.emu), 'sp1': os.path.expanduser(args.runner)}
    axes = args.axis or sorted(AXES)
    want = parse_blocks(args.blocks) if args.blocks else None
    cache = load_cache()
    summaries, allrows, payload = [], {}, {}

    collected = []
    for axis in axes:                                    # 1) run everything first…
        if not os.path.exists(tools[AXES[axis]['backend']]):
            print(f"skip {axis}: {AXES[axis]['backend']} tool not found ({tools[AXES[axis]['backend']]})")
            continue
        # Guest ELFs, checked here rather than deep inside collect() — a missing one used to surface
        # as a raw FileNotFoundError from the cache's mtime stamp.
        missing = [rp(AXES[axis][k]['elf']) for k in ('a', 'b')
                   if not os.path.exists(rp(AXES[axis][k]['elf']))]
        if missing:
            print(f"skip {axis}: guest ELF not built — {', '.join(missing)}")
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

    # 2) …pool EVM gas. It's a property of the BLOCK, and only the reth ZisK guest prints it, so
    # read it from the whole CACHE — otherwise an --axis sp1 run shows no gas even though a past
    # zisk run already measured it.
    gas_map, tx_map = {}, {}
    for k, v in cache.items():
        if not isinstance(v, dict): continue
        try: blk = int(k.split('/')[2])
        except (IndexError, ValueError): continue
        if v.get('gas'): gas_map[blk] = v['gas']
        if v.get('txs'): tx_map[blk] = v['txs']          # tx count is a block property too

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
                s['fam_scale'] = round(max(ka, kb), 1)      # >1 means sampled, not counted
                save_cache(cache)
        print_summary(s)
        print_outliers(s, rows, entries, mad, args.show_outliers, gas_map)
        if args.deep: deep(axis, blocks[:args.deep], args)
        if args.spread: spread(axis, s, args.spread_side)

    if not summaries: return 1
    # One command = one full run: the report is produced unless explicitly declined.
    if not args.no_report:
        args.html_out = args.html_out or os.path.join(HERE, 'results', 'compare.html')
        args.json_out = args.json_out or os.path.join(HERE, 'results', 'compare.json')
    if args.json_out:
        json.dump(payload, open(args.json_out, 'w'), indent=1); print(f"\nwrote {args.json_out}")
    if args.html_out:
        os.makedirs(os.path.dirname(args.html_out), exist_ok=True)
        write_html(args.html_out, summaries, allrows, gas_map, tx_map)
    return 0

if __name__ == '__main__':
    sys.exit(main())
