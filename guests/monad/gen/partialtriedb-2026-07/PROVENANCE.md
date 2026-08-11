# generation `partialtriedb-2026-07` — the baseline

Every figure in `profiling/results/compare.html` and `levers.html` as published on 2026-08-04 was
measured against this generation. Frozen copies of those reports:
`profiling/results/baseline-partialtriedb-2026-08-04/`.

## Guest
Execution path `PartialTrieDb::from_witness(encoded_nodes)` — pre-OffsetTrieDb.
The ELFs were built by **sbal**, not on this Mac: their build paths read `/home/sbal/.cargo/…`,
and this machine has no `riscv_gcc` toolchain.

| elf | sha256 |
|---|---|
| `monad-zkvm-guest-zisk.elf` | `843e46b154697087e09e0b2321118a7c92c3a9ef7edb5b6a43f2f384233487e3` |
| `monad-zkvm-guest-sp1.elf` | `ed5d058d3d00338f8d9d82b1091d3bdcb95aa2d30d5d5ac92325ee2449fbc3c4` |

## Witnesses
Wire format: field **[1]** is an RLP list of MPT node preimages — the *old* format. Measured first
four bytes of that field, on the fijcrst / median / last block of the set: `f90211a0`, an RLP list
header. The offset format reads `4d5a5701` (`MZW\x01`) instead, so the two are told apart without
trusting a filename.

- pairs: **503** × (`.witness` + `.post_state_root`)
- range: **25551992 … 25552494**, contiguous: **yes**
- median size 7.2 MB, total 3.67 GB
- produced on the devcore box from `sam/osaka_witness_gen`, replayed from snapshot 25551990

## Results measured against it
`monad-sp1` **1.221×** the cycles of `rsp` (1.263× engine-to-engine, excluding the 80 blocks where
`rsp` runs BN254 in software) · `monad-zisk` **1.462×** the steps of `zisk-reth`.
