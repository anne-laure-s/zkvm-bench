# ziskethone inputs

Block witnesses for the **C++ ZisK Ethereum guest** (`ziskethone`), the sibling of `zisk-reth`: same
blocks, same source material, a completely different input format. Where `zisk-reth` feeds the guest
two bincode slices (an RLP block + a raw `debug_executionWitness`), ziskethone consumes a single
`ZEG0` container in which the host has already **pre-linearised the state trie** into an opcode
stream. See `vendor/ziskethone/BINARY_FORMAT.md` for the layout.

## Layout

| path | role |
|---|---|
| `fixtures/<chain-id>-<block>.bin` | the witness queue, same naming as every other guest (`1-` = mainnet). Git-ignored per `guests/*/fixtures/` — regenerable, see below. |
| `inputs/` | working directory for ad-hoc generation; `*.bin` git-ignored. |

`inputs/mainnet_25551991_258_24_zec_ziskethone.bin` predates the provenance rule below and does **not**
reproduce its block hash — it was built from an addresses-only witness. It is inert (nothing scans that
naming, only `<chain-id>-<block>.bin`) and git-ignored, kept only as the reproducer for the two guards.

One artifact per block — no `.hints` sibling. Hints are emitted by instrumented Rust guests; the
ziskethone client is a native C++ input checker and `ExecutionClient::emits_hints()` returns `false`
for it, so the hints harness rejects it by design rather than writing an empty file.

## Provenance rule — read this before generating any witness

A ziskethone witness is only valid if it was produced from an execution witness whose `keys` field
carries the **32-byte storage-slot preimages**, not just the 20-byte account addresses. `keys` is
specified as `keccak(address|slot) => address|slot`, and the ZEG0 encoder needs the plaintext slot to
write a storage leaf's `position` field.

Without them the failure is silent and expensive:

1. every storage leaf degrades to an `Op::PhantomLeaf` — no Storages row, `numberOfStorages == 0`
2. `PhantomLeaf` still contributes its hash, so the pre-execution root **still matches** the trusted
   parent anchor and nothing complains
3. `ZiskStateDB::get_storage` reads a missing row as "block-original 0", so every `SLOAD` returns zero
4. execution diverges — reverts, `INVALID` opcodes, ~30 % low `gas_used` — and the only symptom is a
   wrong block hash at the very end of the run

Two guards catch it, and **neither is upstream yet** — they live in
[`cli/vendor-patches/ziskethone.patch`](../../../cli/vendor-patches/ziskethone.patch), against
`ziskethone.git`. Nothing applies that patch for you: `guests/ziskethone/build.sh` checks the
upstream out at the commit its build record pins, and that commit has neither guard. Apply it to
your `vendor/ziskethone` checkout before generating anything, or carry the check yourself.

- the **encoder** (`rust-input-gen/src/state_root.rs`) refuses to write a container when it walked
  storage leaves, indexed none of them, and `witness.keys` holds no 32-byte entry at all — it fails in
  seconds at generation time, exit 1, and prints the preimage counts. Patching it changes no guest
  binary, so this half costs nothing to adopt.
- the **guest** (`cpp-guest/src/state_root.cpp`) fatals before executing a container whose
  `numberOfStorages == 0` while the walk crossed keyless storage leaves — exit 134. This half changes
  the ELF, so a build carrying it will not meet the `elf_sha256` equality in
  `guests/ziskethone/ziskethone.build.json`, and the shipped `ziskethone.elf` does **not** carry it.

Known producers:

| producer | slot preimages | usable |
|---|---|---|
| reth `debug_executionWitness` | yes (`ExecutionWitnessRecord` pushes address **and** slot) | yes |
| `openvm-rpc-proxy` **with** the `keys` fix in `cli/vendor-patches/openvm-eth.patch` | yes | yes |
| `openvm-rpc-proxy` **without** that fix | no — addresses only | no |

`vendor/zisk-eth-client/reth-inputs/` (747 files) was collected through the unfixed proxy: fine for
`zisk-reth`, which re-hashes slots itself and never touches preimages, but unusable here. Both guards
reject those files. The upstream-shipped inputs under
`vendor/zisk-eth-client/bin/guests/stateless-validator-reth/inputs/` came from a real debug node and
are good.

## Reference

All 246 fixtures currently in `fixtures/` reproduce their canonical block hash under the host build of
the guest — each was executed and hash-checked before being adopted, and none is kept otherwise. The
collection is two contiguous runs plus the upstream singletons:

| range | blocks | source |
|---|---|---|
| `25815000..25815199` | 200 | live, patched proxy |
| `25831000..25831030` | 31 | live, patched proxy |
| 15 scattered blocks (24.6M–25.4M) | 15 | upstream-shipped reth inputs, transcoded offline |

Spot check:

```sh
# fixtures are ZiskStdin-framed: u64-LE length prefix, then the ZEG0 container.
# The native host guest wants the bare container, so strip the 8-byte prefix.
tail -c +9 guests/ziskethone/fixtures/1-25831000.bin > /tmp/check.zeg0
vendor/ziskethone/cpp-guest/build/zisk_eth_guest /tmp/check.zeg0 | tail -1
# → 0xd6b042744b1de6a27182abdc63bbc189b2c68703536b7a5a43aade4432f1ce6d
```

Build that host guest with the narrow target — `evmone-standalone`, which is not needed, is the only
thing that fails against macOS's BSD `ar`:

```sh
cmake -S vendor/ziskethone/cpp-guest -B vendor/ziskethone/cpp-guest/build -DCMAKE_BUILD_TYPE=Release
cmake --build vendor/ziskethone/cpp-guest/build -j8 --target zisk_eth_guest
```

## Generate more

**Offline, from an existing reth witness** — no RPC, no quota. Works for blocks too old for a normal
node to serve state, and the only route that costs nothing:

```sh
vendor/zisk-eth-client/tools/reth-to-ziskethone/target/release/reth-to-ziskethone <reth-input-or-dir> -o /tmp/zeg0
# then adopt the harness naming
for f in /tmp/zeg0/mainnet_*_zec_ziskethone.bin; do
  b=$(basename "$f" | cut -d_ -f2); cp "$f" guests/ziskethone/fixtures/1-$b.bin
done
```

**Live, through the patched proxy** (Alchemy has no `debug` namespace, hence the proxy):

```sh
RUST_LOG=info vendor/openvm-eth/target/release/openvm-rpc-proxy --rpc-url "$ALCHEMY_URL" \
  --bind-address 127.0.0.1:8545 --rpc-retry-cu 8300 --rpc-concurrency 128 \
  --preimage-cache-nibbles 7 --witness-cache-dir run-data/wcache
```

`RUST_LOG=info` matters: the proxy's default filter hides even its startup lines, so it looks hung
while it brute-forces 16^7 keccak prefixes into a ~2 GB table (~7 min) before the port answers.

**Set `--rpc-retry-cu` ~17 % below the account's CU/s limit.** Measured on a 10 000 CU/s account:
`--rpc-retry-cu 9000` produced a dashboard reading of **9 700**, so alloy's per-request CU estimate
runs light. 8 300 keeps real consumption under the cap — a 429 that gets retried and lands is billed
twice, so overshooting costs money, not just speed.

Keep `--preimage-cache-nibbles 7`. A smaller table starts faster but misses more preimages, which
means more `eth_getProof` — the wrong trade when paying per CU, and the extra failures break
contiguity.

Then fan out `input-gen` across blocks. Throughput is bounded by the CU ceiling, not by the machine:
measured on 18 cores, load stayed under 6 and the proxy under 2 % CPU throughout, because the workers
sit in network wait.

| workers | CU/s | s/block (wall) | blocks/min |
|---|---|---|---|
| 1 | — | 28.0 | 2.14 |
| 12 | 4 200 | 14.2 | 4.21 |
| 24 | ~5 900 | 10.2 | 5.88 |
| 30 | 9 700 | 8.0 | 7.50 |

Do **not** reach for `cli/witness-farm(-parallel)` to produce these. Its `zisk` arm builds the guest
ELF and generates hints — neither of which a ziskethone fixture needs — and N workers each invoking it
serialise on the cargo target-dir lock (measured: load 55 on 18 cores, one `rustc` busy, six `cargo`
processes asleep, zero blocks produced). Fan out `input-gen` alone instead.

> **Stale cache warning.** `run-data/wcache/witness/` is keyed by block number alone, with no notion of
> format version. Entries dated before 2026-08-25 came from the unfixed proxy and hold addresses-only
> `keys`; re-fetching one of those blocks serves the broken witness straight from cache and the fix will
> look like it failed. Move those entries aside before regenerating.

## Not wired into the harness yet

`cli/guests.registry` has no `ziskethone` row, so `./run`, `cli/gen-witness` and `cli/prove-farm` do
not know this guest — the fixtures above are driven by hand through the host binary. Two pieces are
missing for parity with `zisk-reth`:

- a `guest.sh` under `infra/zisk-infra/guests/ziskethone/`
- a ZisK RISC-V ELF: cross-compile `cpp-guest/zisk` with `toolchain.cmake` (needs the xPack RISC-V
  toolchain that `vendor/zisk-eth-client/setup.sh` installs), then drop it here as `ziskethone.elf`
  alongside a `ziskethone.commit` pin

The **same-commit rule** applies as for `zisk-reth`: a ZEG0 container only matches a guest built from a
ziskethone commit that speaks the same `kVersion` (currently `9`). Note the two checkouts in this repo
have drifted — `vendor/ziskethone` is at `7e6c702`, while
`vendor/zisk-eth-client/third_party/ziskethone` (the `path` dep the tools actually build) is at
`2bb899a`. The encoder file is identical between them; `cpp-guest/` is not.
