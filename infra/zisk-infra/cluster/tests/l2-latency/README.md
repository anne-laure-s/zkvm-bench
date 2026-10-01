# l2-latency — how long a proof of one L2 block takes, by block size

The question this answers: **at 50 TPS, what is the smallest latency a ZisK prover can give the
monad L2, and on how many GPUs?** Every figure measured so far was COST under `ziskemu`, which
models prover work but not time, on blocks of 21 to 500 transactions. zkvm-bench's GPU runs give
`prove_secs = 5.33 + 0.159 x Msteps` (one RTX 5090, ZisK 1.1, mainnet blocks of 18-289 Msteps), and
an L2 block at 50 TPS is 0.4-2 Msteps: if that 5.33 s holds down there, it is the latency, and the
block's content barely matters. This measures it instead of extrapolating it.

## What it runs

| set | blocks | why |
|---|---|---|
| sweep | payouts' mix (transfers, a withdrawal in twenty) at 1, 10, 25, 50, 100, 250, 500, 1,000, 2,000 and 5,000 transactions, three blocks each | the shape of `secs(Msteps)`, from the fixed cost to the range zkvm-bench measured |
| presets | two blocks each of `wholesale` (21 tx), `wholesale-cbdc` (21), `worker-payouts` (130), `payouts` (500) | the design document's mixes, token contracts included |
| arms | every L2 block twice: the encrypting guest, and the same chain built with the plaintext cipher suite | what the encryption costs in time, block for block |
| JUMPDEST | every L2 block a third time, on the encrypting guest built with the JUMPDEST precompile (`MONAD_ZKVM_JUMPDEST_SOFTWARE=OFF`) | what the precompile's instance costs a proof: an L2 build analyses JUMPDESTs in software by default, one instance fewer for about 9,600 more steps |
| Keccak-f | the L2 blocks of up to 250 transactions a fourth time, on the encrypting guest built with every Keccak-f in software (`MONAD_ZKVM_KECCAKF_SOFTWARE=ON`, the memo off) | whether a block that plans no Keccakf instance -- about a fifth of a small block's plan by area -- proves faster: at 4,138 steps a permutation, the plan's area against the default's is 0.79x on transfer blocks of up to 25 transactions, 0.85x at 50, 1.20x at 100 and 1.62x at 250, and larger blocks would only time the overflow |
| mainnet | blocks 25815195, 25815036 and 25815092 of `r10zisk-rtp` (p10, p50, p90: 27-76 Msteps), on the mainnet guest | ties this box to zkvm-bench's mainnet fits |

The L2 corpora come from `monad-zkvm-corpus-gen` (monad, `al/zkvm-l2`): the L2's own chain from
genesis 0, 256 warm-up blocks, witnesses carrying only the ancestor headers their block reads. The
ELFs are dev builds with the six levers the official profile forces, built with ZisK 1.3.1-alpha's
own toolchain, the L2 ones with the L2's default of JUMPDESTs in software but for the third arm's,
and the fourth arm's with every Keccak-f in software; `inputs/provenance.txt` has their hashes.

Each prove is timed on the client's wall clock around `cargo-zisk remote prove`, submission to
proof on disk, against a warm worker: what a sequencer waiting on a proof would see. Setup runs
before every prove, outside the clock, as in `tests/paired`. The proofs are STARK (VADCOP final)
proofs, timed and verified as they are: there is no SNARK wrap in this design, so none is timed and
no PLONK key is installed.

## Running it

On the Mac, once (the corpora are already generated; see `prepare-inputs.py --help`):

```sh
python3 prepare-inputs.py --elf-l2 <L2 ELF> --elf-control <control ELF> --elf-mainnet <mainnet ELF> \
    [--elf-l2-precompile <L2 ELF, JUMPDEST precompile>] [--elf-l2-keccak-sw <L2 ELF, Keccak-f in software>] \
    --l2 <L2 corpora> --control <control corpora> \
    --mainnet <zkvm-bench>/guests/monad/gen/r10zisk-rtp-25815000-25815199-cb7b6b1ae/witnesses
ZISK_PUBLICS_BIN=<zisk-publics built for glibc 2.35> bash make-bundle.sh
```

On the box — any NVIDIA box, fresh or not, as root, with 80 GB of disk for the proving key:

```sh
tar xzf l2-latency-bundle.tar.gz && bash zisk-infra/cluster/tests/l2-latency/run.sh
```

That is the one command. It detaches, prints the `tail -f` to follow it, installs ZisK
1.3.1-alpha if the box does not have it (15-60 min, most of it the key's constant trees), and ends
with `summary.md` printed and the run packed into `~/l2-latency-<stamp>.tar.gz`. A fresh 1-GPU box:
about an hour of install, then roughly 40 min of proofs.

Knobs: `PASSES=2`, `WARMUPS=1`, `GPU_SETS="1 all"` to time a one-GPU worker beside the all-GPU
one, `ONLY=<regex on input ids>`.

## What comes back

| file | what |
|---|---|
| `summary.md` | the tables: seconds per block size on every arm, the fit `fixed + slope x Msteps` per arm, the presets, the mainnet blocks against zkvm-bench's 1.1 fit, the longest worker phases, and the sizing table at 50 TPS — for each block interval, the proof time, the provers it takes to keep up and the latency |
| `stark-<set>/timings.csv` | every prove: pass, input, arm, seconds, return code |
| `stark-<set>/phases.csv` | every `<<< PHASE (N ms)` span the worker logged during each prove |
| `precheck.csv` | the ziskemu replay of every input before any proving |
| `publics.csv` | every kept proof, verified by zisk-publics and checked to commit to its block |
| `*/gpu.csv`, `*/metrics/` | nvidia-smi every 500 ms while proving; the coordinator's Prometheus metrics after each prove |

## Read the box before reading the numbers

zkvm-bench measured the same guest on the same blocks 3.2x faster on one RTX 5090 box than on
another with the same card and driver (`tests/paired`): a per-GPU rate belongs to the host as much
as to the card. `up.sh` runs `tests/t3-topo.sh` before installing on a fresh box and stops on a
device-to-host bandwidth below the healthy box's; keep its report with the results.

## What changed for 1.3.1-alpha

The cluster scripts were written against 1.1.0-alpha. For this run:

- `00-install-once.sh` installs 1.3.1-alpha by default (`ZISK_VER=1.1.0-alpha` for `tests/r8` and
  `tests/paired`), and names the RAM-mode key after the release's setup version.
- `up.sh` treats another installed release as missing: a running 1.1 cluster is torn down and
  replaced, not reused.
- `start.sh` passes `--gpu` only to a worker that has it, and takes `MAX_RECURSIVE_STREAMS`.
- `zisk-publics` reads proofs with zisk-sdk 1.3.1-alpha (a proof is only readable by the release that
  wrote it), built `cpu-only`; it needs nasm, libsodium and OpenMPI headers, which
  `00-install-once.sh` installs.

Checked against the 1.3.1-alpha release itself: the GPU worker's flags, the coordinator's
registration line `up.sh` waits for, the key archives' names, the `globals.c` line the memlock
patch rewrites, and that `cargo-zisk execute` and `ziskemu` read the framed inputs identically.
Not checked, because only a GPU box can: a full prove.
