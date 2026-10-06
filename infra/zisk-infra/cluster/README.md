# zisk-cluster — multi-GPU ZisK prover on a Vast box (gRPC coordinator/worker)

**A reference, not a sequence.** To take a box from nothing to a proof, follow
[`../README.md`](../README.md) § *Experiment — prove one block*; come here for what `up.sh` drives,
the tuning knobs, the ports and the first failures.

The box only **proves**. The Mac builds the ELF + generates the witness (input + hints) and ships
them over (same discipline as the SP1 infra). ZisK installs cleanly via `ziskup` — no skopeo image
extraction, no redis/postgres.

**Multi-GPU here = single-process (`NO_MPI=1`), NOT MPI.** One worker process drives **all** GPUs
(proofman assigns every GPU to the single rank) — what `start.sh` does by default. The **stock**
worker does this from 1.1.0-alpha on: `count_and_plan` binds the owning GPU at every entry point, so
no worker binary is patched or shipped. `00-install-once.sh` refuses a box still carrying an old
hand-patched worker under the stock name — `--version` reports the same string, so it checks the
BuildID.

ZisK's *official* multi-GPU path is MPI — `start.sh` still builds the exact `mpirun -np MPI_NP
-map-by ppr:N:numa --bind-to numa … zisk-worker` that ZisK's deploy uses (`mpi_params.sh` auto-sizes
~2 GPUs/rank). Opt in with `USE_MPI=1`. **But it segfaults on unprivileged vast.ai containers**
(NUMA membind fails on socket-1 ranks); if you must, drop NUMA: `MPI_MAPBY=slot MPI_BIND=none` with
`-np = n_gpus` (1 rank/GPU). The `-g/--gpu` flag exists only on a **GPU build** (hidden on CPU builds).

> ⚠️ **ZisK is v1.3.1-alpha & GPU flags are hidden on CPU builds.** On the box (GPU build) re-check
> `zisk-worker --help` / `cargo-zisk prove --help` for GPU options. The canonical bring-up is ZisK's
> own installer (which `start.sh` mirrors):
> ```sh
> bash ~/zisk/distributed/deploy/scripts/coordinator/install.sh --no-service --api-port 7000
> bash ~/zisk/distributed/deploy/scripts/worker/install.sh      --no-service --gpu --coordinator-url http://127.0.0.1:50051
> ```
> Each prints the exact foreground command (incl. the `mpirun …` line). Use it if `start.sh` misbehaves.

## Bring-up — `up.sh`
```sh
bash up.sh                           # probe → install / verify the key / tear down zombies → start,
                                     #   returning only once a worker is REGISTERED. Safe to re-run.
FORCE_RESTART=1 bash up.sh           # tear the cluster down first, whatever its state
```
It drives the two scripts below and decides which are needed, so neither is a step you normally take
by hand. On a fresh box it gates on d2h (`tests/t3-topo.sh`) before spending the install, and it runs
`check-setup` every time — that is what catches a key left incomplete by an install that still exited 0,
whose only other symptom is `remote setup` failing with "all workers failed setup" tens of minutes later.
From the Mac, `PROVER=<box> ./rtp-up --real --bootstrap` ships this directory there and runs it.

### the pieces it drives
```sh
./00-install-once.sh                 # system deps + ziskup 1.3.1-alpha --provingkey (GPU) + provingKey
#   -> cargo-zisk --version MUST report [gpu]; check provingKey size it printed.
./start.sh                           # launch only: coordinator + worker. Nothing in it installs.
./stop.sh                            # kills by pid file — up.sh also kills by port and by card
```
Box checks (`t0`…`t6`, `summary.sh`) live in [tests/](tests/); `up.sh` calls `t3-topo.sh` itself.

## Proving — multi-GPU (verified against cargo-zisk v1.3.1-alpha)

**Distributed multi-GPU (coordinator + worker).** Setup is done on the coordinator
(`remote setup`), NOT locally. The worker takes no `--gpu` (auto on GPU build) — it needs a
proving-key folder and a backend (default `--asm`; `--emulator` only for hint-less guests).
```sh
./start.sh                                          # (or `bash up.sh`) coordinator (api 7000 / cluster 50051) + single-process worker, ALL GPUs
                                                    #   default: NO_MPI + asm backend + nolock.so auto-loaded (the benchmark config)
                                                    #   MPI path instead: USE_MPI=1 ZISK_SRC=~/zisk ./start.sh  (segfaults on vast.ai — see top)
cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000   # once/ELF
./submit.sh ~/zisk-reth.elf ~/1-24628607.bin ~/1-24628607.hints   # remote prove -> runs/<tag>-<ts>/
# hint-less guest (e.g. Monad): compiled without a hints stream, so just drop hints — the DEFAULT asm
#   backend handles it (asm is a superset; --hints is optional on it). `remote setup -e <elf>` WITHOUT
#   --hints, then submit with NO hints arg:
#   ./submit.sh ~/elfs/monad-zkvm-guest-zisk.elf ~/witnesses-monad/1-<blk>.bin   (submit.sh: hints optional)
#   (WORKER_BACKEND=emulator also works as a fallback — same Rust emulator, but slower trace-gen.)
./stop.sh
```
Confirm in `logs/worker.log` that the single worker registers and `nvidia-smi` shows **all** GPUs busy
during a proof (for the MPI path, `--report-bindings` should show MPI_NP ranks across all GPUs, not 1).
⚠️ Worker backend wiring (`WORKER_BACKEND=asm|emulator`, `PROVING_KEY`, `ASM_FILE`) is the piece to
confirm on the box from `zisk-worker --help` + what `remote setup` produced — see `start.sh`.

## What you copy from your Mac first
```sh
# from zisk-infra/ on the Mac (using a committed sample block, e.g. the smallest):
./cluster/ship.sh root@<HOST>:<PORT>     # this directory + zisk-runner, one stream, no witnesses
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
