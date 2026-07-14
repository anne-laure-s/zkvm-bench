# scripts/ — helpers around the SP1 block-proving infra

`core.sh` is **sourced by `../run`** (not run directly); everything else is standalone.
The Python tools are stdlib-only and self-document — read the top docstring or run `--help`.

## Pipeline
- **core.sh** — generic, guest-agnostic pipeline steps (execute / prove / verify), sourced by
  `../run`. Operates purely on explicit artifact paths; never regenerates artifacts.
- **mint-inputs.sh** — batch-mint RSP block witnesses (default: 10 pre-Pectra blocks) into
  `../../guests/rsp/inputs/` via `./run gen-input`. Needs `RSP_DIR` + an archive `RPC_URL`.

## Analytics — gas & cycles
- **pull-block-gas.py** — pull `gas_used` for a set of blocks via JSON-RPC → CSV + stats.
  1 RPC call/block (cheap). Default: a reproducible 1000-block sample of Succinct's Hypercube range.
- **block-cycles.py** — heavy companion: run RSP *execute* per block to collect REAL cycle counts,
  then stats. Needs an archive node (`eth_getProof`) + the RSP checkout; minutes-to-hours for 1000 blocks.
- **plot-gas.py** — render a self-contained SVG (histogram + CDF) of the gas distribution from
  `block-gas.csv` (→ `docs/gas-distribution.svg`). Stdlib only.
- **rpc-throttle.py** — local token-bucket reverse-proxy in front of a rate-limited JSON-RPC
  endpoint (Alchemy). Makes callers *wait* instead of getting HTTP 429. Point RSP's `RPC_1` at it.

### Typical analytics wiring
```sh
# terminal A — throttle in front of your archive RPC
UPSTREAM=https://eth-mainnet.g.alchemy.com/v2/<KEY> python3 rpc-throttle.py
# terminal B — point RSP at the proxy (RPC_1=http://localhost:8545 in ../../vendor/rsp/.env), then collect
python3 pull-block-gas.py          # gas   (cheap, 1 RPC/block)
python3 block-cycles.py --n 100    # cycles (heavy: executes each block)
python3 plot-gas.py                # → docs/gas-distribution.svg
```
