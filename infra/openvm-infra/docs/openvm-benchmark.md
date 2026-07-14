# OpenVM benchmark results

Cross-zkVM comparison of OpenVM against SP1 (and ZisK) on the same mainnet blocks. Blocks are the
union of what SP1 was benchmarked on (`guests/rsp/inputs/`), which already includes every
ZisK-tested block.

## Execution (validated on the Mac, CPU, no GPU) — 2026-07-08

`./run execute BLOCK=<n>` runs the guest in the OpenVM executor from the cached witness (offline)
and validates the block: the VM-computed block hash is compared to a native-reth execution.

- **34 / 34 blocks validate** (VM block hash == host block hash) — including the 4 **pre-Merge**
  blocks (5000000, 10000000, 13000000, 15537394). So OpenVM's guest handles the full range; the
  earlier "pre-Merge may not be provable" worry is resolved.
- This confirms the whole pipeline off-box: binary, embedded guest ELF, `openvm.toml` extensions,
  the 34 minted witnesses, and block-validation correctness. Only the *proving* remains (GPU box).

### OpenVM executed instructions vs SP1 cycles

| block | OpenVM instr | OpenVM exec (s) | SP1 cycles | SP1 / OpenVM |
|---:|---:|---:|---:|---:|
| 5000000 | 70,429,573 | 0.818 | 116,316,921 | 1.65× |
| 10000000 | 81,989,017 | 0.852 | 160,509,449 | 1.96× |
| 13000000 | 14,814,459 | 0.587 | 26,815,265 | 1.81× |
| 15537394 | 78,506,797 | 0.806 | 169,409,433 | 2.16× |
| 17000000 | 80,355,819 | 0.889 | 152,293,277 | 1.90× |
| 18000000 | 117,717,009 | 1.039 | 193,100,893 | 1.64× |
| 18884864 | 56,022,283 | 0.734 | 91,097,550 | 1.63× |
| 19000000 | 80,778,900 | 0.896 | 154,369,891 | 1.91× |
| 19426587 | 24,846,622 | 0.664 | 45,355,130 | 1.83× |
| 19500000 | 276,228,447 | 1.741 | 509,998,265 | 1.85× |
| 20000000 | 106,110,812 | 0.996 | 209,134,338 | 1.97× |
| 20250000 | 195,951,400 | 1.323 | 354,374,910 | 1.81× |
| 20500000 | 15,891,783 | 0.605 | 36,009,897 | 2.27× |
| 20750000 | 329,402,464 | 1.814 | 632,481,995 | 1.92× |
| 21000000 | 124,300,018 | 1.087 | 239,849,752 | 1.93× |
| 21250000 | 263,922,185 | 1.724 | 496,747,806 | 1.88× |
| 21500000 | 137,183,592 | 1.154 | 266,519,176 | 1.94× |
| 21750000 | 114,451,251 | 1.089 | 192,374,053 | 1.68× |
| 22000000 | 94,192,470 | 0.948 | 185,380,008 | 1.97× |
| 22200000 | 85,767,990 | 0.933 | 165,094,127 | 1.92× |
| 22300000 | 226,050,019 | 1.291 | 399,716,042 | 1.77× |
| 24626900 | 163,332,403 | 1.236 | 305,221,681 | 1.87× |
| 24628590 | 510,376,343 | 2.602 | 862,543,244 | 1.69× |
| 24628595 | 554,545,214 | 2.871 | 1,009,408,383 | 1.82× |
| 24628607 | 92,297,102 | 0.888 | 160,809,813 | 1.74× |
| 24628608 | 130,125,595 | 1.061 | 224,523,047 | 1.73× |
| 24628611 | 588,684,074 | 4.232 | 1,003,751,719 | 1.71× |
| 24647140 | 85,389,668 | 0.901 | 162,195,976 | 1.90× |
| 24697070 | 191,079,851 | 1.332 | 392,469,402 | 2.05× |
| 24697073 | 177,850,515 | 2.579 | 349,264,932 | 1.96× |
| 25229951 | 521,864,701 | 2.509 | 894,253,314 | 1.71× |
| 25229957 | 92,538,157 | 0.934 | 180,413,792 | 1.95× |
| 25367437 | 397,746,394 | 2.182 | 763,363,102 | 1.92× |
| 25367820 | 319,365,254 | 3.234 | 559,296,782 | 1.75× |
| **total** | **6,400,108,181** | **48.55** | **11,664,463,365** | **1.82×** |

**Reading this correctly:** OpenVM executes ~1.8× fewer work-units per block than SP1. This is a
real structural advantage (less to prove), driven largely by OpenVM's native extensions
(keccak/sha256/bigint/ecc/pairing in `openvm.toml`) folding work SP1 spends many cycles on.

⚠️ **But "instructions" (OpenVM) and "cycles" (SP1) are not identical units, and fewer work-units
≠ proportionally faster proving** — the two use different proof systems (SP1 STARK vs OpenVM
SWIRL), so per-unit proving cost differs. This table predicts *relative execution work*, not proving
latency. Proving latency is what the GPU run below measures.

## Proving (GPU box) — partial run: 8× RTX 5090, block 20000000 — 2026-07-09

Multi-GPU via the multi-process **path ①** (`cluster/submit.sh`): 8 worker processes, one GPU
each (`CUDA_VISIBLE_DEVICES` pinned), each proving its shard of the block's continuation segments
(`seg_idx % 8`), then a single `aggregate` step. This sidesteps the in-process multi-device VPMM
crash (`cudaErrorInvalidResourceHandle`) — see `openvm-multigpu.md`.

Run record: `results/mg-1-20000000-20260709-113056Z/`.

### App-proof (segment) phase — measured ✅

Block 20000000 splits into **28 segments**, sharded across the 8 GPUs (4 on GPUs 0–3, 3 on GPUs 4–7).
Each worker's end-to-end wall time (root `block` span = ~1 s CPU metered execution + trace-gen +
STARK proof of its shard):

| worker (GPU) | segments | `block` span (s) |
|---:|---:|---:|
| 0 | 4 | 18.2 |
| 1 | 4 | 20.8 |
| 2 | 4 | 15.4 |
| 3 | 4 | **21.8** |
| 4 | 3 | 14.4 |
| 5 | 3 | 19.0 |
| 6 | 3 | 18.5 |
| 7 | 3 | 18.1 |

- **App-proof phase latency ≈ 22 s** — the workers run concurrently, so the phase ends when the
  slowest (worker 3, 21.8 s) finishes (span excludes process/CUDA startup). All 8 GPUs exercised:
  **peak util 100 %, peak VRAM 23,328 MiB (~23.3 GB) / GPU. No VPMM crash** → path ① validated on
  real 8-GPU hardware.
- **Poorly balanced (14.4 s – 21.8 s).** Static `seg_idx % 8` sharding + unequal segment cost leaves
  fast GPUs idle (GPU 4 done at 14.4 s) while the slowest runs to 21.8 s — exactly what a dynamic
  scheduler would recover. Note the span isn't a pure function of segment count (GPU 2: 4 seg /
  15.4 s vs GPU 5: 3 seg / 19.0 s) — segments have unequal cost.
- Per-worker breakdown (GPU 0): `execute_metered` 1.03 s (CPU, **duplicated in every worker**) +
  `stark_prove_excluding_trace` ≈ 4.3 s over its 4 segments; the balance (~13 s) is trace generation.

### Aggregation — interrupted ⚠️ (no end-to-end latency)

The single `aggregate` step ran **on one GPU** and was **stopped before completion** (`aggregate.log`
ends at "aggregating 28 segment proofs"; no `proof.json`, no `timing.txt`). So **no completed
single-block latency** was captured on the box — the run was halted precisely because the aggregation
is not distributed and dominated the tail (see `openvm-multigpu.md`).

### Correctness — validated off-box ✅

Path ① end-to-end (workers + aggregation) was validated on the Mac (CPU, block 13000000,
`results/mg-1-13000000-20260708-222759Z/`, `workers_secs=36` / `aggregate_secs=2864` / 4 seg):
it produced a valid `proof.json` whose block hash matches native reth. So the multi-process
decomposition is **correct**; only the box *latency* of the aggregation phase is unmeasured.

### Summary vs SP1 / ZisK

| block | OpenVM app-proof, 8×5090 (s) | OpenVM aggregation | SP1 (s) | ZisK (s) |
|---:|---:|---:|---:|---:|
| 20000000 | ≈22 (28 seg, path ①) | interrupted (1 GPU) | ~40 (8×5090) | _no witness for this block_ |

Still to capture for a clean cross-zkVM row: a **completed** end-to-end latency (let the aggregation
finish once) and **cycles/s per GPU**. The first box attempt
(`mg-1-20000000-20260709-104128Z`) panicked on a patch marker-collision bug (`unreachable!()` at
`bin/reth-benchmark/src/lib.rs:432`), fixed before this run.
