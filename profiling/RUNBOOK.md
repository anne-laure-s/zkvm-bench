# profiling — runbook

This file is the shortest path from a checkout to a profiling artifact. Commands run from
`profiling/` unless a `cd` says otherwise.

- Which tool to use: [`README.md`](README.md)
- Cache keys and reuse: [`cache-format.md`](cache-format.md)
- Fresh-clone r10 setup: [`series/RUN-R10.md`](series/RUN-R10.md)
- Detailed hotspot interpretation: [`hotspots.md`](hotspots.md)

## Rules that prevent plausible but wrong reports

1. Compare work only inside one zkVM: ZisK steps and SP1 cycles are not comparable.
2. Pin the block range. An unbounded run uses whatever inputs happen to be on disk.
3. Check that both ELFs can read the selected Monad witness generation.
4. Treat the report's `n` as the block set actually measured. Missing side inputs reduce it.
5. Keep measured results, projections and historical findings separate.
6. A cache hit requires matching ELF and input contents; filenames do not identify a measurement.

`compare.py` can skip an unavailable axis and still finish. Use `axis.py list`, read the final skip
summary, and inspect `n`. The r10 driver is stricter: `series/run-r10.sh --check` validates its whole
workflow before any expensive work.

## Prerequisites

| Workflow | Required locally |
|---|---|
| ZisK comparison/profile | `~/.zisk/bin/ziskemu` |
| SP1 comparison | `infra/sp1-infra/sp1-runner/target/release/sp1-runner` |
| SP1 hotspot/deep/family profile | profiling build under `target-prof/release/` |
| Monad comparison | Monad witnesses and the two guest ELFs |
| r10 comparison and series | prerequisites and artifacts listed in [`series/RUN-R10.md`](series/RUN-R10.md) |

Install the runners from the repository root:

```bash
# ZisK: the r10 campaign is pinned to 1.1.0-alpha
curl https://raw.githubusercontent.com/0xPolygonHermez/zisk/main/ziskup/install.sh | bash
~/.zisk/bin/ziskup --version 1.1.0-alpha --nokey -y
~/.zisk/bin/ziskemu --version

# SP1: normal and profiling builds are distinct
cd infra/sp1-infra/sp1-runner
cargo build --release
cargo build --release --no-default-features \
  --features profiling --target-dir target-prof
cd ../../../profiling
```

The ZisKethone reference ELF is tracked at `guests/ziskethone/ziskethone.elf`. Monad ELFs and large
corpora are git-ignored. Copy them from the artifact host or generate them through
[`infra/monad-witness/`](../infra/monad-witness/README.md).

## Current r10 report

The supported end-to-end command is the series driver: it gates the tip before publishing compare,
then continues with the historical series.

```bash
./series/run-r10.sh --check
./series/run-r10.sh
```

If the tip ELF and its index already exist and you intentionally want only the comparison, run:

```bash
./compare.py --block-min 25815000 --block-max 25815199 \
  --axis r10tip-vs-ziskethone
```

This direct command does not run the root gate. It writes `results/compare.{html,json}` and
`results/compare-summary.{html,md}`. Both bounds are required to keep the canonical 200-block set.

## Compare two versions of the guest, A to Z

The result is one axis per backend and one `compare.py` invocation. If the witnesses and ELFs are
already available, start at step 2.

### 1. Obtain witnesses and ELFs

`witness-backfill` explicitly produces both Monad guest ELFs as well as the witness generation. Run
it from the producer directory:

```bash
cd ../infra/monad-witness
export HOST=<devcore-host>

./witness-backfill plan al/zkvm-r10
BLOCKS=25815000-25815199 ./witness-backfill run al/zkvm-r10
./witness-backfill status
```

> `run` is destructive on the remote box: it recreates its triedb from a snapshot and leaves the
> box on the backfilled range. Stop the RTP producer first. Always inspect `plan` before `run`.

If the trie for this range is already loaded, `again` can avoid the snapshot reload:

```bash
AGAINST=canonical-2026-08-25815000-25815199-d49075fa3 \
  ./witness-backfill again al/zkvm-r10
```

The resulting generation contains:

```text
guests/monad/gen/<GEN>/witnesses/
guests/monad/gen/<GEN>/elf/monad-zkvm-guest-zisk.elf
guests/monad/gen/<GEN>/elf/monad-zkvm-guest-sp1.elf
guests/monad/gen/<GEN>/PROVENANCE.md
```

To compare old and new directly, keep both pairs of ELFs. The selected generation supplies the one
shared witness corpus. If an old ELF has no generation, store it under
`guests/monad-variants/<name>/`; do not overwrite the new ELF.

### 2. Select the witness generation

```bash
cd ../../guests/monad
./use-gen
./use-gen <NEW-GEN>
```

`use-gen` points `fixtures` at that generation and installs its top-level ELFs. It changes tracked
ELF files, so expect `git status` to show them.

Before measuring, execute one non-first block with the old ZisK ELF against the selected generation:

```bash
b=$(basename "$(ls -1 fixtures/*.witness | LC_ALL=C sort | tail -1)" .witness)
mkdir -p /tmp/monad-format-check
ln -sf "$PWD"/fixtures/$b.* /tmp/monad-format-check/
ELF=gen/<OLD-GEN>/elf/monad-zkvm-guest-zisk.elf \
  WIT=/tmp/monad-format-check ./ev.sh
```

Accept `PASS` or `PASS(pv3)`. Stop on `MISMATCH`, `EMU-FAIL` or `SHORT`: the old guest cannot safely
be compared on this generation.

`.expected_pv` files are optional. Execution, profiling, the root gate and `compare.py` do not need
them. They only strengthen `ev.sh` from a post-root check to a positional public-values check. To add
them:

```bash
./gen-expected-pv.py gen/<GEN>/witnesses
```

### 3. Declare the axis

Ratios are `a / b`; use the new build as `a` and the old build as `b`.

```bash
cd ../../profiling

./axis.py add r10-vs-r9 --backend zisk \
  --a-name monad-r10-zisk \
  --a-elf guests/monad/gen/<NEW-GEN>/elf/monad-zkvm-guest-zisk.elf --a-src monad \
  --b-name monad-r9-zisk \
  --b-elf guests/monad/gen/<OLD-GEN>/elf/monad-zkvm-guest-zisk.elf --b-src monad

./axis.py add r10-vs-r9-sp1 --backend sp1 \
  --a-name monad-r10-sp1 \
  --a-elf guests/monad/gen/<NEW-GEN>/elf/monad-zkvm-guest-sp1.elf --a-src monad \
  --b-name monad-r9-sp1 \
  --b-elf guests/monad/gen/<OLD-GEN>/elf/monad-zkvm-guest-sp1.elf --b-src monad
```

`axis.py` edits the tracked `AXES` declaration in `compare.py`. Review that diff. Commit durable axes;
remove one-offs with `./axis.py rm <name>`. Removing an axis does not remove cached measurements.

For a reth side, use `--src bin` and make its name match the guest directory:

```bash
./axis.py add r10-vs-reth --backend zisk \
  --a-name monad-r10-zisk --a-elf <MONAD-ELF> --a-src monad \
  --b-name zisk-reth --b-elf guests/zisk-reth/zisk-reth.elf --b-src bin
```

### 4. Measure and generate `compare.html`

```bash
./axis.py list
./compare.py --block-min <FIRST> --block-max <LAST> \
  --axis r10-vs-r9 --axis r10-vs-r9-sp1
```

Outputs:

- `results/compare.html`: full report
- `results/compare.json`: per-block data and summaries
- `results/compare-summary.html` and `.md`: short synthesis

Useful variants:

```bash
./compare.py --blocks 25815000-25815009 --axis r10-vs-r9  # explicit sample
./compare.py --quick --axis r10-vs-r9                     # skip new ZisK COST collection
./compare.py --deep 5 --axis r10-vs-r9                    # add module-level attribution
./compare.py --summary-from results/compare.json          # render only; measure nothing
```

`--blocks` and `--limit` write `compare-partial.*` by default. `--quick` still reports COST already
in cache. `--force` is the explicit way to ignore reusable measurements.

## Series — one ELF per commit of a lineage

`compare.py` prices two guests against each other. A series builds every commit and compares each
point with its predecessor.

For r10, use the driver rather than the individual stages:

```bash
./series/run-r10.sh --check          # validate everything; start nothing
./series/run-r10.sh                  # full 200-block campaign
./series/run-r10.sh --nb-block 105   # deterministic nested sample for the series only
./series/run-r10.sh --skip-build     # reuse valid builds; build missing/new commits
```

Fresh clone: follow [`series/RUN-R10.md`](series/RUN-R10.md) exactly. In particular, the driver needs
a clean dedicated Monad worktree and two toolchains:

- `SERIES_STOCK_TOOLCHAIN_DIR` for commits before DMA;
- `SERIES_TOOLCHAIN_DIR` for the patched GCC 15.2.0 lineage.

The driver does not need a caller-supplied `BUILDFIX`. Historical flags come from the tracked
`series/r10-buildenv.tsv`; `{toolchain}` and `{commit}` are expanded per build.

Its order is fail-closed:

1. validate prerequisites and the exact 200-block corpora;
2. freeze and build/reuse the remote-tracking tip;
3. run the full root gate;
4. generate `compare.html`;
5. build and measure the remaining lineage;
6. generate `series-r10.html` only if the lineage is complete.

`--nb-block N` uses a stable, representative, nested sample. It never narrows the full root gate or
the 200-block compare. Series measurements reuse compatible entries from the compare cache and are
also stored in `r10-measure.tsv`, keyed by ELF SHA and block.

Without `--skip-build`, every Monad commit is rebuilt; the tip is still built only once in that run.
With it, a build is reused only when its index row is `OK`, its ELF exists and its recorded build
environment matches the current recipe. New commits and previous failures are built.
An incremental run checkpoints each validated row under `series/elf/`; after an interruption, the
next `--skip-build` resumes from that checkpoint while the published index remains complete and
atomic.

The run displays progress normally and appends the same output to `series/run-r10.log`.

## Axes and block selection

An axis is a named same-zkVM pair. Inspect and maintain them with:

```bash
./axis.py list
./axis.py show <axis>
./axis.py rm <axis>
./axis.py prune             # preview removal of ephemeral axes
./axis.py gc                # preview removal of axes whose known build was deleted
```

Input lookup is derived from each side's `src`:

| `src` | Input lookup |
|---|---|
| `monad` | `guests/monad/fixtures/<block>.witness`, then `inputs/` |
| `bin` | `guests/<side-name>/fixtures/1-<block>.bin`, then `inputs/` |

The candidate universe comes from Monad witnesses; an axis keeps blocks for which both sides resolve
an input. Therefore a missing `.bin` can reduce `n`, and a wrong `bin` side name can reduce it to
zero. Check `axis.py list` before a long run.

## Witnesses

### Monad

Monad witnesses are produced by a Monad node, not by `cli/gen-witness`. Use
[`infra/monad-witness/witness-backfill`](../infra/monad-witness/README.md). A generation owns its
witnesses, both ELFs and provenance; never mix files between generations.

```text
guests/monad/gen/<generation>/witnesses/<block>.witness
guests/monad/gen/<generation>/witnesses/<block>.post_state_root
guests/monad/gen/<generation>/witnesses/<block>.expected_pv   optional
guests/monad/gen/<generation>/elf/
guests/monad/gen/<generation>/PROVENANCE.md
```

### ZisKethone

ZisKethone inputs are `ZEG0` containers under
`guests/ziskethone/fixtures/1-<block>.bin`. They are transcoded from compatible reth witnesses or
created through the patched RPC proxy; `witness-farm` is not their producer. Follow
[`guests/ziskethone/inputs/README.md`](../guests/ziskethone/inputs/README.md).

### reth guests

From the repository root:

```bash
./cli/gen-witness --guest zisk-reth --block 25815000
./cli/gen-witness --list
ALCHEMY_URL=<archive-rpc> ./cli/witness-farm 25815000
```

`rsp`, `zisk-reth` and `openvm-reth` use their own inputs. RPC gaps are possible; they appear as a
smaller axis `n`.

## Hotspots — where the difference is

Use [`hotspots.py`](hotspots.py) after `compare.py` tells you how large the gap is. Full options and
interpretation are in [`hotspots.md`](hotspots.md).

```bash
# One build
./hotspots.py profile --backend zisk --elf <ELF> -i <FRAMED-INPUT> \
  --out results/profile

# Before/after
./hotspots.py compare --backend zisk \
  --elf-before <OLD-ELF> --elf-after <NEW-ELF> \
  -i <FRAMED-INPUT> --out results/diff

# Existing profiles: no execution
./hotspots.py diff --json results/a/profile.json --json results/b/profile.json
```

SP1 reads a raw Monad witness. ZisK needs `LE64(length) + witness + pad-to-8`; use a framed file from
`guests/monad/execute-out/` or let `compare.py` do the framing.

## Other artifacts

```bash
./levers-r10.py                     # current r10 levers report
./levers.py                         # old reth-based campaign only; requires n >= 300
./results.py                        # results/results.html from exec-report.json files
./inline-robust.py --verdict-only   # results/inline-verdict.json from cached profiles
./optimized.py                      # frozen campaign only; unavailable on a fresh clone
./compare-optimized.py              # frozen campaign only
```

The optimized-branch scripts read a git-ignored frozen measurement set and cannot regenerate it.

## Less common workflows

Plan an arbitrary set of branch/guest pairs from the repository root:

```bash
./cli/bench-pairs --host <devcore-host> \
  --pair al/zkvm-r10:al/zkvm-r9:zisk
```

Planning is the default and changes nothing; add `run` only after reviewing it. A run may invoke the
destructive witness-backfill workflow.

Build or verify a guest explicitly:

```bash
cd ..
./cli/gen-elf --guest ziskethone -- --check
./cli/build-monad --name monad-r10-zisk --branch al/zkvm-r10
./cli/gen-elf --guest monad-r10-zisk
cd profiling
```

`build-monad` uses the guest tree's official profile; do not pass a separate `BUILDFIX` or restate
its compiler flags. Pre-official-profile commits belong in a series build environment table.

Other retained utilities:

```bash
./rtp-latency.py --help
./studies/elf-equiv.py
./studies/dwarf-tax.py
```

## Troubleshooting checklist

Before trusting a surprising result, check:

```bash
pwd
git status --short
./axis.py show <axis>
sha256sum <ELF>                     # macOS: shasum -a 256 <ELF>
```

Then verify:

- the intended Monad generation is selected;
- the axis points to the intended ELF paths and backend;
- the report names the expected commit/ELF SHA;
- `FIRST`, `LAST` and per-axis `n` match the intended corpus;
- no axis was skipped;
- the root/public-output gate passed where the workflow requires one;
- a number described as measured really came from the persisted JSON/TSV artifact.
