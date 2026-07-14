# Synthesis — SP1 / RSP benchmark on GPU cluster (RTX 5090)

Exhaustive analysis of SP1 runs (RSP, Ethereum mainnet blocks) on the 8× RTX 5090 box, based on
the cluster logs (`diag/diag-out/`), the tuning sweep (`sweep-*.csv`), and the per-block execution
reports (`rsp/report.csv`, `../../../guests/rsp/inputs/*.exec-report.json`).

> **Methodology note — re-execution phase removed.** The raw wall-clock of the sweep included a
> **standalone execution** pass (`--mode execute`, the one that produces the `*.exec-report.json`,
> **3.70 s** for block 20000000) that **duplicates** the work `CoreExecute` already redoes inside
> the prove. It had been kept in the measurement by mistake. All figures below have been
> **stripped** of it (the standalone pass is pure CPU, so it also inflated the GPU idle).

---

## TL;DR

1. **The bottleneck is not GPU compute.** No GPU knob (shard size, weight, splice) moves the
   wall-clock; the dominant cost is the **CPU** chain (execution + trace generation + recursion
   queue) and the startup latency before the GPUs receive work.
2. **`CoreExecute` ≈ 23 s per proof** while the total wall-clock (`Controller`) ≈ **31 s**: the
   execution/trace-gen phase accounts for **~3/4** of the proof, largely on CPU.
3. **Actual EVM execution is only ~31 % of the cycles.** ~56 % go into input deserialization
   (23 %) + witness DB init (21 %) + state root computation (11 %) — this is the "stateless RSP"
   overhead, not the EVM. **This is the primary lever for cycle reduction.**
4. **GPU idle ~37 %** = startup (~4 s before the 1st shard) + load imbalance (shards 5–18 s,
   spread 3.5×) + recursion queue at the end of the proof.

---

## 1. Environment & methodology

| | |
|---|---|
| GPU | **8× NVIDIA RTX 5090** (32 GB; ~20.7 GB used/GPU during proof), driver 595.71.05, CUDA 13.2 |
| Host | Linux 5.15, glibc 2.39, AMD EPYC CPU (~96 cores) |
| Stack | **SP1 6.2.4**, `sp1_circuit_version v6.1.0`, `sp1-cluster` (coordinator + cpu-node + 8 gpu-node) |
| Mode | **compressed** (aggregated STARK; no `Wrap`/`Plonk`/`Groth16` task observed) |
| Guest | RSP `rsp.elf` (reth/revm stateless), witness `EthClientExecutorInput` |
| Diag window | ~2 complete proofs captured (2× `Controller`, 2× `CoreExecute`) → **representative** durations, not exhaustive totals |

---

## 2. Where the wall-clock goes — pipeline & task durations

Observed pipeline: `Controller` orchestrates → `CoreExecute` (CPU, execution + record generation)
→ **streaming** of shards to the GPUs → `ProveShard` (GPU) → `RecursionReduce` (recursive reduction
of the shard proofs).

Measured durations (cluster logs, excluding standalone re-execution):

| Task | n | min | max | **mean** | role |
|---|---:|---:|---:|---:|---|
| **Controller** | 2 | 29.3 s | 33.0 s | **31.2 s** | ≈ wall-clock of a proof |
| **CoreExecute** | 2 | 22.84 s | 22.92 s | **22.9 s** | execution + trace-gen (CPU) |
| **ProveShard** | 45 | 5.15 s | 18.21 s | **10.7 s** | proof of one shard (GPU) |
| **RecursionReduce** | 11 | 1.20 s | 4.80 s | **2.8 s** | recursive reduction (GPU) |

Throughput reference: the *fast* execution (cycle-count only) does **3.70 s for 209 M cycles ≈ 56 Mcycle/s**
(`1-20000000.exec-report.json`). The `CoreExecute` task (22.9 s) is ~6× slower because it additionally
generates the **records/traces** needed by the prover — that is legitimate, but it is massively CPU and
it governs the rate at which shards arrive on the GPUs.

### Timeline of a proof (req…271aa, `Controller` = 33.0 s)
```
t+0.0s   Controller starts
t+1.1s   CoreExecute starts (CPU)
t+4.3s   1st ProveShard received by a GPU   ← ~4 s of GPU idle at startup
t+4→30s  shards streamed & proved (8 GPU), recursion in parallel
t+24s    CoreExecute done (22.9 s)
t+30s    last ProveShard / RecursionReduce
t+33s    Controller done
```

### GPU idle diagnosis (~37 %)
- **Startup**: ~4 s before the 1st shard (the time for `CoreExecute` to produce the first records) → ~13 % on its own.
- **Load imbalance**: `ProveShard` from 5.2 to 18.2 s (**spread 3.5×**) → the fast GPUs wait for the slow ones.
- **Recursion queue**: the final `RecursionReduce` depend on all shards → underutilization at the end of the proof.
- **CPU execution throughput**: until `CoreExecute` has streamed a shard, no GPU can prove it → CPU execution caps the GPU feed.

---

## 3. Tuning sweep — what moves (almost) nothing

| Config | run 1 (cold) | run 2 (warm) |
|---|---:|---:|
| baseline (cluster default) | 45.8 s | 39.9 s |
| shard=21 w=30 | 48.3 s | 39.9 s |
| shard=22 w=30 | 46.8 s | 40.0 s |
| shard=23 w=30 | 46.2 s | 40.2 s |
| splice=32 | 50.5 s | 42.0 s |
| splice=48 | 49.0 s | 41.9 s |
| splice=64 | 50.1 s | 42.9 s |
| numa=interleave / numa=bind | **CRASH** | **CRASH** |

Reading: steady state **~40 s** regardless of shard or splice size; splice **degrades** it
slightly; NUMA pinning crashes. **Confirmation that the lever is not on the GPU parameter side** — the
CPU chain (execution/trace-gen + recursion) or the machine's CPU:GPU ratio must be attacked.
The cold→warm gap (~6–8 s) is warmup (kernels/caches) and should not be counted.

---

## 4. Cycle composition — the real opportunity

Mean over 23 blocks (`report.csv`), as % of total cycles:

| Phase | mean | range | nature |
|---|---:|---:|---|
| **input deserialization** | **23.4 %** | 7.4–28.6 % | RSP overhead (decode witness) |
| **witness DB init** | **21.2 %** | 6.6–25.1 % | RSP overhead (trie/MPT construction) |
| recover senders | 2.9 % | 0.9–5.2 % | ecrecover of the signatures |
| **block execution (EVM)** | **31.1 %** | 17.5–48.3 % | actual EVM execution |
| block validation | ~1 % | (49 % on the Merge block 15537394) | header/post-state validation |
| **state root** | **11.4 %** | 5.0–15.4 % | RSP overhead (recompute MPT root) |

> **~56 % of the cycles are "stateless" overhead (deserialization + witness DB + state root), not
> EVM.** This is exactly what shrinks with (a) a better witness format / preimage caching, (b) more
> complete precompiles (keccak/MPT), or (c) a zkVM whose ISA absorbs Merkle hashing better — hence
> the value of comparing OpenVM/ZisK *at cycle count*, not just at wall-clock.

---

## 5. Per-block statistics (23 blocks, sorted by cycles)

`gas_M` = EVM gas (M) · `cyc_M` = RISC-V cycles (M) · `c/gas` = cycles per unit of gas ·
`pgas_M` = prover gas (M) · phases in % of cycles · `keccak` = `keccak_permute` · `secp+` = `secp256k1_add`.

| block | tx | gas_M | cyc_M | c/gas | pgas_M | desER | initWDB | recSnd | blkEXE | stRoot | keccak | secp+ |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 13000000 | 17 | 1.3 | 26.8 | 20.9 | 33 | 24 | 22 | 3 | 33 | 11 | 5 357 | 8 184 |
| 20500000 | 33 | 1.8 | 36.0 | 20.4 | 46 | 29 | 25 | 4 | 18 | 15 | 7 494 | 8 897 |
| 19426587 | 37 | 2.6 | 45.4 | 17.2 | 57 | 24 | 21 | 4 | 30 | 13 | 8 858 | 20 909 |
| 18884864 | 30 | 4.3 | 91.1 | 21.4 | 110 | 22 | 21 | 2 | 38 | 11 | 17 738 | 8 182 |
| 5000000 | 109 | 8.0 | 116.3 | 14.6 | 141 | 23 | 20 | 4 | 31 | 14 | 21 956 | 30 643 |
| 17000000 | 102 | 9.2 | 152.3 | 16.6 | 190 | 24 | 22 | 3 | 29 | 13 | 31 959 | 30 475 |
| 19000000 | 133 | 9.6 | 154.4 | 16.1 | 194 | 25 | 23 | 4 | 28 | 13 | 32 534 | 35 440 |
| 10000000 | 103 | 10.0 | 160.5 | 16.1 | 195 | 27 | 24 | 3 | 29 | 10 | 30 121 | 27 199 |
| 22200000 | 110 | 9.0 | 165.1 | 18.4 | 208 | 25 | 23 | 3 | 28 | 12 | 35 004 | 41 091 |
| 15537394 ⚠ | 80 | 30.0 | 169.4 | 5.7 | 259 | 7 | 7 | 2 | 27 | 5 | 72 458 | 20 350 |
| 22000000 | 97 | 19.3 | 185.4 | 9.6 | 238 | 26 | 24 | 2 | 27 | 12 | 42 178 | 27 458 |
| 21750000 | 102 | 30.0 | 192.4 | 6.4 | 245 | 24 | 21 | 4 | 32 | 10 | 41 612 | 31 808 |
| 18000000 | 94 | 16.2 | 193.1 | 11.9 | 240 | 24 | 22 | 3 | 34 | 9 | 39 970 | 26 891 |
| **20000000** | 134 | 11.1 | **209.1** | 18.9 | 261 | 26 | 24 | 3 | 28 | 12 | 43 073 | 41 259 |
| 21000000 | 181 | 14.0 | 239.8 | 17.2 | 302 | 25 | 23 | 3 | 28 | 12 | 51 108 | 50 105 |
| 21500000 | 172 | 13.8 | 266.5 | 19.3 | 336 | 26 | 24 | 3 | 28 | 11 | 57 081 | 52 007 |
| 20250000 | 173 | 17.8 | 354.4 | 19.9 | 440 | 25 | 22 | 2 | 32 | 11 | 70 085 | 49 927 |
| 22300000 | 82 | 35.9 | 399.7 | 11.1 | 489 | 24 | 21 | 1 | 31 | 15 | 73 898 | 21 529 |
| 21250000 | 475 | 30.0 | 496.7 | 16.6 | 630 | 24 | 22 | 4 | 28 | 13 | 101 534 | 126 505 |
| 19500000 | 364 | 30.0 | 510.0 | 17.0 | 650 | 24 | 22 | 3 | 30 | 12 | 107 009 | 103 940 |
| 25367820 | 683 | 32.3 | 559.3 | 17.3 | 693 | 23 | 20 | 5 | 29 | 14 | 102 224 | 179 130 |
| 20750000 | 286 | 28.9 | 632.5 | 21.9 | 734 | 18 | 17 | 2 | 48 | 8 | 97 761 | 77 922 |
| 25367437 | 583 | 31.2 | 763.4 | 24.5 | 876 | 17 | 16 | 3 | 48 | 9 | 106 699 | 164 973 |

**Aggregates** (sum of the 23 rows above): total gas ≈ 396 M · total cycles ≈ 6.12 B · aggregate **15.4 cycles/gas**.

Observations:
- **cyc/gas ranges from 5.7 to 24.5 (spread 4.3×)**: proof cost is NOT proportional to gas.
  The "heavy-gas but light-cycle" blocks (15537394, 21750000) are calldata/precompile-heavy;
  those with high cyc/gas (25367437, 20750000) are compute-heavy (48 % EVM execution).
- **⚠ block 15537394 = outlier**: 49 % of cycles in "block validation" (82.9 M cyc, i.e. **36.7×** a
  comparable block) vs ~1 % everywhere else. The ~80 M extra cycles **are not hashing**
  (normal syscall profile) → ordinary computation in validation, **cause unresolved**. It is also
  the Merge block (1st PoS block), but the causal link Merge→validation remains a hypothesis.
  To be excluded from tuning means whatever the reason.
- **keccak_permute scales with block size**; **secp256k1 (ecrecover) scales with the number of tx**
  (683 tx → 179 k secp_add). These two precompiles are the top candidates for ISA optimization.
- **prover_gas ≈ 1.2–1.4× the cycles** fairly linearly → consistent cost metric.

---

## 6. Conclusions — what would actually move the needle

1. **Reduce the overhead cycles (~56 %)** above all: deserialization + witness DB + state root
   dominate the EVM. Lever #1, independent of the GPU. → motivates the **cycle-count** comparison
   with OpenVM/ZisK (different ISA + keccak/MPT precompiles).
2. **Break the `CoreExecute` CPU seriality (~23 s)**: it is what caps the GPU feed and creates the
   startup idle. Either more CPU cores per GPU (the rented machine's ratio), or a zkVM that does
   trace-gen/recursion *on GPU* (see the OpenVM 2.0 argument "non-execution work on GPU").
3. **Smooth out the shard imbalance** (spread 3.5×) and **the recursion queue**: second-order gains,
   but real on the idle.
4. **GPU tuning (shard/splice) is a dead end here** — demonstrated by the sweep.

In other words: on this machine, SP1-cluster is **CPU-bound and overhead-bound**, not GPU-bound.
The ~40 s/8 GPU (≈20 s/16 GPU) reflect the CPU chain + the composition of the RSP cycles, not a
GPU compute ceiling.
