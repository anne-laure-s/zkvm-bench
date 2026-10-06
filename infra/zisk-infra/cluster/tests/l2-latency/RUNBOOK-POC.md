# The PoC on 1, 2 and 4 GPUs — runbook

One box with four GPUs and one key: the 50-tx PoC key with FROPS made for L2 blocks (`poc50f`,
RUNBOOK §8). The run builds the key on the box, then times the same blocks with the worker on 1,
then 2, then 4 of the box's GPUs. The same arms on ZisK 1.3.1's own key are one more command on
the same box (§8): the anchor's cost differs between the two keys, which is why both are worth it.

Everything is three commands on the Mac. `poc-box.sh` does the copying and starts `poc-run.sh` on
the box, which does the rest.

## 0. What is timed

The sequencing anchor's hash, on the chain with everything on Poseidon2 (`al/zkvm-l2` 50d54f6ef,
where the anchor is keccak256 -- "vanilla" -- and `al/l2-anchor-poseidon2`, where it is the chain's
hash), and the Keccak-f path under each:

- **Six arms.** `kanchor-ksw` is vanilla as the bench runs it: keccak256 anchor, Keccak-f in
  software. `kanchor-kpre` is the same with Keccak-f on the precompile (and its memo).
  `p2anchor-ksw` and `p2anchor-kpre` are the Poseidon2 anchor the same two ways.
  `p2anchor-addsw` adds EVM ADD/SUB in software (no Add256), `p2anchor-nodma` no DMA operation
  either. Every arm proves the same blocks: the two trees generate the same witnesses byte for
  byte, and only the sequencing anchor a proof must publish differs.
- **Seven sizes.** Payouts blocks of 1 (three blocks), 10, 25, 50, 100, 250 and 1,000
  transactions on a 1,000,000-account state: 54 inputs. The bundle also holds 2,000 and 5,000,
  and two blocks of each preset (`ONLY` to add them, §7).
- **Three passes** per input on each GPU set, after a warm-up per ELF. Every proof is a STARK
  (VADCOP final, no wrap), verified after the timing pinned to the PoC key, and must publish its
  block's chain id, number, both state commitments and both anchors.

What the plans predict under this key (leaves of the recursion tree, proving area with compressors
and recursion; `results/anchor-cmp/SUMMARY.md` on the Mac):

| tx | `kanchor-ksw` | `kanchor-kpre` | `p2anchor-ksw` | `p2anchor-kpre` | `p2anchor-addsw` | `p2anchor-nodma` |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 17, 5.33 G | 18, 6.47 G | 17, 5.33 G | 18, 6.47 G | 16, 5.18 G | 13, 4.58 G |
| 50 | 17, 5.49 G | 18, 6.57 G | 17, 5.43 G | 18, 6.57 G | 16, 5.28 G | 13, 4.69 G |
| 100 | 22, 7.46 G | 22, 8.45 G | 21, 7.18 G | 22, 8.45 G | 20, 7.03 G | 18, 6.45 G |
| 250 | 33, 11.86 G | 30, 11.59 G | 30, 10.80 G | 31, 12.08 G | 29, 10.66 G | 29, 11.07 G |
| 1,000 | 95, 35.04 G | 80, 29.65 G | 81, 29.56 G | 81, 30.00 G | 80, 28.97 G | 82, 31.65 G |

The table at the end of the run gives each arm over `kanchor-ksw`, block for block. The key's
FROPS were tuned on the previous base's ELFs; on these they may cover less, which costs area and
never soundness. Its ROM is 1.3.1's own height, 2^22 (the PoC tree 8fa8a2af7): the guest's new
interpreter transpiles to 2.4 M ROM instructions, which the PoC's earlier 2^20 refused before
proving a row. That costs 0.18 G in every proof, counted in the table.

## 1. The box

| need | why |
|---|---|
| **4 GPUs**, 32 GB VRAM or more each (RTX 5090), power limit at the default (575-600 W) | the 1-, 2- and 4-GPU sets; the first box was capped at 450 W and was about 10 % slower for it |
| a **CUDA `-devel` image, 12.8 or later**: `nvcc` present | the PoC is built from source with the GPU prover, for ZisK's major archs up to sm_120 (RTX 50xx), which older nvcc cannot target |
| Ubuntu 22.04 or later | glibc 2.35 for the bundled `zisk-publics` |
| **120 GB free disk** (200 GB with `POC_KEYS="poc50f poc50"`) | the tree, its key and the key's GPU constant trees: about 70 GB a key |
| 64 GB RAM or more | the worker keeps every ELF's ASM services resident: six ELFs here |
| about **2 hours** | ~45 min of build and setup, a few minutes of constant trees, about an hour of proofs |

## 2. On the Mac, once

```sh
P="bash $HOME/Documents/zkvms/l2-bench/zkvm-bench-l2lat/infra/zisk-infra/cluster/tests/l2-latency/poc-box.sh"
```

The bundle (`l2-bench/bundle/l2-latency-bundle-poc.tar.gz`) and the key's tree
(`l2-bench/poc/dist50f/zisk-poc-src.tar.gz`) are already built. A changed tree is repacked with
`ZTREE=zisk50f poc/poc-pack.sh`.

## 3. Run it

With `<host>` and `<port>` as vast.ai prints them (`ssh -p <port> root@<host>`):

```sh
$P check <host> <port>
$P start <host> <port>
$P log <host> <port>
```

- `check` takes ten seconds and copies nothing. It wants four GPUs, nvcc 12.8 or later and the
  disk above.
- `start` copies the bundle (208 MB) and the tree (16 MB), checks their sha256 on the box, and
  starts `poc-run.sh`, detached. A second `start` copies only what changed: a box kept from a
  previous run skips its install too.
- `log` follows the run. Ctrl-C stops following, not the run.

To see the plan on the box first without running anything, put `DRY_RUN=1` in front of `start`.

## 4. What a healthy run prints

| step in the log | duration | healthy | if not |
|---|---|---|---|
| `PoC run: keys [poc50f] …` | seconds | four GPUs listed, `54 inputs selected`, `poc50f: to install`, the disk line | §6 |
| `1/3 d2h on the idle box` | ~2 min | `good <mode> d2h … ratio …` | `bad`: rent another box |
| `2/3 building and setting up poc50f` | ~45 min | `poc50f: done — … of key` (details in `~/poc50f-install.log`) | §6 |
| `3/3 STARK proofs`, then run.sh's `1/5` to `5/5` for the key | ~45 min | `1/5 cluster up` with const trees written once; per GPU set a worker restart, then per ELF a `warm` line and `p1`…`p3` lines with `rc=0`; `zisk-publics: N/N proofs verify under …/provingKey/…` | §6 |
| the 1/2/4-GPU table | — | one table per arm (§5) | `poc-summary.py` on the archive, on the Mac |

## 5. Bring it back

```sh
$P fetch <host> <port>
```

This copies to `l2-bench/results/gpu/`:
- `l2-latency-<stamp>-poc50f.tar.gz`: everything run.sh keeps, without the proofs;
- `poc-summary-<stamp>-poc50f.md`: the table;
- `poc-run-<stamp>.log`.

Then destroy the instance. To keep it, stop the cluster first (fetch prints the command).

The table gives, per arm and block size, the median proof time on 1, 2 and 4 GPUs, the speed-up
over one GPU, and the GPUs a 50-TPS chain needs at that block size when each prover takes the
next block as soon as it is done. To read it against the release, put the release's 1/2/4-GPU
runs beside it:

```sh
python3 poc-summary.py l2-latency-20261002T170216Z l2-latency-20261002T172543Z
```

Different boxes differ by their d2h and CPU (§7 of RUNBOOK.md), so read the release-to-PoC ratios
with that in mind. The `d2h` line of the log is the figure to compare.

## 6. When something fails

| symptom | where to read | what to do |
|---|---|---|
| `no nvcc` | — | The image has the driver but not the toolkit. Rent a CUDA `-devel` image. |
| `nvcc 12.x is too old` | — | The same, with CUDA 12.8 or later. |
| `too little disk` | — | A bigger disk. `MIN_FREE_GB` lowers the bar, on your own judgement. |
| `THIS IS THE STARVED CASE` | `~/topo.log` | Rent another box (2 minutes lost). `FORCE_INSTALL=1` goes on, but keep the d2h figure with the results. |
| `<key> did not install` | `~/<key>-install.log`, then `~/zisk-<key>-build.log` or `~/zisk-<key>-key.log` | The build (CUDA, missing package) or the key's setup (network: it fetches pil2-proofman). Fix, then `start` again: it reinstalls only what is missing. |
| `1/5 cluster up` fails | `~/l2-latency-<stamp>-<key>/up.log`, `~/check-setup.log` | `the key is unusable`: delete `~/.zisk-<key>` and `start` again. |
| proofs with `rc=` not 0, `mmap(rom) errno=11` in `worker.log` | `~/zisk-infra/cluster/logs/worker.log` | The memlock cap: the install patched it and built `~/nolock.so`; check both exist (`grep map_locked_flag ~/.zisk-<key>/zisk/emulator-asm/src/globals.c`). |
| `1/5 cluster up` waits 600 s and gives up although `worker.log` shows the worker registered | `up.log` | up.sh calls a worker healthy from 15,000 MiB of VRAM held on the box, the release worker's size. A PoC worker on one GPU should hold more (its streams are floored at the largest compressor, ~6 GB each), but if it does not, `VRAM_FLOOR_MIB=4000` in front of `start`. |
| the 2- or 4-GPU worker dies at startup | `worker.log` | The stack: `poc-run.sh` already sets `RUST_MIN_STACK=67108864 OMP_STACKSIZE=64M`; rerun with larger values in front of `start`. |
| a proof that does not verify | `publics.csv` | That row is not a time of that block: report it, do not use it. |
| the session or the box dropped | — | `start` again: copies and installs are skipped when already done, and a new results directory is made. |

## 7. Options

All are given in front of `$P start`:

| env | default | effect |
|---|---|---|
| `POC_KEYS` | `poc50f` | `"poc50f poc50"` also times the 50-tx key with the release's FROPS (the FROPS effect, one leaf); both install at once, ~40 min more of proofs |
| `GPU_SETS` | `1 2 4` | e.g. `"4"` for four GPUs only, `"1 2"` on a 2-GPU box |
| `PASSES` | `3` | passes per input and GPU set |
| `ONLY` | the 54 inputs above | a regex on input ids, e.g. `'^p2anchor-nodma-'` for one arm, `'-sweep-|-preset-'` for everything staged |
| `DRY_RUN` | — | `1` prints the plan on the box and runs nothing |
| `SKIP_TOPO` | — | `1` skips the d2h measurement |
| `VRAM_FLOOR_MIB` | `15000` | up.sh's VRAM sign of a live worker, see §6 |

## 8. The same arms on the release key

The plans differ between the keys -- under 1.3.1's the keccak anchor in software changes no plan
up to 250 transactions, under the PoC's it does from 100 -- so the release's timing of the same
inputs is the other half of the comparison. After `fetch`, on the same box (`run.sh` installs
ZisK 1.3.1 and its key itself: about 80 GB more disk and up to an hour):

```sh
ssh -p <port> root@<host> 'ONLY="-sweep-d(0001-b[123]|0010-b1|0025-b1|0050-b1|0100-b1|0250-b1|1000-b1)$" GPU_SETS="1 2 4" PASSES=3 bash zisk-infra/cluster/tests/l2-latency/run.sh'
```

It detaches and leaves `~/l2-latency-<stamp>.tar.gz`, which `$P fetch` brings back with the rest.
Run `poc-summary.py` on each unpacked archive on its own -- given both, it would merge their
GPU sets of the same name -- and read the two tables against each other.

