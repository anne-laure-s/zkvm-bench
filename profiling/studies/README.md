# studies — one-shot methodology checks

Each of these answered **one** question, once. What lives here is the method that produced the
answer, not the answer itself. They are kept so a number can
be defended later, not because anyone is expected to run them again — which is exactly why they were
separated from the instruments one directory up, where they looked like tools you might reach for.

They are a pair: `dwarf-tax.py` is only meaningful if `elf-equiv.py` passed first.

## `elf-equiv.py` — is the debug build the same program as the release build?

A source-location taxonomy needs DWARF, and no shipped guest ELF carries any. Adding `-g` is
*supposed* to be orthogonal to codegen — but "supposed to" is not a measurement: a different toolchain
version, a stale lockfile or an LTO decision can change the code while nobody looks, and then every
attribution drawn from the debug build describes a program that was never benchmarked.

So it runs both ELFs on the same inputs and requires the executed **step count to match exactly**.

## `dwarf-tax.py` — attribute cost by source location instead of by symbol name

The taxonomy in `hotspots.py` matches regexes against demangled symbol names, and that cannot see
inlined code: work with no symbol is charged to whatever function absorbed it. The two guests inline
very differently — C++ calls an outlined `__bswapdi2` where Rust inlines `swap_bytes`; the reth guest
inlines its keccak wrapper where Monad calls one — so the two families carrying the entire measured
gap are precisely the two a name-based taxonomy cannot compare. Renaming families does not create the
missing information.

Requires a debug build that `elf-equiv.py` has certified equivalent. Run it the other way round and
the output is attribution for a program you did not measure.

## Related, but not here

`inline-robust.py` asks a neighbouring question — how much of a family's ratio is real and how much is
inlining — but it stayed one level up on purpose: it *writes* `results/inline-verdict.json`, which
`compare.py` and `levers.py` read as an optional column. It is a feeder in the live pipeline, not a
one-shot.
