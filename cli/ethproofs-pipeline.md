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
| **prover** | coordinator/gateway, network | current **16-GPU cluster** (14–46 s) | **< 10 s fleet** |
| **ethproofs** | 3-endpoint API + token | **`cli/ethproofs-mock`** (`:8547`) | **ethproofs.org** (+ creds) |

The clients are fully env/flag-driven, so the seams are pure config. Confirmed in
`vendor/rsp/bin/eth-proofs` (SP1) and `vendor/zisk-ethproofs` (ZisK) — the latter's `input` crate **is**
`zisk-eth-client` v0.10.0 (same `debug_executionWitness` path as `cli/witness-farm`).

## Launch — one shared interface: `cli/ethproofs`

`cli/ethproofs` is the unified launcher: you give the **seam URLs once** (same flags for every stack)
and it maps them to that stack's native config and starts its ethproofs client(s).

```sh
cli/ethproofs --guest zisk --chain-ws wss://eth-mainnet.g.alchemy.com/v2/<key> --coordinator http://<box>:7000
cli/ethproofs --guest rsp  --chain-ws wss://eth-mainnet.g.alchemy.com/v2/<key>
```

Shared knobs (defaults point at the stand-ins): `--witness-rpc` (=proxy `:8545`), `--chain-ws`
(required), `--ethproofs-url` (=mock `:8547`), `--ethproofs-token`, `--ethproofs-cluster`,
`--block-interval` (=100), `--coordinator` (ZisK prover), `--build`, `--dry-run`. Use `--dry-run` to
print the exact per-stack config it will run (the mapping below), then drop `--dry-run` to launch.

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
**prover**: `bin/eth-proofs` now selects the prover from `SP1_PROVER` (`from_env`) — so it's the same
seam as ZisK. `cli/ethproofs --guest rsp --coordinator <gateway>` sets `SP1_PROVER=network` +
`NETWORK_RPC_URL=<gateway>` → the distributed cluster; without `--coordinator` it's `SP1_PROVER=cuda`
(single-box). The network build needs no CUDA: `cargo build --release --bin eth-proofs --no-default-features`.

## Swap to real (each seam independent, config-only)

- **reth node** ready → witness RPC: `RPC_URL` / `--http-rpc-url` = the node. (Kills the proxy's 30 s–9 min.)
- **ethproofs creds** → `ETHPROOFS_API_URL/TOKEN/CLUSTER` / `--eth-proofs-*` = ethproofs.org values.
- **faster fleet** → `COORDINATOR_URL` / cluster sizing (the only lever for the < 10 s RTP target).

## Status

| piece | state |
|---|---|
| stand-in: witness (proxy) | ✅ have it (`cli/witness-farm` uses it) |
| stand-in: prover (16-GPU cluster) | ✅ have it |
| stand-in: ethproofs (`cli/ethproofs-mock`) | ✅ built + tested (returns `{proof_id}`, saves proofs, leaderboard) |
| ZisK ethproofs client | ✅ vendored (`zisk-ethproofs`), cluster-native → wire the 3 seams + run `-s` |
| SP1 ethproofs client | ✅ `bin/eth-proofs` — prover env-selectable (`from_env`/`SP1_PROVER`); cluster via `--coordinator` |
| OpenVM ethproofs client | ❌ none — port the 3-endpoint client (simple; see `zisk-ethproofs .../api.rs`) |
| HARD blockers (shared) | reth node (instant witness) · < 10 s prover · real ethproofs creds |

`cli/ethproofs-mock` accepts what **both** clients send (SP1 `bin/eth-proofs` ignores the reply; ZisK
`api.rs` parses `{proof_id}` — the mock returns that) and is lenient on field names
(`proving_cycles`/`proving_steps`). Point either client's ethproofs URL at it to run the whole pipeline
end-to-end with no ethproofs.org account.
