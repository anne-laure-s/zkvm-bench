# ZisK prover — bring-up & benchmark report (16× RTX 5090)

Bring-up of a **ZisK** prover for Ethereum mainnet blocks on real GPUs: the challenges encountered, and the
result against SP1. Companion to [`zisk-benchmark.md`](zisk-benchmark.md) (raw numbers).

## 1. Objective
Prove real mainnet blocks with ZisK on real GPU hardware, to **compare against SP1** on the **same blocks**.
"Off-box" posture like SP1: the Mac compiles the ELF + generates the witness; the box only **proves** (witness
generation is outside the measured window).

## 2. Test specs

**Hardware (proving box, vast.ai):**
- **16× NVIDIA RTX 5090** (32 GB, compute cap **12.0 / sm_120**), driver 580.95.05
- CPU **AMD EPYC 9654** 96-core, **2 sockets / 2 NUMA nodes**, ~500 GB RAM, `/dev/shm` ~250 GB
- Disk **≥128 GB**, **non-privileged Docker container** (key constraint: see §4)

**Software:**
- **ZisK v1.0.0-alpha** (commit `4b9f758`) — `cargo-zisk`/`ziskemu`/`zisk-coordinator`/`zisk-worker` via `ziskup`
- **proofman v0.18.0**, **CUDA 13.0**, **OpenMPI 4.1.6**, Ubuntu 24.04
- Guest: `zisk-eth-client` → `stateless-validator-reth` (ELF `zec-reth`)
- Proving key **~30 GB** (extracted), asm ROM **~5 GB**

**Block set:** 11 mainnet blocks, **7–58 Mgas**, **74–479 Msteps** (`ziskemu -m`), witnesses (`input.bin`
+ `hints`) of **5.4 to 29.7 MB**. (1 block excluded: `25229955`, the `p256verify`/Osaka precompile crashes `hints-gen`.)

**Methodology:** **distributed** backend (coordinator + 1 worker) in **`NO_MPI`** mode (1 process driving the
16 GPUs), **compressed** (recursive) STARK, proof via `cargo-zisk remote prove`. Numbers = **median of
3 passes** after **warm-up** (2 proofs discarded). All proofs re-verified (`cargo-zisk verify`).

## 3. What was set up (`zisk-infra/`)
- `cluster/00-install-once.sh` — all-in-one non-interactive install: deps (official list), rustup,
  ziskup + key, **memlock patch** (`globals.c`), build of the `nolock.so` shim, drop-in of the **patched worker**
- `cluster/01-setup-elf.sh` — asm ROM setup per ELF (local path)
- `cluster/start.sh` — coordinator + worker (MPI **or** `NO_MPI`), configurable
- `cluster/nolock.c` → `nolock.so` — `LD_PRELOAD` shim (see §4.3) · `fix-memlock-patch.sh` — standalone patch
- `cluster/clean-bench.sh` — clean bench (warm-up + N passes + timings) · `watch.sh` — live dashboard
- `zisk-worker` **patched** (off-box rebuild, see §4.6) · `zisk-runner`, ELF, 11 witnesses, docs

## 4. Challenges encountered & resolved

| # | Problem | Root cause | Fix |
|---|---|---|---|
| 1 | Disk sizing | SP1's 30 GB don't transfer; ZisK key ~30 GB **extracted** + asm ROM + binaries | Box **≥128 GB**; disk pre-check in `00-install-once` |
| 2 | ROM `make` fails (exit 2) | `libgmp-dev`/`libomp-dev`… missing (`-lgmp -lgmpxx`) | **Official** deps list (`tools/test-env/install_deps.sh`) in `00-install-once` |
| 3 | `mmap(rom) errno=11` (C server) | Non-privileged container: `memlock`=64 KB **non-modifiable** (`CAP_SYS_RESOURCE` absent); the asm mmaps the ROM with `MAP_LOCKED` | `globals.c` patch: `map_locked_flag = 0` (the `-u` flag **does not propagate** in the SDK) |
| 4 | `mmap(MAP_FIXED)` 6 GB fails (**Rust** runner) | Same memlock, different layer (`multi_shmem.rs`), compiled into `cargo-zisk` | **`nolock.so`** shim (`LD_PRELOAD`) that strips `MAP_LOCKED` from every mmap — worker + microservices |
| 5 | Silent stall ~4 min (local prove) | OpenMPI `MPI_Init` in **singleton** mode (daemon timeout) | `OMPI_MCA_ess_singleton_isolated=1`; **absent** in distributed mode (under `mpirun`) |
| 6 | `--emulator` backend can't prove reth | `register_hints_stream not supported` — hints require the asm | **asm** backend mandatory (worker without `--emulator`) |
| 7 | Multi-rank MPI: 1 rank **segfaults** | Race in the concurrent startup of the ASM microservices + `--bind-to numa` fails in a container (membind blocked) | Dropped multi-rank → **`NO_MPI`** (1 process, all GPUs) |
| 8 | **`count_and_plan.cu:1586` `cudaMemset invalid argument`** (the big bug) | In multi-GPU single-process, `reset()` issues its `cudaMemset` **without** `cudaSetDevice(gpu_device_)` → drift of the current device | **1-line patch** (adding the `cudaSetDevice`) + **rebuild of `zisk-worker`** off-box (Docker CUDA 13, `CUDA_ARCHS=120`/sm_120) → binary drop-in (BuildID `4da61fa8…`) |
| 9 | Long warm-up | 16 GPU × ~30 GB of allocation before registration | ~8–12 min, **paid once** (worker stays warm), outside measurement |

**Cross-cutting points:**
- The patched worker is **reusable**: built once (Docker, `nvidia/cuda:13.0.2-devel-ubuntu24.04`,
  `CUDA_ARCHS=120`), `cudart` **statically linked** → independent of the box's CUDA version; drop-in in 30 s
  on any **5090/sm_120** box (verified via **BuildID**, not `--version`, which is identical between stock and patched).
- Only **`zisk-worker`** needs the patch (coordinator/cargo-zisk stay stock).
- Permanent fix: upstream the 1-liner to ZisK → future ziskup versions without a rebuild.

## 5. Results — ZisK vs SP1 (same blocks, 16× RTX 5090, compressed STARK, re-execution excluded on both sides)

- **ZisK ≈ 2.2× faster than SP1**, constant across 7→58 Mgas (ratio 2.1–2.6×).
  E.g. the 479 Msteps / 58 Mgas block: **ZisK 20.7 s vs SP1 44.4 s**. Total for 11 blocks: **~121 s vs ~276 s**.
- **ZisK proof ~3.3× smaller**: **373 KB** (constant, recursive STARK) vs SP1 1.27 MB.
- **ZisK throughput**: ~14 Msteps/s (small blocks) → ~23 Msteps/s (large).
- Caveats: ZisK = median of 3 passes (variance <0.5 s); SP1 = 1 run/block. All ZisK proofs
  `verify`-OK; SP1 `verified:false`. `steps` (ZisK) ≠ `cycles` (SP1) — not compared directly.

## 6. Summary
A functional **multi-GPU prover** now runs on 16× RTX 5090, together with a **complete ZisK vs SP1 benchmark**
on 11 real mainnet blocks. The main blocker (multi-GPU `count_and_plan` bug) was resolved with a 1-line patch
+ a reusable off-box rebuild. Conclusion: on this hardware and block set, **ZisK proves ~2× faster than SP1
with a proof ~3× smaller.**
