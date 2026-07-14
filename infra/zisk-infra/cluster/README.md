# zisk-cluster — multi-GPU ZisK prover on a Vast box (gRPC coordinator/worker)

The box only **proves**. The Mac builds the ELF + generates the witness (input + hints) and ships
them over (same discipline as the SP1 infra). ZisK installs cleanly via `ziskup` — no skopeo image
extraction, no redis/postgres.

**Multi-GPU here = single-process (`NO_MPI=1`), NOT MPI.** With the **patched** `zisk-worker`
(`count_and_plan.cu` fix, installed by `00-install-once.sh`), one worker process drives **all** GPUs
(proofman assigns every GPU to the single rank). This is the config the 16×5090 benchmark ran, and
what `start.sh` does by default. Caveat: with the **stock** worker a single process uses ~1 GPU.

ZisK's *official* multi-GPU path is MPI — `start.sh` still builds the exact `mpirun -np MPI_NP
-map-by ppr:N:numa --bind-to numa … zisk-worker` that ZisK's deploy uses (`mpi_params.sh` auto-sizes
~2 GPUs/rank). Opt in with `USE_MPI=1`. **But it segfaults on unprivileged vast.ai containers**
(NUMA membind fails on socket-1 ranks); if you must, drop NUMA: `MPI_MAPBY=slot MPI_BIND=none` with
`-np = n_gpus` (1 rank/GPU). The `-g/--gpu` flag exists only on a **GPU build** (hidden on CPU builds).

> ⚠️ **ZisK is v1.0.0-alpha & GPU flags are hidden on CPU builds.** On the box (GPU build) re-check
> `zisk-worker --help` / `cargo-zisk prove --help` for GPU options. The canonical bring-up is ZisK's
> own installer (which `start.sh` mirrors):
> ```sh
> bash ~/zisk/distributed/deploy/scripts/coordinator/install.sh --no-service --api-port 7000
> bash ~/zisk/distributed/deploy/scripts/worker/install.sh      --no-service --gpu --coordinator-url http://127.0.0.1:50051
> ```
> Each prints the exact foreground command (incl. the `mpirun …` line). Use it if `start.sh` misbehaves.

## One-time, on the box (persists across stop/start)
```sh
./00-install-once.sh                 # system deps + ziskup --provingkey (GPU) + provingKey
#   -> cargo-zisk --version MUST report [gpu]; check provingKey size it printed.
```

## Proving — multi-GPU (verified against cargo-zisk v1.0.0-alpha)

**Distributed multi-GPU (coordinator + worker).** Setup is done on the coordinator
(`remote setup`), NOT locally. The worker takes no `--gpu` (auto on GPU build) — it needs a
proving-key folder and a backend (default `--asm`; `--emulator` only for hint-less guests).
```sh
./start.sh                                          # coordinator (api 7000 / cluster 50051) + single-process worker, ALL GPUs
                                                    #   default: NO_MPI + asm backend + nolock.so auto-loaded (the benchmark config)
                                                    #   MPI path instead: USE_MPI=1 ZISK_SRC=~/zisk ./start.sh  (segfaults on vast.ai — see top)
cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000   # once/ELF
./submit.sh ~/zisk-reth.elf ~/1-24628607.bin ~/1-24628607.hints   # remote prove -> runs/<tag>-<ts>/
./stop.sh
```
Confirm in `logs/worker.log` that the single worker registers and `nvidia-smi` shows **all** GPUs busy
during a proof (for the MPI path, `--report-bindings` should show MPI_NP ranks across all GPUs, not 1).
⚠️ Worker backend wiring (`WORKER_BACKEND=asm|emulator`, `PROVING_KEY`, `ASM_FILE`) is the piece to
confirm on the box from `zisk-worker --help` + what `remote setup` produced — see `start.sh`.

## What you copy from your Mac first
```sh
# from zisk-infra/ on the Mac (using a committed sample block, e.g. the smallest):
scp -P <PORT> -r cluster zisk-runner root@<HOST>:~/zisk-infra/
scp -P <PORT> ../../guests/zisk-reth/zisk-reth.elf root@<HOST>:~/zisk-reth.elf
scp -P <PORT> ../../guests/zisk-reth/inputs/1-24628607.bin   root@<HOST>:~/1-24628607.bin
scp -P <PORT> ../../guests/zisk-reth/inputs/1-24628607.hints root@<HOST>:~/1-24628607.hints
```
`submit.sh` saves the run record `runs/<tag>-<ts>/`: `proof.bin`, `report.json`
(timings + `proof_bytes`), `prove.log`, `env.txt`, plus `coordinator.log` / `worker.log`.

## Tuning (vs the 37% idle seen on SP1)
The primary multi-GPU lever is the **MPI layout** (`MPI_NP`/`ppr`, auto from `mpi_params.sh` —
defaults to ~2 GPUs/rank). `--max-streams` (`zisk-worker -t`) is a secondary per-rank knob.
```sh
WORKERS_ONLY=1 ./stop.sh
MAX_STREAMS=2 WORKERS_ONLY=1 ./start.sh     # try per-rank GPU streams
./submit.sh ~/zisk-reth.elf ~/1-24628607.bin ~/1-24628607.hints
```
Watch `nvidia-smi` during a proof — all GPUs should be busy. `logs/worker.log` (`--report-bindings`)
shows the rank→NUMA→GPU mapping.

## Retrieve results (from the Mac)
```sh
./fetch-runs.sh root@<HOST>:<PORT>        # -> ../results/
```

## Driving it from the Mac instead (no ssh-in)
`zisk-infra/run prove ELF=… INPUT=… REMOTE=root@<HOST> PORT=<PORT>` ssh-uploads ELF+input+hints,
runs `zisk-runner` on the box (backend=remote → the running coordinator), and pulls the run record
back to `results/…`. The coordinator/worker must already be up (`./start.sh`).

## Ports (override via env in start.sh)
- coordinator: api `7000` (client submit), cluster `50051` (worker join), metrics `9090`.
- The runner/submit talk to the **api** port; the worker dials the **cluster** port.

## Likely first failures (and where to look)
- `cargo-zisk --version` says `[cpu]` → ziskup didn't see CUDA at install; reinstall with the driver present.
- `logs/worker.log` — worker can't reach the coordinator → check `--coordinator-url` / `CLUSTER_PORT`.
- `logs/coordinator.log` — no worker registered → worker crashed (CUDA arch? `CUDA_ARCHS`).
- prove fails on `--hints`/`--asm` → run `cargo-zisk remote setup -e <elf> --hints` on the coordinator after `start.sh`.
- worker won't register / proofs hang → check `zisk-worker --help` for the backend wiring (`--emulator` vs `--asm <file>`, `-k <proving-key>`) and adjust `WORKER_BACKEND`/`ASM_FILE`/`PROVING_KEY` in `start.sh`.
