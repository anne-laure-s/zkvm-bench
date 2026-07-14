# sp1-infra — design & internals

How the SP1 harness works: the runner, the guest model, verification, and the report format.
The [README](../README.md) is the copy-paste runbook to prove a block; this is the reference behind it.

## Why this layout

- **Guest is portable.** Any ELF compiled with `cargo prove build` works.
- **Input is opaque.** The runner streams raw bytes into `SP1Stdin`; the guest deserializes whatever format it expects (typically bincode).
- **Public values are opaque too.** Saved as raw bytes; deserialize on the host with knowledge of the guest's commit type.

### Input layering (zkVM-neutral interchange)

The stored input artifact (`guests/<guest>/inputs/<tag>.bin`) is the **raw witness** — the guest's input
bytes (e.g. RSP's `bincode(EthClientExecutorInput)`, byte-identical to RSP's own cache). It is
deliberately **not** wrapped in any zkVM's stdin type. The per-zkVM framing (`SP1Stdin` for SP1,
`ExecutorEnv` for Risc0, `StdIn` for OpenVM, …) is applied **at the edge, by the prover backend**:

- `sp1-runner` wraps it internally (`write_slice` → `SP1Stdin`) for local + direct (`./run prove`) proving.
- For the multi-GPU `sp1-cluster` backend (`cluster-native/`), `sp1-runner` runs as a **network
  client** and wraps the same raw `.bin` itself — it ships the ELF + raw witness to the cluster's
  network-gateway (no separate stdin file).

Keeping the raw witness as the neutral interchange format means the expensive part (witness
generation) is shared, and adding another zkVM later (ZisK, OpenVM, …) is a new **backend adapter**
that wraps the same `.bin` — mirroring how `guests/` are program adapters.

## The runner: one binary, three roles

`sp1-runner` is a single codebase used locally (CPU), on the GPU box (CUDA), and as the cluster's
network client. The backend is gated behind cargo features:

```sh
cargo build --release                     # local CPU (laptop, validation before paying for a GPU)
cargo build --release --features cuda     # direct single-box CUDA (on the box)
# the cluster network client is built by cluster-native/04-build-runner.sh (--features network)
```

> Full build matrix — all four builds (incl. the `profiling` build), their `--target-dir`, and the
> runtime env each needs: [`../sp1-runner/README.md`](../sp1-runner/README.md).

**Prover selection.** The runner defaults to the CUDA backend: if `SP1_PROVER` is unset, it is forced
to `cuda`. An explicit `SP1_PROVER` always wins — so on a GPU box nothing extra is needed. For a
**local CPU run you must opt out of CUDA**, otherwise a CPU-only build fails trying to reach a GPU backend:

```sh
SP1_PROVER=cpu sp1-runner --elf path/to/guest.elf --input path/to/input.bin --mode execute
```

Use this to validate any guest end-to-end on your machine before provisioning a GPU instance.

### Modes

| Mode               | Output                  | Use case                                |
|--------------------|-------------------------|-----------------------------------------|
| `execute`          | Cycle count + PV        | Profiling, no proof; cheapest; default mode |
| `prove-core`       | STARK proof (large)     | Cheapest & largest proof                |
| `prove-compressed` | Recursed STARK (small)  | Default benchmark on Ethproofs          |
| `prove-groth16`    | Groth16 (~260 bytes)    | On-chain verification (EVM)             |
| `verify`           | Pass/fail               | Re-verify a saved proof standalone      |

## The guest model

Guests follow the standard SP1 pattern:

```rust
#![no_main]
sp1_zkvm::entrypoint!(main);

pub fn main() {
    let input: MyInput = sp1_zkvm::io::read();
    // ... compute ...
    sp1_zkvm::io::commit(&output);
}
```

Build with:

```sh
cd path/to/guest-program
cargo prove build
# ELF lands in target/elf-compilation/riscv64im-succinct-zkvm-elf/release/<name>
```

The host serializes inputs with bincode (default for `SP1Stdin`). The convention is to write the raw
serialized payload to a file and let the guest read it back as a typed value via `sp1_zkvm::io::read()`.

### The contract & the dispatcher

The pipeline is split into a generic **core** and per-guest **edges**. The core (proving on the remote
GPU, verifying locally) is the same for every guest — `sp1-runner` takes any ELF + opaque input. Only
three things are guest-specific, declared in `guests/<name>/guest.sh`:

| Contract function | Responsibility |
|---|---|
| `guest_tag`        | echo a canonical id for the current params → drives file names |
| `guest_build_elf`  | build the guest ELF, write it to `$ELF` |
| `guest_gen_input`  | build the witness/input, write it to `$INPUT` |
| `guest_decode_pv`  | pretty-print public values (optional) |

Everything is driven through one dispatcher, `./run`, with two kinds of commands:

- **Producers** (guest-aware) — build the ELF / generate the input; take a `GUEST` and its params:
  ```sh
  ./run build-elf GUEST=<name> [KEY=VALUE ...]   # -> ../../guests/<name>/<name>.elf
  ./run gen-input GUEST=<name> [KEY=VALUE ...]   # -> ../../guests/<name>/inputs/<tag>.bin
  ./run decode-pv GUEST=<name> PV=<path>         # pretty-print public values
  ```
- **Consumers** (generic, file-based) — prove / verify / execute operate on explicit artifact **paths**.
  They never regenerate anything, so they take no guest params:
  ```sh
  ./run execute ELF=<path> INPUT=<path>            # local CPU run -> rich report + pv
  ./run prove   ELF=<path> INPUT=<path> REMOTE=user@host [PORT=p] [MODE=m]
  ./run verify  ELF=<path> INPUT=<path> PROOF=<path>
  ```

`KEY=VALUE` tokens become environment variables (producer params like `N=20`, `BLOCK=20000000`, or
consumer paths like `ELF=…`). Producers print the exact follow-up command to paste.

**Adding a guest:** drop a `guests/<name>/guest.sh` defining the four functions above. The guest's
*source* can live anywhere — inside this repo (like `guests/fibonacci`, see its
[README](../guests/fibonacci/README.md)) or in a separate checkout the adapter points to (like
`guests/rsp`). No changes to `run`, `scripts/core.sh`, or `sp1-runner` are needed.

### Direct proving (`./run prove`) and the run record

Besides the 16-GPU `cluster-native/` path, `./run prove` proves a single block directly on a box that
has `sp1-runner` on its `PATH`:

```sh
./run prove ELF=… INPUT=… REMOTE=user@host [PORT=p] [MODE=prove-compressed]
```

It ships the ELF (only when its checksum changed) + input over SSH, runs `sp1-runner`, and pulls back
a full run record. Override `REMOTE_RUNNER` / `REMOTE_PROVER` / `REMOTE_WS` for a bare SSH host where
`sp1-runner` isn't on `PATH`. `prove` saves **one record per run** (never overwritten) at
`results/<…>/<tag>/<mode>-<timestamp>/`, containing:

- `proof.bin` / `pv.bin` / `vkey.txt`
- `report.json` — timings (setup/prove/verify/total), `proof_bytes`, vkey
- `prove.log` — the full SP1 proving trace, streamed live and saved
- `env.txt` — the run context: GPU (`nvidia-smi`), host, CPU count, SP1 version, mode

The remote run defaults to `RUST_LOG=info` (coarse: the top `prove` span). Set **`RUST_LOG=debug`** for
the full phase-by-phase trace (`prove shard`, `jagged`, `logup GKR`, `zerocheck`, `Dense PCS`, …) — it
gets large for high-cycle blocks.

`./run execute` runs the guest locally (no GPU, no proof) and saves an execution report next to the
input at `guests/<guest>/inputs/<tag>.exec-report.json` — cycle count, gas, and full opcode/syscall
breakdowns. The report describes the **input/block**, so it lives next to the input, not with the proofs.

## Reading public values on the host

The runner writes public values as raw bytes (`--public-values pv.bin`). The guest commits via
`sp1_zkvm::io::commit(&value)`, which uses bincode. On the host, deserialize with the matching type:

```rust
let pv_bytes = std::fs::read("pv.bin")?;
let mut slice = pv_bytes.as_slice();
let my_value: MyType = bincode::deserialize_from(&mut slice)?;
```

For inspection without typed deserialization, the bytes can be hex-dumped to read the layout manually.

## Verification

By default, `prove-*` modes verify the proof immediately after generation. This catches pipeline bugs
early and the cost is negligible (ms to seconds). To skip it (e.g. for pure proving benchmarks):

```sh
sp1-runner ... --skip-verify
```

The report JSON includes `verified: true|false` and `verify_secs`.

### Verifying a saved proof standalone (use with caution)

To verify a proof saved earlier (e.g. after transferring `proof.bin` off the instance), use `--mode
verify`. No guest input is needed — the verifying key is re-derived from the ELF.

Verification is bound to the public values you expect: `--public-values` here is the **expected**
public values file, and the runner checks that the proof commits to exactly those bytes. A
cryptographically valid proof for *different* public values is rejected.

```sh
sp1-runner --elf path/to/guest.elf --mode verify --proof proof.bin --public-values expected_pv.bin
```

It exits non-zero if either the cryptographic verification fails **or** the proof's public values
don't match `expected_pv.bin`. The report records `verified: true` and `public_values_match: true` on
success. The `expected_pv.bin` is typically the `--public-values` output saved during proving (or
independently recomputed from the known input).

> Normally the verifier must check the public inputs against ground truth it independently trusts (for
> a block proof, e.g. fetching the state roots from a trusted source). Here that's simplified to taking
> a public-inputs file that is typically produced by the prover — so use with caution.

## Report format

Each run writes a JSON report; the shared cross-zkVM contract is documented in
[`../../../cli/report-schema.md`](../../../cli/report-schema.md). A prove report looks like:

```json
{
  "mode": "prove-core",
  "setup_secs": 12.34,
  "prove_secs": 145.67,
  "verify_secs": 0.23,
  "total_secs": 158.24,
  "vkey_hash": "0x1234abcd...",
  "public_values_bytes": 64,
  "verified": true
}
```

The **execute** report (`--mode execute`, e.g. via `./run execute`) is richer: alongside `cycles`,
`gas`, `total_syscalls` and `exit_code`, it embeds the full SP1 `ExecutionReport` under
`execution_report` — per-opcode and per-syscall counts (and a cycle tracker). Handy for keeping
detailed stats about a block (e.g. its `KECCAK_PERMUTE` / `SECP256K1_*` syscall load). The serde layout
of that sub-object is stable only within a single SP1 version.

## Proving Ethereum blocks with RSP

[RSP](https://github.com/succinctlabs/rsp) (Reth Succinct Processor) is the guest that re-executes an
EVM block inside the zkVM. It fits the opaque-input model exactly, which lets us keep a strict role split:

- **Local machine** does all the "extras" — fetch the block + state over RPC, run the Reth execution,
  build the witness (`EthClientExecutorInput`). This is RSP's *host*.
- **Remote GPU machine** does *only* proving. It receives the RSP **guest ELF**, a per-block **input
  file**, and runs the already-present `sp1-runner`. It never runs RSP itself.

### Why it just works

- RSP pins **SP1 = 6.2.4**, identical to this repo — the ELF and SDK are compatible out of the box.
- RSP serializes its witness as `bincode::serialize(&client_input)` → `stdin.write_vec(buffer)`, and its
  on-disk cache (`cache/input/{chain_id}/{block}.bin`) is the *same bytes*. `sp1-runner` streams the
  input file with `write_slice`, and the guest reads it back with `io::read_vec()`. So **the RSP cache
  file is the runner's `--input` file, byte for byte** — no conversion, no runner changes.
- The guest commits a `CommittedHeader` (the executed block header) as public values.

> **Hard constraint:** the guest ELF and the input file must come from the **same RSP commit** — the
> `EthClientExecutorInput` layout can change between versions. `build-elf` records the RSP commit in
> `../../guests/rsp/rsp.commit`; regenerate both together when bumping RSP.

### Requirements

- A local RSP checkout (`RSP_DIR` — **required**, no default; the repo's clone lives at `../../vendor/rsp`) and the SP1 toolchain (`cargo prove`).
- An RPC that serves **`eth_getProof`** (RSP builds state proofs for the block). Keyless public
  endpoints usually gate this method (e.g. publicnode → `403 archive token required`), so use a
  **free-tier API key** — Alchemy's free tier includes archive + `eth_getProof` and works for any
  block. Pass `RPC_URL=…` or set `RPC_<chain_id>` (e.g. `RPC_1` for mainnet).
- The chain is parameterized via `CHAIN_ID` (default `1` = Ethereum mainnet).

### Start with a cached input (no RPC at prove/verify time)

The fastest way in is to mint **one** block witness and replay it offline. Because `prove`/`verify`
are file-based, that witness is just an `INPUT` file — no RPC needed once it exists. See
[`../../../guests/rsp/inputs/`](../../../guests/rsp/inputs/README.md) for the one-shot mint recipe and the
same-commit rule.

### A note on verification trust

`./run verify` checks that the proof is cryptographically valid and **bound** to the expected public
values (recomputed locally via `--mode execute`), then points you to `./run decode-pv GUEST=rsp PV=…`
to inspect the committed header. For a *real* block proof the meaningful check is binding to **ground
truth** — the committed block hash must match the canonical hash from a trusted source (a trusted
RPC/explorer), not from the prover. The local recompute proves the proof matches *your* execution of
the same witness; it does not by itself prove the witness describes the canonical chain.

## Cost discipline

GPU instances are expensive. Standard practice:

- Build `sp1-runner` on the box once, not per-session.
- Compile guests locally (free) before uploading.
- Use `execute` mode first to validate the pipeline and get cycle counts before committing to a full
  `prove` run.
- Shut down instances immediately after retrieving results.

## Updating SP1

When upgrading to a new SP1 version:

1. Update the pinned version in `sp1-runner/Cargo.toml`:
   ```toml
   sp1-sdk = { version = "=X.Y.Z", features = ["blocking"], default-features = false }
   ```
2. Rebuild `sp1-runner` on the box (`cd sp1-runner && cargo build --release --features cuda`), or re-run
   [`cluster-native/04-build-runner.sh`](../cluster-native/).
3. The `sp1-gpu-server` downloaded at runtime is keyed to the SDK version — `~/.sp1/bin/` on the box
   re-downloads on the first proof after an upgrade.
