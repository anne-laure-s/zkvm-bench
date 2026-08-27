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
    'tip': '2cf634720',
    'corpus': 'canonical-2026-08-25815000-25815199-d49075fa3 (200 blocks)',
    'runtime': 'ziskos 1.1.0-alpha',
    'vs': 'r10-vs-ziskethone',
    'gap': '+7.5 % steps / +6.4 % COST against ziskethone, measured before the state levers below',
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
    't': 'Trie node lookup — upsert_node and find_original, with append_path now done',
    'num': '5.03 M steps a block on the 3-block profile; the whole state-access gap is 11.39 M',
    'why': "The rows axis is closed: every remaining lookup class is at or under 0.5 %, and the "
           "largest of them is only partly reducible. This is 40 times the budget of the lever that "
           "was next in line. ziskethone has no separable counterpart -- its equivalent work is "
           "inside eval_node and reduce_branch, counted as node building -- so it gets no ratio, "
           "and inventing one from its zero would repeat finding 142.",
    'plan': ["DONE — calls and unit cost: upsert_node 8,074 a block at 235.5 steps, append_path "
             "1,372 at 1,080, find_original 3,781 at 376.4 over a mean descent of 6.23 nodes, so 60 "
             "steps a node step.",
             "DONE — overlap: append_path is called from append_ext, append_acct, append_storage and "
             "clone_acct, all node builders inside upsert_node's operation. One upsert in six builds "
             "a node with a path.",
             "DONE — redundant descents: the accounting closes exactly, 665 read_account plus 666 "
             "sroot_ misses plus 2,450 slot descents = 3,781. The one-entry sroot_ cache misses "
             "27 % of read_storage calls, so 666 re-descents from the root, about 250 k steps a "
             "block. Widening it pays the probe on every call, as the account memo did -- measure "
             "the probe before building it.",
             "DONE — append_path, out of order: its cost per call said so and the fix was already "
             "in the tree.",
             "DONE — find_original's distribution: BRANCH is 86.3 % of node steps and compares "
             "nothing, EXT 0.3 %, leaves 13.4 %. Of the comparisons, 45.5 % had mismatched parity "
             "and walked 67,264 nibbles a block. Fixed by the packed primitive above.",
             "NEXT — the BRANCH step itself: 19,324 a block, 86 % of the descent, and untouched. It "
             "reads one nibble, indexes a child and shortens the key; whether that is 20 steps or "
             "60 decides whether there is anything left here.",
             "AFTER — upsert_node at 1.90 M and 235.5 steps a call, and the node container."],
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
.v{font-size:11px;font-weight:700;letter-spacing:.4px;padding:1px 6px;border-radius:3px;color:#fff}
.CONFIRMED .v{background:#1a7f37}.REFUTED .v{background:#b02020}.DEFERRED .v{background:#a06000}
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

    order = {'CONFIRMED': 0, 'REFUTED': 1, 'DEFERRED': 2}
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
