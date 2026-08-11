# generation `offsettriedb-rework-2026-08` — current

The OffsetTrieDb witnesses **regenerated 2026-08-08**, after the reader rework changed the offset
blob's internal layout. Every post-rebase figure — the `cur-*` and `opt-*` axes of
`profiling/compare.py`, and the round-three optimisation campaign that followed the sam rebase — was
measured against this set.

**Filed 2026-08-10.** Until then this set lived at `guests/monad/fixtures-v2`, beside the generation
layout rather than inside it, with no provenance record. `compare.py` looked there *before*
`guests/monad/fixtures`, so it silently won every lookup while `use-gen` still reported the
pre-rework generation as current — the reports and the generation selector disagreed about their own
input for two days, and nothing could report the disagreement (see the warning below).

## Guests

Two lineages read this generation. The **canonical** pair — what `use-gen` installs at the top level,
and what `guests/monad-{sp1,zisk}/*.elf` therefore resolve to — is `monad-sam`:

| elf | sha256 |
|---|---|
| `monad-zkvm-guest-zisk.elf` (= `monad-sam-zisk`) | `f5cc8205c002fcfc80ebb0acc0de365a1a0ed8178e734e732a6837f0deb958db` |
| `monad-zkvm-guest-sp1.elf` (= `monad-sam-sp1`) | `ef02fbf9b6c926c40ab1c0c78259f9895246934c6c4061932c286a05d4500267` |

`monad-sam` is `origin/sam/zkvm-zisk-sp1` rebased — the baseline the optimisation work is
measured against. Built 2026-08-08.

The second lineage is `monad-r3` = `al/zkvm-r3`, that baseline plus the measured commits and the
soundness binding. It is **not** copied here — it stays at `guests/monad-variants/r3/`, the path
`compare.py`'s axes name — but it reads these same witnesses, so its identity belongs in this record:

| elf | sha256 |
|---|---|
| `monad-r3-zisk.elf` | `9b1aa3ab6e838dea4290bec9c14d5f693e75ef0a0746d1608006efd91778b801` |
| `monad-r3-sp1.elf` | `98a73869079e3b2bf74887dd2fdd12f33d6747eab9fae81a7a65e93cc71314a8` |

Built 2026-08-09.

## Witnesses
Wire format: field **[1]** starts with `4d5a5701` (`MZW\x01`) — the offset blob, post-rework layout.

- pairs: **504** × (`.witness` + `.post_state_root`)
- range: **25551991 … 25552494**, contiguous: **yes**
- median size 7.35 MB, total 3.78 GB

## ⚠️ Indistinguishable from `offsettriedb-prerework-2026-08` by inspection

Against the pre-rework generation, this set has the same format word (`offset`), the same magic
`4d5a5701`, the same block range, the same 504 pairs, the same per-block byte size to the byte, and
**identical** `post_state_root` files (504/504). Field [0] — the block RLP — is identical too; the
whole difference is ~2.3 % of bytes inside field [1], where the rework changed the blob layout.

Consequences, both load-bearing:

- `./witness-fmt` reports the two sets **identically**, so `use-gen`'s mixed-format guard cannot
  catch a mix of them. That guard protects against `rlp-list` ↔ `offset`, not against this pair.
- Feeding the pre-rework set to these ELFs (or the reverse) is not a clean failure — it is the
  garbage-parse / panic mode the generation layout exists to prevent, and here it is *invisible*
  ahead of time.

The only thing separating the two is which directory they sit in. So: never copy witnesses between
generation directories, and keep `guests/monad/fixtures` the single lookup head in the tooling.

## Expected public values (`.expected_pv`, added 2026-08-10)

The guest commits to **three** public values since the soundness binding — post-state root,
pre-state root, block hash — but this set originally shipped only `.post_state_root`, and `ev.sh`
checked that one with a SUBSTRING test (`exp in got`): it passed on a post root sitting anywhere in
the 96-byte output and said nothing about the other two thirds.

`gen-expected-pv.py` now writes `<block>.expected_pv`, 96 bytes, `post || pre || hash`, and `ev.sh`
compares it exactly and positionally (verdict `PASS(pv3)`, or `MISMATCH(post,pre,hash)` naming which
third broke). **503 of 504 blocks** carry one; the first block of the set is skipped because its
parent's post-root is not local.

Provenance differs per third, which is the point:
- **post** — the set's own `.post_state_root`, written by replay, which validates every state root
  against the canonical mainnet header before accepting a block.
- **pre** — the *previous* block's `.post_state_root`. Same source, so equally independent of the
  guest; this is the pre/post chaining check, now applied to every block instead of a sample.
- **hash** — `keccak(header RLP)` derived from the block RLP the witness carries. On its own that is
  only a format check, so it is cross-checked against the **next** block's `parent_hash`:
  **503/503 consecutive pairs agree**, giving one independent confirmation per pair across the whole
  set with no RPC. (A sample of 16 was also checked directly against mainnet RPC during the
  soundness work.)

Regenerate with `./gen-expected-pv.py <witness-dir>`; `--check-only` runs the chain cross-check and
reports without writing. The script bundles its own keccak-256 (with a self-check) so it runs
wherever the fixtures are — note that hashlib's SHA-3 is *not* this hash.
