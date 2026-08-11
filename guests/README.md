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
| `monad-sp1` · `monad-zisk` | Monad guest, per zkVM | the **provable** pair: `cli/prove-farm` routes each to its stack, and `monad-zisk` is what the RTP pipeline proves. ELF is a symlink to `monad/`'s; inputs are minted by that dir's `gen-inputs` |

## What is *not* a guest

[`monad-variants/`](monad-variants/README.md) holds Monad guest builds that exist only to be put on
an axis by `profiling/compare.py` — the baseline, the optimised line, the per-lever ablations, a
superseded branch. They have no registry row, no `inputs/`, no `.commit`, and are never proved, so
they are **not** guests and do not follow the layout above. Their ELFs are git-ignored like any other
build output; `monad-variants/README.md` carries the sha256 of each, which is what actually has to
survive a clone.

Rule of thumb: if it has a row in [`cli/guests.registry`](../cli/guests.registry) it is a guest and
lives here; if it exists to produce a number in a report, it is a variant.

## Naming: a name is a LABEL, never an identity

The one rule everything else follows from: **what identifies a build is its content, not its name.**
A name is assigned by a human and cannot be checked against a binary; a hash is derived from the
binary and cannot be wrong. So names exist to be read, and hashes exist to be trusted.

Two properties of the alternative make the point. An ELF's mtime is not an identity: binaries built
in the same second share one, and a `cp` changes one without changing a byte. And a version suffix is
not one either — it has to be typed, by hand, at the moment iteration is fastest, and a forgotten bump
is silent: the cache would serve an older build's numbers under the newer name.

Hence identity = **sha256 of the ELF** (~10 ms per ELF per invocation, paid once, not per block). It
survives a move, it collapses two names for one binary automatically, and it makes version suffixes
unnecessary — the hash bumps itself. Names are then free to change without corrupting anything.

### The convention

1. **Backend always explicit, never implicit.** `monad-r3-zisk` *and* `monad-r3-sp1`, so the two read
   as peers rather than as one name and a variant of it.
2. **Name from the branch, not the role.** `current`, `opt`, `today` describe a position that moves,
   so they are wrong as soon as it does. Name the lineage (`monad-r3` for `al/zkvm-r3`); keep the role
   as a display alias on the axis if it helps.
3. **An ablation names what it REMOVES, identically on all three layers** — axis, side name, filename.
   `ab-no-opstar` / `ab-no-opstar.elf`, never a file that reads as "the opstar ablation" beside a side
   that reads as "without opstar".
4. **One name, one ELF.** Two axes pointing at the same binary share the name; that is a fact to
   record, not a duplication to fix.

### Scope

The convention governs the measurement variants. **Registry guests are exempt**: `rsp`, `zisk-reth`,
`monad-sp1`, `monad-zisk`, `openvm` and `fibonacci` have their names reaching into
`cli/guests.registry`, `cli/prove-farm`, each stack's `guest.sh` and — for `monad-zisk` — the RTP
pipeline on the devcore box, so a rename there costs far more than it buys.

Input READMEs (how to (re)generate them): [rsp/inputs](rsp/inputs/README.md) ·
[zisk-reth/inputs](zisk-reth/inputs/README.md).
How a guest is wired (the `guest.sh` recipe + registry): [`../cli/README.md`](../cli/README.md).
