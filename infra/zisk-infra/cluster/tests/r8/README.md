# Track C, r8 arm — what the 1:10 sizing costs on the guest that decides it

The r8 arm of Track C. **The sequence to run is in
[`docs/runbook-vastai.md`](../../../docs/runbook-vastai.md) → Track C**; this file carries what is
specific to the arm — the guest's identity, the block set, and the measured result.

It answers one question, on one 1-GPU box: **what a block costs to prove on one RTX 5090, with the
guest and the runtime that would actually be deployed.** That is the whole input to the 1:10 sizing
decision, and it needs no comparison to anything measured before.

## Why there is no zisk-reth arm here

An earlier plan ran zisk-reth alongside to ask whether the per-GPU rate at 1 GPU matches the
1.72 Msteps/s/GPU that `prove_secs = 2.12 + (0.5824/G) x Msteps` assumes. That comparison is not
available: the 1.72 is over ZisK 1.0 and the previous zisk-reth build (`sha256:3d2a9db51125ec95`,
median 227.2 Msteps); both moved — the runtime to 1.1, zisk-reth to `sha256:979da60df5826c5e` at median
172.3 Msteps. Re-anchoring means re-measuring the 16-GPU baseline on 1.1, which is Track A.

A zisk-reth arm is still worth running, for a different question: see
[`../reth/`](../reth/README.md).

## The guest

| | |
|---|---|
| branch / commit | `al/zkvm-r8` @ **`0df7094a100dd859944278a5c5e4fb971307eed6`** |
| ELF sha256 | `fd39fe8c27533b6d06e83734ea15ab942fdfdac71800d5206d79efa155f6aae4` |
| path | `guests/monad-variants/r8/monad-r8-zisk.elf` |
| axis | `r8-vs-zisk-reth` in `profiling/r8-compare.json`, `a_ident` matches |
| generation | `guests/monad/gen/zkvm-r8-2026-08-0df7094a1/` |
| runtime | **ZisK 1.1** |

⚠️ **The sha is the identity, not the commit, and not the path.**
`guests/monad/monad-zkvm-guest-zisk.elf` carries `aca82727de413bb1` — that is `monad-r4bump-zisk`, a
different guest. `prepare-inputs.sh` refuses any other sha, and `clean-bench-r8.sh` prints the one it is
about to prove.

Witnesses come from `zkvm-r4-gen-2026-08-9d7540181`, which the r8 generation shares by symlink — same
wire format, different guest lineage.

## The block set

| block | percentile | steps |
|---|---|---|
| 25552266 | p02 | 18,305,994 |
| 25552304 | p15 | 71,952,490 |
| 25552294 | p35 | 86,657,207 |
| 25552158 | **p50** | 101,053,707 |
| 25552167 | p70 | 128,200,470 |
| 25552110 | p90 | 177,689,043 |
| 25552376 | p99+ | 288,877,414 |

18.3 → 288.9 Msteps. The range matters more than the count: the fit needs a lever arm, and a set
clustered at the median gives a slope with no leverage.

Whole-distribution figures from the same axis (n=365): median **101.1**, mean **109.7**, p90 **177.7**,
max **334.7** Msteps. Against the rebuilt zisk-reth the ratio is **0.588 in steps, 0.729 in COST** —
and **0.670 in measured prove time**, see [`../reth/`](../reth/README.md).

`steps-r8.csv` pins the seven counts. `prepare-inputs.sh` rewrites it from the axis.

## Contents

```
prepare-inputs.sh    on the Mac: extracts, frames, verifies, writes steps-r8.csv
clean-bench-r8.sh    on the box
steps-r8.csv         tag,steps — pinned step counts
inputs/              staged by prepare-inputs.sh, gitignored (large, regenerable)
```

This directory ships **inside `cluster/`**, so it lands wherever `cluster/` lands and
`clean-bench-r8.sh` locates its own inputs. The ELF ships separately to `$HOME`. Override with `ELF=`
or `INPUTS=`.

`prepare-inputs.sh` replays every framed input through the ELF with `ziskemu` and requires the step
count recorded in the axis. It also refuses a `ziskemu` that is not 1.1: the axis counts come from the
1.1 emulator, and a 1.0 one counts a rebuilt guest differently.

No `.hints`: the monad guest uses none, so `--asm` is not required either.

`clean-bench-r8.sh` exists because `cluster/clean-bench.sh` cannot run this guest — it hardcodes
`$HOME/zisk-reth.elf`, globs `$HOME/1-*.bin`, and passes `--hints` on both the setup and every prove,
which fails with no hints file. The variant differs in exactly three places (ELF and inputs as
parameters, no `--hints`, output to `~/bench-r8`) and keeps the same 6-column `timings.csv`. It writes
an `env.txt` before proving, keeps both cluster logs, keeps the warm-up's output, and shouts when every
prove fails instead of leaving a CSV the fit reduces to `n=0`.

## The result — SUPERSEDED, see [`../paired/`](../paired/README.md)

⚠️ The figures below come from an **Intel Core Ultra 7 265K box that no longer exists**, and a later
paired round on a Ryzen 9 9950X measured **6.28 Msteps/s/GPU** for the same guest on the same blocks —
3.2× faster, over three equally consistent passes. Every checkable host-side cause is ruled out (PCIe,
RAM, disk, driver, CPU clock, neighbours, asm clock); the only hypothesis left is that this box was
installed twice, the first attempt failing on incomplete const-trees.

**Quote the paired round.** What follows is kept because it is a real measurement of a real box, and
because the discrepancy is itself the finding: a 1-GPU rate is a property of the host as much as of the
card, and `t3-topo.sh` was never run on this one.

## The superseded result — measured 2026-08-19

1× RTX 5090 (driver 580.159.03), ZisK 1.1.0-alpha, Intel Core Ultra 7 265K, 20 CPU allocated of a
shared host, 63 GB RAM. Three passes, **21/21 rc=0**, inter-pass spread 0.2–2.6 s.

```
prove_secs = -6.15 + 0.5024 x Msteps          1.99 Msteps/s/GPU
```

| block | Msteps | median | s/Mstep |
|---|---|---|---|
| 25552266 | 18.3 | 8.33 | 0.4550 |
| 25552304 | 72.0 | 29.38 | 0.4083 |
| 25552294 | 86.7 | 37.11 | 0.4282 |
| 25552158 | 101.1 | 41.36 | 0.4093 |
| 25552167 | 128.2 | 54.56 | 0.4256 |
| 25552110 | 177.7 | 82.89 | 0.4665 |
| 25552376 | 288.9 | 141.84 | 0.4910 |

**The line predicts well and models badly.** Cost per Mstep is not constant: high at the small end
(fixed overhead), lowest around 72–101 M, climbing to 0.491 on the largest block. Linear residuals are
U-shaped (+5.3 −0.6 −0.3 −3.3 −3.7 −0.2 +2.8), and that curvature is what forces the intercept
negative. **Do not quote −6.15 s as a fixed cost, and do not extrapolate to zero or far past
300 Msteps.** Inside 18–289 Msteps a power fit (`0.393 x M^1.025`) agrees to under 6 % across the whole
distribution, so the predictions below do not depend on the choice.

The curvature shows up independently in the hints A/B: 1.80x the trace weight costs 2.56x the time.

### The cadence answer

Queue simulation over the 365-block r8 distribution, deterministic arrivals:

| cadence | budget | mean block | in slot | max backlog |
|---|---|---|---|---|
| 1:1 | 12 s | 49.0 s — **408 %** | — | diverges |
| **1:10** | **120 s** | **49.0 s — 41 %** | **98.6 %** | **1 block** |
| 1:100 | 1200 s | 49.0 s — 4 % | 100 % | 0 |

On the COST reading (0.729 applied to the rebuilt zisk-reth distribution): 61.1 s mean — 51 % of the
1:10 budget, 95.6 % in slot, backlog 2.

**One RTX 5090 holds 1:10 comfortably, on either reading.** 1:1 needs roughly four cards — an
extrapolation from this single measured point, not a measurement.

### What this number is not

It neither validates nor refutes the historical 1.72 Msteps/s/GPU: that fit is over ZisK 1.0, the
previous zisk-reth build, and is derived from 16 GPUs. This one crosses two version changes and a guest
change. It is the first measured 1-GPU rate on the deployed stack, and it stands alone.

The 2–3 GPU regime stays unmeasured. This point turns it from an extrapolation off the 16-GPU fit into
an interpolation between two measured anchors.
