# zisk-infra — benchmark ZisK on mainnet blocks (GPU)

Prove Ethereum mainnet blocks with **ZisK** on a GPU box, on a fixed block set and box class.
Off-box posture: the **Mac builds** the ELF + witness (a committed offline sample — no RPC), the
**box only proves**. Multi-GPU here is a **single-process worker** (`NO_MPI=1`) that drives all GPUs —
this is what the 16×5090 benchmark ran. ZisK's *official* multi-GPU path is MPI (`mpirun`, ~2 GPUs/rank),
but it **segfaults on unprivileged vast.ai containers** (NUMA membind), so we use the single-process path.

**Which of the three ZisK documents you want.** This one is the **runbook**: from nothing to a proof,
steps 0–5, Mac and box. [`cluster/README.md`](cluster/) is the box-side **reference** — what `up.sh`
drives, tuning, ports, first failures — read when a step here misbehaves.
[`docs/runbook-vastai.md`](docs/runbook-vastai.md) is a **campaign**: three tracks by GPU count, each
with the question it settles, its box time and its deliverable. Start here.

> ⚠️ **The ZisK version is pinned in `00-install-once.sh`** (`ZISK_VER`, default **1.3.1-alpha**).
> `ziskup` otherwise installs *latest*, and the version decides the proving-key tarball and the
> const-trees. All-GPU proving runs the **stock** worker: from 1.1.0-alpha on it binds the owning GPU
> at every `count_and_plan` entry point, so no worker binary is patched or shipped here.
> `00-install-once.sh` refuses a box still carrying an old hand-patched worker under the stock name,
> by BuildID — `--version` reports the same string either way.

> ⚠️ ZisK is **v1.3.1-alpha**; commands are verified against the installed CLI. Building `input-gen` /
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

### 0 · Box — is it usable, before you ship anything
```sh
scp -P $PORT cluster/tests/t0-gonogo.sh $REMOTE:~/ && ssh -p $PORT $REMOTE 'bash ~/t0-gonogo.sh'
```
Two seconds, needs nothing installed. A listing does not show how the GPUs split across NUMA nodes
nor whether the box can lock memory, and both decide what runs here. The full box-check set is
[cluster/tests/](cluster/tests/); `up.sh` re-runs `t3-topo.sh` by itself, but only when it is about
to install.

### 1 · Mac — build the ELF + witness
```sh
./run build-elf GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client
./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client BLOCK=$BLOCK
#   -> ../../guests/zisk-reth/zisk-reth.elf  +  ../../guests/zisk-reth/inputs/1-$BLOCK.{bin,hints}
#   (gen-input deduces the committed sample from $BLOCK; pass SAMPLE=<path> to override)
```

### 2 · Mac — ship the harness, ELF + witness
```sh
./cluster/ship.sh $REMOTE:$PORT          # cluster/ + zisk-runner, one stream, ~86 KB on the wire
scp -P $PORT ../../guests/zisk-reth/zisk-reth.elf $REMOTE:~/zisk-reth.elf
scp -P $PORT ../../guests/zisk-reth/inputs/1-$BLOCK.bin ../../guests/zisk-reth/inputs/1-$BLOCK.hints $REMOTE:~/
```
`ship.sh` sends one compressed stream instead of 65 files, because at 146 ms RTT `scp` pays ~38 ms
per file whatever its size, and it drops the 155 MB of witnesses under `tests/*/inputs*` that
`prepare-inputs.sh` rebuilds on the box — 276 KB instead of 156 MB, under two seconds instead of
four minutes. It re-ships nothing when the box already holds the same content, verifies by
re-hashing what landed, and lists (never deletes) what the box has and the Mac does not
(`EXTRA=<file> ./cluster/ship.sh …` adds one of your own to the stream).

### 3 · Box — bring the cluster up, from whatever state the box is in
```sh
ssh -p $PORT $REMOTE           # you're now on the box (a fresh shell)
export BLOCK=24626900          # set once here too — Mac vars don't cross the ssh
cd ~/zisk-infra/cluster
bash up.sh                     # the one command: it decides what is missing and returns only once a
                               #   worker is REGISTERED. Safe to run every time.
```
`up.sh` is the bring-up, not a wrapper around a fixed sequence. It probes the box (binaries, key,
port, daemons, VRAM, registrations), then does only what that state requires:

| it finds | it does |
|---|---|
| no `cargo-zisk`, no key, or a key under `MIN_KEY_GB` | gates on d2h (`tests/t3-topo.sh`), then runs `./00-install-once.sh` — 15–60 min, box-only |
| a key present | `check-setup` — authoritative, idempotent, writes only the const-trees that are missing |
| a stale coordinator, a zombie worker, held VRAM or a held port | tears them down by process, port **and** card, then waits for the box to let go |
| nothing running | rotates the logs, runs `./start.sh`, waits for a registration, prints the box record (d2h, power limit, streams) |
| all three of port + registration + VRAM agreeing | nothing — "already healthy" |

`FORCE_RESTART=1` tears down first whatever the verdict; `SKIP_PROBE=1` drops the closing box record.
`00-install-once.sh` and `start.sh` stay the pieces it drives, and both are still runnable on their
own: the install (`ZISK_VER=<x.y.z>` for another release) and the launch (`USE_MPI=1` for the MPI
path). `start.sh` only launches — nothing in it installs.

From the Mac, the RTP drives the same path: `PROVER=<box> ./rtp-up --real --bootstrap` ships
`cluster/` to the prover and runs `up.sh` there, and refuses to start rather than installing for an
hour unannounced if you leave `--bootstrap` off ([infra/monad-witness/](../monad-witness/README.md)).

### 4 · Box — register the ELF, prove
```sh
cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000   # once per ELF
./submit.sh ~/zisk-reth.elf ~/1-$BLOCK.bin ~/1-$BLOCK.hints                   # -> runs/<tag>-<ts>/ (report.json, prove.log, …)
./stop.sh
```
The setup cache is keyed on (program, with_hints) and lives in the **worker**, so anything that
restarts the worker drops it and the per-ELF `remote setup` has to be re-run.

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
> (the coordinator must be up — step 3). Full cluster bring-up + tuning: [cluster/README.md](cluster/).

## Layout

| Path | What |
|------|------|
| `run` | dispatcher — `build-elf` · `gen-input` · `execute` · `prove` · `verify` |
| `zisk-runner` | `cargo-zisk`/`ziskemu` wrapper; emits `report.json` (timings, proof_bytes, steps) |
| `cluster/` | on-box multi-GPU proving (gRPC coordinator + worker) |
| `cluster/up.sh` | the bring-up: probe → install/verify/tear down as needed → start → wait for a registration |
| `cluster/ship.sh` | push `cluster/` + `zisk-runner` to a box in one stream, witnesses excluded |
| `cluster/tests/` | box checks — `t0`…`t6`, `summary.sh` (see below) |
| `guests/zisk-reth/guest.sh` | build ELF + generate witness (`.bin` **+** `.hints`) from `zisk-eth-client` |
| `docs/` | design & build prereqs, benchmark results, bring-up report |

Guest ELFs + inputs live in the top-level [`../../guests/`](../../guests/). A witness is `<tag>.bin`
**plus** `<tag>.hints` (`./run` finds the `.hints` sibling automatically).

## Box checks — [cluster/tests/](cluster/tests/)

A rented box is not a known quantity: two hosts identical on every advertised field differed **2.37×**
on device-to-host bandwidth, and the slow one took up to **2.82×** longer on the largest block. None of
that shows in a listing, so it gets measured here. `cluster/` carries `tests/` with it, so step 2 ships
them; `t0` is the exception — it runs before any transfer, off a single scp'd file.

| | script | cost | decides |
|---|---|---|---|
| 0 | `t0-gonogo.sh` | ~2 s, **nothing installed** | is this box usable at all, and how do its GPUs split across NUMA nodes |
| 3 | `t3-topo.sh` | ~2 min, read-only | host↔device bandwidth, and whether the box can lock memory. `up.sh` runs it as the pre-install d2h gate |
| 1 | `t1-numa.sh` | 35–45 min | what the `[ALARM] 8/16 NUMA-local GPUs` actually costs |
| 2 | `t2-tune.sh` | 35–45 min | `--compute-capacity` / `--max-streams` against their defaults |
| 4 | `t4-memlock.sh` | 20–30 min | whether stripping `MAP_LOCKED` is free, as `nolock.c` claims |
| 5 | `t5-mpi.sh` | 45–60 min | upstream's NUMA-bound multi-rank layout against our single process |
| 6 | `t6-duty.sh` | minutes, on the prover | is the card computing or waiting, and what is it allowed to draw |

`summary.sh` turns a `results.csv` from t1…t5 into the two comparison tables. Order, prerequisites and
what each one has already answered: [cluster/tests/README.md](cluster/tests/README.md). Renting and
provisioning a box end to end, including which t0 verdict rejects one: [docs/runbook-vastai.md](docs/runbook-vastai.md).

## What to measure
Recursive **compressed** STARK (the analog of SP1/OpenVM `prove-compressed`); no on-chain wrap
(`--plonk`). Step count (ZisK's work-unit) comes from `./run execute`.

## Details → docs/
- [docs/design.md](docs/design.md) — macOS build prereqs, ZisK CLI facts (v1.3.1-alpha), witness generation (sample vs RPC), tuning knobs, Mac-driven proving.
- [docs/zisk-benchmark.md](docs/zisk-benchmark.md) — results · [docs/zisk-bringup-report.md](docs/zisk-bringup-report.md) — bring-up report.
- [docs/runbook-vastai.md](docs/runbook-vastai.md) — renting a box: what to require, the boot-time go/no-go, the install's expected output.
- [cluster/README.md](cluster/) — cluster bring-up, the MPI path + tuning · [cluster/tests/README.md](cluster/tests/README.md) — the box checks.
- [../../cli/report-schema.md](../../cli/report-schema.md) — the shared `report.json` contract every runner emits.
