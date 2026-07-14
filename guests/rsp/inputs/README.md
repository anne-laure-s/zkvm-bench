# RSP inputs

A block **witness** (`bincode(ClientExecutorInput)`) that lets you exercise the full prove/verify
pipeline **offline** — no RPC, no archive node. Mint it once, replay forever. Witnesses live in this
`inputs/` dir; the `.bin` are git-ignored (large) — regenerate from the recipe below.

Because `./run prove` / `./run verify` are file-based, a witness is used directly as the `INPUT`:

```sh
./run prove  ELF=../../guests/rsp/rsp.elf INPUT=../../guests/rsp/inputs/<file>.bin REMOTE=user@host PORT=p
./run verify ELF=../../guests/rsp/rsp.elf INPUT=../../guests/rsp/inputs/<file>.bin PROOF=results/rsp/<tag>/<run>/proof.bin
```

> **Same-commit rule:** a witness only matches an ELF built from the **same RSP commit** it was minted
> with (the `ClientExecutorInput` layout can change) — rebuild the ELF from that commit before proving.

## How to mint

Building the witness requires `eth_getProof` (state proofs for every account/slot the block touches),
which keyless public endpoints almost always gate (e.g. publicnode: `403 Archive requests require a
personal token`). Use an RPC that serves `eth_getProof`:

- **Free-tier API key** (no payment): Alchemy is the easy choice — its free tier includes archive +
  `eth_getProof`, so any block works. dRPC / Infura free keys work too.
  → `RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<KEY>`
- A recent block (within ~128 of head) avoids needing *archive* state, but you still need an endpoint
  that serves `eth_getProof` — the free-tier key covers both.

```sh
RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<KEY>
BLOCK=25367437                                      # any block (Alchemy free = archive)

# mint -> ../../guests/rsp/inputs/1-<BLOCK>.bin
./run gen-input GUEST=rsp RSP_DIR=/path/to/rsp BLOCK=$BLOCK CHAIN_ID=1 RPC_URL=$RPC_URL
# batch several at once: scripts/mint-inputs.sh (curated pre-Pectra list, or pass blocks)
```

If an endpoint returns 403 / `-32602` about archive/token, it doesn't serve `eth_getProof` on the free
path — switch to a free-tier key.

## Storage

Witnesses can be tens of MB. They're kept local and regenerated from the recipe rather than committed
(the `.bin` are git-ignored); only this README and the `../rsp.commit` pin are versioned.

## Reference

Sanity check at the pinned commit (`../rsp.commit`): block `25367437` (583 txs) executes to ~763M cycles.
