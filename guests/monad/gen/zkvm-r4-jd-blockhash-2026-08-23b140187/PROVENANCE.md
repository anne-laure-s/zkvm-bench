# generation `zkvm-r4-jd-blockhash-2026-08-23b140187`

Built 2026-08-13 on `nyc-003`. Both ELFs come from one checkout — that pairing is the point of a
generation directory.

- **branch** `al/rtp`
- **commit** `23b140187`
- **host** `nyc-003`
- **execution** `--nthreads 1 --nfibers 1` — serialised, so this corpus is REPRODUCIBLE

Those three lines are machine-read: a later run compares its freshly built ELFs against every
generation here and, on a match, reads **commit** back to tell "same guest, same tree" (reuse this
set) from "same guest, different tree" (dump_witness may have moved — regenerate). Keep the field
shape if you edit this file.

## Witnesses

`witnesses` is a SYMLINK to `zkvm-r4-gen-2026-08-9d7540181/witnesses`, not a copy. The guest lineage
moved and the wire format did not, so the two generations read the same 3.5 GB corpus and only the
ELFs differ. What licenses the link: `git diff 9d7540181 23b140187` touches no witness-generation
file — not `witness_generator.{cpp,hpp}`, not `execution_witness.hpp` — so this tree emits the bytes
that set already contains.

504 blocks, **25551991..25552494** (contiguous), wire format `4d5a5701 offset`. The same block set as
`zkvm-r4-gen-2026-08-9d7540181` and `storageroot-det-2026-08`, so figures are comparable across
generations rather than merely similar.

## Guests

| elf | sha256 |
|---|---|
| `monad-zkvm-guest-zisk.elf` | `9b6fc3ed58d68120041d9c10574e3748154b1141338be479eda2f48e57149d1a` |
| `monad-zkvm-guest-sp1.elf` | `c3f19156017b48a4e42c5508fb6fd6df77a4d737f45187514c2d60de35b6141b` |

`23b140187` is `al/zkvm-r4-jd-blockhash` (`a9e953f35`) plus one host-side commit that adds
`--block-db-timeout` to the ethereum runloop. That commit touches `runloop_ethereum.{cpp,hpp}` and
`cmd/monad/main.cpp` only: `git diff a9e953f35 23b140187 -- zkvm/ category/vm/
category/execution/ethereum/` is empty, so the guests are the r4-jd-blockhash guests.

Over `9d7540181`, the lineage adds a table-driven JUMPDEST scan and chain-verification of the
BLOCKHASH ancestor headers.

## Verified by execution, not by provenance

Block 25551992, both guests against this corpus, post-state root compared to the recorded one:

| guest | result |
|---|---|
| `monad-zkvm-guest-zisk.elf` | 6 653 166 steps, root **matches** |
| `monad-zkvm-guest-sp1.elf` | root **matches** (`0x8beeb3a0…155d88`) |

Against `zkvm-r4-gen-2026-08-9d7540181`'s ZisK guest on the same block: 6 707 335 steps, so this
lineage costs **0.81 % fewer steps** there. One block is not a benchmark — run `ev.sh` across the set
for a figure worth quoting.

A matching magic number is NOT sufficient to pair a guest with a corpus. Three mutually incompatible
witness sets on nyc-003 all announce `4d5a5701`, and feeding the wrong one to a guest yields exit
code 0 with 256 zero bytes of public values — a wrong root reported as a clean run. Only the root
comparison above establishes the pairing.

## Select it

```sh
cd guests/monad && ./use-gen zkvm-r4-jd-blockhash-2026-08-23b140187
```

`use-gen` re-checks the witness format across the set before switching; nothing here is trusted
on the strength of this file.
