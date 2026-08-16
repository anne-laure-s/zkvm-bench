#!/usr/bin/env python3
"""levers — what to fix in the Monad guest on the r4 base, ranked, with a way to check each one.

Not compare.py's report. compare.py is a generic instrument and its output must stay neutral; this is
the opposite — specific to *this* guest on *this* base, with a shelf life measured in rebases.

Written for `al/zkvm-r4` (33 commits on `origin/sam/zkvm-zisk-sp1`) and the deterministic corpus
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
# 25552366 is the largest witness of the corpus (15.8 MB, 429.0 M steps on this base). The
# small-block profile (25552101) is quoted where the two disagree, because a fixed cost is a larger
# share there — it has not been re-run since the rebase, so those cells read "—" rather than carry a
# figure from a base that has moved.
#
# The shares below are the base this pass started from, not what it left behind: the levers changed
# the profile they were chosen from.
PROFILE = {
    'block': 25552366, 'steps': 429.0e6, 'small_block': 25552101, 'small_steps': 78.8e6,
    'rows': [
        ('find_jumpdests', 5.56, None, 'Intercode::find_jumpdests'),
        ('trie encode', 11.88, None, 'match&lt;…OffsetTrie…&gt; ×3 — child_ref&lt;true&gt;, '
                                     'child_ref&lt;false&gt;, encode_rlp'),
        ('priming sweep', 4.99, None, 'OffsetTrie::OffsetTrie'),
        ('keccak', 4.65, None, 'monad_zkvm_keccak256_fast'),
        ('push&lt;2&gt;', 4.35, None, 'interpreter::push&lt;2&gt;'),
        ('find_original', 3.18, None, 'OffsetTrie::find_original'),
        ('swap&lt;1&gt;', 2.73, None, 'interpreter::swap&lt;1&gt;'),
        ('mstore', 2.31, None, 'interpreter::mstore'),
        ('upsert_node', 2.09, None, 'OffsetTrie::upsert_node'),
    ],
}

# ── the post-pass ratio ──────────────────────────────────────────────────────────────────────────
# Measured outside compare.py, so it is written down by hand and says so where it renders. Our steps
# per block come from ziskemu on the final ELF; the zisk-reth side is the axis already in
# profiling/cache (sha256:3d2a9db51125ec95), so only our side was re-run. Median of per-block ratios
# over the blocks the two sets share. base_ratio is the same computation on the pre-pass ELF, and it
# lands where the published figure plus the rebase cost says it should — which is why it is quoted:
# a method that cannot reproduce a known number should not be trusted with a new one.
POST = {'n': 365, 'zisk_ratio': 0.6584, 'base_ratio': 0.8016, 'published': '0.786×'}

# ── what was tried on r4 ─────────────────────────────────────────────────────────────────────────
TRIED = [
    {
        'id': 'digestrun', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': "A branch's digest children are already, in the blob, the bytes it must emit",
        'num': '−3.4 % / −6.3 % when added (small / large block)',
        'w': "<code>OffsetTrie::OffsetTrie</code> is <b>4.99 % of the profile on this base and was "
             "absent from the top of the previous one</b>. The rebase moved work into the priming "
             "sweep, and that is most of why this base costs 1.1–1.9 % more. Looking at what the "
             "sweep does with digests is what found this.<br><br>"
             "Digest nodes are <b>90.1 % of the blob</b> — 247 856 of 275 034 on 25552366 — and the "
             "producer lays a branch's digest children at <b>consecutive offsets, stride 33, in slot "
             "order</b>. A digest node is <code>DIGEST ‖ hash32</code>; the hash-ref its parent must "
             "emit is <code>0xa0 ‖ hash32</code>. The two differ in one byte.",
        'fix': "So a run of adjacent digest slots <i>is</i> the byte run the branch needs. Copy the "
               "run whole and stamp the tag bytes, instead of dispatching, resolving and re-encoding "
               "each slot. The 16 child ids are taken in one aligned read — a 4-byte wire field at an "
               "odd offset otherwise widens a byte at a time.<br><br>"
               "Reading the tag straight from the blob is sound on the commit pass too: every "
               "mutation path aborts on a <code>DigestView</code>, so no id reaching "
               "<code>put_node</code> names a digest and a digest is never shadowed.<br><br>"
               "<b>This retires the <code>trie</code> entry that used to sit under “what remains”</b>, "
               "which said there was no cheap win on that surface. There was one — not in the "
               "encoder, but in what the encoder was being asked to encode.",
        'rem': "The stride-33 layout is the producer's convention, not a format guarantee. If it ever "
               "changes the lever degrades slot-by-slot to the old cost rather than breaking, but the "
               "win evaporates silently — so re-measure it after any witness-writer change.<br><br>"
               "It reads the blob directly, so it <b>depends on the constructor's child-offset "
               "check</b> — the one the entry below tried to remove. The two must not be taken "
               "together.",
    },
    {
        'id': 'ctorval', 'verdict': 'REFUTED', 'branch': 'al/zkvm-r4 (reverted)',
        't': 'Drop the constructor\'s child-offset enumeration — reverted, and the reason is '
             'narrower than it first looked',
        'num': '−1.55 % / −2.74 %, given up',
        'w': "The constructor builds a <code>std::vector&lt;uint64_t&gt;</code> bitmap over the blob "
             "and, for every branch, ext and account leaf, asserts each child offset is backwards, in "
             "range, and <b>a recorded node start</b>. <code>MONAD_ASSERT</code> survives "
             "<code>NDEBUG</code> here, so it is real work on every node — and it is the most "
             "expensive thing the constructor does.<br><br>"
             "It was removed on the argument that the properties are enforced where they are used: "
             "<code>get_original</code> bounds the offset against the blob, and a child pointing "
             "forward or mid-node reaches <code>child_ref_compute</code> unprimed, which aborts.",
        'fix': "<b>The second half of that argument is false.</b> The abort is real but it fires "
               "<i>after</i> the read:<br><br>"
               "<code>unsigned char buf[MAX_NODE_RLP];</code><br>"
               "<code>node_rlp_span const rem = encode_rlp&lt;priming_pass&gt;(node, "
               "node_rlp_span{buf});</code><br>"
               "<code>...</code><br>"
               "<code>if constexpr (priming_pass) { MONAD_ABORT(\"... bad offset\"); }</code><br><br>"
               "<code>encode_rlp</code> has already decoded the node. <code>NodeViewBase</code> "
               "carries no end bound — only <code>checked_end(region_end)</code> does, and that is "
               "used solely by the constructor's own walk. So a mid-node offset reads past the blob. "
               "Worse, <code>node_rlp_span::shrink(n)</code> is <code>first(size() - n)</code>: a "
               "garbage node claiming more length than remains underflows the <code>size_t</code> and "
               "<b>writes past a stack buffer</b>, from prover-supplied bytes.<br><br>"
               "What the enumeration establishes is exactly <code>encode_rlp</code>'s precondition: "
               "every child offset is the start of a node whose extent <code>checked_end</code> "
               "proved lies in the region.<br><br>"
               "<b>What that is worth was then tested, and the answer is narrower than the argument.</b> "
               "50 crafted blobs — child offsets pointing forward, mid-node, past the blob, and at the "
               "mid-node positions that decode as the longest-running nodes in the file — are "
               "<b>rejected by both versions</b>, with and without the check. The removal is a lost "
               "precondition and a read that runs before the abort that stops it; it is <b>not</b> a "
               "demonstrated way to get a false proof, and calling it unsound overstated it.<br><br>"
               "Reverted anyway: the burden is on the change, and 2.7 % does not buy a decoder "
               "precondition you cannot show is safe to lose. A third of it came back with "
               "<code>digestids</code> below.",
        'rem': "Two follow-ons were measured and both fail. <b>Merging the two <code>match</code> "
               "dispatches</b> the constructor performs — one to enumerate children, one to hash — "
               "into a single walk was the obvious way to keep the check and drop its overhead: it "
               "comes out at <b>+0.10 % / +0.17 %</b>, i.e. slightly worse. The duplicated dispatch "
               "was not where the cost was. And <code>std::unreachable()</code> on "
               "<code>find_original</code>'s null arm is only sound while this check exists, so it is "
               "back to <code>MONAD_ABORT</code>.<br><br>"
               "Anything further here must start by measuring which part of the check costs — the "
               "bitmap's allocation and zeroing, the per-node bit-set, or the ~350 k per-child "
               "bit-tests — because the one structural guess has now been refuted.",
    },
    {
        'id': 'nibbytes', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': 'Trie paths walked a nibble at a time, and they are 56–59 nibbles long',
        'num': '+2.80 % / +4.85 % when removed',
        'w': "A leaf's path is the whole un-descended tail of the key. Both "
             "<code>compact_encode_raw</code> and <code>NibblesView::operator==</code> walked it "
             "through <code>get_nibble</code>/<code>set_nibble</code> — a byte read-modify-write per "
             "nibble, roughly 60 of them per call.",
        'fix': "Both runs are byte runs underneath. <code>compact_encode_raw</code>'s destination is "
               "byte-aligned after the flag byte, so what remains is a straight copy or one uniform "
               "4-bit shift depending on the source's parity. <code>operator==</code> compares whole "
               "bytes with <code>memcmp</code> when both sides share a parity and handles only the "
               "ragged nibble at each end; mismatched parity keeps the nibble walk.<br><br>"
               "Both keep the nibble-wise form under <code>is_constant_evaluated</code> for the "
               "constexpr callers, and <code>compact_encode.hpp</code> is shared with the host node — "
               "so this was checked <b>exhaustively against the loops it replaces</b>: both parities, "
               "every length, 17 744 cases, no divergence. A one-nibble error changes a leaf's hash "
               "and therefore all three public values, which is exactly the failure a corpus run "
               "might not reach.",
        'rem': "Re-run the exhaustive check, not a corpus run, if either function is touched. It is "
               "seconds and it covers the domain a witness corpus samples thinly.",
    },
    {
        'id': 'pushbe', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': 'PUSH built each lane little-endian and then byte-swapped it',
        'num': '+5.31 % / +3.20 % when removed',
        'w': "<code>generic_push</code> memcpy'd bytes into a little-endian word and ran "
             "<code>bswap64</code>. On a target with no byte-swap instruction the word escapes to the "
             "stack, so each lane pays a reload plus the full mask-and-shift swap. "
             "<code>push&lt;2&gt;</code> is 4.35 % of the large block and <code>push&lt;1&gt;</code> "
             "2.07 % — the gap between two adjacent widths is the swap.",
        'fix': "Assemble the K big-endian bytes straight into the word: K−1 ors, no round trip. The "
               "AVX2 host build never instantiates this path — <code>use_avx2_push</code> is true for "
               "every N &gt; 0 — so it is guest-only in effect without needing a guard.<br><br>"
               "Note this is the <i>opposite</i> shape to the lever that cost ZisK 6.2 %: it removes "
               "a round trip rather than replacing a <code>memcpy</code> with a hand-rolled copy.",
        'rem': "GCC re-recognises the byte-swap idiom at some widths. If this is ever re-tuned, check "
               "the emitted body per K rather than assuming a blanket win.",
    },
    {
        'id': 'toavx', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': '<code>to_avx()</code> memcpy\'d a type into itself, and it became a library call',
        'num': '+3.41 % / +3.06 % when removed — one line',
        'w': "Off AVX2, <code>m256i</code> <i>is</i> <code>words_t&lt;4&gt;</code>, the type "
             "<code>words_</code> already has. <code>to_avx()</code> copied it with "
             "<code>std::memcpy</code> anyway. GCC cannot inline that on rv64: through the builtin's "
             "<code>void *</code> the alignment is inferred as 8 bits, 32 byte-moves blow the "
             "straight-move budget, and it emits a <code>memcpy</code> <b>call</b> — on "
             "<code>swap&lt;N&gt;</code>, the EVM stack's hottest path.<br><br>"
             "The repo already knew: <code>mem_sp1.cpp</code> gives SP1's <code>memcpy</code> a "
             "dedicated fast path for exactly this case. The ZisK arm still paid the call.",
        'fix': "Return <code>words_</code>. One <code>#elif !defined(__AVX2__)</code> arm.<br><br>"
               "Do <i>not</i> reuse the existing SP1 arm's four-lane loop instead: GCC does not "
               "unroll it on rv64 and it comes out worse than the memcpy it replaces.",
        'rem': "Nothing to re-measure. If <code>m256i</code>'s non-AVX2 definition ever stops being "
               "<code>words_t&lt;4&gt;</code>, this arm must go back to a copy.",
    },
    {
        'id': 'srootcache', 'verdict': 'REFUTED', 'branch': 'al/zkvm-r4 (reverted)',
        't': 'Cache the storage-root lookup across an account\'s slot reads',
        'num': '−0.39 % / −0.77 % — under the floor',
        'w': "<code>PartialTrieDb::read_storage</code> keccaks the address and re-descends the "
             "account trie <b>for every slot</b>, and the EVM reads an account's slots in bursts. "
             "Both reads are of the pre-state trie, which is immutable for the whole block, so one "
             "cached entry is sound and never needs invalidating.",
        'fix': "It works, and it is not enough. Mechanically the avoided work is real and countable — "
               "but the measurement lands at −0.39 % and −0.77 %, <b>below the ±1 % floor this method "
               "resolves</b>, and the control that established that floor is in the ablation table "
               "below.<br><br>"
               "Reverted rather than counted. The rule is only worth having if it also bites when the "
               "lever is one you wanted to keep.",
        'rem': "If it is revisited, it needs a method that can see under 1 % — count the avoided "
               "descents directly rather than diffing two ELFs.",
    },
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
        'id': 'advance', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r4 (landed)',
        't': 'The JUMPDEST table held the push data length, so the loop paid a <code>+1</code>',
        'num': '−0.93 % work · −0.44 % COST · 504 blocks — <b>under the floor, and taken anyway</b>',
        'w': "The entry above said this scan was near the ~5-instruction-per-byte floor a "
             "byte-at-a-time walk cannot beat. <b>That was wrong, and only counting showed it.</b> "
             "The loop it landed was seven:<br><br>"
             "<code>lbu / add / beq / lbu / addiw / add / bltu</code><br><br>"
             "The <code>addiw</code> is the <code>+1</code> in <code>p += 1 + "
             "push_data_len[op]</code>, paid on every opcode of every distinct contract.",
        'fix': "Store the whole advance in the table — <code>1 + push data</code>, and 1 for every "
               "non-PUSH byte — and the add disappears. Six instructions:<br><br>"
               "<code>lbu / add / beq / lbu / add / bltu</code><br><br>"
               "<b>The corpus median is under the ±1 % floor, so the delta is not what justifies "
               "this one — the disassembly is.</b> The floor exists because a small aggregate change "
               "cannot be told from layout and inlining shifting around; it does not apply when the "
               "claim is “this instruction is gone” and the binary shows the instruction gone. Both "
               "figures are quoted so a reader can see they agree in sign and rough size.<br><br>"
               "Six is likely the end of it: what is left is the opcode load, the table address, the "
               "JUMPDEST test, the table load, the advance and the loop. The 5-instruction version "
               "an analysis pass proposed does not exist without restructuring the loop.",
        'rem': "Re-count the loop, do not re-run the corpus — one <code>llvm-objdump -d</code> on "
               "<code>find_jumpdests</code> settles whether a change to this scan did what it "
               "claimed, and the corpus cannot at this size.",
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
        'id': 'trie', 'share': None, 'tag': 'TAKEN',
        't': 'What is left of the trie encoder after the digest-run copy',
        'w': "This surface used to carry the largest open share and the note that there was no cheap "
             "win in it. The digest-run copy took most of it: 90 % of the nodes a branch resolves are "
             "digests, and those no longer go through the encoder at all.<br><br>"
             "What remains is the real work the entry always described — encoding nodes to canonical "
             "RLP and resolving their non-digest children — plus the attestation term priced below. "
             "The remaining child slots are ~26 600 real against 247 856 digests on the large block, "
             "so the same trick has nothing left to coalesce.",
        'fix': "The lesson is worth more than the residue. The entry was right that the encoder had "
               "no cheap win and wrong that this meant the surface did: the saving was in <i>what was "
               "being handed to</i> the encoder, which a profile by symbol cannot show. When a site "
               "resists, count what flows through it before concluding it is at its floor.<br><br>"
               "A witness-format change would take the rest — digests carried inside the parent's RLP "
               "rather than as separate blob nodes — but that costs a full corpus regeneration and a "
               "producer change, so it is a decision, not a lever.",
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
        'id': 'keccak', 'share': 7.17,
        't': 'keccak at 7.2 % of steps is already accelerated — the floor is a pricing question',
        'w': "<code>monad_zkvm_keccak256_fast</code> is our word-wise entry, worth +14 % on ZisK and "
             "+19.6 % on SP1 when it landed. What remains is the per-invocation floor: a hash of one "
             "block of input costs more than its permutation.<br><br>"
             "<b>That floor is not the syscall.</b> Disassembled on r4: <code>syscall_keccak_f</code> "
             "issues exactly <b>one</b> instruction per permutation — 90,564 <code>csrrs</code> for "
             "90,564 permutations, closing against the emulator's own keccak op count. The 100-vs-533 "
             "split between two inlined call sites is an <b>emulator accounting artifact</b>, not "
             "work. Any per-invocation model built on 533 over-counts short hashes ~3×.<br><br>"
             "<b>A third ask, sized and sent with the others:</b> the precompile permutes but does "
             "not absorb, so the guest does it — 60,377 rate blocks x 17 lanes of "
             "<code>load input, load state, xor, store state</code> = <b>4,105,636 steps, 2.44 % "
             "of the guest, 33 % of everything the wrapper does</b>. If <code>keccak_f</code> took "
             "the rate block and XORed it at round 0 that becomes a pointer, and in-circuit it is "
             "17 x 64 XOR gates against 24 rounds already constrained. In "
             "<code>infra/zisk-infra/docs/keccak-cost-ask.md</code>, item 3.<br><br>"
             "What the wrapper does pay is its own setup: 30,171 calls carry one <code>lbu</code> and "
             "one <code>ori</code> for the pad byte, and <b>21.6 % of calls (6,529) take the "
             "misaligned branch</b>, whose body is 3,130,932 steps.<br><br>"
             "<b>That branch is now gone</b> (<code>al/zkvm-r4-levers</code>, unbuilt). With "
             "<code>-mtune=generic-ooo</code> landed, <code>bits::load64</code> is one "
             "instruction, so the sponge needs no alignment case: one loop, one load per lane. "
             "The two changes are coupled — under the default tuning that loop would be "
             "byte-staged and worse than the branch it replaces.<br><br>"
             "It is also <b>safer</b>: the shift-combine read <code>w[i+1]</code> for i = 16, up "
             "to 7 bytes past <code>p + RATE</code>, which is why it carried a "
             "<code>len &gt; RATE + 7</code> guard and a buffered trailing block. Both are gone "
             "with it.<br><br>"
             "Sized with the codegen ratio <i>measured</i>, not modelled: at -O3 "
             "-mtune=generic-ooo the 17-lane block unrolls to <b>91 instructions against 153</b>, "
             "so the misaligned body lands near 1.86 M steps. Per misaligned block: 62 instructions "
             "saved (4,216 cells of MAIN) against 17 lanes going from an aligned read to a "
             "boundary-crossing one, 159 against 16 (2,431 of MEMORY) — <b>net 1,785 per "
             "block, 40.3 M, +0.20 % of COST</b>.<br><br>"
             "<b>Repriced twice.</b> A first pass modelled 4 instructions per lane after and "
             "said 0.59 %; compiling it said 5.4. A second charged the crossing read at 106 "
             "and said 0.30 %; <i>measuring</i> it (see <code>memcost</code>) says 159 — 106 "
             "is the price of a sub-word access, not of a straddling one. The direction never "
             "moved; the magnitude moved 3x.<br><br>"
             "Equivalence tested on the host: the 17-lane values XORed into the state, old path "
             "against new, over 8 alignments x 18 lengths x 400 random buffers — <b>57,600 cases, "
             "0 divergences</b>. Not built; this is on keccak, 29.5 % of the block's COST, so run "
             "the corpus and diff public values first.",
        'fix': "Upstream ask, already raised, not a guest change. The guest-side lever that would "
               "matter is <i>fewer</i> hashes, which is the attestation term above — and that one is "
               "load-bearing.",
    },
    {
        'id': 'nodeid', 'share': 14.8, 'tag': 'BUILT',
        't': '<code>read_node_id</code> widens a 4-byte child slot byte by byte — 1.99 % of the '
             'guest, and the fix is already in the tree one path over',
        'w': "The MPT complex is <b>14.8 % of COST</b>: <code>match</code> ×4 at 9.53 %, "
             "<code>find_original</code> 2.65 %, <code>upsert_node</code> 1.70 %, priming 0.89 %.<br><br>"
             "<b><code>match</code> is two functions wearing one name.</b> Two instantiations are "
             "<code>encode_rlp</code> — 17,176 calls at <b>843 and 1,266 steps each</b>, 10.2 % of "
             "the guest in ~17 k calls. Two are <code>child_ref</code> — 171,672 calls at a flat "
             "<b>46</b>. Averaging them hides it, and a lever aimed at &ldquo;match&rdquo; hits one "
             "or the other, never both.<br><br>"
             "<b>The hottest block in both <code>encode_rlp</code> instantiations is the same 15 "
             "instructions</b> — <code>lbu</code>×4, <code>sb</code>×4, <code>lwu</code> — 139,952× "
             "in one and 79,824× in the other. That is <code>offset_trie.hpp:113</code>:<br>"
             "<code>node_id_wire v; std::memcpy(&amp;v, p, sizeof(v));</code> on an alignment-1 "
             "pointer. <code>BranchView::children()</code> calls it sixteen times per branch.<br><br>"
             "Guest-wide: <b>249 static sites, 3,348,973 steps, 1.99 %</b> — <code>match</code> "
             "1.99 M, <code>upsert_node</code> 967 k, <code>find_original</code> 240 k.",
        'fix': "Same root cause as the byte-staging entry, and the compile test already prices this "
               "exact shape: a 4-byte load through a 1-aligned pointer is <b>12 instructions under "
               "<code>-mtune=rocket</code>, 2 under <code>generic-ooo</code></b>. Recoverable "
               "≈2.79 M steps — <b>1.66 % of steps, 0.81 % of COST</b>, the largest single instance "
               "of that cause anywhere in the guest.<br><br>"
               "<b>But on <code>al/zkvm-r4</code> the <code>encode_rlp</code> half is already "
               "fixed</b> — the branch arm now does <code>alignas(8) node_id_wire raw[16]; "
               "memcpy(raw, b.payload(), sizeof(raw))</code>. The 139,952 executions above are on "
               "<code>9b6fc3ed</code>, which predates it. What survives on r4 is "
               "<code>BranchView::children()</code> in <code>fold_ext_node_path_maybe</code> (two "
               "sites) and <code>read_root</code>, plus <code>find_original</code>'s single "
               "<code>b.child()</code> per level — so <b>the recoverable share on the current branch "
               "is smaller than 1.99 % and has not been re-measured.</b> Upper bound until an r4 ELF "
               "exists.<br><br>"
               "<b>Negative result worth recording:</b> <code>match</code> is emitted out of line "
               "(188,848 entries, ≈0.86 % of the guest in ABI and <code>Cases</code> "
               "materialisation), and forcing it inline <b>does not recover that</b> — measured on a "
               "reduction, <code>always_inline</code> is neutral under <code>generic-ooo</code> and "
               "<b>worse</b> under <code>rocket</code> (64 instructions against 61).",
        'num': 'encode_rlp: 37.9 % ABI, 21.3 % read_node_id, 24.0 % nibble work',
        'rem': "<b>The unexplained half of <code>encode_rlp</code> is the calling convention.</b> "
               "Every block of both instantiations classified (17,084,942 steps, 10.16 % of the "
               "guest): <b>register traffic around <code>child_ref</code> calls 37.9 %</b> "
               "(3.86 % of the guest), <code>read_node_id</code> 21.3 %, nibble extraction 17.0 %, "
               "thirty smaller blocks 16.7 %, nibble read-modify-write 6.8 %. The largest single "
               "block is 108,038 × 20 instructions of <code>sd</code>×10 + <code>jalr</code> — ten "
               "registers spilled per per-slot call, where <code>child_ref</code> itself costs "
               "<b>46 steps</b>. A 43 % surcharge on the work it protects.<br><br>"
               "<b>The nibble loop is <code>monad::mpt::concat</code></b> "
               "(<code>nibbles_view.hpp</code>): <code>for i: ret.set(index + i, arg.get(i))</code> — "
               "one nibble per iteration, with a <b>read-modify-write of the destination byte</b>. "
               "The disassembly at <code>0x80090a28</code> matches term for term, and "
               "<code>nm</code> gives exactly one symbol covering it. "
               "<code>compact_encode_raw</code> is exonerated — compiled verbatim, its "
               "<code>is_constant_evaluated()</code> arm folds away and the runtime path is the "
               "byte-wise shift-combine.<br><br>"
               "<b>The same file already knows the trick</b>: <code>compact_encode_raw</code>'s own "
               "comment says the destination is byte-aligned so it is <i>“a byte run, not a nibble "
               "run: either a straight copy or one uniform 4-bit shift”</i>. <code>concat</code> "
               "never got it. Paths run 56–59 nibbles, so a byte loop is 3–4× on <b>≈2.4 % of the "
               "guest here plus 0.71 % in <code>append_path</code></b>.<br><br>"
               "<i>Caveat:</i> the classifier also catches the once-per-call frame block, so read "
               "37.9 % as register traffic around calls, not caller spill alone. And the call chain "
               "from <code>encode_rlp&lt;true&gt;</code> to <code>concat</code> was not traced — the "
               "loop is provably inlined into that body, but the attribution to <code>concat</code> "
               "is by code shape, not by a call graph.<br><br>"
               "The <code>always_inline</code> reduction is <b>not faithful</b> — the real lambdas "
               "recurse into <code>encode_rlp</code>, very likely why the real <code>match</code> "
               "exceeds GCC's inline threshold when the reduction does not. It shows <i>no support</i> "
               "for the inlining lever, not a refutation; reviving it needs the real translation "
               "unit.<br><br>"
               "<b>Half of <code>encode_rlp</code> is still un-attributed</b> — after "
               "<code>read_node_id</code> (14 %), call setup (23 %) and nibble work (14 %), the rest "
               "is presumably RLP byte emission and was not disassembled. ~5 % of the guest, the "
               "largest unexamined thing left.<br><br><b>The <code>concat</code> rewrite is on <code>al/zkvm-r4-levers</code></b>, unbuilt. The <code>read_node_id</code> half needs nothing: <code>-mtune=generic-ooo</code> landed on <code>al/zkvm-r4</code> and compiles the 4-byte load to <b>2 instructions against 12</b>.",
    },
    {
        'id': 'overlayprobe', 'share': None, 'tag': 'BUILT',
        't': 'Hash-map probing is 3.09 % of the guest, and 97.7 % of the lookups find nothing',
        'w': "Found by disassembling the largest unclassified block in <code>encode_rlp</code>, "
             "which turned out to be <code>ankerl::unordered_dense</code>'s open-addressing probe: "
             "8-byte buckets <code>{dist_and_fingerprint, value_idx}</code>, "
             "<code>DIST_INC = 1u&lt;&lt;8</code>, hence the tell-tale <code>addiw a4,a4,256</code>. "
             "Guest-wide <b>5,197,877 steps, 3.09 %, ≈1.52 % of COST</b>.<br><br>"
             "<b>Split by map.</b> <code>OffsetTrie</code>'s two maps sit 56 bytes apart — one "
             "<code>ankerl</code> map — so the bucket-limit load discriminates them: offset 48 is "
             "<code>overlay_</code>, offset 104 is <code>hashes_</code>.<br><br>"
             "<table><tr><th>pass</th><th>map</th><th>probes</th></tr>"
             "<tr><td>mutation <code>&lt;false&gt;</code></td><td>overlay_</td><td>125,379</td></tr>"
             "<tr><td>mutation (upsert/fold)</td><td>overlay_</td><td>16,352</td></tr>"
             "<tr><td>mutation <code>&lt;false&gt;</code></td><td>hashes_</td><td>18,064</td></tr>"
             "<tr><td>constructor</td><td>hashes_</td><td>16,046</td></tr>"
             "<tr><td><b>priming <code>&lt;true&gt;</code></b></td><td><b>overlay_</b></td><td><b>0</b></td></tr>"
             "<tr><td>priming <code>&lt;true&gt;</code></td><td>hashes_</td><td>6,458</td></tr></table>",
        'fix': "<b>The probes cannot be made rarer by pass.</b> All 142,660 <code>overlay_</code> "
               "probes are on the mutation path, where the overlay is genuinely live. The priming "
               "pass touches only <code>hashes_</code>, which is what priming exists to fill.<br><br>"
               "<b>But 97.7 % of them find nothing.</b> Counting <code>find()</code> entries against "
               "probe steps: <code>overlay_</code> 72,247 calls / 142,660 probes (1.97 each), "
               "<code>hashes_</code> 33,742 / 45,812 (1.36). And <code>overlay_</code>'s calls are "
               "almost one site — <b>63,573, exactly <code>child_ref&lt;false&gt;</code>'s call "
               "count</b>: one lookup per non-digest child slot. Walking that site's control flow: "
               "739 first-bucket hits, 427 loop-exit hits, <b>62,054 misses</b>.<br><br>"
               "Cost: hash + probe loop 2,163,898 steps, miss continuation 496,432 — "
               "<b>41.8 steps per lookup, 1.58 % of the guest at that site, ≈1.80 % over all "
               "72,247</b> (≈0.88 % of COST).<br><br>"
               "<b>The fix is a filter sized to the shadow set, not to the blob.</b> A blob-sized "
               "bitmap is already priced here — the constructor's validation bitmap is what "
               "<code>ctorval</code> measured at −1.55 % / −2.74 % — so a second one costs what it "
               "saves. The shadowed ids are few enough for 4 KB.<br><br>"
               "<b>It must be a Bloom filter, not a fingerprint table.</b> A direct-mapped table of "
               "fingerprints is <b>unsound</b>: two shadowed ids colliding on a slot lose one, giving "
               "a false negative and a missed overlay entry. Insertion must only ever <i>set</i> "
               "bits. k = 1, a plain bit array indexed by a hash.<br><br>"
               "<b>Compiled and counted</b> (<code>riscv64-elf-gcc 16.1.0</code>, §12's flags), "
               "both standalone and — the shape that matters — <b>inlined into the caller's 16-slot "
               "loop</b>, which is what <code>encode_rlp</code>'s branch arm is: <b>21</b> "
               "instructions standalone with a 64-bit constant (six materialising it), <b>17</b> "
               "with a 32-bit one, <b>10</b> hoisted into the loop. <i>Not</i> the ~5 an earlier "
               "pass assumed.<br><br>"
               "<b>Once hoisted the constant's width stops mattering</b> — the 64-bit variant is 54 "
               "loop instructions against 51 and all three are prologue, i.e. 0.19 per lookup "
               "instead of 6.<br><br>"
               "Net, against the <b>measured</b> 41.8 steps (the current form is not re-derived "
               "from a compile), with false positives paying filter <i>and</i> find:<br>"
               "<table><tr><th>filter</th><th>size</th><th>FP</th><th>saving</th><th>% guest</th><th>% COST</th></tr>"
               "<tr><td><b>10 — hoisted, measured</b></td><td><b>4 KB</b></td><td>4.01 %</td><td><b>2,109,676</b></td><td><b>1.25 %</b></td><td><b>0.62 %</b></td></tr>"
               "<tr><td>10 — hoisted</td><td>8 KB</td><td>2.03 %</td><td>2,168,224</td><td>1.29 %</td><td>0.63 %</td></tr>"
               "<tr><td>17 — standalone, 32-bit K</td><td>4 KB</td><td>4.01 %</td><td>1,603,947</td><td>0.95 %</td><td>0.47 %</td></tr>"
               "<tr><td>21 — standalone, 64-bit K</td><td>4 KB</td><td>4.01 %</td><td>1,314,959</td><td>0.78 %</td><td>0.38 %</td></tr></table>"
               "<b>1.25 % of the guest, ≈0.62 % of COST</b> in the shape the code is written in. "
               "Doubling the filter buys 0.04 pt, so size is not delicate — <b>hoisting is worth "
               "0.30 pt, seven times the sizing decision</b>, and is the only part of the "
               "implementation that needs care.<br><br>"
               "An <code>if (overlay_.empty())</code> guard in <code>get_current</code> was proposed "
               "here and is <b>refuted</b>: <code>child_ref</code> already dispatches at compile "
               "time — <code>offset_trie.hpp:582</code>, "
               "<code>if constexpr (priming_pass) return get_original(id);</code> — so priming never "
               "reaches <code>get_current</code>. The optimisation exists, in a better form than the "
               "one proposed.<br><br>"
               "What survives is the number: 3.09 % of the guest goes into map probing and this "
               "route cannot reduce it.",
        'rem': "<b>The shadow set is measured: ≈1,341 entries.</b> From "
               "<code>put_node</code>'s own control flow — 1,551 calls, of which 48 take the "
               "<code>id == NULL_ID</code> mint branch (taken-count of <code>beq</code> at "
               "<code>0x80093d34</code>) and <b>1,503 write <code>overlay_[id]</code></b>. "
               "<code>fresh_id()</code> runs 162 times across its four sites, so at most 162 "
               "distinct overlay ids exist and every other target is a blob id.<br><br>"
               "<b>Repeated rewrites: 48 of 1,551 — 3.1 %.</b> <code>overlay_[id]</code> is an "
               "outlined <code>do_try_emplace</code> (<code>0x80093678</code>) with exactly two "
               "exits: <code>0x8009375c</code> returns <b>48</b> times (key found — overwrite), "
               "<code>0x8009387c</code> returns <b>1,503</b> times (insertion). The found path "
               "narrows 52 fingerprint matches to 48 true key equalities. So <b>1,503 distinct keys "
               "are created per block</b> and the shadowed blob-id set is <b>≈1,503 − 162 = 1,341</b> "
               "— measured, not bounded.<br><br>"
               "<b>~54 queries per entry</b> against 72,247 lookups — the 97.7 % miss rate follows "
               "from the ratio rather than being a quirk of one site. And it settles the width: a "
               "4 K-slot fingerprint table is <b>4 KB at load 0.34</b>, false positives ≈0.13 %, "
               "against <b>~940 KB</b> for a blob-sized bitmap on a 7.5 MB witness. Three orders of "
               "magnitude smaller for the same answer — so <code>ctorval</code>'s objection, that a "
               "blob-sized bitmap costs what it saves, does not carry to a structure sized to the "
               "content instead of the address space.<br><br>"
               "<b>The lookups themselves are not removable.</b> <code>get_current</code> means "
               "\"current bytes, overlay first\"; <code>get_original</code> means \"pre-state "
               "bytes\". <code>digest_at</code> already uses the cheap one and <code>child_ref</code> "
               "needs the expensive one. The filter makes the negative answer cheap; nothing here "
               "makes the question go away.<br><br>"
               "And on the entry above: <b>a callee's unconditional-looking work may "
               "be unreachable from the caller that matters, and <code>if constexpr</code> leaves no "
               "runtime trace to notice.</b> Reading the callee is not reading the call. The "
               "measurement that settled it — splitting probe sites by member offset — took one pass "
               "and should have come first.<br><br><b>LANDED on <code>al/zkvm-r4</code></b> as <code>751b826d3</code>. Still unbuilt on the machine it was written on — host tests and isolated codegen only.",
    },
    {
        'id': 'opcodetoll', 'share': 38.08,
        't': 'Every EVM opcode pays 9 instructions before it does anything — and both ways of '
             'reclaiming it are net negative',
        'w': "The interpreter is <b>38.1 % of the guest</b> — 61.2 M steps, 1,070,936 opcode "
             "executions, 57.2 steps/opcode. A fixed toll is paid on every one of them.<br><br>"
             "<b><code>jumpdest</code> is the clean measure</b>: the EVM defines it as a marker, it "
             "moves no data. Its handler is <b>9 instructions</b> — 2 of gas accounting "
             "(<code>addi</code> + <code>blt</code>), 6 of dispatch (read next opcode, jump-table "
             "base, ×8, add, load handler), 1 to advance the instruction pointer. "
             "<code>pop</code> costs 17 steps for what is one <code>addi x13, x13, -32</code>.<br><br>"
             "<b>9 × 1,070,936 = 9.64 M steps = 5.73 % of the guest</b>, spent before any opcode does "
             "its own work.",
        'fix': "<b>The dispatch is a host optimisation that inverted.</b> "
               "<code>instruction_table.hpp</code>'s <code>MONAD_VM_MUST_TAIL</code> threading exists "
               "to avoid indirect-branch <i>misprediction</i>. <b>ZisK has no branch predictor</b>, so "
               "that benefit is exactly zero and the six dispatch instructions per opcode are pure "
               "cost.<br><br>"
               "<b>But both ways of collecting it lose.</b> The fix would amortise the toll into the "
               "analysis pass — and measured, the guest analyses far more than it runs:<br>"
               "<table><tr><th></th><th>count</th></tr>"
               "<tr><td>instructions scanned by <code>find_jumpdests</code></td><td><b>3,759,803</b></td></tr>"
               "<tr><td>opcodes executed</td><td><b>1,070,936</b></td></tr>"
               "<tr><td><b>scanned per executed</b></td><td><b>3.51</b></td></tr></table>"
               "The scan already costs <b>7.80 % of the guest</b>. Tier 1 saves ~2 instructions per "
               "<i>executed</i> opcode and pays per <i>scanned</i> one: at +3 instructions of "
               "analysis the net is <b>−5.44 %</b>, at +5 it is −9.91 %. Tier 2 removes six per "
               "executed opcode, 6.4 M steps, against an analysis pass of the same order or worse.<br><br>"
               "<b>The 9-instruction floor stands; amortising it does not.</b> Wiring it would also "
               "mean templating <code>Intercode</code> on <code>Traits</code> — 247 sites and a "
               "node-side code cache re-keyed by EVM revision — to lose 5 %.",
        'rem': "This also re-reads <code>lazyjd</code>: it attacked the analysis side, which is "
               "where the mass actually is, and was refuted because every bytecode a block touches "
               "does get executed. True — but <i>executed</i> is not <i>executed much</i>, and 3.51 "
               "scanned per executed is the number that decides it. That ratio was never measured at "
               "the time.<br><br>"
               "<b>And the other half is closed too.</b> The scan is 14,384,459 steps (8.56 %) over "
               "956 contracts — 3,933 instructions scanned each, at 3.8 instructions per scanned "
               "instruction, against 97,359 jumps, i.e. <b>148 instructions of scan per jump "
               "performed</b>. Deferring it is <code>lazyjd</code>, measured at −0.14 / −0.36 / "
               "−0.22 %; its overhead is one branch per <code>is_jumpdest</code> × 97,359 = "
               "0.06–0.17 %, so the saving from unjumped contracts was between zero and 0.2 %. "
               "102 jumps per contract says the same thing.<br><br>"
               "Per-target validation is not soundly cheap — whether a pc sits inside PUSH data is "
               "not a local property, so validation starts from zero, and a watermark reaches the "
               "end almost at once because dispatchers jump deep early. The scan's stride is "
               "data-dependent, so it cannot be word-parallelised either. Measured on "
               "<code>9b6fc3ed</code>, block 25551991.",
    },
    {
        'id': 'staging', 'share': None, 'tag': 'LANDED',
        't': 'One <code>-mtune</code> word: the guest stages every map key on the stack, byte by byte',
        'w': "<code>bit_primitives.hpp:41</code> — <code>load64</code> is "
             "<code>__builtin_memcpy(&amp;v, p, 8)</code> on an <code>unsigned char const *</code>, "
             "alignment 1. GCC will not emit one <code>ld</code> from that, so it <b>stages the "
             "object on the stack</b>: N <code>lbu</code>, N <code>sb</code>, then <code>ld</code> "
             "back. <code>load64</code> is what <code>hash_bytes20</code>/<code>hash_bytes32</code> "
             "call — every map-key hash in the guest.<br><br>"
             "<code>monad::Address</code> derives from <code>evmc_address</code> = "
             "<code>uint8_t bytes[20]</code>, alignment 1; <code>bytes32_t</code> the same at 32. "
             "Straight from <code>do_find</code>: 20 <code>lbu</code> from the key, 20 "
             "<code>sb</code> to the stack, one <code>ld</code> back.<br><br>"
             "Guest-wide, detector at ≥4 byte-moves (≥8 silently drops every 4-byte "
             "<code>NodeId</code> copy): <b>10,208,870 steps, 6.07 %</b>, plateauing at ~6.1 %. "
             "<code>ankerl</code> maps on "
             "<code>Address</code> ~2.94 M, <code>mpt::match</code> 1.89 M, <code>upsert_node</code> "
             "867 k, <code>immer::champ</code> on <code>bytes32_t</code> 799 k, "
             "<code>access_storage</code> 427 k.",
        'fix': "<b>LANDED on <code>al/zkvm-r4</code></b> as <code>a19f4bd32</code>, route B — one "
               "word, <code>-mtune=generic-ooo</code> — and <b>validated: 504 of 504 corpus blocks "
               "give byte-identical public values</b>. The memcpy-inlining risk priced below did not "
               "materialise. Route A (inline-asm accessors) was written and then dropped: the tune "
               "produces <i>identical codegen</i> (h20 27 instructions, 4-byte load 2) without an "
               "optimiser barrier, and it is the one with 504 blocks behind it.<br><br>"
               "<b>Compiled and diffed, not assumed.</b> <code>hash_bytes20</code> reproduced "
               "verbatim, <code>riscv64-elf-gcc 16.1.0</code>, "
               "<code>-march=rv64ima -mabi=lp64 -mcmodel=medany -O3</code>:<br><br>"
               "<table><tr><th>flags</th><th>h20</th><th>h32</th></tr>"
               "<tr><td><code>-march=rv64ima</code> (current)</td><td>76</td><td>95</td></tr>"
               "<tr><td>+ <code>-mno-strict-align</code></td><td>76</td><td>95</td></tr>"
               "<tr><td><code>rv64ima_zicclsm</code>, <code>rv64ima_zbb</code></td><td>76</td><td>95</td></tr>"
               "<tr><td><b><code>-mtune=generic-ooo</code></b></td><td><b>27</b></td><td><b>29</b></td></tr>"
               "<tr><td><code>-mtune=size</code> / <code>thead-c906</code></td><td>27</td><td>29</td></tr></table><br>"
               "<b><code>-mno-strict-align</code> is inert</b> — it sets an ELF attribute and nothing "
               "else. The knob is <code>-mtune</code>: <code>rocket</code> is GCC's RISC-V default and "
               "declares slow unaligned access. One word in "
               "<code>category/core/toolchains/riscv64-elf.cmake</code>, the same file §6 wants "
               "<code>_zbb</code> in.<br><br>"
               "Source-level fallbacks, same test: inline <code>asm(\"ld\")</code> gives 29 under "
               "<i>any</i> tune; an <code>aligned(1) may_alias</code> typedef gives <b>86 — worse "
               "than doing nothing</b> under the current tune. Do not reach for the typedef.<br><br>"
               "<b>Worth 3.76 % of COST</b>, counting <i>both</i> terms — the first pass counted "
               "only MAIN and said 2.3 %. Staging is 5,015,300 <code>lbu</code> + 5,193,570 "
               "<code>sb</code>, <b>100 % memory accesses</b>, each carrying its MEMORY cost (25/32) "
               "on top of MAIN (68). Against ~638 k replacement unaligned loads at 106:<br>"
               "<table><tr><th></th><th>now</th><th>after</th><th>saved</th><th>% COST</th></tr>"
               "<tr><td>MAIN</td><td>694,203,160</td><td>43,387,672</td><td>650,815,488</td><td><b>2.79 %</b></td></tr>"
               "<tr><td>MEMORY</td><td>291,576,740</td><td>67,633,724</td><td>223,943,016</td><td><b>0.96 %</b></td></tr>"
               "<tr><td><b>total</b></td><td></td><td></td><td><b>874,758,504</b></td><td><b>3.76 %</b></td></tr></table>"
               "<b>This generalises</b>: sizing any lever in MAIN alone understates it whenever the "
               "lever removes loads or stores — by 37 % here.",
        'rem': "<b>No guest build was run.</b> The instruction counts are assembly-level fact; the "
               "COST figure is arithmetic on top. And <code>-mtune</code> moves scheduling, branch "
               "costs and inline thresholds too — the whole-guest effect is not only this lever and "
               "could go either way on the rest. A build plus <code>ziskemu -X</code> settles it and "
               "nothing short of it does. Measured on <code>9b6fc3ed</code>: the "
               "<code>push&lt;20&gt;</code>/<code>push&lt;32&gt;</code> share (752 k) is probably "
               "already taken by <code>load_be_k</code>.",
    },
    {
        'id': 'memcounters', 'share': None, 'tag': 'ASK',
        't': 'MEMORY is 7.51 % of COST and the counters that would explain it are collected, then '
             'discarded',
        'w': "The <code>-X</code> report gives <code>MEMORY 1,749,239,259</code> and nothing else. "
             "Counted from the disassembly: <b>68,170,925 accesses</b> — 39,997,819 reads, "
             "28,173,106 writes — at an average of <b>25.7 cells each</b>.<br><br>"
             "<code>emu_costs.rs</code> charges aligned word read/write <b>16/18</b>, byte "
             "<b>25/32</b>, and <b>unaligned word read 106 or 159, unaligned write 159 or 265</b> — "
             "an unaligned read is <b>6.6x</b> an aligned one.<br><br>"
             "Priced as if every access were aligned the mix comes to 1,349,603,729. The report says "
             "1,749,239,259: <b>+399,635,530, +22.8 %</b>, which is unaligned access plus the memory "
             "the precompiles do on their own account. <b>That split cannot be made from outside</b>, "
             "and it is exactly the number the byte-staging lever needs — converting a byte-staged "
             "load into a word load trades steps for +90 or +143 cells of unaligned memory.",
        'fix': "<b><code>mem_operations_stats.rs:17-31</code> already keeps the split</b>, per zone: "
               "<code>mread_a</code>, <code>mwrite_a</code>, <code>mread_na1</code>, "
               "<code>mwrite_na1</code>, <code>mread_na2</code>, <code>mwrite_na2</code>, "
               "<code>mread_byte</code>, <code>mwrite_byte</code>. Every run computes them; the "
               "report prints only their weighted sum.<br><br>"
               "<b>Printing them is one line and settles two open questions at once</b> — the "
               "composition of MEMORY, and byte-staging's exchange rate. Cheapest open item in this "
               "document.<br><br>"
               "<b>And opening MEMORY already corrected a delivered lever.</b> Byte-staging was "
               "sized in MAIN alone at 2.3 % of COST; its 10.2 M steps are <i>all</i> memory "
               "accesses, so they carry 25/32 cells of MEMORY on top of 68 of MAIN. Counting "
               "both, it is <b>3.76 %</b> — a 37 % correction upward. Every lever here that "
               "removes loads or stores is understated the same way.<br><br>"
               "<b>Two structural notes while here.</b> <code>BASE_COST = (21 &lt;&lt; 21) + "
               "(119 &lt;&lt; 21) = 293,601,280</code> matches the report's BASE line exactly: it is "
               "the ROM AIR plus the virtual tables and does <i>not depend on the block</i> — 1.26 % "
               "here, several times that on a small block. And <code>MemAlign(N: 2**21)</code> plus "
               "three <code>MemAlignByte(N: 2**22)</code> aliases exist solely for unaligned and "
               "sub-word access, so they carry the first-instance tax: MemAlign alone is "
               "<b>0.48 % of a block's COST</b> to exist. They are already instantiated on this "
               "guest, so byte-staging would fill them rather than create them — the cheap "
               "direction, but a term the earlier sizing did not contain.",
        'rem': "The 28- and 53-column figures are <b>inferred</b> from the cost constants' comments "
               "(<code>Dual RAM 28 cols</code>) and factorisation (<code>53 * 2</code>), not read "
               "from a generated stark_info. The aligned-model fit assumes 32- and 16-bit accesses "
               "price as words. <b>Instance counts for the Mem AIRs are not computed at all</b> — "
               "the rows-per-op under <code>dual_mem</code> was not established, and guessing it is "
               "how the last four traps happened.",
    },
    {
        'id': 'airpad', 'share': None, 'tag': 'ASK',
        't': 'The prover pays committed cells, not the cells COST reports — and the first op of a '
             'precompile costs a whole instance',
        'w': "<b>Every figure in this document comes from the COST model, and the COST model counts "
             "cells <i>used</i>.</b> The prover commits instances x rows x columns, and an instance "
             "is proven at full size however little of it is filled.<br><br>"
             "Instance capacity comes from each state machine, AIR size from <code>pil/zisk.pil</code>, "
             "and the column count factors out of the cost constants, which are written "
             "<code>clocks x width</code> — <code>KECCAK_COST = 25 * 3022</code>, "
             "<code>SHA256_COST = 72 * 121</code>. Keccakf worked through: 2^17 rows, CLOCKS 25, and "
             "<code>keccakf.rs:42-48</code> drops the remainder and one more cycle = <b>5,241 "
             "permutations per instance</b>.<br><br>"
             "<table><tr><th>AIR</th><th>ops</th><th>inst</th><th>fill</th></tr>"
             "<tr><td>Main</td><td>168,112,619</td><td>41</td><td>97.76 %</td></tr>"
             "<tr><td>Keccakf</td><td>90,856</td><td>18</td><td>96.27 %</td></tr>"
             "<tr><td>ArithEq</td><td>119,568</td><td>2</td><td>91.22 %</td></tr>"
             "<tr><td><b>Sha256f</b></td><td><b>1</b></td><td>1</td><td><b>0.03 %</b></td></tr></table>"
             "Over those four, <b>575,775,580 cells of padding — 2.47 % of the block's COST</b>. The "
             "binary family adds ~1.39 % more on an unverified one-row-per-op assumption; Mem, "
             "MemAlign, Arith and the tables are not modelled.",
        'fix': "<b>One SHA-256 in the whole block costs 31.7 M cells — 3,640x its priced cost of "
               "8,712.</b> Nothing in COST shows it, and no profile by symbol or opcode shows it "
               "either. The tax on touching a precompile <i>at all</i>:<br>"
               "<table><tr><th>AIR</th><th>cells</th><th>% of a block's COST</th></tr>"
               "<tr><td>Keccakf</td><td>396,099,584</td><td><b>1.70 %</b></td></tr>"
               "<tr><td>Add256</td><td>109,051,904</td><td>0.47 %</td></tr>"
               "<tr><td>ArithEq</td><td>93,323,264</td><td>0.40 %</td></tr>"
               "<tr><td>ArithEq384</td><td>82,837,504</td><td>0.36 %</td></tr>"
               "<tr><td>Blake2br</td><td>53,739,520</td><td>0.23 %</td></tr>"
               "<tr><td>Sha256f</td><td>31,719,424</td><td>0.14 %</td></tr>"
               "<tr><td>Poseidon2</td><td>9,830,400</td><td>0.04 %</td></tr></table>"
               "A block containing one BLS pairing, one BLAKE2 and one SHA-256 pays 0.73 % of its "
               "COST for having touched them at all. Sporadic precompile use is priced as heavy "
               "use.<br><br>"
               "<b>The ask: variable instance sizes.</b> Each precompile declares one N and the "
               "planner emits <code>ceil(ops / capacity)</code> of exactly that size. A smaller "
               "variant chosen at plan time from the op count would turn 3,640x into near 1x. "
               "<code>Mem</code> already shows the mechanism, declaring three sizes under three "
               "aliases; what is missing is choosing between sizes for the <i>same</i> machine. "
               "Fourth ask, and the only one that came from reading the prover rather than the guest.",
        'rem': "<b>Static, not measured.</b> Op counts against declared AIR sizes and planner "
               "arithmetic; no prover was run and nothing here is checked against an actual "
               "<code>GENERATING_INNER_PROOFS</code> breakdown — that check is one instrumented run "
               "away. The binary-family rows-per-op is assumed, not read. And proving time is not "
               "proportional to committed cells alone: column count, constraint degree and the "
               "commit/FFT split differ per AIR. This sizes area, and inherits the COST model's own "
               "caveat.",
    },
    {
        'id': 'frops', 'share': None,
        't': 'ZisK\'s frequent-ops table was never aimed at this workload — 10.5 % of COST',
        'w': "ZisK discharges an operation by lookup when its <code>(op, a, b)</code> triple is in a "
             "precomputed table, and <code>ziskemu -X</code> reports the hit rate. On block 25551991 "
             "the tables already save <b>6.7 % of COST</b> and <b>10.5 % is still missing</b> — ops "
             "that were eligible and did not hit.<br><br>"
             "<b>The contents are hand-written constants.</b> "
             "<code>state-machines/binary/src/binary_basic_frops.rs</code> picks the ranges by hand "
             "— <code>MAX_A_LOW_VALUE = 386</code>, <code>MAX_ADD_MINUS_ONE = 24628</code>, address "
             "windows <code>0xA010_0000..0xA020_0000</code>. Nothing derived them from a profile, "
             "and the hit rates say so: <code>add</code> <b>6.7 %</b> over 25 M operations "
             "(2.51 % of COST missing), <code>and</code> 27.9 %, <code>xor</code> 11.0 % — against "
             "<code>eq</code> 74.9 % and <code>srl_w</code> 96.1 %.<br><br>"
             "Measured on both r4 generations, which agree within 0.6 pt (10.46 % on "
             "<code>9b6fc3ed</code>, 11.07 % on <code>9a5378dc</code>). It is a property of the "
             "table and the workload, not of a guest generation — so unlike the rest of this "
             "document it should survive a rebase.",
        'fix': "<b>Upstream ask, and the cheapest of the three</b> — it costs ZisK data, not "
               "constraints, and changes neither proof size nor security.<br><br>"
               "The ask is <b>re-target, not enlarge</b>: at a constant number of entries, which "
               "triples go in is a free choice, and the current one was made without an Ethereum "
               "profile. A histogram of <code>(op, a, b)</code> over the corpus is ours to produce "
               "and hand over.<br><br>"
               "Two things to say when sending it, or the number will be read as a promise: "
               "<b>10.5 % is a ceiling at 100 % hit rate</b>, not an achievable gain, and the "
               "marginal proving cost of a larger table has not been measured. Whether the AIR pads "
               "to the next power of two — which would make some enlargement free — is a question "
               "for them, not a claim of ours.",
    },
    {
        'id': 'decnum', 'share': 0.11, 'tag': 'BUILT',
        't': 'The last uncovered symbol — and it was the encode-side defect on the inverse operation',
        'w': "The whole-guest sweep left exactly one symbol above 0.046 % with no lever entry: "
             "<code>AccountLeafView::account</code>, <b>145,258 steps, 701 calls, 207 each</b>. It "
             "decodes three fields out of a fixed-layout blob node — a 33-byte RLP string into a "
             "<code>bytes32_t</code>, a <code>uint64_t</code> nonce, a <code>uint256_t</code> "
             "balance.<br><br>"
             "207 steps for that is a lot, and the block profile says where. Two <b>memcpy calls</b> "
             "per account, and an 85-instruction block running 354 times that is "
             "<code>bswap(uint256_t)</code> — the <code>lui 0xff0000</code>/<code>slli</code>/"
             "<code>and</code>/<code>or</code> dance across all four words. Both come from one "
             "function:<br><br>"
             "<code>T result{}; memcpy(&amp;as_bytes(result)[sizeof(T) - enc.size()], enc.data(), "
             "enc.size()); return bswap(result);</code><br><br>"
             "The memcpy length is a <b>runtime value</b>, so gcc emits a real call — "
             "<code>dma_memcpy</code>, ~950 COST — for a copy that is usually one to eight bytes. "
             "And the byte-swap runs over the <b>full width</b>: four <code>bswap64</code> for a "
             "uint256 even when three of its words are zero, which for a nonce or a balance is the "
             "common case.",
        'fix': "<b>This is <code>f4c0373fe</code> again, on the other side.</b> Same defect — work "
               "proportional to the <i>type's</i> width rather than the <i>value's</i> — on the "
               "inverse operation, sitting uncovered for two more sweeps because a ranking shows a "
               "decoder doing decoder things.<br><br>"
               "Whole words now go through one load and one <code>bswap64</code> with a "
               "constant-size memcpy that inlines; the short remainder, which is all there is for a "
               "small value, goes byte at a time. <b>No call left in either instantiation</b> — 202 "
               "instructions for uint256, 131 for uint64, counted after assembly. "
               "<code>e49cdb7bf</code>.<br><br>"
               "It reaches past this one symbol: <code>decode_raw_num</code> is every RLP integer "
               "the guest decodes. <code>decode_unsigned&lt;uint256_t&gt;</code> alone is 1,477 "
               "out-of-line calls and 142,999 steps, plus the copies inlined into "
               "<code>AccountLeafView::account</code> and the transaction decoders.",
        'rem': "Byte-identical: <b>14,624 encodings</b> — every length 1..32 against every non-zero "
               "leading byte, 200 random per length, all-<code>0xff</code> and one-followed-by-zeros "
               "— checked old against new <i>and</i> old against an independent big-endian "
               "reference. 0 divergences on both.<br><br>"
               "What remains in <code>AccountLeafView::account</code> after this is RLP generality "
               "over a layout the node format already fixes — <code>decode_bytes32</code> parses "
               "string metadata for a field that is always <code>0xa0 &#8214; 32</code>. That check "
               "is load-bearing (nothing else validates the tag byte, and a garbage code_hash turns "
               "a contract call into an empty one), so it stays.",
    },
    {
        'id': 'rawroot', 'share': None, 'tag': 'BUILT',
        't': 'Root the transactions against the bytes they were decoded from',
        'w': "The transactions-root check re-encoded every transaction it had just decoded and "
             "hashed that. Recorded here for two sessions as blocked on a soundness question — "
             "&ldquo;only if the decoder rejects non-canonical RLP&rdquo;. <b>The gate pointed the "
             "wrong way.</b><br><br>"
             "<i>Re-encode and hash</i> proves &ldquo;what I executed, canonically re-encoded, "
             "hashes to the committed root&rdquo;. That admits any input whose re-encoding is "
             "canonical — precisely every case where decode is lossy.<br>"
             "<i>Hash the source bytes</i> proves &ldquo;the bytes I decoded from hash to the "
             "committed root&rdquo;, and the transactions executed came from exactly those slices by "
             "construction. It rests on nothing about <code>encode &compfn; decode</code>.<br><br>"
             "The second is strictly stronger and needs <b>no canonicity argument at all</b>. "
             "Auditing the decoder for the question that turned out not to matter did find one real "
             "defect — the blob-versioned-hash loop consumed 33 bytes per iteration against a guard "
             "testing 32 and never checked the list was empty, dropping 1&ndash;31 trailing bytes "
             "across 124 shapes (<code>be2e0cc12</code>).",
        'fix': "<code>decode_transaction_list</code> gains an out-parameter for the slices; the "
               "one-argument form delegates with <code>nullptr</code>, so no other caller changes. "
               "The slices are free — legacy: the span the decoder consumed, list plus header; "
               "typed: the <code>parse_string_metadata</code> result, already the unwrapped "
               "<code>type &#8214; payload</code> the trie holds. Captured <i>before</i> decoding, "
               "since <code>decode_transaction_eip2718</code> advances the view it is given.<br><br>"
               "<b>And the tool was already in the tree</b>: <code>parse_list_metadata_raw</code>, "
               "commented &ldquo;useful when the caller needs to re-emit or hash the list in its "
               "original wire form&rdquo;, used only in a test.<br><br>"
               "Validated against the block: <b>258 slices, 213 typed and 45 legacy</b> — exactly "
               "what the profile shows for <code>encode_eip2718_base</code> (426 calls, two per "
               "transaction) and <code>encode_legacy_base</code> (90). The spans tile the list with "
               "no gap, 128,879 of 128,879 bytes. <b>~120,000 steps, ~0.05 % of COST</b>; "
               "<code>e8303d275</code>.",
        'rem': "The steps are the smaller half of this. What it buys is that the root check no "
               "longer depends on a property of the encoder/decoder pair — and that property had a "
               "live counterexample in the same file.<br><br>"
               "<code>28fc8086e</code>'s <code>tx_bases</code> vector goes with it: it existed to "
               "share the pre-signature encoding between the signing hash and the root, and the root "
               "no longer wants one. <code>recover_sender</code> returns to its one-argument form; "
               "the base-taking overloads stay for the node.",
    },
    {
        'id': 'rlpdup', 'share': 2.91, 'tag': 'BUILT',
        't': 'RLP, reopened twice — every transaction encoded twice, and 24 zero bytes walked one at a time',
        'w': "This family was called flat twice, on the grounds that no single symbol in it exceeds "
             "0.42 %. That judged it on its most expensive <i>symbol</i>. Two things were visible by "
             "following <b>callers</b> and <b>the distribution of the values</b>, neither of which a "
             "ranking shows.<br><br>"
             "<b>Every transaction is encoded twice.</b> <code>encode_legacy_base</code> and "
             "<code>encode_eip2718_base</code> are each called from "
             "<code>encode_transaction_for_signing</code> (for <code>recover_sender</code>) <i>and</i> "
             "from <code>encode_transaction</code> (for the transactions root) — <b>426 + 90 calls "
             "for 258 transactions</b>. Both are <code>&lt;base&gt; &#8214; &lt;what follows&gt;</code> "
             "in a list header, and the base is the same bytes, which is why those two functions "
             "exist separately.<br>"
             "<table><tr><th></th><th>steps</th></tr>"
             "<tr><td>the two base encoders</td><td>173,596</td></tr>"
             "<tr><td><code>encode_unsigned&lt;uint256_t&gt;</code> inside them</td><td>432,566</td></tr>"
             "<tr><td><code>encode_access_list</code></td><td>50,474</td></tr>"
             "<tr><td><code>encode_string2</code> over tx.data</td><td>28,380</td></tr>"
             "<tr><td><b>two passes</b></td><td><b>~685,000</b></td></tr></table>"
             "<b>And <code>to_big_compact</code> has the wrong shape for its inputs.</b> It "
             "byte-swaps the whole 256-bit value and then walks the leading zeros off one "
             "<code>lbu</code> at a time — 35 % and <b>53 %</b> of "
             "<code>encode_unsigned&lt;uint256_t&gt;</code> respectively, 47 loop iterations per "
             "call — because <b>the average uint256 RLP field carries 24 leading zero bytes</b>.",
        'fix': "<b>Both taken.</b> <code>28fc8086e</code> takes the base once and hands it to both "
               "consumers: <b>~342,500 steps, 0.26 % of the guest, 0.12 % of COST</b>. "
               "<code>f4c0373fe</code> finds the top non-zero word with four compares, swaps only "
               "the words at or below it and takes the tail — ~70 instructions against ~200, "
               "<b>0.15 % of the guest, 0.07 % of COST</b>, and zero stops being the worst case "
               "(six instructions instead of walking 32 bytes).<br><br>"
               "Byte-identical both times: the transaction diff replaces five "
               "<code>encode_*_base(txn)</code> call sites with <code>base</code> and adds nothing "
               "else; <code>to_big_compact</code> checked on 24,841 values three ways — old against "
               "Python's minimal big-endian, new against Python, old against new — 0 divergences.",
        'rem': "What is genuinely at the floor: <code>encode_unsigned</code> is now bounded by "
               "<code>countl_zero</code> and <code>bswap64</code>, both software for want of Zbb, and "
               "storing bytes instead of swapping is <i>worse</i> (1,888 against 1,446, and dropping "
               "the mask makes it worse still — a byte store from a dirty register is 106, not 32). "
               "The receipt encoders' tree of <code>std::string</code>s accounts for 18,997 of the "
               "guest's 65,529 <code>operator new</code> calls, but that allocator is a bump pointer "
               "at 15 steps — 0.1 % — and the copies go through <code>dma_memcpy</code>. <b>The "
               "structure is ugly; the cost is not.</b><br><br>"
               "Still open, needing one question answered: the transactions root could use the "
               "witness's own bytes instead of re-encoding (another 0.12 %), <i>if</i> the decoder "
               "rejects non-canonical RLP. Unverified, and a non-minimal encoding accepted would put "
               "the root on different bytes than the chain commits to.",
    },
    {
        'id': 'statejournal', 'share': 0.92, 'tag': 'BUILT',
        't': 'The state journal — logs come off immer, but pop_accept is load-bearing',
        'w': "<b>Logs.</b> <code>logs_</code> was "
             "<code>VersionStack&lt;immer::vector&lt;Log&gt;&gt;</code>, immer chosen so that opening "
             "a frame copies the log list in O(1). Sound reasoning — but a watermark copies it in "
             "O(1) too, for a <code>size_t</code>, and immer charges on <b>every append</b>: a "
             "node-path allocation and an rbtree descent. 193,316 steps in "
             "<code>store_log</code> (701 calls at 275.8), 117,747 in immer's rbtree, 103,679 in the "
             "deque of versions.<br><br>"
             "<b><code>pop_accept</code>.</b> 2,054 calls, <b>1,207,733 steps, 588 each</b> — against "
             "<code>State::push</code> at 46.9. It walks 5,812 dirty addresses (2.83 per frame) at "
             "208 steps each, and <b>33.4 % of that is container index arithmetic</b> — "
             "<code>segmented_map</code> segments and <code>deque</code> chunks. The map <i>find</i> "
             "is not even in the total; it sits in "
             "<code>unordered_dense::table&lt;Address, VersionStack&lt;AccountState&gt;&gt;</code>.",
        'fix': "<b>Logs: taken</b> (<code>f1a98ffe0</code>). Append-only within a transaction, and a "
               "rejected frame discards exactly what it appended — so a flat vector plus one "
               "watermark per open frame is the whole journal. <b>~300,000 steps, 0.23 % of the "
               "guest, 0.10 % of COST</b>; what survives is the Log copy itself. No call site "
               "changes (both consumers iterate, the tests call <code>.size()</code>) and "
               "<code>State</code> already deletes copy and move. Modelled against "
               "<code>version_stack.hpp</code> over 200,000 nested frame sequences and 4,087,397 "
               "operations, comparing the visible log list after every one — 0 divergences.<br><br>"
               "<b><code>pop_accept</code>: not taken, and both exits are closed.</b>",
        'rem': "<b>Skipping the relabel is unsound.</b> <code>VersionStack::pop_accept</code> "
               "relabels the top entry from <code>version</code> to <code>version - 1</code>. Without "
               "it the label stays high, a later <code>current(lower)</code> finds "
               "<code>lower &gt; back().first</code> false and returns the <i>existing</i> entry, and "
               "a subsequent reject then looks for its own version and misses — a later frame's "
               "writes silently merged into an older frame's entry.<br><br>"
               "<b>A flat dirty-journal is equivalent and still a loss.</b> Modelled against the real "
               "<code>deque&lt;Set&lt;Address&gt;&gt;</code> over 300,000 sequences and 4,646,840 "
               "operations, comparing every account's whole version stack: 0 divergences. Then "
               "priced: <code>current_account_state</code> runs <b>15,322</b> times and "
               "<code>pop_accept</code> walks <b>5,812</b> — the set collapses touches into distinct "
               "addresses <b>2.64 to 1</b>, before the expensive per-address find. Dropping it trades "
               "5,812 set inserts for 9,510 extra map finds: <b>&minus;261,540 against +637,000, net "
               "+375,000 steps</b>.",
    },
    {
        'id': 'divhint', 'share': 2.51, 'tag': 'BUILT',
        't': 'ZisK checks divisions, it does not do them — and zisklib exports the checker to C',
        'w': "<code>udivrem&lt;4,4&gt;</code> is <b>18,367 calls, 3,284,818 steps, 1.14 % of the "
             "guest's COST</b> — 223.4 M MAIN + 5.0 M MEMORY, <b>12,434 per call</b>. It is Knuth's "
             "algorithm D in software, and <b>31 of its 179 instructions per call are the prologue "
             "spilling thirteen callee-saved registers</b> before any dividing starts. ~1,996 calls "
             "reach the full Knuth loop; the rest take <code>long_div</code> or the trivial "
             "<code>m &lt; n</code> return, and even those average 164 steps.<br><br>"
             "<b>ZisK's own 256-bit division does not divide.</b> It hints and verifies: "
             "<code>fcall_uint256_div</code> returns (q, r) as a free input, then one "
             "<code>arith256</code> — a 256x256&rarr;512 multiply, <b>1,424</b> COST — checks "
             "Euclid's lemma, <code>q&middot;b + r == a</code> with a zero high half, and "
             "<code>r &lt; b</code>.",
        'fix': "And the whole routine, <i>verification included</i>, is exported "
               "<code>#[no_mangle] extern \"C\"</code>: <code>div_rem256_c</code>, "
               "<code>wrapping_div256_c</code>, <code>checked_div256_c</code>. <b>Present at the tag "
               "the guest pins</b> (<code>ziskos v1.0.0-alpha</code>) — which is what makes this a "
               "patch rather than an ask.<br><br>"
               "So the guest writes a declaration, a zero-divisor guard and a call in "
               "<code>udivrem(uint256_t, uint256_t)</code> — the overload "
               "<code>operator/</code>, <code>operator%</code> and <code>sdivrem</code> all funnel "
               "through. 34 instructions of glue, counted after assembly.<br>"
               "<table><tr><th></th><th>per call</th></tr>"
               "<tr><td>software Knuth</td><td><b>12,434</b></td></tr>"
               "<tr><td>hinted (34 glue + ~56 zisklib + arith256 + staging)</td><td><b>~7,944</b></td></tr>"
               "<tr><td>saving</td><td><b>82 M, 0.41 % of COST</b></td></tr></table>"
               "<b>BUILT</b> as <code>d220f5d5c</code>.",
        'rem': "The verification is deliberately <b>not</b> written in the guest. Getting Euclid's "
               "lemma subtly wrong — checking the low half and forgetting the high one — lets a "
               "prover claim any quotient it likes. That code is zisklib's and is tested there.<br><br>"
               "The weak term in the estimate is zisklib's own instruction count; only a build "
               "settles it. Two things a build must confirm beyond the state root: that "
               "<code>div_rem256_c</code> resolves at all (if ziskos is ever compiled with its "
               "<code>hints</code> feature the symbol becomes <code>hints_div_rem256_c</code> and "
               "the link fails — loudly, which is the good case), and the routine's real cost.<br><br>"
               "Error behaviour is unchanged: the guard routes only <code>v != 0</code>, so a zero "
               "divisor still aborts through <code>MONAD_ASSERT(n)</code> from where it does today. "
               "<code>addmod</code>/<code>mulmod</code> are untouched.<br><br>"
               "Marshalling tested against <b>60,121 vectors</b> whose quotients and remainders come "
               "from Python's arbitrary precision — every divisor width 1..4 words, a&lt;b, a==b, "
               "a==b&plusmn;1, exact multiples, and the 2^k / 10^18 shapes real contracts divide by. "
               "0 result mismatches, 0 operand-order faults.",
    },
    {
        'id': 'jdscan', 'share': 9.63, 'tag': 'BUILT',
        't': 'The JUMPDEST scan reads two bytes per opcode where one will do',
        'w': "<code>Intercode::find_jumpdests</code> is the hottest function in the guest — "
             "<b>12,544,700 steps, 9.57 %</b> — because it visits every opcode of every distinct "
             "contract a block touches: 1,935,892 positions over 3,467,005 bytes.<br><br>"
             "Its loop was six instructions with <b>two</b> <code>lbu</code>: read the opcode, index "
             "the 256-entry advance table, read the advance. <code>codelazy</code> established that "
             "none of this work can be skipped — the codes are all served — so the only thing left "
             "is the price per position.",
        'fix': "An unsigned range test is the <i>same six instructions</i> with one load:<br>"
               "<table><tr><th>arm</th><th>share</th><th>instructions</th></tr>"
               "<tr><td>non-PUSH</td><td>73.7 %</td><td><code>lbu / addi / bgeu / beq / addi / bltu</code></td></tr>"
               "<tr><td>PUSH</td><td>26.3 %</td><td><code>lbu / addi / bgeu / addi / add / bltu</code></td></tr></table>"
               "At 25 per byte load on top of 68 per instruction that is <b>25 per position, "
               "48.4 M, 0.24 %</b> — uniform across both arms. <b>BUILT</b> as "
               "<code>fff053f47</code>.<br><br>"
               "The arithmetic is 64-bit on purpose: as <code>unsigned</code> the advance needs a "
               "<code>slli</code>/<code>srli</code> pair to widen for the pointer add, which puts "
               "the PUSH arm back at eight. Equivalence: identical maps on all 384 witness "
               "bytecodes, plus 36,572 synthetic cases (every length to 600 with PUSH/JUMPDEST-dense "
               "fills, every opcode alone, every opcode leading 40 JUMPDESTs).",
        'rem': "<b>This is not a revert of <code>c4b1f9797</code>.</b> The table beat what preceded "
               "it, which paid two range compares <i>and</i> a subtract <i>and</i> its own load. It "
               "does not beat one range test — and the reason that was invisible for two months is "
               "that the two shapes have identical instruction counts and differ only in memory "
               "ops, which is the axis <code>memcost</code> had to be measured to see.<br><br>"
               "Ideas that do not work, so nobody re-derives them: a SWAR pass over 8 bytes needs a "
               "window with no PUSH in it and the average advance is 1.79 bytes; a JUMPDEST sentinel "
               "past the code would remove the bound check but the padding must stay zero for the "
               "interpreter, and a PUSH near the end skips over it anyway; folding the table base "
               "into the load's immediate needs it below address 2048, which is not mapped.",
    },
    {
        'id': 'calldatacount', 'share': 0.44, 'tag': 'BUILT',
        't': 'Every transaction has its calldata counted four times',
        'w': "EIP-2028 and EIP-7623 price zero and non-zero calldata bytes differently, so both "
             "<code>intrinsic_gas</code> and <code>floor_data_gas</code> need the split — and every "
             "transaction goes through both twice, once to validate and once to execute. "
             "<code>tokens_in_calldata</code> ran <b>1,032 times for 258 transactions</b>: 578,740 "
             "steps, three quarters of it recounting bytes it had already counted.<br><br>"
             "The scan itself is fine — 12 instructions per 8 bytes, constants correctly hoisted out "
             "of the loop, 104 COST per byte. There are just four of them.",
        'fix': "<code>CalldataTokens{zeros, nonzeros}</code> taken once in "
               "<code>ExecuteTransactionNoValidation</code>'s constructor and handed down; "
               "<code>static_validate_transaction</code> takes it as a <b>required</b> parameter. "
               "<b>404,892 steps, 27.5 M, 0.14 %.</b> <b>BUILT</b> as <code>4c7b24690</code>.<br><br>"
               "Required rather than defaulted, because a default lets a caller silently keep paying "
               "for a recount — the ten call sites, including seven tests, now say what they are "
               "counting. Two names rather than an overload "
               "(<code>intrinsic_gas</code>/<code>intrinsic_gas_counted</code>): "
               "<code>EXPLICIT_TRAITS</code> instantiates through <code>decltype(f&lt;traits&gt;)</code>, "
               "which an overload set makes ambiguous.<br><br>"
               "Not one gas unit moves: 32,776 calldata shapes — every length to 4096, random, "
               "all-zero and all-non-zero fills — give identical intrinsic and floor gas.",
        'rem': "The tempting shortcut is wrong and stays wrong: memoising on "
               "<code>(tx.data.data(), tx.data.size())</code> breaks on the node, which recycles "
               "transaction buffers across blocks, so a stale hit mis-prices gas and produces a "
               "wrong state root. The counts had to be threaded, not cached.",
    },
    {
        'id': 'popcnt', 'share': 0.96, 'tag': 'BUILT',
        't': 'immer calls libgcc\'s popcount 41,909 times a block, and twelve of its thirty instructions are constants',
        'w': "riscv64ima has no <code>cpop</code>, so every <code>__builtin_popcountll</code> is a "
             "call into libgcc. immer's HAMT makes one per level of every lookup and every insert: "
             "<b>41,909 calls, 1,257,270 steps, 0.96 % of the guest</b>.<br><br>"
             "The helper is 30 instructions, and <b>twelve of them rebuild the four SWAR "
             "constants</b> from <code>lui/addi/slli/add</code> — the same thing "
             "<code>fmixk</code> found in the hash. <code>bits::popcount64</code> had it too, which "
             "is exactly why its own note said it &ldquo;barely pays&rdquo;. That note was right "
             "about the measurement and wrong about the reason.",
        'fix': "Fetch the constants and define the symbol in the guest: <b>19 instructions and four "
               "loads against 30, 684 COST per call, 28.7 M, 0.14 %</b>. <b>BUILT</b> as "
               "<code>659563030</code>.<br><br>"
               "In the guest's libc shim rather than by patching a caller, because libgcc is a "
               "static archive searched after it — the mechanism <code>malloc</code>/<code>free</code> "
               "already use — and it composes with the immer hunk in "
               "<code>third_party/patches</code>: if that is ever applied, immer inlines its own copy "
               "and this goes uncalled. The fetch helper is now shared as <code>bits::imm64</code> "
               "between <code>popcount64</code> and <code>fmix64</code>, 154,334 calls a block "
               "between them.",
        'rem': "<b>A passing build does not demonstrate this one works.</b> If link order ever put "
               "libgcc first the override does nothing at all, results stay correct, and 0.14 % is "
               "quietly not saved. The check is a disassembly of the shipped ELF — 19 instructions "
               "with loads off a shared anchor is ours, 30 with three <code>lui</code> is libgcc's — "
               "and it is written into the source beside the definition.<br><br>"
               "It also nearly did not work at all: <code>uint64_t const k3 = imm64(...)</code> "
               "folded the constant straight back to an immediate, because gcc constant-evaluates a "
               "const-initialised local and that takes <code>if !consteval</code> down its false "
               "branch. Any such trick has to be checked in the emitted code.",
    },
    {
        'id': 'blobfee', 'share': 0.76, 'tag': 'BUILT',
        't': 'The blob base fee is recomputed on every message frame — 258 times per block',
        'w': "Assigning <i>every</i> symbol in the build to a family left 4.27 M steps of "
             "&ldquo;everything else&rdquo; that no lever entry claimed. The largest thing in it was "
             "<code>get_base_fee_per_blob_gas</code> at <b>989,836 steps, 0.76 % of the guest</b> — "
             "sitting at rank ~27, reading like a getter.<br><br>"
             "262 calls, <b>258 of them from <code>get_tx_context</code></b>, which the evmc host "
             "interface asks for on every message frame. Each one is <code>fake_exponential</code>: "
             "53 iterations of a 256-bit Taylor series, <b>3,778 steps</b>. Both arguments — the "
             "parent header's <code>excess_blob_gas</code> and the schedule's "
             "<code>blob_base_fee_update_fraction</code> — are fixed for the whole block.",
        'fix': "A one-entry memo. The key cannot change within a block and changes once between "
               "blocks, and it is a <b>complete</b> key: <code>fake_exponential</code> reads nothing "
               "from the schedule but the update fraction, so two calls that agree on "
               "(excess, fraction) cannot disagree on the result. That completeness is the only "
               "thing that makes a memo safe here.<br><br>"
               "<code>MONAD_THREAD_LOCAL</code> rather than a bare static — on the node this runs on "
               "execution threads, and the macro already expands to nothing in the zkVM mirror. The "
               "struct is constant-initialised, so it lands in <code>.bss</code> with no guard "
               "variable and no atexit registration, neither of which the bare-metal guest has.<br><br>"
               "Hit path ~10 instructions: <b>~968,000 steps, 65.8 M COST, 0.33 %</b>. "
               "<b>BUILT</b> as <code>12c695bcf</code>.",
        'rem': "<b>NOT BUILT for the guest.</b><br><br>"
               "The sibling finding, <i>not</i> taken: <code>tokens_in_calldata</code> runs "
               "<b>1,032 times for 258 transactions</b> — <code>intrinsic_gas</code> and "
               "<code>floor_data_gas</code> each call it, and each of those is called once in "
               "<code>validate_transaction</code> and once in <code>execute_transaction</code>. One "
               "scan per transaction instead of four is <b>404,892 steps, 27.5 M, 0.14 %</b>. It "
               "needs a new parameter on two public <code>EXPLICIT_TRAITS</code> functions or the "
               "pair threaded across two files of consensus-critical gas accounting — not worth "
               "writing blind. <b>And the tempting shortcut is wrong:</b> memoising on "
               "<code>(tx.data.data(), tx.data.size())</code> breaks on the node, which recycles "
               "transaction buffers across blocks, so a stale hit mis-prices gas and produces a wrong "
               "state root.",
    },
    {
        'id': 'sweep', 'share': None, 'tag': 'PRICED',
        't': 'Whole-guest accounting — 100 % assigned, 180 steps unexplained',
        'w': "Every symbol in the current build assigned to a family, so that &ldquo;what is "
             "left&rdquo; is a fact rather than an impression:<br>"
             "<table><tr><th>family</th><th>steps</th><th>%</th></tr>"
             "<tr><td>interpreter — EVM opcode handlers</td><td>40,106,742</td><td>30.61 %</td></tr>"
             "<tr><td>MPT / OffsetTrie / trie encode</td><td>25,220,171</td><td>19.25 %</td></tr>"
             "<tr><td>state + maps</td><td>20,290,926</td><td>15.49 %</td></tr>"
             "<tr><td>JUMPDEST scan</td><td>12,614,924</td><td>9.63 %</td></tr>"
             "<tr><td>keccak wrapper</td><td>9,389,534</td><td>7.17 %</td></tr>"
             "<tr><td>uint256 arithmetic</td><td>7,585,134</td><td>5.79 %</td></tr>"
             "<tr><td>libc / allocator / libgcc</td><td>3,643,100</td><td>2.78 %</td></tr>"
             "<tr><td>bn254 / bls12 / other precompiles</td><td>2,796,856</td><td>2.13 %</td></tr>"
             "<tr><td>RLP encode/decode</td><td>2,785,639</td><td>2.13 %</td></tr>"
             "<tr><td>secp256k1 / ecrecover</td><td>2,088,725</td><td>1.59 %</td></tr>"
             "<tr><td>guest entry / witness parse</td><td>221,501</td><td>0.17 %</td></tr>"
             "<tr><td>everything else</td><td>4,277,998</td><td>3.27 %</td></tr></table>"
             "Unattributed: <b>180 steps</b>.",
        'fix': "Three families are now confirmed <i>not</i> to be levers, which is worth as much as "
               "finding one:<br><br>"
               "<b><code>memcpy</code>/<code>memset</code>.</b> They are ZisK DMA precompiles. "
               "<code>dma_memcpy</code> moves bytes at ~0.55 COST/byte plus ~1,000 fixed, against "
               "~21 COST/byte for a hand-rolled 8-byte loop, so the break-even is around <b>50 "
               "bytes</b> and the guest's 32-byte copies are right to stay hand-rolled. The whole "
               "family is 0.66 % of COST. One rule worth carrying: <b>8-align the source</b> — a "
               "misaligned <code>dma_memcpy</code> source costs ~4.8 COST/byte extra.<br><br>"
               "<b><code>ecrecover</code>.</b> 2,088,725 steps plus 106 M of "
               "<code>secp256k1_add</code>/<code>_dbl</code> — ~1.2 % of COST for 292 calls, and all "
               "of it inside <code>ziskos::zisklib</code>. Nothing to take in the guest.<br><br>"
               "<b><code>operator new</code>.</b> 65,529 calls at 15 steps each — already a bump "
               "allocator.",
        'rem': "The families are regex-assigned, first match wins, so a symbol that could belong to "
               "two lands in the earlier one — the interpreter/Intercode split is written out "
               "explicitly for exactly that reason. Re-derive rather than trust the split if a "
               "family boundary is what a decision turns on.",
    },
    {
        'id': 'lazyhash', 'share': 8.5, 'tag': 'BUILT',
        't': 'The OffsetTrie constructor hashes all 11,000 blob nodes, and execution erases half of them',
        'w': "Keccak is <b>34.78 % of the guest's COST</b> — 90,856 permutations — and nobody had "
             "split that by <i>who asks for it</i>. Every call site in the ELF, with the profile's "
             "count, accounts for all 30,171 calls:<br>"
             "<table><tr><th>caller</th><th>calls</th></tr>"
             "<tr><td><b>OffsetTrie::OffsetTrie</b> — the priming sweep</td><td><b>11,000</b></td></tr>"
             "<tr><td>child_ref_compute&lt;false&gt; — the update pass</td><td>6,042</td></tr>"
             "<tr><td>interpreter <code>sha3</code></td><td>3,927</td></tr>"
             "<tr><td>read_storage / read_account</td><td>4,163</td></tr>"
             "<tr><td><code>set_3_bits</code> (receipt bloom)</td><td>2,536</td></tr>"
             "<tr><td>the witness bytecodes, to key the code index</td><td>384</td></tr>"
             "<tr><td>commit, recover_address, rest</td><td>1,519</td></tr></table>"
             "Re-encoding the blob offline the way <code>encode_rlp</code> does gives the sweep's "
             "exact price: <b>31,021 permutations, 11.70 % of total COST</b> (8,747 branches at 3.29 "
             "each, 2,253 leaves and exts at 1).<br><br>"
             "<b>And more than half of it is thrown away.</b> <code>upsert_node</code> and "
             "<code>put_node</code> both <code>hashes_.erase(id)</code> — a node whose <i>descendant</i> "
             "changed has a stale hash, and <code>put_node</code> keeps the id when it rewrites the "
             "bytes. <code>child_ref_compute&lt;false&gt;</code> then runs 6,042 times against "
             "<code>fresh_id()</code>'s 162, so <b>~6,014 of the 11,000 primed hashes were erased "
             "before anything read them</b>: ~19,600 permutations, <b>7.4 % of total COST</b>, plus "
             "~2.9 M steps of wasted encoding.",
        'fix': "<code>child_ref_compute</code> was <i>already</i> a lazy memoising hash — it encodes a "
               "node, caches the digest, and its recursion bottoms out on cached entries, on the "
               "digests the witness supplies, and on nodes under 32 B that the parent inlines. The "
               "sweep buys exactly one thing: it keeps that recursion one level deep.<br><br>"
               "<b>Delete it.</b> The recursion then goes as deep as the trie — one ~800 B frame per "
               "level. The blob for this block is <b>18 levels</b> deep counting the storage tries "
               "nested under account leaves (measured by walking it); the structural ceiling is "
               "64 + 64, ~102 KB against ziskos's <b>1 MiB</b> stack. The constructor keeps every "
               "structural check — extents, exact tiling, and every child id being a recorded node "
               "start, which is what <code>find_original</code>'s &ldquo;node not found&rdquo; arm "
               "rests on — and its codegen goes <b>1,914 &rarr; 440 instructions</b>.<br><br>"
               "<b>BUILT</b> on <code>al/zkvm-r4-levers</code> as <code>f5bd05450</code>. With "
               "<code>priming_pass</code> gone, <code>encode_rlp</code>/<code>child_ref</code>/"
               "<code>child_ref_compute</code> stop being templates.",
        'rem': "<b>NOT BUILT for the guest.</b> This is the largest single change on the branch and it "
               "is in the soundness-critical file: it needs <code>test_offset_trie</code>, "
               "<code>test_witness_generator</code> and a state-root diff over the corpus.<br><br>"
               "The one abort that disappears is <code>child_ref_compute</code>'s &ldquo;unprimed "
               "hash-referenced node (bad offset)&rdquo; — it fired only inside the sweep, and only "
               "for a forward or garbage child offset, which the constructor's own check rejects "
               "first. A blob node that no longer gets encoded is one that nothing reachable from the "
               "post-state root references.",
    },
    {
        'id': 'memcost', 'share': 1.50, 'tag': 'PRICED',
        't': "A 4-byte access costs 6.6&times; an 8-byte one — the cost model's axis is width, not alignment",
        'w': "Everything written here before this entry reasoned about ZisK's memory cost from the "
             "constant names in <code>emu_costs.rs</code>. That was wrong. Measured instead — a ZisK "
             "guest whose whole body is one 100,000-iteration loop around a single inline-asm access, "
             "run under <code>ziskemu -X</code>, input byte picking the shape:<br>"
             "<table><tr><th>access</th><th>MEMORY</th><th>+MAIN</th></tr>"
             "<tr><td><code>ld</code> 8 B at 0 mod 8</td><td><b>16</b></td><td>84</td></tr>"
             "<tr><td><code>sd</code> 8 B at 0 mod 8</td><td><b>18</b></td><td>86</td></tr>"
             "<tr><td><code>lbu</code> 1 B</td><td>25</td><td>93</td></tr>"
             "<tr><td><code>sb</code>, source register holds only the byte</td><td>32</td><td>100</td></tr>"
             "<tr><td><code>lw</code>/<code>lhu</code>/<code>sw</code> — any 4- or 2-byte access, "
             "<i>any</i> alignment</td><td><b>106</b></td><td>174</td></tr>"
             "<tr><td><code>sb</code>, source register has dirty high bits</td><td>106</td><td>174</td></tr>"
             "<tr><td>anything crossing an 8-byte boundary</td><td>159</td><td>227</td></tr></table>"
             "The rule (<code>mem_operations_stats.rs</code>): an access is <i>aligned</i> only if "
             "<code>(addr &amp; 7) == 0</code> <b>and</b> <code>width == 8</code>. So a <code>lw</code> "
             "at a 16-aligned address is priced exactly like one at a 4-aligned address. "
             "<b>Alignment is not the axis. Width is.</b>",
        'fix': "Guest-wide there are <b>2,842,952 sub-word accesses = 301 M COST = 1.50 %</b> "
               "(1.62 M <code>lw</code>, 639 K <code>sw</code>, 423 K <code>lwu</code>, 143 K "
               "<code>lhu</code>, 17 K <code>sh</code>). As 8-byte aligned accesses they would cost "
               "47 M, so the ceiling on &ldquo;stop using 32-bit fields&rdquo; is <b>&minus;1.27 %</b> "
               "— a ceiling, not a lever: most are <code>uint32_t</code> struct fields whose width is "
               "someone else's decision (<code>node_id_wire</code>, the AccountState version, "
               "<code>evmc_message</code>).<br><br>"
               "<b>One piece of it landed.</b> The OffsetTrie constructor staged a branch's eight "
               "loaded words into an <code>alignas(8) node_id_wire raw[16]</code> and read them back "
               "a field at a time. Consuming each word in the register it lands in removes, per "
               "branch node, 78 instructions and — the part nothing in the source suggested — "
               "<b>16 <code>lw</code> at 106 each</b>, which cost more than everything else in the "
               "block put together. 682,266 steps, <b>62.5 M COST, 0.31 %</b>; "
               "<code>d47cadf92</code>, 800,016 host cases.<br><br>"
               "Two corollaries worth keeping: four <code>lbu</code> (100) cost less <i>memory</i> "
               "than one <code>lw</code> (106) but three more instructions, so <code>-mtune</code> "
               "still wins; and one unaligned <code>ld</code> (227) beats two <code>lw</code> (348), "
               "so <code>digestrun</code>'s bulk read still wins.",
        'rem': "The write constants (<code>MEM_WRITE_UNALIGNED_*</code>) are defined and never used — "
               "<code>get_cost</code> charges writes at the <i>read</i> constants. Measured "
               "<code>sw</code> = 106, so the code is what runs, but a ZisK change there would move "
               "every number in this entry.<br><br>"
               "<code>sb</code> from a register with non-zero high bits is billed 106 instead of 32, "
               "and RISC-V gives gcc no reason to clear them. Real but small: masking costs 68 to "
               "save 74, ~4.3 M (0.02 %) across the guest's 717,104 byte stores.",
    },
    {
        'id': 'codelazy', 'share': None, 'tag': 'REFUTED',
        't': "Building the code index lazily — refuted, 380 of the witness's 384 bytecodes are used",
        'w': "Before executing a single transaction the guest walks every bytecode in the witness "
             "twice: <code>keccak256</code> to key the code index (<b>25,686 permutations, 9.69 % of "
             "total COST</b>) and <code>Intercode::find_jumpdests</code> to build the JUMPDEST bitmap "
             "(<b>12,544,700 steps, 9.57 % of the guest, ~4.7 % of COST</b> — the single largest "
             "function). 384 codes, 3,467,005 bytes, 1,935,892 scanned opcode positions.<br><br>"
             "If a large share of those contracts were never executed, deferring both would be the "
             "biggest guest lever there is.",
        'fix': "<b>They are executed.</b> Measured directly rather than argued: "
               "<code>$SC/codeuse.py</code> drops one code from the witness, re-runs the guest under "
               "<code>ziskemu</code> and compares the public output — a group whose removal leaves "
               "the output identical contains nothing the guest reads. 768 runs, bisected:<br><br>"
               "<b>380 of 384 used. 4 unused, 146 bytes — 0.004 % of the code bytes.</b><br><br>"
               "The producer supplies code only for contracts it serves. That closes lazy "
               "<code>Intercode</code>, lazy hashing, and the upstream ask that was going to ride on "
               "them (&ldquo;carry the code hash in the witness so the guest only keccaks what it "
               "serves&rdquo;).",
        'rem': "What made over-inclusion look plausible: the pre-state trie has 713 account leaves and "
               "<b>387</b> distinct non-empty code hashes against <b>384</b> supplied codes, so the "
               "producer does look like it is including every touched contract. It is — those "
               "contracts are simply all called.<br><br>"
               "The scan itself is still 9.57 % of the guest and is now known to be necessary work. "
               "Its loop is 6 instructions and 2 <code>lbu</code> per opcode position (458 COST); a "
               "SWAR pass over 8 bytes at a time would be 112 COST/byte against 256, but the fast "
               "path needs an 8-byte window with no PUSH in it and the average opcode advance is "
               "1.79 bytes, so it would almost never fire.",
    },
    {
        'id': 'fmixk', 'share': 0.33, 'tag': 'BUILT',
        't': "rv64 has no 64-bit immediate, so every hash rebuilds fmix64's two constants",
        'w': "The hot block of the account-state map is 36 instructions, and <b>13 of them are gcc "
             "re-deriving murmur3's two finalisation constants</b> — "
             "<code>lui/addi/slli/addi/addi/slli/addi</code> for the first and six more for the "
             "second — because rv64 cannot hold a 64-bit immediate and there is no register to keep "
             "one in across an inlined call site.<br><br>"
             "The guest calls <code>fmix64</code> <b>112,425 times</b> on block 25551991 (counted by "
             "the distinctive <code>lui</code>, so it is the number of times the constant is actually "
             "rebuilt): 32,760 in the account-state map, 17,947 in the touched-address set, 12,491 in "
             "immer's champ, 10,393 in the original-account-state map, then a tail of storage and "
             "code lookups.",
        'fix': "Hold them in <code>.rodata</code> and load them. One shared PC-relative address plus "
               "one load each: <b>13 instructions become 4</b>, and <code>hash_bytes20</code> "
               "assembles to <b>18 instead of 27</b>. At 68 per instruction and 16 for an 8-aligned "
               "8-byte load that is <b>580 COST per call, 65.2 M, 0.33 %</b>.<br><br>"
               "The hash is bit-identical — same constants, same order, same arithmetic. Only how the "
               "multiplier reaches the multiply changes, so the existing note about why "
               "<code>fmix64</code> has three xor-shifts and two multiplies is untouched.<br><br>"
               "<b>BUILT</b> as <code>d4fbeadf8</code>. The same trick, in the immer popcount hunk, "
               "takes that call path 31 &rarr; 18 instructions: <b>34.4 M, 0.17 %</b> "
               "(<code>b03b51b89</code>).",
        'rem': "It has to be <code>asm(\"ld %0, %1\" : \"=r\"(v) : \"m\"(k))</code>. gcc folds any "
               "constant it can see straight back into an immediate however it is spelled, and the "
               "operand has to be <code>\"m\"</code> rather than <code>\"r\"</code> or gcc is free to "
               "satisfy the address by rebuilding the value — the exact thing being removed. Guarded "
               "to ZisK behind <code>if !consteval</code>: SP1 is rv32im, where a 64-bit constant is "
               "two 32-bit halves and materialising costs about what loading would.<br><br>"
               "Counted after <i>assembly</i>, not from the <code>.s</code>: <code>li</code> for these "
               "values is a bare <code>lui</code>, so the listing over-counts and an earlier pass here "
               "said 0.44 %.",
    },
    {
        'id': 'mptrest', 'share': 19.80, 'tag': 'PRICED',
        't': 'The rest of the MPT is at cost — but the constructor grew 37 % while the guest shrank 22 %',
        'w': "Re-measured on the current build. The MPT/trie family is <b>25,940,386 steps, 19.80 % "
             "of the guest</b>, and the ranking is not what the old build said:<br>"
             "<table><tr><th>function</th><th>steps</th><th>% guest</th><th>calls</th><th>st/call</th></tr>"
             "<tr><td><b>OffsetTrie::OffsetTrie</b></td><td><b>6,133,840</b></td><td><b>4.68 %</b></td><td><b>1</b></td><td>—</td></tr>"
             "<tr><td>match&lt;encode_rlp…&gt;</td><td>5,736,479</td><td>4.38 %</td><td>11,000</td><td>521</td></tr>"
             "<tr><td>match&lt;encode_rlp…&gt;</td><td>3,621,050</td><td>2.76 %</td><td>6,176</td><td>586</td></tr>"
             "<tr><td>upsert_node</td><td>2,453,786</td><td>1.87 %</td><td>7,833</td><td>313</td></tr>"
             "<tr><td>find_original</td><td>2,168,772</td><td>1.66 %</td><td>4,163</td><td>521</td></tr></table>"
             "<b>The constructor was 4,466,136 steps on <code>9b6fc3ed</code> and is 6,133,840 now — "
             "+37 %</b>, over a period in which the guest went −22 %. Nobody optimised it and nobody "
             "meant to grow it; <code>digestrun</code> notes that \"the rebase moved work into the "
             "priming sweep\", which is presumably this. It is now the largest function in the MPT "
             "and the third largest in the guest, and it runs once.",
        'fix': "<b>Nothing new to take.</b> Two blocks are 57 % of the constructor: <b>108,408 x 16</b> "
               "for the node walk (tag switch through a jump table, 7 instructions, then a "
               "read-modify-write to set the node's bit in the validation bitmap, 8) and "
               "<b>8,747 x 202</b> for per-branch child validation — 16 ids in one aligned bulk read, "
               "then backwards / in-range / recorded-node-start per child, ~12.6 instructions each. "
               "That is the <code>ctorval</code> work, already priced at <b>−1.55 % / −2.74 % and "
               "reverted for soundness</b>. The validation is doing what it must.<br><br>"
               "<code>find_original</code> is 521 steps/call with a hot block run 46,973 times over "
               "4,163 calls — <b>11.3 per call, one nibble per level of descent</b>, which is what "
               "picking a branch child is. Not a walk that could go byte-wise. "
               "<code>NibblesView::operator==</code> <i>already</i> has the byte-wise fast path "
               "(<code>memcmp</code> on the shared run at equal parity) — that is "
               "<code>nibbytes</code>, landed; there is no second copy of it to take.<br><br>"
               "<b>One compiler artifact, below the floor:</b> the constructor's frame exceeds "
               "RISC-V's 12-bit immediate window, so GCC re-materialises the stack base "
               "(<code>addi rX, sp, 0x7ff</code>) per access — <b>225,504 steps guest-wide, 0.17 %</b>, "
               "211,407 of them here. A <code>[[gnu::noinline]]</code> helper for the child-validation "
               "loop would give it its own small frame, but 0.17 % is under the floor.",
        'rem': "<b>Watch the constructor.</b> +37 % in absolute steps with no intent behind it is the "
               "kind of drift that a per-function regression check would have caught and a "
               "whole-guest ratio does not. It is a fixed cost paid once per block, so it does not "
               "scale away on larger blocks — it gets relatively cheaper, which is exactly why it "
               "hides.<br><br>"
               "A first eyeball read <code>addi:64</code> in the block profile as 64 instructions of "
               "address re-materialisation per branch node; counting the actual pattern says ~24. "
               "Most of those <code>addi</code> are ordinary address arithmetic.",
    },
    {
        'id': 'frame', 'share': None, 'tag': 'BUILT',
        't': 'Four instructions of stack frame on every opcode, including the ones that call nothing',
        'w': "Every handler carries <code>addi sp,sp,-16 / sd ra,8(sp) ... ld ra,8(sp) / "
             "addi sp,sp,16</code> — including <code>add</code>, which calls nothing on the fast "
             "path. On the current build, block 25551991: <b>4 x 1,069,980 opcodes = 4.3 M steps, "
             "3.3 % of the guest</b>.<br><br>"
             "The cause is not the checks. <code>check_requirements</code> is "
             "<code>always_inline</code> and <code>Context::exit</code> is <code>[[noreturn]]</code> "
             "— but a noreturn <i>call</i> is still a <code>jal</code>, which clobbers "
             "<code>ra</code>, and <code>ra</code> is live across the whole handler because the "
             "dispatch at the end is a tail jump that must return to <i>our</i> caller. So GCC saves "
             "and restores it.<br><br>"
             "Found by decomposing <code>add</code>'s 43 instructions: 26 of actual 256-bit "
             "addition, 12 of fixed toll, <b>5 of frame and stack-pointer adjust</b>. The 26 are "
             "irreducible — the EVM stack is in memory, so it is 8 loads and 4 stores before any "
             "arithmetic.",
        'fix': "<b><code>MONAD_VM_CHECK(OP)</code></b> mirrors <code>check_requirements</code> "
               "exactly — same <code>if constexpr</code> structure, same conditions, same "
               "StatusCode, same <code>Context::exit</code> — and differs only in tail-calling the "
               "exits.<br><br>"
               "<b>musttail has to be lexically in the handler.</b> Marking it inside "
               "<code>check_requirements</code> compiles without complaint and the inliner drops it: "
               "27 instructions with the frame either way, against 23 with a macro. That is the same "
               "reason <code>MONAD_VM_NEXT</code> is a macro.<br><br>"
               "45 of the 46 sites in <code>instruction_table.hpp</code> are lexically inside a "
               "handler and convert. The 46th is inside a <code>void (*f)(FnArgs...)</code> wrapper "
               "where a tail call would leave from the wrapper; <code>push.hpp</code>'s three are in "
               "helpers for the same reason. <code>check_requirements</code> stays for them.<br><br>"
               "Assembly-verified on the literal macro text with a stub opcode table exercising all "
               "three <code>if constexpr</code> paths:<br>"
               "<table><tr><th>case</th><th>instr</th><th>frame</th></tr>"
               "<tr><td>no gas, no stack check</td><td>14</td><td><b>0</b></td></tr>"
               "<tr><td>gas + underflow (ADD-like)</td><td>23</td><td><b>0</b></td></tr>"
               "<tr><td>+ overflow (DUP-like)</td><td>25</td><td><b>0</b></td></tr>"
               "<tr><td><i>the equivalent call</i></td><td><i>27</i></td><td><i>4</i></td></tr></table>",
        'rem': "<b>Error behaviour is unchanged</b> — same calls, same arguments, same points. Only "
               "the instruction differs (<code>j</code> for <code>jal</code>) and the frame is torn "
               "down first, which is safe because <code>Context::exit</code> longjmps: it restores "
               "<code>sp</code> from a <code>jmp_buf</code> captured far up the stack rather than "
               "unwinding through the handler's frame (guest disassembly: "
               "<code>ld a0,312(a0); li a1,1; jal longjmp</code>).<br><br>"
               "The 45-site rewrite was scripted and diffed: the only lines removed are the call "
               "sites, the only lines added are the macro and its invocations.<br><br>"
               "<b>NOT BUILT.</b> <code>/opt/riscv</code> is absent on the machine this was written "
               "on, so the real header has never been compiled in context and no test has run. This "
               "is the gas and stack metering of <i>every</i> EVM opcode — run the VM suite and diff "
               "public values over the corpus before trusting it. On "
               "<code>al/zkvm-r4-levers</code>.",
    },
    {
        'id': 'interp', 'share': 6.08, 'tag': 'ASK',
        't': 'Half of <code>mstore</code>, <code>mload</code> and <code>calldataload</code> is '
             'hand-rolled byte reversal — and they are the three handlers the landed levers did '
             'not touch',
        'w': "<b>Re-measured on the current build</b> "
             "(<code>monad-variants/mtune/monad-mtune-zisk.elf</code>), because everything else in "
             "this document was measured on <code>9b6fc3ed</code>, which predates "
             "<code>pushbe</code>, <code>toavx</code> and the tune. Same block 25551991: "
             "<b>131,021,430 steps against 168,112,619 (−22.1 %)</b> and 20.03 G of COST against "
             "23.29 G (−14.0 %).<br><br>"
             "<table><tr><th>handler</th><th>9b6fc3ed</th><th>current</th></tr>"
             "<tr><td><code>push&lt;2&gt;</code></td><td>57 st/call</td><td><b>31</b></td></tr>"
             "<tr><td><code>swap&lt;1&gt;</code></td><td>54</td><td><b>34</b></td></tr>"
             "<tr><td><code>push&lt;20&gt;</code></td><td>120</td><td>77</td></tr>"
             "<tr><td><code>mstore</code></td><td>133.7</td><td><b>134.7</b></td></tr>"
             "<tr><td><code>mload</code></td><td>125.1</td><td><b>121.1</b></td></tr>"
             "<tr><td><code>calldataload</code></td><td>132.6</td><td><b>132.6</b></td></tr></table>"
             "<code>pushbe</code> and <code>toavx</code> did what they claimed. <b>Three handlers "
             "did not move at all.</b>",
        'fix': "<code>mstore</code> is <b>one straight-line block of 128 instructions</b> run 28,115 "
               "times — 95 % of its cost — mixing <code>and</code> 17, <code>slli</code> 17, "
               "<code>srli</code> 12, <code>or</code>, against <code>ld</code> 17 and <code>sd</code> "
               "14: four interleaved <code>bswap64</code>. EVM memory is big-endian, the register "
               "value little-endian, so every access converts.<br><br>"
               "Counted directly inside each handler rather than by idiom detector:<br>"
               "<table><tr><th>handler</th><th>steps</th><th>mask+shift</th><th>share</th></tr>"
               "<tr><td>mstore</td><td>3,787,896</td><td>1,776,514</td><td>47 %</td></tr>"
               "<tr><td>mload</td><td>2,392,142</td><td>1,225,166</td><td>51 %</td></tr>"
               "<tr><td>calldataload</td><td>1,078,577</td><td>504,016</td><td>47 %</td></tr>"
               "<tr><td>sha3</td><td>569,301</td><td>259,158</td><td>46 %</td></tr>"
               "<tr><td>callvalue</td><td>134,232</td><td>82,824</td><td>62 %</td></tr>"
               "<tr><td><b>total</b></td><td><b>7,962,148 — 6.08 %</b></td><td><b>3,847,678 — 2.94 %</b></td><td></td></tr></table>"
               "With <code>rev8</code>, four instructions per 256-bit word instead of ~48: "
               "<b>≈3.60 M steps, 2.75 % of steps, ~1.58 % of COST</b>.<br><br>"
               "<b>So the interpreter yields no new guest lever — it re-sizes the Zbb ask.</b> This "
               "entry's old conclusion holds. What is new is that <b>55 % of the remaining bswap now "
               "sits in three memory opcodes</b>: three sites, one mechanism, one instruction. Zbb "
               "on current code is 2.94 % (bswap) + 0.96 % (<code>__popcountdi2</code>) = "
               "<b>3.90 % of steps, ~2.2 % of COST</b> — the 2.3–2.8 % estimate held.",
        'rem': "<b>The idiom detector under-counts interleaved bswaps.</b> It attributes 1,068,370 of "
               "<code>mstore</code>'s 3,787,896 (28 %) where the direct count finds 47 % — a 24-row "
               "window cannot resolve four interleaved <code>bswap64</code>. Detector and direct "
               "count agree on the 2.94 % total by compensation, not by agreement. <b>Quote the "
               "per-handler measurement in the ask, not the detector.</b><br><br>"
               "<b>The arithmetic opcodes are already minimal</b> — the \"excess\" was an arithmetic "
               "error of mine. <code>add</code> is 43 instructions: 26 of actual 256-bit addition "
               "(8 <code>ld</code>, 7 <code>add</code>, 5 <code>sltu</code> carries, 2 <code>or</code>, "
               "4 <code>sd</code>), 12 of toll, 5 of frame. The claim that it is ~12 instructions "
               "forgot that <b>the EVM stack lives in memory</b>. Across handlers the dominant line is "
               "always <code>ld</code>/<code>sd</code> — 17 of 33 for a bitwise AND — and that is "
               "irreducible while the stack is in memory. The frame was the one recoverable part; it "
               "has its own entry.",

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
    "The levers that failed on r4 failed with <b>the same shape</b>: a probe that avoids work, sitting "
    "on a hotter path than the work it avoids. The ones that worked all have the other shape — they "
    "make the same work cheaper, or they notice the work did not need doing at all. Its hot paths are "
    "near their instruction floor, so conditional avoidance needs the avoided work to be large "
    "<i>and</i> the check to be rare.<br><br>"
    "The second prior is newer and cost more to learn: <b>a profile by symbol tells you where time "
    "goes, not why</b>. <code>encode_rlp</code> looked like an encoder at its floor for two "
    "campaigns. It was — it was just being handed 247 856 nodes that never needed encoding. Count "
    "what flows through a hot site before concluding it is finished.<br><br>"
    "The third is the costliest and the newest: <b>a lever whose justification is an argument, not a "
    "measurement, needs the argument checked as hard as the number.</b> The constructor's child-offset "
    "enumeration was removed for 2.7 % on the reasoning that a bad offset “reaches child_ref_compute, "
    "which aborts”. It does — after <code>encode_rlp</code> has already decoded the node past the end "
    "of the blob. The measurement was right, the ablation was clean, the corpus gate passed 504/504, "
    "and the change was still wrong, because none of those instruments look at what a <i>crafted</i> "
    "blob does. It reached origin before it was caught."
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
             "<b>Branch</b> <code>al/zkvm-r4</code> — 33 commits on "
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

    h.append("<h2>Where r4 stood before this pass</h2>")
    h.append("<p class=note>These come from <code>results/compare-r4.json</code>, which was rendered "
             "<b>before</b> the five levers below landed — they are the base the pass started from, "
             "not its result. The axes have not been re-run since; regenerating this JSON is what "
             "replaces the line under the table.</p>")
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
    h.append(f"<div class=item><p><b>After the pass, measured directly:</b> "
             f"<span class=num>{POST['zisk_ratio']:.4f}×</span> zisk-reth on work — median of "
             f"per-block ratios over {POST['n']} blocks, our steps from <code>ziskemu</code> against "
             f"the cached zisk-reth axis. The same computation puts the pre-pass base at "
             f"{POST['base_ratio']:.4f}×, which is the published {POST['published']} plus the cost of "
             f"the rebase — so the method reproduces the known figure before it reports a new one."
             f"<br><br><b>Not the same instrument as the table above.</b> It is work only, no prover "
             f"cost, and it does not go through <code>compare.py</code>. Treat it as a measurement "
             f"pending the axis re-run, not as a replacement row.</p></div>")

    h.append("<h2>Where the work goes</h2>")
    h.append(f"<p class=note>Profile of the r4 ZisK guest on block {PROFILE['block']} "
             f"({PROFILE['steps']/1e6:.1f} M steps, the largest witness of the corpus). The second "
             f"column is block {PROFILE['small_block']} ({PROFILE['small_steps']/1e6:.1f} M) — a fixed "
             f"cost is a larger share of a small block, and the two columns disagreeing is "
             f"information, not noise.</p>")
    h.append("<table><tr><th>site</th><th class=n>large</th><th class=n>small</th>"
             "<th>symbol</th></tr>")
    for name, big, small, sym in PROFILE['rows']:
        # A row may carry only the large-block share: this profile was re-run on the rebased base,
        # the small-block one was not, and an absent number is written as absent rather than carried
        # over from the run before the rebase.
        small_cell = f"{small:.2f}%" if small is not None else "—"
        h.append(f"<tr><td>{name}</td><td class=n>{big:.2f}%</td><td class=n>{small_cell}</td>"
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
        # An entry is one of three shapes: a target (carries a profile share), a priced property, or
        # a surface a lever has since taken. Written out rather than folded into a ternary over the
        # whole concatenation, which is how the shapes silently swapped their tags the first time.
        tag = r.get('tag') or ('OPEN' if r['share'] else 'PRICED')
        # A REFUTED entry in this section is a closed door, not a live target:
        # rendering it in the same neutral chip as an OPEN one is how a dead
        # idea gets re-proposed six weeks later.
        cls = {'REFUTED': 'no', 'LANDED': 'ok', 'TAKEN': 'ok', 'BUILT': 'ok'}.get(tag, 'n')
        if r['share']:
            head = (f"<h3><span class='tag {cls}'>{tag}</span>{r['t']}</h3>"
                    f"<p><span class=num>{r['share']:.2f}%</span> "
                    f"<span class=note>of the large block</span></p>")
        else:
            head = f"<h3><span class='tag {cls}'>{tag}</span>{r['t']}</h3>"
        # 'rem' is optional here and was silently dropped until 2026-08-15, which published four
        # entries' magnitudes with their caveats stripped. A figure without its caveat is the exact
        # shape this document exists to prevent, so it renders whenever an entry carries one.
        rem = (f"<div class=lbl>what would undo this</div><p>{r['rem']}</p>") if r.get('rem') else ''
        h.append(f"<div class=item>{head}"
                 f"<div class=lbl>what</div><p>{r['w']}</p>"
                 f"<div class=lbl>where that leaves it</div><p>{r['fix']}</p>{rem}</div>")

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
