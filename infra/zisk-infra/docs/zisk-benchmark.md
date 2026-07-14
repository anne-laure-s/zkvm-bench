# ZisK block-proving benchmark — results & SP1 comparison

Head-to-head with SP1 on the **same blocks** and the **same class of box** (target: 16× RTX 5090,
matching ZisK's public `Zisk 16x5090` ethproofs cluster and the SP1 `cluster-native` runs).

## Mode mapping (apples-to-apples)

| Axis            | SP1                       | ZisK                                   |
|-----------------|---------------------------|----------------------------------------|
| Core STARK proof (measured) | `prove-compressed`        | `prove-compressed` (default recursive STARK) |
| On-chain proof (not measured) | `prove-groth16`           | `prove-plonk` (`--plonk`)              |
| "cycles"        | instruction cycles        | **steps** (`ziskemu -m`)              |
| Witness         | `bincode(EthClientExecutorInput)` | `input.bin` (framed) + `hints`        |

> We compare `prove_secs` only (the GPU proving window). ZisK's per-ELF `cargo-zisk setup` and the
> witness generation run **outside** that window, so the number stays clean — same posture as SP1's
> network prover skipping pre-submit simulation.

## Block set

Using the committed `zisk-eth-client` sample blocks (no debug RPC needed — see "Why samples" below).
ZisK witnesses (input + hints) generated locally; **ZisK steps measured** via `ziskemu`. SP1 side =
generate the SAME blocks with RSP (`eth_getProof`/Alchemy) and prove on the SP1 cluster.

| Block (tag)   | txs | Mgas | ZisK steps   | SP1 cycles | ZisK prove_secs | SP1 prove_secs | ZisK proof B | SP1 proof B |
|---------------|-----|------|--------------|------------|-----------------|----------------|--------------|-------------|
| 1-24647140    | 60  | 7    | 74,123,669   |            | 5.3             | 12.08          | 381,643      | 1,273,373   |
| 1-24628607    | 66  | 7    | 77,032,188   |            | 5.5             | 12.08          | 381,643      | 1,273,373   |
| 1-25229957    | 107 | 9    | 78,779,780   |            | 5.4             | 12.08          | 381,643      | 1,273,376   |
| 1-24628608    | 146 | 12   | 104,704,880  |            | 6.2             | 14.10          | 381,643      | 1,273,373   |
| 1-24626900    | 221 | 16   | 134,583,289  |            | 7.7             | 18.16          | 381,643      | 1,273,373   |
| 1-24697073    | 308 | 22   | 143,056,130  |            | 7.8             | 20.11          | 381,643      | 1,273,374   |
| 1-24697070    | 270 | 17   | 153,259,439  |            | 7.7             | 20.11          | 381,643      | 1,273,376   |
| 1-24628590    | 478 | 49   | 409,510,987  |            | 17.9            | 40.31          | 381,643      | 1,273,368   |
| 1-25229951    | 420 | 57   | 413,614,075  |            | 17.5            | 38.23          | 381,643      | 1,273,372   |
| 1-24628595    | 641 | 54   | 448,359,946  |            | 19.8            | 44.32          | 381,643      | 1,273,364   |
| 1-24628611    | 627 | 58   | 478,728,818  |            | 20.7            | 44.36          | 381,643      | 1,273,373   |
<!-- SP1: runs-zisk-16gpu-optim (16× RTX 5090, compressed, re-exec removed via skip_simulation), 2026-07-03.
     ZisK/SP1 prove_secs ratio ≈ 2.1–2.6× (ZisK faster); ZisK proof 373 KB vs SP1 1.27 MB (~3.3× smaller).
     SP1 cycles unavailable on these optim runs (the client re-exec that logs them was removed). SP1 single-run/block. -->


(~8 Msteps/Mgas; higher ratio on small blocks = fixed overhead dominates, as seen on SP1.)
⚠️ Block **25229955** (305 txs, 39 Mgas) excluded: it uses the **P256/secp256r1 `p256verify` (Osaka)
precompile** and `hints-gen` panics natively on it (`non-unwinding panic`) — likely a precompile gap
in the native hints path. Revisit if needed.

## ZisK results — 16× RTX 5090, run 2026-07-03 (median of 3 passes)
Config: ZisK v1.0.0-alpha with the **patched `zisk-worker`** (count_and_plan.cu `reset()` + `cudaSetDevice`
fix, built for sm_120; BuildID `4da61fa8…`), **NO_MPI** single-process backend (all 16 GPUs, one rank), asm
backend + `nolock.so` LD_PRELOAD shim, distributed `remote prove` on a warm worker. `prove_secs` = end-to-end
`remote prove` wall time on the warm cluster, **median of 3 passes** (2 warm-up proofs discarded). All 33 proofs
`cargo-zisk verify`-OK; proof size constant **381,643 B** (recursive STARK → fixed-size final proof).

- **Very stable run:** inter-pass spread < 0.5 s (e.g. 24628611: 20.71/20.66/20.64) — no outliers, unlike the earlier 8-GPU single-shot.
- **Throughput** climbs with size then plateaus: ~**14 Msteps/s** (74–105 M) → ~**22–24 Msteps/s** (410–479 M). Biggest block (479 M steps / 58 Mgas) proved in **20.7 s**.
- **16 vs 8 GPU:** big blocks scale well — 24628590 (410 M) 44.9 s → **17.9 s**, 24628611 (479 M) 26.2 s → **20.7 s**; small blocks ~flat (fixed overhead dominates). (The 8-GPU numbers were a single shared-host run — indicative only.)
- **Warm-up (paid once):** worker registration ~8–12 min on 16 GPU (allocating ~30 GB × 16 GPUs); it's excluded from `prove_secs`.
- Raw per-pass timings + `worker.log` phase breakdown archived under `results/zisk-reth-16gpu-clean/`.


Sources: `cluster/runs/<tag>-<ts>/report.json` (box-driven) or `results/zisk-reth/<tag>/…/report.json`
(Mac-driven); SP1 comparison numbers come from the SP1 stack's own run records.

## Why samples (no debug RPC)
The reth block-input path (`zisk-eth-client` + `zisk-ethproofs`) builds the witness via reth's
`debug_executionWitness` RPC (node-side), unlike SP1/RSP which traces accesses client-side and pulls
them with standard `eth_getProof` (Alchemy-friendly). Alchemy doesn't expose `debug_executionWitness`,
so arbitrary/historical blocks would need a self-hosted reth node (archive for old blocks). The
committed samples sidestep this entirely (inputs shipped, hints generated natively, no RPC).

## GPU occupancy / tuning

Tune `--max-streams` (the ZisK lever vs the 37% idle observed on SP1's 8-GPU runs). Record effective
occupancy from `nvidia-smi` during a proof.

| Config (`--max-streams`) | prove_secs | approx GPU util | notes |
|--------------------------|-----------|------------------|-------|
|                          |           |                  |       |

## Environment / reproducibility
Each run record carries `env.txt` (GPU model, driver, `cargo-zisk --version`). The ELF is pinned by
`../../guests/zisk-reth/zisk-reth.commit`; re-run the per-ELF setup (`cargo-zisk remote setup` for the
distributed path, `cluster/01-setup-elf.sh` for the local backend) whenever the ELF changes.

## CLI status (verified against installed v1.0.0-alpha)
CONFIRMED (scripts match the real CLI):
- `cargo-zisk prove`: `-e -i -o(file) --asm --hints` (GPU auto on GPU build, **no `--gpu`**; `--hints` needs `--asm`); `-c`=minimal, `--plonk`=on-chain.
- `cargo-zisk remote prove --coordinator <url>(default :7000) -e -i -o --hints --timeout` (no `--asm`).
- `cargo-zisk setup -e <elf> --asm --hints` (local) / `cargo-zisk remote setup -e <elf> --hints` (cluster).
- `cargo-zisk verify -p <proof>`; `ziskemu -e -i -m` (steps); `input-gen [-c reth] rpc -u -b`; `hints-gen [-c reth] -o <dir> <input.bin>`.
- coordinator `-a/--api-port --cluster-port --metrics-port`; worker `-c/--coordinator-url -t/--max-streams -k/--proving-key (--emulator|--asm <file>)`.

MULTI-GPU (as actually run — supersedes the MPI-first assumption below):
- **Used config = single-process (`NO_MPI=1`) worker + PATCHED `zisk-worker`.** With the count_and_plan.cu fix, one process drives ALL GPUs (proofman assigns every GPU to the single rank). This is what `cluster/start.sh` does by default. A single *STOCK* worker only uses ~1 GPU — the patch is what makes single-process multi-GPU work.
- ZisK's *official* path is MPI (`mpirun -np MPI_NP -map-by ppr:N:numa --bind-to numa`, ~2 GPUs/rank from `mpi_params.sh`); `start.sh` still builds it under `USE_MPI=1`. **It segfaults on unprivileged vast.ai containers** (NUMA membind fails on socket-1 ranks — see zisk-bringup-report.md step 7), so we did not use it.
- ⚠️ GPU-only flags (worker `-g/--gpu`, possibly `cargo-zisk prove`'s GPU flag) are **hidden on a CPU build's `--help`** — they exist on the GPU build. Re-check on the box.

STILL TO CONFIRM ON THE BOX (the only unknowns left):
- worker backend wiring: `--emulator` vs `--asm <file>` + the `-k <proving-key>` folder from `remote setup` (see `cluster/start.sh`: `WORKER_BACKEND`/`ASM_FILE`/`PROVING_KEY`).
- that `logs/worker.log` (`--report-bindings`) shows MPI_NP ranks across all GPUs (not 1).
- provingKey: 2.98 GB **download** but **≈30 GB+ EXTRACTED** on disk (measured 2026-06-30: `~/.zisk/provingKey` hit 30 GB and still filled a 32 GB box mid-const-tree-gen → full size is >30 GB). The const-tree files (Merkle trees over all AIRs/precompiles + recursion) are the bulk. **30 GB is IMPOSSIBLE; SP1's 30 GB does NOT transfer to ZisK.** Box disk budget: **≥64 GB** (key ~30-40 GB + ROM asm few GB + binaries/toolchain ~1.6 GB + proofs/logs + headroom). We do NOT fetch the 20.4 GB PLONK key (no on-chain).
- The `cargo-zisk prove` GPU flag exists only on the GPU build. Note: single-process ≈ 1 GPU holds only for the STOCK worker; with the patched worker a single process (`NO_MPI`) drives ALL GPUs — that's the path used.
