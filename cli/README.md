# cli — drive any guest from the repo root

Guest-agnostic command-line entry points over [`guests.registry`](guests.registry), the single source
of truth. Each reads the registry via `reg.sh` and delegates to the guest's stack
(`infra/<stack>-infra/run`) — so you name a **guest**, not a stack. Run them from the repo root; paths
resolve against it.

| CLI | Does | Errors clearly on |
|-----|------|-------------------|
| `cli/gen-elf   --guest <name>` | build the guest ELF → `guests/<dir>/<dir>.elf` | `monad-*` (ELF pre-supplied) |
| `cli/gen-witness --guest <name> --block <n> [--rpc <url>]` | generate a block witness → `guests/<dir>/inputs/` | `zisk` (needs a debug node), `monad-*` (pre-supplied), `fibonacci` (toy) |
| `cli/execute  --guest <name> --input <path>` *(or `--block <n>`)* | run locally, no proof → the deterministic cycle/step count | `monad-*` (batch → run `guests/monad/ev.sh`) |
| `cli/prove-farm --guest <name> --remote user@host [--port p]` *(+ blocks)* | batch-prove collected witnesses on the **remote cluster** → run records `{proof.bin · prove.log · report.json}` | `monad-*` (batch) |

Above, `<dir>` is the guest's **directory** (the registry `run_guest` column), which is `openvm-reth`/`zisk-reth` for `openvm`/`zisk` — not the guest `<name>`.

`--list` on any of them prints the registry. Full quickstart + worked examples: the [root README](../README.md).

## Farming: collect → prove

Two continuous drivers pair up for bulk witness→proof runs (outputs at the repo root, git-ignored):

- **`cli/witness-farm [START_BLOCK]`** — continuously generate RSP + ZisK block witnesses (via the
  openvm-eth rpc-proxy over a hosted RPC), marching forward from a block, resumable. Writes
  `guests/<dir>/fixtures/<tag>.bin` + a `witness-farm.csv` recap. Knobs (env): `STRIDE MARGIN NIBBLES
  RETRY_CU RSP_RPC_CU PROXY_START_TRIES RSP_SKIP_EXEC MAX_BLOCKS`. Deterministic proxy preimage
  failures at the current `--preimage-cache-nibbles` are marked `zisk=NIB<n>` and skipped (a higher
  `NIBBLES` re-attempts them); RSP is skipped for those blocks too (no comparable pair possible).
- **`cli/prove-farm --guest <g> --remote user@host [--port p] [--mode m] [--num-gpus k]`** — batch-prove
  those witnesses on the remote cluster and record timings (`prove-farm.csv`). The prove-side analog of
  `cli/execute`: registry-driven, resumable, **zero per-stack branching**.

`prove-farm` delegates each block to the uniform verb **`infra/<stack>/run prove-cluster GUEST=<g>
BLOCK=<n> REMOTE=...`**, which each stack implements behind ONE contract — resolve its own ELF/witness,
drive ITS cluster backend (SP1 network-gateway · ZisK coordinator · OpenVM per-GPU), and fetch back a
run record with **`proof.bin` (or `segments/`) + a detailed proving log + `report.json`** (the shared
[contract](report-schema.md)). Exit 0 ⇔ the proof was produced and fetched. Adding a stack = a registry
row + a `prove-cluster` verb — nothing in `prove-farm`.

The cluster stays a **pure proving service**: `run prove-cluster` (via `prove_remote`) does the ssh/scp;
nothing here runs on the box. Bring up the box (runner, gateway/coordinator) via each stack's
`infra/<stack>-infra/cluster*/` scripts.

## guests.registry — add a guest = add a row

One `|`-separated row per guest (surrounding spaces ignored):

```
name | infra_dir | run_guest | eth_var | vendor_dir | elf | witness | exec | description
```

The three per-capability columns tell the CLIs what each guest supports (and what to error on):

- **elf**: `build` (from `vendor_dir`) · `presupplied` (shipped in-repo)
- **witness**: `rpc` (archive RPC) · `rpc-debug` (needs a `debug_executionWitness` node) · `presupplied` · `toy` (not a block witness)
- **exec**: `input` (`execute ELF INPUT`) · `block` (`execute BLOCK`) · `batch` (`guests/monad/ev.sh`)

Adding a guest is purely additive — a new row, no code change. `reg.sh` (`reg_lookup` / `reg_list`) is
the shared parser the CLIs source.

## report-schema.md

The shared `report.json` contract every stack's runner emits (execute + prove), so one consumer reads
all zkVMs uniformly → [report-schema.md](report-schema.md).
