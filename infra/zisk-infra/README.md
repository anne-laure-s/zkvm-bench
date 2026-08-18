# zisk-infra — benchmark ZisK on mainnet blocks (GPU)

Prove Ethereum mainnet blocks with **ZisK** on a GPU box, on a fixed block set and box class.
Off-box posture: the **Mac builds** the ELF + witness (a committed offline sample — no RPC), the
**box only proves**. Multi-GPU here is a **single-process worker** (`NO_MPI=1`) that drives all GPUs —
this is what the 16×5090 benchmark ran. ZisK's *official* multi-GPU path is MPI (`mpirun`, ~2 GPUs/rank),
but it **segfaults on unprivileged vast.ai containers** (NUMA membind), so we use the single-process path.

> ⚠️ **The ZisK version is pinned in `00-install-once.sh`** (`ZISK_VER`, default **1.1.0-alpha**).
> `ziskup` otherwise installs *latest*, and the version decides the proving-key tarball, the
> const-trees and which `zisk-worker` binary is correct. All-GPU proving runs the **stock** worker:
> 1.1.0-alpha binds the owning GPU at every `count_and_plan` entry point. The committed
> `zisk-worker.patched` is a **1.0.0-alpha** build, installed only when `ZISK_VER=1.0.0-alpha`
> reproduces [results/zisk-reth-16gpu-clean](results/zisk-reth-16gpu-clean); pairing it with a newer
> coordinator/key does not work.

> ⚠️ ZisK is **v1.1.0-alpha**; commands are verified against the installed CLI. Building `input-gen` /
> `hints-gen` on macOS needs a couple of fixes — see [docs/design.md](docs/design.md).

## Setup (once, on the Mac)
```sh
curl https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/ziskup/install.sh | bash
git clone https://github.com/0xPolygonHermez/zisk-eth-client ../../vendor/zisk-eth-client
```

## Experiment — prove one block

Set these once (on the Mac); everything below is then copy-paste. The benchmark replays a **committed
offline sample** — `BLOCK` picks one and `gen-input` finds its `.bin` by number. Run from `infra/zisk-infra/`.

```sh
export REMOTE=<user@host>       # the GPU box
export PORT=<port>              # its SSH port
export BLOCK=24626900           # a committed offline sample block (list + why in docs/design.md);
                                #   any other block needs the RPC path (a debug_executionWitness node)
```

### 1 · Mac — build the ELF + witness
```sh
./run build-elf GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client
./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client BLOCK=$BLOCK
#   -> ../../guests/zisk-reth/zisk-reth.elf  +  ../../guests/zisk-reth/inputs/1-$BLOCK.{bin,hints}
#   (gen-input deduces the committed sample from $BLOCK; pass SAMPLE=<path> to override)
```

### 2 · Mac — ship the harness, ELF + witness
```sh
# nolock.c rides along in cluster/. Add zisk-worker.patched to the scp only for a
# ZISK_VER=1.0.0-alpha box (that release needs the patched worker for multi-GPU).
scp -P $PORT -r cluster zisk-runner $REMOTE:~/zisk-infra/
scp -P $PORT ../../guests/zisk-reth/zisk-reth.elf $REMOTE:~/zisk-reth.elf
scp -P $PORT ../../guests/zisk-reth/inputs/1-$BLOCK.bin ../../guests/zisk-reth/inputs/1-$BLOCK.hints $REMOTE:~/
```

### 3 · Box — one-time setup (persists across stop/start)
```sh
ssh -p $PORT $REMOTE           # you're now on the box (a fresh shell)
export BLOCK=24626900          # set once here too — Mac vars don't cross the ssh
cd ~/zisk-infra/cluster
./00-install-once.sh           # ziskup 1.1.0-alpha --provingkey (GPU) + memlock fix
                               #   ZISK_VER=<x.y.z> installs another release
```

### 4 · Box — start the cluster, register the ELF, prove
```sh
./start.sh                                                                    # coordinator + single-process worker, ALL GPUs (default: asm backend + nolock auto; USE_MPI=1 for the MPI path)
cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000   # once per ELF
./submit.sh ~/zisk-reth.elf ~/1-$BLOCK.bin ~/1-$BLOCK.hints                   # -> runs/<tag>-<ts>/ (report.json, prove.log, …)
./stop.sh
```

### 5 · Mac — retrieve
```sh
./cluster/fetch-runs.sh $REMOTE:$PORT     # -> results/
```

### All blocks (the full benchmark set)
Build + ship every committed-sample witness, then benchmark them with the cluster up (warm-up + N timed passes):
```sh
# Mac — build all committed-sample witnesses, then ship the set:
for s in ../../vendor/zisk-eth-client/bin/guests/stateless-validator-reth/inputs/*_zec_reth.bin; do
  ./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client SAMPLE="$s" || true
done
scp -P $PORT ../../guests/zisk-reth/inputs/1-*.{bin,hints} $REMOTE:~/
# box (coordinator up, step 4) — prove all, N timed passes -> ~/bench/timings.csv:
PASSES=3 WARMUPS=2 ./clean-bench.sh
```
(`|| true`: block `25229955` crashes `hints-gen` — the Osaka `p256verify` precompile — so it's skipped.)

> Or drive proving from the Mac without sshing in: `./run prove ELF=… INPUT=… REMOTE=$REMOTE PORT=$PORT`
> (the coordinator must be up). Full cluster bring-up + tuning: [cluster/README.md](cluster/).

## Layout

| Path | What |
|------|------|
| `run` | dispatcher — `build-elf` · `gen-input` · `execute` · `prove` · `verify` |
| `zisk-runner` | `cargo-zisk`/`ziskemu` wrapper; emits `report.json` (timings, proof_bytes, steps) |
| `cluster/` | on-box multi-GPU proving (MPI: gRPC coordinator + `mpirun` worker) |
| `guests/zisk-reth/guest.sh` | build ELF + generate witness (`.bin` **+** `.hints`) from `zisk-eth-client` |
| `docs/` | design & build prereqs, benchmark results, bring-up report |

Guest ELFs + inputs live in the top-level [`../../guests/`](../../guests/). A witness is `<tag>.bin`
**plus** `<tag>.hints` (`./run` finds the `.hints` sibling automatically).

## What to measure
Recursive **compressed** STARK (the analog of SP1/OpenVM `prove-compressed`); no on-chain wrap
(`--plonk`). Step count (ZisK's work-unit) comes from `./run execute`.

## Details → docs/
- [docs/design.md](docs/design.md) — macOS build prereqs, ZisK CLI facts (v1.1.0-alpha), witness generation (sample vs RPC), tuning knobs, Mac-driven proving.
- [docs/zisk-benchmark.md](docs/zisk-benchmark.md) — results · [docs/zisk-bringup-report.md](docs/zisk-bringup-report.md) — bring-up report.
- [cluster/README.md](cluster/) — full MPI cluster bring-up + tuning.
- [../../cli/report-schema.md](../../cli/report-schema.md) — the shared `report.json` contract every runner emits.
