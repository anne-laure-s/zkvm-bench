# zisk-infra — design & internals

Reference behind the [README](../README.md) runbook: macOS build prerequisites, the ZisK CLI facts
the harness relies on, how witnesses are generated, and the tuning knobs.

## Components

| Piece | Role |
|-------|------|
| `run` | producer/consumer dispatcher (build-elf · gen-input · execute · prove · verify) |
| `zisk-runner` | shell wrapper over `cargo-zisk`/`ziskemu`; emits `report.json` (timings, proof_bytes, steps) |
| `guests/zisk-reth/guest.sh` | builds the ELF + generates the witness from a `zisk-eth-client` checkout (`stateless-validator-reth`) |
| `cluster/` | on-box multi-GPU proving (gRPC coordinator + worker; `--gpu --max-streams`) |
| witness | `<tag>.bin` **+ `<tag>.hints`** (ZisK needs a separate hints artifact) |
| per-ELF setup | one-off `cargo-zisk setup` on the box (not in per-block timing) |

Inputs are zkVM-specific (not byte-compatible across stacks) — comparison is at the **block** level.

## macOS (Apple Silicon) build prerequisites — for `input-gen`/`hints-gen`

`input-gen`/`hints-gen` transitively compile the ZisK STARK prover C++ lib (`host → zisk-sdk →
pil2-stark`). On a Mac with **Command Line Tools only** (no full Xcode.app), two fixes are required:

1. Homebrew libs the prover links/includes:
   ```sh
   brew install gmp libomp libsodium open-mpi nlohmann-json
   ```
   (`cargo-zisk` itself also needs `gmp libomp libsodium open-mpi` — see `otool -L ~/.zisk/bin/*`.)
2. The pil2-stark Makefiles hardcode an Xcode.app SDK path that doesn't exist on CLT-only machines.
   Point it at the CLT SDK (re-apply after any `cargo update` of pil2-proofman):
   ```sh
   PMROOT="$(echo ~/.cargo/git/checkouts/pil2-proofman-*/*)"
   BAD='/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
   for f in pil2-stark/Makefile pil2-stark/src/goldilocks/Makefile setup/circom/Makefile; do
     [ -f "$PMROOT/$f" ] && sed -i '' "s#$BAD#\$(shell xcrun --show-sdk-path)#g" "$PMROOT/$f"
   done
   ```
Then `cargo build --release -p input-gen -p hints-gen` should succeed (~2-3 min). (`cargo-zisk build`
of the guest and `ziskemu` need none of this — they don't pull the prover.)

## Witness generation: offline sample vs RPC

ZisK's own `input-gen` builds the block witness by fetching state via **`debug_executionWitness`**
(the reth/geth *debug* namespace), which standard archive RPCs (Alchemy/Infura) don't expose. So for
an offline benchmark, `guest.sh` reuses the **committed sample inputs** that upstream `zisk-eth-client`
ships under `bin/guests/stateless-validator-reth/inputs/` (~12 mainnet blocks). Each is a
pre-generated stateless block input for the reth guest; the filename encodes its facts:

```
mainnet_24626900_221_16_zec_reth.bin
   │        │      │  │  │    └ client (reth)
   │        │      │  │  └ zec = zisk-eth-client marker
   │        │      │  └ block gas (Mgas)
   │        │      └ tx count
   │        └ block number
   └ chain
```

In **SAMPLE mode** (`SAMPLE=<path>`), `guest.sh` copies that `.bin` as the input and runs `hints-gen`
natively on it to produce the sibling `.hints` — **no RPC**. You usually don't pass the path: with
neither `SAMPLE` nor `RPC_URL` set, `gen-input` **deduces the sample from `BLOCK`** by globbing that
dir for `*_<BLOCK>_*_zec_<client>.bin`. If no committed sample matches, it errors and lists the
available blocks (so pick one of those, or switch to the RPC path). Pass `SAMPLE=<path>` to override.

The **RPC path** (`BLOCK=<n> RPC_URL=…`) runs `input-gen` + `hints-gen` for an arbitrary block, but
needs a node exposing the debug namespace:

```sh
./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client BLOCK=<n> RPC_URL=http://<node>:8545
```

## ZisK is v1.0.0-alpha — CLI facts the harness relies on

All `cargo-zisk`/`ziskemu`/`zisk-coordinator`/`zisk-worker` invocations were checked against the
installed v1.0.0-alpha `--help`. Baked-in facts: local `prove --hints` **requires `--asm`** (so
`setup --asm --hints`); `remote prove` sends hints inline (no `--asm`) via `--coordinator` (default
`:7000`); `-o` is the **proof file**; coordinator binds `7000` (client) / `50051` (worker) / `9090`
(metrics). **Multi-GPU default = single-process, all GPUs** (`NO_MPI`): the patched worker lets one
process drive every GPU (proofman assigns all GPUs to the single rank), which is what `cluster/start.sh`
runs by default. MPI (`mpirun` → `MPI_NP` ranks, ~2 GPUs/rank via `mpi_params.sh`) is the official
alternative path — opt in with `USE_MPI=1`.

⚠️ **GPU-only flags are hidden on a CPU build's `--help`** (e.g. the Mac). The worker's `-g/--gpu`
exists only on a GPU build; likewise re-check `cargo-zisk prove --help` on the box for a GPU flag (add
via `ZISK_PROVE_EXTRA_FLAGS` if needed). To confirm on the box: worker backend wiring
(`--emulator`/`--asm`, `-k <proving-key>`) + that `mpirun` spawns one rank per ~2 GPUs (`logs/worker.log`).
Env knobs: `ZISK_PROVE_BACKEND`, `ZISK_SRC`, `WORKER_BACKEND` (default `asm`), `PROVING_KEY`,
`USE_MPI` (opt into MPI; single-process all-GPU is the default), `NO_MPI` (force single-process),
`API_PORT`/`CLUSTER_PORT`, `MAX_STREAMS`, `CUDA_ARCHS`.

## Drive proving from the Mac (ssh)

Instead of sshing into the box, `./run prove` uploads the ELF + input + hints and runs `zisk-runner`
there (backend = remote → the running coordinator, which must already be up):

```sh
./run prove  ELF=../../guests/zisk-reth/zisk-reth.elf INPUT=../../guests/zisk-reth/inputs/1-<block>.bin REMOTE=root@<HOST> PORT=<PORT>
./run verify ELF=../../guests/zisk-reth/zisk-reth.elf INPUT=../../guests/zisk-reth/inputs/1-<block>.bin PROOF=results/zisk-reth/1-<block>/…/proof.bin
```
