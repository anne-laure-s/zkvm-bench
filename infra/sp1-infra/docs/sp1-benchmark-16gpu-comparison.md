# Synthesis — 16 GPU results & 8 vs 16 comparison (SP1/RSP, RTX 5090)

Follow-up to [`sp1-benchmark-synthesis.md`](sp1-benchmark-synthesis.md). Data: `results/cluster-native-runs/`
(23 blocks proved on **16× RTX 5090**, mode `compressed`, SP1 6.2.4 / circuit v6.1.0, dataset 2026-06-24),
decoded from each `prove.log` (+ `env.txt`).

> **Re-execution phase removed — and here it is measured.** Each `sp1-runner` (NetworkProver) run
> runs a **client executor** (`minimal executor finished in N cycles`) *before* submitting to the
> cluster, just to recount cycles/gas — even though those cycles are **already known**
> (`rsp/report.csv`). This pass is on the critical path and **accounts for 33 % of the measured
> `Prove` time on average** (up to 39 %). The columns below isolate it (`re-exec`) and give the
> **net** GPU proof time (`netProve`).

---

## 1. 16 GPU results — per block (best run/block)

`gas_M` = Ethereum gas (M) · `cyc_M` = RISC-V cycles (M) · `Setup` = vkey (per-ELF, ~constant) ·
`re-exec` = **client** executor (CPU, skippable) · `CPUchain` = **cluster** `CoreExecute` (CPU,
trace-gen; **included in** `netProve`, streamed in parallel with the GPUs) · `netProve` = cluster proof (CPUchain
+ GPU) · `Prove` = `re-exec` + `netProve` · `Total` = `Setup` + `Prove` + I/O · `Mc/s` = net cycles/s on 16 GPU.

| block | gas_M | cyc_M | Setup s | re-exec s | CPUchain s | **netProve s** | Prove s | Total s | Mc/s net |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 13000000 | 1.3 | 27 | 1.32 | 1.4 | 3.1 | **6.0** | 7.5 | 9.4 | 4.5 |
| 20500000 | 1.8 | 36 | 1.34 | 1.7 | 3.6 | **8.0** | 9.8 | 11.7 | 4.5 |
| 19426587 | 2.6 | 45 | 1.29 | 2.3 | 4.3 | **8.0** | 10.4 | 12.2 | 5.6 |
| 18884864 | 4.3 | 91 | 1.28 | 3.4 | 5.6 | **10.1** | 13.5 | 15.4 | 9.0 |
| 5000000 | 8.0 | 116 | 1.33 | 4.7 | 6.4 | **10.1** | 14.8 | 16.7 | 11.6 |
| 17000000 | 9.2 | 152 | 1.32 | 5.8 | 7.4 | **12.1** | 17.8 | 19.7 | 12.6 |
| 19000000 | 9.6 | 154 | 1.33 | 6.0 | 8.2 | **12.1** | 18.1 | 20.0 | 12.8 |
| 10000000 | 10.0 | 161 | 1.32 | 5.8 | 8.0 | **12.1** | 17.9 | 19.8 | 13.3 |
| 22200000 | 9.0 | 165 | 1.34 | 6.4 | 7.8 | **12.1** | 18.4 | 20.4 | 13.7 |
| 15537394 ⚠ᵉˣᵉᶜ | 30.0 | 169 | 1.33 | 6.0 | 8.4 | **12.1** | 18.1 | 20.0 | 14.1 |
| 22000000 | 19.3 | 185 | 1.30 | 6.7 | 8.3 | **12.1** | 18.8 | 20.7 | 15.4 |
| 21750000 | 30.0 | 192 | 1.33 | 7.1 | 8.7 | **12.1** | 19.2 | 21.1 | 15.9 |
| 18000000 | 16.2 | 193 | 1.28 | 6.9 | 8.5 | **12.1** | 19.0 | 20.8 | 16.0 |
| **20000000** | 11.1 | 209 | 1.29 | 7.9 | 8.8 | **14.1** | 22.0 | 23.8 | 14.8 |
| 21000000 | 14.0 | 240 | 1.29 | 8.9 | 9.9 | **14.1** | 23.0 | 24.9 | 17.0 |
| 21500000 | 13.8 | 267 | 1.31 | 9.8 | 11.0 | **16.1** | 25.9 | 27.8 | 16.5 |
| 20250000 | 17.8 | 354 | 1.30 | 11.2 | 14.6 | **18.1** | 29.3 | 31.2 | 19.5 |
| 22300000 | 35.9 | 400 | 1.31 | 10.4 | 13.4 | **18.1** | 28.6 | 30.5 | 22.1 |
| 21250000 | 30.0 | 497 | 1.30 | 13.3 | 18.1 | **24.2** | 37.5 | 39.3 | 20.5 |
| 19500000 | 30.0 | 510 | 1.31 | 13.0 | 20.0 | **24.2** | 37.2 | 39.1 | 21.1 |
| 25367820 | 32.3 | 559 | 1.31 | 14.8 | 22.6 | **28.2** | 43.0 | 44.9 | 19.8 |
| 20750000 | 28.9 | 632 | 1.30 | 13.6 | 21.1 | **26.2** | 39.8 | 41.7 | 24.2 |
| 25367437 | 31.2 | 763 | 1.30 | 18.8 | 26.4 | **30.2** | 49.0 | 50.9 | 25.3 |

**CPU chain (`CPUchain` = cluster `CoreExecute`, excluding client re-exec)**: extracted from the `cpu-node`
logs by `req_id` (the `cpu-node` only does `CoreExecute` + orchestration; shards & recursion are 100 %
GPU). Model: **`CoreExecute ≈ 2.6 s + 0.0316·cyc_M` → ~32 Mc/s**, single-thread, **independent of the
number of GPUs**. Its share of `netProve` **rises with block size**: 52 % at 27 M cyc → 62 % at
209 M → **87 % at 763 M**. In other words, the bigger the block, the more the GPUs **wait for CPU
execution** — and adding GPUs changes nothing. With the client re-exec (also CPU and serial, but
skippable), block 20000000 pays **7.9 s + 8.8 s = 16.7 s of CPU** out of 23.8 s of `Total`.

### CPU chain breakdown (`CoreExecute`) by step — wall-clock

Sub-steps extracted from the `cycle-tracker` markers of the `cpu-node` logs, windowed on the `CoreExecute`
of each `req` (1 execution per window, verified). `trace-gen` = remainder of `CoreExecute` (startup +
header validation + **record/trace generation**). Times in **wall-clock seconds**.

The first 7 columns break down the **CPU chain** (`CoreExec` = their sum). `GPUspan` is added
as a **parallel GPU track**: the fleet's wall-clock window (1st `ProveShard` → last `RecursionReduce`),
extracted from the `gpu*` logs by `req`. **It overlaps `CoreExec` (streaming), it is NOT additive.**

| block | cyc_M | deser | witDB | recSnd | blkExe | stRoot | trace-gen | **CoreExec (CPU)** | GPUspan |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 13000000 | 27 | 0.03 | 0.04 | 0.11 | 0.16 | 0.02 | 2.73 | 3.09 | 3.5 |
| 20500000 | 36 | 0.06 | 0.06 | 0.22 | 0.06 | 0.04 | 3.15 | 3.59 | 3.9 |
| 19426587 | 45 | 0.06 | 0.06 | 0.24 | 0.39 | 0.04 | 3.47 | 4.26 | 4.6 |
| 18884864 | 91 | 0.11 | 0.12 | 0.20 | 0.27 | 0.07 | 4.86 | 5.63 | 5.8 |
| 5000000 | 116 | 0.14 | 0.15 | 0.71 | 0.31 | 0.11 | 4.95 | 6.37 | 6.5 |
| 17000000 | 152 | 0.21 | 0.22 | 0.67 | 0.47 | 0.13 | 5.68 | 7.38 | 7.6 |
| 19000000 | 154 | 0.21 | 0.22 | 0.87 | 0.38 | 0.14 | 6.35 | 8.17 | 8.4 |
| 10000000 | 161 | 0.23 | 0.24 | 0.67 | 0.35 | 0.11 | 6.44 | 8.04 | 8.1 |
| 22200000 | 165 | 0.23 | 0.24 | 0.72 | 0.68 | 0.14 | 5.83 | 7.84 | 8.2 |
| 15537394 ⚠ᵉˣᵉᶜ | 169 | 0.07 | 0.07 | 0.52 | 0.32 | 0.06 | 7.32 | 8.36 | 8.5 |
| 22000000 | 185 | 0.27 | 0.29 | 0.64 | 0.48 | 0.20 | 6.40 | 8.28 | 8.9 |
| 21750000 | 192 | 0.27 | 0.26 | 0.70 | 0.66 | 0.20 | 6.65 | 8.74 | 9.5 |
| 18000000 | 193 | 0.26 | 0.27 | 0.63 | 0.58 | 0.15 | 6.57 | 8.46 | 9.1 |
| **20000000** | 209 | 0.30 | 0.32 | 0.88 | 0.77 | 0.26 | 6.31 | 8.84 | 9.7 |
| 21000000 | 240 | 0.34 | 0.36 | 1.26 | 0.90 | 0.34 | 6.70 | 9.90 | 10.7 |
| 21500000 | 267 | 0.39 | 0.41 | 1.20 | 1.10 | 0.32 | 7.57 | 10.99 | 11.9 |
| 20250000 | 354 | 0.49 | 0.54 | 1.25 | 1.70 | 0.54 | 10.11 | 14.63 | 14.8 |
| 22300000 | 400 | 0.55 | 0.57 | 0.55 | 1.66 | 1.06 | 9.00 | 13.39 | 14.5 |
| 21250000 | 497 | 0.68 | 0.73 | 4.80 | 1.80 | 0.18 | 9.86 | 18.05 | 20.0 |
| 19500000 | 510 | 0.70 | 0.76 | 3.49 | 2.63 | 0.20 | 12.19 | 19.97 | 20.3 |
| 25367820 | 559 | 0.72 | 0.76 | 6.76 | 2.12 | 0.23 | 12.02 | 22.61 | 24.2 |
| 20750000 | 632 | 0.66 | 0.71 | 2.68 | 2.83 | 0.15 | 14.11 | 21.14 | 22.3 |
| 25367437 | 763 | 0.75 | 0.80 | 5.82 | 2.88 | 0.91 | 15.26 | 26.42 | 27.2 |

`deser` = deserialize inputs · `witDB` = initialize witness db · `recSnd` = recover senders (ecrecover) ·
`blkExe` = block execution · `stRoot` = compute state root · `GPUspan` = GPU wall-clock window (≠ additive).

Reading:
- **`trace-gen` is the dominant item: ~70–80 % of `CoreExecute`** (6.3 s out of 8.8 s for the 209 M). The
  reduction of the CPU chain plays out **there** (record/trace generation), not in the EVM phases.
- **`recover senders` (ecrecover) is the largest guest execution phase**, and it scales with the
  **number of tx**, not with the cycles: 0.1 s (17 tx) → **6.8 s (683 tx, block 25367820)**. It runs
  slowly (~4–6 MHz) because of the native secp256k1 on the executor side.
- ⚠️ **wall-clock ≠ cycles**: the *cycle* composition (deser 23 %, witDB 21 % — see the 8-GPU synthesis) is NOT
  found in *time* (deser/witDB are near-free at ~180 MHz; recover senders is slow). The
  two views are complementary: cycles = proof cost, wall-clock = CPU execution cost.
- **`GPUspan` ≈ `netProve`**: the GPU starts ~4 s after the beginning of `CoreExec` (the time for the 1st
  shards to be streamed) and finishes last → `netProve` ≈ offset + `GPUspan`. For the 209 M: `CoreExec`
  8.8 s (CPU) ‖ `GPUspan` 9.7 s (GPU) **overlapped** → `netProve` 14.1 s. The fleet proves **several
  shards per GPU in parallel** (effective concurrency ~30× on the 209 M, up to ~66× on the large
  blocks — well beyond the 16 physical GPUs), so the GPU is not the wall as long as the CPU chain feeds it.

---

**Setup ~constant 1.28–1.34 s** (mean 1.31 s): this is the derivation of the vkey of `rsp.elf`, therefore
**per-ELF and not per-block**. In a steady-state service it is done once and amortized →
to be excluded from the per-block cost (like the re-exec). That is why it is in `Total` but not in `Prove`.

⚠ᵉˣᵉᶜ 15537394: outlier of **cycle composition** (49 % in "block validation", cause unresolved —
see the 8-GPU synthesis), not an outlier of proof time. Its 16 GPU times are normal for its 169 M cycles.

## 2. Scaling model (regressions over the 23 blocks)

| Component | model | throughput | threshold < 10 s |
|---|---|---:|---:|
| **Total** | ≈ 10.7 s + 0.055·cyc_M | 18 Mc/s | never (floor ~9 s) |
| **Prove** (with re-exec) | ≈ 8.8 s + 0.055·cyc_M | 18 Mc/s | < 21 M cyc |
| **re-exec** (client, skippable) | ≈ 2.3 s + 0.022·cyc_M | 46 Mc/s | — |
| **CPUchain** (CoreExecute cluster) | ≈ 2.6 s + 0.0316·cyc_M | **32 Mc/s** | < 234 M cyc |
| **netProve** (cluster: CPUchain+GPU) | ≈ 6.5 s + 0.033·cyc_M | **30 Mc/s** | **< 106 M cyc** |

Reading:
- **Fixed floor ~6.5 s** even net of re-exec (vkey setup 1.3 s + startup before 1st shard + final
  recursion queue). It is what prevents small blocks from going below ~6 s.
- **`netProve` throughput ~30 Mcycle/s on 16 GPU** = ~1.9 Mc/s/GPU — but this is NOT pure GPU:
  the **CPU chain (`CoreExecute`, ~32 Mc/s single-thread) weighs from 52 % to 87 %** depending on block size,
  and it is **independent of the number of GPUs**. It, not the GPU, sets the ceiling on the large blocks.
- The client re-exec (~46 Mc/s, single-thread CPU, does not parallelize) is a pure serial bottleneck **on top** — skippable.
- Net of re-exec, only blocks **< ~106 M cycles** come in under 10 s. But a typical mainnet block
  is **150–250 M cycles** → **12–16 s net** (and 18–25 s raw).

---

## 3. 8 vs 16 GPU comparison

| | 8 GPU (sweep, block ~200–240 M) | 16 GPU (209–240 M) |
|---|---:|---:|
| steady-state wall-clock | ~40 s | 22–23 s |
| **speedup 8→16** | — | **~1.8×** (≈ 90 % of the ideal 2×) |
| GPU idle | ~37 % | structurally ≥ 8 GPU (floor + re-exec amplified) |

- **Scaling ~90 %**: doubling the GPUs divides the wall-clock by ~1.8, not by 2. The loss comes from
  the **fixed floor** (setup + recursion + startup) and the **serial re-exec** that do not benefit
  from the extra GPUs. Direct consequence: a 3rd doubling (→32 GPU) would yield even less.
- ⚠️ Methodology caveat: the 8 GPU run (diag, CLI `bench input`) and the 16 GPU runs (`sp1-runner`
  NetworkProver) do not take exactly the same submission path; the comparison holds at the
  level of **total wall-clock per comparable block**, not phase by phase.
- On the 8 GPU side, the idle profile (see the previous synthesis) is identical in nature: `CoreExecute`
  CPU ~23 s, shard imbalance 3.5×, recursion queue. The 16 GPU **does not fix** these causes,
  it partially masks them by adding parallelism — hence the sub-linear scaling.

---

## 4. Conclusions

1. **~1/3 of the 16 GPU time is a useless re-execution.** The client recounts the cycles before
   submitting although they are already available (`report.csv`). **Immediate, free gain**: pass the
   cycle/gas limit explicitly (or disable the runner's local execution) → block 20000000: **22.0 s → ~14.1 s**.
   To be done before any other optimization, and to be excluded from any future comparison.

2. **Even net, ~14 s for 209 M cycles on 16 GPU**, vs ethproofs ~7 s. GPU scaling alone does not close
   the gap: fixed floor ~6.5 s + throughput 30 Mc/s.

3. **The only two levers that close the ethproofs gap:**
   - **Reduce the cycles**: ~56 % are stateless overhead (deserialization + witness DB + state
     root, see the 8-GPU synthesis), not EVM. Cutting that by ~2–3× does more than doubling the GPUs.
   - **Recursion/trace-gen on GPU**: the ~6.5 s floor and the idle come from the CPU phases (startup +
     recursion queue). This is exactly the architectural argument of OpenVM 2.0 / ZisK (non-execution
     work on GPU) — hence the value of comparing them **at cycle count and by pipeline structure**, not at
     raw GPU throughput.

4. **Sub-linear scaling (~90 %)**: adding GPUs has diminishing returns as long as the fixed floor
   and the serial re-exec are not addressed. On this stack, **16 GPU ≈ the point of diminishing
   returns** for 200 M blocks; aiming for 32+ GPU without fixing the floor would be a waste.

### Key reference figures
- Block 20000000 (209 M) on 16 GPU: **Total 23.8 s · Prove 22.0 s · re-exec 7.9 s · NET 14.1 s**.
- 8→16 GPU: **~1.8×** (90 % efficiency).
- Net GPU throughput: **~30 Mc/s on 16 GPU** (~1.9 Mc/s/GPU). Fixed floor **~6.5 s**.
- Under 10 s (net) only for **< ~106 M cycles**; typical mainnet blocks 150–250 M → 12–16 s net.
