# guests — per-guest artifacts (shared, prover-agnostic)

Compiled guest **ELFs** and their **inputs**, one directory per guest. These are shared across the
stacks: the same artifacts are produced on the Mac ([`cli/gen-elf`](../cli/) · [`cli/gen-witness`](../cli/),
which delegate to `infra/<stack>-infra/run`) and consumed by proving (`infra/<stack>-infra`) and by
[`profiling/`](../profiling/). The regenerable outputs (compiled `*.elf`, block witnesses, and the
`*.exec-report.json` execute reports) are git-ignored, as is the Monad `inputs/` (witnesses + roots);
the `*.commit` pins and the pre-supplied Monad ELFs are versioned (see the root `.gitignore`).

## Layout of a guest

```
guests/<name>/
├── <name>.elf            # the compiled guest
├── <name>.commit         # source commit the ELF was built from (pin ELF + inputs together)
└── inputs/               # per-block witnesses (+ <tag>.exec-report.json from `execute`), plus a
                          #   README: how to (re)generate them + a reference block
```

> **Same-commit rule:** an input only matches an ELF built from the **same source commit** (the witness
> layout can change). Regenerate both together when bumping the upstream; `<name>.commit` records it.

## The guests

| Guest | zkVM · client | Notes |
|-------|---------------|-------|
| `rsp` | SP1 · reth (RSP) | witness minted from an archive RPC (`eth_getProof`) into `inputs/` |
| `zisk-reth` | ZisK · reth | input is `<tag>.bin` **+** `<tag>.hints` in `inputs/` |
| `openvm-reth` | OpenVM · reth | no shipped ELF/witness — the box mints per block into `inputs/` (RPC cache) |
| `fibonacci` | SP1 · toy example | minimal guest to validate the SP1 pipeline |
| `monad` | Monad guest on **SP1 + ZisK** | block-replay ELFs + pre-supplied witnesses in `inputs/` + `ev.sh` — see [monad/README.md](monad/README.md) |

Input READMEs (how to (re)generate them): [rsp/inputs](rsp/inputs/README.md) ·
[zisk-reth/inputs](zisk-reth/inputs/README.md).
How a guest is wired (the `guest.sh` recipe + registry): [`../cli/README.md`](../cli/README.md).
