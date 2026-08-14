# generation `storageroot-det-2026-08`

Generated 2026-08-12 by `infra/monad-witness/witness-backfill` on `nyc-003`. Witnesses and
ELFs come from a single checkout — that pairing is the point of a generation directory.

- **branch** `al/zkvm-sam-gen`
- **commit** `848a45572`
- **host** `nyc-003`
- **execution** `--nthreads 1 --nfibers 1` — serialised, so this corpus is REPRODUCIBLE

Those three lines are machine-read: a later run compares its freshly built ELFs against every
generation here and, on a match, reads **commit** back to tell "same guest, same tree" (reuse this
set) from "same guest, different tree" (dump_witness may have moved — regenerate). Keep the field
shape if you edit this file.

## Witnesses

504 blocks, **25551991..25552494** (contiguous), wire format `4d5a5701 offset`. The same block set as the
generation this replaced, so figures are comparable across generations rather than merely similar.
Replayed from snapshot `25551990` with a block_db pruned to `25551735..25552494` — the 256
leading blocks are the BLOCKHASH ancestor window, read for headers and never replayed.

## Guests

| elf | sha256 |
|---|---|
| `monad-zkvm-guest-zisk.elf` | `13158c9e089f0d6584ca8bebb5e672466668fafcaa4278adf1d38042d3eed7e5` |
| `monad-zkvm-guest-sp1.elf` | `977721c4ea2d4d201d79de4e34c48cfea2020820251463854811f3664fb0bd07` |

## Select it

```sh
cd guests/monad && ./use-gen storageroot-det-2026-08
```

`use-gen` re-checks the witness format across the set before switching; nothing here is trusted
on the strength of this file.
