# monad-variants — Monad guest builds that exist only to be measured

**These are not guests.** A guest has a row in [`cli/guests.registry`](../../cli/guests.registry), an
`inputs/` directory and a `.commit` pin, and is provable through `cli/prove-farm` — see
[`../README.md`](../README.md). Everything here is an ELF and nothing else: a build of the Monad guest
kept around so `profiling/compare.py` can put it on an axis against another build. No registry row, no
inputs, never proved, never shipped.

All of them read the **`offsettriedb-rework-2026-08`** witness generation
(`guests/monad/fixtures` → see its `PROVENANCE.md`). Feeding them the pre-rework set is the silent
failure documented there: same format word, same magic, undetectable by `witness-fmt`.

## The variants

### the baseline — no longer here

`origin/sam/zkvm-zisk-sp1` rebased, built 2026-08-08: what the optimisation work is measured
*against*. It used to sit here as `sam/monad-sam-{zisk,sp1}.elf`, a byte-for-byte copy of the rework
generation's canonical pair, with a sha table as the only guard that the two stayed in step.

**Removed 2026-08-10.** The `cur-*` axes now point straight at
`guests/monad/gen/offsettriedb-rework-2026-08/elf/`, where that pair already lives and where its
sha256 is already recorded (`PROVENANCE.md`). Nothing was lost to the cache: builds are addressed by
content (`profiling/cache.py`), so the copy and the original were always one identity — the axis
names `monad-sam-zisk` / `monad-sam-sp1` are unchanged and still resolve.

Keep this directory for builds **no generation owns**: the ablations below, and branch builds measured
against a set they did not generate.

### `r3/` — the optimised line
`al/zkvm-r3` = the baseline plus the measured commits **and** the soundness binding (three public
values + body roots), so it proves a strictly stronger statement than `sam`. Built 2026-08-09.

| elf | sha256 (32) |
|---|---|
| `monad-r3-zisk.elf` | `9b1aa3ab6e838dea4290bec9c14d5f69` |
| `monad-r3-sp1.elf` | `98a73869079e3b2bf74887dd2fdd12f3` |

### `ab/` — ablation builds
One ELF per lever, each the optimised guest with **one** thing removed, so an axis against `r3/`
prices that lever alone. The `ab-*` set (2026-08-09 00:20) ablates EVM opcodes; the `ab2-*` set
(03:05) ablates the guest-side levers.

| elf | sha256 (32) | | elf | sha256 (32) |
|---|---|---|---|---|
| `ab-no-addmod` | `257f8c4643da2b4f2fd884235f01bdfe` | | `ab2-no-fmix` | `f1c77665f1dacda2248467e99c2db8e6` |
| `ab-no-mulmod` | `d8a3188fdd58180ba951dafb32458f41` | | `ab2-no-hashinline` | `51c570082513a6c283f543c45faf0207` |
| `ab-no-opstar` | `3038abe88f3f2f03ac1c3bc4257e220b` | | `ab2-no-kec2` | `160c0f170fbf46ce49982bff76a2487b` |
| `ab2-no-arena` | `598ce79007031be66f80360b44d48a89` | | `ab2-no-nodeid` | `9bd91d432a3d80aa53b945075fa1c74c` |
| `ab2-no-div` | `29e37e953f0a698c2c71c9eed2b3ccc5` | | `ab2-no-scanidx` | `a0bad48878754946c95bc95cd82a59d4` |
| `ab2-no-fasthead` | `1acac9e8fbba021bb9e2b440b71edced` | | `ab2-no-tokens` | `69362f2894a4f24b881be7d2d18f3aae` |
| `ab2-no-flat` | `f802b2ba101b1e0125a56c363edfab1a` | | | |

> There is deliberately **no bswap ablation**: that lever was priced by a separate 22-block
> measurement rather than by a build, and `levers.py` and `compare.py` read that result where it
> lands. Do not declare an axis for it — an axis that cannot resolve its ELF reads as coverage
> while measuring nothing.

### `levers/` — record only, no binaries

`al/zkvm-levers` (11 commits on `ed16787ae`), built 2026-08-07. It predates the reader rework, so it
reads the **pre-rework** witness format, not the generation every other variant uses, and there is no
`levers-*` axis to run it on. Only the record is kept.

Its measurements are live and independent of the binaries: a **frozen measurement set**, produced
while the branch still existed, feeds `profiling/optimized.py`, whose output `compare-optimized.py`
renders. Nothing in that chain re-measures, and nothing can: the ELFs are gone.

| elf (not kept) | sha256 (32) |
|---|---|
| `monad-levers-zisk.elf` | `d314c8940ccf49efa8b90898e16959bc` |
| `monad-levers-sp1.elf` | `c5773ffd41b819b3fb0f7197bba1286c` |

Build details (`.text` sizes, the missing submodule patches) are in [`levers/README.md`](levers/README.md).

`profiling/levers.py` reads this build's profiles from the cache by name. It renders; it cannot be
re-measured. Treat it and `compare-optimized.py` as reports over frozen data.

## Why the ELFs are git-ignored and this file is not

They are built artifacts (on the devcore box), like every other guest ELF in this repo — only the Monad
*pre-supplied* pair under `guests/monad/` is versioned. What has to survive a clone is not the 68 MB
of binaries, it is **which build produced which number**, and that is what the shas above are for.
Same contract as a generation's `PROVENANCE.md`.
