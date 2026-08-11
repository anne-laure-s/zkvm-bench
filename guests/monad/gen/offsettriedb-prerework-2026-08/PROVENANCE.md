# generation `offsettriedb-prerework-2026-08` — RETIRED, record only

> ## ⚠️ The data is gone. This file is what is left, and it is the point.
>
> **Deleted 2026-08-10**: `witnesses/` (504 pairs, 3.5 GB) and `elf/` (2 ELFs, 6.9 MB). The set was
> fully shadowed — the rework generation covers the identical block range, so no lookup could ever
> reach it again — and it cost 3.5 GB to keep a copy nothing could read.
>
> Everything below is preserved **unchanged**. It is the only remaining record of what produced the
> figures published between 2026-08-04 and 2026-08-08, and of why those figures are not comparable to
> anything measured after the reader rework. `use-gen` still lists this generation, at `0 witnesses`,
> and refuses to select it.
>
> **To resurrect it**: replay on the devcore box from snapshot 25551990 with the pre-rework guest — see
> `infra/monad-witness/README.md`. Cheaper in most cases: re-measure on the current generation and
> retire the old figures rather than reproduce them.

The OffsetTrieDb witnesses as they were **before** the reader rework of 2026-08-08 — the set the
2026-08-04 numbers were measured on, and the only thing that can tell you whether a figure from that
week predates the rework.

**Renamed 2026-08-10** from `offsettriedb-2026-08`. The bare name dated from when it was the only
offset generation; once the rework produced a second one, "the offset generation" stopped being a
unique description. Nothing but the `current` symlink referenced the old name.

## Guest
Execution path `OffsetTrieDb`, pre-rework reader.

| elf | sha256 |
|---|---|
| `monad-zkvm-guest-zisk.elf` | `6f1788322ab4065f…` |
| `monad-zkvm-guest-sp1.elf` | `8d77155d76a2dc8b…` |

Both dated 2026-08-04. These are the ELFs `use-gen` installed at the top level while this generation
was current, and therefore the ones the pre-rebase `zisk` / `sp1` axes of `profiling/compare.py` read.

## Witnesses
Wire format: field **[1]** starts with `4d5a5701` (`MZW\x01`) — the offset blob.

- pairs: **504** × (`.witness` + `.post_state_root`)
- range: **25551991 … 25552494**, contiguous: **yes**
- median size 7.35 MB, total 3.78 GB

## ⚠️ Indistinguishable from `offsettriedb-rework-2026-08` by inspection

This is the trap this file exists to record. Against the rework generation, this set has:

- the **same** wire format word (`offset`) and the same magic `4d5a5701` — `./witness-fmt` reports
  them identically, and `use-gen`'s mixed-format guard therefore **cannot** catch a mix of the two
- the **same** block range, the same 504 pairs, the same per-block byte size to the byte
- **identical** `post_state_root` files, 504/504
- identical field **[0]** (the block RLP); the entire difference — ~2.3 % of bytes — lives inside
  field [1], the offset blob, whose internal layout the reader rework changed

So the only thing separating them is which directory they sit in. Never copy witnesses between the
two generation directories, and never add a second lookup path ahead of `guests/monad/fixtures` in
the tooling — that is precisely how the two sets drifted apart between 2026-08-08 and 2026-08-10.
