# ethproofs pipeline — wiring (seams + home-made stand-ins)

Goal: a continuous [ethproofs.org](https://ethproofs.org) submission pipeline, built so every external
dependency we don't have yet is a **swappable seam** with a home-made stand-in. Each seam is a
URL/config knob — swapping a stand-in for the real thing is a config change, **zero pipeline change**.

```
chain-follow (÷100)  ──►  witness (RPC)  ──►  prove (coordinator/gateway)  ──►  submit (ethproofs API)
   WS (Alchemy now)        SEAM 1                SEAM 2                            SEAM 3
```

## The 3 seams

| seam | interface (the knob) | stand-in **now** | real **later** (just change the knob) |
|---|---|---|---|
| **witness** | `debug_executionWitness` RPC | Alchemy + openvm-eth **proxy** (`:8545`) | co-located **reth node** |
| **prover** | coordinator/gateway, network | **16-GPU cluster** (14–46 s) · `prove-farm --mock` (no box) | **< 10 s fleet** |
| **ethproofs** | 3-endpoint API + token | **`cli/ethproofs-mock`** (`:8547`) | **ethproofs.org** (+ creds) |

Every seam has a home-made stand-in for running with **zero external deps** (plumbing test): witness =
proxy, prover = `prove-farm --mock` (mock-cluster), ethproofs = `cli/ethproofs-mock`.

The clients are fully env/flag-driven, so the seams are pure config. Confirmed in
`vendor/rsp/bin/eth-proofs` (SP1) and `vendor/zisk-ethproofs` (ZisK) — the latter's `input` crate **is**
`zisk-eth-client` v0.10.0 (same `debug_executionWitness` path as `cli/witness-farm`).

## Two ways to run (same three seams)

- **Decoupled farm — recommended for continuous / RTP.** Three composable stages, the witness fetched
  **once** and reused; each stage a separate process/seam you can swap alone. This is also, internally,
  what ZisK's own client does (`input-gen-server` produces, `ethproofs-client` proves+submits).
- **One-shot live (`cli/ethproofs`).** The stack's reference monolith (RSP `bin/eth-proofs` / ZisK
  client) follows the tip and does witness+prove+submit in **one process**, re-fetching the witness
  every block. Convenient for a quick end-to-end; less modular. Kept as an option.

## Decoupled pipeline (recommended): `witness-farm │ prove-farm │ ethproofs-submit`

```
cli/witness-farm            cli/prove-farm --watch             cli/ethproofs-submit
(follow tip, write   ─────► (drain the queue, prove    ─────►  (proof.bin + report.json
 witness files)              1 block → proof + report)          → POST queued/proving/proved)
   SEAM witness                SEAM prover                        SEAM ethproofs
```

- **produce** — `ALCHEMY_URL=… cli/witness-farm` writes witnesses to `guests/<rung>/fixtures/<chain>-<block>.bin`.
  (For real ethproofs.org, produce cadence-aligned blocks — `STRIDE=100` from a multiple of 100; the mock accepts any block.)
- **prove + submit** — `cli/prove-farm --guest rsp --remote … --port … --watch --ethproofs-url http://localhost:8547`
  proves each new witness on the cluster, then hands the proof to `cli/ethproofs-submit` (POSTs the 3
  endpoints). Drop `--ethproofs-url` and prove-farm just proves (benchmark mode). Add `--newest-first`
  for the RTP policy (stay nearest the tip).
- **submit alone** — `cli/ethproofs-submit --run-dir <stack/results/…/run> --block N [--ethproofs-url …]`
  submits any finished run record on its own — **stack-agnostic**: it only reads the shared `report.json`
  + a proof file, so SP1 / ZisK / **OpenVM** all submit through the same tool, no per-stack Rust client.
  Field mapping (report.json → `/proofs/proved`): `proof.bin`→`proof`, `prove_secs×1000`→`proving_time`,
  `cycles`/`steps`→`proving_cycles`/`proving_steps`, `vkey_hash`→`verifier_id`, `zkvm`/`guest`→`vm`/`guest`,
  `*.pv.bin` digest→`public_inputs`, and `mode=mock`→`mock` (CLI flags override). See `cli/ethproofs-submit --help`.
- **no box? add `--mock`** — `cli/prove-farm --guest rsp --mock --watch --ethproofs-url http://localhost:8547`
  swaps the real cluster for a **mock-cluster** stand-in (fake proof, local, instant), so the whole
  decoupled pipeline runs on **real blocks with zero external dep** — the prover-seam sibling of
  `ethproofs-mock` and `SP1_PROVER=mock`. Plumbing test only (proves nothing cryptographically); drop
  `--mock` and add `--remote` to prove for real.

Why decoupled: the witness (minutes of getProof / Alchemy CU) is fetched **once**; prove+submit reuse it
with zero RPC — re-prove on a faster cluster or re-submit after a fix for free. And it keeps our proxy's
witness time out of the reported number (ethproofs measures proving latency, not our node stand-in).

## Proof identity, clusters, and mock marking

**Identity = `(cluster_id, block)`** — exactly as on ethproofs. Each prover is its OWN cluster, so run rsp
and zisk under different `cluster_id`s (else they overwrite on the same block). `cli/prove-farm` does this
automatically: the cluster defaults to the **deployment** — `<n>gpu-<vm>` (e.g. `16gpu-SP1`, via
`--num-gpus`) on a GPU cluster, or `local-<vm>` (e.g. `local-ZisK`) for `--mock`. Override with
`--ethproofs-cluster <id>` for your real ethproofs cluster id (numeric → sent as int; a name → string; the
mock takes both).

**What was proven** — every submission also carries `vm` (SP1/ZisK/OpenVM), `guest`
(rsp/zisk-reth/monad-sp1/…) and, for non-block proofs, a `public_inputs` digest (from the run's
`*.pv.bin`). These are metadata shown on the leaderboard; the identity stays `(cluster, block)`.

**Mock proofs are unmistakable** — a `prove-farm --mock` proof is fake and marked so it can never pass for
real, four ways:
- the **bytes** begin with a `MOCK-PROOF/mock-cluster` magic (visible with `head`/`strings`), and
  `report.json` has `mode=mock`/`backend=mock-cluster`;
- `cli/ethproofs-submit` auto-detects it → sends `mock:true`, prints `[MOCK]`, and **warns** if aimed at a
  non-local endpoint;
- `cli/ethproofs-mock` logs `[MOCK]`, shows a `⚠️MOCK` leaderboard column + `mock:true` in `/proofs`, and
  independently re-detects the magic;
- on the real ethproofs.org a mock proof fails cryptographic verification anyway — it can't masquerade.

## One-shot live launcher: `cli/ethproofs`

`cli/ethproofs` is the unified launcher: you give the **seam URLs once** (same flags for every stack)
and it maps them to that stack's native config and starts its ethproofs client(s).

```sh
cli/ethproofs --guest zisk --chain-ws wss://eth-mainnet.g.alchemy.com/v2/<key> --coordinator http://<box>:7000
cli/ethproofs --guest rsp  --chain-ws wss://eth-mainnet.g.alchemy.com/v2/<key>
```

Shared knobs (defaults point at the stand-ins): `--witness-rpc` (=proxy `:8545`), `--chain-ws`
(required), `--ethproofs-url` (=mock `:8547`), `--ethproofs-token`, `--ethproofs-cluster`,
`--block-interval` (=100), `--coordinator` (prover), `--build`, `--dry-run`. Use `--dry-run` to print
the exact per-stack config it will run (the mapping below), then drop `--dry-run` to launch.

> ⚠️ **One-shot limitations vs the decoupled path.** The vendored clients (`bin/eth-proofs`, ZisK
> `ethproofs-client`) don't send `vm`/`guest`/`mock`, and default `cluster_id=1` (RSP's
> `--eth-proofs-cluster-id` is a `u64`, so it can't take a name). Consequences: running rsp and zisk
> one-shot with the defaults **collides on cluster 1**, and their `--mock` proofs (SP1 `SP1_PROVER=mock`)
> are **not tagged mock** in the server. So pass a distinct `--ethproofs-cluster <int>` per prover, and
> for mock / multi-prover testing prefer the **decoupled path** (`prove-farm --mock`), which carries
> cluster-per-prover, vm/guest, and mock marking. Full parity would need a vendor patch to the clients.

**Test the plumbing with no cluster/GPU — `--mock` (SP1):** `cli/ethproofs --guest rsp --mock` sets
`SP1_PROVER=mock` (fake proofs, instant, never feature-gated) so the whole flow runs end-to-end —
chain-follow → witness → "prove" → submit to `ethproofs-mock` — without the cluster. The only real
dependency left is the witness RPC. This is the cheapest way to validate the pipeline before the
cluster/creds are in place:

```sh
cli/ethproofs-mock &                                                   # the ethproofs stand-in
cli/ethproofs --guest rsp --mock --build --chain-ws wss://…/<key>      # fake-prove → submit → watch the mock fill up
```

## Per-stack wiring to the stand-ins (what `cli/ethproofs` maps to)

### ZisK — `zisk-ethproofs` (vendored via `cli/install-vendors`, cluster-native)
`input-gen-server/.env`:
```
RPC_URL=http://<proxy-host>:8545                        # SEAM witness → openvm-eth proxy
RPC_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/<key>     # chain-follow (tip)
BLOCK_MODULUS=100                                       # ethproofs cadence
WS_PORT=8765 ; INPUTS_FOLDER=inputs
```
`ethproofs-client/.env` (+ run with `-s`):
```
INPUT_GEN_SERVER_URL=ws://localhost:8765
COORDINATOR_URL=<zisk cluster coordinator>              # SEAM prover → the ZisK cluster
ETHPROOFS_API_URL=http://<mock-host>:8547               # SEAM ethproofs → ethproofs-mock
ETHPROOFS_API_TOKEN=dev ; ETHPROOFS_CLUSTER=1
```

### SP1 — RSP `bin/eth-proofs`
```
--http-rpc-url http://<proxy-host>:8545                 # SEAM witness → proxy
--ws-rpc-url   wss://eth-mainnet.g.alchemy.com/v2/<key> # chain-follow
--block-interval 100
--eth-proofs-endpoint http://<mock-host>:8547           # SEAM ethproofs → ethproofs-mock
--eth-proofs-api-token dev --eth-proofs-cluster-id 1
```
**witness**: `bin/eth-proofs` builds the stateless witness itself via `eth_getProof` (RSP `BasicRpcDb`,
not `debug_executionWitness`), so the proxy is **optional** here — `--http-rpc-url` can point straight at
Alchemy. (The proxy still works — it forwards `getProof` — but ZisK and `witness-farm`'s RSP-EW path, by
contrast, DO use `debug_executionWitness` and require it.)

**prover**: `bin/eth-proofs` now selects the prover from `SP1_PROVER` (`from_env`) — so it's the same
seam as ZisK. `cli/ethproofs --guest rsp --coordinator <gateway>` sets `SP1_PROVER=network` +
`NETWORK_RPC_URL=<gateway>` → the distributed cluster; without `--coordinator` it's `SP1_PROVER=cuda`
(single-box). The network build needs no CUDA: `cargo build --release --bin eth-proofs --no-default-features`.

## Go-live runbook (each seam independent, config-only)

Each blocker resolves to a config change — no pipeline change. What to do when each arrives:

- **reth node** → point the witness RPC at it: `witness-farm` `ALCHEMY_URL=<node>` / `cli/ethproofs
  --witness-rpc <node>` / RSP `--http-rpc-url <node>`. `debug_executionWitness` is then native — no
  getProof storm (kills the proxy's ~200 s/block; witnesses in ~1 s). The proxy stand-in is retired.
- **ethproofs.org creds** → decoupled path: `prove-farm --ethproofs-url https://…ethproofs.org/api
  --ethproofs-token <token> --ethproofs-cluster <your-registered-id>`, **and add `--minimal`** on the
  submitter so only the canonical fields go out (see below). One-shot path: `cli/ethproofs
  --ethproofs-url/--ethproofs-token/--ethproofs-cluster` = the real values.
- **faster fleet (< 10 s RTP)** → `--coordinator <gateway>` / `COORDINATOR_URL` / cluster sizing — the
  only lever for the RTP target.

**Payload fidelity (verified against the vendored clients).** `cli/ethproofs-submit`'s CANONICAL payload
— `{block_number, cluster_id, proof, proving_cycles, proving_time, verifier_id}` + `Content-Type` +
`Authorization: Bearer` — matches EXACTLY what RSP `bin/eth-proofs` and ZisK `api.rs` POST (both put their
work-unit in `proving_cycles`; there is no `proving_steps`). It adds `vm/guest/mock/public_inputs` for the
mock leaderboard; **`--minimal` drops those** so a strict ethproofs.org sees only the canonical set.

## Status

| piece | state |
|---|---|
| stand-in: witness (proxy) | ✅ have it (`cli/witness-farm` uses it) |
| stand-in: prover — real (16-GPU cluster) | ✅ have it |
| stand-in: prover — mock (`prove-farm --mock`) | ✅ built + tested (mock-cluster: local fake run record, no box) |
| stand-in: ethproofs (`cli/ethproofs-mock`) | ✅ built + tested (verifies proofs — mock, `--verify-cmd` for real; returns `{proof_id,verified}`, saves proofs, leaderboard) |
| submit seam: `cli/ethproofs-submit` | ✅ built + tested (proof+`report.json` → 3 endpoints; stack-agnostic; sends vm/guest/public_inputs; marks mock; cluster int/str coercion) |
| identity + mock marking | ✅ identity `(cluster_id, block)`; cluster-per-prover default (`16gpu-SP1`/`local-ZisK`); mock proofs magic-tagged + flagged + warned |
| decoupled pipeline (farm) | ✅ wired + tested end-to-end no-box (`prove-farm --mock --ethproofs-url` → `ethproofs-submit`) |
| ZisK ethproofs client (one-shot live) | ✅ vendored (`zisk-ethproofs`), cluster-native → wire the 3 seams + run `-s`. ⚠️ no vm/guest/mock; set a distinct `--ethproofs-cluster` |
| SP1 ethproofs client (one-shot live) | ✅ `bin/eth-proofs` — prover env-selectable (`from_env`/`SP1_PROVER`); cluster via `--coordinator`. ⚠️ no vm/guest/mock; cluster is `u64` (default 1) |
| OpenVM ethproofs | ✅ via the decoupled path (`prove-farm → ethproofs-submit`); a live monolith client is still unported |
| HARD blockers (shared) | reth node (instant witness) · < 10 s prover · real ethproofs creds |

`cli/ethproofs-mock` accepts what **both** clients send (SP1 `bin/eth-proofs` ignores the reply; ZisK
`api.rs` parses `{proof_id}` — the mock returns that) and is lenient on field names
(`proving_cycles`/`proving_steps`). Like the real server it **verifies** each proof on `/proved` (the
trust anchor, not a dumb store): by default a **mock verification** (structural — a real, non-empty,
decodable proof with a verifier id + block → ✓/✗, shown on the leaderboard), swappable for a REAL
verifier via `--verify-cmd <cmd> <proof_path> <block> <verifier_id>` (exit 0 = valid). Verification is
thus itself a seam: mock now, real verifier or ethproofs.org later. Point either client's ethproofs URL
at it to run the whole pipeline end-to-end with no ethproofs.org account.
