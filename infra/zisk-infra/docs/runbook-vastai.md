# vast.ai runbook — measuring and closing the gap with ZisK's own cluster

**Not the entry point.** To prove one block, follow [`../README.md`](../README.md) § *Experiment —
prove one block*. This document is what to run on a **rented** box to settle a question, and it
assumes that one works: its phases 0–2 are the same ship and bring-up, with the rental criteria and
the per-track timings added.

Three tracks, by GPU count. They do not answer the same question:

| box | what it decides | box time | deliverable |
|---|---|---|---|
| **16 GPU** (8+8) | Does NUMA cost throughput? Does the MPI layout help? What do the never-touched knobs cost? | ~5 h | the verdict on the 1.35× gap |
| **8 GPU** | the **measured** 8-GPU fit (today it is derived) + the config to ship if NUMA is structural | ~3 h | `prove_secs_8 = a + b × Msteps` |
| **1 GPU** | what the **1:10** sizing costs on the **r8** guest, on the runtime that would be deployed | ~1 h 30 | **measured 2026-08-20**: r8 `+5.33 + 0.1594 × Msteps` (6.28 Msteps/s/GPU), r8-vs-reth **0.753×** in prove time — one card holds 1:10 at 19 % of budget |

Shared reference: the live `Zisk 16x5090` cluster averages **8.2 s** per mainnet block; our 16-GPU fit
`prove_secs = 2.12 + 0.0364 × Msteps` puts us at **11.0 s** on 244 Msteps. zisk-reth distribution:
median 227, mean 244, p90 388, max 666 Msteps.

---

## Phase 0 — before renting

**None of what decides a box is visible before renting it.** The two hosts compared here were
indistinguishable on everything a listing shows — same GPU model, same PCIe gen5 x16 *maximum*, same
driver — and on the one axis a listing does differentiate, the CPU, the **slower** box looked better
(6.9 GHz against 5.7). What separated them, `d2h` and the enforced power limit, appears on no listing
and is only readable from inside. So the protocol is not to pre-screen: rent, spend two minutes
measuring, and abandon before spending an hour installing. Step 1 below is that gate.

### Rental criteria, per track

| | 16 GPU | 8 GPU | 1 GPU |
|---|---|---|---|
| GPUs | **all** of the machine's (16/16) | all (8/8) | 1× RTX 5090 |
| VRAM | 32 GB/GPU | 32 GB/GPU | **32 GB** — the worker allocates ~30 GB per GPU |
| NUMA | 2 nodes, 8+8 (near-guaranteed: a single-socket EPYC has only 128 PCIe lanes = 8 GPUs at x16) | single-socket **or** 4+4, both are useful | irrelevant |
| disk | ≥ 150 GB | ≥ 150 GB | **≥ 150 GB** — measured: the key extracts to **54–70 GB**, and `00-install-once.sh` refuses to start with under **64 GB free**. A 124 GB disk therefore installs once and cannot reinstall over its own key. |
| RAM | ≥ 64 GB | ≥ 64 GB | ≥ 64 GB |
| type | **on-demand**, not interruptible | on-demand | on-demand |
| **d2h** | — | — | **The one criterion that decided a box, and no listing shows it.** Measured 24.07 GB/s against 56.97 (four runs and two), while h2d differed by only 1.11×. The slow box then took **2.82× longer** on the largest block of the set. Gate on it in step 1, before the install. |
| power limit | — | — | Record it (`nvidia-smi -q -d POWER`): a host can enforce below the vendor default, 500 W against 575 on one box. **But on both boxes measured it was never reached** — 428 W at full load against a 500 W cap — so capture it, do not price it. |
| CPU | favour **clock** over core count — the asm phase is single-threaded, and that is where we sit at 790 MHz against an advertised 1.5 GHz | same | **clock is NOT the criterion here**: the slower box had the *higher* clock (6.9 GHz) and won the CPU-only `EXECUTE` phase. What separated the two was **d2h**. Their L3 differs 16× (30 against 480 MiB) but no experiment varied it alone, so its share is unknown — do not treat it as a criterion. |

Small disk but plenty of RAM: `ZISK_KEY_DIR=/dev/shm/zisk ./00-install-once.sh` puts the proving key
(**70 GB**, read-only data) in RAM — `/dev/shm` must be that large, which most boxes are not. Not persistent across an instance STOP → re-run the script after
each start.

### What ships from the Mac

From `infra/zisk-infra/`:

```bash
export REMOTE=root@<host> PORT=<port>
```

```bash
./cluster/ship.sh $REMOTE:$PORT
```

One compressed stream, 276 KB: `cluster/` with `tests/` and `nolock.c`, plus `zisk-runner`. The
155 MB of witnesses under `tests/*/inputs*` stay home — `prepare-inputs.sh` rebuilds them on the box,
and at ~600 kB/s per flow they would cost four minutes. `EXTRA=<file> ./cluster/ship.sh $REMOTE:$PORT`
adds a path of your own to the stream.

```bash
scp -P $PORT ../../guests/zisk-reth/zisk-reth.elf $REMOTE:~/zisk-reth.elf
for t in $(awk -F, '$1 ~ /^1-/ {print $1}' cluster/tests/steps.csv); do
  scp -P $PORT ../../guests/zisk-reth/inputs/$t.bin ../../guests/zisk-reth/inputs/$t.hints $REMOTE:~/
done
```

The 11 tags in `steps.csv` are exactly the blocks whose step count is already measured — the
denominator of every Msteps/s. Do not ship the `*.pv.bin` files; proving does not use them.

---

## Phase 1 — at boot, BEFORE installing anything

```bash
scp -P $PORT cluster/tests/t0-gonogo.sh $REMOTE:~/ && ssh -p $PORT $REMOTE 'bash ~/t0-gonogo.sh'
```

Two seconds, no dependencies. vast.ai listings do not show how the GPUs split across NUMA nodes, and
that single fact is what `t1` depends on: finding out after `00-install-once.sh` costs 30–60 minutes
and 70 GB; finding out at boot costs cents.

| t0 verdict | what to do |
|---|---|
| `t1 GO — GPUS=8` | an 8+8 box: run the full 16-GPU track |
| `t1 GO — GPUS=4` | a 4+4 box: answers NUMA, but below the config we want to ship |
| `t1 NO-GO — a single NUMA node` | single-socket: no cross-socket arm exists → 8-GPU track (measurement) or 1-GPU track |
| `t1 NO-GO — numa_node = -1` | **destroy the instance**: the kernel exposes no affinity, so any "split" arm would be a fiction |
| `t4 GO — both arms` | **rare and valuable**: this box can lock memory → run `t4` here (see the 1-GPU track) |
| `disk ⚠️` | under 128 GB free: change box, or put the key in RAM |

---

## Phase 2 — install (identical on all three tracks)

```bash
ssh -p $PORT $REMOTE
cd ~/zisk-infra/cluster && bash up.sh      # installs what is missing, starts what is down, and
                                           #   returns only once a worker is REGISTERED
```

`up.sh`, not `00-install-once.sh` then `start.sh` by hand. It drives both, and it is the same command
the zisk-infra README's step 3 gives — one bring-up procedure for the whole repo. What it adds is the
part this phase used to leave to the reader: it refuses to read an incompletely extracted proving key
as a success, kills a coordinator or worker that `stop.sh` missed and that still holds port 7000 or
the card, and never accepts a registration line from an unrotated previous session as this run's.

30–60 min, and **~15 min measured on a 1-GPU box** (48 threads, NVMe at 9.7 GB/s): the const-tree
generation is CPU-bound and the extraction disk-bound, so the span tracks the box, not the GPU count.
It fails fast when the disk is too small. Check the install output for:

- `cargo-zisk: … [gpu]` — otherwise ziskup did not see CUDA; reinstall with the driver present.
- `cargo-zisk: … 1.3.1-alpha …` matching the pinned `ZISK_VER`, and `stock worker kept (bind_device fix
  is upstream in 1.3.1-alpha)`. If instead it ERRORs on a BuildID, the box carries an old hand-patched
  worker under the stock name — reinstall it as the message says.
- `patched map_locked_flag -> 0 … (pristine copy kept at …globals.c.orig)` and `built ~/nolock.so` —
  the pristine copy is what t4's `locked` arm needs in order to revert the patch.
- `provingKey: …` (**21 GB** extracted on 1.3.1-alpha; 54 on 1.1.0-alpha, 70 on 1.0.0-alpha).

Registration takes 1–2 min on 1 GPU, 4–6 on 8, 8–12 on 16, and `up.sh` waits for it rather than
returning for you to tail a log. Then, once per ELF:

```bash
cargo-zisk remote setup -e ~/zisk-reth.elf --hints --coordinator http://127.0.0.1:7000
```

`remote setup` is **once per ELF** (**~2 min measured**, 1 GPU) and runs against the coordinator. The
tests call it again (idempotent), but never restart the coordinator between arms: it holds the ELF's
proving-key cache, and every arm would pay the setup again.

---

## Track A — 16-GPU box (8+8)

The order is not numeric: it is arranged so each test frames the next.

```bash
cd ~/zisk-infra/cluster
bash tests/t3-topo.sh                          # ~2 min, read-only, cluster idle
GPUS=8 WITH_ALL16=1 bash tests/t1-numa.sh      # ~60 min (4 arms, one of them at 16 GPUs)
GPUS=8 bash tests/t2-tune.sh                   # ~40 min
FETCH_MPI_PARAMS=1 bash tests/t5-mpi.sh        # ~60 min
GPUS=8 bash tests/t4-memlock.sh                # ~25 min (unlocked arm only)
```

### t3 — topology (read it first, despite the number)

Starts nothing, proves nothing. `BW=1` (the default) briefly allocates ~1 GB per GPU: run it with the
cluster idle, or pass `BW=0`.

| observation | consequence |
|---|---|
| `pcie.link.width.max` < 16, or many `PIX`/`PXB` pairs in `topo -m` | host↔device bandwidth is a real ceiling; expect t2's sweep to plateau early and GPU util to sit low |
| concurrent `h2d_GBps` collapsing against serial | GPUs share a PCIe switch uplink → an argument for **fewer GPUs per host**, which is upstream's own topology |
| **`d2h_GBps` well below `h2d_GBps`** | **measured 24.07 against 50.3 on one box (four runs), while the healthy box read 56.97.** That box worked its card only a third of the time and took **2.82× longer on the largest block** of the set, 1.21× on the smallest — the penalty grows with block size, because a bigger block returns more data. The transfer can be deficient in ONE DIRECTION ONLY: h2d differed by just 1.11×, so a listing's single aggregate PCIe figure cannot show it, and neither can the link generation. Read both directions. |
| `ulimit -l unlimited : DENIED` | expected on vast.ai; t4's *locked* arm is out of reach here |

### t1 — the test

Compares 8 GPUs all on one NUMA node (`local8`) against 8 straddling both sockets (`split8`), with a
third arm `local8b` at the end (A-B-A) because the box is a shared host. `WITH_ALL16=1` adds the
16-GPU arm, i.e. the config we ship today.

| reading | conclusion |
|---|---|
| `numa_local == numa_total` on `local8`, `<` on `split8` | the arms genuinely differ (otherwise the test measures nothing) |
| `local8` > `split8` on `/GPU` | **NUMA confirmed**: ship 8 GPUs from one socket; the 16-GPU config needs the MPI path (t5) to stop straddling |
| `local8` ≈ `split8` | NUMA is not the main cost → spend the time on t2/t3 |
| `local8` ≠ `local8b` | shared-host noise; the run proves nothing, re-run when the box is quiet |
| `all16` against `local8` on `/GPU` | prices the current config exactly |

Reference: our marginal figure is **1.72 Msteps/s/GPU**, against an estimated **~2.46** on ZisK's side.

### t2 — the two defaults nobody touched

`--compute-capacity` (we advertise the bare `10CU` default on a ~192-thread, 48-stream box; upstream's
rule gives ~230) and `--max-streams` (auto → 3/GPU, never checked).
**Run t2 after t1 and on a NUMA-local set**: sweeping streams while half the GPUs sit on the wrong
socket measures two variables at once.

Default grid `STREAMS="auto 2 4"` × `CAPS=rec` plus one isolation arm = 4 arms. A 4×3 grid is three
hours — decide that on purpose, not by editing a default.

⚠️ `--max-streams N` is a **cap**, not a setting: the worker derives the real number from free VRAM and
can land below what was asked. The script compares the effective value and warns; two arms with
different labels and identical configs would otherwise "show" that the knob does nothing.

Mean GPU utilisation well under ~85 % with the best config → the bottleneck is upstream of the GPUs;
go back to t3.

### t5 — process layout (runs on all 16 GPUs, deliberately)

Three arms: `nompi-all` (what we ship), `mpi-numa` (upstream's, ~2 GPUs/rank, `--bind-to numa`),
`mpi-slot` (1 rank per GPU, no binding — the container-safe fallback).

`mpi-numa` is **expected** to fail on vast.ai with `failed to bind memory`. That is not a bug: recorded,
it is the evidence that the NUMA gap is structural on this class of infra — and therefore that the
answer is "8 NUMA-local GPUs per host", which t1 measures with no privileges at all. The script
classifies the failure instead of reporting a crash.

`mpi-slot` landing between the two → part of the win is the rank split rather than NUMA; and it needs
no extra privilege, so it is worth keeping.

### t4 — memlock

On vast.ai only the `unlocked` arm runs. It still earns its time: it gives the reference asm ratio
against the published 1500 MHz, and that number alone is worth recording. The `locked` arm is bought
elsewhere (see the 1-GPU track).

---

## Track B — 8-GPU box

The point here is not an A/B but a **measured fit**: the `prove_secs_8 ≈ 2.12 + 0.0728 × Msteps` used
everywhere is *derived* from the 16-GPU fit, and the repo's only 8-GPU numbers come from a single
shared-host run that is internally inconsistent (410 M → 44.9 s but 479 M → 26.2 s).

### 1. The fit, over the 11 blocks

```bash
cd ~/zisk-infra/cluster && PASSES=3 WARMUPS=2 bash clean-bench.sh     # ~45-60 min → ~/bench/timings.csv
```

```bash
awk -F, 'FNR==NR{if($1~/^1-/)s[$1]=$2/1e6;next} FNR>1&&$6==0{x=s[$2];y=$3;if(x){n++;sx+=x;sy+=y;sxx+=x*x;sxy+=x*y}} END{b=(n*sxy-sx*sy)/(n*sxx-sx*sx);printf "prove_secs = %.2f + %.4f x Msteps   (n=%d)\n",(sy-b*sx)/n,b,n}' ~/zisk-infra/cluster/tests/steps.csv ~/bench/timings.csv
```

The expected marginal term is ~0.0728 if the scaling is clean. Clearly above that means the box or the
config is losing something Track A has to explain.

### 2. Then, according to t0

**8 GPUs, single-socket** — `t1` is NO-GO (no cross-socket arm can be built), and that is good news:
this box **is** the target config, with no straddle. Go straight on:

```bash
GPUS=0 bash tests/t2-tune.sh        # GPUS=0 = every GPU on the box
GPUS=0 bash tests/t4-memlock.sh
```

Compare the resulting `/GPU` against the 1.72 Msteps/s/GPU of the straddled 16-GPU config: the
difference **is** the NUMA cost, measured across two boxes instead of two arms.

**8 GPUs in 4+4** — `GPUS=4 bash tests/t1-numa.sh` answers the NUMA question below the target config,
then `GPUS=0` for t2 so the tuning applies to the config actually shipped.

---

## Track C — 1-GPU box

One arm, one guest: **what a block costs to prove on one RTX 5090**, with the guest and the runtime
that would actually be deployed. That is the whole input to the 1:10 sizing decision, and it needs no
comparison to anything measured before.

**There is no zisk-reth arm.** It existed to ask whether the per-GPU rate at 1 GPU matches the
1.72 Msteps/s/GPU that `prove_secs = 2.12 + (0.5824/G) x Msteps` assumes. That fit is over the 1.0
runtime and the previous zisk-reth build (`sha256:3d2a9db51125ec95`, median 227.2 Msteps); both moved —
the runtime to ZisK 1.1, zisk-reth to `sha256:979da60df5826c5e` at median 172.3 Msteps. Re-anchoring the
comparison means re-measuring the 16-GPU baseline on 1.1, which is Track A, not this box.

Budget: 30–60 min of install (~15 min measured on a fast-NVMe box), then the arm. The seven blocks total
873 Msteps and run three passes plus two warm-ups — **~2 655 Msteps of proving**, so 22 to 44 min
depending on the rate, which is the thing being measured.

Guest identity, the block set and how to read the fit are in
[`cluster/tests/r8/`](../cluster/tests/r8/README.md).

### 1. Screen the box — 2 s, before transferring anything

```bash
export REMOTE=root@<host> PORT=<port>
```

```bash
scp -P $PORT cluster/tests/t0-gonogo.sh $REMOTE:~/ && ssh -p $PORT $REMOTE 'bash ~/t0-gonogo.sh'
```

One kilobyte, no dependencies. `disk ⚠️` means under 128 GB free — change box now rather than after the
transfer. Note whether it reports `t4 GO — both arms`: that decides step 6.

**Then the d2h gate, in the same step and still before the big transfer.** This is the measurement that
decided one box was unusable, and it costs two minutes against the hour an install costs. `t3-topo.sh`
compiles a small CUDA probe, so it needs `nvcc` — present on these images — and `lib.sh` beside it:

```bash
scp -P $PORT cluster/tests/t3-topo.sh cluster/tests/lib.sh $REMOTE:~/ && ssh -p $PORT $REMOTE 'cd ~ && BW=1 bash t3-topo.sh'
```

Read **`d2h_GBps`**, not just `h2d_GBps`. A box near 24 GB/s d2h while its h2d sits near 50 is the
starved case: it will take up to 2.8× longer on large blocks and no amount of tuning recovers it.
Near 57 is healthy. Also record `nvidia-smi -q -d POWER` here — one line, and it is the only place the
enforced limit is visible.

### 2. Ship — on the Mac

Stage the inputs first. `prepare-inputs.sh` frames the seven witnesses and verifies each against the
`r8-vs-zisk-reth` axis, aborting rather than ship a guest paired with the wrong witness generation or
checked by the wrong emulator. They land in `cluster/tests/r8/inputs/` and ride along with `cluster/`.

```bash
bash cluster/tests/r8/prepare-inputs.sh
```

Then **two streams, one per destination**. Not `scp`: on a 135 ms link a fresh connection costs ~2 s of
setup before any byte moves, and compression pays on this payload.

```bash
./cluster/ship.sh $REMOTE:$PORT
```

```bash
tar czf - -C ../../guests/monad-variants/r8 monad-r8-zisk.elf | ssh -p $PORT $REMOTE 'tar xzf - -C ~'
```

No worker binary is shipped or patched: upstream carries the `bind_device()` fix from 1.1.0-alpha on,
so the stock worker drives all GPUs. `00-install-once.sh` refuses a box still carrying an old
hand-patched worker under the stock name — `--version` cannot tell them apart, so it checks the
BuildID, and pairing that binary with this coordinator hangs the contributions phase.

### 3. Install and start — on the box

```bash
ssh -p $PORT $REMOTE 'set -o pipefail; cd ~/zisk-infra/cluster && ./00-install-once.sh 2>&1 | tee ~/install.log && ./start.sh 2>&1 | tee -a ~/install.log'
```

**`set -o pipefail` is not optional here.** Without it the `&&` tests `tee`'s exit code, which is
always 0, so `start.sh` runs even when the install aborted — a worker then comes up on an absent or
stale proving key and every later failure points somewhere else.

**`tee` is the load-bearing part, not the foreground.** Without it the output exists only in the
terminal that launched it, and a dropped connection takes the whole diagnosis with it — which is how an
earlier run of this track had to be debugged blind. The process itself survives a disconnect on this
class of box.

To close the laptop instead, detach it and poll the same log:

```bash
ssh -p $PORT $REMOTE 'cd ~/zisk-infra/cluster && nohup bash -c "./00-install-once.sh && ./start.sh" > ~/install.log 2>&1 < /dev/null & echo launched'
```

Either way, follow it with `tail -c 400 ~/install.log` — `curl`'s progress bar writes with carriage
returns, so `tail -n` returns one unreadable line.

`ZISK_VER` defaults to `1.3.1-alpha`. Do **not** pass `ZISK_KEY_DIR=/dev/shm` unless `/dev/shm`
exceeds the key — 21 GB extracted on 1.3.1-alpha, 54 on 1.1.0-alpha, 70 on 1.0.0-alpha.

Wait for registration — 1–2 min on 1 GPU:

```bash
ssh -p $PORT $REMOTE 'grep -a "Registered worker" ~/zisk-infra/cluster/logs/coordinator.log; nvidia-smi --query-gpu=memory.used --format=csv,noheader'
```

The VRAM figure is the one to trust: the worker allocates ~30 GB, so a card still at ~0.5 GB has not
registered whatever the log says.

### 4. The arm — on the box

```bash
ssh -p $PORT $REMOTE 'PASSES=3 WARMUPS=2 bash ~/zisk-infra/cluster/tests/r8/clean-bench-r8.sh 2>&1 | tee ~/r8.log'
```

It streams to the terminal and to `~/r8.log`. To detach instead, and poll that same log with
`tail -12 ~/r8.log`:

```bash
ssh -p $PORT $REMOTE 'nohup env PASSES=3 WARMUPS=2 bash ~/zisk-infra/cluster/tests/r8/clean-bench-r8.sh > ~/r8.log 2>&1 < /dev/null & echo launched'
```

The script prints the sha it is about to prove — `fd39fe8c27533b6d06e83734ea15ab942fdfdac71800d5206d79efa155f6aae4`
— then runs its own `cargo-zisk remote setup` without `--hints`, two discarded warm-ups, and 21
measurements. It shouts if every prove fails, rather than writing a CSV the fit reduces to `n=0`.

**Watch the first warm-up.** `warm 1 ok` means the guest, the runtime and the witnesses agree. A hang
followed by `warm 1 FAIL` means they do not — read `~/bench-r8/<tag>.p1.log` and
`cluster/logs/worker.log` before anything else.

### 4b. Optional — the reth arm and the price of hints

Two questions the r8 arm alone cannot answer, on the same box, ~30 min. Ship the guest and its blocks
first, from the Mac:

```bash
tar czf - -C ../../guests/zisk-reth zisk-reth.elf \
          -C inputs 1-24647140.bin 1-24647140.hints 1-24697073.bin 1-24697073.hints \
             1-24628590.bin 1-24628590.hints 1-24628611.bin 1-24628611.hints \
  | ssh -p $PORT $REMOTE 'tar xzf - -C ~'
```

`clean-bench.sh` needs no variant here: it hardcodes `$HOME/zisk-reth.elf`, globs `$HOME/1-*.bin` and
passes `--hints`, which is exactly this guest's native configuration.

```bash
ssh -p $PORT $REMOTE 'cd ~/zisk-infra/cluster && PASSES=3 WARMUPS=2 bash clean-bench.sh 2>&1 | tee ~/reth.log'
```

Fit it against **`tests/reth/steps-reth.csv`**, never `tests/steps.csv` — the latter holds 1.0 counts for
the previous binary and would put the slope ~28 % off.

Then the hints A/B, paired and order-alternating:

```bash
ssh -p $PORT $REMOTE 'bash ~/zisk-infra/cluster/tests/reth/hints-ab.sh 2>&1 | tee ~/ab.log'
```

Read it on the Mac after repatriation: `python3 cluster/tests/reth/pair.py bench-hints-ab/hints-ab.csv`.
[`cluster/tests/reth/`](../cluster/tests/reth/README.md) carries the method and what each outcome means.

**Compare slopes, not blocks**: the two arms use different block sets and different witness corpora, so
`b_r8` against `b_reth` is the only meaningful comparison — and it is the one that settles whether the
0.588 steps ratio or the 0.729 COST ratio predicts proving time.

> **Measured 2026-08-19** on 1× RTX 5090 / ZisK 1.1.0-alpha: r8 `prove_secs = -6.15 + 0.5024 × Msteps`
> (1.99 Msteps/s/GPU, 21/21), zisk-reth `-9.92 + 0.4409 × Msteps` (2.27). One card holds 1:10 at 41 % of
> budget. r8-vs-reth is **0.670 in prove time** against 0.588 in steps — quote the former. Hints buy
> nothing measurable. Full figures in [`cluster/tests/r8/`](../cluster/tests/r8/README.md) and
> [`cluster/tests/reth/`](../cluster/tests/reth/README.md).

### 5. The fit — on the box

```bash
awk -F, 'FNR==NR{if($1~/^1-/)s[$1]=$2/1e6;next} FNR>1&&$6==0{x=s[$2];y=$3;if(x){n++;sx+=x;sy+=y;sxx+=x*x;sxy+=x*y}} END{b=(n*sxy-sx*sy)/(n*sxx-sx*sx);a=(sy-b*sx)/n;printf "prove_secs = %.2f + %.4f x Msteps  (n=%d, %.2f Msteps/s/GPU)\n",a,b,n,1/b}' ~/zisk-infra/cluster/tests/r8/steps-r8.csv ~/bench-r8/timings.csv
```

`n` must read **7**. Below that, some blocks carry a non-zero `rc` and are excluded — find out why
before reading the rate.

Evaluate the fitted line at the r8 percentiles (median 101.1, mean 109.7, p90 177.7, max 334.7 Msteps)
against the cadence budget. `cluster/tests/r8/README.md` carries the thresholds and the steps-against-COST
caveat: the two readings diverge by 24 % on this guest, enough to move the 1:10 answer.

### 6. The asm clock, and t4 where t0 allows it

`Assembly execution speed` is a CPU number, already in the log — nothing extra to run:

```bash
ssh -p $PORT $REMOTE 'grep -a "Assembly execution speed" ~/zisk-infra/cluster/logs/worker.log | tail -3'
```

719–809 MHz is what is measured elsewhere, and **501 MHz** on a shared low-clock EPYC, against an
advertised 1.5 GHz. A high-clock CPU climbing back toward 1.5 makes this a rental criterion, not a
patch — which is why running t0 across two or three 1-GPU candidates costs cents.

Only where t0 reported `t4 GO — both arms` (~25 min):

```bash
ssh -p $PORT $REMOTE 'GPUS=1 bash ~/zisk-infra/cluster/tests/t4-memlock.sh'
```

| reading | conclusion |
|---|---|
| `locked` clearly above `unlocked` on asm_MHz | `nolock.c`'s claim that unlocking is harmless is wrong; the fix is a **box choice**, not a config change |
| both arms ≈ equal | memlock is exonerated; the remaining suspects are CPU clock and host contention, neither tunable, both fixable by box choice |

### 7. Repatriate — on the Mac

Everything the run produced, in one stream — the arms' output directories **and** the cluster's own
logs, which no bench script copies for itself:

```bash
ssh -p $PORT $REMOTE 'tar czf - --ignore-failed-read bench-r8 bench bench-hints-ab r8.log reth.log ab.log install.log zisk-infra/cluster/logs' | tar xzf -
```

`timings.csv` is the deliverable. `env.txt` sits beside it and records the stack that produced it —
`cargo-zisk` version, driver, ELF sha, allocated CPU and RAM. A fit without that record cannot be read
six months on, and a version mismatch is what invalidated an earlier run of this track.

`coordinator.log` is the one to open when a prove fails fast: a phase timeout, a failed job and the
`Cluster unavailable` recovery that follows are recorded there and appear nowhere worker-side. The
`.proof` files come along and are `cargo-zisk verify`-able.

> The cluster path (coordinator + worker) is kept on purpose rather than a local `cargo-zisk prove`:
> it is the same definition of `prove_secs` as the other two tracks.


---

## Repatriation, and where the results go

From the Mac, **with `KEEP_REMOTE=1`** — without it `fetch-runs.sh` deletes the remote source dir, and
pointed at `tests` that would take every run with it, not just the one being fetched:

```bash
KEEP_REMOTE=1 ./cluster/fetch-runs.sh $REMOTE:$PORT tests
```

```bash
./cluster/fetch-runs.sh $REMOTE:$PORT              # submit.sh run records → results/
```

Read them back, several runs at once as long as the arm names differ:

```bash
bash cluster/tests/summary.sh ~/tests/t1-numa-*/results.csv
```

| result | file |
|---|---|
| streams × capacity table + GPU occupancy | the "GPU occupancy / tuning" section of [zisk-benchmark.md](zisk-benchmark.md), currently empty |
| NUMA verdict, asm ratio, MPI layout | the ZisK section of `AGENT-NOTES.md` |
| a measured 8-GPU or 1-GPU fit | `infra/monad-witness/RTP-FINDINGS.md`, which carries the 16-GPU fit |
| step count for a newly measured block | `cluster/tests/steps.csv` (lib.sh appends it itself) |

---

## Things that cost a run

- **Every arm pays a full worker registration**: 4–6 min on 8 GPUs, 8–12 on 16 (30 GB allocated per
  GPU). That is what turns a "quick sweep" into three hours. The silence during allocation is normal —
  the log prints peak VRAM every 30 s.
- **`PASSES=2` by default in the test arms (`lib.sh`), 3 in `clean-bench.sh`, and the "median" of two
  is the lower one.** Deliberate on a shared host: the faster pass is the one less polluted by a
  neighbour. Raise `PASSES` when the box is quiet.
- **Ctrl-C is safe but does not restore the worker** — the script prints the command to run. Do not
  leave the box on a test config without knowing it.
- **`nvidia-smi` ignores `CUDA_VISIBLE_DEVICES`**, and so does upstream's `mpi_params.sh` (it sizes
  `-np` from `nvidia-smi -L`). That is why t5 runs on all GPUs.
- **`numa_node = -1` is not node 0.** It means the kernel exposes no affinity. t1 stops rather than
  pretending.
- **A failing arm is a result.** Rows with `rc != 0` are excluded from the rollup and counted at the
  bottom of the summary; the rollup keeps only the blocks common to every arm, otherwise an arm that
  skipped a large block would be ranked on an easier mix and could "win".
- **Step counts are pinned to the ELF** in `guests/zisk-reth/zisk-reth.build.json`. A guest rebuild
  invalidates `steps.csv` and every Msteps/s with it.
