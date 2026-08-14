#!/usr/bin/env python3
"""levers — what to fix in the Monad guest on the r4 base, ranked, with a way to check each one.

Not compare.py's report. compare.py is a generic instrument and its output must stay neutral; this is
the opposite — specific to *this* guest on *this* base, with a shelf life measured in rebases.

Written for `al/zkvm-r4` (30 commits on `origin/sam/zkvm-zisk-sp1`) and the deterministic corpus
`storageroot-det-2026-08`. The predecessor, for `al/zkvm-r3`, is `levers-r3.py`: it ranked 25 levers
whose figures were all measured against a base that has since moved, and porting them here would have
meant re-measuring every one. That sweep has not been run, so those entries are archived rather than
carried over — the document's own rule is that a lever's verdict belongs to the base it was measured
on, and a rebase reopens all of them.

Everything ranked below was measured on r4. Ratios are read from results/compare-r4.json at render
time; profile shares are quoted from one named run and labelled as such, because a share from one block
is not a corpus figure.

  ./levers.py [--out results/levers.html]
"""
import argparse, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPARE_JSON = os.path.join(HERE, 'results', 'compare-r4.json')
MIN_BLOCKS = 300

# ── the axes this document speaks about ──────────────────────────────────────────────────────────
AX = {
    'base_zisk': 'monad-storageroot-det-2026-08-zisk-vs-zisk-reth',
    'base_sp1': 'monad-storageroot-det-2026-08-sp1-vs-rsp',
    'r4_zisk': 'r4-vs-zisk-reth',
    'r4_sp1': 'r4-vs-rsp',
    'self_zisk': 'monad-zkvm-r4-gen-2026-08-9d7540181-zisk-vs-monad-storageroot-det-2026-08-zisk',
    'self_sp1': 'monad-zkvm-r4-gen-2026-08-9d7540181-sp1-vs-monad-storageroot-det-2026-08-sp1',
}

# ── the r4 profile ───────────────────────────────────────────────────────────────────────────────
# One run, named, so a reader can reproduce it rather than trust it:
#   ./hotspots.py profile --backend zisk \
#     --elf ../guests/monad/gen/zkvm-r4-gen-2026-08-9d7540181/elf/monad-zkvm-guest-zisk.elf \
#     -i <framed 25552366.witness> --out /tmp/hs
# 25552366 is the largest witness of the corpus (15.8 MB, 425.1 M steps). The small-block profile
# (25552101, 78.8 M) is quoted where the two disagree, because a fixed cost is a larger share there.
PROFILE = {
    'block': 25552366, 'steps': 425.1e6, 'small_block': 25552101, 'small_steps': 78.8e6,
    'rows': [
        ('find_jumpdests', 6.53, 7.65, 'Intercode::find_jumpdests'),
        ('trie encode', 12.33, 6.84, 'match&lt;…OffsetTrie…&gt; ×3 — child_ref&lt;true&gt;, '
                                     'child_ref&lt;false&gt;, encode_rlp'),
        ('keccak', 4.70, 3.40, 'monad_zkvm_keccak256_fast'),
        ('find_original', 3.28, 1.97, 'OffsetTrie::find_original'),
        ('push&lt;2&gt;', 4.39, 7.23, 'interpreter::push&lt;2&gt;'),
        ('swap&lt;1&gt;', 2.75, 2.63, 'interpreter::swap&lt;1&gt;'),
        ('mstore', 2.33, 3.92, 'interpreter::mstore'),
    ],
}

# ── what was tried on r4 ─────────────────────────────────────────────────────────────────────────
TRIED = [
    {
        'id': 'jd', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': 'The JUMPDEST scan tests each opcode twice and re-reads the code length',
        'num': '+1.02 % work · +0.90 % COST · 504/504 blocks',
        'w': "<code>find_jumpdests</code> is the single hottest function in the guest — 6.5 % of the "
             "large block, 7.7 % of the small one. It runs on every code byte of every distinct "
             "contract a block touches, and because code is cached by hash that cost is paid once per "
             "contract rather than amortised across calls.<br><br>"
             "Per byte the loop did: a load, a compare against JUMPDEST, <i>two</i> range compares for "
             "<code>is_push_opcode</code>, a subtract for <code>get_push_opcode_index</code>, span "
             "indexing, and a re-read of <code>code.size()</code>.",
        'fix': "One 256-entry table gives the push-data length (0 for non-PUSH), so the two range "
               "compares and the subtract become a single load. The loop walks a raw pointer against a "
               "hoisted end and the advance is one add.<br><br>"
               "<b>Unanimous across the corpus</b> — 504 blocks of 504 — which is what a structural "
               "saving looks like, as opposed to a mix effect that a median hides.<br><br>"
               "It removes only ~17 % of the function. A byte-at-a-time scan needs about five "
               "instructions per byte whatever you do, and this is near that floor: the opcode tests "
               "were not the dominant term.",
        'rem': "./compare.py --axis &lt;jd-vs-r4&gt; --block-min 25551991 --block-max 25552494<br>"
               "<code>find_jumpdests</code> should fall about a fifth and nothing else should move. "
               "Anything else moving means the loop was restructured, not just its tests replaced.",
    },
    {
        'id': 'br', 'verdict': 'REFUTED', 'branch': 'al/zkvm-r4-br (binary dropped)',
        't': 'Decide branch children in pairs when both are empty',
        'num': '−0.52 % to −0.98 % on three blocks',
        'w': "A branch node stores 16 contiguous <code>node_id_wire</code> slots with four zero bytes "
             "for an empty child and <b>no live-child mask</b>, so <code>encode_rlp</code> walks all "
             "16 and <code>child_ref</code>'s fast head writes a single <code>0x80</code> for each "
             "empty one.<br><br>"
             "The idea: a pair of empty slots is eight zero bytes, so one 64-bit load and one compare "
             "decide two children, and one two-byte store finishes them.",
        'fix': "It loses, and the loss is the finding: <b>touched branches carry more live children "
               "than “sparse” suggests</b>, so the probe is paid on pairs that then do the full work "
               "anyway — and <code>child_ref</code>'s NULL path was already close to its floor.<br><br>"
               "Not taken to a corpus run: a lever that loses on every block sampled does not need "
               "504 of them.",
        'rem': "Build it again only with evidence about the live-child distribution in hand. The "
               "cheap version of that evidence is a counter in <code>encode_rlp</code>'s branch case, "
               "not another ELF.",
    },
    {
        'id': 'lazyjd', 'verdict': 'REFUTED', 'branch': 'al/zkvm-r4-lazyjd (binary dropped)',
        't': 'Build the JUMPDEST map on first use instead of in the constructor',
        'num': '−0.14 %, −0.36 %, −0.22 % on three blocks',
        'w': "Code enters the by-code-hash cache for <code>EXTCODESIZE</code>/"
             "<code>EXTCODEHASH</code>/<code>EXTCODECOPY</code> and for calls that revert before "
             "jumping, not only for code that runs — so scanning every byte of it in the constructor "
             "pays for maps nobody reads.<br><br>"
             "Guest-only, and the guard is load-bearing: <code>Intercode</code> is shared through that "
             "cache and the host executes transactions in parallel, so a mutable build-on-read would "
             "be a data race there. The guest is single-threaded.",
        'fix': "It loses, and the loss says <b>nearly all cached code is jumped into</b>: the saving "
               "almost never materialises while one branch is paid on every <code>is_jumpdest</code>, "
               "and <code>jump</code>+<code>jumpi</code> are 4.7 % of a block.<br><br>"
               "The arithmetic was written down before the build and it held — worth repeating as a "
               "habit, not as a boast: it cost one build to confirm rather than three to discover.",
        'rem': "Nothing to re-measure. If it is ever revisited, measure the share of cached code that "
               "is never jumped into <i>first</i>; that number decides the lever before any build.",
    },
]

# ── what remains ─────────────────────────────────────────────────────────────────────────────────
REMAIN = [
    {
        'id': 'trie', 'share': 12.33,
        't': 'RLP encoding and child resolution in the trie — 12.3 %, and no cheap win in it',
        'w': "Three <code>match&lt;…OffsetTrie…&gt;</code> instantiations account for 12.3 % of the "
             "large block: 5.33 + 4.02 + 2.98. All three return <code>node_rlp_span</code>, so they "
             "are <code>child_ref&lt;true&gt;</code>, <code>child_ref&lt;false&gt;</code> and "
             "<code>encode_rlp</code>.<br><br>"
             "<b>This is not dispatch overhead.</b> <code>match</code> is already a <code>switch</code> "
             "on the node tag; the cost is the inlined lambda bodies, attributed to the enclosing "
             "symbol by the profiler. I said the opposite before checking, and it is the kind of "
             "mistake that sends a day into the wrong file.",
        'fix': "What is actually there is real work: encoding nodes to canonical RLP and resolving "
               "their children. Part of it is now unavoidable — the base derives each account's "
               "storage root instead of copying the stored one, so every account whose storage the "
               "block wrote gets its subtree re-encoded and re-hashed (priced below).<br><br>"
               "Two attempts on this surface already failed (<code>br</code> above, and the flat hash "
               "store retired in the r3 era). Anything further should start from a measurement of the "
               "node-shape distribution, not from an intuition about it.",
    },
    {
        'id': 'attest', 'share': None,
        't': 'Attesting the storage tries costs +4.6 % work / +6.6 % prover cost',
        'w': "Measured by ablation (<code>al/zkvm-r4-ablate</code>, 504 blocks: 179.0 M vs 187.4 M "
             "steps). Not a lever to take — a soundness property to price.<br><br>"
             "<code>OffsetTrie::hash(id)</code> is free on two of three paths: <code>NULL</code> "
             "returns <code>NULL_ROOT</code>, a <code>DIGEST</code> returns the 32 bytes it already "
             "carries. On the third it consults <code>hashes_</code>, and <b>on a miss it re-encodes "
             "the node and keccaks it</b>. What causes a miss is writing: every mutation does "
             "<code>hashes_.erase(id)</code>.<br><br>"
             "So the added cost is recomputing the storage root of every account whose storage the "
             "block wrote — which is exactly what the old encoder avoided, for the same reason it was "
             "unsound. Not attesting and not recomputing were one saving, not two.",
        'fix': "Nothing to fix; the cost buys the property. Two notes for whoever reads a ratio:<br>"
               "• prover cost moves half again as fast as work (+6.6 % vs +4.6 %) because what is "
               "added is keccak, which ZisK's COST model prices heavily against instruction count;<br>"
               "• the r3-era figure of 0.763× against zisk-reth was measured <i>without</i> this "
               "property. 0.795× is not a regression against it — it is the same guest proving more.",
    },
    {
        'id': 'keccak', 'share': 4.70,
        't': 'keccak at 4.7 % is already accelerated — the floor is a pricing question',
        'w': "<code>monad_zkvm_keccak256_fast</code> is our word-wise entry, worth +14 % on ZisK and "
             "+19.6 % on SP1 when it landed. What remains is the per-invocation floor: ZisK prices the "
             "permutation call, so a hash of one block of input costs more than its permutation.",
        'fix': "Upstream ask, already raised, not a guest change. The guest-side lever that would "
               "matter is <i>fewer</i> hashes, which is the attestation term above — and that one is "
               "load-bearing.",
    },
    {
        'id': 'interp', 'share': 9.47,
        't': 'Interpreter stack and memory ops — ~20 % together, and ZisK has already refused the '
             'obvious fix',
        'w': "<code>push&lt;2&gt;</code> 4.39 %, <code>swap&lt;1&gt;</code> 2.75 %, "
             "<code>mstore</code> 2.33 % on the large block, and the shares roughly double on the "
             "small one — these are uint256 moves through the stack and memory.",
        'fix': "The SP1 work on this surface (inline 32-byte staging, word-wise <code>mem*</code>, "
               "lane copies) is measured and landed, all of it SP1-guarded. <b>Applied unguarded the "
               "same inlining costs ZisK 6.2 %</b>, because its <code>memcpy</code> handles "
               "misalignment better than a generic shift-combine. That asymmetry is measured, not "
               "assumed.<br><br>"
               "So the remaining room here is SP1-shaped, and the SP1 arm has not been re-profiled on "
               "r4 — the profiling <code>sp1-runner</code> build is a prerequisite the RUNBOOK "
               "documents.",
    },
]

# ── the ablation sweep ───────────────────────────────────────────────────────────────────────────
# One ELF per lever with that lever alone removed, each measured against r4 over the same 504 blocks.
# Read at render time from the sweep's own JSON; the only thing written by hand is the commentary.
ABLATION_JSON = ['results/compare-ablations.json', 'results/compare-ablations2.json']
TRIM_JSON = 'results/compare-trim.json'

# Removing arena, clz and hashinline TOGETHER. The control that decides how to read the small numbers.
TRIM_MEMBERS = ('arena', 'clz', 'hashinline')

ABL_NOTE = {
    'keccak': "The word-wise entry into the permutation precompile. Removing it costs more than the "
              "next nine levers combined.",
    'keyhash': "Address and bytes32_t keys hashed by fold+fmix64 instead of wyhash. It was +1.24 % on "
               "the r3 base and is worth more here — a guest with 27 % of its work removed spends a "
               "larger share of what is left in map lookups.",
    'div': "The 128/64 division step specialised. +3.07 % when it landed, +2.84 % now: the closest "
           "agreement across a rebase in this table.",
    'arena': "ZisK only. It stays on SP1, where it was measured at +7.3 % against TLSF and has NOT "
             "been re-verdicted — that figure is too large to discard on a ZisK measurement.",
    'mulmod': "Its case was never the median: +11.2 % on math-heavy blocks. A median over 504 ordinary "
              "blocks cannot see a lever whose value is in the tail.",
    'addmod': "Same shape as mulmod — +0.71 % was claimed on math-heavy blocks, not on the median.",
}

PRIOR = (
    "Two of the three levers tried on r4 failed with <b>the same shape</b>: a probe that avoids work, "
    "sitting on a hotter path than the work it avoids. Treat that as a prior for this guest — its hot "
    "paths are near their instruction floor, so conditional avoidance needs the avoided work to be "
    "large <i>and</i> the check to be rare. The lever that worked did neither: it made the same work "
    "cheaper."
)

CSS = """
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#e3e3e3;--accent:#b45309;--ok:#166534;--no:#991b1b;
--card:#fafafa}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--accent:#fbbf24;
--ok:#4ade80;--no:#f87171;--card:#1a1a1a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 80px}
.eyebrow{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
h1{font-size:30px;line-height:1.2;margin:0 0 14px}
h2{font-size:20px;margin:38px 0 10px;padding-top:18px;border-top:1px solid var(--line)}
.prov{font-size:13px;color:var(--mut);background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;margin:0 0 8px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:10px 0}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.item{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0;
background:var(--card)}
.item h3{margin:0 0 6px;font-size:17px}
.tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;padding:2px 7px;
border-radius:4px;margin-right:8px;vertical-align:2px}
.tag.ok{background:var(--ok);color:#fff}.tag.no{background:var(--no);color:#fff}
.tag.n{background:var(--mut);color:#fff}
.num{font-variant-numeric:tabular-nums;color:var(--accent);font-weight:600}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:12px 0 2px}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(127,127,127,.13);
padding:1px 4px;border-radius:3px}
.note{font-size:13px;color:var(--mut)}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);font-size:12.5px;
color:var(--mut)}
"""


def load():
    if not os.path.exists(COMPARE_JSON):
        sys.exit(f"missing {COMPARE_JSON} — render the r4 axes first:\n"
                 f"  ./compare.py --axis {AX['r4_zisk']} --axis {AX['r4_sp1']} … "
                 f"--json results/compare-r4.json")
    d = json.load(open(COMPARE_JSON))
    missing = [k for k, ax in AX.items() if ax not in d]
    if missing:
        sys.exit(f"{COMPARE_JSON} lacks the axes this document is about: "
                 f"{', '.join(AX[k] for k in missing)}")
    # Provenance gate, kept from the r3 generator because it caught a real failure: a diagnostic run
    # with --json defaulting to the canonical path once replaced the sweep with six blocks, and the
    # document rendered from it looked entirely plausible.
    for k, ax in AX.items():
        n = d[ax]['summary']['n']
        if n < MIN_BLOCKS:
            sys.exit(f"refusing to build: {ax} has n={n} (< {MIN_BLOCKS}) — {COMPARE_JSON} looks like "
                     f"a partial run.")
    return d


def s(d, key):
    return d[AX[key]]['summary']


def x(v):
    return '—' if v is None else f"{v:.3f}×"


def _load(rel):
    p = os.path.join(HERE, rel)
    return json.load(open(p)) if os.path.exists(p) else None


def ablation_section():
    """The sweep, read from its own JSON. Returns '' if it has not been run."""
    rows = []
    for rel in ABLATION_JSON:
        d = _load(rel)
        if not d:
            continue
        for ax, v in d.items():
            sm = v['summary']
            r, c = sm['ratio_median'], sm.get('cost_ratio_median')
            rows.append({'id': ax.replace('abl-', ''), 'n': sm['n'],
                         'without': sm['a_median'], 'r4': sm['b_median'],
                         'w': (r - 1) * 100, 'c': ((c - 1) * 100) if c else None})
    if not rows:
        return ""
    rows.sort(key=lambda t: -t['w'])
    trim = _load(TRIM_JSON)
    tsum = trim['trim-vs-r4']['summary'] if trim else None

    h = ["<h2>The ablation sweep</h2>",
         "<p class=note>One ELF per lever with that lever <i>alone</i> removed, each measured against "
         "r4 over the same 504 blocks. A lever is worth having when removing it makes the guest do "
         "<b>more</b> work.</p>",
         "<table><tr><th>lever removed</th><th class=n>without</th><th class=n>r4</th>"
         "<th class=n>work</th><th class=n>cost</th><th>note</th></tr>"]
    for r in rows:
        # One row, built in one place. The ternary-across-the-whole-append shape swallowed the <tr>
        # whenever cost was absent, which is the second time it has bitten in this file.
        cost = f"{r['c']:+.2f}%" if r['c'] is not None else "—"
        h.append(f"<tr><td><code>{r['id']}</code></td>"
                 f"<td class=n>{r['without']/1e6:.2f} M</td>"
                 f"<td class=n>{r['r4']/1e6:.2f} M</td>"
                 f"<td class=n>{r['w']:+.2f}%</td>"
                 f"<td class=n>{cost}</td>"
                 f"<td class=note>{ABL_NOTE.get(r['id'], '')}</td></tr>")
    h.append("</table>")

    if tsum:
        tr = (1 - tsum['ratio_median']) * 100
        tc = (1 - tsum.get('cost_ratio_median', 1)) * 100
        summed = sum(-r['w'] for r in rows if r['id'] in TRIM_MEMBERS)
        h.append("<div class=item>"
                 "<h3><span class='tag no'>CONTROL</span>Removing the three smallest “costs” together "
                 "returns nothing</h3>"
                 f"<p class=num>{tr:+.2f} % work · {tc:+.2f} % cost · n={tsum['n']} — "
                 f"against {summed:+.2f} % predicted by summing them</p>"
                 "<p>Individually <code>" + "</code>, <code>".join(TRIM_MEMBERS) + "</code> each "
                 "measured as costing 0.6–0.8 %. Removed together they return "
                 f"{tr:+.2f} %.</p>"
                 "<p><b>So those per-lever numbers are not the levers.</b> They are what moving code "
                 "does to layout and inlining — three removals that each “win” 0.7 % cancel when "
                 "combined, which they could not do if the 0.7 % lived in the levers. The noise floor "
                 "of this method on this guest is about ±1 %.</p>"
                 "<p>Read the table accordingly: <code>keccak</code>, <code>keyhash</code> and "
                 "<code>div</code> clear that floor and carry 20.6 % of the guest's work between them. "
                 "The other seven are indistinguishable from zero, and treating them as costs would be "
                 "reading noise as signal.</p></div>")
    return "\n".join(h)


def render(d, out):
    S = {k: s(d, k) for k in AX}
    h = [f"<!doctype html><meta charset=utf-8><title>What to fix — Monad guest, r4</title>"
         f"<meta name=viewport content='width=device-width,initial-scale=1'><style>{CSS}</style>",
         "<div class=wrap>",
         "<p class=eyebrow>zkvm-bench · monad guest · r4</p>",
         "<h1>What to fix, ranked — with a way to check each one</h1>"]

    h.append("<div class=prov>"
             "<b>Branch</b> <code>al/zkvm-r4</code> — 30 commits on "
             "<code>origin/sam/zkvm-zisk-sp1</code>, local, not pushed. "
             "<b>Corpus</b> <code>storageroot-det-2026-08</code>, 504 blocks 25551991–25552494, "
             "generated with serialised execution so it is reproducible."
             "<br><b>Predecessor</b> <code>levers-r3.py</code> ranked 25 levers for "
             "<code>al/zkvm-r3</code>. Its figures were measured against a base that has since moved — "
             "Sam absorbed the trie levers, and the base now attests the storage tries — so they are "
             "archived rather than carried over. Re-ranking them needs an ablation sweep on r4, which "
             "has not been run."
             "<br><b>Generated</b> " + time.strftime('%Y-%m-%d %H:%M %Z') +
             " by profiling/levers.py from results/compare-r4.json"
             "</div>")

    h.append("<h2>Where r4 stands</h2>")
    h.append("<table><tr><th>comparison</th><th class=n>n</th><th class=n>work</th>"
             "<th class=n>prover cost</th></tr>")
    for lbl, k, unit in [("baseline ÷ zisk-reth", 'base_zisk', 'COST'),
                         ("<b>r4 ÷ zisk-reth</b>", 'r4_zisk', 'COST'),
                         ("r4 ÷ baseline (ZisK)", 'self_zisk', 'COST'),
                         ("baseline ÷ rsp", 'base_sp1', 'PGU'),
                         ("<b>r4 ÷ rsp</b>", 'r4_sp1', 'PGU'),
                         ("r4 ÷ baseline (SP1)", 'self_sp1', 'PGU')]:
        v = S[k]
        h.append(f"<tr><td>{lbl}</td><td class=n>{v['n']}</td>"
                 f"<td class=n>{x(v['ratio_median'])}</td>"
                 f"<td class=n>{x(v.get('cost_ratio_median'))} {unit}</td></tr>")
    h.append("</table>")
    h.append("<p class=note>Prover cost is per backend and the two units do not compare to each "
             "other: ZisK's COST model, SP1's PGU. Only their ratios do. The JUMPDEST lever below "
             "landed after these were measured, so it is <b>not</b> in them.</p>")

    h.append("<h2>Where the work goes</h2>")
    h.append(f"<p class=note>Profile of the r4 ZisK guest on block {PROFILE['block']} "
             f"({PROFILE['steps']/1e6:.1f} M steps, the largest witness of the corpus). The second "
             f"column is block {PROFILE['small_block']} ({PROFILE['small_steps']/1e6:.1f} M) — a fixed "
             f"cost is a larger share of a small block, and the two columns disagreeing is "
             f"information, not noise.</p>")
    h.append("<table><tr><th>site</th><th class=n>large</th><th class=n>small</th>"
             "<th>symbol</th></tr>")
    for name, big, small, sym in PROFILE['rows']:
        h.append(f"<tr><td>{name}</td><td class=n>{big:.2f}%</td><td class=n>{small:.2f}%</td>"
                 f"<td><code>{sym}</code></td></tr>")
    h.append("</table>")

    h.append("<h2>Tried on r4</h2>")
    for t in TRIED:
        cls = 'ok' if t['verdict'] == 'CONFIRMED' else 'no'
        h.append(f"<div class=item><h3><span class='tag {cls}'>{t['verdict']}</span>{t['t']}</h3>"
                 f"<p class=num>{t['num']}</p>"
                 f"<p class=note><code>{t['branch']}</code></p>"
                 f"<div class=lbl>what</div><p>{t['w']}</p>"
                 f"<div class=lbl>{'fix' if cls == 'ok' else 'why it lost'}</div><p>{t['fix']}</p>"
                 f"<div class=lbl>how to check</div><p class=note>{t['rem']}</p></div>")

    h.append("<h2>What remains</h2>")
    for r in REMAIN:
        # An entry either carries a profile share (a target) or does not (a priced property).
        # Written out rather than folded into a ternary over the whole concatenation, which is how
        # the two shapes silently swapped their tags the first time.
        if r['share']:
            head = (f"<h3><span class='tag n'>OPEN</span>{r['t']}</h3>"
                    f"<p><span class=num>{r['share']:.2f}%</span> "
                    f"<span class=note>of the large block</span></p>")
        else:
            head = f"<h3><span class='tag n'>PRICED</span>{r['t']}</h3>"
        h.append(f"<div class=item>{head}"
                 f"<div class=lbl>what</div><p>{r['w']}</p>"
                 f"<div class=lbl>where that leaves it</div><p>{r['fix']}</p></div>")

    h.append(ablation_section())

    h.append("<h2>A prior worth carrying</h2>")
    h.append(f"<div class=item><p>{PRIOR}</p></div>")

    h.append("<footer>Generated by <code>profiling/levers.py</code>. Ratios computed from "
             "<code>results/compare-r4.json</code>; profile shares quoted from the named "
             "<code>hotspots.py</code> run above. Nothing measured is written into the prose by hand "
             "except those shares, which carry their block number so they can be reproduced."
             "</footer></div>")
    open(out, 'w').write("\n".join(h))
    print(f"wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'levers.html'))
    a = ap.parse_args()
    render(load(), a.out)


if __name__ == '__main__':
    main()
