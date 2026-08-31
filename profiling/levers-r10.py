#!/usr/bin/env python3
"""levers-r10 — what is left to fix in the Monad ZisK guest on the r10 base, ranked, with the
measurement behind each verdict.

Same rule as levers.py, and the reason there is a separate document at all: a lever's verdict belongs
to the base it was measured on, and a rebase reopens all of them. levers.py speaks about r4 and
levers-r3.py about r3; neither's figures survive to here.

Everything below was measured on `al/zkvm-r10` against the canonical 200-block corpus
`canonical-2026-08-25815000-25815199-d49075fa3` (blocks 25815000-25815199), under ziskos 1.1.0-alpha.
Every ratio is the MEDIAN OF PER-BLOCK RATIOS from an A/B on that corpus, not a quotient of medians.
Profile figures come from the 50-block exact profile in results/prof50.json, whose coverage against
the real steps of those same blocks is 100.00 % -- see finding 144 for why that check matters and what
it caught.

  ./levers-r10.py [--out results/levers-r10.html]
"""
import argparse, html, os, time

HERE = os.path.dirname(os.path.abspath(__file__))

# ── the base, and the axes this document speaks about ────────────────────────────────────────────
BASE = {
    'branch': 'al/zkvm-r10',
    'tip': '494b1bbd6',
    'corpus': 'canonical-2026-08-25815000-25815199-d49075fa3 (200 blocks)',
    'runtime': 'ziskos 1.1.0-alpha',
    'vs': 'r10tip-vs-ziskethone',
    # AGAINST THE BUILD UPSTREAM SHIPS, not its develop branch. The figure here used to read
    # "+7.5 % steps / +6.4 % COST", measured against ziskethone's `develop` (sha dd445f3f1a093a42,
    # pin 7e6c702), which lacks the unmerged perf/dma-precompiles branch its own shipped guest
    # carries (no JUMPDEST precompile, no -mzisk-dma) and costs 1.348x its own shipped build.
    # Comparing to it understated the gap by roughly a third. guests/ziskethone/ziskethone.elf is
    # the SHIPPED build (39ad249e2e50ee17); see guests/ziskethone/ziskethone.build.
    'gap': '+39.6 % steps / +24.8 % COST against the ziskethone build upstream ships '
           '(vendor/zisk-eth-client submodule pin 2bb899a), 50-block sample of the same corpus, '
           'measured before the state levers below',
}

# ── where the guest spends, 50 blocks, exact ─────────────────────────────────────────────────────
# Buckets cut by SYMBOL and deliberately independent of hotspots.FAMILIES: which family a node-RLP
# symbol lands in is a naming question that has already moved once and took a headline with it
# (finding 142). The instructions have not moved.
SPEND = [
    ('EVM interpreter',              51_168_816, 58_853_034),
    ('state access + trie lookup',   18_851_324,  7_458_759),
    ('node build / RLP encode',      14_846_794, 18_094_665),
    ('keccak',                        8_287_057,  7_407_626),
]

TRIED = [
    {'id': 'nullchild', 'verdict': 'REFUTED', 'branch': 'retiré de al/zkvm-r10 au nettoyage',
     't': 'The priming walk tests `c == NULL_ID` before consulting the seen-set',
     'num': '+0.10 % steps, +0.02 % COST',
     'w': "NULL_ID <i>is</i> offset 0, the blob's magic header, and the walk starts at "
          "HEADER_LEN = 8 -- so no node begins there, and marking that one slot valid makes the "
          "seen-set encode \"null or already visited\" by itself. The accepted set is unchanged, "
          "{0} union {visited starts} either way, and <code>node_offsets</code> is a constructor "
          "local so the mark does not leak.",
     'fix': "<code>node_offsets[0] = 1</code>, then drop the <code>c == NULL_ID</code> arm of the "
            "assertion -- one compare and its branch per child, sixteen per branch node.",
     'rem': "<b>The test was not waste but a fast path.</b> For a null child the <code>||</code> "
            "short-circuits <i>before</i> the seen-set load; removing it makes every child pay the "
            "bound, the load and the compare. Put beside the empty-pair skip, which needed both "
            "halves of a 64-bit word null and bought nothing, the two agree on one fact about the "
            "data: <b>nulls are scattered, not clustered</b> — frequent enough that a per-child "
            "short-circuit pays, too dispersed for a per-pair test to fire.<br><br>Landed as "
            "<code>f1b2f7335</code> and reverted by <code>687cace4e</code>; both were dropped when "
            "the branch was cleaned, so this entry is the only versioned record of the "
            "measurement. Same family as <code>pairread</code>: replacing a cheap compare with a "
            "memory access loses.",
     },
    {'id': 'seqmerge', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 — 1d94954ca',
     't': 'The guest pays the node\'s optimistic-merge conflict check on a serialized loop',
     'num': '−0.536 % steps, −0.314 % COST over five blocks, 504/504 roots',
     'w': "ExecuteTransaction runs a transaction BEFORE its predecessor has merged -- that is what "
          "<code>prev_</code> is for -- so <code>can_merge</code> detects a stale pre-state and the "
          "retry repairs it. The guest's loop constructs an already-satisfied promise and runs each "
          "transaction to completion before the next begins. On 25815100: <b>231 of 233 can_merge "
          "calls are that gate</b>, the retry path runs <b>zero</b> times, ~1,836 steps each.",
     'fix': "A <code>SequentialExecutionToken</code> with a private constructor, befriended to one "
            "type defined only in the guest, admitting a dedicated "
            "<code>execute(SequentialExecutionToken)</code>. Not a trait and not a template "
            "parameter: serialization is a property of the SCHEDULER, and ExecuteTransaction is "
            "explicitly instantiated for ~20 traits sets.",
     'rem': "<code>MONAD_ZKVM_CHECK_SEQUENTIAL_MERGE</code> restores can_merge as an assertion on "
            "the same path, so the token's claim is falsifiable rather than trusted; the arm passes "
            "504/504. That the assertion is LIVE and not compiled away is itself measured -- "
            "can_merge entries a block: <b>233 before, 2 after, 233 in the differential arm</b>."
            "<br><br>An independent arm (FINDINGS 187, another session, unlanded) censused the same "
            "removal over 200 blocks: 43,599 can_merge calls for 43,599 merges, <b>zero false "
            "returns</b>, and −0.43 % steps / <b>−0.24 % COST</b>. That is the better-sampled figure; "
            "our five-block −0.314 % is optimistic.<br><br>The invariant needed correcting after "
            "landing (79953c5da): reads DO write block_state -- <code>read_storage</code> ends in "
            "<code>emplace(key, {result, result})</code> -- but both sides of every comparison come "
            "from that one emplace. What the loop excludes is a CONCURRENT writer.",
     },
    {'id': 'pairread', 'verdict': 'REFUTED', 'branch': 'construit, non landé',
     't': 'encode_rlp reads a branch\'s sixteen 4-byte child ids one field at a time',
     'num': '+2.021 % steps, +0.961 % COST — worse on all five blocks',
     'w': "ZisK prices a memory access by its <b>width</b>, not its address: the trace's "
          "<code>lw/lwu/sw</code> count is exactly <code>TOTAL unaligned 4B</code> and its "
          "<code>lb/lbu/sb</code> count exactly <code>TOTAL unaligned 1B</code>. <b>17.2 for 8 bytes, "
          "58.8 for 1, 136 for 4.</b> The guest makes 5.2 M sub-word accesses a block, "
          "<b>2.87 % of COST</b>, and encode_rlp alone makes 345,421 of the 4-byte ones.",
     'fix': "Hold the sixteen ids as eight <code>uint64_t</code> and take each out of the word it "
            "already sits in -- the shape the constructor's own validation sweep already uses.",
     'rem': "<b>The model priced what it removed and not what it added.</b> All-in, a 4-byte access "
            "is 204 (136 + 68 of MAIN), an 8-byte access is 85, and one added ALU instruction is "
            "~125. <code>ld</code> plus a single shift already loses; with a runtime index it is a "
            "shift, a mask and index arithmetic -- five instructions for one.<br><br>This closes the "
            "whole \"widen the load\" family, the 2.87 % pool included: <b>at 68 COST an "
            "instruction, the arithmetic that avoids a narrow access costs more than the access.</b> "
            "Same shape as the four-word arm of <code>keycmp</code>, six hours earlier.",
     },
    {'id': 'keycmp', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 — 69fcf8088',
     't': 'A 32-byte storage key is compared in address order, and its first three words are zero',
     'num': '−0.121 % steps, −0.059 % COST over five blocks, 504/504 roots',
     'w': "The three flat containers a storage access walks -- <code>FlatStorage</code>, "
          "<code>PrestateStorage</code> and A_K's warm-slot set -- use "
          "<code>__builtin_memcmp</code>, which GCC unrolls into four 8-byte compares in ADDRESS "
          "order. <code>bytes32_t</code> is big-endian, so a slot index below 2^64 is 24 zero bytes "
          "then the value. Measured in <code>get_storage</code>: word 0 rejects 13,320 of 21,424 "
          "entries, words 1 and 2 reject <b>zero</b>, word 3 rejects 5,693. Program-wide, 43,862 "
          "entries a block reach word 1 and those two words reject 2.98 % of them.",
     'fix': "Test word 0, then word 3, then 1 and 2 -- where they now cost nothing on the entries "
            "already rejected. All four words must match either way, so reordering an equality is "
            "semantics-preserving.",
     'rem': "<b>The model was wrong twice before it was right.</b> Written in plain C++ the arm "
            "measured −0.016 % and was really nothing: GCC re-sank the key loads into the scan "
            "-- key and entry share a type, so a store to an entry may alias the key -- then "
            "recognised the four compares as the memcmp it knows and re-emitted them in address "
            "order. The loop disassembled instruction-for-instruction identical to no change at "
            "all. An <code>__asm__(\"\" : \"+r\")</code> on the hoisted word holds the "
            "ordering.<br><br>Hoisting all four words then REGRESSED, <b>+0.103 % steps, +0.056 % "
            "COST</b>: the model priced the loop and not the call. These scans are ~4 entries long, "
            "so a four-load prologue on every call costs more than the loop saves, and four values "
            "live across the loop spill in functions this size. Hoisting ONE word -- the one that "
            "decides -- is the only shape whose per-call cost is repaid.",
     },
    {'id': 'dirtymark', 'verdict': 'CONFIRMED', 'branch': 'al/dirtymark — 279098207, not merged',
     't': 'Every upsert descent pays a hash-map erase to invalidate one cached hash',
     'num': '−0.543 % steps, −0.338 % COST — 86 % of the 754,759-step ceiling, 504/504 roots',
     'w': "The per-PC heat map shows one concentration in upsert_node: fifteen instructions at "
          "8006aa78-8006aab8, 7,387 executions a block. It is <code>hashes_.erase(id)</code>, and "
          "specifically unordered_dense's backward-shift deletion -- checked against the container's "
          "source rather than guessed: the bucket is two uint32s (the <code>slli ×8</code>), the shift "
          "copies both fields down (<code>[+0]</code> and <code>[+4]</code> loaded and stored), and "
          "<code>dist_inc = 1U&lt;&lt;8</code> is the <code>addiw −256</code>.",
     'fix': "Not the erase but the structure: an invalidation that only has to be REMEMBERED does not "
            "need a backward shift. Ids are dense offsets and overlay indices, so a dirty mark keyed "
            "by id answers hash()'s question in O(1).",
     'rem': "The heat map priced only its backward-shift loop at 110 k; outlining the call into a "
            "noinline wrapper and subtracting an empty one gives <b>6.8x that</b>, spread over hash, "
            "probe, compare and shift. 0.56-0.70 % across five blocks.<br><br>The empty arm also "
            "shows the whole guest 3.6 M cheaper, of which only 491 k is the mechanism: the other "
            "~3.1 M is hash recomputation the invalidation CAUSES, which is load-bearing. A cheaper "
            "mark collects the 0.63 % and none of that.<br><br>The obvious implementation is the one "
            "that already failed: blob ids are SPARSE offsets, so a table indexed by "
            "<code>offset&nbsp;&gt;&gt;&nbsp;2</code> is proportional to blob size and a flat hash "
            "store over that domain regressed 0.66 %. Overlay ids are dense only after subtracting "
            "OVERLAY_BASE. A mark must split the domains, carry its init cost against a 0.63 % "
            "ceiling, cover every mutation and clear only after a successful recomputation."
            "<br><br><b>Split by domain, the rule decides against it.</b> blob 7,869 calls a block "
            "and 0.65 %; overlay <b>10 calls</b> and 279 steps; null 195 and 6,160. The half with no "
            "risk is worth nothing, and all of the ceiling is in the domain whose bitmap regressed "
            "0.66 % last time. Deferred to the prefix-order/constructor prototype, which "
            "materialises dense indices anyway and may retire the question."},
    {'id': 'viewresult', 'verdict': 'REFUTED', 'branch': 'measured, not attempted',
     't': 'Return a NibblesView from upsert_node instead of an owning Nibbles',
     'num': 'ceiling 35-55 k steps a block — 0.03-0.05 % of the guest',
     'w': "The returned path is always a sub-view of the caller's key, so the owning Nibbles looked "
          "like an allocation per upsert. The arm counts say it is <b>1,250</b> a block, not 8,074: "
          "the four terminal arms sum to exactly 1.00 per logical upsert, and BranchView -- 84.2 % of "
          "calls -- only passes the result up.",
     'fix': "The allocation is real: upsert_node calls <code>_Znam</code> at seven sites in the "
            "disassembly, which is the check the plan required before attributing any ceiling. Its "
            "cost is not. <code>operator new[]</code> is 32,463 steps a block across the WHOLE guest "
            "(0.03 %), delete[] another 2,164.",
     'rem': "Two follow-ons closed with it. The EXT arm runs 24 times a block in total -- 22 followed, "
            "2 split -- so its unconditional path copy is 24 allocations; the leaf-split path is 98 "
            "calls. What remains of upsert_node's 1.49 M sits in the recursion itself, with no named "
            "construct holding it: the same place find_original is, needing the same per-basic-block "
            "attribution the tooling lacks."},
    {'id': 'lazychildren', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 494b1bbd6)',
     't': 'upsert built a branch\'s whole child array to read one entry',
     'num': '−0.3 % steps / −0.2 % COST — 254,967 steps a block',
     'w': "The BranchView arm materialised sixteen unaligned 4-byte reads into 64 bytes of stack, "
          "then used one entry. The array is needed only when the slot was empty, because only then "
          "is the branch rewritten -- and the slot is already occupied on <b>97.1 %</b> of descents, "
          "6,608 of 6,802 a block.",
     'fix': "The occupied path reads one child and returns straight out of the recursion, never "
            "touching b again. The empty path still reads all sixteen and reads them BEFORE "
            "recursing: b points into the overlay's bytes and the recursion put_*s into that same "
            "overlay.",
     'rem': "Predicted 330-460 k from the descent count and three instructions a read; 23 % under, "
            "so gcc was already eliding part of the array -- it could, since only one index was read "
            "on that path. Second time in a day that a reconstruction which looked expensive was "
            "partly free already."},

    {'id': 'nullhasherase', 'verdict': 'REFUTED', 'branch': 'measured, not attempted',
     't': 'upsert_node erases a hash for NULL_ID, an id that can never have one',
     'num': '190 calls a block of 8,836 — 5,700-11,400 steps, under 0.01 % of the guest',
     'w': "hashes_.erase(id) runs at the top of every upsert_node, including the calls that pass "
          "NULL_ID to let the callee allocate a fresh leaf.",
     'fix': "Nothing: the wasted calls are 2.2 % of a cheap operation.",
     'rem': "Measured rather than reasoned about, which is the only reason it can be dismissed with "
            "a number instead of an opinion."},
    {'id': 'keycursor', 'verdict': 'REFUTED', 'branch': 'measured, reverted',
     't': 'Walk the descent with a cursor instead of rebuilding the key view at every branch',
     'num': '1.000× on 200 blocks — +8,914 steps, a hair worse',
     'w': "BRANCH is 86.3 % of the descent's node steps and each one rebuilt the key through "
          "<code>substr(1)</code> to read a single nibble. Predicted 193-386 k on the reasoning that "
          "a 16-byte view rebuild costs memory traffic.",
     'fix': "It does not. NibblesView is trivially copyable and lives in registers, so substr(1) "
            "adjusts two fields and the compiler was already doing exactly that. Reverted; the tree "
            "reproduces the previous ELF byte for byte.",
     'rem': "Gated before being judged: a differential build ran BOTH descents on every call over "
            "200 blocks and aborted on any disagreement — stricter than the roots, which only catch "
            "a divergence that reaches the root. Its control: with the cursor deliberately not "
            "advancing, the guest writes 256 zero bytes instead of three roots, so the asserts were "
            "live."},

    {'id': 'pairchild', 'verdict': 'REFUTED', 'branch': 'not attempted — closed by disassembly',
     't': 'Read a branch\'s child NodeIds in pairs and select a half',
     'num': 'the read is already three instructions',
     'w': "A comment in the OffsetTrie constructor prices a slot at nine instructions to read, "
          "because the 4-byte wire field never lands aligned.",
     'fix': "That comment is about <code>b.children()</code> widening all 16 fields for the "
            "constructor's validation, and that code ALREADY reads them as eight words. In the "
            "descent the arm disassembles to <code>slli</code>, <code>add</code>, <code>lwu</code> — "
            "one unaligned 4-byte load. A paired read costs more, not less.",
     'rem': "Closed without an A/B, which is what putting the disassembly before the experiment is "
            "for. Note a withdrawn claim: I set ~15 traced instructions against 58.7 steps a node "
            "step as if they conflicted. 58.7 is find_original's whole self cost over all node "
            "steps -- per-call prologue, leaves, EXT, checks and the loop included -- so a BRANCH "
            "step's own cost is below it and unmeasured. find_original is ON HOLD, not cleared: two "
            "candidates refuted, the function not exonerated."},
    {'id': 'nibpack', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 2cf634720)',
     't': 'Path comparison fell back to a nibble walk whenever the two parities differed',
     'num': '−0.4 % steps / −0.1 % COST — 414,934 steps a block',
     'w': "operator== compared whole bytes when both sides sat at the same parity and its own comment "
          "said the rest: <i>mismatched parity keeps the nibble walk</i>. That is <b>45.5 %</b> of the "
          "descent's path comparisons -- blob paths are byte-aligned, the key turns odd after an odd "
          "number of branch steps -- and the walk examines <b>67,264 nibbles a block</b>, because a "
          "comparison mostly runs to the end rather than failing early (the short-circuit saves only "
          "16 % against the full length). starts_with inherited the fallback; common_prefix_length, in "
          "upsert_node and erase_node, had no fast path at all.",
     'fix': "All three ask one question -- how far do the runs agree -- so <code>nibble_mismatch</code> "
            "answers it and the others derive from it. 16 nibbles at a time as a big-endian word, an "
            "odd start aligned by a 4-bit shift, and the first difference read out of the XOR as "
            "leading zeros / 4.",
     'rem': "Predicted 300-400 k from the walk side before measuring; 414,934 with the same-parity "
            "side and common_prefix_length included, neither instrumented. COST moves a quarter as "
            "much as steps: the cost model weights nibble work lightly, so this kind of lever will "
            "always read smaller in COST than in steps.<br><br>Gated against THIS header, not a "
            "replica: 487,344 cases, both parities each side, every length, a difference at every "
            "position, the derived operations checked too, under ASan with exact-sized buffers -- "
            "which disproved the padding requirement an earlier draft of the comment asserted. Five "
            "injected slips, five caught."},

    {'id': 'srootprime', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 834996844)',
     't': 'read_account reached the account leaf, which carries its storage root, and dropped it',
     'num': '−50,505 steps a block',
     'w': "The next read_storage for the same address descended from the root again to find what the "
          "previous descent had already passed through.",
     'fix': "Prime the existing one-entry cache from the leaf, NULL_ID included for an absent "
            "account. No invalidation, for the reason the cache never had any: it holds a PRE-STATE "
            "root read from the blob, and commit's mutations land in an overlay find_original does "
            "not consult.",
     'rem': "The ceiling was 666 re-descents a block and this removes <b>127</b>: the other 539 are "
            "EVICTIONS, not an empty cache. So sroot_'s problem is capacity, and widening it is a "
            "different lever paying a probe on all 2,450 calls -- finding 145's caution applies. "
            "Predicted 47,752 steps, measured 50,505: the first prediction of the round to land, "
            "because its ceiling came from a measured delta in call counts."},
    {'id': 'pathbytes', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 72f457d7b)',
     't': 'A node\'s path was appended one nibble at a time, and paths are 56-59 nibbles',
     'num': '−1.1 % steps / −0.5 % COST — 1,063,188 steps a block, 72 % of what the function cost',
     'w': "append_path ran set_nibble in a loop: 57 shifts and 57 byte read-modify-writes a call. "
          "<b>1,080 steps a call</b> over 1,372 calls a block, by far the worst cost per call in the "
          "trie layer -- against 235.5 for upsert_node and 376.4 for find_original.",
     'fix': "The destination is byte-aligned by construction, so the run is a byte run: a memcpy when "
            "the source starts on a byte boundary, one uniform 4-bit shift when it does not, and the "
            "odd tail nibble written on its own. This is the r4 <code>nibbytes</code> lever's fix, in "
            "a function that lever did not reach.",
     'rem': "Gated by an exhaustive check -- every length, both source parities, 2,048 cases, and it "
            "catches all three plausible slips put to it -- plus 200 of 200 roots. NOT by the rv64 "
            "self-test: that runs the revert semantics, never reaches node encoding, and gc-sections "
            "drops append_path from its binary. Anything touching offset_trie.cpp needs its own "
            "exhaustive check; the self-test covers the state layer only."},
    {'id': 'readmemo', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed a11c8b93c)',
     't': 'The read paths hashed an address the memo already held',
     'num': '−0.7 % steps / −0.4 % COST — 686,058 steps a block, the largest of the state levers',
     'w': "current_account_state has kept a one-entry memo since 3f91e4a9b and it answers 63.5 % of "
          "the MUTATION path. The read paths ignored it. Measured against the memo as it stands, "
          "filled only by mutations: get_storage names the memoised address <b>99.9 %</b> of the "
          "time, recent_account_state <b>87.3 %</b>, rows_for_read only <b>5.1 %</b>.",
     'fix': "A read-only helper. It never populates: a read does no dirty insert, so writing the "
            "memo would need memo_epoch_ left at a value frame_epoch_ can never take, and it would "
            "evict the entry the mutation path is about to want. rows_for_read is left alone on its "
            "5.1 %.",
     'rem': "The saving is <b>43 steps per avoided lookup</b>, not the 71-97 a whole lookup costs: "
            "the difference is the 20-byte memo compare, paid on every call including hits. Any memo "
            "widened to more entries pays that probe more than once, which is the thing to measure "
            "before building it."},

    {'id': 'origptr', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 1bfad9eab)',
     't': 'A storage read hashed the same address twice, once per map',
     'num': '−0.5 % steps / −0.3 % COST — 502,799 steps a block',
     'w': "get_storage found the row in current_, missed the overlay -- the common case for a read "
          "-- and then hashed the same 20 bytes again to reach original_. set_storage did the same "
          "pair.",
     'fix': "Each current row carries a pointer to the original row it was created from. Safe on a "
            "property of the container, not on care: original_ has exactly one try_emplace, no erase "
            "and no clear, and it is segmented, so an element never moves. current_ IS erased, in "
            "pop_reject, but that erases the row holding the pointer.",
     'rem': "This is what priced a map lookup at roughly 97 steps gross and said this path spends in "
            "the hash and probe, not in the linear scans -- which is what closed indexing (address, "
            "slot) pairs before it was started."},

    {'id': 'typedjournal', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed 62a54b08a)',
     't': 'A frame snapshotted a whole row to undo whatever it might change',
     'num': '−0.4 % steps / −0.3 % COST — 474,256 steps a block',
     'w': "journal_first_touch copied 184 bytes plus two vector allocations, 201 steps a call and "
          "1.43 M a block, for a frame that mostly went on to change one field.",
     'fix': "One record per mutation, carrying only what it overwrote. Ten kinds; flags record a "
            "TRANSITION so a second touch() adds nothing; warm slots record the one key appended.",
     'rem': "A third of the 1.43 M, and the profile says why: the typed entries cost 222,722 a block "
            "against the snapshot's 476,646, so entry VOLUME eats half. journal_balance is the "
            "largest at 89,208 over ~2,550 mutations -- volume at ~35 steps each, not a dear entry. "
            "push() also got 13,849 dearer, six watermarks instead of three."},

    {'id': 'rowsforread', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 (landed b8edcca04)',
     't': 'get_balance asked for the row, then asked again for the row behind it',
     'num': '−0.1 % steps / −0.1 % COST — 137,049 steps a block',
     'w': "Two lookups with a current row, THREE without: recent_account_state goes to original_ "
          "itself and the caller then asked for it again.",
     'fix': "rows_for_read answers both in one lookup.",
     'rem': "A quarter of what the same change was worth in get_storage, because these run about "
            "four times less often. The ratio is the lesson: remove a double lookup only while it "
            "sits on a hot path."},

    {'id': 'dirtydedup', 'verdict': 'REFUTED', 'branch': 'measured, never committed',
     't': 'Let the frame dirty-list keep duplicates and drop its linear scan',
     'num': '+0.6 % steps — the ablation is 663,842 steps a block WORSE',
     'w': "DirtyAccounts::emplace scans the frame's list on every insert. Nothing reads its return "
          "value since typed records replaced the row snapshot, so the scan looked like overhead.",
     'fix': "Ablated it -- all 200 roots still reproduce, so the ablation is behaviour-preserving "
            "here -- and the guest got slower. Without dedup the duplicates accumulate AND propagate "
            "to the parent on every accept, and at a mean frame depth of 4.99 the merge costs far "
            "more than the scan saved.",
     'rem': "This closes exactly one option and nothing more. It prices REMOVING the scan, not "
            "REPLACING it: once rows carry an index, a bitmap or epoch stamp per index answers "
            "'already in this frame?' with no scan and no duplicates, and this measurement says "
            "nothing about that design."},

    {'id': 'unifiedrow', 'verdict': 'REFUTED', 'branch': 'not attempted',
     't': 'Merge original_ and current_ into one row holding both halves',
     'num': 'the path it would fix fires 0-10 times a block, mean 3 over eight blocks',
     'w': "get_storage with NO current row still does current_.find then original_.find, and that "
          "double cannot be removed while the two maps are separate.",
     'fix': "Nothing to do: the path is essentially never taken. Every storage read goes through an "
            "account the execution already materialised.",
     'rem': "Established on eight blocks, not one. The single-block version of this claim said "
            "'zero', which was both wrong and unfalsifiable."},

    {'id': 'evmrowid', 'verdict': 'REFUTED', 'branch': 'not attempted',
     't': 'Carry a row id in the EVM call context so SLOAD and SSTORE never look up',
     'num': '~9 lookups a block remain in get_storage: 625-854 steps, 0.00 % of the guest',
     'w': "Before the read memo this was the biggest single target, 8,603 lookups a block in "
          "get_storage alone.",
     'fix': "The read memo took it. get_storage went from 8,603 lookups a block to <b>9</b>.",
     'rem': "The general form still holds -- an id resolved at the boundary from a raw address saves "
            "nothing, only an id already CARRIED does -- but on this base there is no longer a "
            "carried-id budget to collect."},

    {'id': 'prefixorder', 'verdict': 'REJECTED', 'branch': 'al/witness-format — research prototype, not merged',
     't': 'A prefix-order witness format, so the guest reaches a child without resolving an offset',
     'num': 'size -11.56 % over all 504 witnesses; no COST figure — the guest reader was stopped before it could run',
     'w': "The priming pass is 12,725,854 steps a block, 10.67 % of the guest. The offset format makes "
          "a reader resolve a node id to a blob offset before it can read a child; prefix order puts "
          "the child where the parent ends.",
     'fix': "<code>profiling/experiments/prefix-order/</code>: <code>GRAMMAR.md</code> and "
            "<code>prefix.py</code>, the transcoder with an independent decoder. A branch is a u64 "
            "descriptor (four bits a slot), a branch-local table of <code>0xa0 || hash32</code> "
            "digests, then u32 offsets for the second non-digest child onward, then the subtrees. "
            "Ids carry the kind in bits 28-30, the overlay flag in bit 31, and the blob is bounded "
            "at 2^28 so an offset never reaches the kind field.",
     'rem': "<b>Rejected on cost of ownership, not on a measurement that failed.</b> Everything "
            "static passed: five gates on all 504 witnesses -- canonical grammar and exact stream "
            "consumption, structural equivalence, digest runs identical and verified contiguous at a "
            "33-byte stride, tree before conversion, and 64,702,655 (node, slot) pairs resolved from "
            "the descriptor, cpop and the directory alone and compared to the old child id. Size "
            "-11.56 %, of which the directory costs 0.10 points; rebuilding an equivalent index in "
            "the guest heap would have cost 1.94 % of the blob.<br><br>Three findings the census "
            "forced. The blob is a <b>tree</b>, indegree 1 on 1,473,781 nodes, so prefix order "
            "duplicates nothing and needs no visited set -- and that retires the reason given for the "
            "visited bitmap in FINDINGS 38. Digests must be <b>grouped</b> into a branch-local table, "
            "not left in slot order: one that follows a variable-sized subtree is not addressable "
            "from the descriptor. And only the <b>second</b> non-digest child onward needs an offset, "
            "which empties 86 % of directories.<br><br>What stopped it: the reader needs two storage "
            "models, two navigations and a <code>(payload, tag, backing)</code> triple threaded "
            "everywhere -- 438 lines of header before the fold was written. The threshold set was "
            "1 % of global COST and <b>no COST was ever measured</b>, so the rejection is on scope. "
            "The prototype is sound and reproducible, and the cost-of-ownership judgement is the "
            "only thing standing against it -- not a measurement that failed. That is a "
            "separate question from the post-order descent, which was re-measured on "
            "c59b7ab05 and is refuted outright (levers-r10 postorder)."},

    {'id': 'evmwithdrawn', 'verdict': 'REFUTED', 'branch': 'withdrawn — measured against the wrong ELF',
     't': 'A per-opcode comparison that read the develop build as if it were the shipped guest',
     'num': 'withdrawn: +8,527,617 a block, a 69 % dispatch+frame split, and every per-opcode delta',
     'w': "The provenance was already recorded -- <b>shipped</b> is sha <code>39ad249e2e50ee17</code>, "
          "pin <code>2bb899a</code>; <b>develop</b> is sha <code>dd445f3f1a093a42</code>, pin "
          "<code>7e6c702</code>. The tool defaulted to develop because that file was newer and "
          "larger, which is evidence of nothing. compare.py's axes always pointed at the shipped "
          "ELF, so compare.html was never affected. <code>guests/ziskethone/ziskethone.elf</code> "
          "now holds the shipped build and <code>ziskethone.build</code> beside it pins and "
          "rebuilds it, so the two can no longer be confused by picking a path.",
     'fix': "opcheat.py now names both ELFs with their shas and defaults to the shipped one.",
     'rem': "The comparison did not merely use the wrong build; its central premise fails against "
            "the right one. Shipped pin 2bb899a is <i>evmone: fuse hot opcode sequences in the cgoto "
            "dispatch</i>, so a secondary opcode never enters its generic handler and 'same EVM "
            "execution =&gt; same entries per handler' is false. Against the shipped guest "
            "1,093,211 of 4,933,665 executions diverge on this sample -- 22.2 %, with JUMPI at "
            "251,426 executions against 88 handler entries.<br><br><b>The gate that should have "
            "caught this was fail-OPEN.</b> Its guard read <code>if dis and not agree</code>, so it "
            "only tripped when NOTHING agreed, and it printed <i>too few to move a per-execution "
            "figure</i> unconditionally underneath. It now stops on either of two hard bounds: more "
            "than 0.05 % of executions diverging, or any opcode above 1,000 executions diverging by "
            "more than 1 %.<br><br>Also withdrawn: <code>baseline::analyze</code> at 7,732,806 steps "
            "a block, which is a develop-only figure.<br><br>A second defect of the same family: the "
            "scratch <code>.disasm</code> was keyed by block alone, so a file cached from one guest "
            "was reparsed against another's address ranges and reported <i>0.0 % in the "
            "dispatcher</i> instead of an error. Keyed by the ELF's sha256 now.<br><br>Two more of "
            "the same family, both found by carrying the correction through rather than stopping at "
            "the ELF name.<br><br><b>--fusion was itself fail-open.</b> The abort read "
            "<code>and not a.fusion</code>, so under that flag the run continued past a measured "
            "22.58 % divergence, printed <i>below the 0.05% bound</i>, and produced a per-opcode "
            "delta and a dispatch/frame split from counts the gate had just rejected. The flag now "
            "selects a different deliverable, never a weaker gate: the map is emitted by its own "
            "function and the caller returns, so nothing past it is reachable.<br><br><b>The monad "
            "default was r8.</b> <code>guests/monad/monad-zkvm-guest-zisk.elf</code> is "
            "<code>fd39fe8c</code>; the report's r10 is <code>profiling/series/elf/r10-dma.elf</code>, "
            "<code>cc6ec731</code>. So the monad-only ceilings were measured on the wrong guest too, "
            "and both are now named with their sha wherever they are quoted."},

    {'id': 'dispatchreg', 'verdict': 'REFUTED', 'branch': 'measured, then dropped for tablearg',
     't': 'Pin the dispatch table base in s11 — faster, and dropped anyway',
     'num': 'steps −2.931 %, COST −1.254 %; superseded by tablearg, which needs no invariant',
     'w': "Every handler ends in a <code>musttail</code> through "
          "<code>instruction_table&lt;traits&gt;[*instr_ptr]</code>, and GCC re-materialises that "
          "base at each dispatch as <code>auipc</code> plus an <code>ld</code> from a constant pool. "
          "JUMPDEST -- gas and dispatch, nothing else -- was 10 instructions against evmone's 8, and "
          "those were the two.<br><br><b>The root cause is the toolchain.</b> "
          "<code>MONAD_VM_INSTRUCTION_CALL</code> is <code>__attribute__((preserve_none))</code>, "
          "which threads state through registers across a tail-call chain and would make this lever "
          "unnecessary -- but it is <b>Clang-only</b> (gcc-15 lacks it, g++-16 miscompiles it with "
          "musttail at -Og, corrupting gas accounting) and the ZisK guest is built with GCC, so the "
          "attribute expands to nothing.",
     'fix': "A GCC global register variable: <code>register InstrEval const *monad_vm_pinned_table "
            "asm(\"s11\")</code> in <code>types.hpp</code>, the five dispatch sites in "
            "<code>instruction_table.hpp</code> routed through a macro, and one assignment per "
            "interpreter entry in <code>execute.cpp</code>. 36 lines, all behind "
            "<code>-DMONAD_VM_PINNED_TABLE</code> so the A/B is a flag and not a source fork.",
     'rem': "<b>Over the full canonical 200:</b> median −2.931 %, mean −2.866 %, aggregate "
            "−2.952 % (−2,936,303 steps a block). p10 −3.295 %, p90 −2.417 %, range −3.847 % to "
            "−0.069 %, and <b>200 of 200 blocks improve with none regressing</b>.<br><br>On the three "
            "blocks where the ceiling was measured the lever realises 2,513,948 of 3,129,627 -- "
            "<b>80 %</b>. The missing 607,052 is the NET cost of every codegen effect of reserving "
            "s11 -- one register fewer to allocate is the obvious candidate, but nothing here "
            "separates it from changed spill placement, scheduling or inlining decisions, so calling "
            "it register pressure would be a hypothesis stated as a measurement. That a ceiling and "
            "a gain differ is the point; which mechanism eats the difference is unmeasured. Those three blocks also "
            "understate it: −2.582 % against the 200-block median of −2.931 %.<br><br><b>COST</b>, "
            "on every tenth block of the corpus because the instrumented pass costs ~7x: median "
            "−1.254 %, aggregate −1.305 %, range −1.688 % to −0.482 %, 20 of 20 improving. COST moves "
            "less than half as far as steps -- the two instructions removed are among the cheapest "
            "in the model -- but it moves the same way, which is the check that matters.<br><br>Mechanism confirmed rather than inferred: the table tax falls from "
            "3,129,627 to <b>8,627</b> steps a block (the once-per-call pin), while the handler-frame "
            "count is <b>unchanged at 2,530,003</b>. That is a counter agreeing, not a binary "
            "comparison: no targeted diff of the frame instructions was made, so the claim is that "
            "the frame cost did not move, not that the frame code is identical. Lever 2 keeps its "
            "full ceiling either way.<br><br>Per block: −2.475 %, −2.943 %, −1.622 %. "
            "The spread tracks how interpreter-heavy the block is.<br><br>Gated: <b>200/200</b> "
            "canonical post-state roots, and the gate was shown to be live -- corrupting one nibble "
            "of a recorded root makes it report DIFFER. The flag-OFF build is <b>byte-identical</b> "
            "to the baseline, so the macro indirection is inert.<br><br>Baseline: rebuilt from "
            "6f2a29d4d with the zisk-dma GCC 15.2.0 and <code>-mzisk-dma</code>. Its sha "
            "(c61db075) differs from the report's r10-dma (cc6ec731) because build paths are embedded "
            "in the binary; it is <b>step-identical</b> on all three blocks, which is the equivalence "
            "that matters.<br><br>s11 is callee-saved, so translation units that never see the "
            "declaration preserve it across the runtime calls handlers make. A nested interpreter "
            "entry re-assigns it, sound only because one block runs a single traits instantiation -- "
            "That invariant is <b>unvalidated</b>: the 200-root gate does not reach it, as the paragraph "
            "below sets out.<br><br><b>Merge gates.</b> The root proof is "
            "now an artefact, not a terminal line: "
            "<code>results/gates/ab-pin-canonical200.tsv</code> carries the ELF sha, the corpus path "
            "and all 200 blocks with the root produced and the root recorded, written by "
            "<code>series/gate-roots-record.sh</code>, which fails closed if it compared fewer "
            "blocks than the corpus holds. The build option is "
            "<code>MONAD_ZKVM_PINNED_TABLE</code>, guarded to a RISC-V target and GCC with a "
            "FATAL_ERROR naming the offending value on anything else -- verified by configuring it "
            "on the host, which errors. Built through that option the ELF is <b>identical</b> to the "
            "raw <code>-D</code> build.<br><br><b>Not established: the cross-fork invariant.</b> The "
            "canonical 200 are one fork, and they exercise nesting (CALL 1,156, DELEGATECALL 421, "
            "STATICCALL 939, REVERT 25, SELFDESTRUCT 2 a block) but <b>CREATE and CREATE2 zero "
            "times</b>. The 17 wider-range witnesses in <code>guests/monad/inputs</code> cannot "
            "serve: on those the guest writes 256 zero bytes and the BASELINE fails all 17 too, so "
            "the corpus predates this guest's witness format. Host EEST would not help either -- the "
            "mechanism is RISC-V + GCC only, so a host build does not contain it. This needs "
            "witnesses generated for a pre-Prague range."},

    {'id': 'tablearg', 'verdict': 'CONFIRMED', 'branch': 'al/zkvm-r10 — 06d6ef114',
     't': 'The same base threaded as a seventh musttail argument instead of pinned in a register',
     'num': 'LANDED 06d6ef114 on al/zkvm-r10 — steps −2.771 %, COST −1.186 %, 200/200 roots',
     'w': "RISC-V passes eight arguments in registers, so a seventh is free at the call. The point "
          "is not the extra 0.14 points the pinned version wins -- it is that this variant has "
          "<b>no global state and no invariant</b>. The pinned register is only sound because a "
          "nested interpreter entry cannot install a different traits instantiation's table; "
          "threading the base as an argument makes that question disappear rather than documenting "
          "it.",
     'fix': "<code>MONAD_VM_TABLE_ARG</code>: parameter macros in <code>types.hpp</code>, 84 "
            "declarations and 84 definitions gaining <code>MONAD_VM_TBL_PARAM</code>, the five "
            "dispatch sites gaining <code>MONAD_VM_TBL_ARG</code>, and the entry supplying "
            "<code>instruction_table&lt;traits&gt;.data()</code>. Mutually exclusive with the pinned "
            "flag, enforced by an <code>#error</code>.",
     'rem': "<b>Over the same canonical 200 as the pinned variant:</b> median −2.771 % against "
            "−2.931 %, aggregate −2.804 % against −2.952 %, mean −2.722 %, p10 −3.131 %, p90 "
            "−2.301 %. <b>200 of 200 improve, none regress</b>, and the roots gate 200/200 with the "
            "evidence at <code>results/gates/ab-arg-canonical200.tsv</code>.<br><br>So it costs "
            "<b>0.16 percentage points</b> against the register, or 94 % of that gain. GCC does not "
            "spill it, which was the stated worry.<br><br><b>COST, measured on its own and not "
            "inherited:</b> the same 20 blocks as the s11 run, verified to be the identical block "
            "set. Median −1.186 %, aggregate −1.233 %, range −1.601 % to −0.454 %, 20 of 20 "
            "improving, against s11's −1.254 % / −1.305 %. It keeps ~95 % of the gain in COST as "
            "well as in steps -- the cost model does not price the extra live argument differently "
            "in any material way.<br><br><b>Build option:</b> <code>MONAD_ZKVM_TABLE_ARG</code>, "
            "guarded to a RISC-V target and GCC with a FATAL_ERROR naming the offending value; built "
            "through the option the ELF is identical to the raw <code>-D</code> build. The s11 CMake "
            "option is <b>gone</b> -- that path survives in types.hpp behind the raw "
            "<code>-DMONAD_VM_PINNED_TABLE</code> only so the published comparison can be re-run, "
            "and landing should delete it.<br><br><b>Landed as 06d6ef114.</b> Another session had "
            "written the same lever unconditionally on the same branch; per Anne-Laure the commit "
            "keeps their implementation and grafts the flag and the CMake option onto it. Their "
            "version was checked first and is <b>step-identical</b> to mine on all three blocks, and "
            "the grafted result rebuilds to 9085598f -- the exact binary every number here was "
            "measured on -- with the flag off giving c61db075, the baseline.<br><br>The control "
            "that makes the pair comparable: with <b>both</b> flags off the ELF is byte-identical to "
            "the baseline (c61db075) after the entire signature refactor -- 168 signatures touched, "
            "zero codegen change. So the two variants differ from the baseline only by what their "
            "flag turns on.<br><br>Four handlers were missed on the first pass because their last "
            "parameter is unnamed (<code>jump</code>, <code>return_</code>, <code>revert</code>, "
            "<code>invalid</code>, then <code>stop</code>); the linker named each one. "
            "<code>jump</code> dispatches so it needs the parameter named, the others exit and do "
            "not."},

    {'id': 'exithandler', 'verdict': 'REFUTED', 'branch': 'A/B measured — three shapes, all worse',
     't': 'Route the cold exits through a function of the InstrEval signature, so GCC sibcalls them',
     'num': 'REGRESSION on all three shapes tried: +2.2/+4.0 % (seven args), +1.3 % (one arg). Axis closed',
     'w': "The reasoning was sound and the measurement refuses it. <code>exit_h&lt;Code&gt;</code> "
          "with exactly the InstrEval signature, tail-called from MONAD_VM_CHECK: steps +3.274 %, "
          "+4.036 %, +2.234 % on the three blocks, and handler frames <b>2,530,003 -&gt; "
          "5,823,579</b>. A seven-argument tail call has to marshal all seven into a0-a6 at the exit "
          "point, which keeps the pre-mutation values live across the hot path -- so it buys spills, "
          "not fewer frames.",
     'fix': "None. All three shapes tried; the first two in FINDINGS §161.",
     'rem': "<b>The second arm settles the cause.</b> Dropping <code>[[noreturn]]</code> from "
            "<code>Context::exit</code>'s declaration -- a diagnostic build, guarded and since "
            "removed -- also regresses: +2.206 %, +2.432 %, +1.343 %, frames 2,530,003 -&gt; "
            "2,923,106. So the attribute is <b>not</b> what stops GCC sibcalling the exit, and my "
            "earlier explanation naming it was wrong.<br><br>What survives from the analysis is the "
            "accounting, which stands: real ra frames are 2,141,845 steps a block, 2,008,613 of it "
            "on entries whose exit never fires, push alone 1,630,743. The cost is real and both "
            "obvious ways of removing it cost more than they save. Why GCC will not sibcall this "
            "particular call is still unexplained.<br><br>One lead not tried: "
            "<code>monad_vm_runtime_context_out_of_gas_exit</code> already exists as an extern \"C\" "
            "noreturn taking only <code>Context*</code>. A one-argument tail call is a different "
            "shape from both arms above."
            "<br><br><b>Third shape, and the last one: one argument.</b> The lead "
            "this entry left open -- an extern \"C\" noreturn taking only "
            "<code>Context*</code> -- was tried, with a twin ..._error_exit added so all "
            "three exits in MONAD_VM_CHECK_AT took the same shape. It <b>regresses</b>: "
            "steps +1.286 %, +1.449 %, +0.754 % and COST +0.626 %, +0.690 %, +0.259 % on "
            "the three blocks, aggregate <b>+1.308 % steps / +0.618 % COST</b>. 504 of 504 "
            "corpus roots either way.<br><br>So the three shapes order monotonically the "
            "wrong way -- seven arguments +2.2 to +4.0 %, one argument +1.3 %, and the "
            "current two the cheapest of them. The marshalling account given above for the "
            "seven-argument arm does not explain the one-argument arm, so it was not the "
            "general cause. What the one-argument form adds is an <b>external boundary</b> "
            "the current call does not have, and the cost is most likely codegen around it "
            "-- but the mechanism is <b>not attributed</b>: the 24-byte Context::exit symbol "
            "in the ELF does not establish that this callsite is inlined.<br><br><b>The axis "
            "is closed.</b> The 2.14 M steps a block of dead frames are real and all three "
            "ways of removing them cost more than they save.<br><br>Controls, this arm: both "
            "arms carry 9,293 movmem sites, so the DMA chain is the same on each; the OFF "
            "arm builds to f83b995095d1, identical to the reference guest built separately; "
            "roots 504/504.<br><br>Earlier controls: with the flag off the ELF "
            "is 9085598f, identical to the TABLE_ARG build, and the whole exit_h construct plus the "
            "diagnostic are now removed from the tree."},

    {'id': 'swapscratch', 'verdict': 'REFUTED', 'branch': 'A/B measured — FINDINGS §161',
     't': "SWAP's 64-byte stack scratch, and whether the lane exchange removes it at a profit",
     'num': 'REGRESSION: +0.9 % to +1.8 % steps. The scratch goes; the exchange costs 4.5x more',
     'w': "The lane loop already existed, under <code>#if defined(MONAD_ZKVM_SP1)</code> -- it is "
          "there because rv32 lowers the 32-byte uint256 copies to memcpy CALLS. Routing ZisK down "
          "the same path removes exactly the expected 317,067 steps a block of scratch (handler "
          "frame total 2,530,003 -&gt; 2,212,936, the difference to the step) and costs far more "
          "than that: <b>swap goes from 19.0 to 28.0 steps an execution</b>, +1,426,806 a block over "
          "158,534 executions.",
     'fix': "None, and the reason is the point: with <code>-mzisk-dma</code> the two assignments "
            "lower to four <code>dma_xmemcpy</code>, and DMA is cheaper than a four-lane scalar "
            "exchange. The scratch is what those copies cost, not waste beside them.",
     'rem': "<b>Two of my framings were wrong here.</b> Calling it 'pure codegen waste' was wrong -- "
            "Anne-Laure caught that -- and so was expecting the lane loop to win once the scratch "
            "was correctly separated from the ra frames. A per-handler frame count files SWAP's sp "
            "adjustment beside the ra frames of push and mload because SWAP saves no return address; "
            "they are not the same thing.<br><br>The flag is removed "
            "from the tree and the build returns to 9085598f, the committed lever.<br><br>Written "
            "up in FINDINGS &sect;161 with the exit_h refutation, which is the same family: the frame "
            "cost is real and neither route collects it. It is <b>not</b> in a commit -- a measured "
            "refutation belongs in the findings, not in the source at the branch."},

    {'id': 'superinstr', 'verdict': 'CONFIRMED', 'branch': 'A/B measured — four flags, one per fusion',
     't': 'Fuse hot opcode sequences, taking the ones ZiskEthOne actually ships',
     'num': 'LANDED 6318098a9 — steps −4.498 % median, COST −2.131 %, 200/200 blocks and roots',
     'w': "Ported from <code>cpp-guest/zisk/evm/fused_dispatch.inl</code> in the ziskethone "
          "submodule, which is a specification and not just a commit message: an all-or-nothing "
          "contract, gas charged as literals with a static_assert that the price is "
          "revision-invariant, a safe peek because code is STOP-padded, and its own list of "
          "candidates rejected as measured-negative.<br><br>monad needs neither the literal gas nor "
          "the all-or-nothing contract. Its handlers already charge through "
          "<code>opcode_table&lt;traits&gt;</code>, so a fused follower can reuse "
          "<code>MONAD_VM_CHECK</code> against the stack its predecessor leaves and stay "
          "revision-exact by construction. Two new primitives carry that: "
          "<code>MONAD_VM_CHECK_AT(OP, SHIFT)</code> and "
          "<code>MONAD_VM_FUSED_NEXT(NBYTES, DELTA)</code>, both codegen-neutral with every flag "
          "off.",
     'fix': "Measured one at a time, against the landed dispatch lever, on 25815000/100/199:<table>"
            "<tr><th>fusion<th>steps<th>roots</tr>"
            "<tr><td>JUMP/JUMPI swallow the landing JUMPDEST<td class=num>−0.350 / −0.410 / −0.249 %"
            "<td>200/200</tr>"
            "<tr><td>PUSH1 + {ADD, SHL, SHR, SAR}<td class=num>−0.878 / −0.980 / −0.627 %"
            "<td>200/200</tr>"
            "<tr><td>PUSH2 + JUMP/JUMPI<td class=num>−1.442 / −1.745 / −1.058 %<td>200/200</tr>"
            "<tr><td>ISZERO/EQ + PUSH2 + JUMPI<td class=num>−1.871 / −2.133 / −1.313 %<td>200/200</tr>"
            "<tr><td><b>all four, over the full canonical 200</b><td class=num>"
            "<b>−4.498 % median, −4.544 % aggregate</b><td><b>200/200</b></tr>"
            "<tr><td>POP + POP<td class=num>+0.089 / +0.121 / +0.079 %<td>REFUTED</tr></table>"
            "They overlap: the four sum to 4.541 % on the first block and deliver 3.835 %, because "
            "the JUMPDEST swallow is also inside the two jump fusions. The three-block sample "
            "understates it: −3.835 / −4.431 / −2.740 % against a 200-block median of −4.498 %."
            "<br><br>COST on 20 blocks: median −2.131 %, aggregate −2.237 %, range −2.957 % to "
            "−0.857 %, 20 of 20 improving. Inside ZiskEthOne's own claim for the same patterns "
            "(−2.2 % to −10.3 % steps, −1.2 % to −6.0 % COST).",
     'rem': "<b>The gate is the design.</b> PUSH1's followers behind a chain of four byte compares "
            "measured <b>+1.360 / +1.483 / +0.663 %</b>; the identical fusion behind a 64-bit "
            "bitmap test measured <b>−0.878 / −0.980 / −0.627 %</b>. A 2.24-point swing with no "
            "change to what is fused -- eight instructions on every PUSH1 to save a dispatch on one "
            "in seven. Every gate here is four instructions or fewer.<br><br>POP+POP fails for the "
            "same arithmetic and cannot be rescued: its gate is already a single compare, and one "
            "saved dispatch on 27 % of pops does not cover a peek on all of them. ziskethone "
            "rejected SWAPn+{SWAPm,POP,PUSH1} and SGT+ISZERO on exactly this ground.<br><br>Two of "
            "their fusions are not here. <b>DUPn+MUL</b> (1,158 absorbed a block, the smallest) "
            "would need <code>checked_runtime_call</code> rewritten, because monad reaches MUL "
            "through the arith256 precompile, which reads its operands from the stack. "
            "<b>PUSH1+{PUSH1, DUP2}</b> have followers at 0x60 and 0x81, outside a 64-bit mask, and "
            "both write their operands to memory anyway, so they save a dispatch and not a round "
            "trip -- the half the bitmap experiment showed a wider gate would eat.<br><br>Every arm "
            "is gated alone, not only in combination. The PUSH1 arm went unrun at first and is the "
            "one that most needed it: <code>SHL</code>, <code>SHR</code> and <code>SAR</code> take "
            "(shift, value) in that order, so a swapped operand in the fused path shows up nowhere "
            "except in a post-state root. It passes 200/200 "
            "(<code>results/gates/fu-p1-canonical200.tsv</code>), and the corpus exercises all four "
            "followers -- SAR least, on the order of 300 executions a block.<br><br>Landed "
            "behind <code>MONAD_ZKVM_FUSE</code>, with the four per-fusion macros still reachable as "
            "raw <code>-D</code> so any one can be dropped and re-measured. Grafted onto the branch "
            "head rather than copied over it: another session had committed the keccak memo "
            "(6fe5634e9) on top of the dispatch lever, and overwriting CMakeLists.txt wholesale "
            "would have deleted their option."},


    {'id': 'batchcommit', 'verdict': 'REFUTED', 'branch': 'instrumented — FINDINGS §162',
     't': 'A sparse batch commit over the union of the touched paths',
     'num': 'ceiling 400,765 steps a block, 0.43 % — the write descent repeats only 1.3-1.6x',
     'w': "Measured, not modelled: <code>zkvm/category/core/trie_overlap.hpp</code> counts node "
          "visits against DISTINCT NodeIds, with two bitsets because an id below OVERLAY_BASE is a "
          "blob offset and above it an overlay index. Zero out-of-range ids on every block, so the "
          "distinct counts are exact. Emitted as a tail after the public values, so the run that "
          "reports is the run whose roots are checked; flag off, the ELF is byte-identical to "
          "pristine.<br><br>upsert_node visits 13,458 / 6,674 / 2,117 against 8,609 / 4,976 / 1,657 "
          "distinct -- <b>1.56x, 1.34x, 1.28x</b>. At this build's own 171.6 steps a visit the "
          "2,336 repeated visits a block are 400,765 steps.",
     'fix': "None worth writing. A batch commit still visits every distinct node once and adds "
            "collecting and deduplicating the touched paths, work the current code does not do at "
            "all. It would have to cost under ~40 % of what it saves, against a 0.43 % prize.",
     'rem': "Invalidations are not a separate prize: all four <code>hashes_.erase</code> sites are "
            "inside upsert_node, so the 7,503 repeated invalidations a block are already inside its "
            "171.6 steps.<br><br>The instrumentation's real output is the <b>split</b>: the write "
            "descent's repeats and the read descent's are disjoint populations, so this lever and "
            "leafrowlink cannot both collect the same steps. See §162."},

    {'id': 'leafrowlink', 'verdict': 'REFUTED', 'branch': 'quantified — FINDINGS §162',
     't': 'Keep a leaf\'s hash, NodeId and path after the first resolution, to skip re-descending',
     'num': 'ceiling 256,021 steps a block — 0.274 % of steps, before its own lookup cost',
     'w': "find_original visits 39,023 / 18,224 / 4,625 nodes against 19,229 / 9,614 / 2,965 "
          "distinct, so half the read descent re-walks nodes this block already walked -- but that is "
          "<b>not this lever's ceiling</b>, and an earlier version of this entry wrongly said 0.62 %. "
          "Two distinct keys share ancestors, and a lazy per-key link must still walk them on the "
          "FIRST access to each key. Most of those 10,021 repeated visits a block are shared-ancestor "
          "traffic between different keys, which no per-leaf cache collects.<br><br>What it is worth "
          "needs three counts: calls per logical key, visits during each key's first call, and visits "
          "during later calls for the same key. Only the third is collectable lazily.<br><br><b>Taken, "
          "and it closes the lever.</b> A key is looked up <b>1.15 to 1.22 times</b> -- the one-entry "
          "account memo and the sroot_ cache already absorb almost every repeat. Later-call visits are "
          "4,454 a block, 44 % of the repeated node visits and none of the shared-ancestor traffic: "
          "256,021 steps, <b>0.274 % of steps</b> before the lever's own lookup cost.",
     'fix': "Not designed. The population is read-descent repeats, which a per-leaf cache of "
            "(hash, NodeId, path) would answer without touching the witness format.",
     'rem': "The read descent's total overlap, 576,082 steps a block, is an upper bound on a "
            "<b>priming-built sidecar</b> instead -- a different lever that can exploit shared "
            "ancestors because it resolves every key up front, and that pays its own construction "
            "cost.<br><br>This is the transposition of what ziskethone gets from binding a key to its leaf once "
            "per leaf, done without adopting prefix-order -- which is closed at <b>+0.28 % COST</b>. "
            "A dense sidecar validated at priming would go further but changes the witness format, "
            "so it is a separate decision.<br><br>An estimate of 1-2 M steps for this was the right "
            "order, but 0.58 M was the wrong quantity to compare it against.<br><br>Whatever the "
            "key-level count gives has to be converted to <b>COST</b> as well as steps: a descent's "
            "instruction mix is memory-heavy and the guest's global COST-per-step ratio does not "
            "apply to it -- and no COST figure is offered here, because none is available by "
            "attribution. Reconstructing per-symbol COST from COST BY BASE OPCODE gives <b>807 %</b> "
            "of the reported OPCODES total and omits <code>copyb</code>, the largest single op; MEMORY "
            "is a lump with no per-instruction breakdown. An earlier bracketed range is withdrawn: it "
            "assumed the guest's average mix, which is an assumption and not a bound.<br><br>The "
            "attempt did establish one thing against that assumption: find_original touches memory on "
            "<b>13.7 % of its steps against the guest's 32.5 %</b> -- which invalidates the "
            "average-mix assumption and supports no conclusion about COST either way. A memory "
            "instruction share is not a cost model.<br><br>What survives is the <b>sidecar</b>, and its ceiling is not this lever's "
            "overlap -- see sidecar."},


    {'id': 'sidecar', 'verdict': 'REFUTED', 'branch': 'built and A/B measured — FINDINGS §163',
     't': 'A dense sidecar built at priming: key to leaf, resolving every read without a descent',
     'num': 'REGRESSION +9.311 % steps. The index is correct, complete, and 7.3x too expensive',
     'w': "Built, and it needs <b>no witness format change and no validation</b>: the priming sweep "
          "already reconstructs the pre-state trie and binds it to the pre-state root, so a top-down "
          "descent can derive each leaf's 64-nibble path itself and the key-to-leaf binding is "
          "structural. That is the cheap version of the lever.<br><br>It serves <b>100 % of lookups "
          "with zero fallback</b> and a differential arm confirms it <b>agrees with the descent on "
          "every key</b>. Roots OK. It is simply far too expensive: +9.129 / +8.652 / +14.984 %, mean "
          "+8,712,338 steps a block against a 1,185,581 ceiling.",
     'fix': "None. The cost is structural, not an implementation detail.",
     'rem': "<b>Why, and it is the architectural point.</b> A top-down traversal must touch every node "
            "in the witness trie -- 185,856 on the first block, some 85,000 of them digests standing "
            "in for untouched subtrees -- while the runtime descents it replaces visit only 20,624. "
            "The witness carries an order of magnitude more nodes than the block's accesses touch, so "
            "indexing all of them costs nine times what using the index saves.<br><br>ziskethone does "
            "not pay this because its witness IS prefix-order with plaintext keys: the binding is "
            "verified once per leaf as the stream is read, with no separate traversal. <b>The advantage "
            "is in the format and it is not transposable at this end.</b><br><br>Two implementation "
            "traps worth keeping: <code>OffsetTrie::root</code> is not const, so a miss on a root the "
            "walk never covered is not an absence -- the first version ran 80 % faster with wrong "
            "roots. And an earlier arm reported the walk at +126,844 steps, 10.7 % of the ceiling, "
            "from a build where <code>sidecar_index()</code> was never called: the flag compiled dead "
            "code. Retracted in §163."},


    {'id': 'plinkindex', 'verdict': 'REFUTED', 'branch': 'built and A/B measured — FINDINGS §164',
     't': 'A leaf index from parent links recorded by the bottom-up sweep that already runs',
     'num': 'REGRESSION +5.658 % steps; the link stores ALONE are 1.4x the whole budget',
     'w': "The variant that escapes §163's objection: no second traversal. The constructor's sweep "
          "already reads every child of every node to validate offsets, so it records "
          "<code>child -&gt; (parent, slot)</code> there and paths are rebuilt for the LEAVES ONLY by "
          "walking up. Budget: construction plus lookups under 1,185,581 steps.<br><br>It works -- "
          "185,855 links, all 4,048 leaves indexed, 6,393 of 6,403 lookups served, differential agrees "
          "on every key, 200/200 roots -- and costs <b>+5,294,052 steps a block</b>.",
     'fix': "None. The cost is in the destination, not the traversal.",
     'rem': "<b>The graft alone is already over budget:</b> +1,668,835 steps a block for the link "
            "stores, measured before the index was built or consulted, 1.4x the entire budget at about "
            "nine steps a store.<br><br>That is the transferable part. The premise -- the sweep already "
            "reads the child, so remembering where it came from is nearly free -- is false because the "
            "DESTINATION is not free. Node ids are sparse blob offsets, so the table is indexed by "
            "<code>offset &gt;&gt; 2</code> over a buffer proportional to the blob and every store is a "
            "scattered write into cold memory. Same wall that killed <code>dirtymark</code>, where a "
            "flat store over that domain regressed 0.66 %. Reading a child during a linear sweep is "
            "cheap; writing anything keyed by it is not."},


    {'id': 'mularith', 'verdict': 'REFUTED', 'branch': 'A/B measured — ziskethone routing, transplanted',
     't': 'Route MUL through arith256, the way ziskethone does',
     'num': 'REGRESSION: +0.19 % COST',
     'w': "Monad executes more scalar sub, shift, compare and multiply than ziskethone does, and "
          "their guest routes MUL into <code>arith256</code>. The family ratio that motivates this "
          "is misleading on its own -- they displace or inline the same work elsewhere -- so the "
          "transplant is the only honest test of it.",
     'fix': "None. Reverted.",
     'rem': "<b>The target is too small to carry the staging.</b> On block 25815100, "
            "<code>mul</code> is 495,779 executions, 0.56 % of steps and <b>0.32 % of COST</b>: "
            "removing every multiply in the guest would not return a third of a point, and the "
            "routing costs more than that to stage against our stack layout.<br><br>Scalar "
            "arithmetic is not where the scalar cost is. Comparisons are: <code>ltu</code>, "
            "<code>lt</code> and <code>eq</code> are 7,459,614 executions and <b>2.94 % of COST</b>, "
            "nine times <code>mul</code>. See FINDINGS 49 for where they come from."},

    {'id': 'slotmask', 'verdict': 'REFUTED', 'branch': 'not attempted — closed by two measurements',
     't': "Skip a branch's empty slots with a presence mask and ctz, instead of walking all sixteen",
     'num': 'ceiling ~28,785 empty slots a block, and nowhere to put the mask',
     'w': "encode_rlp's branch arm iterates all sixteen slots whatever the node holds, and the loop "
          "dispatch is 11 % of its 1,480,811 comparisons (FINDINGS 49).",
     'fix': "None.",
     'rem': "<b>Two independent reasons, either sufficient.</b><br><br>The branches are nearly "
            "full: <b>12.21 of 16 children present</b> on block 25815100, not the 4.3 I asserted "
            "from a corpus average I had not re-measured on this block. That is 28,785 empty slots "
            "in the whole block -- a ceiling around 0.17 % of steps, and less in COST. The lever "
            "aims at three empty slots a branch, not eleven.<br><br>And there is nowhere to keep "
            "the mask. encode_rlp is given a NodeId, which is a blob offset, and nodes have "
            "variable size, so no dense rank is derivable from an offset: indexed by offset the "
            "mask is <b>6.4 MB</b> against a 3.4 MB blob; indexed by branch rank it is 15 KB plus "
            "the offset-to-rank map that is exactly the hashmap ruled out. Recomputing it inside "
            "encode_rlp rescans the sixteen slots it exists to avoid."},

    {'id': 'postorder', 'verdict': 'REFUTED', 'branch': 'A/B measured twice — a2742c15c and c59b7ab05',
     't': 'Prime by descending from the root instead of walking the blob as a tiling',
     'num': 'REGRESSION +3.905 % COST / +6.690 % steps on c59b7ab05 (2026-08-28); +3.40/+4.27 % on a2742c15c',
     'w': "<code>is_valid_offset</code> -- the children-before-parents check the linear walk owes on "
          "every child -- is <b>56 % of the constructor's comparisons</b> (FINDINGS 49), and the "
          "constructor is 6.9 % of the block's. A descent from the root needs none of it.",
     'fix': "None. The first attempt's patches are kept under "
            "<code>profiling/experiments/post-order-descent/</code>; they no longer apply, the "
            "constructor having moved, and were re-implemented for the second measurement.",
     'rem': "<b>Re-measured because the first verdict predated three landed levers</b> -- DMA, "
            "table-arg and fusion -- two of which had already flipped other rejections. It did not "
            "flip this one: +6.492/+6.156/+11.908 % steps and +3.910/+3.627/+5.009 % COST on "
            "25815000/100/199, ELF 13c4a5b66467 off against 3327534616f2 on, both arms with every "
            "performance option ON and the DMA chain confirmed on each.<br><br>It moved the wrong "
            "way, and necessarily: those levers shortened everything else, so the fixed cost of "
            "materialising an order the byte layout used to supply takes a larger share of a smaller "
            "total. An absolute cost does not improve when the denominator shrinks.<br><br>The stop "
            "threshold set before the run was <b>+0.5 % COST</b>. At eight times it, the adversarial "
            "audit a positive would have required -- cycles, aliasing, unreachable nodes, an "
            "invariant equivalent to the postorder -- was <b>not pursued</b>. <b>504/504 roots "
            "pass</b>: the rejection is the materialised stack, not correctness."},

    {'id': 'undoptr', 'verdict': 'DEFERRED', 'branch': 'not attempted',
     't': 'Undo records key by Address; a pointer or index would skip 1,252 lookups a block',
     'num': 'ceiling 89-121 k steps a block (0.08-0.11 %), plus 12 bytes off each record',
     'w': "pop_reject looks a row up by address once per record replayed -- 1,252 a block for 73 "
          "rejects, about 17 records each.",
     'fix': "Deferring the erases to the end of a pop_reject is NOT enough to make pointers safe: a "
            "child's reject erases while the PARENT's journal still holds pointers into current_. It "
            "needs the created rows proved to be a strict LIFO suffix, or tombstones, or pointer "
            "repair -- which is the stable-id project the numbers above just deprioritised.",
     'rem': "0.1 % is not worth a new rollback invariant. Reopen with stable ids, if they are ever "
            "justified by something else."},
]

# ── what the measurements point at next ─────────────────────────────────────────────────────────
NEXT = {
    't': 'The descent routes are closed; the state axis is not — they were 24 % of it',
    'num': 'landed and pushed: dispatch base −2.771 % steps, fusions −4.498 % steps / −2.131 % COST',
    'why': "Their advantage is an architecture that holds end to end. Three routes transpose its "
           "per-leaf key binding into the current witness format; all three are now implemented, "
           "root-gated and negative. The cheapest possible version -- reusing a traversal that already "
           "happens, one word per node, paths rebuilt for leaves only -- still costs more than the "
           "descents it removes, because node ids are sparse blob offsets and the store, not the "
           "traversal, is what is expensive.",
    'plan': ["DONE — the interpreter: dispatch base (06d6ef114) and fused superinstructions "
             "(6318098a9), both gated 200/200 and pushed.",
             "DONE — the modified-path overlap: 1.28-2.03x, closing the batch commit at 0.428 %. §162.",
             "DONE — the key-level count: a key is looked up 1.15-1.22 times, closing the lazy "
             "leaf-to-row link at 0.274 %. §162.",
             "DONE — the top-down sidecar, built and A/B'd: +9.311 %. §163.",
             "DONE — the parent-link index grafted onto the existing sweep: +5.658 %, and the graft "
             "alone 1.4x the budget. §164. This was the last untested route, so the claim that the "
             "gap needs their architecture and not a transposition is now measured, not asserted.",
             "CORRECTION — §165: the trie descents are 2,531,243 steps a block against a state/trie "
             "gap of +10,468,365 on the same blocks. Refuting all three descent routes left about three "
             "quarters of the axis untouched. Two things hold whatever the classification: they have no "
             "descent at all: access is Storages::find, Accounts::index_of, mark_touched, 1.74 M a "
             "block. The post-root comparison is WITHDRAWN -- reduce_branch serves the parse as well "
             "as the post-root, so its 4.57 M cannot be attributed to the second by name; only "
             "eval_node (2.33 M) is certainly post-root.",
             "DONE for OUR side — §166, one ELF with the cutoff chosen at runtime, no "
             "taxonomy in the phase assignment. Construction plus pre-root 16.94 M (18.1 %), EVM "
             "execution 67.68 M (72.3 %), commit 3.22 M (3.4 %), post-root 5.73 M (6.1 %). Full "
             "mode root-gated 200/200; the executed prefix differs across modes only by each "
             "one's exit path -- 3,386 / 4,318 / 29 steps, named by symbol. A separate-ELF arm "
             "agrees to 4,000 steps on 93.5 M, a genuine cross-check -- unlike the four slices "
             "summing to the family total, which is an algebraic identity and was wrongly "
             "offered as one.<br><br><b>The commit is small and the bulk of our state/trie work "
             "is in the state layer during execution, not in find_original or upsert_node.</b>",
             "NEXT — the same method on THEIR side: construction, EVM access, "
             "calculate_new_state_root. The crux is that reduce_branch is called from both the parse "
             "and the post-root, so its 4.57 M can only be split by separating the call sites "
             "dynamically. No comparison is claimed until that exists. A name-based split left "
             "7.6 M of ours and 2.0 M of theirs unclassified, more than most of the deltas it was "
             "meant to explain, and opening the residual immediately corrected two buckets. No lever "
             "should be aimed at this axis until that is done.",
             "RESIDUE, all under a few tenths of a point: a multi-entry storage-root cache, a direct "
             "append_acct encoding, nibble_mismatch widening at 279 k.",
             "WHAT IS LEFT is not a lever: a prefix-order witness with plaintext keys, taken together "
             "with the dense tables and the array post-root. As a pure format swap prefix-order is "
             "closed at +0.28 % COST; it only pays as part of the whole design, which is an interface "
             "decision.",
             "CLOSED — general post-order, prefix-order as a swap, replacing the storage HAMT, the "
             "allocator, dropping more witness nodes, the batch commit, the lazy link, the top-down "
             "sidecar, the parent-link index, exit_h and the SWAP scratch."],
}


def render(out):
    e = html.escape
    h = ["<meta charset=utf-8><title>levers r10</title>", """<style>
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:1000px;
margin:0 auto;padding:28px 20px 80px;color:#1a1a1a;background:#fff}
h1{font-size:23px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 10px;border-bottom:1px solid #e5e5e5;padding-bottom:5px}
.sub{color:#666;font-size:13px;margin:0 0 22px}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13.5px}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #eee}th{color:#666;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.card{border:1px solid #e5e5e5;border-left-width:4px;border-radius:5px;padding:13px 15px;margin:0 0 13px}
.CONFIRMED{border-left-color:#1a7f37;background:#f6fef8}.REFUTED{border-left-color:#b02020;background:#fff7f7}
.DEFERRED{border-left-color:#a06000;background:#fffdf5}
.CANDIDATE{border-left-color:#1f5fa8;background:#f5f9ff}
.REJECTED{border-left-color:#b02020;background:#fff7f7}.REJECTED .v{background:#b02020}
.v{font-size:11px;font-weight:700;letter-spacing:.4px;padding:1px 6px;border-radius:3px;color:#fff}
.CONFIRMED .v{background:#1a7f37}.REFUTED .v{background:#b02020}.DEFERRED .v{background:#a06000}
.CANDIDATE .v{background:#1f5fa8}
.t{font-weight:600;margin:6px 0 3px}.n{font-variant-numeric:tabular-nums;color:#333;font-size:13px;margin:0 0 8px}
.b{color:#333;font-size:13.5px;margin:6px 0}.b b{color:#000}
code{background:#f2f2f2;padding:1px 4px;border-radius:3px;font-size:12.5px}
.rem{color:#666;font-size:12.5px;border-top:1px dotted #ddd;padding-top:7px;margin-top:9px}
ol{font-size:13.5px;color:#333}
</style>"""]
    h.append(f"<h1>levers — Monad ZisK guest on {e(BASE['branch'])}</h1>")
    h.append(f"<p class=sub>tip <code>{e(BASE['tip'])}</code> · {e(BASE['corpus'])} · "
             f"{e(BASE['runtime'])} · every ratio is the median of per-block ratios from an A/B on "
             f"that corpus.<br>Starting point: {e(BASE['gap'])}.</p>")

    h.append("<h2>where the guest spends</h2>")
    h.append("<p class=sub>50 blocks, exact — the profile accounts for 100.00 % of the real steps of "
             "those blocks. Cut by symbol, not by family: which family a node-RLP symbol lands in is "
             "a naming question that has already moved once and took a headline with it.</p>")
    h.append("<table><tr><th>what<th>Monad r10<th>ziskethone<th>gap</tr>")
    for name, a, b in SPEND:
        h.append(f"<tr><td>{e(name)}<td class=num>{a:,}<td class=num>{b:,}"
                 f"<td class=num>{a-b:+,}</tr>")
    h.append("</table>")

    # A bare order[verdict] killed the whole page with a KeyError the moment a session introduced
    # a verdict this map did not list ('REJECTED', on prefixorder). Named, so an unknown verdict says
    # which lever carries it instead of failing as a stray KeyError -- and still fails, because
    # silently sorting an unrecognised verdict last would hide a typo.
    order = {'CONFIRMED': 0, 'CANDIDATE': 1, 'REFUTED': 2, 'REJECTED': 2, 'DEFERRED': 3}
    for L in TRIED:
        if L['verdict'] not in order:
            raise SystemExit(f"lever {L['id']!r} has verdict {L['verdict']!r}, which render() does "
                             f"not know; add it to `order` and give it a CSS class")
    h.append("<h2>measured on this base</h2>")
    for L in sorted(TRIED, key=lambda x: (order[x['verdict']], x['id'])):
        h.append(f"<div class='card {L['verdict']}'><span class=v>{L['verdict']}</span> "
                 f"<code>{e(L['id'])}</code> &middot; <span class=sub>{e(L['branch'])}</span>"
                 f"<div class=t>{e(L['t'])}</div><div class=n>{e(L['num'])}</div>"
                 f"<div class=b>{L['w']}</div><div class=b>{L['fix']}</div>"
                 f"<div class=rem>{L['rem']}</div></div>")

    h.append("<h2>next</h2>")
    h.append(f"<div class='card DEFERRED'><div class=t>{e(NEXT['t'])}</div>"
             f"<div class=n>{e(NEXT['num'])}</div><div class=b>{NEXT['why']}</div><ol>"
             + "".join(f"<li>{e(s)}</li>" for s in NEXT['plan']) + "</ol></div>")
    h.append(f"<p class=sub>rendered {time.strftime('%Y-%m-%d %H:%M')}</p>")
    open(out, 'w').write("\n".join(h))
    print(f"wrote {out}  ({len(TRIED)} levers on {BASE['branch']})")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(HERE, 'results', 'levers-r10.html'))
    render(ap.parse_args().out)
