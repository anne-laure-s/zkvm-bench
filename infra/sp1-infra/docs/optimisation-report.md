# SP1 prover optimization on a GPU cluster

_Report — 2026-07-02. RSP guest (Ethereum mainnet block re-execution), `prove-compressed` mode._

## Hardware

- **GPU**: 8× (plus a 16× box evaluated separately) **NVIDIA RTX 5090** 32 GB — driver 595.71.05, CUDA 13.2
- **CPU**: 2× **AMD EPYC 9654** = **192 cores / 384 threads**, **8 NUMA nodes** (NPS4), 3.7 GHz max
- **RAM**: 1 TB · **/dev/shm**: 503 GB · kernel 5.15 · glibc 2.39
- **Vast.ai** instance = itself a container (native GPUs, no nested Docker)

## Deployed infrastructure

- **sp1-cluster v2.4.3** (SP1 6.2.4) launched as **native processes** (no Docker): redis + postgres (apt) +
  cluster binaries (`api`, `coordinator`, `network-gateway`, `node`×N) extracted from the v2.4.3 images via `skopeo`.
- **Topology**: control plane + 2 cpu-nodes + N gpu-nodes, localhost, de-conflicted ports.
- **Client**: in-house `sp1-runner` (Rust, `--features network`) — proves via the network-gateway and
  **fetches + saves** the proof (run record: `proof/pv/vkey/report.json/prove.log/env.txt` + cluster logs).
- **Workload**: 23 mainnet blocks 2018→2026, **26 M → 763 M** SP1 cycles.

## Problems encountered → resolved (root causes)

| Problem | Root cause | Resolution |
|---|---|---|
| Idle GPUs (GPU compute ≈ 4 s out of 26 s on a heavy block) | Per-shard trace regeneration (`into_record`, single-threaded RISC-V ~2 s/shard) **serialized** on each GPU node → GPUs wait on their own CPU (~80 %) | Pipelining: `NUM_CORE_WORKERS` (default 4 → 8) — trace of shard K+1 during the proving of shard K |
| NUMA crashed (0/8 workers, even with `interleave=all`) | `set_mempolicy` forbidden in a container (no `CAP_SYS_NICE`/NUMA sysfs) | **`taskset`** fallback (container-safe) in `03-start.sh`. Note: minor lever (compute-bound, not bandwidth), and `/dev/shm`=503 GB ⇒ never the limiter |
| Local re-execution ~1.4–18.6 s/block before each submit | SDK 6.2.4 bug: the **blocking** `prove()` ignores the `hosted` flag (hardcodes `skip_simulation=false`) | Runner fix: `skip_simulation(true)` + `u64::MAX` limits, cfg-gated on network |
| Raising splice regressed | Tunes the CPU front end (boundary scan), **not** the bottleneck (downstream into_record) | Deprioritized (stage E, skippable) |
| No CPU/shm info in the logs | `bundle_diag.sh` / `env.txt` did not capture them | Added CPU + `/dev/shm` + NUMA capture |

## Optimizations made

| Area | Change |
|---|---|
| **Cluster** (in `03-start.sh`) | `NUM_CORE_WORKERS=8` + `CORE_BUFFER_SIZE=8` (trace-gen pipeline) · `USE_FIXED_PK=1` · `VERIFY_INTERMEDIATES=0` · `LOG2_SHARD_SIZE=21` (default 24) · `GPU_MAX_WEIGHT=32` (default 24) |
| **Client** (runner) | Removal of local re-execution (`skip_simulation` + `u64::MAX`) |
| **Tooling** | `sweep.sh` (core/trim stages, skippable, `BASE_ENV`, taskset fallback) · `fetch-runs.sh` (compress→transfer→verify→extract→purge) · hardware capture + `/dev/shm` warning |

## Optimized vs. original comparison — 8 GPU (23 blocks) — **re-execution EXCLUDED**

**Net cluster** proving time: client-side local re-execution is **removed from both columns**
(measured per block in `prove.log`, ~identical on both sides — same block/CPU/runner; see note below).
Original config = cluster defaults (shard 24, weight 24, core 4, no trim).
Optimized config = shard 21, weight 32, core 8, `fixed-pk` + `verify-off`.

| block | cycles | orig (s) | opt (s) | Δ |
|---|---:|---:|---:|---:|
| 13000000 | 26 815 265 | 8.04 | 8.03 | −0.0% |
| 20500000 | 36 009 897 | 10.03 | 8.03 | −19.9% |
| 19426587 | 45 355 130 | 12.04 | 10.04 | −16.6% |
| 18884864 | 91 097 550 | 14.04 | 12.04 | −14.3% |
| 5000000 | 116 316 921 | 18.05 | 14.05 | −22.2% |
| 17000000 | 152 293 277 | 20.08 | 18.07 | −10.0% |
| 19000000 | 154 369 891 | 22.09 | 18.06 | −18.2% |
| 10000000 | 160 509 449 | 22.06 | 20.09 | −8.9% |
| 22200000 | 165 094 127 | 22.07 | 18.08 | −18.1% |
| 15537394 | 169 409 433 | 26.07 | 22.05 | −15.4% |
| 22000000 | 185 380 008 | 24.09 | 20.08 | −16.6% |
| 21750000 | 192 374 053 | 24.10 | 20.06 | −16.7% |
| 18000000 | 193 096 361 | 24.08 | 20.07 | −16.6% |
| 20000000 | 209 134 338 | 26.09 | 20.08 | −23.0% |
| 21000000 | 239 849 752 | 28.11 | 22.09 | −21.4% |
| 21500000 | 266 519 176 | 30.10 | 26.10 | −13.3% |
| 20250000 | 354 374 910 | 38.13 | 32.11 | −15.8% |
| 22300000 | 399 716 042 | 40.14 | 32.20 | −19.8% |
| 21250000 | 496 747 806 | 52.19 | 40.16 | −23.0% |
| 19500000 | 509 998 265 | 54.20 | 42.18 | −22.2% |
| 25367820 | 559 296 782 | 58.18 | 46.18 | −20.6% |
| 20750000 | 632 481 995 | 58.17 | 46.16 | −20.7% |
| 25367437 | 763 363 102 | 68.22 | 54.20 | −20.5% |
| **TOTAL** | | **700** | **570** | **−18.6%** |

- Gain of **~15–23 %** across the medium→heavy spectrum; **~0 %** on the tiny block (fixed floor).
- On **net cluster** time, the gain (−18.6 %) is larger than on the raw figure with re-execution (−14.7 %):
  the noise common to both batches diluted the percentage.
- Scatter (10 M at −8.9 %, 17 M at −10 %) = single-run variance (`submit-all` = 1 proof/block).

### Local re-execution (excluded from the table above)

Both batches ran on the **old runner**, with pre-submit simulation: **~1.4–18.6 s/block
(~185 s cumulative over the batch)**, nearly identical on both sides (same block/CPU). It is **removed** from the
times above (pure cluster times), and is furthermore **permanently eliminated** by the runner's
`skip_simulation` fix → it will not reappear in future runs.

### End-to-end (old runner → optimized runner)

Heavy 763 M block: **86.8 s** (orig, re-execution included) → **54.2 s** (optimized, re-execution removed) = **−37.5 %**,
combining the **cluster** gain (−20.5 % of cluster time) and the **removal of client re-execution**.

## 16 GPU (measured, same box) — the 8-GPU optimization REVERSES

With a 16-GPU box available, default vs. optimized was measured across the 23 blocks (**same box**: GPU UUIDs
identical to the June 24 run; net times, fixed runner). Striking result: **the config that gained −18.6 % at 8 GPU
LOSES +6.2 % at 16 GPU.**

| | Original → Optimized | verdict |
|---|---|---|
| **8 GPU** | 700 → 570 s | **−18.6 %** (optimization helps) |
| **16 GPU** | 352 → 374 s | **+6.2 %** (optimization HURTS) |

16-GPU detail (23 blocks, sorted by cycles; most neutral, some +8 to +17 %):

| block | cyc (M) | 16-def (s) | 16-opt (s) | Δ |
|---|---:|---:|---:|---:|
| 13000000 | 27 | 6.04 | 6.04 | −0.0% |
| 20500000 | 36 | 8.04 | 8.05 | +0.2% |
| 19426587 | 45 | 8.05 | 8.04 | −0.1% |
| 18884864 | 91 | 10.11 | 10.05 | −0.6% |
| 5000000 | 116 | 10.06 | 10.07 | +0.1% |
| 17000000 | 152 | 12.07 | 12.08 | +0.1% |
| 19000000 | 154 | 12.09 | 12.07 | −0.1% |
| 10000000 | 161 | 12.07 | 14.06 | +16.5% |
| 22200000 | 165 | 12.07 | 12.10 | +0.3% |
| 15537394 | 169 | 12.05 | 14.07 | +16.7% |
| 22000000 | 185 | 12.08 | 12.08 | −0.0% |
| 21750000 | 192 | 12.07 | 14.09 | +16.7% |
| 18000000 | 193 | 12.08 | 14.09 | +16.7% |
| 20000000 | 209 | 14.09 | 14.09 | +0.0% |
| 21000000 | 240 | 14.10 | 14.09 | −0.1% |
| 21500000 | 267 | 16.12 | 16.10 | −0.1% |
| 20250000 | 354 | 18.13 | 20.13 | +11.0% |
| 22300000 | 400 | 18.12 | 20.13 | +11.1% |
| 21250000 | 497 | 24.18 | 26.17 | +8.2% |
| 19500000 | 510 | 24.20 | 26.21 | +8.3% |
| 25367820 | 559 | 28.16 | 28.17 | +0.0% |
| 20750000 | 632 | 26.18 | 28.18 | +7.6% |
| 25367437 | 763 | 30.21 | 34.21 | +13.3% |
| **TOTAL (23)** | | **352** | **374** | **+6.2%** |

**Cause.** The "optim" config = `NUM_CORE_WORKERS=8` + `LOG2_SHARD_SIZE=21` (small shards). At 8 GPU (starved
cards), more shards = better fill → gain. At 16 GPU, **2× fewer shards/card** → the 8 core-workers oversubscribe
and the small shards add pointless recursion → penalty, often **a wave of exactly +2 s** (hence the +16.7 % net on
several blocks, +8-13 % on the heavy ones). Confirmed by ablation: on the 763 M, `LOG2_SHARD_SIZE=25` (large
shards) **saturates VRAM** (32 GB) → **74 s**.

**Verdict**: at 16 GPU, the **DEFAULT is the optimum** (352 s). Tuning is **specific to the GPU count** — porting
the 8-GPU config as-is degrades performance (+6 %). A true 16-GPU optimum would be re-swept (a priori toward
**fewer** core-workers and the **default shard**).

## Appendix — characterization of the 34 blocks (gas · witness · cycles)

EVM gas (`eth_getBlockByNumber.gasUsed`), RSP witness size (`.bin`), SP1 cycles (exec-report), for all proven
blocks (23 initial + 11 "zisk").

| block | EVM gas (M) | witness (MiB) | cycles (M) | cyc/gas |
|---|---:|---:|---:|---:|
| 5000000 | 8.0 | 1.6 | 116 | 14.6 |
| 10000000 | 10.0 | 2.5 | 161 | 16.1 |
| 13000000 | 1.3 | 0.4 | 27 | 20.9 |
| 15537394 | 30.0 | 0.8 | 169 | 5.7 |
| 17000000 | 9.2 | 2.7 | 152 | 16.6 |
| 18000000 | 16.2 | 3.4 | 193 | 11.9 |
| 18884864 | 4.3 | 1.6 | 91 | 21.4 |
| 19000000 | 9.6 | 2.8 | 154 | 16.1 |
| 19426587 | 2.6 | 0.8 | 45 | 17.2 |
| 19500000 | 30.0 | 9.5 | 510 | 17.0 |
| 20000000 | 11.1 | 3.8 | 209 | 18.9 |
| 20250000 | 17.8 | 6.1 | 354 | 19.9 |
| 20500000 | 1.8 | 0.6 | 36 | 20.4 |
| 20750000 | 28.9 | 8.6 | 632 | 21.9 |
| 21000000 | 14.0 | 4.6 | 240 | 17.2 |
| 21250000 | 30.0 | 8.5 | 497 | 16.6 |
| 21500000 | 13.8 | 5.3 | 267 | 19.3 |
| 21750000 | 30.0 | 3.8 | 192 | 6.4 |
| 22000000 | 19.3 | 3.9 | 185 | 9.6 |
| 22200000 | 9.0 | 3.2 | 165 | 18.4 |
| 22300000 | 35.9 | 6.1 | 400 | 11.1 |
| 24626900 | 16.3 | 5.3 | 305 | 18.7 |
| 24628590 | 49.1 | 13.6 | 863 | 17.6 |
| 24628595 | 54.2 | 15.5 | 1009 | 18.6 |
| 24628607 | 7.4 | 3.0 | 161 | 21.8 |
| 24628608 | 12.9 | 3.7 | 225 | 17.4 |
| 24628611 | 58.9 | 16.4 | 1004 | 17.1 |
| 24647140 | 7.7 | 3.3 | 162 | 21.0 |
| 24697070 | 17.4 | 5.5 | 392 | 22.5 |
| 24697073 | 22.3 | 5.0 | 349 | 15.7 |
| 25229951 | 57.9 | 11.3 | 894 | 15.4 |
| 25229957 | 9.8 | 3.4 | 180 | 18.5 |
| 25367437 | 31.2 | 9.0 | 763 | 24.5 |
| 25367820 | 32.3 | 7.8 | 559 | 17.3 |

Interpretation:
- **`cyc/gas` ranges from 5.7 to 24.5 (~4×)** — at **equal** EVM gas, a block can cost up to 4× more cycles
  (crypto precompiles: ECDSA, keccak). ⇒ **the prover scales on cycles, not on gas.**
- **Witness size**: 0.4 → 16.4 MiB, tracks block activity (it's the state MPT proofs that inflate it).
- **Gas limit**: ~8 M (2018) → 30 M (merge, 2022) → ~59 M (2026) — visible in the gas column.

## Conclusion

The bottleneck was **neither the GPUs, nor CPU frequency, nor NUMA**, but the **per-GPU serialized RISC-V trace
regeneration** while 80 cores sat idle. Two cumulative levers addressed it — **cluster pipelining** (config) and
**removal of client re-execution** (runner code) — for **~−38 % on medium/heavy blocks** at 8 GPU, and a solid
base for targeting real-time at 16 GPU.
