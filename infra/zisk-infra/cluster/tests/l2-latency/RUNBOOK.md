# l2-latency — runbook for one run on a rented GPU box

One command times a STARK proof (VADCOP final, no SNARK wrap) of every staged L2 block on seven guest
configurations, plus three mainnet blocks. It answers three questions:

1. **The floor.** How long does a proof of a small L2 block take? That is the latency floor at 50 TPS.
2. **Area into time.** `cargo-zisk execute` puts some configurations at 0.79× the proving area of the
   default. Does the proof get shorter by that much?
3. **Sizing.** For each block interval, how long is a proof, and how many provers keep up with 50 TPS?

The README next to this file describes the design. This file is the procedure.

## 0. What you need

**The bundle.** `l2-latency-bundle.tar.gz` from `~/Documents/zkvms/l2-bench/bundle/` on the Mac.
- It holds 249 inputs and 8 ELFs, built from monad `al/zkvm-l2` 59915cb3e with ZisK 1.3.1-alpha.
- `inputs/provenance.txt` inside it has every ELF's sha256.
- Take its sha256 before copying (§1), so you can check it on the box.

**A box.**

| need | why |
|---|---|
| NVIDIA GPU, **32 GB VRAM or more** per card (reference: RTX 5090) | a healthy worker holds well over 15 GB; zkvm-bench's 1.1 fit was measured on one 5090 |
| root, a working `nvidia-smi`, CUDA visible to the installer | `cargo-zisk --version` must say `[gpu]` after the install |
| Linux with glibc 2.35 or later (Ubuntu 22.04+) | the bundled `zisk-publics` binary; on older systems it is rebuilt after the timing |
| **80 GB free disk** | the proving key needs at least 64 GB, plus results |
| outbound internet | apt, `ziskup`, the 5 GB proving key |
| **3 hours** | up to 60 min of install on a fresh box, about 90 min of proofs |

The GPU count does not matter: one worker drives all the cards. Two boxes with the same card and
driver have differed 3.2× in zkvm-bench, from device-to-host bandwidth that no listing shows. The
run measures it before installing and refuses a starved box (§5).

## 1. Before renting (on the Mac)

```sh
shasum -a 256 ~/Documents/zkvms/l2-bench/bundle/l2-latency-bundle.tar.gz
```

Note the hash. Check that the inputs are the ones staged:

```sh
tar xzOf ~/Documents/zkvms/l2-bench/bundle/l2-latency-bundle.tar.gz zisk-infra/cluster/tests/l2-latency/inputs/provenance.txt
```

## 2. Start it

Optional ten-second look at the box first:

```sh
ssh -p <PORT> root@<HOST> 'nvidia-smi -L; df -h ~ | tail -1; grep PRETTY /etc/os-release; ldd --version | head -1'
```

Then copy the bundle, check it, and start the run:

```sh
scp -P <PORT> ~/Documents/zkvms/l2-bench/bundle/l2-latency-bundle.tar.gz root@<HOST>:~/
ssh -p <PORT> root@<HOST> 'sha256sum l2-latency-bundle.tar.gz'
ssh -p <PORT> root@<HOST> 'tar xzf l2-latency-bundle.tar.gz && bash zisk-infra/cluster/tests/l2-latency/run.sh'
```

The second command must print the hash from §1. The last one returns at once with
`running in the background (pid N)` and the `tail -f` line to follow. The run is detached
(`nohup setsid`), so a dropped ssh session does not stop it.

## 3. Follow it

```sh
ssh -p <PORT> root@<HOST> 'tail -f ~/l2-latency-*/run.log'
```

| step in run.log | duration | a healthy run prints | if not |
|---|---|---|---|
| `1/5 cluster up` | 15-60 min fresh, about 1 min otherwise | the last lines of `up.sh`, ending on a registered worker; the rest is in `$OUT/up.log`: the d2h verdict on a fresh box, the install, `const trees written` or `key already complete` | §5 |
| `2/5 ziskemu replay` | about 1 min | `ziskemu: 249/249 inputs publish their block and run the staged step count` | stop: the bundle or the release is not what was staged |
| `3/5 STARK proofs` | about 90 min | 8 warm-ups (one per ELF), then one `p<pass> <input id> <secs>s rc=0` line per proof, ending `STARK done: 498 proves, 0 failed` | §5 |
| `4/5 every kept proof` | a few min (more if `zisk-publics` has to be built) | `zisk-publics: 249/249 proofs verify and commit to their block` | §5 |
| `5/5 summary` | seconds | `summary.md` printed, then `done — ~/l2-latency-<stamp>.tar.gz` | `summarize.err` |

During step 3, `nvidia-smi` should show every GPU busy. The worker's log is
`~/zisk-infra/cluster/logs/worker.log`. Timings accumulate in
`~/l2-latency-<stamp>/stark-all/timings.csv` as they are taken.

Input ids are `<arm>-<set>-<label>-b<n>`, for example `l2-poseidon-sig-ksw-sweep-d0050-b2` or
`control-preset-wholesale-b1`, plus `mainnet-<block>`.

## 4. Bring the results back, then stop paying

```sh
scp -P <PORT> root@<HOST>:'l2-latency-20*.tar.gz' ~/Documents/zkvms/l2-bench/results/
```

(`20*` skips the bundle, which also starts with `l2-latency-`.) The archive holds everything except
the proofs:
- `summary.md` and the per-proof `timings.csv`, `phases.csv`, `gpu.csv`;
- the Prometheus metrics;
- `precheck.csv` and `publics.csv`;
- the logs, `env.txt`, `inputs.csv` and `provenance.txt`.

Then destroy the instance. To keep it for a rerun instead, stop the cluster first:
`bash ~/zisk-infra/cluster/stop.sh`. A kept box skips the install the next time.

## 5. When something fails

| symptom in run.log | where to read | what to do |
|---|---|---|
| step 1 stops on `THIS IS THE STARVED CASE` / `no terminal to ask on` | `~/topo.log` | The box's d2h is under the screen: below 40 GB/s, or below 0.7 of its h2d. Rent another box; that costs 2 minutes. To run anyway: `FORCE_INSTALL=1 bash zisk-infra/cluster/tests/l2-latency/run.sh`, and keep the d2h figure beside the results. |
| step 1: `only N GB free` | `$OUT/up.log` | Too little disk. Use a bigger one. |
| step 1: `cargo-zisk` reports `[cpu]`, or the install fails | `~/install.log` | CUDA was not visible to `ziskup`. Use another box or image with a working driver. |
| step 1: `the key is unusable` | `~/check-setup.log` | Delete `~/.zisk/provingKey` and rerun: `up.sh` reinstalls what is missing. |
| step 1: no worker registers within 600 s | `$OUT/up.log`, `~/zisk-infra/cluster/logs/worker.log` | Rerun with `FORCE_RESTART=1` in front of the command. A stale worker holding the card is the usual cause. |
| step 2: `BAD <id>` lines | `$OUT/precheck.csv` | The bundle is corrupt or ZisK is not 1.3.1-alpha (`~/.zisk/bin/ziskemu --version`). Recopy and check the hash. Nothing has been timed yet. |
| step 3: `SETUP FAILED for <elf>` | `$OUT/stark-all/logs/setup.*.log` | `all workers failed setup, no VK received` means an incomplete key: see the `the key is unusable` row. |
| step 3: proves with `rc=` not 0 | `$OUT/stark-all/logs/<id>.p<n>.log`, `worker.log` | Out-of-memory or the 1,800 s `PROVE_TIMEOUT`. The summary takes only successful proves; rerun those ids (§6). |
| step 4: a proof that does not verify or commit to its block | `$OUT/publics.csv` | That input's time is not a time of that block. Report it; do not use that row. |
| the ssh session dropped, the box restarted mid-run | — | A dropped session changes nothing. After a restart, rerun the command: the install persists on disk, and a new results directory is created. |

## 6. Options

Each option goes in front of the `bash .../run.sh` command:

| setting | effect |
|---|---|
| `GPU_SETS="1 all"` | on a multi-GPU box, also time a worker on one GPU (it restarts between sets; about double the proof time) |
| `PASSES=1` | one timing per input instead of two; about half the proof time, noisier |
| `ONLY='<regex>'` | only the input ids that match, e.g. `ONLY='^(l2|l2-poseidon-sig-ksw)-sweep'` or `ONLY='d0(001|050|100)-'` |
| `OUT=<dir>` | results directory (default `~/l2-latency-<stamp>`) |
| `L2LAT_FG=1` | stay in the foreground |
| `FORCE_INSTALL=1`, `FORCE_RESTART=1` | see §5 |

## 7. Reading the results

**The arms.** `l2` is the reference. Every other L2 arm proves the same blocks, so each ratio in
`summary.md` is the same transactions proven two ways.

| arm | guest | blocks |
|---|---|---|
| `l2` | the L2 as built by default: keccak tries and signatures, Keccak-f precompile, JUMPDESTs in software | 38 |
| `l2-precompile` | `l2` with the JUMPDEST precompile | 38 |
| `control` | `l2` with the plaintext cipher suite: no encryption | 38 |
| `l2-keccak-sw` | `l2` with every Keccak-f in software | 24 (up to 250 tx) |
| `l2-poseidon` | tries on Poseidon2, the remaining keccak on the precompile | 38 |
| `l2-poseidon-ksw` | tries on Poseidon2, the remaining keccak in software | 32 (up to 1,000 tx) |
| `l2-poseidon-sig-ksw` | tries and signatures on Poseidon2, the remaining keccak in software | 38 |
| `mainnet` | the mainnet guest on three `r10zisk-rtp` blocks | 3 |

**What to compare the time ratios with.** These are proving-area ratios against `l2`, from the
instance plans `cargo-zisk execute` prints. An area is 2^nBitsExt × columns plus the compressor,
from the proving key's starkinfo. Each figure is the median of the two blocks the run proves at
that size. If proof time follows area, the sweep table's ratios land near these:

| block | tx | `l2` area | precompile | control | keccak sw | Poseidon2 trie | trie + keccak sw | trie + signatures + keccak sw |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sweep | 1 | 9.55 G | 1.03 | 0.97 | 0.79 | 1.00 | 0.79 | 0.79 |
| sweep | 10 | 9.55 G | 1.03 | 0.97 | 0.79 | 1.00 | 0.79 | 0.79 |
| sweep | 25 | 9.55 G | 1.03 | 0.97 | 0.79 | 1.00 | 0.79 | 0.79 |
| sweep | 50 | 9.55 G | 1.03 | 0.97 | 0.85 | 1.00 | 0.79 | 0.79 |
| sweep | 100 | 9.55 G | 1.03 | 0.97 | 1.20 | 1.00 | 0.79 | 0.79 |
| sweep | 250 | 9.74 G | 1.03 | 0.95 | 1.62 | 1.03 | 0.82 | 0.82 |
| sweep | 500 | 10.07 G | 1.03 | 0.94 | – | 1.07 | 0.93 | 0.88 |
| sweep | 1,000 | 15.93 G | 1.02 | 0.80 | – | 0.96 | 0.93 | 0.87 |
| sweep | 2,000 | 25.04 G | 1.01 | 0.86 | – | 0.85 | – | 0.81 |
| sweep | 5,000 | 48.29 G | 1.01 | 0.88 | – | 0.81 | – | 0.78 |
| wholesale | 21 | 9.55 G | 1.03 | 0.97 | 0.79 | 1.00 | 0.79 | 0.79 |
| wholesale-cbdc | 21 | 9.55 G | 1.03 | 0.97 | 0.85 | 1.00 | 0.79 | 0.79 |
| worker-payouts | 130 | 11.57 G | 1.02 | 0.98 | 2.41 | 0.87 | 1.03 | 1.03 |
| payouts | 500 | 10.07 G | 1.03 | 0.94 | – | 1.05 | 0.91 | 0.88 |

A dash is a size the arm does not prove.

**Where the answers are in `summary.md`.**

1. **The floor:** the `l2` row of the fit table (`fixed s`), and the sweep table's seconds at 1 to
   100 transactions. The `l2` area is the same 9.55 G from 1 to 100 transactions, so those times
   should be nearly equal. A clear climb with size means something other than the plan's area
   dominates there.
2. **Area into time:** the sweep table's ratio columns, against the table above.
3. **Sizing at 50 TPS:** the sizing table, built from the `l2` arm's fit. For another arm, scale the
   proof time by its measured ratio at that block size.

**Read the box before the numbers.** Look for the d2h figure in `up.log` (or `~/topo.log`) and the
`gpu.csv` utilisation. A "marginal" verdict or low utilisation makes absolute times specific to this
box. The ratios between arms are the robust result: every arm ran on the same box, in alternating
order within each block.
