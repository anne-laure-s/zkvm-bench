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

Above, `<dir>` is the guest's **directory** (the registry `run_guest` column), which is `openvm-reth`/`zisk-reth` for `openvm`/`zisk` — not the guest `<name>`.

`--list` on any of them prints the registry. Full quickstart + worked examples: the [root README](../README.md).

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
