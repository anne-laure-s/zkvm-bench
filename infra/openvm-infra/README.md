# openvm-infra — benchmark OpenVM on mainnet blocks (GPU)

Prove Ethereum mainnet blocks with **OpenVM** on a GPU box, on a fixed block set and box class.
Unlike SP1/ZisK, OpenVM bakes the guest ELF into the prover binary and **mints its own witness on the
box** from an archive RPC (by block number) — so only the harness is shipped, then the box proves
straight from `--block-number`.

> **Multi-GPU, briefly:** open-source OpenVM proves 1 GPU/process; the 16×5090 headline is Axiom's
> *closed* multi-node coordinator. This repo's multi-GPU driver is **`cluster/submit.sh`** (**path ①**):
> one process **per GPU** (`CUDA_VISIBLE_DEVICES`-pinned), each proving a shard of the block's segments
> (`seg_idx % N`), then a single aggregate — a real single-block multi-GPU app-proof latency, with no
> cross-device resource sharing. (An earlier in-process attempt — **path ②**, one process driving N GPUs —
> was removed: it crashes at runtime with VPMM `cudaErrorInvalidResourceHandle`; see the header of
> `patches/apply.sh`.) Full story + the patch: [docs/openvm-multigpu.md](docs/openvm-multigpu.md).

> ⚠️ RTX 5090 = Blackwell → **`CUDA_ARCH=120`** (not 89). `openvm-eth` pins move fast; the vendored
> patch may need re-rebasing (details in [docs/openvm-multigpu.md](docs/openvm-multigpu.md)).

## Experiment — prove one block

Set `REMOTE`/`PORT` on the Mac (the deploy); the `RPC` + block are set **on the box**, where the
witness is minted. Run the Mac step from `infra/openvm-infra/`.

```sh
export REMOTE=<user@host>       # the GPU box
export PORT=<port>              # its SSH port
```

### 1 · Mac — deploy the harness (rsync, not git — it isn't versioned)
```sh
rsync -az -e "ssh -p $PORT" ./ $REMOTE:/workspace/openvm/openvm-infra/
```
> Optional pre-warm: mint the RPC cache (34 blocks) with `scripts/mint-witnesses.sh` and ship it (it
> prints the `rsync` line), or push one block's cache per-prove with `./run prove … RSYNC_CACHE=1` —
> otherwise the box just mints from `RPC_1` on first use.

### 2 · Box — one-time setup
```sh
ssh -p $PORT $REMOTE
export RPC_1=<archive-rpc>       # OpenVM mints the witness here (by block number)
export BLOCK=20000000            # mainnet block to prove
cd /workspace/openvm/openvm-infra
CUDA_ARCH=120 cluster/00-install-once.sh    # clone + patch + build (guest ELF + CUDA binary)
cluster/01-keygen.sh $BLOCK                 # app_pk/agg_pk ONCE (uses RPC_1; excluded from timing)
```

### 3 · Box — prove one block
```sh
NUM_GPUS=8 cluster/submit.sh $BLOCK          # multi-GPU (path ①: one process per GPU) prove-stark
#  -> runs/mg-1-$BLOCK-<ts>/ : timing.txt (workers/aggregate/total) · worker-*.log · aggregate.log
#     · gpu-util.csv · proof.json · env.txt
```

### 4 · Mac — retrieve
```sh
cluster/fetch-runs.sh $REMOTE $PORT          # from infra/openvm-infra/ -> results/
```

### All blocks (the full benchmark set)
`prove-all.sh` proves every cached block in sequence via `submit.sh` (path ①) — resume-able, one run
record each. Run it on the box:
```sh
NUM_GPUS=8 cluster/prove-all.sh
```
Pre-warm the cache first (`scripts/mint-witnesses.sh`, ~34 blocks, ship it) or keep `RPC_1` set so the box mints each.

> Cheap check without a GPU: `./run execute BLOCK=$BLOCK RPC_URL=<archive-rpc>` prints the OpenVM
> cycle count on the Mac.

## Layout

| Path | What |
|------|------|
| `run` | dispatcher — `build-elf` · `build-bin` · `gen-input` · `execute` · `prove` · `verify` |
| `openvm-runner` | `openvm-reth-benchmark` wrapper; emits `report.json` (timings, proof_bytes, cycles) |
| `cluster/` | on-box multi-GPU proving — **one process per GPU** (path ①, `submit.sh`); no coordinator |
| `patches/` | the multi-GPU patch (`apply.sh`) + its manifest |
| `guests/openvm-reth/guest.sh` | builds the benchmark binary (guest ELF `include_bytes!`'d in) |
| `docs/` | multi-GPU design + patch, benchmark results |

The witness is minted by **block number** from `RPC_1` (same var as RSP) — no separate witness file to ship.

## What to measure
STARK proof with aggregation (`--mode prove-stark`, the analog of SP1/ZisK `prove-compressed`); no
on-chain EVM wrap by default. Cycle count comes from `./run execute`. The decisive cross-zkVM number
is the **single-block wall-clock latency** (multi-GPU, path ①).

## Details → docs/
- [docs/openvm-multigpu.md](docs/openvm-multigpu.md) — the multi-GPU reality, design & the vendored patch, tuning, troubleshooting.
- [docs/openvm-benchmark.md](docs/openvm-benchmark.md) — results.
- [cluster/README.md](cluster/) — on-box run modes (patched · baseline · throughput) + validation.
- [patches/README.md](patches/) — what the patch edits.
- [../../cli/report-schema.md](../../cli/report-schema.md) — the shared `report.json` contract every runner emits.
