# tests/ — why our 16×5090 is ~1.35× slower than ZisK's own cluster

Five experiments, in order, each isolating one suspect. They exist because the gap is **config and
hardware, not code**: we run the release ZisK's own live cluster runs, pinned in `00-install-once.sh`
(`ZISK_VER`). The measurements below are on `v1.0.0-alpha`; `v1.1.0-alpha` (2026-08-18) ships
proving-performance changes, so a re-run on it moves both sides of the comparison.

**The gap, stated precisely** (so no test has to re-derive it): ethproofs' live `Zisk 16x5090`
averages **8.2 s** per mainnet block. Our fit `prove_secs = 2.12 + 0.0364 × Msteps` puts us at
**11.0 s** on the mean block (244 Msteps). That is **1.35× on the total**, **1.43× on the GPU-bound
part**. Full reasoning in `AGENT-NOTES.md` at the repo root.

## Order, cost, and what each one decides

| # | script | cost | decides |
|---|--------|------|---------|
| 0 | `t0-gonogo.sh` | ~2 s, **needs nothing installed** | Is this rented box even usable? Which `GPUS=` can t1 use here? |
| 3 | `t3-topo.sh` | ~2 min, **read-only** | Is host↔device bandwidth a ceiling? Can this box lock memory at all? |
| 1 | `t1-numa.sh` | 35–45 min | Does the `[ALARM] 8/16 NUMA-local GPUs` cost throughput? **Also the 8-GPU bring-up.** |
| 2 | `t2-tune.sh` | 35–45 min | What do `--compute-capacity 10` (the default) and auto `--max-streams` cost? |
| 4 | `t4-memlock.sh` | 20–30 min (+ a ROM rebuild) | Is stripping `MAP_LOCKED` free, as `nolock.c` claims? |
| 5 | `t5-mpi.sh` | 45–60 min | Does upstream's NUMA-bound multi-rank layout beat our single process on 16 GPUs? |

**On a rented box, run `t0-gonogo.sh` before installing anything.** vast.ai listings do not show how
the GPUs split across NUMA nodes, and that single fact decides which `GPUS=` t1 can use — or whether
t1 can run at all. Finding out after `00-install-once.sh` costs 30–60 minutes and 30 GB; finding out
at boot costs cents. It is self-contained: scp that one file, or paste its body.

**Then run t3** even though it is numbered 3 — it costs nothing, starts nothing, and its answers
change how you read the other four. Then t1, because it is the strongest hypothesis *and* the config
we want to ship. t2 only makes sense with t1's GPU set pinned. t4 and t5 both want a privileged or
bare-metal box and will tell you politely when they do not have one.

## Prerequisites

Everything from `../README.md` steps 3–4: the box installed (`00-install-once.sh`), the ELF at
`~/zisk-reth.elf`, witnesses at `~/1-<block>.{bin,hints}`, and **the coordinator running**:

```bash
cd ~/zisk-infra/cluster && ./start.sh
```

The tests replace the *worker* between arms and leave the coordinator alone — it caches the ELF's
proving keys, and restarting it would make every arm pay `remote setup` again. Each one calls
`restore_default_worker` on the way out, so the box is left with a normal all-GPU worker up.

## Reading the results

Every test writes `~/tests/<name>-<utc>/` containing `results.csv` (one row per pass per block, the
canonical schema in `lib.sh`), per-arm `*.worker-startup.log` and `*.worker.log`, per-block prove
logs, and `*.gpuutil.csv`. Then:

```bash
bash tests/summary.sh ~/tests/t1-numa-*/results.csv
```

`summary.sh` computes Msteps/s and per-GPU throughput — those are **not** stored in the CSV, so
fixing the arithmetic never costs a re-run. It prints reference points (ZisK's 8.2 s, our fit, the
mainnet distribution, the 1500 MHz asm figure) next to your numbers. Pass several `results.csv` at
once to compare across runs, as long as the arm names differ.

Repatriate to the Mac with the existing helper — **with `KEEP_REMOTE=1`**, because `fetch-runs.sh`
deletes the source dir on success, and pointed at `tests` that would take every run with it, not
just the one you fetched:

```bash
KEEP_REMOTE=1 ./cluster/fetch-runs.sh $REMOTE:$PORT tests
```

## Things that will bite you

- **`PASSES=2` by default, and the "median" of two is the lower one.** That is deliberate on a shared
  host: the faster pass is the one less polluted by a neighbour. Raise `PASSES` when the box is quiet.
- **Every arm pays a full worker registration** — 4–6 min on 8 GPUs, 8–12 min on 16 (30 GB allocated
  per GPU). This is what makes a "quick sweep" three hours. `REG_TIMEOUT=1200` covers it. Each test
  prints its plan (arms × proofs × registrations) before starting, and registration logs progress
  every 30 s with peak VRAM — the silence during allocation is normal, not a hang.
- **Ctrl-C is safe but does not restore the worker.** It stops the GPU sampler and prints the one
  command to put a default worker back. Restoring automatically would cost another registration,
  which is not what someone interrupting wants; being *left* on a test config silently would be worse,
  hence the message. `t4-memlock.sh` is the exception — it always reverts its `globals.c` patch.
- **`CUDA_DEVICE_ORDER=PCI_BUS_ID` is exported by `lib.sh`.** CUDA's default order is
  `FASTEST_FIRST`; without pinning it, `CUDA_VISIBLE_DEVICES=0..7` could select a different eight
  GPUs than the ones resolved from sysfs, and the whole NUMA experiment would be measuring nothing.
- **`nvidia-smi` ignores `CUDA_VISIBLE_DEVICES`.** So does upstream's `mpi_params.sh`, which sizes
  `-np` from `nvidia-smi -L`. That is why t5 runs on all GPUs rather than a restricted set.
- **`numa_node = -1` is not node 0.** It means the kernel exposes no affinity — NUMA off in BIOS, or
  the container hiding it. t1 stops rather than pretending, since the split arm would be a fiction.
- **t4 reverts a source patch.** It restores `globals.c` from `.orig`, purges the ROM asm cache, and
  re-applies `fix-memlock-patch.sh` on exit — including on Ctrl-C. If its cleanup ever reports a
  failure, run `fix-memlock-patch.sh` then `01-setup-elf.sh` by hand before proving anything else.
- **Steps come from `steps.csv`**, pinned to the ELF in `guests/zisk-reth/zisk-reth.build.json`. A guest
  rebuild invalidates them and every Msteps/s with them.
- **A failing arm is a result.** `mpi-numa` is *expected* to fail on the vast.ai container with
  `failed to bind memory`; t5 classifies the failure instead of just reporting a crash. Rows with
  `rc != 0` are excluded from the summary and counted at the bottom.
