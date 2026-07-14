# sp1-infra — benchmark SP1 on mainnet blocks (GPU)

Prove Ethereum mainnet blocks with **SP1** on a GPU box, on a fixed block set and box class.
Off-box posture: the **Mac builds** the guest ELF + the block witness, the **box only proves**. The
benchmark path is **16-GPU proving on one box** via [`cluster-native/`](cluster-native/).

> ⚠️ **Box requirement — CUDA ≥ 13.0 (driver ≥ 580).** The v2.4.3 cluster binaries link the CUDA 13.0
> runtime (no forward-compat). Pick a box whose `nvidia-smi` *CUDA Version* is ≥ 13.0 (ours ran 13.2 / 595).

## Experiment — prove one block

Set these once (on the Mac); everything below is then pure copy-paste. `RPC` must serve
`eth_getProof` — a free Alchemy key works. Run from `infra/sp1-infra/`.

```sh
export REMOTE=<user@host>       # the GPU box
export PORT=<port>              # its SSH port
export RPC=https://eth-mainnet.g.alchemy.com/v2/<KEY>
export BLOCK=<block number>     # mainnet block to prove
```

### 1 · Mac — build the RSP guest + the block witness
```sh
./run build-elf GUEST=rsp RSP_DIR=../../vendor/rsp
./run gen-input GUEST=rsp RSP_DIR=../../vendor/rsp BLOCK=$BLOCK RPC_URL=$RPC
#   -> ../../guests/rsp/rsp.elf  +  ../../guests/rsp/inputs/1-$BLOCK.bin
```

### 2 · Mac — ship the harness, runner, ELF + witness
```sh
scp -P $PORT -r cluster-native sp1-runner $REMOTE:~/
scp -P $PORT ../../guests/rsp/rsp.elf ../../guests/rsp/inputs/1-$BLOCK.bin $REMOTE:~/
```

### 3 · Box — one-time setup (persists across stop/start)
```sh
ssh -p $PORT $REMOTE          # you're now on the box (a fresh shell)
export BLOCK=<block number>   # set once here too — Mac vars don't cross the ssh
cd ~/cluster-native
./00-install-once.sh          # redis, postgres, skopeo, zstd
./02-fetch-binaries.sh        # pull v2.4.3 images, extract binaries   (fails fast if CUDA < 13)
./04-build-runner.sh          # build sp1-runner as the network client
```

### 4 · Box — start the cluster + prove
```sh
./boot.sh                     # redis + postgres
NUM_GPUS=16 ./03-start.sh     # coordinator + gateway + cpu/gpu nodes
                              #   (validate wiring with NUM_GPUS=1 first — see cluster-native/README)
./submit.sh ~/rsp.elf ~/1-$BLOCK.bin
#  -> prints prove_secs; saves runs/1-$BLOCK-<ts>/ : proof.bin · report.json · prove.log · env.txt
```

### 5 · Mac — retrieve + stop the box (GPU time is the cost)
```sh
./cluster-native/fetch-runs.sh $REMOTE:$PORT   # from infra/sp1-infra/ -> results/
```

### All blocks (the full benchmark set)
Ship every RSP witness once, then batch them — each block gets its own run record, one at a time on the 16 GPUs:
```sh
scp -P $PORT ../../guests/rsp/inputs/*.bin $REMOTE:~/witnesses/    # Mac: all witnesses
./submit-all.sh ~/rsp.elf ~/witnesses                             # box: prove every one -> runs/<tag>-<ts>/
```

> **Lighter path (no cluster):** `./run prove ELF=… INPUT=… REMOTE=… PORT=…` proves a single block
> directly with `sp1-runner`. **Validate locally first (free):** `./run execute ELF=… INPUT=…` prints
> the cycle count with no GPU. Both are in [`docs/design.md`](docs/design.md). Full cluster bring-up
> (2×8 GPU, tuning, troubleshooting) is in [`cluster-native/README.md`](cluster-native/).

## Layout

| Path | What |
|------|------|
| `run` | guest-agnostic dispatcher — `build-elf` · `gen-input` · `execute` · `prove` · `verify` |
| `sp1-runner/` | the SP1 runner (Rust): local CPU · direct CUDA · cluster network client |
| `cluster-native/` | 16-GPU sp1-cluster run natively on the box (the benchmark path) |
| `guests/<name>/guest.sh` | per-guest recipe (`rsp`, `fibonacci`): build ELF / gen input / decode PV |
| `docs/` | design, benchmark results, versions |

Guest ELFs + inputs live in the top-level [`../../guests/`](../../guests/) (shared, prover-agnostic).

## What to measure
Recursive **compressed** STARK (`prove-compressed`, the default `MODE` — the Ethproofs standard); no
on-chain wrap. Cycle count comes from `./run execute`.

## Details → docs/

- [docs/design.md](docs/design.md) — how it works: the runner (modes, prover selection), the guest
  model + `guest.sh` contract, input layering, `./run prove` + the run record, verification & trust,
  report format, RSP, updating SP1.
- [docs/sp1-benchmark-synthesis.md](docs/sp1-benchmark-synthesis.md) · [docs/sp1-benchmark-16gpu-comparison.md](docs/sp1-benchmark-16gpu-comparison.md) — results (8× / 16× RTX 5090).
- [docs/versions.md](docs/versions.md) — SP1 / cluster / RSP version matrix · [docs/guest-profile.md](docs/guest-profile.md) · [docs/gas-distribution.md](docs/gas-distribution.md) · [docs/optimisation-report.md](docs/optimisation-report.md).
- [../../cli/report-schema.md](../../cli/report-schema.md) — the shared `report.json` contract every runner emits.
