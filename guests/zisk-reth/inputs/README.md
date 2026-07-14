# ZisK inputs

A block **witness** that lets you exercise the full prove/verify pipeline **offline** — no RPC, no
archive node. Generate once, replay forever. ZisK needs **two** artifacts per block (unlike SP1's single
`.bin`): the framed input `<tag>.bin` and the precompile `<tag>.hints`. `./run` finds the `.hints`
automatically as the sibling of the `INPUT` (same stem), so you only pass the `.bin`. Both are
git-ignored (large) — regenerate from the recipe below.

```sh
# prove on a remote GPU box (hints sibling auto-detected)
./run prove  ELF=../../guests/zisk-reth/zisk-reth.elf INPUT=../../guests/zisk-reth/inputs/1-24628607.bin \
             REMOTE=root@<host> PORT=<port>
./run verify ELF=../../guests/zisk-reth/zisk-reth.elf INPUT=../../guests/zisk-reth/inputs/1-24628607.bin \
             PROOF=results/zisk-reth/1-24628607/…/proof.bin
```
(Override the hints path with `HINTS=<path>` if it isn't a sibling.)

> **Same-commit rule:** a witness only matches an ELF built from the **same zisk-eth-client commit** it
> was generated with (the witness/hints layout can change). Rebuild the ELF from that commit first.

## Reference

Sanity check at the pinned commit (`../zisk-reth.commit`): block `1-24628607` executes to 77 032 188 steps.

## Generate more
```sh
./run gen-input GUEST=zisk-reth ZISK_ETH_DIR=../../vendor/zisk-eth-client \
  SAMPLE=../../vendor/zisk-eth-client/bin/guests/stateless-validator-reth/inputs/<sample>.bin
# -> ../../guests/zisk-reth/inputs/1-<block>.{bin,hints}
```
