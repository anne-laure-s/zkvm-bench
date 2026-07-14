# openvm-infra/cluster — running multi-GPU OpenVM on the box

**OpenVM has no daemons** (unlike a gRPC coordinator/worker or redis-cluster design).
Self-hosted multi-GPU here (**path ①**) is **one transient `openvm-reth-benchmark` process per GPU**
(`CUDA_VISIBLE_DEVICES`-pinned), each proving a shard of the block's segments, then one aggregate —
enabled by the vendored patch in `../patches/` (see `../docs/openvm-multigpu.md`). So there is no
`start.sh`: you just `submit.sh` a block. (The earlier in-process **path ②** — one process driving N
GPUs — was removed: it crashes at runtime with VPMM `cudaErrorInvalidResourceHandle`.)

## One-time
```sh
CUDA_ARCH=120 ./00-install-once.sh           # RTX 5090 = Blackwell. clones+patches+builds+guest ELF
RPC_1=<archive-rpc> ./01-keygen.sh 20000000  # generate app_pk/agg_pk ONCE (excludes keygen from timing)
```
`00-install-once` vendors `openvm` + `stark-backend` at the revs `openvm-eth` pins, applies the
multi-GPU patch via `cargo [patch]`, builds the guest ELF (`cargo openvm build`, pinned to the host
rev) + the host binary with `--features cuda`, and writes `box-env.sh`. Re-run after a rev drift.

**`01-keygen.sh` is not optional for meaningful numbers** — it is the OpenVM analog of SP1 `setup` /
`cargo-zisk setup`. Without it, keygen runs *inside* the timed prove and inflates `prove_secs`.
`submit.sh` auto-detects the keys and passes `--app-pk-path/--agg-pk-path`.

## Per block  (needs `RPC_1` set, or the block already cached)
```sh
NUM_GPUS=8 ./submit.sh 20000000              # multi-GPU prove-stark of block 20000000 (path ①)
```
`submit.sh` launches one worker per GPU (`CUDA_VISIBLE_DEVICES`-pinned, `--skip-comparison`
so no CPU re-execution in the timed region), shards the block's segments (`seg_idx % N`), then runs a
single `aggregate`. It samples `nvidia-smi` to `gpu-util.csv` (proof the fan-out is real + VRAM peak).
Each run is saved (never overwritten) under `runs/mg-<chain>-<block>-<ts>/`:
`timing.txt` (`workers_secs` / `aggregate_secs` / `total_secs` / `num_segments`) · `worker-*.log` ·
`aggregate.log` · `gpu-util.csv` · `proof.json` · `env.txt`.

## All blocks (the full benchmark set)
`prove-all.sh` sweeps every cached block through `submit.sh` (path ①) — resume-able, one run record each:
```sh
NUM_GPUS=8 ./prove-all.sh                          # all cached blocks
BLOCKS="20000000 20500000" ./prove-all.sh          # a specific set
```

## Validate the multi-GPU patch actually uses all GPUs
While a `NUM_GPUS=8` prove runs, in another shell:
```sh
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv -l 1
```
All `NUM_GPUS` devices should show utilization during the app-proof phase (the long middle
of the run). If only GPU 0 is busy, the patch's device-pinning didn't take — see the
troubleshooting section of `../docs/openvm-multigpu.md`.

## Stop / cleanup
```sh
./stop.sh            # kill a stray prove process, free GPUs
```
