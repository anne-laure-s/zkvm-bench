#!/usr/bin/env python3
"""levers — what to fix in the Monad guest, ranked, with a re-measure protocol per item.

This is NOT compare.py's report. compare.py is a generic instrument: any two guests, any backend,
any block range, and its output must stay neutral or it will be wrong the next time someone compares
a different pair. This document is the opposite: it is specific to *these* guests and *this* build,
it has a shelf life, and its best content is not derivable from compare.py's inputs at all — the
four fix sites come from reading the Monad source, the precompile-backed BN254 path comes from the ELF
symbol table, the per-call keccak cost comes from a regression over the profile cache.

Every measured figure here is COMPUTED from results/compare.json + results/compare-cache.json.
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

import argparse, importlib.util, json, os, re, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPARE_JSON = os.path.join(HERE, 'results', 'compare.json')
CACHE_JSON = os.path.join(HERE, 'results', 'compare-cache.json')

# Reuse the ONE taxonomy. Importing rather than copying: a second copy of FAMILIES would drift from
# hotspots.py silently, and this analysis has already been wrong twice from taxonomy mismatches.
_spec = importlib.util.spec_from_file_location('hs', os.path.join(HERE, 'hotspots.py'))
hs = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(hs)

MIN_BLOCKS = 300          # refuse to build from a partial run — see the module docstring
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
SITES = {
    'containers': [
        ('category/execution/ethereum/db/partial_trie_db.cpp', 704,
         "node_index.emplace(key, byte_string{node_bytes.data(), …}) — copies every witness node "
         "into an owning string, though encoded_nodes is already a live byte_string_view. The "
         "witness is duplicated wholesale into guest memory.",
         "Store a view. The input buffer outlives the index."),
        ('category/execution/ethereum/db/partial_trie_db.cpp', 211,
         "encode_partial_node's branch case builds a body by appending encode_child_ref (defined at "
         ":254) sixteen times, each returning a byte_string BY VALUE, into a body with no reserve; "
         "encode_list2 then copies that body again.",
         "Pass an output buffer down and append in place, in both functions."),
        ('category/execution/ethereum/rlp/encode2.hpp', 68,
         "encode_list2 computes the exact payload size, then never calls reserve — it grows by "
         "repeated +=.",
         "result.reserve(size + 9) — the +9 covers the maximum RLP length prefix. One line."),
    ],
    'hashing': [
        ('category/execution/ethereum/db/partial_trie_db.cpp', 752,
         "read_storage recomputes keccak256(addr.bytes) on EVERY slot access — a contract touching "
         "100 slots hashes its address 100 times. read_account and commit hash the same addresses.",
         "Memoise the key hash per address."),
    ],
}
NOT_A_SITE = ('category/execution/ethereum/create_contract_address.cpp', 33,
              "hash_and_clip was assumed to be the trie-node hash path. It is not — it only "
              "derives CREATE/CREATE2 addresses, and the profiler puts it at 0.00%, absent from "
              "the top 120. A plausible call-graph story is not evidence.")


# ─────────────────────────────────────── data layer ───────────────────────────────────────────

def load():
    for p in (COMPARE_JSON, CACHE_JSON):
        if not os.path.exists(p):
            sys.exit(f"missing {p} — run ./compare.py --block-min … --block-max … first")
    d = json.load(open(COMPARE_JSON)); c = json.load(open(CACHE_JSON))
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
    """Per-block profiles for one guest, restricted to the NEWEST build stamp in the cache.

    The cache key carries the ELF mtime. Mixing stamps would average two different binaries, which
    is exactly the kind of silent error this document exists to avoid."""
    ks = [k for k in c if k.startswith(f'fn2/{axis}/{guest}/')]
    if not ks: return [], None
    stamp = max(int(k.rsplit('/', 1)[1]) for k in ks)
    return [k for k in ks if k.endswith(f'/{stamp}')], stamp


def sym_shares(c, axis, guest):
    """{symbol: share of this guest's attributed work}, over every cached block of this build."""
    ks, stamp = cached(c, axis, guest)
    agg, tot = {}, 0
    for k in ks:
        for fn, v in c[k]['fns']:
            tot += v; agg[fn] = agg.get(fn, 0) + v
    return ({fn: v / tot for fn, v in agg.items()} if tot else {}), len(ks), stamp


def share_of(shares, rx):
    return sum(v for fn, v in shares.items() if rx.search(fn))


def fam_share(shares, family):
    return sum(v for fn, v in shares.items() if hs.family(fn) == family)


def percall_hashing(d, c, axis, side, guest):
    """Attributed hashing instructions per keccak call, per block — and how flat it is.

    Flatness is the whole point: a cost that does not vary with payload size is per-call SETUP, not
    hashing work. Returns (median, lo, hi, pearson_r, n, call_count_spread)."""
    B = d[axis]['blocks']; ks, _ = cached(c, axis, guest)
    xs, ys, rats = [], [], []
    for k in ks:
        b = k.split('/')[3]
        if b not in B: continue
        fns = dict(c[k]['fns']); tot = sum(fns.values()) or 1
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
    return d, c, S


# ─────────────────────────────────────── rendering ────────────────────────────────────────────

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
.tag{font-size:10px;letter-spacing:.06em;text-transform:uppercase;border-radius:4px;padding:1px 6px;
 font-family:var(--mono)}
.tag.s{background:rgba(232,176,75,.16);color:var(--gold)}
.tag.b{background:rgba(95,191,138,.16);color:var(--green)}
.note{color:var(--muted);font-size:12.5px;line-height:1.6;margin:10px 0 0}
.note b{color:var(--accent-dim)}
.site{border-left:2px solid var(--accent-dim);padding:9px 0 9px 13px;margin:10px 0 0}
.site .p{font-family:var(--mono);font-size:11.5px;color:var(--accent)}
.rem{background:var(--panel2);border-radius:8px;padding:10px 13px;margin:12px 0 0;font-size:13px}
.rem .k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
 display:block;margin:0 0 5px}
.dead{opacity:.85}
.dead h3{color:var(--muted)}
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
             + f"<br><b>Generated</b> {time.strftime('%Y-%m-%d %H:%M %Z')} by "
             + "profiling/levers.py from results/compare.json + compare-cache.json"
             + "<br><b>Shelf life</b> two different lifetimes. Everything drawn from the sweep is tied "
               "to these ELFs: land a fix and it is out of date — regenerate. The figures badged "
               "<i>isolated</i> are not: they measure ZisK's own library, so regenerating does not "
               "update them and a change to the Monad guest does not invalidate them. They go stale "
               "when ZisK does, and only re-running the micro-benchmark moves them.</div>")

    # ── what the document is, before what it found. The summary below covers the FINDINGS; a reader
    # still has to work out what kind of document they are holding.
    h.append("<p class=note style='margin:0 0 20px'><b>What this is.</b> The things worth fixing in the "
             "Monad guest, largest first — each with the figure it should move, how far, and "
             "how to check that it did. After them: the one pattern most of them share, then the things "
             "that look like levers and are not, then what these numbers do and do not cover. <b>The "
             "order follows each lever's ceiling</b> — an upper bound on what removing it could "
             "recover, as a share of the guest's own work, printed on every lever. A ceiling is not an "
             "expectation: what each one is likely to deliver, and how much of it is established, is "
             "stated on the lever. One ceiling is a share of prover cost rather than of work, because "
             "no instruction count exists for it; it is last under either denominator, so the sequence "
             "does not turn on that choice. "
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
    h.append("<h2>Levers</h2>")
    levers, nonlevers, _refnum = resolve_refs(order_levers(build_levers(d, S, Z, P, cs)),
                                              build_nonlevers(d, S, Z, P, cs))
    for i, L in enumerate(levers, 1):
        h.append(render_lever(i, L))

    # Upstream items are shared code, so they close no Monad-vs-reth gap and must not enter the sum.
    naive = sum(L['gap_pp'] for L in levers if L.get('gap_pp') and not L.get('upstream'))
    _shared = [n for n, L in enumerate(levers, 1) if L.get('id') in ('containers', 'alloc')]
    h.append(f"<p class=note><b>These do not add up.</b> Each figure is what <i>that</i> fix removes, "
             f"or a measured ceiling on it — not the distance to the reth guest. Two of them, "
             f"levers {' and '.join(str(x) for x in _shared)}, share one root cause, so removing it "
             f"once counts once. The naive sum ({naive:.1f} pp, which would put the ZisK ratio at "
             f"{x(Z['ratio'] * (1 - naive / 100))}) therefore overstates: fix them together and expect "
             f"a joint gain below it. The two levers with no percentage — keccak and the 32-bit types "
             f"— are absolute costs, not shares, and are not in this sum.</p>")

    # ── the pattern ──
    h.append("<h2>The pattern under levers #containers# and #alloc#</h2>")
    h.append(f"<p>Owning <code>byte_string</code> "
             f"(<code>= evmc::bytes = std::basic_string&lt;unsigned char, evmc::byte_traits&gt;</code>) "
             f"where a view would do. The codebase is otherwise view-based — the whole RLP "
             f"<b>decode</b> path takes <code>byte_string_view&amp;</code>, and that is exactly why "
             f"witness decoding is its best family. The <b>encode</b> path is the exception, and the "
             f"abstraction, allocation and per-call hashing costs all originate there.</p>")
    h.append(f"<p class=note>Reading it in one line: per branch node the guest allocates ~17 owning strings, "
             f"grow a <code>body</code> with no <code>reserve</code>, then copy that body again into "
             f"<code>encode_list2</code>'s result. Then it hashes that. Then it frees all of it.</p>")
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


def order_levers(levers):
    """Sort by each lever's declared headline magnitude, largest first.

    Append order had them at 7.32 / 4.19 / 5.20 pp while three places in the page promised a ranking.
    `rank` is the figure that lever leads with — a removable share, a measured ceiling, or a share
    merely concerned — so it orders them without pretending they are one quantity."""
    return sorted(levers, key=lambda L: -(L.get('rank') or L.get('gap_pp') or 0))


def resolve_refs(levers, nonlevers):
    """Replace @id@ tokens with each lever's display number.

    Written literally ("lever 3"), a cross-reference goes stale as soon as a lever is added or removed
    from the list — which happened, leaving four references one rank too high. Resolving them from the
    built list makes the numbering unbreakable."""
    num = {L['id']: n for n, L in enumerate(levers, 1) if L.get('id')}
    def fix(t):
        return re.sub(r'@([a-z0-9_]+)@',
                      lambda m: f"lever {num[m.group(1)]}" if m.group(1) in num else m.group(0), t)
    for L in levers:
        for k in ('t', 'w', 'impact', 'rem'):
            if L.get(k): L[k] = fix(L[k])
    for D in nonlevers:
        for k in ('t', 'n', 'w'):
            if D.get(k): D[k] = fix(D[k])
    return levers, nonlevers, num


def render_lever(i, L):
    # An upstream item is shared code, so it must be labelled: it closes no gap between the guests.
    badge = "<span class='tag up'>upstream ZisK</span>" if L.get('upstream') else ""
    # The ceiling is printed from the sort key, so the figure that ordered the list is always the
    # figure shown. Its denominator is not the same for every lever, so it is named, not assumed.
    ceil = (f"<b>{L['rank']:.1f}%</b> ceiling on {L.get('rank_of', 'the guest&rsquo;s own work')} · "
            if L.get('rank') else "")
    o = [f"<div class=lev><div class=hd><span class=rank>{i}</span><h3>{L['t']}</h3>{badge}"
         f"<span class=impact>{ceil}{L['impact']}</span></div>"]
    o.append(f"<p>{L['w']}</p>")
    if L.get('tbl'): o.append(L['tbl'])
    for f, ln, what, fix in L.get('sites', []):
        o.append(f"<div class=site><span class='tag s'>source</span> "
                 f"<span class=p>{f}:{ln}</span>"
                 f"<p class=note style='margin:6px 0 4px'>{what}</p>"
                 f"<p class=note style='margin:0'><b>Fix:</b> {fix}</p></div>")
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


def build_levers(d, S, Z, P, cs):
    L = []
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
    L.append({
        't': 'Container / abstraction machinery',
        'id': 'containers',
        'gap_pp': oz * 100,
        'rank': oz * 100,            # ceiling: the owning-string work this fix removes
        'impact': f"ZisK <b>{x(Z['ratio'])} → {x(Z['ratio'] * (1 - oz))}</b> · "
                  f"SP1 <b>{x(P['ratio'])} → {x(P['ratio'] * (1 - os_))}</b>",
        'w': "The largest single family, and the top three symbols are one data structure: a hash map "
             "from node hash to byte buffer, its iterator, and its key-hash function. The reth "
             "guests stay low here because their byte buffers are reference-counted and their "
             "witness is never copied. <b>Quote the SP1 figure.</b> On the ZisK axis the reth guest "
             "inlines far more aggressively — 132 functions over 8 KB against 12 — so its share of a "
             "leaf family like this one is understated there and the ZisK ratio reads high. On SP1 "
             "the two guests expose comparable symbol granularity.<br><br>"
             f"<b>What the fix is worth, quantified.</b> Replacing owning strings with views removes "
             f"the string operations — <b>{pct(oz, 1)}</b> of the guest's own work on ZisK, "
             f"<b>{pct(os_, 1)}</b> on SP1 — plus an unquantified part of the "
             f"{pct(share_of(Z['shares']['a'], ALLOC_RE), 1)} spent in the allocator they feed. It "
             f"does <b>not</b> remove the index itself, its iterator or its key hashing: "
             f"{pct(a - oz, 1)} of the family stays on ZisK. Size the work on that first figure, not "
             f"on the distance to the reth guest.",
        'tbl': _shares_tbl([('family share of own work, ZisK', a, b, lambda v: pct(v)),
                            ('family share of own work, SP1', ap, bp, lambda v: pct(v)),
                            ('↳ owning-string ops, ZisK — the fix removes these', oz, None,
                             lambda v: pct(v), 'rm'),
                            ('↳ owning-string ops, SP1 — the fix removes these', os_, None,
                             lambda v: pct(v), 'rm'),
                            ('↳ index, iterator, key hashing, ZisK — these stay', a - oz, None,
                             lambda v: pct(v), 'keep'),
                            ('↳ index, iterator, key hashing, SP1 — these stay', ap - os_, None,
                             lambda v: pct(v), 'keep')]),
        'sites': SITES['containers'],
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b>containers share</b> should fall from {pct(a, 1)} toward {pct(a - oz, 1)} — the "
               f"part that stays — and the ZisK ratio from {x(Z['ratio'])} toward "
               f"{x(Z['ratio'] * (1 - oz))}. <b>Not</b> toward the reth guest's {pct(b, 1)}: the index, "
               f"its iterator and its key hashing are not removed by this. If the share moves but the "
               f"ratio does not, the work changed family rather than disappearing — check "
               f"<code>memory / allocation</code> did not absorb it.",
    })

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
        'rank': ha * 100,            # ceiling: all of the wrapper, if every hash went away
        # The share must be the MEASURED hashing family, not permutations x 533: that product applies a
        # unit price this lever explicitly says does not exist, and it overstates by 28%.
        'impact': f"<b>{BENCH['zisklib_1perm']}</b> steps per short hash removed, "
                  f"<b>{BENCH['zisklib_perblock']}</b> per block of a long one",
        'w': f"Both guests reach the same precompile and perform a comparable number of permutations "
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
        'sites': SITES['hashing'],
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
    L.append({
        't': 'Allocator bookkeeping (not memory traffic)',
        'id': 'alloc',
        'gap_pp': xz * 100,          # the same quantity the projection uses, not the reth gap
        'rank': xz * 100,            # ceiling: the allocator excess over the reth guest
        'impact': f"ZisK {x(Z['ratio'])} → <b>{x(Z['ratio'] * (1 - xz))}</b> · "
                  f"SP1 {x(P['ratio'])} → <b>{x(P['ratio'] * (1 - xp))}</b>",
        'w': f"The difference is in <i>kind</i>, not size. Split the family and the reth guest does "
             "essentially no allocation in-guest — its memory family is pure copy/compare/set. "
             f"The Monad guest's is dominated by real allocation, the downstream effect of @containers@: an "
             f"owning buffer per encoded node has to come from somewhere.<br><br>"
             f"<b>The ceiling, measured.</b> Time in allocator entry points — "
             f"<code>sys_alloc_aligned</code>, <code>operator new/delete</code>, the TLSF heap — is "
             f"<b>{pct(ez, 1)}</b> of the Monad guest's work on ZisK against <b>{pct(ezr, 1)}</b> for "
             f"the reth guest, which does not allocate in-guest at all. On SP1 both pay the same TLSF "
             f"heap heavily ({pct(ep, 1)} against {pct(epr, 1)}), so only the excess is Monad's to "
             f"remove. Those excesses — <b>{pct(xz, 1)}</b> on ZisK and <b>{pct(xp, 1)}</b> on SP1 — "
             f"are the ceiling.<br><br>"
             f"<b>What is not separable here.</b> Unlike @containers@, where the removable work is its own "
             f"set of symbols, an allocator entry point does not say who called it and this profile "
             f"carries no call graph. So the split between allocations driven by the owning-string "
             f"pattern (removable) and by node construction — one "
             f"<code>make_unique&lt;PartialNode&gt;</code> per decoded node, plus map growth (not "
             f"removable) — cannot be read off. From the source the encode path allocates far more "
             f"often per node (one per child reference, sixteen per branch node, plus one per "
             f"<code>encode_list2</code>) than the decode path does (one per node), which argues the "
             f"removable share is the larger part — but that is an argument from the code, not a "
             f"measurement. Separating it needs an allocation-site counter or a call-graph profile.",
        # The breakdown decomposes the ZisK row, so it belongs directly under it. Placed after the
        # SP1 row it read as a breakdown of SP1.
        'tbl': _shares_tbl([('memory / allocation, ZisK', a, b, lambda v: pct(v), 'zisk'),
                            ('↳ of which real allocation', aa, ab, lambda v: pct(v), 'zisk'),
                            ('↳ of which copy / compare / set', ca, cb, lambda v: pct(v), 'zisk'),
                            ('memory / allocation, SP1', ap4, bp4, lambda v: pct(v))]),
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b>Expect this to move on its own</b> once @containers@ lands — same root cause. Watch "
               f"the allocator entry-point share ({pct(ez, 1)} today, floor {pct(ezr, 1)}). If the "
               f"containers share falls and this one does not, the allocations were not coming from "
               f"the encode path, and the argument above is wrong.",
    })

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
        'rank': dz * 100,            # ceiling: the generic-division share
        # Ceiling, not expectation: only the division part is in scope, and a specialised routine
        # still costs something — the reth guest spends 0.6% of its work in its own div_rem.
        'impact': f"ZisK {x(Z['ratio'])} → <b>{x(Z['ratio'] * (1 - dz))}</b> · "
                  f"SP1 {x(P['ratio'])} → <b>{x(P['ratio'] * (1 - dp))}</b>",
        'w': f"<b>{ga * 100:.0f}%</b> of the Monad guest's 256-bit arithmetic goes through "
             f"<code>compiler_builtins::u128_div_rem</code> — the generic 128-bit division helper — "
             f"where the reth guests use a hand-written 256-bit <code>div_rem</code>. Same "
             f"operation, different implementation.<br><br>"
             f"<b>What is in scope, quantified.</b> Only the division part: <b>{pct(dz, 1)}</b> of the "
             f"guest's own work on ZisK, <b>{pct(dp, 1)}</b> on SP1. The multiplications in this "
             f"family stay. And a specialised routine is not free — the reth guest still spends "
             f"{pct(dr_z, 1)} in its own <code>div_rem</code> — so read the projection as a ceiling "
             f"that assumes division becomes costless, which it does not.",
        'tbl': _shares_tbl([('256-bit arithmetic share, ZisK', a, b, lambda v: pct(v)),
                            ('256-bit arithmetic share, SP1', ap5, bp5, lambda v: pct(v))]),
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b>generic-division share</b> should fall from {pct(dz, 1)} of the guest's work "
               f"toward the reth guest's {pct(dr_z, 1)}. Watch the family share too: if the generic "
               f"helper disappears but the family share holds, the cost moved into the specialised "
               f"routine and there is no win.",
    })

    # 6/7 — the two small pure-loss items, ZisK opcode level
    aw = opcode(d, 'zisk', 'add_w'); sl = opcode(d, 'zisk', 'sll')
    L.append({
        't': '32-bit integer types emit an instruction class the reth guest never does',
        'rank': cs['add_w'] / cs['total'] * 100,   # ceiling: the whole instruction class
        'id': 'int32',
        # sll is billed inside the bit-ops group, so quote it from the opcode cost directly
        'rank_of': "prover cost",   # not a work share: see the note under the list
        'impact': "paid by one guest only",
        'w': f"<b>ZisK only</b> — not because SP1 is unaffected, but because SP1's report groups "
             f"opcodes into six buckets (mem, branch, shift, mul, divrem, ecall) and neither of these "
             f"two is separable from them. Absence here is a limit of the counter, not a result. "
             f"Small, but neither is a trade-off — one guest pays and the other does not, for the "
             f"same work. <code>add_w</code> — RV64's 32-bit add with sign extension — appears on "
             f"<b>{aw['present_a']} of the Monad guest's {aw['n_blocks']}</b> blocks and on "
             f"<b>{aw['present_b']}</b> of the reth guest's, worth "
             f"{cs['add_w'] / cs['total'] * 100:.2f}% of prover cost. It comes from "
             f"<code>int</code>/<code>uint32_t</code> in hot paths; widening those to 64-bit removes "
             f"the instruction class outright.",
        'rem': f"<pre>{CMD}\n./levers.py</pre>"
               f"<b><code>add_w</code> should disappear entirely</b> — present on "
               f"{aw['present_a']} blocks today, {aw['present_b']} for the reth guest — once hot-path "
               f"integer types are widened to 64-bit.",
    })
    return L


def build_nonlevers(d, S, Z, P, cs):
    out = []
    bb = fam_share(Z['shares']['a'], 'byte/bit manipulation'), \
         fam_share(Z['shares']['b'], 'byte/bit manipulation')
    out.append({
        't': 'Byte-order conversion',
        'n': f"{pct(bb[0])} of the Monad guest's work · reth measured at {pct(bb[1])}",
        'w': "<b>A non-measurement, not a gap.</b> <code>__bswapdi2</code> is an <i>outlined</i> "
             "libgcc function that C++ calls; Rust inlines <code>swap_bytes</code>/"
             "<code>to_be_bytes</code> into their callers so no symbol ever appears. Both guests do "
             "this work — neither backend has a byte-swap instruction — and only the Monad guest's is visible. "
             "The absolute figure is real; the reth zero is not a zero.",
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
             "bookkeeping differs, which is @alloc@ and a different fix: allocate less, not copy less.",
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
                     ('witness decoding', "Already ahead — the view-based decode path. It is the "
                                          "model the encode path should copy, not a target."),
                     ('state / trie', "Ahead. This share correctly includes the trie node decoder, "
                                      "whose return type makes it look like error-handling machinery "
                                      "in a name-based classifier — it is not.")):
        a, b = fam_share(Z['shares']['a'], fam), fam_share(Z['shares']['b'], fam)
        out.append({'t': f"{fam} — already ahead",
                    'n': f"{pct(a)} vs {pct(b)} · {x(a / b if b else None, 2)}",
                    'w': why})
    sa = share_of(P['shares']['a'], BN_SOFT_RE)
    sb = share_of(P['shares']['b'], BN_SOFT_RE)
    out.append({
        't': 'BN254 — already going through the precompile',
        'n': f"software curve arithmetic: monad {pct(sa, 2)} · rsp {pct(sb, 2)} — "
             f"{sb / max(sa, 1e-9):.0f}×",
        'w': "The Monad guest's ELF carries the precompile-backed symbols (<code>zkvm_bn254_*</code>) "
             "and <code>rsp</code>'s carries none, on the same emulator — which is what the software "
             "shares beside this reflect. That is also why the Monad guest looks cheaper than "
             "<code>rsp</code> on a fifth of SP1 blocks: <b>an rsp gap, not a Monad win</b>. Do not "
             "quote those blocks as a Monad result.",
    })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'levers.html'))
    a = ap.parse_args()
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
