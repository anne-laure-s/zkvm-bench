# monad-levers

The Monad guest built from branch `al/zkvm-levers` (11 commits on `ed16787ae`), for the
`levers-*` axes of `compare.py`. **Not what ships** — `guests/monad-{zisk,sp1}` are.

    monad-levers-zisk.elf   built on the devcore box, .text 1,410,308
    monad-levers-sp1.elf    built on the devcore box, .text 1,623,112

ZisK: 504 blocks measured through `guests/monad/ev.sh`, 504/504 post-state roots PASS.
SP1: cycles only — the runner does not expose the committed public values, so that side carries no
root verdict.

Neither carries the two submodule patches (`third_party/patches/`), worth a further 0.40 point.

## `jd` — table-driven JUMPDEST scan — CONFIRMED (2026-08-13)

**+1.02 % work, +0.90 % COST, 504/504 blocks.** Unanimous across the corpus, which is what a structural
saving looks like rather than a mix effect.

`find_jumpdests` runs on every code byte of every distinct contract a block touches, and because code
is cached by hash that cost is paid once per contract instead of amortised — 6.5 % of a large block,
the single hottest function in the guest. Per byte it did a load, a compare against JUMPDEST, two range
compares for `is_push_opcode`, a subtract for `get_push_opcode_index`, span indexing and a re-read of
`code.size()`. Now one 256-entry table gives the push-data length, the loop walks a raw pointer against
a hoisted end, the advance is a single add.

It removes only ~17 % of the function: a byte-at-a-time scan needs ~5 instructions per byte whatever
you do, and this is near that floor.

**No binary here — it landed in `al/zkvm-r4`** (verified: the guest built from that branch has the
sha256 the measurement used, `aefc6eed9e8414cb…`). The variant that produced the figure is gone with
it.

## `br` — branch children decided in pairs — REFUTED (2026-08-13)

`al/zkvm-r4-br`, built and measured, **−0.52 % to −0.98 %** against the jd build on three blocks. Not
taken to a corpus run: a lever that loses on every block sampled does not need 504 of them.

The branch node stores 16 contiguous `node_id_wire` slots, four zero bytes for an empty child, and no
live-child mask — so `encode_rlp` walks all 16 and `child_ref`'s fast head writes a single `0x80` for
each empty one. The idea: a pair of empty slots is eight zero bytes, so one 64-bit load and one compare
decide two children, and one two-byte store finishes them.

It loses. The probe costs more than it saves, which says two things worth keeping: **touched branches
carry more live children than "sparse" suggests**, and `child_ref`'s NULL path was already close to the
floor. The binary is not kept.

## `lazyjd` — JUMPDEST map built on first use — REFUTED (2026-08-13)

`al/zkvm-r4-lazyjd`, guest-only (the host shares `Intercode` across parallel transactions, so a
mutable build-on-read would race there). **−0.14 % to −0.36 %** against the `jd` build on three blocks.

The idea: code enters the by-code-hash cache for `EXTCODESIZE`/`EXTCODEHASH`/`EXTCODECOPY` and for
calls that revert before jumping, so scanning every byte of it in the constructor pays for maps nobody
reads. The trade is one branch per `is_jumpdest` against the whole scan of never-jumped code.

It loses, and the losing tells us the useful thing: **nearly all cached code is jumped into**, so the
saving almost never materialises while the branch is paid on every jump — and `jump`+`jumpi` are 4.7 %
of a block. The arithmetic was written down before building and it held.

Third lever in a row where a "skip the work" probe sits on a hotter path than the work it skips
(see `br` above). Worth treating as a prior: in this guest, the hot paths are close enough to their
floor that conditional avoidance loses unless the avoided work is large *and* the check is rare.
