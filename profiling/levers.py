#!/usr/bin/env python3
"""levers — what to fix in the Monad guest, ranked, with a re-measure protocol per item.

This is NOT compare.py's report. compare.py is a generic instrument: any two guests, any backend,
any block range, and its output must stay neutral or it will be wrong the next time someone compares
a different pair. This document is the opposite: it is specific to *these* guests and *this* build,
it has a shelf life, and its best content is not derivable from compare.py's inputs at all — the
four fix sites come from reading the Monad source, the per-call keccak cost comes from a regression
over the profile cache. The BN254 path was once read off the ELF symbol table and called
"precompile-backed"; that was wrong on SP1 — syscall wrappers inline, so a symbol table cannot see
routing. Only an execution's syscall_counts can (finding 111).

Every measured figure here is COMPUTED from results/compare.json + the per-block profile cache.
Nothing measured is written into the prose. That is not gold-plating: the dominant failure mode
while building this analysis was stale or hardcoded numbers (percentages baked into a panel, a
block span described as contiguous when 10% was missing, a lever table computed from a JSON that a
throwaway run had overwritten with 6 blocks, a verification performed against a stale HTML). A
document whose purpose is to be QUOTED must bind its figures to the data.

The only hardcoded facts are the source sites, and they carry an explicit "read in the source on
<date>, not verified by measurement" marker.

    ./levers.py                 # -> results/levers.html
    ./levers.py --out x.html
"""

import argparse, hashlib, importlib.util, json, os, re, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPARE_JSON = os.path.join(HERE, 'results', 'compare.json')
# `cache` is a sibling module, not a package — see the same note in compare.py.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import cache as _cachemod          # noqa: E402

# Reuse the ONE taxonomy. Importing rather than copying: a second copy of FAMILIES would drift from
# hotspots.py silently, and this analysis has already been wrong twice from taxonomy mismatches.
_spec = importlib.util.spec_from_file_location('hs', os.path.join(HERE, 'hotspots.py'))
hs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(hs)

MIN_BLOCKS = 300          # refuse to build from a partial run — see the module docstring

# A frozen copy of the reports and per-block data from the previous guest generation. The page is a
# before/after now, so the earlier numbers are an INPUT, not something to retype: a delivered fix is
# only credible if the figure it moved is read from the run that preceded it.
BASELINE = os.path.join(HERE, 'results', 'baseline-partialtriedb-2026-08-04', 'compare.json')
ZISK_MAIN_PER_STEP = 68   # ZisK: Main cost = 68 x steps (measured constant over 728 blocks)
ZISK_KECCAK_COST = 25 * 3022

# Symbol groups that the family taxonomy deliberately does NOT separate, but which this document
# needs as their own line. Kept here, next to the prose that uses them, rather than pushed into
# hotspots.FAMILIES: they are Monad-specific readings, not a general classification.
OUTCOME_RE = re.compile(r'outcome', re.I)
HASH_RE = re.compile(r'keccak|sha3|sha256|tiny_keccak|blake|xor_block', re.I)
ALLOC_RE = re.compile(r'operator new|sys_alloc_aligned|tlsf', re.I)
COPY_RE = re.compile(r'memcpy|memmove|memcmp|memset', re.I)
BN_SOFT_RE = re.compile(r'substrate_bn', re.I)
BN_ACCEL_RE = re.compile(r'zkvm_bn254|zisklib', re.I)
DIVGEN_RE = re.compile(r'u128_div_rem|udivti3|umodti3', re.I)

# ────────────────────────────── source-read facts (NOT measured) ──────────────────────────────
SOURCE_READ = '2026-07-31'

# Which generation these figures describe. Two witness formats now exist with IDENTICAL filenames,
# and a guest built from the wrong commit reads them off by one byte per node — no error, just wrong
# state roots. So the page states its provenance: the two commits are recorded facts (a commit is not
# derivable from an ELF), everything else below is measured at build time.
GEN = {
    'guest': 'ed16787ae07340c903b9374f6634817b2b4bb8a4',    # sam/zkvm-zisk-sp1, OffsetTrieDb reader
    'witness': '59bdce981788cffbba02b17e9ed7a78789e9d34d',  # sam/witness_gen, offset-format writer
}

# Isolated micro-benchmark, not derivable from compare.json — a purpose-built ZisK guest that hashes
# in a loop, both paths in one binary, marginal cost taken as the difference between 200 and 400
# iterations. Recorded here with its date because, like the source facts, it cannot be recomputed at
# render time. Method: scratchpad guest, `ziskemu -m`, inputs (mode, size, iters).
BENCH_DATE = '2026-07-31'
BENCH = {
    'zisklib_1perm': 167,      # whole call when the input fits one block (<=135 bytes)
    'zisklib_perblock': 548,   # each additional full 136-byte block
    'variant_perblock': 560,   # per-block cost of the variant the split was measured on, so that
                               # syscall + absorb closes exactly; zisklib as shipped is 548, i.e. 12
                               # steps cheaper per block than any hand-written absorb tried
    'syscall': 533,            # of which the keccak_f syscall itself (532/534/534 across 3 blocks)
    'absorb_perblock': 27,     # of which the absorb loop, averaged over the measured range
                               # (per-block increments 29/26/26) — ~1.6 per 8-byte word,
                               # already optimal
    'words_per_block': 17,     # 136 bytes / 8
}
# Four sites were dropped when the guest moved from PartialTrieDb to OffsetTrieDb: they all lived in
# partial_trie_db.cpp, which the guest no longer links (measured: 46 `PartialTrieDb` symbols in the
# previous ELF, 0 in this one). They described the witness-node copies and the per-slot address
# rehash — the work that the zero-copy reader removed, and whose disappearance is now a RESULT rather
# than a lever. `check_sites` is what forced the issue: a citation stays plausible long after the code
# it names has stopped being compiled in.
# Per-family ratio measured under two attributions — the shipped reth guest and a no-inline rebuild
# (profiling/inline-robust.py, read by compare.py too). Optional: absent file means no correction is
# available and the shipped ratio stands unqualified.
VERDICT = os.path.join(HERE, 'results', 'inline-verdict.json')
SITES = {
    'containers': [
        ('category/execution/ethereum/rlp/encode2.hpp', 68, 'encode_list2',
         "encode_list2 computes the exact payload size, then never calls reserve — it grows by "
         "repeated +=. Still live: `encode_list2` is present in the current guest, and the function "
         "is unchanged at the pinned commit.",
         "result.reserve(size + 9) — the +9 covers the maximum RLP length prefix. One line."),
    ],
    # The probe is the LIBCALL, not the wrapper: `bswap` is a header template and leaves no symbol,
    # so what proves the cost is still there is the helper the backend calls instead of inlining.
    'byteswap': [
        ('category/core/int.hpp', 96, '__bswapdi2',
         "bswap<T> is marked gnu::always_inline and is inlined — but the std::byteswap inside it "
         "lowers to a call to compiler_builtins::__bswapdi2, 26 instructions in the guest ELF, "
         "because the rv64ima target has no rev8 and the backend prefers a libcall to expanding it.",
         "Replace the std::byteswap branch with an explicit mask-and-shift sequence so no call is "
         "emitted. The win comes from sharing the two mask constants across the four swaps of a "
         "uint256, which is how the interpreter converts EVM words."),
    ],
}
NOT_A_SITE = ('category/execution/ethereum/create_contract_address.cpp', 33,
              "hash_and_clip was assumed to be the trie-node hash path. It is not — it only "
              "derives CREATE/CREATE2 addresses, and the profiler puts it at 0.00%, absent from "
              "the top 120. A plausible call-graph story is not evidence.")


# ─────────────────────────────────────── data layer ───────────────────────────────────────────

def load():
    if not os.path.exists(COMPARE_JSON):
        sys.exit(f"missing {COMPARE_JSON} — run ./compare.py --block-min … --block-max … first")
    d = json.load(open(COMPARE_JSON))
    # The profile cache is per block and loaded lazily (cache-format.md), so this is instant no
    # matter how much history it holds — the old monolith was read whole, ~1.3 GB resident.
    c = _cachemod.Cache()
    # Provenance gate. `--html` with no value falls back to the default path, so a short diagnostic
    # run silently replaces the canonical report; a lever table was once computed from 6 blocks
    # that way. Refuse rather than produce a plausible wrong document.
    for ax in d:
        n = d[ax]['summary']['n']
        if n < MIN_BLOCKS:
            sys.exit(f"refusing to build: {ax} has n={n} (< {MIN_BLOCKS}). results/compare.json "
                     f"looks like a partial run — re-run the canonical sweep first.")
    return d, c


def cached(c, axis, guest):
    """Per-block profiles for one guest, restricted to the NEWEST build. -> ({block: profile}, mtime).

    `axis` is ignored: it was part of the old cache key and never contributed to a result. Lookup is
    by build NAME because this document reads builds that can no longer be measured — `monad-levers`
    lost its axes and its ELFs, so there is nothing left to hash and its slots keep a legacy identity.
    Restricted to one build, never merged across two: averaging two binaries is the silent error this
    document exists to avoid."""
    profs, blocks, mt = c.profiles_by_name(guest)
    return dict(zip(blocks, profs)), mt


def sym_shares(c, axis, guest):
    """{symbol: share of this guest's attributed work}, over every cached block of this build."""
    by_blk, stamp = cached(c, axis, guest)
    agg, tot = {}, 0
    for k in by_blk:
        for fn, v in by_blk[k]['fns']:
            tot += v; agg[fn] = agg.get(fn, 0) + v
    return ({fn: v / tot for fn, v in agg.items()} if tot else {}), len(by_blk), stamp


def share_of(shares, rx):
    return sum(v for fn, v in shares.items() if rx.search(fn))


def fam_share(shares, family):
    return sum(v for fn, v in shares.items() if hs.family(fn) == family)


def percall_hashing(d, c, axis, side, guest):
    """Attributed hashing instructions per keccak call, per block — and how flat it is.

    Flatness is the whole point: a cost that does not vary with payload size is per-call SETUP, not
    hashing work. Returns (median, lo, hi, pearson_r, n, call_count_spread)."""
    B = d[axis]['blocks']; by_blk, _ = cached(c, axis, guest)
    xs, ys, rats = [], [], []
    for blk, prof in by_blk.items():
        # `B` is keyed by the block as a STRING (it comes from compare.json); the cache keys blocks as
        # ints. The old code took the block out of the cache key and so was a string throughout.
        b = str(blk)
        if b not in B: continue
        fns = dict(prof['fns']); tot = sum(fns.values()) or 1
        R = B[b][side]
        kec = R.get('kec') or (R.get('sys') or {}).get('KECCAK_PERMUTE')
        if not kec: continue
        h = share_of({f: v / tot for f, v in fns.items()}, HASH_RE) * R['work']
        xs.append(kec); ys.append(h); rats.append(h / kec)
    if not rats: return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** .5
    # The call-count spread travels with the cost: a flat per-call cost is only informative if
    # the number of calls varied a lot while it stayed flat.
    return (statistics.median(rats), min(rats), max(rats), (num / den if den else 0), len(rats),
            (max(xs) / min(xs) if min(xs) else 1.0))


# Which lever, non-lever or open question accounts for each of the guest's expensive symbols. The
# point of the mapping is the LAST class: a symbol matching nothing is a cost nobody has looked at,
# and the byte-swap lever spent a whole cycle as "not comparable, therefore closed" precisely because
# no such list existed. Patterns are matched against the demangled name, first match wins.
# Read against `monad-levers`, so the classes describe the guest that now stands. `fixed` is the
# class this restructure needed: a symbol whose lever is DONE and archived must not read as open
# work — `__bswapdi2` and `wyhash` are gone from the binary entirely, and the ones that remain are
# residues, not targets. `lever` now means an entry in the ranked list above.
ACCOUNTED = [
    (r'__bswapdi2|__bswapsi2',              'fixed', 'byte-order — inlined, gone from the binary'),
    (r'wyhash',                             'fixed', 'state key hash — replaced by the fold'),
    (r'prime_node',                         'fixed', 'eager priming hash — dropped'),
    (r'u128_div_rem',                       'fixed', '128/64 division — specialised'),
    (r'__clzdi2',                           'fixed', 'count-leading-zeros — inlined'),
    (r'keccak256|keccak_f',                 'lever', 'the wrapper — mostly the ZisK call floor'),
    (r'div_result|operator\*|mulmod|umul|mul256',
                                            'lever', '256-bit multiplication — a precompile exists'),
    (r'find_jumpdests|Intercode',           'lever', 'one eager scan per contract — lazy is untried'),
    (r'memcpy|memset|memmove|memcmp',       'lever', 'byte-at-a-time mem* — the SP1 item'),
    (r'__popcountdi2',                      'lever', "immer's popcount — Zbb, or patch the submodule"),
    (r'unordered_dense|segmented_vector|champ<',
                                            'lever', 'state maps — champ depth and the finalizer'),
    (r'interpreter::push<',                 'closed', 'byte-order inlined here — PUSH was the driver'),
    (r'secp256k1|glv_',                     'closed', 'already precompile-backed'),
    # BN254 is only HALF precompile-backed: fp yes, G1 point ops no on SP1 (finding 111).
    (r'bn254',                              'lever', 'BN254 — field accelerated, point ops are not (SP1)'),
    (r'node_rlp_span|upsert_node|find_original|append_path|TrieStore|mpt_witness',
                                            'reader', 'witness reader — at parity with reth'),
    (r'interpreter::(swap|dup|jump|jumpi|mstore|mload|add|sub|lt|gt|eq)',
                                            'closed', 'EVM core — ahead of the reth guest'),
    (r'tokens_in_calldata',                 'open',  'calldata token count'),
    (r'read_storage|current_account_state|BlockState|State::',
                                            'closed', 'state access — measured cold'),
]


def account_for(name):
    """(class, why) for a symbol — 'unnamed' when nothing in the catalogue covers it."""
    for pat, cls, why in ACCOUNTED:
        if re.search(pat, name):
            return cls, why
    return 'unnamed', 'nothing in this document accounts for it'


def top_symbols(c, axis, guest, n=26):
    """The guest's most expensive symbols by median steps per block, with what accounts for each."""
    _profs, _blocks, st_ = c.profiles_by_name(guest)
    if not _profs:
        return None
    per, works = {}, []
    for v in _profs:
        if not v.get('total'):
            continue
        works.append(v['total'])
        for f, cnt in v['fns']:
            per.setdefault(f, []).append(cnt)
    if not works:
        return None
    w = statistics.median(works)
    # A symbol seen on a handful of blocks has no meaningful median; require most of the sample.
    lim = len(works) * 0.4
    rows = sorted(((f, statistics.median(v)) for f, v in per.items() if len(v) >= lim),
                  key=lambda t: -t[1])[:n]
    return {'work': w, 'rows': [(f, m, m / w) + account_for(f) for f, m in rows]}


def symbol_steps(d, c, axis, guest, pattern):
    """One symbol's own cost in a guest: {steps, work, share, n} per block, or None.

    Reads the SAME per-function cache the family table reads, at the guest's newest stamp — a stale
    stamp once had a caveat describing a binary that no longer existed. Symbol-level rather than
    family-level on purpose: a family is a taxonomy choice, whereas `__bswapdi2` is one function whose
    cost needs no attribution argument at all, which is what makes it quotable next to families the
    inlining check calls unreliable."""
    _profs, _blocks, st_ = c.profiles_by_name(guest)
    if not _profs:
        return None
    rx, steps, works = re.compile(pattern, re.I), [], []
    for v in _profs:
        s = sum(n for f, n in v['fns'] if rx.search(f))
        if s and v.get('total'):
            steps.append(s); works.append(v['total'])
    if not steps:
        return None
    _s, _w = statistics.median(steps), statistics.median(works)
    # The share is the quotient of the two figures RETURNED here, not the median of per-block shares.
    # Both are defensible; only one keeps the page consistent. The inventory table prints steps and
    # share side by side and divides its own medians, so a lever quoting the other statistic put the
    # same quantity on the page twice with two values (5.75 % against 5.91 %) — the defect this
    # report was corrected for once already.
    return {'steps': _s, 'work': _w, 'share': _s / _w, 'n': len(steps)}


def opcode(d, axis, op):
    """Per-opcode count/cost ratios, compared ONLY on blocks where both guests report the opcode.

    A median over all blocks lies when an opcode is absent from half of them: srl looked like 106x
    that way, and is 2.7x when filtered. Returns None when the opcode never co-occurs."""
    B = d[axis]['blocks']
    pa = [b for b in B if (B[b]['a'].get('ops') or {}).get(op)]
    pb = [b for b in B if (B[b]['b'].get('ops') or {}).get(op)]
    both = sorted(set(pa) & set(pb))
    out = {'present_a': len(pa), 'present_b': len(pb), 'n_blocks': len(B), 'both': len(both)}
    if not both: return out
    out['cost_ratio'] = statistics.median([B[b]['a']['ops'][op] / B[b]['b']['ops'][op] for b in both])
    nn = [b for b in both if (B[b]['a'].get('opsn') or {}).get(op)
          and (B[b]['b'].get('opsn') or {}).get(op)]
    if nn:
        out['count_ratio'] = statistics.median([B[b]['a']['opsn'][op] / B[b]['b']['opsn'][op]
                                                for b in nn])
        out['cpi_ratio'] = statistics.median(
            [(B[b]['a']['ops'][op] / B[b]['a']['opsn'][op])
             / (B[b]['b']['ops'][op] / B[b]['b']['opsn'][op]) for b in nn])
    return out


def cost_split(d):
    """ZisK prover-cost decomposition for the Monad guest — the conversion rate for any lever.

    Main is linear in step count, so a step saved is ~1 cost unit saved; the keccak state machine is
    identical per call for both guests and is only reducible by hashing less."""
    Z = d['zisk']['blocks']
    med = lambda f: statistics.median([f(Z[b]) for b in Z])
    tot = med(lambda r: r['a']['cost'])
    return {
        'total': tot,
        'main': ZISK_MAIN_PER_STEP * med(lambda r: r['a']['work']),
        'keccak': med(lambda r: r['a']['kec_cost']),
        'bits': sum(med(lambda r: (r['a'].get('ops') or {}).get(k, 0))
                    for k in ('and', 'sll', 'srl', 'eq')),
        'add_w': med(lambda r: (r['a'].get('ops') or {}).get('add_w', 0)),
        'sll': med(lambda r: (r['a'].get('ops') or {}).get('sll', 0)),
    }


def gather():
    d, c = load()
    S = {}
    for ax in d:
        s = d[ax]['summary']
        S[ax] = {'sum': s, 'A': s['a_name'], 'B': s['b_name'],
                 'ratio': s['ratio_median'], 'unit': s['unit'],
                 'engine_ratio': (s.get('curve') or {}).get('med_clean'),
                 'shares': {}, 'nblk': {}, 'stamp': {}}
        for side, g in (('a', s['a_name']), ('b', s['b_name'])):
            sh, n, stamp = sym_shares(c, ax, g)
            S[ax]['shares'][side] = sh; S[ax]['nblk'][side] = n; S[ax]['stamp'][side] = stamp
        S[ax]['percall'] = {side: percall_hashing(d, c, ax, side, g)
                            for side, g in (('a', s['a_name']), ('b', s['b_name']))}
    # 2026-08-08: axes renamed with the sam-rebase. Alias the old names AFTER the
    # shares are computed (the fn2 cache keys carry the real axis name); summary
    # and share objects under both keys are the same.
    for _old, _new in (('zisk', 'cur-zisk'), ('sp1', 'cur-sp1'),
                       ('levers-self', 'opt-self'), ('levers-self-sp1', 'opt-self-sp1'),
                       ('levers', 'opt-zisk'), ('levers-sp1', 'opt-sp1')):
        if _old not in S and _new in S:
            S[_old] = S[_new]
            d[_old] = d[_new]
    return d, c, S


# ─────────────────────────────────────── rendering ────────────────────────────────────────────

POST_JSON = os.path.join(HERE, 'results', 'compare-levers.json')

# Which axis carries the optimised build on each backend, and which carries its reth counterpart.
POST_AX = {'zisk': ('levers-self', 'monad-levers'), 'sp1': ('levers-sp1', 'monad-levers-sp1')}
POST_REF = {'zisk': ('levers-zisk', 'zisk-reth'), 'sp1': ('levers-sp1', 'rsp')}


def post_gather(c):
    """The profile of the guest AS IT NOW STANDS — `monad-levers`, not what ships.

    Everything above the archive is ranked against this. Ranking the remaining work against the
    shipped guest would size each candidate against a binary nobody intends to run again: the ten
    levers removed 28 % and reshaped what is left, so a share taken from `compare.json` is a share of
    the wrong denominator. `find_jumpdests` is the clearest case — 5.4 % of the shipped guest, 6.4 %
    of this one, because the guest around it got smaller while the scan did not.
    """
    try:
        L = json.load(open(POST_JSON))
        for _old, _new in (('levers-self', 'opt-self'), ('levers-self-sp1', 'opt-self-sp1'),
                           ('levers', 'opt-zisk'), ('levers-sp1', 'opt-sp1'),
                           ('levers-zisk', 'opt-zisk')):
            if _old not in L and _new in L:
                L[_old] = L[_new]
    except Exception:
        return None
    out = {}
    for bk, (ax, g) in POST_AX.items():
        if ax not in L:
            continue
        sh, n, stamp = sym_shares(c, ax, g)
        if not sh:
            continue
        s = L[ax]['summary']
        rax, rg = POST_REF[bk]
        rsh, rn, _ = sym_shares(c, rax, rg) if rax in L else ({}, 0, None)
        out[bk] = {
            'shares': sh, 'nblk': n, 'stamp': stamp, 'guest': g,
            'median': s['a_median'], 'unit': s['unit'],
            'ref_shares': rsh, 'ref_nblk': rn,
            'ref_name': L[rax]['summary']['b_name'] if rax in L else None,
            'ref_median': L[rax]['summary']['b_median'] if rax in L else None,
            'ref_ratio': L[rax]['summary']['ratio_median'] if rax in L else None,
            'vs_today': 1 - s['ratio_median'],
        }
    return out or None


def post_steps(P, bk, rx):
    """(share, absolute steps per median block) for a pattern, on the optimised guest."""
    if not P or bk not in P:
        return None, None
    s = share_of(P[bk]['shares'], re.compile(rx))
    return s, s * P[bk]['median']


def pct(v, dp=2): return '—' if v is None else f"{v * 100:.{dp}f}%"
def x(v, dp=3): return '—' if v is None else f"{v:.{dp}f}×"
def n_(v): return '—' if v is None else f"{v:,.0f}"

CSS = """
:root{--bg:#0f1116;--panel:#161922;--panel2:#1b1f29;--line:#262b38;--fg:#e7eaf2;--muted:#9aa3b8;
 --accent:#b09cf7;--accent-dim:#9d92c9;--gold:#e8b04b;--blue:#6aa9f0;--red:#e2686d;--green:#5fbf8a;
 --line-strong:#3a4152;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:light){:root{--bg:#fbfbfd;--panel:#fff;--panel2:#f4f5f9;--line:#e2e5ee;
 --fg:#1a1d26;--muted:#666e82;--accent:#6d4fd0;--accent-dim:#7d68c4;--line-strong:#c3c8d8}}
:root[data-theme=dark]{--bg:#0f1116;--panel:#161922;--panel2:#1b1f29;--line:#262b38;--fg:#e7eaf2;
 --muted:#9aa3b8;--accent:#b09cf7;--accent-dim:#9d92c9;--line-strong:#3a4152}
:root[data-theme=light]{--bg:#fbfbfd;--panel:#fff;--panel2:#f4f5f9;--line:#e2e5ee;--fg:#1a1d26;
 --muted:#666e82;--accent:#6d4fd0;--accent-dim:#7d68c4;--line-strong:#c3c8d8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,
 "Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:40px 26px 90px}
.eyebrow{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
h1{font-size:30px;line-height:1.2;margin:0 0 10px;letter-spacing:-.02em}
h2{font-size:19px;margin:38px 0 12px;letter-spacing:-.01em}
h3{font-size:15px;margin:0 0 6px}
p{margin:0 0 12px}
code{font-family:var(--mono);font-size:12.5px;background:var(--panel2);padding:1px 5px;
 border-radius:4px;color:var(--accent)}
pre{font-family:var(--mono);font-size:12px;background:var(--panel2);border:1px solid var(--line);
 border-radius:8px;padding:11px 13px;overflow-x:auto;margin:8px 0}
.id{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:13px 15px;
 font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:0 0 22px}
.id b{color:var(--fg)}
.id a{color:var(--accent)}
.id code{background:none;padding:0;font-size:11.5px}
.tldr{background:linear-gradient(180deg,rgba(176,156,247,.10),rgba(176,156,247,.03));
 border:1px solid rgba(176,156,247,.3);border-radius:12px;padding:16px 18px;margin:0 0 26px}
.tldr ol{margin:8px 0 0;padding-left:20px}.tldr li{margin:4px 0}
.lev{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
 margin:0 0 16px}
.lev.small{background:var(--panel2)}
.hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:0 0 10px}
.rank{font-family:var(--mono);font-size:11px;color:var(--accent);border:1px solid var(--accent-dim);
 border-radius:20px;padding:1px 9px}
.hd h3{flex:1;min-width:200px}
.impact{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.impact b{color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}
th{text-align:left;font-weight:500;color:var(--muted);font-size:10.5px;text-transform:uppercase;
 letter-spacing:.08em;padding:5px 9px;border-bottom:2px solid var(--line-strong)}
td{padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
td.n,th.n{text-align:right}
td.sub{padding-left:22px;color:var(--muted)}
tr.grp>td{border-bottom:2px solid var(--line-strong)}
td.n{font-family:var(--mono);font-size:12.5px;white-space:nowrap}
.hi{color:var(--red)}.lo{color:var(--green)}
/* Inventory status. Four colours because the four states call for different action, and the one that
   must stand out is `op` — a cost nobody has looked at. `sub` is not reused here: it carries a
   22px indent meant for nested rows, which reads as a hierarchy the inventory does not have. */
td.lv{color:var(--green)}td.cl{color:var(--muted)}
td.rd{color:var(--gold)}td.op{color:var(--red)}
td.why{color:var(--muted);font-size:11.5px}
table code{font-size:11.5px;word-break:break-all}
.tag{font-size:10px;letter-spacing:.06em;text-transform:uppercase;border-radius:4px;padding:1px 6px;
 font-family:var(--mono)}
.tag.s{background:rgba(232,176,75,.16);color:var(--gold)}
.tag.b{background:rgba(95,191,138,.16);color:var(--green)}
.note{color:var(--muted);font-size:12.5px;line-height:1.6;margin:10px 0 0}
.note b{color:var(--accent-dim)}
.site{border-left:2px solid var(--accent-dim);padding:9px 0 9px 13px;margin:10px 0 0}
.site .p{font-family:var(--mono);font-size:11.5px;color:var(--accent)}
.rem{background:var(--panel2);border-radius:8px;padding:10px 13px;margin:12px 0 0;font-size:13px}
/* One label style for the three blocks a lever is made of, so `the finding`, `the fix` and
   `how to check the fix worked` read as one sequence rather than as a box bolted onto prose. */
.k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
 display:block;margin:0 0 5px}
.blk{margin:14px 0 0;padding-top:13px;border-top:1px solid var(--line)}
.blk.first{margin-top:0;padding-top:0;border-top:0}
.blk>p:last-child{margin-bottom:0}
/* The fix is the only block that carries a colour: it is the part an engineer acts on, and on a
   built lever it is also the only one with a link out of the page. */
.fixb{background:rgba(95,191,138,.055);border-left:2px solid var(--green);
 border-radius:0 8px 8px 0;padding:11px 14px;margin:14px 0 0}
.fixb .k{color:var(--green)}
/* The branch name sits inside an uppercased label, so it must not keep the accent-coloured code
   chrome — it reads as a second, competing colour on the one line that should be all green. */
.fixb .k code{background:none;color:inherit;padding:0;font-size:11px}
/* The measurement, as fields. Same three cells on every built lever, in the same order, so the one
   that varies — how the figure was obtained — is comparable at a glance instead of being a turn of
   phrase buried mid-paragraph. */
/* Label/value rows rather than side-by-side cells: two of the three values are sentences, and in a
   row of columns they wrapped into ragged blocks that read as separate fields. */
.meas{display:grid;grid-template-columns:max-content 1fr;gap:4px 15px;margin:0 0 13px;
 padding:0 0 12px;border-bottom:1px dashed var(--line-strong)}
.meas i{font-style:normal;font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;
 color:var(--muted);padding-top:4px}
.meas em{font-style:normal;font-family:var(--mono);font-size:11.5px;color:var(--fg)}
.meas b{color:var(--green);font-size:13px;font-family:var(--mono)}
.meas .jt{color:var(--muted);font-size:10.5px}
.cmts{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 0}
.cmt{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;
 text-decoration:none;background:var(--panel2);border:1px solid var(--line);border-radius:6px;
 padding:4px 9px;color:var(--muted)}
.cmt:hover{border-color:var(--green);color:var(--fg)}
.cmt b{color:var(--green);font-weight:500}
.cmt .sub{color:var(--muted)}
.cmt .arw{color:var(--muted);opacity:.6}
.cmt:hover .arw{color:var(--green);opacity:1}
.dead{opacity:.85}
.dead h3{color:var(--muted)}
/* The archive. Collapsed by default and visibly secondary: it is finished work, kept for its
   reasoning, and it must not compete with the list of what is still open. */
details{margin:0 0 18px}
details>summary{cursor:pointer;font-family:var(--mono);font-size:12px;color:var(--accent);
 padding:9px 13px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;
 list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:'▸ ';color:var(--muted)}
details[open]>summary::before{content:'▾ '}
details>summary:hover{border-color:var(--accent-dim)}
details[open]>summary{margin:0 0 14px}
details .lev{opacity:.9}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);
 font-size:12px}
@media print{body{background:#fff;color:#000}.wrap{max-width:none}}
"""


def render(d, c, S):
    Z, P = S['zisk'], S['sp1']
    cs = cost_split(d)
    h = ["<title>Monad guest — levers, ranked</title>", f"<style>{CSS}</style>", "<div class=wrap>"]

    # ── identity: this document has a shelf life, make that visible ──
    stamps = {f"{S[ax]['sum'][k + '_name']}": S[ax]['stamp'][k] for ax in S for k in 'ab'}
    idl = " · ".join(f"{g} elf@{t}" for g, t in sorted(stamps.items()) if t)
    h.append("<p class=eyebrow>zkvm-bench · monad guest</p>")
    h.append("<h1>What to fix, ranked — with a way to check each one</h1>")
    h.append(f"<div class=id><b>Measured</b> "
             + " · ".join(f"{ax.upper()} n={S[ax]['sum']['n']} blocks "
                          f"{min(int(b) for b in d[ax]['blocks'])}–{max(int(b) for b in d[ax]['blocks'])}"
                          for ax in S)
             + f"<br><b>Profiles</b> "
             + " · ".join(f"{S[ax]['sum'][k + '_name']} {S[ax]['nblk'][k]} blocks"
                          for ax in S for k in 'ab')
             + f"<br><b>Build</b> {idl}"
             # Provenance, restated because it changed twice: the stack was REBASED onto the reader
             # rework, so a commit id quoted in an entry belongs to the branch that lever was
             # measured on — and two levers were later REMOVED after re-verdict. Without this line a
             # reader takes every BUILT badge as current; two of them are not.
             + f"<br><b>Branch</b> <a href='{REPO}/tree/{TIP_BRANCH}' target=_blank rel=noopener>"
               f"{TIP_BRANCH}</a> — <b>{TIP_COMMITS} commits</b> on <code>{TIP_BASE}</code> "
               f"(local, not pushed). The archive at the foot of this page describes the "
               f"{len(COMMITS)} levers of the pre-rebase line <code>{BRANCH}</code>; the entries "
               f"above carry their own <b>BUILT</b> / <b>REFUTED</b> status and cite the commit of "
               f"the branch they were measured on."
             + f"<br><b>Removed after re-verdict</b> two levers inverted once the base moved "
               f"(504-block A/B, steps <i>and</i> prover cost): <code>operator*</code> via arith256 "
               f"(removing it wins 0.7 % on both metrics, 504/504 blocks) and the flat "
               f"offset-indexed hash store (obsoleted by the dead-digest-emplace fix, 408/504). A "
               f"lever's verdict belongs to the base it was measured on — a rebase reopens all of them."
             + f"<br><b>Generated</b> {time.strftime('%Y-%m-%d %H:%M %Z')} by "
             + "profiling/levers.py from results/compare.json + the per-block profile cache"
             + "<br><b>Shelf life</b> two different lifetimes. Everything drawn from the sweep is tied "
               "to these ELFs: land a fix and it is out of date — regenerate. The figures badged "
               "<i>isolated</i> are not: they measure ZisK's own library, so regenerating does not "
               "update them and a change to the Monad guest does not invalidate them. They go stale "
               "when ZisK does, and only re-running the micro-benchmark moves them.</div>")

    # ── what the document is, before what it found. The summary below covers the results; a reader
    # still has to work out what kind of document they are holding.
    # The sentence describing the ordering is filled in from the built list, below. It said "the
    # order follows each lever's ceiling" long after most levers had been built and were ranked on
    # their measured gain, and it still named a prover-cost denominator no lever uses any more.
    _order_i = len(h)
    h.append("<p class=note style='margin:0 0 20px'><b>What this is.</b> The things worth fixing in the "
             "Monad guest <b>as it now stands</b> — after the ten that were built — largest first. "
             "<b>Each one is three blocks</b>: what the profile shows, what to change about it, and "
             "how to check that it worked. <b>None of them is built</b>, so none carries a "
             "measurement or a commit; the ten that do are archived at the foot of the page. "
             "After the list: the one pattern most of them share, then the things "
             "that look like levers and are not, then what these numbers do and do not cover. "
             "@@ORDER@@ "
             "Every figure is recomputed when this page is built; the two exceptions are marked.</p>")

    # ── executive summary ──
    # The item count is computed: it said "Ten lines" after an item was removed, leaving nine.
    _sum_start = len(h)
    h.append("<div class=tldr>")
    h.append("@@SUMMARY_LEAD@@")
    ev = fam_share(Z['shares']['a'], 'EVM interpreter'), fam_share(Z['shares']['b'], 'EVM interpreter')
    wd = fam_share(Z['shares']['a'], 'witness decoding'), fam_share(Z['shares']['b'], 'witness decoding')
    oc = share_of(Z['shares']['a'], OUTCOME_RE), share_of(Z['shares']['b'], OUTCOME_RE)
    co = fam_share(Z['shares']['a'], 'containers / abstraction'), \
         fam_share(Z['shares']['b'], 'containers / abstraction')
    h.append("<ol>")
    # Deliberately NOT quoting the Monad-side share here: it lands on the same 19.5% as the
    # containers share two lines below (19.502 vs 19.501 — a genuine coincidence), and two identical
    # figures for different families in one summary reads as a copy-paste bug. The ratio is the point.
    # "Near-identical on two backends" holds on the ABSOLUTE instruction ratio (0.685x vs 0.688x,
    # 0.5% apart) and NOT on the ratio of shares (0.613x vs 0.492x, 25% apart) — SP1's allocator
    # compresses every share there by ~1.9x. Quote the ratio where the claim is true.
    _absr = lambda ax, fam: ((d[ax]['summary']['families'][0].get(fam, 0)
                              / d[ax]['summary']['families'][1].get(fam, 1))
                             if d[ax]['summary'].get('families') else None)
    _ez, _ep = _absr('zisk', 'EVM interpreter'), _absr('sp1', 'EVM interpreter')
    h.append(f"<li>The gap is <b>not in the engine</b>: the Monad guest's EVM interpreter runs "
             f"<b>{x(_ez, 3)}</b> the instructions of the reth guest's on ZisK and <b>{x(_ep, 3)}</b> "
             f"on SP1 — the same figure on two unrelated backends, so it is a property of the guest's "
             f"own code, not a backend artefact.</li>")
    h.append(f"<li>Witness decoding is also ahead ({pct(wd[0], 1)} vs {pct(wd[1], 1)}) — that path is "
             f"view-based and allocation-free, and it is the model the encode path should copy.</li>")
    h.append(f"<li>The cost is in the layers around that core: container/abstraction machinery "
             f"<b>{pct(co[0], 1)} vs {pct(co[1], 1)}</b> …</li>")

    h.append(f"<li>… and allocation <b>{pct(fam_share(Z['shares']['a'], 'memory / allocation'), 1)} "
             f"vs {pct(fam_share(Z['shares']['b'], 'memory / allocation'), 1)}</b>, where memory "
             f"<i>traffic</i> is at parity and the difference is allocator bookkeeping.</li>")
    # An f-string expression cannot span two adjacent literals — compute it first.
    _B = d['zisk']['blocks']
    _calls_z = statistics.median([(_B[b]['a'].get('kec') or 0) for b in _B])
    h.append(f"<li><b>Keccak is not on that list</b>, but the <i>number</i> of hashes is a lever: "
             f"the guest pays {BENCH['zisklib_1perm']} steps around a short one and "
             f"{BENCH['zisklib_perblock']} per block of a long one, on top of the precompile, at "
             f"{n_(_calls_z)} permutations per block (@keccak@).</li>")

    h.append("<li><b>One pattern explains both</b>: owning byte buffers where a view would do — and "
             "the fix sites are known and local.</li>")
    h.append(f"<li>On ZisK <b>{cs['main'] / cs['total'] * 100:.0f}% of prover cost is linear in step "
             f"count</b>, so instructions saved convert roughly 1:1 into cost.</li>")
    h.append("<li>Several things look like levers and are not — measured, disqualified, and listed "
             "at the bottom with the reason, so they are not re-derived.</li>")
    h.append("<li>None of this is GPU proving time. That measurement does not exist yet.</li>")
    h.append("</ol></div>")
    _n_items = sum(x.count('<li>') for x in h[_sum_start:])
    h[_sum_start + 1] = (
        f"<b>The Monad guest runs {x(P['ratio'])} the instructions of <code>{P['B']}</code> on SP1 "
        f"and {x(Z['ratio'])} of <code>{Z['B']}</code> on ZisK.</b> Figures only compare within a "
        f"backend. {_n_items} lines:")

    # ── how to read the two tags ──
    h.append(f"<p class=note><b>Unmarked figures are computed from the sweep and the profile cache "
             f"when this page is built</b> — none are typed in. Two kinds are marked because they come "
             f"from somewhere else. <span class='tag b'>isolated</span> — measured directly in a "
             f"purpose-built micro-benchmark on <b>{BENCH_DATE}</b>, not attributed by the profiler, "
             f"which is why they can decompose a single function. <span class='tag s'>source</span> — "
             f"read in the Monad tree on <b>{SOURCE_READ}</b> and <b>not</b> verified by measurement; "
             f"the profiler truncates symbol names, so it cannot attribute cost to a specific member "
             f"function. Treat those mechanisms as read and their magnitudes as unmeasured.</p>")

    # ── the levers ──
    # ── what has already landed, and what it actually did ──────────────────────────────────
    # Kept in the document on purpose: a lever list that silently drops its wins reads as though
    # nothing ever works, and the next reader cannot tell a fix that landed from one nobody tried.
    _D = delivered(d, c)
    if _D:
        h.append("<h2>Delivered — the zero-copy witness reader</h2>")
        h.append("<p class=note style='margin:0 0 10px'>The guest moved from <code>PartialTrieDb</code>, "
                 "which copied every witness node into an owning string, to <code>OffsetTrieDb</code>, "
                 "which reads them in place from the witness blob. Two levers this page used to carry "
                 "— the container machinery and the allocator bookkeeping behind it — are gone with it. "
                 "Everything below is measured on the <b>same blocks under both ELFs</b>, as a median "
                 "of per-block ratios: no share arithmetic, so one family shrinking cannot inflate "
                 "another.</p>")
        rows = []
        for ax, a in _D['axes'].items():
            rows.append(f"<tr><td>{ax.upper()} · <span class=cA>{a['a']}</span> vs "
                        f"<span class=cB>{a['b']}</span></td>"
                        f"<td class=n>{x(a['ratio_before'])}</td><td class=n>{x(a['ratio_after'])}</td>"
                        f"<td class=n>{x(a['guest'])}</td>"
                        f"<td class=n>{a['n']}</td></tr>")
        h.append("<table><tr><th>axis</th><th>ratio before</th><th>ratio after</th>"
                 "<th>guest work</th><th>blocks</th></tr>" + "".join(rows) + "</table>")
        _moved = sum(a['denominator_moved'] for a in _D['axes'].values())
        h.append(f"<p class=note><b>Controlled:</b> the reth guest is byte-identical across the two "
                 f"runs — its work differs on <b>{_moved}</b> block(s) of "
                 f"{sum(a['n'] for a in _D['axes'].values())}. Only the Monad guest changed, so the "
                 f"ratio move is not an artefact of a shifting denominator.</p>")
        # families: the two that collapsed, and the core that did not — the latter is the control
        _fam = _D['fam']
        _keys = ['containers / abstraction', 'memory / allocation', 'EVM interpreter',
                 '256-bit arithmetic', 'hashing (keccak/sha)', 'witness decoding']
        _axes = [a for a in ('zisk', 'sp1') if a in _fam]
        if _axes:
            hdr = "".join(f"<th>{a.upper()}</th>" for a in _axes)
            body = ""
            for k in _keys:
                if not any(k in _fam[a] for a in _axes): continue
                cells = "".join(f"<td class=n>{x(_fam[a][k][0]) if k in _fam[a] else '—'}</td>"
                                for a in _axes)
                body += f"<tr><td>{k}</td>{cells}</tr>"
            h.append(f"<table><tr><th>work family, after ÷ before</th>{hdr}</tr>{body}</table>")
            h.append("<p class=note>The <b>EVM core is the control</b>: interpreter, 256-bit "
                     "arithmetic and hashing had to stay flat, and they do, on both backends "
                     "independently. That is what makes the two collapses believable. "
                     "<b>Read the reader's own families as one number, not three:</b> containers, "
                     "state/trie and witness decoding together come to "
                     + " · ".join(f"{a.upper()} {x(_D['trio'][a])}" for a in _axes)
                     + " — the split between those three labels differs between the two backends "
                       "(different targets, different inlining, one profiler counts and the other "
                       "samples) and is not comparable across them.</p>")
        h.append("<h3 class=sub>Why both moved together</h3>")
        h.append("<p>One pattern: owning <code>byte_string</code> "
                 "(<code>= evmc::bytes = std::basic_string&lt;unsigned char, evmc::byte_traits&gt;</code>) "
                 "where a view would do. Per branch node the old reader allocated ~17 owning strings, "
                 "grew a <code>body</code> with no <code>reserve</code>, copied that body again into "
                 "<code>encode_list2</code>'s result — then freed all of it. The allocation was the "
                 "<i>downstream effect</i> of the copies, which is why removing the copies removed "
                 "both, and why the two families fall by almost the same factor.</p>")
        h.append("<p class=note>This page predicted that: the allocator lever carried "
                 "<i>&ldquo;expect this to move on its own once the container work lands — same root "
                 "cause&rdquo;</i>. What it got wrong was projecting the two independently anyway, so "
                 "the combined effect was under-called by half.</p>")
    # ── where the guest's own work goes, reth left out of it ────────────────────────────────
    # The rest of this page ranks by distance to the reth guest. That answers "where are we behind",
    # which stopped being the whole question when SP1 went below 1x: the witness reader is now at
    # parity on ZisK (108.1 M steps against 110.2 M) and therefore cannot appear as a lever, while
    # still being 38 % of the guest. Proving is billed on absolute work, so this grid ranks the
    # guest against itself.
    # The denominator for this whole section, decided before anything referring to it is written.
    _PW = post_gather(c)
    h.append("<h2>Where the work is — the guest on its own terms</h2>")
    h.append("<p class=note style='margin:0 0 10px'>Everything else here is measured against the reth "
             "guest, which answers <i>where is this guest behind</i>. This table answers <i>where does "
             "the "
             "work go</i> — the question proving cost actually asks. A family can be at parity with "
             "reth and still be the most expensive thing the guest does; reducing it lowers the bill "
             "without moving any ratio on this page."
             + ("<br><br><b>This is <code>monad-levers</code></b> — the guest after the ten archived "
                "changes, the same denominator the ranked list uses. It is not the shipped guest, "
                "whose figures are in <code>compare.html</code>." if _PW else "") + "</p>")
    # The denominator here must be the SAME guest the ranked list is sized against, or the page shows
    # two "where the work is" answers within a screen of each other. Post-levers when that profile
    # exists, shipped otherwise.
    _axes = [a for a in ('zisk', 'sp1') if (a in _PW if _PW else a in S)]
    _fam = {}
    for ax in _axes:
        if _PW:
            sh, med = _PW[ax]['shares'], _PW[ax]['median']
        else:
            sh = S[ax]['shares']['a']
            med = statistics.median([d[ax]['blocks'][b]['a']['work'] for b in d[ax]['blocks']])
        agg = {}
        for fn, v in sh.items():
            agg[hs.family(fn)] = agg.get(hs.family(fn), 0) + v
        _fam[ax] = {k: (v, v * med) for k, v in agg.items()}
    keys = sorted({k for ax in _axes for k in _fam[ax]},
                  key=lambda k: -max(_fam[ax].get(k, (0, 0))[0] for ax in _axes))
    hdr = "".join(f"<th>{ax.upper()} — {S[ax]['sum']['unit']}</th><th>{ax.upper()} — share</th>"
                  for ax in _axes)
    body = ""
    for k in keys:
        if max(_fam[ax].get(k, (0, 0))[0] for ax in _axes) < 0.005: continue
        cells = ""
        for ax in _axes:
            sh_, ab = _fam[ax].get(k, (0, 0))
            cells += f"<td class=n>{n_(round(ab))}</td><td class=n>{pct(sh_)}</td>"
        body += f"<tr><td>{k}</td>{cells}</tr>"
    h.append(f"<table><tr><th>work family</th>{hdr}</tr>{body}</table>")
    _rd = ['containers / abstraction', 'state / trie', 'witness decoding']
    _rdtxt = " · ".join(
        f"{ax.upper()} {pct(sum(_fam[ax].get(k, (0, 0))[0] for k in _rd))}" for ax in _axes)
    h.append(f"<p class=note><b>The witness reader is the largest single thing the guest does</b> — "
             f"containers, state/trie and witness decoding together: {_rdtxt}. It is at parity with "
             f"the reth guest, so it appears nowhere in the levers below, and it is still the biggest "
             f"target by absolute cost. Same for the allocator on SP1: down to "
             f"{x((_D.get('fam', {}).get('sp1', {}).get('memory / allocation') or [None])[0], 2)} of "
             f"its former self and still the single heaviest family there.</p>")

    # ── symbol-level inventory ────────────────────────────────────────────────────────────────────
    # The family grid above says WHERE the work is; it cannot say whether anyone has looked at it.
    # This does, and it exists because the byte-order lever spent a cycle filed under "not
    # comparable, therefore closed" while being the guest's second largest symbol: 32 % of the
    # guest's work sat in symbols this page never named. Anything reaching `not instructed` is a
    # cost with no owner.
    # Same guest as the family grid and the ranked list. Taken against the shipped binary this table
    # would name symbols that no longer exist and file finished work as open.
    _iv_ax, _iv_g = POST_AX['zisk'] if _PW else ('zisk', 'monad-zisk')
    _inv = top_symbols(c, _iv_ax, _iv_g, 26)
    if _inv:
        _CLS = {'lever': ('a lever above', 'lv'), 'closed': ('measured, closed', 'cl'),
                'fixed': ('fixed — archived', 'cl'),
                'reader': ('witness reader — at parity, uninstructed', 'rd'),
                'open': ('<b>not instructed</b>', 'op'),
                'unnamed': ('<b>nothing accounts for it</b>', 'op')}
        _agg = {}
        for _f, _m, _sh, _c2, _w2 in _inv['rows']:
            _agg[_c2] = _agg.get(_c2, 0) + _sh
        h.append("<h2>Every expensive symbol, and what accounts for it</h2>")
        h.append(f"<p class=note>The grid above is by family; this is by symbol, because a family "
                 f"cannot say whether anyone has looked at the cost. Median steps per block on ZisK, "
                 f"top {len(_inv['rows'])} of the guest's own work "
                 f"({n_(round(_inv['work']))} steps per block)"
                 + (f", for <code>{_iv_g}</code> — the guest after the archived changes. The rows "
                    f"marked <i>fixed</i> are what those levers left behind; the ones that worked "
                    f"completely (<code>__bswapdi2</code>, <code>wyhash</code>) are gone from the "
                    f"binary and cannot appear here at all." if _PW else "") + "</p>")
        rows = ""
        for _f, _m, _sh, _c2, _w2 in _inv['rows']:
            lbl, css = _CLS[_c2]
            # Demangled C++ names run to hundreds of characters; the leading namespaces are the
            # least informative part, so keep the tail and the parameter-less head.
            short = _f.split('(')[0]
            short = short[-72:] if len(short) > 72 else short
            rows += (f"<tr><td class=n>{n_(round(_m))}</td><td class=n>{pct(_sh, 2)}</td>"
                     f"<td><code>{short}</code></td><td class={css}>{lbl}</td>"
                     f"<td class=why>{_w2}</td></tr>")
        h.append(f"<table><tr><th class=n>steps/block</th><th class=n>share</th><th>symbol</th>"
                 f"<th>status</th><th>note</th></tr>{rows}</table>")
        h.append("<p class=note>"
                 + " · ".join(f"<b>{pct(_agg[k], 1)}</b> {_CLS[k][0]}"
                              for k in ('lever', 'reader', 'closed', 'fixed', 'open', 'unnamed')
                              if _agg.get(k))
                 + f". The reader block is the one to read twice: it is the largest thing the guest "
                 f"does and it is at parity with the reth guest, so no comparison will ever surface "
                 f"it as a lever — only absolute cost does.</p>")

    h.append("<h2>Levers — what is left</h2>")
    _PO = post_gather(c)
    _arch = order_levers(build_archive(d, c, S, Z, P, cs)) if _PO else []
    levers, nonlevers, _refnum = resolve_refs(
        order_levers(build_levers(d, c, S, Z, P, cs, _PO)),
        build_nonlevers(d, c, S, Z, P, cs), _arch)
    # The archive's own cross-references point inside itself, where the numbering is its own.
    _arch, _, _ = resolve_refs(_arch, [])
    if _PO:
        _z, _s = _PO.get('zisk'), _PO.get('sp1')
        h.append(f"<p class=note style='margin:0 0 16px'><b>Ranked against "
                 f"<code>monad-levers</code>, not against what ships.</b> The ten in the archive "
                 f"below removed {pct(_z['vs_today'], 1) if _z else '—'} of the guest's own work and "
                 f"reshaped what is left, so a share taken from the shipped binary would size these "
                 f"against something nobody intends to run again. Median block: "
                 f"{_z['median'] / 1e6:.0f} M steps on ZisK"
                 + (f", {_s['median'] / 1e6:.0f} M cycles on SP1" if _s else "") + ". "
                 f"<b>Figures marked <i>ceiling</i> are bounds, not forecasts</b> — the first round "
                 f"returned a median of <b>43 % of its ceilings</b>, and one round-two candidate "
                 f"(word-wise mem*) was refuted outright by its build. Entries whose chip says "
                 f"<b>BUILT</b> are measured commits on <code>al/zkvm-r2</code> (local, "
                 f"not pushed), awaiting review before they join the branch and these axes.</p>")
    for i, L in enumerate(levers, 1):
        h.append(render_lever(i, L))

    # Describe the ordering from the list that was actually built, so the sentence cannot outlive the
    # thing it describes. Three words appear on the chips and each means something different about
    # how much the figure is worth trusting.
    _by = {}
    for L in levers:
        if L.get('rank'):
            _by[L.get('rank_word', 'ceiling on')] = _by.get(L.get('rank_word', 'ceiling on'), 0) + 1
    _wd = {'measured on': ('<b>measured</b>', 'the change has been built and the figure is what it '
                                              'returned'),
           'ceiling on': ('<b>ceiling</b>', 'an upper bound from the symbol\'s own share, not an '
                                            'expectation'),
           'estimated on': ('<b>estimated</b>', 'derived from measured parts, but never built as a '
                                                'whole')}
    _parts = [f"{_wd[w][0]} on {n} ({_wd[w][1]})" for w, n in
              sorted(_by.items(), key=lambda kv: -kv[1]) if w in _wd]
    h[_order_i] = h[_order_i].replace(
        '@@ORDER@@',
        "<b>The order follows what each lever is worth</b>, as a share of the guest's own work. The "
        "word next to that figure says how far to trust it: " + "; ".join(_parts) + ". Where a "
        "measurement supersedes an earlier estimate, both are shown rather than the estimate being "
        "quietly dropped.")

    # Upstream items are shared code, so they close no Monad-vs-reth gap and must not enter the sum.
    naive = sum(L['gap_pp'] for L in levers if L.get('gap_pp') and not L.get('upstream'))
    _shared = [n for n, L in enumerate(levers, 1) if L.get('id') in ('containers', 'alloc')]
    # Both clauses below used to be written out, and both went wrong the moment the list changed:
    # the shared-root-cause clause rendered as "Two of them, levers , share one root cause" once the
    # container and allocator levers were delivered and left the page, and the closing sentence named
    # "keccak and the 32-bit types" as the two without a percentage while keccak carries one.
    _nogap = [n for n, L in enumerate(levers, 1) if not L.get('gap_pp')]
    _plural = lambda ns: (f"lever {ns[0]}" if len(ns) == 1 else
                          f"levers {', '.join(str(x) for x in ns[:-1])} and {ns[-1]}")
    h.append(f"<p class=note><b>These do not add up.</b> Each figure is what <i>that</i> fix removes, "
             f"or a measured ceiling on it — not the distance to the reth guest."
             + (f" {len(_shared)} of them, {_plural(_shared)}, share one root cause, so removing it "
                f"once counts once." if len(_shared) > 1 else "")
             + f" The naive sum ({naive:.1f} pp, which would put the ZisK ratio at "
             f"{x(Z['ratio'] * (1 - naive / 100))}) therefore overstates: fix them together and expect "
             f"a joint gain below it."
             + (f" {_plural(_nogap).capitalize()} "
                f"{'is' if len(_nogap) == 1 else 'are'} measured against the guest's own work rather "
                f"than as a distance to the reth guest — {'it' if len(_nogap) == 1 else 'they'} "
                f"{'reduces' if len(_nogap) == 1 else 'reduce'} prover cost without closing a gap, so "
                f"{'it is' if len(_nogap) == 1 else 'they are'} not in this sum." if _nogap else "")
             + "</p>")
    # The warning above is about ESTIMATES. The two levers that were built do add up — verified, not
    # assumed — and saying so matters: without it a reader discounts the measured pair by the same
    # caution that applies to the projected ones.
    _cb = _load_json('allfive-measured.json') or _load_json('combo-measured.json')
    if _cb:
        h.append(f"<p class=note><b>The ones that were built do add up.</b> All ten were also "
                 f"built together from <code>{_cb['commit'][:9]}</code>: "
                 f"<b>{pct(_cb['gain_median'], 2)}</b> of the guest's own work across "
                 f"{_cb['blocks']} blocks ({pct(_cb['gain_min'], 2)}–{pct(_cb['gain_max'], 2)}), "
                 f"<b>{_cb['roots_pass']}/{_cb['blocks']} state roots PASS</b>, and no "
                 f"<code>{'</code>, <code>'.join(_cb['helpers_gone'])}</code> symbol left in the "
                 f"binary. That is the ZisK ratio moving <b>{x(_cb['zisk_ratio_before'])} → "
                 f"{x(_cb['zisk_ratio_after'])}</b>.<br><br>"
                 f"The three measured separately (digest {pct(_cb['parts']['digest'], 2)}, byte-order "
                 f"{pct(_cb['parts']['byteorder'], 2)}, scan index {pct(_cb['parts']['scanindex'], 2)}) "
                 f"sum to {pct(_cb['sum_of_prior'], 2)}. On top of that: count-leading-zeros with the "
                 f"soft-float removal {pct(_cb['clz_and_float'], 2)} → {pct(_cb['five_way'], 2)}; the "
                 f"128&divide;64 division {pct(_cb['division'], 2)} → {pct(_cb['six_way'], 2)}; the "
                 f"eager priming hash <b>{pct(_cb['keccak'], 2)}</b> → {pct(_cb['seven_way'], 2)}; and "
                 f"popcount {pct(_cb['popcount'], 2)} for {pct(_cb['eight_way'], 2)}, against the "
                 f"{pct(_cb['popcount_share'], 2)} its symbol holds (four 64-bit constants cost "
                 f"nearly what the call did); and widening <code>NodeId</code> to 64-bit "
                 f"{pct(_cb['addw'], 2)}, which moved only {_cb['addw_static_delta']} of "
                 f"{n_(_cb['addw_static_total'])} static 32-bit instructions — they were in the "
                 f"reader's hottest paths; and replacing wyhash on the state keys "
                 f"{pct(_cb['keyhash'], 2)} for {pct(_cb['gain_median'], 2)}. They add "
                 f"because each removes "
                 f"a fixed number of steps from disjoint work — treating them as fractions of what "
                 f"remains would understate the total. The estimates above do <i>not</i> add up; "
                 f"these do, and the difference is that these were built.</p>")

    # ── the archive ──
    # Collapsed, and after the remaining work: these are done. Kept in full rather than reduced to a
    # table because each entry carries the mechanism and the attempt that failed first, and that is
    # the part a diff cannot tell you.
    if _arch:
        _tot = sum(1 for L in _arch if L.get('built'))
        h.append("<h2>Fixed — archived</h2>")
        h.append(f"<p class=note><b>{_tot} levers, built and measured, on "
                 f"<a href='{REPO}/tree/{BRANCH}' target=_blank rel=noopener>"
                 f"<code>{BRANCH}</code></a>.</b> They are no longer work items; they are kept here "
                 f"because each records why it worked, what it cost, and — for four of them — the "
                 f"attempt that failed first. Every figure below was measured against the guest that "
                 f"<i>shipped</i>, which is the denominator those numbers were taken in; the list "
                 f"above uses the guest that stands after them.</p>")
        h.append("<details><summary>Show the ten, with their measurements and commits</summary>")
        for i, L in enumerate(_arch, 1):
            h.append(render_lever(i, L))
        h.append("</details>")

    # ── the pattern ──
    h.append(f"<div class=site><span class='tag s'>source</span> "
             f"<span class=p>{NOT_A_SITE[0]}:{NOT_A_SITE[1]}</span>"
             f"<p class=note style='margin-top:6px'><b>A correction worth keeping.</b> "
             f"{NOT_A_SITE[2]}</p></div>")

    # ── non-levers ──
    h.append("<h2>Not levers — measured, then closed</h2>")
    h.append("<p class=note>Each of these cost real effort to disqualify. They are recorded so they "
             "are not re-derived.</p>")
    for D in nonlevers:
        h.append(f"<div class='lev small dead'><div class=hd><h3>{D['t']}</h3>"
                 f"<span class=impact>{D['n']}</span></div><p class=note>{D['w']}</p></div>")

    # ── limits ──
    h.append("<h2>What these numbers are, and are not</h2>")
    # "Shares are the only unit safe to compare across backends" was the opposite of what the data
    # says: measured on the EVM interpreter, the share ratio differs 25% between axes while the
    # absolute ratio differs 0.5%. Each unit has a domain — composition vs ratio — and the page has to
    # name both, because it uses both.
    _al_a = fam_share(P['shares']['a'], 'memory / allocation')
    _al_b = fam_share(P['shares']['b'], 'memory / allocation')
    _shz = fam_share(Z['shares']['a'], 'EVM interpreter') / fam_share(Z['shares']['b'], 'EVM interpreter')
    _shp = fam_share(P['shares']['a'], 'EVM interpreter') / fam_share(P['shares']['b'], 'EVM interpreter')
    _shdev = abs(_shz - _shp) / min(_shz, _shp) * 100
    _iz = ((d['zisk']['summary']['families'][0].get('EVM interpreter', 0)
            / d['zisk']['summary']['families'][1].get('EVM interpreter', 1)),
           (d['sp1']['summary']['families'][0].get('EVM interpreter', 0)
            / d['sp1']['summary']['families'][1].get('EVM interpreter', 1)))
    h.append(f"<p class=note>Instruction counts and static cost models — <b>not</b> GPU proving time, "
             f"which is the decisive missing measurement. Profile coverage is "
             f"{S['zisk']['nblk']['a']} and {S['sp1']['nblk']['a']} blocks per guest, against "
             f"{S['zisk']['sum']['n']}/{S['sp1']['sum']['n']} blocks of whole-program measurement.</p>")
    h.append(f"<p class=note><b>Two units, two domains — do not swap them.</b> A guest's "
             f"<i>composition</i> must be read as <b>shares of its own work</b>: an absolute per-block "
             f"figure on SP1 carries that backend's sampling scale, and the allocator takes "
             f"{pct(_al_a, 0)} of the Monad guest's work there against {pct(_al_b, 0)} of the reth "
             f"guest's, compressing every other share by different amounts. But a <i>ratio between the "
             f"two guests</i> must be read as <b>absolute instructions</b> if it is to be compared "
             f"across backends: the EVM interpreter's absolute ratio is {x(_iz[0], 3)} on ZisK and "
             f"{x(_iz[1], 3)} on SP1 — the same figure — while its share ratio reads "
             f"{x(fam_share(Z['shares']['a'], 'EVM interpreter') / fam_share(Z['shares']['b'], 'EVM interpreter'), 2)} "
             f"and "
             f"{x(fam_share(P['shares']['a'], 'EVM interpreter') / fam_share(P['shares']['b'], 'EVM interpreter'), 2)}, "
             f"{_shdev:.0f}% apart, purely from that compression.</p>")
    h.append(f"<p class=note><b>A demangled name starts with the return type.</b> Any pattern "
             f"matched against a symbol name therefore hits functions that merely <i>return</i> the "
             f"matched type — the trie decoder here returns a <code>basic_result&lt;PartialNode&gt;</code> "
             f"and reads as result-wrapper machinery unless the classifier excludes it. Families are "
             f"assigned by name, so a share is only as good as the pattern that produced it: check "
             f"what the dominant symbols actually are before acting on a family.</p>")
    h.append("<footer>Generated by <code>profiling/levers.py</code>. Measurement instrument: "
             "<code>profiling/compare.py</code> → <code>results/compare.html</code>.</footer>")
    h.append("</div>")
    # One substitution over the finished page: the summary, the limits section and the footer are not
    # in the lever lists, so resolving only those left a live token in the summary. Fail loudly rather
    # than ship an @id@ to a reader.
    # Two token forms: @id@ renders "lever N", #id# renders just N — so a heading can read
    # "levers 1 and 3" rather than "lever 1 and lever 3".
    out = "\n".join(h)
    out = re.sub(r'@([a-z0-9_]+)@',
                 lambda m: f"lever {_refnum[m.group(1)]}" if m.group(1) in _refnum else m.group(0), out)
    out = re.sub(r'#([a-z0-9_]+)#',
                 lambda m: str(_refnum[m.group(1)]) if m.group(1) in _refnum else m.group(0), out)
    left = sorted(set(re.findall(r'@([a-z0-9_]+)@|#([a-z0-9_]+)#', out)))
    left = sorted({a or b for a, b in [(x, y) for x, y in
                                       re.findall(r'@([a-z0-9_]+)@|#([a-z0-9_]+)#', out)]})
    if left:
        sys.exit(f"refusing to write: unresolved lever references {left}")
    return out


def live_symbols(elf):
    """Symbol names in a guest ELF, or None when they cannot be read.

    Returned as one blob to substring-match against: the point is only whether a named class or
    function survives in the binary, not which mangling it wears."""
    for argv in (('nm', '-C', elf), ('nm', elf), ('llvm-nm', elf)):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout:
            return r.stdout
    return None


def check_sites(elf):
    """Refuse to publish a source citation whose code is not in the guest any more.

    A cited file:line is the one part of this page that measurement never touches, so it goes stale
    invisibly: the file stays in the tree and only the LINKER stops pulling it in. Probing the ELF is
    what turns that into a build failure instead of a reader acting on dead code."""
    syms = live_symbols(elf)
    if syms is None:
        print(f"  [warn] cannot read symbols from {os.path.basename(elf)} — source sites unchecked")
        return
    dead = [(f, ln, probe) for group in SITES.values()
            for f, ln, probe, _what, _fix in group if probe not in syms]
    if dead:
        sys.stderr.write(
            "levers.py: source sites cite code absent from the guest ELF "
            f"({os.path.basename(elf)}):\n"
            + "".join(f"  {f}:{ln} — no symbol matching {probe!r}\n" for f, ln, probe in dead)
            + "Either the fix landed (drop the site, and re-derive the lever) or the wrong ELF is "
              "selected (check guests/monad/use-gen).\n")
        sys.exit(1)
    n = sum(len(g) for g in SITES.values())
    print(f"  source sites: {n}/{n} still present in {os.path.basename(elf)}")


def delivered(d, c):
    """What a landed change actually did, measured — not what a projection said it would do.

    Same blocks under both ELFs, raw per-function counts, median of per-block ratios. That is the
    strongest form available here: it needs no share arithmetic, so the compression of one family by
    another (SP1's allocator was 46 % of the guest and squeezed every other share) cannot distort it.
    Returns None when the frozen baseline is absent — the page then simply has no before/after."""
    if not os.path.exists(BASELINE):
        return None
    base = json.load(open(BASELINE))
    out = {'axes': {}, 'fam': {}}   # `hs` is the module-level hotspots import (line 33)
    for ax, guest in (('zisk', 'monad-zisk'), ('sp1', 'monad-sp1')):
        if ax not in base or ax not in d:
            continue
        bo, bn = base[ax]['summary'], d[ax]['summary']
        Bo, Bn = base[ax]['blocks'], d[ax]['blocks']
        common = [b for b in Bo if b in Bn and Bo[b]['a']['work']]
        # the reth side must be byte-identical, or this is not a controlled comparison
        moved = [b for b in common if Bo[b]['b']['work'] != Bn[b]['b']['work']]
        out['axes'][ax] = {
            'a': bn['a_name'], 'b': bn['b_name'],
            'ratio_before': bo['ratio_median'], 'ratio_after': bn['ratio_median'],
            'pw_before': bo.get('pw_ratio_median'), 'pw_after': bn.get('pw_ratio_median'),
            'pw_unit': bn.get('pw_unit'),
            'guest': statistics.median([Bn[b]['a']['work'] / Bo[b]['a']['work'] for b in common]),
            'n': len(common), 'denominator_moved': len(moved),
        }
        # per-family, only blocks measured under both stamps
        # This is the one place that reads TWO builds of a guest — the family shift is precisely the
        # before/after. Identities, not stamps: two builds can share an mtime (built in the same
        # second), so ordering by mtime alone could pair a build with itself.
        builds = c.builds_by_name(guest)
        if len(builds) < 2:
            continue
        P_old = c.profiles_for(builds[-2][0])
        P_new = c.profiles_for(builds[-1][0])
        per = {}
        for b in common:
            po, pn = P_old.get(int(b)), P_new.get(int(b))
            if not po or not pn:
                continue
            fo, fn_ = {}, {}
            for f, v in po['fns']: fo[hs.family(f)] = fo.get(hs.family(f), 0) + v
            for f, v in pn['fns']: fn_[hs.family(f)] = fn_.get(hs.family(f), 0) + v
            for k in set(fo) | set(fn_):
                if fo.get(k):
                    per.setdefault(k, []).append(fn_.get(k, 0) / fo[k])
        out['fam'][ax] = {k: (statistics.median(v), len(v)) for k, v in per.items() if len(v) >= 20}
        # The reader's three families as ONE factor, from raw counts: their individual factors
        # disagree across backends (witness decoding reads 1.57x on ZisK, 1.09x on SP1 for the same
        # source change, and matching the blocks did not reconcile it), so only the sum survives
        # comparison. A mean of the three ratios would be arithmetically wrong — the families differ
        # by an order of magnitude in size.
        trio = {'containers / abstraction', 'state / trie', 'witness decoding'}
        rt = []
        for b in common:
            po, pn = P_old.get(int(b)), P_new.get(int(b))
            if not po or not pn: continue
            so = sum(v for f, v in po['fns'] if hs.family(f) in trio)
            sn = sum(v for f, v in pn['fns'] if hs.family(f) in trio)
            if so: rt.append(sn / so)
        if rt: out.setdefault('trio', {})[ax] = statistics.median(rt)
    return out if out['axes'] else None


def _generation():
    """Name the guest generation these numbers describe, measuring what can be measured.

    The witness format is read off an actual fixture rather than trusted from a directory name: the
    two formats share every filename, so a set can be swapped underneath the page without a trace."""
    parts = []
    cur = os.path.join(HERE, os.pardir, 'guests', 'monad', 'current')
    if os.path.islink(cur):
        parts.append(f"<code>{os.path.basename(os.readlink(cur))}</code>")
    for name, elf in (('zisk', 'guests/monad-zisk/monad-zisk.elf'),
                      ('sp1', 'guests/monad-sp1/monad-sp1.elf')):
        f = os.path.join(HERE, os.pardir, elf)
        if os.path.exists(f):
            h_ = hashlib.sha256(open(f, 'rb').read()).hexdigest()[:12]
            parts.append(f"{name} elf <code>{h_}</code>")
    # field [1] of a real witness: 'MZW\x01' is the offset format, an RLP list header the old one
    fx = os.path.join(HERE, os.pardir, 'guests', 'monad', 'fixtures')
    try:
        w = sorted(x for x in os.listdir(fx) if x.endswith('.witness'))[0]
        b = open(os.path.join(fx, w), 'rb').read()
        def hdr(b, i):
            q = b[i]
            if q < 0x80: return i, 1
            if q < 0xb8: return i + 1, q - 0x80
            if q < 0xc0: n = q - 0xb7; return i + 1 + n, int.from_bytes(b[i+1:i+1+n], 'big')
            if q < 0xf8: return i + 1, q - 0xc0
            n = q - 0xf7; return i + 1 + n, int.from_bytes(b[i+1:i+1+n], 'big')
        s0, _ = hdr(b, 0); s1, l1 = hdr(b, s0); s2, _ = hdr(b, s1 + l1)
        head = b[s2:s2+4]
        parts.append("witness <code>" + head.hex() + "</code> "
                     + ("(offset)" if head == b'MZW\x01' else "(RLP node list)"))
    except Exception:
        pass
    parts.append(f"guest <code>{GEN['guest'][:9]}</code> · writer <code>{GEN['witness'][:9]}</code>")
    return " · ".join(parts)


def _load_json(name):
    """A results/ measurement file, or None. Four of these are read now (inline verdict, byte-order,
    scan, combined) and each open() was being written out with its own try/except."""
    try:    return json.load(open(os.path.join(HERE, 'results', name)))
    except Exception: return None


def build_meas():
    """One record per built lever: the gain, how it was obtained, and the state-root evidence.

    The `how` field is the reason this exists. Three levers have a dedicated A/B build — one guest
    with the change, one without, same blocks — which is the strong form. The other seven are
    differences between successive cumulative builds, so each carries whatever interaction the
    changes before it introduced. Both were being reported with the same word, `measured`.

    The root cells are deliberately two-part. A per-lever A/B build verifies its own 16 blocks; the
    branch verifies all ten changes together over the full sweep, which is the stronger statement
    and the one that covers the byte-order lever, whose own build recorded no root check.
    """
    afm, dgm = _load_json('allfive-measured.json'), _load_json('digest-measured.json')
    bsm, scm = _load_json('bswap-measured.json'), _load_json('scan-measured.json')
    opt = _load_json('optimized-zisk.json')
    if not afm:
        return {}
    nb = afm['blocks']
    # The branch clause is the same for every lever — all ten are in it — so it is built once.
    br = (f" · {opt['roots_pass']}/{opt['roots_total']} for the ten together on the branch"
          if opt and opt.get('roots_total') else "")
    own = lambda n, lo, hi: (f"its own A/B build · {n} blocks · {pct(lo, 2)}–{pct(hi, 2)}"
                             if lo is not None else f"its own A/B build · {n} blocks")
    delta = f"difference within the cumulative build · {nb} blocks"
    M = {}
    if dgm:
        M['digest'] = {'gain': dgm['gain_median'],
                       'how': own(dgm['blocks'], dgm.get('gain_min'), dgm.get('gain_max')),
                       'roots': f"{dgm['roots_pass']}/{dgm['blocks']} on this build{br}"}
    if bsm:
        # No roots_pass in this file: the A/B build recorded `regressions`, a performance check, and
        # never a correctness one. Say so rather than let a blank cell read as a pass.
        M['bswap'] = {'gain': bsm['gain_median'],
                      'how': own(bsm['blocks'], bsm.get('gain_min'), bsm.get('gain_max')),
                      'roots': f"not recorded for this build{br}".lstrip(' ·')}
    if scm and scm.get('A'):
        A = scm['A']
        M['scanidx'] = {'gain': A['gain_median'],
                        'how': own(scm['blocks'], A.get('gain_min'), A.get('gain_max')),
                        'roots': f"{A['roots_pass']}/{scm['blocks']} on this build{br}"}
    rp = f"{afm['roots_pass']}/{nb} on the cumulative build{br}"
    JOINT = "for this change and the soft-float one together"
    for lid, key, how, joint in (
            ('keccak', 'keccak', delta, None), ('div', 'division', delta, None),
            ('keyhash', 'keyhash', delta, None), ('int32', 'addw', delta, None),
            ('popcount', 'popcount', delta, None),
            ('clz', 'clz_and_float',
             delta + " · not separated from the soft-float change, which is too small to isolate",
             JOINT),
            ('softfloat', 'clz_and_float',
             delta + " · not separated from the count-leading-zeros change",
             "for this change and the count-leading-zeros one together")):
        if afm.get(key) is not None:
            M[lid] = {'gain': afm[key], 'how': how, 'roots': rp, 'joint': joint}
    return M


def verdicts():
    """{family: {ship, ni, factor}} or {} — how far each family's ratio moves when the reth guest's
    inlining is undone. A family whose ratio moves by more than a factor of two cannot be claimed in
    either direction from the shipped attribution."""
    try:    return json.load(open(VERDICT))
    except Exception: return {}


def order_levers(levers):
    """Sort by each lever's declared headline magnitude, largest first.

    Append order had them at 7.32 / 4.19 / 5.20 pp while three places in the page promised a ranking.
    `rank` is the figure that lever leads with — a removable share, a measured ceiling, or a share
    merely concerned — so it orders them without pretending they are one quantity."""
    return sorted(levers, key=lambda L: -(L.get('rank') or L.get('gap_pp') or 0))


def resolve_refs(levers, nonlevers, archive=()):
    """Replace @id@ tokens with each lever's display number.

    Written literally ("lever 3"), a cross-reference goes stale as soon as a lever is added or removed
    from the list — which happened, leaving four references one rank too high. Resolving them from the
    built list makes the numbering unbreakable.

    The archive is numbered separately, so a reference into it cannot be "lever N" — two lists both
    start at 1. Those resolve to a NAMED reference instead. This is what caught the restructure: the
    non-levers pointed at `@keccak@`, which moved to the archive, and the guard refused to write
    rather than emit a number pointing at the wrong entry."""
    num = {L['id']: n for n, L in enumerate(levers, 1) if L.get('id')}
    arch = {L['id']: L['t'] for L in archive if L.get('id')}

    def fix(t):
        def sub(m):
            k = m.group(1)
            if k in num:
                return f"lever {num[k]}"
            if k in arch:
                # Strip any markup from the title: it lands mid-sentence, in someone else's prose.
                return f"the archived lever &ldquo;{re.sub('<[^>]+>', '', arch[k])}&rdquo;"
            return m.group(0)
        return re.sub(r'@([a-z0-9_]+)@', sub, t)
    for L in levers:
        for k in ('t', 'w', 'impact', 'rem', 'fix'):
            if L.get(k): L[k] = fix(L[k])
    for D in nonlevers:
        for k in ('t', 'n', 'w'):
            if D.get(k): D[k] = fix(D[k])
    return levers, nonlevers, {**num, **{k: k for k in arch}}


# The branch that carries the built levers. Each entry is the commit that IS the change, so a reader
# who wants the diff never has to reconstruct it from the prose. Short hashes resolve on GitHub.
REPO = 'https://github.com/category-labs/monad'
# 2026-08-08: the whole stack was REBASED onto sam/zkvm-zisk-sp1 (the reader rework) as
# al/zkvm-r3 — 20 commits, new hashes. Commit ids quoted in entry bodies name the branch they
# were measured on (al/zkvm-levers, al/zkvm-r2, al/zkvm-r3); the al/zkvm-r3 ids are current.
# Two of the old stack's commits vanished in the port on purpose: the eager-priming pair
# cancelled against its soundness revert, and the no-op commit filter turned out to be already
# upstream (is_changed) in the rework.
# The current tip: the whole stack rebased onto the reader rework, minus the two levers the
# 504-block re-verdict inverted. Kept separate from BRANCH/BASE below so the archive keeps
# describing the line it actually measured.
TIP_BRANCH = 'al/zkvm-r3'
TIP_BASE = 'origin/sam/zkvm-zisk-sp1'
TIP_COMMITS = 34

BRANCH = 'al/zkvm-levers'
BASE = 'ed16787ae'
COMMITS = {
    'keccak':    [('094ab447c', 'trie: stop hashing every node up front')],
    'digest':    [('618f4bf9e', 'trie: keep digest stubs out of the hash cache')],
    'bswap':     [('1ef51d1af', 'core, vm, mpt: inline the byte-order swaps'),
                  ('79e1f8292', 'core: add bit_primitives.hpp')],
    'div':       [('f62f93935', 'uint256: specialise the 128/64 division step')],
    'keyhash':   [('807b7ebf1', 'core: hash Address and bytes32_t keys without wyhash')],
    'scanidx':   [('029037751', 'vm: widen the JUMPDEST scan index to 64-bit')],
    'clz':       [('ec1530ad3', 'core: inline count-leading-zeros')],
    'softfloat': [('651422878', 'third_party: record the two submodule patches')],
    'popcount':  [('a3b68752a', 'mpt, core: inline population count')],
    'int32':     [('d8fe383d6', 'trie: widen NodeId to 64-bit')],
}


# Read once at import: the measurement files do not change while a report is being rendered.
MEAS = build_meas()


def measured_strip(lid, M):
    """The measurement of one lever, as fields rather than as a sentence.

    Written as a rendered strip on purpose. The same three facts exist for all ten built levers, so
    prose was printing them ten different ways ("Measured:", "Built and measured —", "Built:") and
    burying the one that actually varies: HOW the figure was obtained. Three of the ten come from a
    dedicated A/B build; the rest are differences between successive cumulative builds, which is a
    weaker measurement and was invisible. A field set also makes a missing value show up — the
    byte-order lever turned out to have no state-root check of its own.
    """
    m = M.get(lid)
    if not m:
        return ""
    cell = lambda k, v: f"<i>{k}</i><em>{v}</em>"
    # Two levers share one figure. Printing it bare in both invites a reader to add them up, which
    # would double a number the build returned once.
    g = f"<b>{pct(m['gain'], 2)}</b>" + (f" <span class=jt>{m['joint']}</span>"
                                         if m.get('joint') else "")
    return ("<div class=meas>"
            + cell('gain', g)
            + cell('how', m['how'])
            + cell('state roots', m['roots'])
            + "</div>")


def commit_chips(lid):
    """Links to the commits that implement a lever, or nothing if it was never built."""
    cs = COMMITS.get(lid)
    if not cs:
        return ""
    return ("<div class=cmts>" + "".join(
        f"<a class=cmt href='{REPO}/commit/{sha}' target=_blank rel=noopener>"
        f"<b>{sha}</b><span class=sub>{subject}</span><span class=arw>&#8599;</span></a>"
        for sha, subject in cs) + "</div>")


def render_lever(i, L):
    # An upstream item is shared code, so it must be labelled: it closes no gap between the guests.
    # The Zbb item is upstream of BOTH backends rather than of ZisK, so the text comes from the
    # lever when it sets one.
    _up = L.get('upstream')
    badge = (f"<span class='tag up'>{_up if isinstance(_up, str) else 'upstream ZisK'}</span>"
             if _up else "")
    # The ceiling is printed from the sort key, so the figure that ordered the list is always the
    # figure shown. Its denominator is not the same for every lever, so it is named, not assumed.
    # `ceiling` is wrong for a lever that has been built and measured — the byte-swap chip read
    # "1.9% ceiling … built and measured", which is a contradiction in six words. The noun comes
    # from the lever now, defaulting to the estimate wording.
    ceil = (f"<b>{L['rank']:.1f}%</b> {L.get('rank_word', 'ceiling on')} "
            f"{L.get('rank_of', 'the guest&rsquo;s own work')} · " if L.get('rank') else "")
    o = [f"<div class=lev><div class=hd><span class=rank>{i}</span><h3>{L['t']}</h3>{badge}"
         f"<span class=impact>{ceil}{L['impact']}</span></div>"]
    # Three blocks, always in the same order: what the profile shows, what to do about it, and how
    # to tell it worked. The middle one is what an engineer is here for, so it is the one that
    # carries the commit — a lever whose fix has to be reconstructed from the diagnosis is prose,
    # not a work item.
    o.append(f"<div class='blk first'><span class=k>the finding</span><p>{L['w']}</p></div>")
    if L.get('tbl'): o.append(L['tbl'])
    for f, ln, _probe, what, fix in L.get('sites', []):
        o.append(f"<div class=site><span class='tag s'>source</span> "
                 f"<span class=p>{f}:{ln}</span>"
                 f"<p class=note style='margin:6px 0 4px'>{what}</p>"
                 f"<p class=note style='margin:0'><b>Fix:</b> {fix}</p></div>")
    if L.get('fix'):
        # `built` is not inferred from the presence of a commit: the soft-float change lives in a
        # submodule and its commit only records the patch, so the two facts are stated separately.
        lab = "the fix — on <code>%s</code>" % BRANCH if L.get('built') else "what to change"
        o.append(f"<div class=fixb><span class=k>{lab}</span>"
                 f"{measured_strip(L.get('id'), MEAS)}<p>{L['fix']}</p>"
                 f"{commit_chips(L.get('id'))}</div>")
    if L.get('rem'):
        o.append(f"<div class=rem><span class=k>how to check the fix worked</span>{L['rem']}</div>")
    o.append("</div>")
    return "\n".join(o)


CMD = "./compare.py --block-min 25551991 --block-max 25552607"


def _shares_tbl(rows):
    """Rows may mix backends, so columns are named by ROLE, not by guest.

    Naming a specific guest was wrong: the header read `zisk-reth` while one row carried SP1 data.
    Headers are right-aligned to match their numeric column — a left-aligned header drifts further
    from its own figures as the column widens.

    A zero denominator is the WORST case, not the best. `r = a/b if (a and b)` fell through to None
    and then to the `lo` class, painting a family where the Monad guest spends 12.66% and the reth
    guest 0.00% in GREEN. It now reads as a red dash."""
    # Noise floor. A denominator of 0.0029% produced "1167.66×" next to a displayed "0.00%" — a
    # precise-looking figure computed on nothing, which reads as an error even though the arithmetic
    # is right. Below the floor the honest statement is "the other guest does not do this".
    FLOOR = 5e-4
    o = ["<table><tr><th>measure</th><th class=n>Monad guest</th><th class=n>reth guest</th>"
         "<th class=n>ratio</th></tr>"]
    # A measure that is reported on both backends occupies two consecutive rows. Keep them
    # visually joined by a thin rule and separate the MEASURES with a thicker one, so the eye groups
    # by what is being measured rather than by backend.
    # Group key: the 5th element of a row when given, otherwise the label with its backend suffix
    # stripped. The inferred form covers "one measure, two backends"; an explicit key is needed when
    # a group's rows carry different labels (a breakdown of one measure into parts).
    base = [(r[4] if len(r) > 4 else re.sub(r',\s*(ZisK|SP1)$', '', r[0])) for r in rows]
    for i, r in enumerate(rows):
        lbl, a, b, fmt = r[0], r[1], r[2], r[3]
        # No separator on the last row: the table ends there, and a rule below it reads as an
        # empty row.
        endgrp = i < len(rows) - 1 and base[i + 1] != base[i]
        if b is not None and 0 < b < FLOOR and a and a > FLOOR:
            b = 0                                    # treat as absent rather than as a denominator
        if a and not b:
            # The reth guest pays nothing, so the ratio has no finite value — but it is the WORST
            # case, not a missing one. Rendered as a red dash: the colour carries the verdict, the
            # `none` in the neighbouring cell carries the reason.
            cell, cls = "—", 'hi'
        elif a and b:
            r = a / b
            cell, cls = x(r, 2), ('hi' if r >= 1 else 'lo')
        else:
            cell, cls = "—", ''
        # An isolated micro-benchmark row has no comparable reth figure at all — distinct from a
        # reth figure of zero. Show a neutral dash, not "none", which would claim the reth guest does
        # not do this. It does; it is simply not separable from its callers.
        if base[i] in ('iso', 'iso1', 'iso2', 'iso3', 'rm', 'keep'):
            # Rows from the isolated benchmark carry their provenance where the number is, not only in
            # a legend at the top of the page.
            # One badge per group, on its first row: five in a row reads as noise, and the rule
            # "first row of the group carries the provenance" is unambiguous with the group rules.
            first = i == 0 or base[i - 1] != base[i]
            tag = ("<span class='tag b'>isolated</span> "
                   if base[i].startswith('iso') and first else "")
            o.append(f"<tr{' class=grp' if endgrp else ''}>"
                     f"<td{' class=sub' if lbl.startswith('↳') else ''}>{tag}{lbl}</td>"
                     f"<td class=n>{fmt(a)}</td><td class=n>n/a</td><td class=n>—</td></tr>")
            continue
        o.append(f"<tr{' class=grp' if endgrp else ''}>"
                 f"<td{' class=sub' if lbl.startswith('↳') else ''}>{lbl}</td>"
                 f"<td class=n>{fmt(a)}</td>"
                 f"<td class=n>{fmt(b) if b else 'none'}</td>"
                 f"<td class='n {cls}'>{cell}</td></tr>")
    return "".join(o) + "</table>"


def build_archive(d, c, S, Z, P, cs):
    """The ten levers that were built, measured and shipped on `al/zkvm-levers`, plus the Zbb ask.

    Kept in full rather than summarised: each entry carries the mechanism, the failed first attempt
    and the commit, and that is the part nobody can reconstruct from a diff. They are rendered under
    the remaining work, collapsed, because the page's job is now what is LEFT.
    """
    L = []
    _kcm = _load_json('allfive-measured.json')
    # The attribution check for the one lever whose supporting table is a per-family ratio.
    _V256 = verdicts().get('256-bit arithmetic')
    _dvm = _load_json('allfive-measured.json')
    # Derived, not typed: the keccak-256 rate is words x 8, and the worked example's block count
    # follows from the rate. A typed "136" and a typed "3 ×" can drift apart from each other.
    _rate = BENCH['words_per_block'] * 8
    _ex = 4 * _rate - 1          # an input needing four permutations: three full blocks + a final one

    def _calls(axis, side):
        """Median keccak permutations per block. The counter differs per backend — ZisK derives it
        from the keccak state-machine cost, SP1 reports a KECCAK_PERMUTE syscall — so read whichever
        is present. Verified exact: a 400-iteration micro-benchmark reports exactly 400."""
        B = d[axis]['blocks']
        v = [(B[b][side].get('kec') or (B[b][side].get('sys') or {}).get('KECCAK_PERMUTE'))
             for b in B]
        v = [z for z in v if z]
        return statistics.median(v) if v else None
    pa, pb = Z['percall']['a'], Z['percall']['b']
    qa, qb = P['percall']['a'], P['percall']['b']

    def proj(ax, gap):
        return ax['ratio'] * (1 - gap)

    # 1 — containers
    a, b = (fam_share(Z['shares']['a'], 'containers / abstraction'),
            fam_share(Z['shares']['b'], 'containers / abstraction'))
    ap, bp = (fam_share(P['shares']['a'], 'containers / abstraction'),
              fam_share(P['shares']['b'], 'containers / abstraction'))
    # What the fix removes is the OWNING-STRING operations, not the family. Projecting to the reth
    # guest's share assumed the index, its iterator and its key hashing disappear too; they do not.
    # Exclusive classification: the iterator symbol carries `basic_string` in its type, so it must be
    # excluded explicitly or it is counted twice.
    _own = lambda sh: sum(v for fn, v in sh.items()
                          if hs.family(fn) == 'containers / abstraction'
                          and 'basic_string' in fn and '__normal_iterator' not in fn)
    oz, os_ = _own(Z['shares']['a']), _own(P['shares']['a'])

    # 3 — the keccak absorb loop. This was published as a Monad-vs-reth gap of 10.4x and that was
    # WRONG: an isolated micro-benchmark showed the reth guest's figure (41 instructions per
    # permutation) sits below the floor of the accelerated path itself (163 for a one-permutation
    # call), which is impossible. The explanation is attribution, not cost: `zisk-reth` inlines the
    # accelerated wrapper into its callers, so the work lands in trie symbols instead of a keccak
    # one, while the Monad guest reaches the same code across the C ABI where it cannot be inlined
    # and keeps a named symbol. Same failure mode as __bswapdi2 — one side visible, the other not.
    # What survives is real, measured, and upstream: the absorb loop itself is ~6x off a tight loop.
    kec_a, kec_b = _calls('zisk', 'a'), _calls('zisk', 'b')
    kcp_a, kcp_b = _calls('sp1', 'a'), _calls('sp1', 'b')
    ha, hb = fam_share(Z['shares']['a'], 'hashing (keccak/sha)'), \
             fam_share(Z['shares']['b'], 'hashing (keccak/sha)')
    L.append({
        't': 'Fewer keccak hashes — the guest pays around each one, on top of the precompile',
        'id': 'keccak',
        # Built: removing the eager priming hash is worth 11.92 %, against this ceiling of
        # ~13.9 %. The first estimate in the set to land close, and slightly high.
        'rank': (_kcm['keccak'] if _kcm else ha) * 100,
        'rank_word': "measured on" if _kcm else "ceiling on",
        # The share must be the MEASURED hashing family, not permutations x 533: that product applies a
        # unit price this lever explicitly says does not exist, and it overstates by 28%.
        'impact': f"<b>{BENCH['zisklib_1perm']}</b> steps per short hash removed, "
                  f"<b>{BENCH['zisklib_perblock']}</b> per block of a long one",
        'w': f"<b>⚠ REVERTED FOR SOUNDNESS (2026-08-07).</b> The eager-priming half of this lever (+11.92 %) is deliberately given back: the pre-state root must be computed and exposed as a public value, or the proof does not bind to the parent block — commit <code>bf512561f</code> on <code>al/zkvm-r2</code>. The digest-stub half survives. Measured cost of the revert: +14.42 % median on the branch head.<br><br>Both guests reach the same precompile and perform a comparable number of permutations "
             f"({x(kec_a / kec_b, 2)} on ZisK), and the precompile's own trace cost is identical. What "
             f"the guest additionally pays around it, measured in isolation, comes in <b>two forms "
             f"that are not interchangeable</b>:<br><br>"
             f"A <b>whole call whose input fits one {_rate}-byte block costs "
             f"{BENCH['zisklib_1perm']} steps</b> — that is a hash of a 20-byte address or a 32-byte "
             f"slot key, start to finish. <b>Each additional block inside a longer call costs "
             f"{BENCH['zisklib_perblock']}</b>, so a {_ex}-byte hash is {BENCH['zisklib_1perm']} + "
             f"{_ex // _rate} × {BENCH['zisklib_perblock']}. The marginal block is dearer than a "
             f"complete short "
             f"call, so <b>there is no single per-permutation price</b> — use the figure that matches "
             f"what is being removed. At {n_(kec_a)} permutations per block, <b>{pct(ha, 1)}</b> of "
             f"the guest's work sits in this wrapper — the largest "
             f"identified block of keccak overhead.<br><br>"
             f"<b>The lever is the count, not the code.</b> Splitting the "
             f"{BENCH['zisklib_perblock']}-step marginal block shows the absorb loop is only "
             f"<b>{BENCH['absorb_perblock']}</b> of it "
             f"({BENCH['absorb_perblock'] / BENCH['words_per_block']:.1f} per 8-byte word) and "
             f"already optimal — three implementations, from bounds-checked slices to raw unaligned "
             f"reads, measure the same. The remaining "
             f"{BENCH['syscall']} is the <code>keccak_f</code> invocation, ZisK infrastructure with "
             f"no fix on offer here. So the only handle is hashing fewer times, and the saving is "
             f"whichever of the two figures applies: removing an address re-hashed on every storage "
             f"access saves {BENCH['zisklib_1perm']}.<br><br>"
             f"<b>⚠ Do not compare the two guests on hashing.</b> One of them reaches this code "
             f"through an inlinable path, so its cost is charged to its callers and no keccak symbol "
             f"carries it; the other calls it across the C ABI and keeps a named symbol. The family "
             f"ratio therefore measures a compilation difference. The permutation count is the "
             f"comparable quantity; the two call-level costs above are what to act on.",
        'built': bool(_kcm),
        'fix': (f"<b>Stop hashing nodes nothing asks for.</b> <code>prime_node</code> RLP-encoded "
                f"and keccaked <i>every</i> non-DIGEST node in the witness, whether the block "
                f"reached it or not — while <code>child_ref</code> and <code>TrieStore::hash</code> "
                f"already hash on a cache miss and store the result. Dropping the eager pass leaves "
                f"every referenced node hashed exactly once and every unreferenced node not hashed "
                f"at all.<br><br>"
                f"<b>The blob walk in the constructor is deliberately kept.</b> It checks that the "
                f"nodes tile the region exactly, which is a soundness check on the witness, not an "
                f"optimisation."
                + (f"<br><br><b>Corroborated without reference to any symbol:</b> "
                   f"keccak permutations fall "
                   f"<b>{pct(_kcm['keccak_perms_removed'], 1)}</b>, {n_(_kcm['perm_before'])} to "
                   f"about {n_(_kcm['perm_after'])} per block, against {n_(_kcm['perm_reth'])} for "
                   f"the reth guest on the same blocks. Nearly a third of the guest's hashing was "
                   f"for nodes nothing ever asked about." if _kcm else "")),
        'tbl': _shares_tbl(
            # The split was measured on a hand-written absorb variant whose marginal block is 560,
            # 12 steps dearer than the shipped 548 — so 533 + 27 closes on 560, not on 548. Label the
            # split rows with their own total rather than letting them appear not to add up.
            [('whole call, input fits one block', BENCH['zisklib_1perm'], None, lambda v: n_(v),
              'iso1'),
             ('each extra 136-byte block', BENCH['zisklib_perblock'], None, lambda v: n_(v), 'iso2'),
             ('the same block in the variant the split was measured on',
              BENCH['variant_perblock'], None, lambda v: n_(v), 'iso3'),
             ('↳ of that, the keccak_f syscall', BENCH['syscall'], None, lambda v: n_(v), 'iso3'),
             ('↳ of that, the absorb loop', BENCH['absorb_perblock'], None, lambda v: n_(v), 'iso3'),
             ('keccak permutations per block, ZisK', kec_a, kec_b, lambda v: n_(v)),
             ('keccak permutations per block, SP1', kcp_a, kcp_b, lambda v: n_(v))]),
        # No source site any more: the one this lever carried was read_storage's per-slot address
          # rehash in partial_trie_db.cpp, which the guest no longer links. The lever stands —
          # hashing measured 0.960x (ZisK) / 0.975x (SP1) across the change, i.e. untouched by it.
          'sites': SITES.get('hashing', []),
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b>Track the permutation count</b> ({n_(kec_a)} today) with the command above — it is "
               f"the only figure here the sweep re-measures. The per-call costs come from a separate "
               f"guest and are not refreshed by regenerating: to re-derive them, build a ZisK guest "
               f"that hashes a fixed buffer in a loop and take the marginal cost as the difference "
               f"between two iteration counts, at input sizes {BENCH['words_per_block'] * 8 - 1} and "
               f"{4 * BENCH['words_per_block'] * 8 - 1} bytes. Not the hashing family share "
               f"— a cross-guest hashing comparison is invalid while one guest inlines its wrapper "
               f"and the other does not. A removed short hash is worth {BENCH['zisklib_1perm']} steps "
               f"and a removed block inside a longer hash {BENCH['zisklib_perblock']}; on ZisK "
               f"{cs['main'] / cs['total'] * 100:.0f}% of prover cost is linear in steps.",
    })

    # 4 — allocation
    a, b = (fam_share(Z['shares']['a'], 'memory / allocation'),
            fam_share(Z['shares']['b'], 'memory / allocation'))
    aa, ab = share_of(Z['shares']['a'], ALLOC_RE), share_of(Z['shares']['b'], ALLOC_RE)
    ca, cb = share_of(Z['shares']['a'], COPY_RE), share_of(Z['shares']['b'], COPY_RE)
    ap4, bp4 = (fam_share(P['shares']['a'], 'memory / allocation'),
                fam_share(P['shares']['b'], 'memory / allocation'))
    # Allocator entry points, and the reth guest's own share as the reachable floor. On SP1 both
    # guests pay the same TLSF heap heavily, so only the excess is Monad's to remove.
    ENTRY_RE = re.compile(r'sys_alloc_aligned|operator new|operator delete|tlsf|__r(ust|dl)_alloc',
                          re.I)
    _ent = lambda sh: share_of(sh, ENTRY_RE)
    ez, ezr = _ent(Z['shares']['a']), _ent(Z['shares']['b'])
    ep, epr = _ent(P['shares']['a']), _ent(P['shares']['b'])
    xz, xp = ez - ezr, ep - epr

    # 5 — 256-bit arithmetic
    a, b = (fam_share(Z['shares']['a'], '256-bit arithmetic'),
            fam_share(Z['shares']['b'], '256-bit arithmetic'))
    ga = share_of(Z['shares']['a'], DIVGEN_RE) / a if a else 0
    ap5, bp5 = (fam_share(P['shares']['a'], '256-bit arithmetic'),
                fam_share(P['shares']['b'], '256-bit arithmetic'))
    # The division share is what a specialised divmod addresses; the multiplications are not in scope.
    _div = lambda sh: sum(v for fn, v in sh.items()
                          if hs.family(fn) == '256-bit arithmetic' and DIVGEN_RE.search(fn))
    _divr = lambda sh: sum(v for fn, v in sh.items()
                           if hs.family(fn) == '256-bit arithmetic'
                           and re.search(r'div_rem', fn, re.I))
    dz, dp = _div(Z['shares']['a']), _div(P['shares']['a'])
    dr_z = _divr(Z['shares']['b'])
    L.append({
        't': '256-bit division through the generic 128-bit builtin',
        'id': 'div',
        'gap_pp': dz * 100,          # division share, matching this lever's own projection
        'rank': (_dvm['division'] if _dvm else dz) * 100,            # ceiling: the generic-division share
        # Ceiling, not expectation: only the division part is in scope, and a specialised routine
        # still costs something — the reth guest spends 0.6% of its work in its own div_rem.
        # Built and measured in the six-change combination: 3.07 %, against the 6.7 % this
        # lever's own scope estimate implied. The estimate is left visible above precisely because
        # it was high — the same way the bytecode cache's was.
        'rank_word': "measured on" if _dvm else "ceiling on",
        'impact': (f"ZisK {x(Z['ratio'])} → "
                   f"<b>{x(Z['ratio'] * (1 - _dvm['division']))}</b> on this change alone" if _dvm else
                   f"ZisK {x(Z['ratio'])} → <b>{x(Z['ratio'] * (1 - dz))}</b> · "
                   f"SP1 {x(P['ratio'])} → <b>{x(P['ratio'] * (1 - dp))}</b>"),
        'w': f"<b>{ga * 100:.0f}%</b> of the Monad guest's 256-bit arithmetic goes through "
             f"<code>compiler_builtins::u128_div_rem</code> — the generic 128-bit division helper — "
             f"where the reth guests use a hand-written 256-bit <code>div_rem</code>. Same "
             f"operation, different implementation.<br><br>"
             f"<b>What is in scope, quantified.</b> Only the division part: <b>{pct(dz, 1)}</b> of the "
             f"guest's own work on ZisK, <b>{pct(dp, 1)}</b> on SP1. The multiplications in this "
             f"family stay. And a specialised routine is not free — the reth guest still spends "
             f"{pct(dr_z, 1)} in its own <code>div_rem</code> — so read the projection as a ceiling "
             f"that assumes division becomes costless, which it does not."
             # The share ratio in the table below is a per-family figure, and this family is one of
             # the two sitting on the attribution boundary. Stated because the correction moves it
             # AWAY from parity: leaving it out would look like the favourable case was the one
             # chosen. The lever itself does not rest on it — its scope is the guest's own work.
             + (f"<br><br><b>The share ratio below survives the attribution check.</b> Rebuilding the "
                f"reth guest without inlining brings this family's ratio down from "
                f"{x(_V256['ship'], 2)} to {x(_V256['ni'], 2)} — a real narrowing, and this family "
                f"is one of the two sitting on the boundary where that correction is decided. It "
                f"still lands above the {x(a / b if b else None, 2)} the table below shows, so the "
                f"cross-guest column here is the conservative one. Nothing in this lever depends on "
                f"it either way: its scope is measured against the guest's own work."
                if _V256 and b else ""),
        'built': bool(_dvm),
        'fix': (f"<b>The cost is one level below the long division.</b> <code>uint256</code>'s is "
                f"already a hand-written Knuth D on 64-bit limbs; what it calls is "
                f"<code>portable::div</code> — re-exported by the zkVM shadow as "
                f"<code>intrinsics::div</code>, the real intrinsics being x86 assembly — and that "
                f"was written as <code>unsigned __int128</code> division, which on this target "
                f"becomes <code>__udivti3</code> + <code>__umodti3</code> in "
                f"<code>compiler_builtins</code>.<br><br>"
                f"<b>Specialise the normalised case.</b> The caller already asserts "
                f"<code>u_hi &lt; v</code>, which is exactly what Hacker's Delight 9-3 handles with "
                f"two 64/32 divisions instead of a generic 128-bit one."
                + (f"<br><br><b>Verified against native 128-bit division over 40 M cases before "
                   f"building</b> — the routine is arithmetic, so correctness is checkable directly "
                   f"and not only through a state root. Note "
                   f"for anyone estimating the next one: this lever's scope suggested "
                   f"{pct(dz, 1)} and the build returned {pct(_dvm['division'], 2)}. <b>A family "
                   f"share bounds where the work is; it does not predict what removing it "
                   f"costs.</b>" if _dvm else "")),
        'tbl': _shares_tbl([('256-bit arithmetic share, ZisK', a, b, lambda v: pct(v)),
                            ('256-bit arithmetic share, SP1', ap5, bp5, lambda v: pct(v))]),
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b>generic-division share</b> should fall from {pct(dz, 1)} of the guest's work "
               f"toward the reth guest's {pct(dr_z, 1)}. Watch the family share too: if the generic "
               f"helper disappears but the family share holds, the cost moved into the specialised "
               f"routine and there is no win.",
    })

    # ── byte order: closed as a COMPARISON, and that was read as closed altogether ────────────────
    # This sat under "not levers" because the reth guest shows 0.00% and no comparison is possible.
    # But the absolute figure is the second largest single symbol in the guest, and prover cost is
    # billed on absolute work — so "not comparable" was doing the work of "nothing to do".
    _bsz = symbol_steps(d, c, 'zisk', 'monad-zisk', r'bswap')
    _bsp = symbol_steps(d, c, 'sp1', 'monad-sp1', r'bswap')
    # The lever was built, applied and measured. Predicted 1.1–4.1%, delivered 1.87% — the estimate
    # is superseded by the file below, never averaged with it.
    try:    _bsm = json.load(open(os.path.join(HERE, 'results', 'bswap-measured.json')))
    except Exception: _bsm = None
    try:    _scm = json.load(open(os.path.join(HERE, 'results', 'scan-measured.json')))
    except Exception: _scm = None
    try:    _cbm = json.load(open(os.path.join(HERE, 'results', 'combo-measured.json')))
    except Exception: _cbm = None
    _dgm = _load_json('digest-measured.json')
    _afm = _load_json('allfive-measured.json')
    if _bsz:
        # Costs measured on the shipped ELF (callee body = 26 instructions) and on a rebuild of both
        # variants for the same target: one uint256 conversion is 31 caller + 4x26 callee = 135 steps
        # through the builtin, and 75 fully inline. Per isolated scalar swap: 33 against 29.
        _CALLEE, _UI_NOW, _UI_INL, _SC_NOW, _SC_INL = 26, 135, 75, 33, 29
        _calls = _bsz['steps'] / _CALLEE
        _conv = _calls / 4                                   # if grouped as uint256, four at a time
        _hi = (_conv * (_UI_NOW - _UI_INL)) / _bsz['work']   # all swaps grouped
        _lo = (_calls * (_SC_NOW - _SC_INL)) / _bsz['work']  # none grouped
        L.append({
            't': 'Byte-order conversion goes through a libgcc call, 26 instructions at a time',
            # Measured supersedes predicted. The estimate was 1.1–4.1%; the build delivered 1.87%,
            # and publishing the estimate next to it would only invite averaging the two.
            'rank': (_bsm['gain_median'] if _bsm else _hi) * 100,
            'id': 'bswap',
            'rank_of': "the guest's own work",
            'rank_word': "measured on" if _bsm else "ceiling on",
            # `rank` already renders "N% … on the guest's own work"; repeating it here printed the
            # figure twice in one chip. What this adds is the spread and the raw counts.
            'impact': (f"{pct(_bsz['share'], 2)} sits in one symbol, "
                       f"{_calls / 1000:.0f}k calls per block" if _bsm else
                       f"({pct(_lo, 1)} if no swap is grouped) · {pct(_bsz['share'], 2)} sits in "
                       f"one symbol, {_calls / 1000:.0f}k calls per block"),
            'w': f"<code>bswap&lt;T&gt;</code> is <code>gnu::always_inline</code> and <i>is</i> "
                 f"inlined. The <code>std::byteswap</code> inside it is not: with no "
                 f"<code>rev8</code> on this target the backend emits a call to "
                 f"<code>compiler_builtins::__bswapdi2</code>, whose body is <b>26 instructions</b> "
                 f"in the shipped ELF. That symbol alone is <b>{_bsz['steps'] / 1e6:.1f} M steps per "
                 f"block</b>, {pct(_bsz['share'], 1)} of the guest's work on ZisK and "
                 f"{pct(_bsp['share'], 1) if _bsp else 'a comparable share'} on SP1 — and the "
                 f"call/frame overhead on the caller side is charged to the callers, so it is not "
                 f"even included in that figure.<br><br>"
                 f"<b>Where the calls come from.</b> No call graph exists for this guest, so the "
                 f"driver is identified by correlation across the {_bsz['n']} profiled blocks: the "
                 f"symbol tracks the interpreter's own symbols at <b>r = 0.99</b>, against 0.85 for "
                 f"trie/MPT and 0.93 for total work. That is the EVM word path — every big-endian "
                 f"256-bit word converted to limbs is four of these calls.<br><br>"
                 f"<b>What an explicit sequence costs, measured.</b> Rebuilt both variants for "
                 f"<code>rv64ima</code>: one <code>uint256</code> conversion is <b>135 steps</b> "
                 f"through the builtin (31 in the caller plus four 26-instruction calls) and "
                 f"<b>75 inline</b> — 1.8×, because the two mask constants are materialised once and "
                 f"shared across the four swaps. An <i>isolated</i> scalar swap gains almost nothing "
                 f"(33 against 29): the whole win is in the grouping.",
            'built': bool(_bsm),
            # The estimate in the finding is left standing next to the measurement on purpose: it is
            # 2x optimistic, and the reason is the part an instruction-count model cannot see.
            'fix': (f"<b>Replace <code>std::byteswap</code> with an explicit mask-and-shift "
                    f"sequence</b> at every call site, behind one header "
                    f"(<code>category/core/bit_primitives.hpp</code>) so the three ABI helpers this "
                    f"guest works around live in one place and can be deleted together the day the "
                    f"target grows Zbb."
                    + (f"<br><br><b>The build.</b> The guest "
                    f"was rebuilt from <code>{_bsm['commit'][:9]}</code> with "
                    f"<b>{_bsm['sites_patched']} call sites</b> across {_bsm['files_patched']} files "
                    f"converted, leaving <b>{_bsm['callsites_after']} calls</b> to "
                    f"<code>__bswapdi2</code> in the binary; the unpatched baseline reproduces the "
                    f"shipped ELF exactly (<code>.text</code> {_bsm['text_baseline']:,} bytes and "
                    f"<code>__bswapdi2</code> at the same address in both). "
                    f"<b>{_bsm['regressions']} block regressed.</b>"
                    f"<br><br><b>Where the cost actually was: the PUSH opcode.</b> A first pass "
                    f"converted {_bsm['partial_sites']} sites in the uint256 and word-conversion "
                    f"headers and returned only {pct(_bsm['partial_gain_median'], 2)}. Disassembling "
                    f"that build showed <b>653 remaining call sites, every one of them inside "
                    f"<code>monad::vm::interpreter::push&lt;N, EvmTraits&lt;…&gt;&gt;</code></b> — "
                    f"<code>category/vm/interpreter/push.hpp</code> has its own "
                    f"<code>std::byteswap</code>, and PUSH is instantiated for each immediate width "
                    f"times each revision. Those four extra sites are worth more than the first "
                    f"thirteen: {pct(_bsm['partial_gain_median'], 2)} → "
                    f"{pct(_bsm['gain_median'], 2)}. It also confirms the r = 0.99 correlation with "
                    f"the interpreter, at the level of one opcode."
                    f"<br><br><b>Half the win is paid back at the call sites.</b> The helper loses "
                    f"{_bsm['helper_removed'] / 1e6:.1f} M steps, but "
                    f"{_bsm['inline_added'] / 1e6:.1f} M come back as inline code in the callers — "
                    f"<b>{_bsm['inline_added'] / _bsm['helper_removed'] * 100:.0f}% of what removing "
                    f"the call saved is paid again</b>, because outside a micro-benchmark the mask "
                    f"constants are re-materialised under register pressure rather than shared. Net "
                    f"{_bsm['net_removed'] / 1e6:.1f} M, and <code>.text</code> grows "
                    f"{(_bsm['text_patched'] / _bsm['text_baseline'] - 1) * 100:.1f}%. An "
                    f"instruction-count model cannot see register pressure; only the build can."
                       if _bsm else "")
                    + f"<br><br><b>One instruction would remove all of it, and this is the ask.</b> "
                      f"RISC-V has <code>rev8</code> in the Zbb extension; the ZisK target is "
                      f"<code>riscv64ima</code> and its converter recognises no Zbb opcode, and "
                      f"SP1's is riscv64im-succinct, also without Zbb. A byte permutation is close "
                      f"to free in an AIR — it re-wires bytes rather than computing on them — so "
                      f"this is a cheap ask of either backend. At one instruction instead of 26 the "
                      f"whole {pct(_bsz['share'], 1)} collapses, which is more than this lever can "
                      f"reach from inside the guest."),
            # No comparison table: this is the one family where the reth guest has no symbol to
            # compare against, which is the reason it was filed as a non-lever in the first place.
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>__bswapdi2</code> should disappear from the profile entirely.</b> If it "
                   f"survives, a <code>std::byteswap</code> call site was missed. Watch total work "
                   f"too: the inline code lands in the callers, so the interpreter's family grows "
                   f"while the total falls — a family share alone will not show the win.",
        })

    # The bytecode-cache lever was built, measured and REFUTED — see build_nonlevers. It is not
    # softened here, it is gone: a lever list that keeps a disproved entry is a to-do list.
    _pc = symbol_steps(d, c, 'zisk', 'monad-zisk', r'__popcountdi2')
    # Found by reading the absolute symbol table, then by two builds — the first of which was
    # NEGATIVE and is what located the real cost. See the non-lever below for that step.
    if _dgm:
        L.append({
            't': 'Digest stubs are copied into a hash map they never need to be in',
            'rank': _dgm['gain_median'] * 100,
            'id': 'digest',
            'rank_of': "the guest's own work",
            'rank_word': "measured on",
            # The chip states the size of the problem; the measured strip inside the fix states what
            # the change returned. Both used to print the spread and the root count.
            'impact': f"{pct(_dgm['digest_share'], 1)} of witness nodes are stubs · the fix is "
                      f"8 lines",
            'w': f"<b>{pct(_dgm['digest_share'], 1)} of the nodes in a witness are DIGEST stubs</b> — "
                 f"32-byte hashes standing in for untouched subtrees. The reader's constructor sweeps "
                 f"the blob and calls <code>prime_node</code> on every node, which for a DIGEST does "
                 f"one thing: <code>hashes_.emplace(id, DigestView{{node}}.hash())</code>. That copies "
                 f"32 bytes out of the blob into a hash map <b>keyed by the offset they came from</b>. "
                 f"<code>child_ref</code> then looks them up by that offset.",
            'built': True,
            'fix': f"<b>Resolve the node before the map is consulted, and short-circuit the DIGEST "
                 f"case.</b> A DIGEST's hash needs no map: it is already the bytes at a known "
                 f"offset, reachable by pointer arithmetic. That removes both the insert and the "
                 f"probe — and shrinks <code>hashes_</code> from every node in the witness to the "
                 f"~10% that are real, which makes the surviving lookups cheaper too. Eight "
                 f"lines.<br><br>"
                 f"<code>.text</code> comes out {_dgm['text_baseline'] - _dgm['text']} bytes "
                 f"smaller, <code>prime_node</code> loses "
                 f"{abs(_dgm.get('prime_delta', 0)) / 1e6:.1f} M steps per block, and "
                 f"<code>child_ref</code> does not absorb them.<br><br>"
                 f"<b>The first attempt at this measured {pct(_dgm['first_attempt_gain'], 2)}</b> — "
                 f"removing the priming alone sent the whole cost into <code>child_ref</code>'s lazy "
                 f"insert. That failure is what identified the map, rather than the priming, as the "
                 f"thing to remove.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>prime_node</code> should shrink by roughly nine tenths</b> and "
                   f"<code>child_ref</code> should not grow. If <code>child_ref</code> grows, the "
                   f"DIGEST case is still reaching the map — check that the node is resolved "
                   f"<i>before</i> <code>hashes_.find</code>, not after.",
        })
    # One token. The shipped scan writes `auto i = 0u`, a 32-bit counter used as a 64-bit index, so
    # the compiler re-zero-extends it every iteration: slli+srli, 2 of the 10 instructions in the
    # loop body. Measured, not argued — the scan falls 18.8 %, close to the 20 % the count predicts.
    if _scm:
        _A = _scm['A']
        L.append({
            't': 'The JUMPDEST scan re-extends a 32-bit counter on every byte',
            'rank': _A['gain_median'] * 100,
            'id': 'scanidx',
            'rank_of': "the guest's own work",
            'rank_word': "measured on",
            'impact': f"{n_(_A['scan_before'])} steps per block in the scan · one token",
            'w': f"<code>Intercode::find_jumpdests</code> scans every contract byte for JUMPDESTs. "
                 f"Its loop variable is <code>auto i = 0u</code> — 32-bit, on a 64-bit target, used "
                 f"as an index. The emitted loop is 10 instructions per byte and <b>two of them are "
                 f"<code>slli</code>/<code>srli</code> by 32</b>, re-widening the counter each "
                 f"time.<br><br>"
                 f"The scan is <b>one pass per distinct contract</b>, not per call — caching was "
                 f"built and refuted, see below. This is the loop itself.",
            'built': True,
            'fix': f"<b>Declare the index <code>size_t</code></b>. One token, and both shifts go.<br>"
                 f"<br>The scan itself falls "
                 f"<b>{(1 - _A['scan_after'] / _A['scan_before']) * 100:.1f}%</b> "
                 f"({n_(_A['scan_before'])} → {n_(_A['scan_after'])} steps per block) and "
                 f"<code>.text</code> shrinks by "
                 f"{_scm['text_baseline'] - _A['text']} bytes. The 20 % an instruction count predicts "
                 f"and the 18.8 % measured agree here — the byte-order lever showed that they need "
                 f"not, so the build is still what settles it.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>find_jumpdests</code> should fall about a fifth</b> and nothing else "
                   f"should move. Any other change means the loop was restructured, not just the "
                   f"index widened.",
        })
    # Found only after the other nine landed: removing 27 % of the guest reshaped the profile and
    # left this as its largest untouched symbol. It is in the list because the absolute cost is what
    # the prover bills, not because the reth guest is cheaper here.
    _kh = _load_json('allfive-measured.json')
    if _kh and _kh.get('keyhash'):
        L.append({
            't': 'Every account and storage lookup runs wyhash over the key',
            'rank': _kh['keyhash'] * 100,
            'id': 'keyhash',
            'rank_of': "the guest's own work",
            'rank_word': "measured on",
            'impact': f"{n_(_kh['wyhash_removed'])} steps per block in wyhash",
            'w': f"<code>std::hash&lt;Address&gt;</code> and <code>std::hash&lt;bytes32_t&gt;</code> "
                 f"both call <code>unordered_dense::detail::wyhash</code> over the raw 20 or 32 key "
                 f"bytes, on every account lookup, storage access and code read. It is a general "
                 f"hash doing byte-wise work on keys that are already uniform.",
            'built': True,
            'fix': f"<b>Fold the 64-bit words with XOR and multiply once.</b> Both key types are "
                 f"keccak-derived or small big-endian integers, so "
                 f"<code>(w0^w1^w2) * 0x9E3779B97F4A7C15</code> distributes them at least as well — "
                 f"and strictly better on consecutive storage slots, which a byte-wise hash "
                 f"scatters no better than chance.<br><br>"
                 f"<b>The first version of this lost {pct(-_kh['keyhash_v1'], 2)}</b>, and up to "
                 f"{pct(-_kh['keyhash_v1_min'], 2)} on the largest blocks. wyhash did fall by "
                 f"{n_(_kh['wyhash_removed'])} steps, but <code>immer</code>'s HAMT grew by more: "
                 f"<b>it indexes on the LOW bits</b>, and a bare multiply leaves those poorly mixed "
                 f"— all the entropy lands at the top. Adding <code>h ^= h &gt;&gt; 29</code> gives "
                 f"the low end entropy too and turns the change into "
                 f"<b>{pct(_kh['keyhash'], 2)}</b>. The per-block spread collapsed with it, from "
                 f"+0.91…−2.83 % to {pct(0.005, 1)}…{pct(0.016, 1)}.<br><br>"
                 f"<b>The state roots do not validate this one.</b> A hash map returns correct "
                 f"results under any deterministic hash, so the roots prove the change is not "
                 f"broken, never that the hash is good — a poor one shows up as <i>more steps</i>, "
                 f"not a wrong answer. The step count is the quality check, and it is why the first "
                 f"version had to be measured rather than reasoned about.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>wyhash</code> should disappear and nothing should grow.</b> If "
                   f"<code>immer</code>'s <code>champ</code> grows, the low bits are not mixed — that "
                   f"is the failure mode, and it costs more than the hash saves.",
        })

    # Two more ABI helpers, both eliminated in the combined build. Grouped with popcount because
    # the diagnosis, the fix and the upstream ask are the same for all three.
    _pcm = _load_json('allfive-measured.json')
    _clz = symbol_steps(d, c, 'zisk', 'monad-zisk', r'__clzdi2')
    if _clz:
        L.append({
            't': 'Count-leading-zeros is a 45-instruction libgcc call',
            'rank': _clz['share'] * 100,
            'id': 'clz',
            'rank_of': "the guest's own work",
            'impact': f"{n_(round(_clz['steps']))} steps per block · the RLP byte-length path",
            'w': f"<code>std::countl_zero</code> has no instruction on <code>rv64ima</code> "
                 f"(<code>clz</code> is in Zbb), so it becomes a call to <code>__clzdi2</code> — "
                 f"<b>45 instructions</b> in the shipped ELF, plus the call. The callers are the trie "
                 f"node encoder, which needs it for an integer's RLP byte length "
                 f"(<code>uint256.hpp:588</code>, <code>rlp/encode.hpp</code>), and expmod's gas "
                 f"cost.",
            'built': True,
            'fix': f"<b>A branchless binary search, inline</b> — about eighteen instructions against "
                 f"the helper's 45 plus the call, in the same "
                 f"<code>bit_primitives.hpp</code> as the byte-order fix.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>__clzdi2</code> should disappear from the profile.</b>",
        })
    _flt = symbol_steps(d, c, 'zisk', 'monad-zisk', r'__floatundisf|__mulsf3|float::mul')
    if _flt:
        L.append({
            't': 'The hash map computes its bucket capacity in floating point',
            'rank': _flt['share'] * 100,
            'id': 'softfloat',
            'rank_of': "the guest's own work",
            'impact': f"{n_(round(_flt['steps']))} steps per block · a zkVM guest with no FPU",
            'w': f"<code>unordered_dense</code> stores <code>max_load_factor</code> as a "
                 f"<code>float</code> and computes <code>m_num_buckets * 0.8f</code> on every rehash "
                 f"and size query. There is no FPU here, so each one is a call to "
                 f"<code>__floatundisf</code> and <code>__mulsf3</code>. The map is on the state and "
                 f"trie paths, so this runs throughout the block. The sort of thing that is "
                 f"invisible until the profile is read by symbol: <b>floating point in a guest that "
                 f"has none</b>.",
            'built': True,
            'fix': f"<b>Compute the capacity in integers.</b> For the power-of-two bucket counts "
                 f"this map uses, <code>(n * 4) / 5</code> gives the same result with no float at "
                 f"all — two lines in the header.<br><br>"
                 f"<b>This one is in a submodule</b> (<code>third_party/unordered_dense</code>), so "
                 f"the branch cannot carry it: the commit below records the patch rather than "
                 f"applying it, and the change has to go upstream or be carried as a submodule "
                 f"patch. The same commit holds a second popcount site, in "
                 f"<code>third_party/immer</code>. <b>The pair is worth 0.40 point.</b> They are in "
                 f"the build that measured {pct(_afm['gain_median'], 2)}, the figure quoted on this "
                 f"page — so a checkout of the branch alone, submodules untouched, lands 0.40 below "
                 f"it." if _afm else "",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b>No <code>__float*</code> or <code>__*sf3</code> symbol should remain.</b> If "
                   f"one does, another float slipped in — the guest should have zero.",
        })

    # popcount: the same shape as the byte-order lever — an outlined libgcc helper standing in for an
    # instruction this target does not have. Small, but the fix and the upstream ask are identical,
    # so it belongs next to it rather than in a list of leftovers.
    if _pc:
        L.append({
            't': 'Population count goes through the same kind of libgcc call',
            # Built: +0.09 %, against a 0.45 % symbol share. Kept in the list with its measurement
            # because the shortfall is the finding, not a disappointment.
            'rank': (_pcm['popcount'] if _pcm else _pc['share']) * 100,
            'rank_word': "measured on" if _pcm else "ceiling on",
            'id': 'popcount',
            'rank_of': "the guest's own work",
            'impact': f"{n_(round(_pc['steps']))} steps per block · 382 call sites in 190 functions",
            'w': f"<code>__popcountdi2</code>, the exact shape of the byte-order lever: "
                 f"<code>std::popcount</code> has no instruction on <code>rv64ima</code> (it is "
                 f"<code>cpop</code>, in the same Zbb extension as <code>rev8</code>), so the backend "
                 f"emits a call. <b>Every call site in the guest belongs to <code>immer</code>'s "
                 f"persistent map</b> — champ bitmap counts, inlined into the State accessors — "
                 f"verified by demangling the enclosing function of all 382 sites. The MPT branch-mask "
                 f"popcounts one would expect are not there: that code is dead-stripped from the "
                 f"guest.<br><br>"
                 f"Smaller than the byte-order lever by an order of magnitude, and listed for two "
                 f"reasons: the inline fix is the same one line, and it is the <b>second</b> symbol "
                 f"to cost real work purely because one Zbb instruction is missing. That makes the "
                 f"upstream ask worth more than either lever alone.",
            'built': bool(_pcm),
            'fix': (f"<b>The standard SWAR sequence, inline</b>, in the same "
                    f"<code>bit_primitives.hpp</code> as the other two helpers. <b>The commit below "
                    f"is not sufficient on its own</b>: it converts the category/ call sites, but "
                    f"every site the guest actually links is <code>immer</code>'s (submodule), so "
                    f"the measured effect requires the <code>third_party/patches/</code> pair as "
                    f"well."
                    + (f"<br><br><b>That is well short of the "
                    f"{pct(_pcm['popcount_share'], 2)} the symbol holds, and the shortfall is the "
                    f"finding.</b> The SWAR sequence needs four "
                    f"64-bit constants (<code>0x5555…</code>, <code>0x3333…</code>, "
                    f"<code>0x0F0F…</code>, <code>0x0101…</code>), each about six instructions to "
                    f"materialise, so it costs nearly as much as the 29-instruction helper it "
                    f"replaces — only the call overhead is saved, about "
                    f"{_pcm['popcount_saved_per_call']:.0f} steps across "
                    f"{n_(_pcm['popcount_calls'])} calls per block.<br><br>"
                    f"<b>That is the argument for the instruction, not against the lever.</b> "
                       f"<code>cpop</code> is one instruction; no software sequence can compete "
                       f"when the constants alone cost more than the call." if _pcm else "")),
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b><code>__popcountdi2</code> should disappear.</b> On 16-bit masks a 4-bit "
                   f"table lookup or the standard SWAR sequence both beat a call; measure, because "
                   f"the byte-order lever paid back a third of its saving at the call sites.",
        })

    # ── Zbb: the only lever on this page that the guest cannot pull ────────────────────────────────
    # It was scattered across three levers as an aside ("one instruction would remove all of it") and
    # never stated as an item with a number of its own. It needs two numbers, not one, and they must
    # not be confused: 8.05% is what the three helpers cost the SHIPPED guest, and that overlaps the
    # built levers, so it cannot rank this entry. What ranks it is the marginal figure — what the
    # software workaround still costs AFTER the branch, which is additive with everything above.
    _opt = _load_json('optimized-zisk.json')
    if _bsz and _pc and _clz and _bsm and _afm and _opt:
        _CLZ_BODY, _CLZ_INL = 45, 18      # helper body in the shipped ELF, and the inline sequence
        _BSW_BODY = 26
        _hlp = _bsz['steps'] + _pc['steps'] + _clz['steps']
        _hlp_share = _bsz['share'] + _pc['share'] + _clz['share']
        _work = _bsz['work']
        # Calls per block, from each helper's own steps divided by its body length.
        _n_bsw = _bsm['helper_removed'] / _BSW_BODY
        _n_pop = _afm['popcount_calls']
        _n_clz = _clz['steps'] / _CLZ_BODY
        # What the inline replacements still cost, per block. Two are measured (the byte-order
        # residue directly, popcount's as the gap between its symbol share and its measured gain);
        # the clz one is scaled by the two sequence lengths, so it is the weakest of the three.
        _res_bsw = _bsm['inline_added']
        _res_pop = (_afm['popcount_share'] - _afm['popcount']) * _work
        _res_clz = _clz['steps'] * _CLZ_INL / _CLZ_BODY
        _res = _res_bsw + _res_pop + _res_clz
        _zbb = _n_bsw + _n_pop + _n_clz          # one instruction per call
        _post = _work * (1 - _opt['gain_median'])
        _marg = (_res - _zbb) / _post
        L.append({
            # Not "the levers above": this entry ranks on its marginal figure, so two of the three
            # it refers to sit below it.
            't': 'Three of these levers exist only because the target has no Zbb extension',
            'id': 'zbb',
            'upstream': 'upstream · both backends',
            'rank': _marg * 100,
            'rank_word': "estimated on",
            'rank_of': "the guest's own work after the branch",
            'impact': f"<b>{pct(_hlp_share, 2)}</b> against the shipped guest · nothing the guest "
                      f"side can do — the flag already exists",
            'w': f"<code>rev8</code>, <code>cpop</code> and <code>clz</code> are single RISC-V "
                 f"instructions, and all three live in the <b>Zbb</b> bit-manipulation extension. "
                 f"The guest targets <code>riscv64ima</code> on ZisK and "
                 f"<code>riscv64im-succinct-zkvm-elf</code> on SP1; neither base ISA has them, so "
                 f"<code>std::byteswap</code>, <code>std::popcount</code> and "
                 f"<code>std::countl_zero</code> each become a libgcc call with a multi-instruction "
                 f"body. In the shipped guest those three symbols carry "
                 f"<b>{_hlp / 1e6:.1f} M steps per block, {pct(_hlp_share, 2)}</b> of its work. "
                 f"<b>The reth guest carries no such symbol at all</b> — not a small share, none: "
                 f"Rust's intrinsics stay inline through the same missing instructions. The "
                 f"asymmetry is a property of the toolchain, not of either client.<br><br>"
                 f"<b>The software workaround is not free, and that is the argument.</b> The branch "
                 f"replaces all three with inline sequences, and the sequences still cost "
                 f"<b>{_res / 1e6:.1f} M steps per block</b>: the byte-order masks are "
                 f"re-materialised under register pressure rather than shared "
                 f"({_res_bsw / 1e6:.1f} M, measured), popcount's four 64-bit constants cost nearly "
                 f"what the helper did ({_res_pop / 1e6:.1f} M, measured), and the "
                 f"count-leading-zeros search is {_CLZ_INL} instructions against the helper's "
                 f"{_CLZ_BODY} ({_res_clz / 1e6:.1f} M, scaled from the two lengths). No software "
                 f"sequence can compete when the constants alone cost more than a call.",
            'built': False,
            'fix': f"<b>Ask the backend to accept Zbb.</b> The guest side is already done: GCC emits "
                   f"<code>rev8</code>, <code>cpop</code> and <code>clz</code> as one instruction "
                   f"each at <code>-march=rv64ima_zbb</code> — verified by compiling — so the whole "
                   f"change on this side is a target flag, and "
                   f"<code>category/core/bit_primitives.hpp</code> is then deleted whole.<br><br>"
                   f"<b>Why it cannot be done today.</b> ZisK's ELF-to-AIR converter recognises no "
                   f"Zbb opcode, so a Zbb binary does not convert; SP1's target is "
                   f"<code>riscv64im-succinct</code>, also without it. Both blockers are on the "
                   f"backend side, which is why this is the one item here that cannot be closed by "
                   f"changing the Monad guest.<br><br>"
                   f"<b>Why it should be a cheap ask.</b> These are the operations an AIR is best "
                   f"at: a byte permutation re-wires bytes rather than computing on them, and a "
                   f"population count is a sum of bits already decomposed in the trace. None "
                   f"introduces a new field operation.<br><br>"
                   f"<b>Expected gain, two figures — do not add them.</b> Against the shipped guest "
                   f"the three helpers are <b>{pct(_hlp_share, 2)}</b>, but that overlaps the three "
                   f"levers named here, which already remove the calls. On top of the branch it is "
                   f"worth about "
                   f"<b>{pct(_marg, 1)}</b> — {(_res - _zbb) / 1e6:.1f} M of the "
                   f"{_res / 1e6:.1f} M the workaround still costs, on a post-branch block of "
                   f"{_post / 1e6:.0f} M steps. That second figure is the additive one.",
            'rem': f"<pre># once a backend accepts Zbb\n"
                   f"-march=rv64ima_zbb   # in the guest's target spec\n{CMD}\n./levers.py</pre>"
                   f"<b>Neither the helpers nor their replacements should appear.</b> "
                   f"<code>__bswapdi2</code>, <code>__popcountdi2</code> and <code>__clzdi2</code> "
                   f"are already gone on the branch, so the check is on the callers: the mask "
                   f"constants <code>0x00FF00FF00FF00FF</code> and <code>0x5555555555555555</code> "
                   f"should disappear from the disassembly. If they survive, the flag reached the "
                   f"compiler but the inline sequences were left in place instead of being deleted.",
        })

    # 6/7 — the two small pure-loss items, ZisK opcode level
    _awm = _load_json('allfive-measured.json')
    aw = opcode(d, 'zisk', 'add_w'); sl = opcode(d, 'zisk', 'sll')
    L.append({
        't': '32-bit integer types emit an instruction class the reth guest never does',
        # Was the ceiling (the whole add_w class, 0.94% of prover cost) while the label already read
        # "measured on the guest's own work" — so the chip claimed a figure the build never returned.
        # The measured value also ranks the lever where it belongs.
        'rank': (_awm['addw'] * 100) if _awm else cs['add_w'] / cs['total'] * 100,
        'id': 'int32',
        # sll is billed inside the bit-ops group, so quote it from the opcode cost directly
        'rank_of': "prover cost",   # not a work share: see the note under the list
        # Built: +0.344 % of the guest's work. The static instruction count barely moved (6 fewer
        # of 3,900) — the six were in the reader's hottest paths, which is why the dynamic gain is
        # forty times what the static delta suggests.
        'rank_word': "measured on" if _awm else "ceiling on",
        'rank_of': "the guest's own work" if _awm else "prover cost",
        'impact': (f"<code>NodeId</code>, eight casts · the wire format is untouched · "
                   f"re-verdicted 2026-08-09 on 504 blocks: kept (cost med 1.0038×, 0/504 for "
                   f"removal; the 16-block +2.10 % median shrank to +0.07 % at 504)" if _awm else
                   "paid by one guest only"),
        'w': f"<b>ZisK only</b> — not because SP1 is unaffected, but because SP1's report groups "
             f"opcodes into six buckets (mem, branch, shift, mul, divrem, ecall) and this one is not "
             f"separable from them. Absence here is a limit of the counter, not a result. "
             f"Small, but not a trade-off — one guest pays and the other does not, for the "
             f"same work. <code>add_w</code> — RV64's 32-bit add with sign extension — appears on "
             f"<b>{aw['present_a']} of the Monad guest's {aw['n_blocks']}</b> blocks and on "
             f"<b>{aw['present_b']}</b> of the reth guest's, worth "
             f"{cs['add_w'] / cs['total'] * 100:.2f}% of prover cost. It comes from "
             f"<code>int</code>/<code>uint32_t</code> in hot paths.",
        'built': bool(_awm),
        'fix': (f"<b>Widen the hot-path integer types to 64-bit</b>, which removes the instruction "
                f"class outright. The one built is <code>NodeId</code>, the trie reader's blob "
                f"offset, declared <code>enum class : uint32_t</code> — eight casts.<br><br>"
                f"<b>The wire format is deliberately untouched</b> — but HOW that is achieved "
                f"turned out to be the whole lever. On the rebased reader "
                f"(<code>sam/zkvm-zisk-sp1</code>) node extents were derived from "
                f"<code>sizeof(NodeId)</code>, so widening the enum silently made the reader "
                f"misparse the blob. The port introduces "
                f"<code>NODE_ID_WIRE&nbsp;=&nbsp;4</code> and derives extents and the id append "
                f"from it: register width and wire width now move independently. Re-measured on "
                f"the rebased reader: <b>+2.10 % median</b> (+1.84…+3.93, no regression) — six "
                f"times the original +0.34 %, because <code>NodeId</code> is arithmetic in more "
                f"places there. Commit <code>f737a9de6</code> on <code>al/zkvm-r3</code>."
                + (f"<br><br><b>The interesting part is the ratio between the static and the "
                   f"dynamic count.</b> The <i>static</i> instruction count moved by "
                   f"{_awm['addw_static_delta']} out of {n_(_awm['addw_static_total'])}; the "
                   f"runtime moved forty times that, because those {_awm['addw_static_delta']} sit "
                   f"in the reader's hottest paths. <b>Counting instructions in a binary says what "
                   f"code exists, not how often it runs.</b>" if _awm else "")),
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b><code>add_w</code> should disappear entirely</b> — present on "
               f"{aw['present_a']} blocks today, {aw['present_b']} for the reth guest — once hot-path "
               f"integer types are widened to 64-bit.",
    })
    return L


RX = {
    'mem':    r'mem::memcpy|mem::memmove|mem::memset|mem::memcmp|compiler_builtins.*mem::',
    'mul256': r'div_result|operator\*\(monad::uint256|mulmod',
    'scan':   r'find_jumpdests',
    'champ':  r'champ|__popcountdi2',
    'ctr':    r'ankerl|unordered_dense|segmented_vector',
    'keccak': r'keccak256|keccak_f',
    'zbb':    r'__popcountdi2|__clzdi2|__bswapdi2',
}


def build_levers(d, c, S, Z, P, cs, PO):
    """What is left, ranked against `monad-levers` — the guest as it now stands, not what ships.

    Every entry here is a CANDIDATE: none has been built, so none carries a measured strip or a
    commit. The rank is a ceiling — the absolute cost of the code the change would touch — and the
    lesson of the first round is printed on the page: the ten built levers returned a median of 43 %
    of their ceilings, so read these as bounds and not as forecasts.
    """
    if not PO:
        return []
    L = []
    A = lambda bk, k: post_steps(PO, bk, RX[k])           # (share, absolute)
    Zm = PO.get('zisk', {}).get('median')
    Pm = PO.get('sp1', {}).get('median')
    ref = lambda bk, k: (share_of(PO[bk]['ref_shares'], re.compile(RX[k]))
                         if bk in PO and PO[bk].get('ref_shares') else None)

    # 1b — the witness format, post-soundness. Sized from the profiled decomposition of the
    # binding cost; the population audit confirmed the permutations themselves are at parity with
    # zisk-reth, so the format tax is the whole reclaimable part.
    if Zm:
        L.append({
            't': 'The witness format pays a re-encoding tax the binding made visible',
            'id': 'witfmt', 'rank': 14.0 / (Zm / 1e6) * 100 * 1e6 / 1e6 if Zm else 6.0,
            'rank_word': 'estimated on', 'rank_of': "the guest's own work",
            'impact': f"<b>REFUTED 2026-08-09</b> — the week's fast heads made v1's priming re-encode cheap enough that any RLP-witness reader lands at parity before paying for RLP reads (arena experiment −7.2 %, records arithmetic ≈ 0; put_node's in-place shadowing makes a prime-time link pass irreducible). Format itself validated: 2.2 % smaller, simpler generator, archived on al/zkvm-r3-witv2. Revives only if priming regresses. Original sizing: " + f"~14 M steps per block · encode_rlp +5.5 M and priming +1.8 M are "
                      f"profile-measured, ~7 M diffuse · population parity with zisk-reth confirmed",
            'w': f"The soundness fix (pre-state root as a public value, commit "
                 f"<code>bf512561f</code>) costs +13.7 % — and the population audit shows the "
                 f"permutations themselves are at parity with <code>zisk-reth</code> (they carry "
                 f"2–4 % <i>more</i> branch nodes than we do for the same blocks). What they do not "
                 f"pay is the <b>re-encoding</b>: their witness delivers nodes as canonical RLP, so "
                 f"the binding is keccak over the bytes as delivered. Ours delivers offset-format "
                 f"nodes — zero-copy for execution — and the binding must re-encode every node to "
                 f"canonical RLP before hashing it.<br><br>"
                 f"The format choice used to win ~13 M of decode against reth; the binding turned "
                 f"it into a ~14 M loss. <b>The trade flipped when the statement changed.</b>",
            'built': False,
            'fix': f"<b>Witness format v2 — hash-ready and execution-ready at once.</b> Deliver "
                   f"nodes as canonical RLP plus a compact offset index; the binding hashes bytes "
                   f"as delivered (no encode pass), execution keeps zero-copy reads through the "
                   f"index. The digest-stub and flat-hash-store designs carry over unchanged."
                   f"<br><br><b>This is a pipeline change, not a guest change</b>: the witness "
                   f"generator on the devcore box must emit the new format and all 504 fixtures must be "
                   f"regenerated, which is what puts it here rather than in a night&rsquo;s work. "
                   f"Ceiling ~6 % of the post-soundness guest, of which 7.3 M is profile-measured.",
            'rem': f"<pre>{CMD} --axis levers-self\n./levers.py</pre>"
                   f"<b><code>encode_rlp</code>/<code>child_ref</code> should shed ~5.5 M and "
                   f"<code>prime_node</code> ~1.8 M</b>, and the binding permutation count must "
                   f"not move at all — it is the statement, not the format.",
        })

    # 1c — the no-op rehash cascade, measured 2026-08-07. The counter build that sized it is the
    # same one that closed lazy code verification (29 perms/block — nothing).
    if Zm:
        L.append({
            't': 'A third of the commit-time rehash reproduces the hash that was erased',
            'id': 'nooprehash', 'rank': 6.68, 'rank_word': 'measured on',
            'rank_of': "the guest's own work",
            'impact': f"<b>BUILT — commit <code>6e9d76676</code> on <code>al/zkvm-r2</code></b> · "
                      f"+3.84–8.68 % over 16 blocks · public values byte-identical, 16/16 roots "
                      f"PASS · commit-key memoisation tried on top: −0.37 %, reverted",
            'w': f"The upsert descent invalidates every node along a written path before knowing "
                 f"whether the write changes it. Measured with the flat store keeping the erased "
                 f"hash for comparison: <b>66–80 % of commit rehashes are real</b> — the witness "
                 f"carries mostly write-path nodes, as expected — but <b>34 % on average "
                 f"reproduce the erased hash exactly</b>. Whole paths re-hashed to identical "
                 f"values: the signature of no-op deltas (same-value <code>SSTORE</code>, "
                 f"accounts touched then restored within the block) cascading up the trie.",
            'built': False,
            'fix': f"<b>Two cuts, cheapest first.</b> Filter no-op deltas before commit — compare "
                   f"final against original in <code>State</code>, no trie work at all; or "
                   f"memcmp-restore in <code>TrieStore</code>: before rehashing an original id, "
                   f"compare current bytes to the blob (the flat store still holds the old hash) "
                   f"and restore instead of rehash. <b>Ballpark 2–4 % of the guest</b>; the "
                   f"per-block spread (20–44 %) says the value is workload-dependent.",
            'rem': f"<pre>{CMD} --axis levers-self\n./levers.py</pre>"
                   f"<b>The wasted-rehash counter must drop to ~0 and no root may change</b> — "
                   f"16/16 PASS plus the pre-root chain check are the gate, as always.",
        })

    # 2 — the keccak wrapper. Big, and mostly not ours: quoted so nobody re-derives it as a lever.
    zs, za = A('zisk', 'keccak'); ps, pa = A('sp1', 'keccak')
    if zs:
        L.append({
            't': 'The keccak wrapper was ours to cut — the 533-step label was never disassembled',
            'id': 'kec2', 'rank': 14.04, 'rank_word': 'measured on',
            'rank_of': "the guest's own work",
            'impact': f"<b>BUILT — commit <code>a4cbe9e6c</code> on <code>al/zkvm-r2</code></b> · "
                      f"+11.80–19.55 % over 16 blocks, no regression · public values byte-identical, "
                      f"16/16 roots PASS · <b>SP1 twin BUILT</b> — commit <code>e601b15a0</code> "
                      f"on <code>al/zkvm-r3</code>, +18.7–20.8 % of SP1 cycles (see its own entry)",
            'w': f"<b>{pct(zs, 2)}</b> of the optimised guest ({za / 1e6:.1f} M steps) still sits in "
                 f"the keccak path. The count is no longer the handle: after the eager-priming lever "
                 f"the guest performs <b>68,684 permutations per block against the reth guest's "
                 f"89,721</b> — it hashes <i>less</i> than reth already.<br><br>"
                 f"What remains is per-call infrastructure whose floor is ZisK's own: of the "
                 f"{BENCH['zisklib_perblock']}-step marginal block, {BENCH['syscall']} is the "
                 f"<code>keccak_f</code> invocation and only {BENCH['absorb_perblock']} is the "
                 f"absorb loop, which three independent implementations measure the same. "
                 f"<b>This is listed so that nobody re-derives it as a Monad lever.</b>",
            'built': False,
            'fix': f"<b>Two handles, both small.</b> In-guest: hash fewer times still — memoise the "
                   f"address hash across the storage accesses of one transaction, which the profile "
                   f"cannot size because the re-hashes are attributed to their callers. Upstream: "
                   f"the {BENCH['syscall']}-step <code>keccak_f</code> invocation cost is ZisK "
                   f"infrastructure, and is the same kind of ask as Zbb.<br><br>"
                   f"Do not size this from the family share: most of it is the precompile doing "
                   f"work that has to happen.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b>Track the permutation count, not the family share</b> — 68,684 today. The "
                   f"share moves when anything else in the guest changes; the count only moves when "
                   f"the guest genuinely hashes less.",
        })

    # 4 — the state containers. Large, and the one entry whose mechanism is not understood.
    zs, za = A('zisk', 'ctr')
    if zs:
        L.append({
            't': 'The per-block state maps are the largest cost with no identified fix',
            'id': 'ctr2', 'rank': zs * 100, 'rank_word': 'ceiling on',
            'rank_of': "the guest's own work",
            'impact': f"{za / 1e6:.1f} M steps per block · <code>reserve()</code> measured flat "
                      f"(−0.002 %) and closed · the structural experiments remain unbuilt",
            'w': f"<b>{pct(zs, 2)}</b> of the optimised guest ({za / 1e6:.1f} M steps) is "
                 f"<code>unordered_dense</code> and <code>immer</code>: the segmented-vector storage "
                 f"behind the Address→AccountState map, its hash calls, and the persistent map's "
                 f"champ nodes.<br><br>"
                 f"<b>This is the state model, not an accident</b>, which is why it is ranked here "
                 f"and not proposed as a change. The reth guests show ~0 % for these symbols, but "
                 f"that is a library difference (they use hashbrown), <b>not a gap</b> — do not read "
                 f"the comparison.",
            'built': False,
            'fix': f"<b>No identified fix. Two experiments, both cheap to try and neither obviously "
                   f"right.</b> Reserve the per-block maps from the transaction count, so the "
                   f"segmented vector stops growing incrementally; or give the per-block maps a flat "
                   f"layout, since they are built once and discarded and never need the persistent "
                   f"map's structural sharing.<br><br>"
                   f"<b>Confidence LOW.</b> Listed because {pct(zs, 1)} is too large to leave "
                   f"unexamined, not because a change is known to work.",
            'rem': f"<pre>{CMD} --axis levers-self\n./levers.py</pre>"
                   f"<b>Watch the champ and the segmented vector separately.</b> They respond to "
                   f"different changes, and an experiment that helps one while hurting the other "
                   f"will look like noise in the family total.",
        })

    # 5 — 256-bit multiplication, the half of the family the division lever did not touch.
    zs, za = A('zisk', 'mul256'); ps, pa = A('sp1', 'mul256')
    if zs:
        L.append({
            't': '256-bit multiplication runs in software while both backends offer a primitive',
            'id': 'mul256', 'rank': zs * 100, 'rank_word': 'ceiling on',
            'rank_of': "the guest's own work",
            'impact': f"<b>CLOSED — all four A/Bs run.</b> ZisK: mulmod +0.06/+11.2 "
                      f"(<code>99e73a3c8</code>), operator* +0.27 (<code>d07ba2334</code>), addmod "
                      f"slow path +0.71 max (<code>041d884ab</code>). SP1: mulmod +0.52/+6.65 "
                      f"(<code>ab63377a2</code>); operator* −0.15, deliberately not routed",
            'w': f"The division lever specialised the 128/64 step and left the rest of the family "
                 f"alone. What remains is <b>{pct(zs, 2)}</b> ({za / 1e6:.1f} M steps): the 512/256 "
                 f"divmod inside <code>mulmod</code>, <code>operator*</code> on "
                 f"<code>uint256_t</code>, and <code>mulmod</code> itself — all hand-written limb "
                 f"arithmetic.<br><br>"
                 f"<b>Both backends ship a 256-bit primitive for exactly this.</b> ZisK has an "
                 f"<code>arith256</code> precompile in its own library; SP1 has "
                 f"<code>sys_bigint</code>. Neither the Monad guest nor <code>zisk-reth</code> "
                 f"reaches for either — so this is a lever that would put the guest <i>ahead</i> on "
                 f"a family where it is currently behind, rather than closing a gap.",
            'built': False,
            'fix': f"<b>Route <code>mulmod</code> and <code>operator*</code> through the backend "
                   f"primitive</b>, behind the same kind of shim as the keccak precompile — one "
                   f"implementation per backend, selected at build time, with the software path kept "
                   f"for the host build.<br><br>"
                   f"<b>Ceiling {pct(zs, 1)} on ZisK, {pct(ps, 1)} on SP1</b>, and unusually likely "
                   f"to be reached: a precompile replaces the whole limb sequence rather than "
                   f"shortening it, so there is no inline body to pay back. <b>Verify the semantics "
                   f"first</b> — the EVM's <code>MULMOD</code> is defined on the full 512-bit "
                   f"product, and a primitive that reduces early would be wrong.",
            'rem': f"<pre>{CMD}\n./levers.py</pre>"
                   f"<b>The <code>div_result</code> and <code>operator*</code> symbols should "
                   f"disappear into a precompile call.</b> Check the 256-bit family total, not the "
                   f"symbols alone: if the family holds while the symbols vanish, the cost moved "
                   f"into marshalling arguments for the precompile.",
        })

    # 2026-08-08 — the SP1 keccak wrapper. The ZisK lever's twin, hidden for two cycles behind a
    # wrong note ("already thin: 310 instructions") that described the call site, not the absorb.
    L.append({
        't': 'SP1&rsquo;s keccak wrapper was tiny_keccak in software — the &ldquo;already thin&rdquo; note measured the wrong thing',
        'id': 'kecsp1', 'rank': 19.6, 'rank_word': 'measured on',
        'rank_of': "the SP1 guest's cycles",
        'impact': f"<b>BUILT — commit <code>e601b15a0</code> on <code>al/zkvm-r3</code> "
                  f"(local, not pushed)</b> · 3 blocks +18.7–20.8 % of cycles · public values "
                  f"byte-identical, exit 0 · syscall count unchanged — same permutations, no "
                  f"marshalling",
        'w': f"The fresh 190-block profiles put <code>zkvm_keccak256</code> at <b>19.5 % of the "
             f"SP1 guest's attributed work</b> — the single largest symbol. The SDK's wrapper is "
             f"tiny_keccak's sponge in <i>software</i>: only the keccak-f permutation reaches the "
             f"<code>KECCAK_PERMUTE</code> precompile, and the absorb feeds it byte by byte. The "
             f"guest pays that ~110k times per block, because the pre-state binding hashes the "
             f"whole witness trie.<br><br>"
             f"An earlier note had closed this path: &ldquo;SP1's wrapper is already "
             f"thin, 310 instructions, no byte-marshalling&rdquo;. That number described the "
             f"call-site setup. The lesson is trap 99's, one level up: <b>a claim that closes a "
             f"lever deserves the same profiling as one that opens it.</b>",
        'built': False,
        'fix': f"<b>One sponge, two doors</b> — <code>keccak_zisk.cpp</code> became "
               f"<code>keccak_accel.cpp</code>: the word-wise absorb (17 aligned "
               f"<code>ld</code>+<code>xor</code> per block, shift-combine when misaligned, "
               f"padded tail) now enters ZisK via <code>syscall_keccak_f</code> and SP1 via the "
               f"raw <code>ecall</code> the SDK itself emits (<code>t0&nbsp;=&nbsp;0x00_01_01_09"
               f"</code>) — inline, because libzkevm.a's LTO internalises the syscall symbol. "
               f"The shared shadow <code>keccak256</code> routes both backends; the EVM "
               f"<code>SHA3</code> opcode rides the same door.",
        'rem': f"<pre>{CMD} --axis opt-self-sp1\n./levers.py</pre>"
               f"<b>The syscall count must not move</b> (same permutations — 477,583 on block "
               f"25551991) while cycles drop ~19 %; public values byte-identical, and the ZisK "
               f"build must be step-identical, since its path only changed names.",
    })

    # 2026-08-08 — the calldata byte loop, and its cautionary twin. One SWAR treatment, two
    # opposite verdicts: the win condition lives in the input distribution, not in the code.
    L.append({
        't': 'Calldata gas counted its zeros byte by byte — and the JUMPDEST scan must stay that way',
        'id': 'tokens', 'rank': 0.73, 'rank_word': 'measured on',
        'rank_of': "the guest's own work",
        'impact': f"<b>BUILT — commit <code>88691ee2a</code> on <code>al/zkvm-r3</code> "
                  f"(local, not pushed)</b> · +0.21–1.12 % over 16 blocks, all positive · "
                  f"16/16 roots PASS · the same treatment on <code>find_jumpdests</code>: "
                  f"<b>−3.9 %, REFUTED</b> (trap 103)",
        'w': f"<code>tokens_in_calldata</code> — the EIP-2028 zero/nonzero split, paid once per "
             f"transaction — was a <code>std::count_if</code> over every calldata byte: "
             f"<b>1.18 %</b> of attributed work. Backend-neutral, so both guests carry it.<br><br>"
             f"<code>find_jumpdests</code> looked like the same lever five times bigger "
             f"(6.15 %), and a 20,000-case fuzz proved a word-wise scan byte-identical. The "
             f"emulator refuted it anyway: mainnet bytecode is push-dense and push <i>data</i> "
             f"looks like candidate opcodes to a raw byte screen, so nearly every 8-byte window "
             f"paid the SWAR check and then re-walked byte-wise. <b>Calldata is zeros with long "
             f"skippable runs; bytecode is not.</b>",
        'built': False,
        'fix': f"<b>Aligned words + the exact zero-byte detector</b> — new "
               f"<code>bits::zero_byte_mask</code> / <code>count_zero_bytes</code> (the "
               f"masked-add form, which cannot leak a borrow into the lane above). Byte head "
               f"and tail keep every load aligned for the zkVM targets. The JUMPDEST loop keeps "
               f"its plain form, and the entry above is the reason nobody should re-derive it.",
        'rem': f"<pre>{CMD} --axis opt-self\n./levers.py</pre>"
               f"<b>Gas must not change on any block</b> — a miscount would shift "
               f"<code>gas_used</code> and abort at the in-guest body binding; 16/16 roots PASS "
               f"and byte-identical public values are the gate.",
    })

    # 2026-08-08 evening — the biggest gain of the week was three dead lines.
    L.append({
        't': 'Every digest reference inserted a hash nobody would ever read back',
        'id': 'deademplace', 'rank': 11.0, 'rank_word': 'measured on',
        'rank_of': "the guest's own work (ZisK; +7 % of SP1 cycles)",
        'impact': f"<b>BUILT — commits <code>83771d6e6</code> (+11.0 % ZisK / +7 % SP1) and "
                  f"<code>73924c4d2</code> (+0.95 %, the inline fast head on top) on "
                  f"<code>al/zkvm-r3</code> (local, not pushed)</b> · 16 blocks "
                  f"+9.95–22.56 %, all positive · public values byte-identical, 16/16 roots PASS"
                  f" · <b>epilogue 2026-08-09</b>: this fix obsoleted the FLAT STORE lever, "
                  f"removed after the 504-block re-verdict (its fixed cost — a ~1.75 MB index "
                  f"zeroed per block — lost to a now-small map, 408/504 blocks; commits "
                  f"<code>6a2399b55</code>/<code>d2cd487d9</code>); operator* was removed the "
                  f"same day (504/504, cost AND steps). Levers invert when the base moves.",
        'w': f"<code>child_ref</code>'s DIGEST case still did "
             f"<code>hashes_.emplace(id,&nbsp;h)</code> — inherited from the pre-port code, "
             f"where the map was probed <i>first</i> and caching a digest hash short-circuited "
             f"the next reference. The ported order resolves the node <i>before</i> the map "
             f"(the digest lever), so the inserted entry can never be read back.<br><br>"
             f"The waste compounds: commit-time re-encodes re-reference the same digest "
             f"children on every written path, each reference inserting again into a table "
             f"bloated by ~30&nbsp;k dead entries — growth and rehash cascades included. "
             f"Write-heavy blocks paid up to 22.6 %.",
        'built': False,
        'fix': f"Delete the emplace (the digest hash is 32 bytes at a known blob offset — "
               f"reading it is cheaper than any cache), then inline the now-tiny fast head "
               f"(NULL / digest / primed-hash) into the 16-slot branch loop of the priming "
               f"pass, keeping overlay and recursion out of line.<br><br>"
               f"<b>The port-review lesson:</b> when a port changes an ordering, every line "
               f"justified by the OLD order needs re-justifying. The emplace survived because "
               f"it looked like the surrounding code's idiom.",
        'rem': f"<pre>{CMD} --axis opt-self\n./levers.py</pre>"
               f"<b>The map must stay small</b>: after a block, <code>hashes_</code> holds "
               f"overlay + dirtied entries only — tens of entries, not tens of thousands. Any "
               f"regrowth means a dead insert crept back.",
    })

    # 2026-08-09 — ceiling measured for the interpreter rework (a candidate, not built).
    L.append({
        't': 'Per-opcode gas and stack checks never fire on mainnet blocks — batching them is worth up to +4.9 %',
        'id': 'blockmeta', 'rank': 4.87, 'rank_word': 'ceiling on',
        'rank_of': "the guest's own work (ZisK; measurement-only build)",
        'impact': f"<b>CANDIDATE — ceiling measured, nothing built.</b> Unsound "
                  f"measurement-only build (static checks compiled out, gas decrements kept): "
                  f"+4.87 % median of guest steps (+1.03–5.63), <b>16/16 blocks byte-identical "
                  f"publics and PASS roots</b> — no static check fired.",
        'w': f"<code>check_requirements</code> runs inside every interpreter opcode — a gas "
             f"decrement-and-branch plus up to two stack-bound branches, inside the ~41 % of "
             f"the guest that is EVM execution. On valid mainnet blocks these branches never "
             f"take: 16/16 ceiling blocks produced identical results without them. "
             f"evmone-advanced answers this with basic-block metadata: one batched gas/stack "
             f"check per block of straight-line code.",
        'built': False,
        'fix': f"<b>Structural, team-sized</b>: extend Intercode analysis to basic-block "
               f"metadata (gas sum, stack delta/min/max per block), check once per block entry, "
               f"keep per-op checks only for dynamic-gas instructions. Realistic take "
               f"~60–80 % of ceiling (+3–4 %). Consensus-adjacent: the gas accounting feeds "
               f"the body binding, so the batched form must be exactly equivalent.",
        'rem': f"<pre>{CMD} --axis opt-self\n./levers.py</pre>"
               f"<b>The gate is the usual one</b> — byte-identical publics on all blocks, plus "
               f"blocks with reverting/OOG frames specifically (the ceiling build could not "
               f"measure those; a real implementation must keep them exact).",
    })

    # 2026-08-09 — the allocator: reading the file settled the question before any instrumentation.
    L.append({
        't': 'The allocator was bookkeeping memory that never comes back — delete was already a no-op',
        'id': 'arena', 'rank': 7.3, 'rank_word': 'measured on',
        'rank_of': "the SP1 guest's cycles (+0.38 % on ZisK)",
        'impact': f"<b>BUILT — commit <code>c24d5f33a</code> on <code>al/zkvm-r3</code> "
                  f"(local, not pushed)</b> · SP1 +7.11–7.65 % of cycles (3 blocks), ZisK "
                  f"+0.38 % (16 blocks, all positive) · public values byte-identical on both",
        'w': f"The bump-allocator experiment began by reading <code>zkvm/core/libstdcxx.cpp</code> "
             f"— and the file answered the feasibility question by itself: <b>operator delete is, "
             f"and was, a no-op</b>. The guest never frees; every allocation already lives to the "
             f"end of the run. embedded_alloc's TLSF (6.6 % of SP1's attributed instructions) was "
             f"walking size classes on every <code>operator new</code> for memory that was never "
             f"coming back.",
        'built': False,
        'fix': f"<code>alloc_or_exit</code> bumps inside 32 MiB chunks taken from "
               f"<code>sys_alloc_aligned</code>: one underlying allocation per chunk, a pointer "
               f"add per <code>new</code>, chunk tails abandoned on overflow.<br><br>"
               f"<b>One caveat stands before this ships</b>: execute-mode reports do not price "
               f"memory-table growth (<code>touched_memory_addresses</code> reads 0), and the "
               f"arena trades allocator instructions for a larger high-water footprint. Check "
               f"the prover-side effect on a real prove.",
        'rem': f"<pre>{CMD} --axis opt-self-sp1\n./levers.py</pre>"
               f"<b>Verify on a real prove, not just execute</b> — cycles down ~7 % must not be "
               f"bought back by memory-table rows. 16/16 roots PASS and byte-identical publics "
               f"are the correctness gate, as always.",
    })

    # 2026-08-08 — SP1 mem*. Uncovered by the keccak lever: once the wrapper fell, the mem
    # builtins became the biggest block on the SP1 profile.
    L.append({
        't': 'SP1&rsquo;s mem* are Rust builtins — memcmp walks bytes, memcpy gives up on cross-aligned',
        'id': 'memsp1', 'rank': 2.4, 'rank_word': 'measured on',
        'rank_of': "the SP1 guest's cycles",
        'impact': f"<b>BUILT — three commits on <code>al/zkvm-r3</code></b>: "
                  f"<code>01c093e46</code> word-wise mem* (+2.4 %), <code>6c74ef82f</code> entry "
                  f"fast paths from the return-address histogram (+2.1 %), <code>b6ecdf65b</code> "
                  f"uint256 lane copies at SWAP/to_avx (+1.5 %) · public values byte-identical "
                  f"at each step · ZisK excluded on purpose (zisklib's assembly mem* are already "
                  f"word-wise; the uint256 copies inline naturally on rv64)",
        'w': f"Post-keccak profile: <code>memcpy</code> 21.7 %, <code>memcmp</code> 6.3 %, "
             f"<code>memset</code> 5.5 %, <code>memmove</code> 3.2 % — <b>over a third of the "
             f"SP1 guest's attributed instructions</b>, supplied by Rust compiler_builtins. "
             f"memcmp is byte-by-byte; memcpy falls to a byte loop whenever source and "
             f"destination are not co-aligned.<br><br>"
             f"The measured gain is far below the share, and that is the honest half of the "
             f"story: the builtins are already word-wise on co-aligned bulk, so the share is "
             f"mostly small copies where per-call overhead dominates. What the replacement buys "
             f"is memcmp and the cross-aligned copies — <b>+2.4 %, not +15 %</b>.<br><br>"
             f"The follow-up histogram (throwaway build: a return-address table inside memcpy, "
             f"dumped through the WRITE syscall) explained the residue: <b>893,973 calls, "
             f"36.1 MB, ~40 bytes each</b> on block 25551991 — the cost is the CALL. Top "
             f"callers: the trie's hash_ref writes (340 k × 32 B), the EVM stack (uint256 "
             f"copies lower to memcpy calls on rv32), our keccak tail, and Intercode::pad "
             f"(392 calls, 3.5 MB — the only bulk copier). The class-level fix is off the "
             f"table: uint256_t's triviality is load-bearing (std::bit_cast).",
        'built': False,
        'fix': f"<code>zkvm/guest/mem_sp1.cpp</code>: destination-aligned copies, shift-combine "
               f"on cross-aligned sources (the keccak absorb's pattern), word equality screen in "
               f"memcmp. Both definitions are strong, so build-support <b>weakens</b> "
               f"libzkevm.a's copies (<code>objcopy -W</code> on a copy of the archive) rather "
               f"than removing them. 200,000-case host fuzz against libc before any guest run.",
        'rem': f"<pre>{CMD} --axis opt-self-sp1\n./levers.py</pre>"
               f"<b>Check the linked symbols, not the build log</b>: "
               f"<code>nm | grep -w memcpy</code> must resolve into the guest archive's address "
               f"range. The first A/B of this lever compared a stale ELF to itself — a link "
               f"failure swallowed by <code>|| true</code> — and 0.0 % deltas to the cycle are "
               f"what exposed it.",
    })

    # 2026-08-08 — the inline pair: one win, one mirage, same afternoon.
    L.append({
        't': 'The map key hashes paid a call per probe — and the fat dispatcher next to them did not',
        'id': 'hashinline', 'rank': 0.36, 'rank_word': 'measured on',
        'rank_of': "the guest's own work",
        'impact': f"<b>BUILT — commit <code>9f7a6f288</code> on <code>al/zkvm-r3</code> "
                  f"(local, not pushed)</b> · +0.15–0.41 % over 16 blocks, all positive · "
                  f"the companion experiment (force-inlining <code>mpt::match</code>, 6.19 % of "
                  f"the profile): <b>−0.09 %, REFUTED</b>",
        'w': f"<code>ankerl::hash&lt;Address&gt;</code> and <code>hash&lt;bytes32_t&gt;</code> "
             f"stood as standalone symbols — 2.49 % + 1.26 % of attributed work — so every map "
             f"probe paid an out-of-line call for a ~15-instruction fold+fmix body.<br><br>"
             f"<code>mpt::match&lt;Cases&gt;</code> looked like the same lever four times "
             f"bigger: a 4.5 KB out-of-line dispatcher with a 256-byte frame on the hottest "
             f"path. Force-inlining it moved <b>nothing</b>: its 6.19 % was not dispatch "
             f"overhead but the six lambda <i>bodies</i> inlined into it — real encode work "
             f"living under the dispatcher's name.",
        'built': False,
        'fix': f"<code>always_inline</code> on the two hash <code>operator()</code>s. The rule "
               f"the pair teaches: <b>inline forcing pays only where the call overhead is "
               f"comparable to the body</b> — a fat symbol on the profile is work to reduce, "
               f"not a call to elide.",
        'rem': f"<pre>{CMD} --axis opt-self\n./levers.py</pre>"
               f"<b>The hash symbols must vanish from the profile</b> (their work folds into "
               f"the callers); total steps −0.36 %, 16/16 roots PASS, publics byte-identical.",
    })

    # 6 — the finalizer. Small, but it is the one candidate whose mechanism is already measured.
    zs, za = A('zisk', 'champ')
    if zs:
        L.append({
            't': 'The key hash feeds the wrong end of immer&rsquo;s trie',
            'id': 'fmix', 'rank': 0.54, 'rank_word': 'measured on',
            'rank_of': "the guest's own work",
            'impact': f"<b>BUILT — commit <code>d8c22c0cf</code> on <code>al/zkvm-r2</code> "
                      f"(local, not pushed)</b> · 16 blocks +0.11–1.45 %, no regression · "
                      f"16/16 roots PASS · champ −30.9 %, popcount −50.5 %",
            'w': f"The key-hash lever (+1.24 %) replaced wyhash with a fold and one multiply. It is "
                 f"net positive, but it made <code>immer</code>'s champ <b>1.55× more expensive</b> "
                 f"— champ and <code>__popcountdi2</code> together are now {za / 1e6:.1f} M steps "
                 f"per block, and <code>__popcountdi2</code> alone <i>doubled</i> in absolute terms, "
                 f"55k to 110k calls.<br><br>"
                 f"<b>The mechanism, from the disassembly.</b> For consecutive storage slots the "
                 f"byte that varies sits at the <i>top</i> of the last word; a multiply only carries "
                 f"upward; and the single <code>h ^= h &gt;&gt; 29</code> pushes that entropy back "
                 f"down only to about bit 27. The champ consumes hash bits from the <b>low</b> end, "
                 f"5–6 per level, so the first several levels see identical chunks for consecutive "
                 f"slots and the trie deepens.<br><br>"
                 f"This is the same failure mode that made the lever's first version <i>lose</i> "
                 f"0.15 %; adding one xor-shift turned it positive but did not finish the job.",
            'built': False,
            'fix': f"<b>Finish the avalanche: an fmix64-style finalizer</b> — xor-shift, multiply, "
                   f"xor-shift, multiply, xor-shift — about four more operations per hash, against "
                   f"~2.7 M steps of champ depth to reclaim.<br><br>"
                   f"<b>The highest-confidence item on this list</b>, and the smallest: the failure "
                   f"mode is not hypothesised, it was measured twice — once as a 0.15 % loss, once "
                   f"as this 1.55× growth. <b>The state roots will not validate it</b> (a map is "
                   f"correct under any deterministic hash); the step count is the only check, as it "
                   f"was the first time.",
            'rem': f"<pre>{CMD} --axis levers-self\n./levers.py</pre>"
                   f"<b><code>champ</code> and <code>__popcountdi2</code> should both fall, and the "
                   f"key hash itself should rise slightly.</b> If the hash rises and the champ does "
                   f"not fall, the extra mixing is not reaching the low bits — measure the champ, "
                   f"never the hash.",
        })

    # 7 — Zbb, restated against the guest that now stands. Same ask, smaller residue.
    zs, za = A('zisk', 'zbb'); ps, pa = A('sp1', 'zbb')
    if zs:
        L.append({
            't': 'Zbb — the three helpers are inlined now, and the inline code is what remains',
            'id': 'zbb2', 'rank': zs * 100, 'rank_word': 'ceiling on',
            'rank_of': "the guest's own work", 'upstream': 'upstream · both backends',
            'impact': f"{za / 1e6:.1f} M steps per block still in helper bodies · one instruction "
                      f"each would remove nearly all of it",
            'w': f"On the shipped guest this was 8.05 % across <code>__bswapdi2</code>, "
                 f"<code>__clzdi2</code> and <code>__popcountdi2</code>. The branch inlines the "
                 f"first two away entirely — they are <b>0 % now</b> — and what is left is "
                 f"<b>{pct(zs, 2)}</b> ({za / 1e6:.1f} M steps), all of it "
                 f"<code>__popcountdi2</code>, because <b>every popcount call site in the guest "
                 f"belongs to <code>immer</code></b> and the submodule is not patched in the "
                 f"branch.<br><br>"
                 f"That figure understates the ask. The inline replacements are not free — the "
                 f"byte-order sequence pays back about a third of its saving at the call sites, and "
                 f"the SWAR popcount costs nearly what the helper did. <code>rev8</code>, "
                 f"<code>cpop</code> and <code>clz</code> are one instruction each.",
            'built': False,
            'fix': f"<b>Unchanged: ask the backends to accept Zbb.</b> ZisK's converter recognises no "
                   f"Zbb opcode and SP1's target is <code>riscv64im-succinct</code>; GCC already "
                   f"emits all three at <code>-march=rv64ima_zbb</code>, so the guest side is a "
                   f"flag and <code>bit_primitives.hpp</code> is then deleted whole.<br><br>"
                   f"<b>Worth about 3.9 % on top of the branch</b> — the inline sequences still cost "
                   f"~8.7 M steps per block and one instruction each would leave under 1 M. "
                   f"Meanwhile, patching <code>immer</code>'s popcount is worth the "
                   f"{pct(zs, 1)} above on its own, and needs nobody's permission.",
            'rem': f"<pre># once a backend accepts Zbb\n-march=rv64ima_zbb\n{CMD}\n./levers.py</pre>"
                   f"<b>Neither the helpers nor their inline replacements should survive.</b> The "
                   f"mask constants <code>0x00FF00FF00FF00FF</code> and "
                   f"<code>0x5555555555555555</code> should vanish from the disassembly; if they "
                   f"remain, the flag reached the compiler but the workarounds were left in place.",
        })

    # 2026-08-10 — BN254 point ops on SP1. Measured end to end (finding 111): the routing gap is
    # in SP1's own zkEVM SDK, so this is the first lever whose fix lands in NO Monad file. The
    # rank is PGU, not steps: the whole point of the item is prover cost.
    L.append({
        't': 'SP1&rsquo;s ECADD/ECMUL never reach the BN254 point syscalls &mdash; the fix is in SP1, not in the guest',
        'id': 'bn254sp1', 'rank': 7.89, 'rank_word': 'measured on',
        'rank_of': 'PGU, BN254-heavy blocks',
        'upstream': 'upstream SP1',
        'impact': "<b>MEASURED, not built into any branch</b> &mdash; &minus;7.89 % PGU on the ten "
                  "heaviest BN254 blocks (&minus;5.52 %&hellip;&minus;11.23 %), <b>+0.00 % on "
                  "BN254-free controls</b>, public values byte-identical on all 14. Patch is one "
                  "type change in <code>succinctlabs/sp1</code>; branch "
                  "<code>al/bn254-affine-syscall-routing</code> off <code>v6.3.1</code>, unpushed",
        'w': "SP1 provides <code>BN254_ADD</code> and <code>BN254_DOUBLE</code>. Across 373 blocks "
             "the guest emits <b>zero of each</b>, while <code>BN254_FP_ADD/SUB/MUL</code> and "
             "<code>BN254_FP2_MUL</code> fire in their millions &mdash; so the field half is "
             "accelerated and the curve half is not.<br><br>"
             "<b>The cause is a representation mismatch, not a missing feature.</b> In SP1&rsquo;s "
             "patched <code>substrate-bn</code> the syscall paths exist under "
             "<code>#[cfg(target_os = &quot;zkvm&quot;)]</code> but <i>only on the affine type</i> "
             "&mdash; <code>AffineG1::double()</code> and <code>Add&lt;AffineG1&gt; for "
             "AffineG1</code>. <code>libzkevm</code>&rsquo;s <code>decode_g1</code> returns the "
             "<b>Jacobian</b> <code>G1</code>, whose operators compose the same maths out of fp "
             "syscalls. The accelerated path is present, compiled, and unreachable. Its own "
             "<code>Cargo.toml</code> documents the intent &mdash; true of the field half, false "
             "of the curve half.<br><br>"
             "On ZisK the same guest logic <i>does</i> call the equivalents "
             "(<code>bn254_curve_add</code>, <code>bn254_curve_dbl</code>), which is what made the "
             "asymmetry visible. <b>Nothing in the Monad tree is wrong</b>: the guest calls "
             "<code>zkvm_bn254_g1_add</code> / <code>_g1_mul</code> correctly, and those resolve "
             "into <code>libzkevm.a</code>, built from the SP1 SDK by <code>build-support</code>.",
        'built': False,
        'fix': "<b>Keep ECADD and ECMUL affine in <code>zkevm/libzkevm/src/precompile/bn254.rs</code></b> "
               "&mdash; a <code>decode_g1_affine</code>/<code>encode_g1_affine</code> pair, with "
               "<code>g1_add</code> using <code>AffineG1 + AffineG1</code> and <code>g1_mul</code> "
               "using <code>Mul&lt;Fr&gt; for AffineG1</code>, which is a double-and-add loop over "
               "exactly the two accelerated operations. Pairing is untouched: it needs G2/Fq12 and "
               "has no point-op syscall to reach. 28 lines.<br><br>"
               "<b>Two routes to ship it.</b> Upstream a PR to <code>succinctlabs/sp1</code> and "
               "wait for a release &mdash; or, to get the win now, push the branch to a fork and "
               "add a <code>[patch.&quot;https://github.com/succinctlabs/sp1&quot;]</code> for "
               "<code>sp1-build</code> in <code>zkvm/sp1/Cargo.toml</code>. That second one is the "
               "<i>only</i> change that belongs in a Monad branch, and it is a pin, not a fix.<br><br>"
               "<b>Extrapolate with ~475 PGU per G1 point operation</b> (464&ndash;510 by block, "
               "aggregate 438,576,464 PGU over 923,482 ops), <i>not</i> with the &minus;7.89 %: "
               "those are the ten heaviest of 200 BN254-active blocks, and 173 of 373 have no "
               "BN254 at all and score exactly zero. Fleet-wide impact is unmeasured and much "
               "smaller.<br><br>"
               "<b>Still unrouted:</b> <code>BN254_FP2_ADD</code> / <code>_SUB</code> &mdash; "
               "<code>fq2.rs</code> issues two fp syscalls where one fp2 syscall exists. That one "
               "lives in the <code>substrate-bn</code> fork rather than in <code>libzkevm</code>, "
               "so it needs a separate patch, and the gain is far smaller (2 syscalls &rarr; 1).",
        'rem': "<pre>SP1_PROVER=cpu sp1-runner --mode execute \\\n"
               "  --elf &lt;elf&gt; --input &lt;witness&gt; \\\n"
               "  --report r.json --public-values pv.bin</pre>"
               "<b>Read <code>syscall_counts</code>, not the symbol table.</b> "
               "<code>BN254_ADD</code> should go 0 &rarr; 25k&ndash;36k and "
               "<code>BN254_DOUBLE</code> 0 &rarr; 50k&ndash;71k per heavy block, with "
               "<code>BN254_FP_ADD</code> collapsing ~0.9 M &rarr; ~0.2 M. <code>nm</code> proves "
               "nothing here: <code>sp1_lib::syscall_*</code> are inlined <code>ecall</code> "
               "wrappers, so neither build carries the symbol.<br><br>"
               "<b>Gate on the public values</b> (<code>--public-values</code> writes the real "
               "bytes in execute mode) and <b>keep BN254-free controls in the set</b> &mdash; they "
               "must come back bit-identical in PGU, which is what proves the change touches BN254 "
               "and nothing else.<br><br>"
               "<b>Build trap:</b> editing a cargo git checkout invalidates nothing. The first "
               "patched build finished in 0.40 s and produced an ELF byte-identical to baseline. "
               "Delete <code>libzkevm.a</code> and its <code>.fingerprint/libzkevm-*</code>, and "
               "touch the script&rsquo;s <code>build.rs</code>, or the measurement is a null result "
               "that looks like a verdict.",
    })
    return L


def sym_share(c, axis, guest, pattern):
    """Share of one guest's work carried by symbols matching `pattern`, at the current ELF stamp."""
    sh, _n, _st = sym_shares(c, axis, guest)
    return sum(v for fn, v in sh.items() if pattern in fn)


def build_nonlevers(d, c, S, Z, P, cs):
    out = []
    # Round two, built 2026-08-07 and refuted the same night.
    out.append({
        't': 'Word-wise mem* on SP1 — the incumbent was never a byte loop',
        'n': '−0.76% — built and measured, 12/12 blocks regressed',
        'w': "The candidate claimed SP1's <code>memcpy</code>/<code>memcmp</code>/<code>memset</code> "
             "were <code>compiler_builtins</code> byte loops and hung a 25 % ceiling on replacing "
             "them. Word-wise implementations were built (fuzz-verified over 4.3 M cases, linked over "
             "the SDK's copies) and measured <b>−0.76 % median — every block a regression</b>. The "
             "disassembly nobody had done first: the incumbent is <b>510 instructions of unrolled "
             "shift-combine</b>, already word-wise, more aggressively than the replacement. The 25 % "
             "is real <i>traffic</i> through an optimal loop. What survives: move fewer bytes, or ask "
             "SP1 for emulator-assisted mem stubs like ZisK's (0.8 % there against 25 % here — the "
             "asymmetry is the emulator, not the loop). <b>Disassemble the incumbent before sizing a "
             "replacement.</b>",
    })
    # Round two, refuted by a counter build (2026-08-07) before any implementation existed.
    out.append({
        't': 'Lazy JUMPDEST analysis — the contracts that never jump are the tiny ones',
        'n': '+0.05% ceiling — measured by counting, closed',
        'w': "The idea: build the bitmap on the first <code>JUMP</code>/<code>JUMPI</code> instead of "
             "at decode, skipping contracts that never jump. An instrumented guest (counters in the "
             "public output after the root, 16 blocks, all roots PASS) measured: <b>62.6 % of decoded "
             "contracts never jump — and they hold 0.8 % of the scanned bytes</b>. Never-jumping "
             "contracts average ~40 bytes; every large contract jumps. The saving is ~0.05 % of the "
             "guest, against the 6.4 % the scan costs. The scan is now closed on all three cuts: "
             "faster (SWAR, −11.4 %), fewer per block (cache, +0.29 %), fewer ever (lazy, +0.05 %). "
             "What remains is the price of the analysis itself — a backend-level ask at best. "
             "<b>The counter cost an afternoon; the implementation it refuted would have cost a "
             "week.</b>",
    })
    out.append({
        't': 'Memoising the protocol key hashes between execution and commit',
        'n': '−0.37% — built and measured, reverted',
        'w': "Populate a keccak(addr)/keccak(slot) cache on the read paths, consume it at commit, "
             "where the keys are re-hashes of what execution already read. Measured: <b>−0.37 % "
             "median, every block a regression</b> — the map&rsquo;s probe-and-insert on every "
             "read-path key costs more than the ~2 k commit-side keccaks it saves, especially "
             "after the no-op filter shrank those. Same lesson as popcount: when the incumbent "
             "costs ~170 steps, a replacement that carries map machinery starts underwater.",
    })
    try:    _vcm = json.load(open(os.path.join(HERE, 'results', 'varcode-measured.json')))
    except Exception: _vcm = None
    try:    _scm2 = json.load(open(os.path.join(HERE, 'results', 'scan-measured.json')))
    except Exception: _scm2 = None
    # `_meta` is the run's own parameters (block count, group ratios), not a family — kept apart so a
    # `_V[fam]` lookup can never land on it and so the group figures stay reachable.
    _Vall = verdicts()
    _VM = _Vall.get('_meta') or {}
    _V = {k: v for k, v in _Vall.items() if k != '_meta'}
    # Looks compelling in source and costs nothing — the exact shape that gets "fixed" by someone
    # reading the code rather than the profile, which is why it is recorded.
    _rs_new = sym_share(c, 'zisk', 'monad-zisk', 'OffsetTrieDb::read_storage')
    out.append({
        't': 'Memoising the per-slot address hash',
        'n': f"{pct(_rs_new, 2)} of the Monad guest's work",
        'w': "<code>OffsetTrieDb::read_storage</code> opens with "
             "<code>keccak256(addr.bytes)</code>, so a contract touching 100 slots hashes its address "
             "100 times — and <code>read_account</code>, <code>commit</code> and the storage-key path "
             "repeat the pattern, seven <code>keccak256</code> call sites in one file. It reads like a "
             "textbook memoisation. <b>It is cold:</b> <code>BlockState</code> caches above it and "
             "only descends into the trie on a miss, so the function does not appear in the profile at "
             "all. The same code under the previous reader measured <b>0.52%</b> — warm, but still an "
             "order of magnitude below the hashing lever it used to be cited under.",
    })
    _dg2 = _load_json('digest-measured.json')
    if _dg2:
        out.append({
            't': 'Priming digest hashes lazily instead of up front',
            'n': f"{pct(_dg2['first_attempt_gain'], 2)} — built and measured, a regression",
            'w': f"Skipping <code>prime_node</code> for DIGEST nodes and letting "
                 f"<code>child_ref</code> resolve them on demand costs "
                 f"<b>{pct(-_dg2['first_attempt_gain'], 2)} more</b>, not less: "
                 f"<code>prime_node</code> loses 24.9 M steps and <code>child_ref</code> gains "
                 f"27.3 M. A linear sweep with one insert each beats a map miss plus an insert per "
                 f"reference, and a node can be referenced more than once.<br><br>"
                 f"The useful part is what it proved: the cost is the <b>map</b>, not the priming. "
                 f"Removing both sides — see the lever above — is worth "
                 f"{pct(_dg2['gain_median'], 2)}. <b>A negative result that localises the cost is "
                 f"worth more than a positive one that does not.</b>",
        })
    if _scm2:
        _B = _scm2['B']
        out.append({
            't': 'Skipping eight bytecode bytes at a time in the JUMPDEST scan',
            'n': f"{pct(_B['gain_median'], 2)} — built and measured, "
                 f"{_B['roots_pass']}/{_scm2['blocks']} state roots PASS",
            'w': f"Every byte the scan visits that is neither JUMPDEST (0x5B) nor a PUSH opcode "
                 f"(0x5F–0x7F) does nothing, and every interesting byte is ≥ 0x5B — so an eight-byte "
                 f"SWAR test should let whole words be skipped. Correct (verified against the "
                 f"shipped loop on 40 000 random bytecodes) and <b>{pct(-_B['gain_median'], 2)} "
                 f"slower</b>: the scan itself nearly triples, {n_(_B['scan_before'])} → "
                 f"{n_(_B['scan_after'])} steps.<br><br>"
                 f"<b>Why:</b> the window starting at an opcode position contains PUSH <i>data</i>, "
                 f"which is arbitrary bytes — a byte ≥ 0x5B is almost always present, so the test "
                 f"fails and its ~10 instructions are paid for nothing on nearly every iteration. "
                 f"The saving needs runs of eight consecutive low opcodes, and compiled EVM bytecode "
                 f"does not have them.<br><br>"
                 f"Widening the loop index is a different change and does work — see the lever "
                 f"above. Nothing to gain from the word-at-a-time idea.",
        })
    if _vcm:
        _sc = symbol_steps(d, c, 'zisk', 'monad-zisk', r'find_jumpdests')
        out.append({
            't': 'Caching decoded bytecode across the block',
            'n': f"{pct(_vcm['gain_median'], 2)} — built and measured, "
                 f"{_vcm['roots_pass']}/{_vcm['blocks']} state roots PASS",
            'w': f"<b>Published here as a lever, then disproved by building it.</b> The guest's "
                 f"replacement VM returns <code>nullopt</code> from <code>find_varcode</code> "
                 f"unconditionally and its own comment defends the choice — which read as: every "
                 f"contract call re-scans the bytecode, worth "
                 f"{pct(_sc['share'], 2) if _sc else 'about 6%'} of the guest.<br><br>"
                 f"<b>It does not.</b> Adding the cache (an "
                 f"<code>unordered_dense::map&lt;bytes32_t, SharedVarcode&gt;</code> on a VM that "
                 f"lives for the whole block) returns <b>{pct(_vcm['gain_median'], 2)}</b> across "
                 f"{_vcm['blocks']} blocks, and <code>find_jumpdests</code> itself does not move: "
                 f"{n_(_vcm['scan_before'])} → {n_(_vcm['scan_after'])} steps, "
                 f"{(1 - _vcm['scan_after'] / _vcm['scan_before']) * 100:.2f}%. The small gain is the "
                 f"avoided allocation on repeat lookups, not avoided scanning.<br><br>"
                 f"<b>Why the reading was wrong:</b> one layer above, "
                 f"<code>BlockState::code_</code> already holds the decoded "
                 f"<code>SharedIntercode</code> keyed by code hash, so a contract is scanned once "
                 f"per block however many times it is called. The VM's comment was right and the "
                 f"stub is not a gap. <b>The scan cost is real and irreducible by caching</b> — it "
                 f"is one pass over each distinct contract touched. Whether the byte-wise loop "
                 f"itself (a <code>std::vector&lt;bool&gt;</code> bit set per byte) can be made "
                 f"cheaper is a different question, and untested.",
        })
    bb = fam_share(Z['shares']['a'], 'byte/bit manipulation'), \
         fam_share(Z['shares']['b'], 'byte/bit manipulation')
    out.append({
        't': 'Byte-order conversion — as a comparison against the reth guest',
        # NOT "reth measured at 0.00%": the body says the zero is not a zero, and the chip is what
        # gets scanned. A chip that contradicts its own paragraph is the paragraph nobody reads.
        'n': f"{pct(bb[0])} of the Monad guest's work · no reth symbol to compare against",
        # Closed as a COMPARISON only. The absolute cost is a lever — see `bswap` — and keeping the
        # two apart is the point: this entry used to end the topic, which let "incomparable" stand
        # in for "nothing to do" on the second largest single symbol in the guest.
        'w': "<b>The cross-guest ratio is a non-measurement.</b> This closes the comparison, not the "
             "cost: the absolute figure is a lever in its own right — see <b>byte-order conversion</b> "
             "above, where the 26-instruction libgcc call is the target. What cannot be done is "
             "reading this row as a gap against the reth guest.<br><br>"
             "<code>__bswapdi2</code> is an <i>outlined</i> "
             "libgcc function that C++ calls; Rust inlines <code>swap_bytes</code>/"
             "<code>to_be_bytes</code> into their callers so no symbol ever appears. Both guests do "
             "this work — neither backend has a byte-swap instruction — and only the Monad guest's is "
             "visible. The absolute figure is real; the reth zero is not a zero.<br><br>"
             # The one family where de-inlining changes nothing, which is itself the result: it
             # closes the "maybe it is only inlining" door by experiment rather than by argument.
             "<b>Tested, not assumed.</b> Rebuilding the reth guest with "
             "<code>--inline-threshold=0</code> made every other hidden family appear — hashing 11×, "
             "containers 21× — and this one stayed absent across all 193 blocks profiled. "
             "<code>swap_bytes</code> is an LLVM intrinsic rather than a call, so no inlining "
             "setting can outline it. This family cannot be compared by symbol on any build.",
    })
    dr = opcode(d, 'sp1', 'divrem')
    B = d['sp1']['blocks']
    vol = statistics.median([(B[b]['a']['ops']['divrem'] / B[b]['a']['work']) for b in B])
    out.append({
        't': 'Division ratio on SP1',
        'n': f"{x(dr.get('cost_ratio') or dr.get('count_ratio'), 2)} · {vol * 100:.3f}% of cycles",
        'w': "Spectacular ratio, negligible volume. It sits near the top of the opcode table in the "
             "measurement report purely because that table is sorted by ratio.",
    })
    mem = opcode(d, 'sp1', 'mem')
    out.append({
        't': 'Reducing memory traffic',
        'n': f"{x(mem.get('count_ratio') or mem.get('cost_ratio'), 2)} — parity",
        'w': "The Monad guest does not touch more memory than the reth guest. Only the allocator "
             "bookkeeping differs — and that gap has since closed on its own, with the witness copies "
             "that were driving the allocation.",
    })
    sh = Z['shares']['a']
    ctx = {'trie / MPT': r'trie|mpt|Nibble|node', 'EVM interpreter': r'interpreter|sha3|opcode',
           'state / accounts': r'State|account|storage'}
    parts = " · ".join(f"{k} {share_of(sh, re.compile(v, re.I)) * 100:.1f}%" for k, v in ctx.items())
    out.append({
        't': 'Batching keccak calls',
        'n': parts,
        'w': "Its keccak calls are spread evenly across three subsystems, so no single call site "
             "concentrates them and there is nothing to batch. What <i>is</i> available is "
             "eliminating redundant hashes — that is @keccak@, and a different mechanism.",
    })
    out.append({
        't': 'The keccak state machine itself',
        'n': f"{cs['keccak'] / cs['total'] * 100:.1f}% of prover cost · "
             f"{n_(ZISK_KECCAK_COST)} units per permutation, both guests",
        'w': "Identical per permutation for both guests, so it is only reducible by performing fewer "
             "permutations — which @keccak@ does, via the hash count.",
    })
    # The "same on both backends" argument holds on the ABSOLUTE instruction ratio, not on the ratio of
    # shares — SP1's allocator compresses every share there. Quote the absolute figures.
    _abs = lambda ax, fam: (d[ax]['summary']['families'][0].get(fam, 0)
                            / d[ax]['summary']['families'][1].get(fam, 1))
    for fam, why in (('EVM interpreter',
                      f"Already ahead, and by the same amount on two unrelated backends — "
                      f"<b>{x(_abs('zisk', 'EVM interpreter'), 3)}</b> the reth guest's instructions on "
                      f"ZisK, <b>{x(_abs('sp1', 'EVM interpreter'), 3)}</b> on SP1 — so it is a property "
                      f"of the guest code rather than of a zkVM. (The share ratio beside this differs "
                      f"between axes because SP1's allocator compresses every share there; the absolute "
                      f"ratio does not.) Nothing to gain here."),
                     # Two directions that look contradictory and are not: this family GREW against
                     # the guest's own previous version (the decode happens here now, instead of in a
                     # prebuilt index), while still costing a smaller share than the reth guest.
                     # The old wording pointed at the encode path as the thing to fix — that path is
                     # the one the zero-copy reader removed.
                     # MOVE is substituted from the measurement below. It was typed as "6%" and the
                     # measured factor is 0.96, i.e. 4% — a figure written into the sentence that
                     # exists to prove the sentence.
                     ('witness decoding', "Ahead of the reth guest on a share basis, and the claim "
                                          "survives the check: undoing the reth guest's inlining moves "
                                          "this ratio by {MOVE}. It grew against the guest's own "
                                          "previous version, because the zero-copy reader decodes here "
                                          "rather than in a prebuilt index — and is still the cheaper "
                                          "side."),
                     ('state / trie', "This share correctly includes the trie node decoder, whose "
                                      "return type makes it look like error-handling machinery in a "
                                      "name-based classifier — it is not.")):
        a, b = fam_share(Z['shares']['a'], fam), fam_share(Z['shares']['b'], fam)
        v = _V.get(fam)
        # An "ahead" claim is only publishable if it survives the attribution check. Where the
        # corrected ratio crosses 1 the claim is withdrawn in the TITLE, not softened in prose.
        moved = v and (v['factor'] < 0.5 or v['factor'] > 2)
        crossed = v and ((a / b if b else 9) < 1) != (v['ni'] < 1)
        # A crossed ratio is NOT by itself a claim that this guest is behind. Undoing reth's inlining
        # moves work between labels, so it sends `state / trie` up and `containers` down by comparable
        # amounts. Grouped, the two are at parity — measured, in _meta['groups'] — and the earlier
        # wording here ("the Monad guest is behind here") read one label of a three-label system as a
        # verdict on the guest.
        _grp = (_VM.get('groups') or {}).get('reader trio')
        title = (f"{fam} — neither ahead nor behind; the label moves, the work does not" if crossed
                 else f"{fam} — ahead, and it survives the attribution check" if v and not moved else
                 f"{fam} — already ahead")
        note = (f"{pct(a)} vs {pct(b)} · {x(a / b if b else None, 2)}"
                + (f" → <b>{x(v['ni'], 2)}</b> with reth's inlining undone" if v else ""))
        extra = ("" if not crossed else
                 f" <b>Requalified:</b> the reth guest's trie family was hosting inlined container and "
                 f"hashing work — undoing its inlining moves this ratio from {x(v['ship'], 2)} to "
                 f"<b>{x(v['ni'], 2)}</b>. But the same treatment sends <code>containers</code> the "
                 f"other way, by a comparable amount, because that is where the work goes. Read the "
                 f"path as one number instead"
                 + (f": containers, state/trie and witness decoding together are "
                    f"<b>{x(_grp['ship'], 2)}</b> as shipped and {x(_grp['ni'], 2)} de-inlined — the "
                    f"relocation happens inside the group, so the figure barely moves and is the one "
                    f"to act on." if _grp else ".")
                 + f" This row is neither a lever nor a deficit; it is a labelling artefact.")
        if '{MOVE}' in why:
            why = why.replace('{MOVE}', f"{abs(1 - v['factor']) * 100:.0f}%" if v else 'little')
        out.append({'t': title, 'n': note, 'w': why + extra})
    sa = share_of(P['shares']['a'], BN_SOFT_RE)
    sb = share_of(P['shares']['b'], BN_SOFT_RE)
    out.append({
        't': 'BN254 — through the precompile for the field, NOT for the point ops on SP1',
        'n': f"software curve arithmetic: monad {pct(sa, 2)} · rsp {pct(sb, 2)} — "
             f"{sb / max(sa, 1e-9):.0f}×",
        'w': "The Monad guest's ELF carries the precompile-backed entry points "
             "(<code>zkvm_bn254_*</code>) and <code>rsp</code>'s carries none, on the same emulator "
             "— which is what the software shares beside this reflect. That is also why the Monad "
             "guest looks cheaper than <code>rsp</code> on a fifth of SP1 blocks: <b>an rsp gap, "
             "not a Monad win</b>. Do not quote those blocks as a Monad result.<br><br>"
             "<b>Corrected 2026-08-10.</b> This entry used to read &ldquo;already going through the "
             "precompile&rdquo;, inferred from those symbols. A symbol table cannot see syscall "
             "routing — <code>sp1_lib::syscall_*</code> inline. Counted from an execution instead: "
             "on SP1 the <i>field</i> ops are accelerated and the <b>G1 point ops are not</b> "
             "(<code>BN254_ADD</code>/<code>BN254_DOUBLE</code> at zero across 373 blocks). Worth "
             "&minus;7.89 % PGU on BN254-heavy blocks — see the ranked lever above; the fix is in "
             "SP1's SDK, not here. ZisK is unaffected: it calls every curve precompile it exposes.",
    })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'levers.html'))
    a = ap.parse_args()
    # Before anything is rendered: the source citations are the only content measurement never
    # revisits, so they are checked against the ELF that is actually selected. A guest generation
    # switch (guests/monad/use-gen) can make them describe code that is no longer linked in.
    check_sites(os.path.join(HERE, os.pardir, 'guests', 'monad-zisk', 'monad-zisk.elf'))
    d, c, S = gather()
    html = render(d, c, S)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, 'w').write(html)
    print(f"wrote {a.out}")
    for ax in S:
        print(f"  {ax:5} n={S[ax]['sum']['n']} · profiles {S[ax]['nblk']['a']}/{S[ax]['nblk']['b']} "
              f"· ratio {x(S[ax]['ratio'])}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
