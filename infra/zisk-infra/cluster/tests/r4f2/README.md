# Track C, r4f2 arm — the single-GPU rate on the guest that decides

The r4f2 arm of Track C. **The sequence to run is in
[`docs/runbook-vastai.md`](../../../docs/runbook-vastai.md) → Track C**, steps 1 to 8; this file carries
what is specific to the arm — the guest's identity, the block set, and how to read the result.

It answers two questions nothing else here answers, on one 1-GPU box:

1. **Is the per-GPU rate at 1 GPU the same as at 16?** Every cadence figure in this repo descends from
   `prove_secs = 2.12 + (0.5824/G) x Msteps`, which *assumes* 1.72 Msteps/s/GPU at every G. Nothing
   below 8 GPUs is measured. ZisK's own two published points imply the opposite of clean scaling:
   2.51 Msteps/s/GPU at 16x5090 (8.2 s) against 1.76 at 1 GPU (141 s), i.e. **1.43x worse per GPU**
   single. Their 1-GPU figure is on an unspecified cloud card, so part of that penalty may be hardware.
2. **Do steps or COST predict prove time on r4f2?** The guest is 0.547x zisk-reth in steps but 0.681x
   in COST — 24 % apart, and that span covers the 1:10 sizing decision.

The zisk-reth arm runs in the same session for a reason: it is the guest the 16-GPU fit was built on, so
it isolates the single-GPU scaling effect from the guest effect. r4f2 alone cannot separate them.

## The guest

The final `al/zkvm-r4` build, confirmed by three concordant sources:

| | |
|---|---|
| branch / commit | `al/zkvm-r4` @ **`f85a3acc9`**, 21 commits over `origin/sam/zkvm-zisk-sp1` |
| ELF sha256 | `362b8eb0318bc6a6f7f1f958e851374b79557051f1d8f823f33f52322503c2e2` |
| axis | `r4f2-vs-reth` in `profiling/results/compare-r4.json`, `a_ident` matches |
| generation | `guests/monad/gen/zkvm-r4final2-2026-08-f85a3acc9/` |

It is the last of the ladder: r4 179.3M → r4jd 177.1M → r4jd2 150.8M → mtune 138.5M → tip 132.8M →
**r4f2 123.9M** median steps.

⚠️ **The sha is the identity, not the commit.** The ZisK guest carries `-mtune=generic-ooo`; a build
without it is a different binary at ~8 % more work. `prepare-inputs.sh` refuses to run against any other
sha, and `clean-bench-r4f2.sh` prints the sha it is about to prove.

The witnesses come from `zkvm-r4-gen-2026-08-9d7540181`, which the r4final2 generation shares by
symlink — same wire format, different guest lineage.

## The block set

Seven blocks spanning the r4f2 distribution, p02 to p99+:

| block | percentile | steps |
|---|---|---|
| 25552266 | p02 | 22,818,090 |
| 25552379 | p15 | 88,701,297 |
| 25552378 | p35 | 108,748,958 |
| 25552303 | **p50** | 123,948,301 |
| 25552336 | p70 | 153,987,591 |
| 25552187 | p90 | 209,217,882 |
| 25552376 | p99+ | 328,394,318 |

22.8 → 328.4 Msteps, ~31 min for 3 passes at 1 GPU. The range matters more than the count: the fit needs
a lever arm, and a set clustered at the median gives a slope with no leverage.

`steps-r4f2.csv` pins those counts — the denominator of every Msteps/s, the same role `tests/steps.csv`
plays for zisk-reth. `prepare-inputs.sh` rewrites it from the axis.

## Contents

```
prepare-inputs.sh     on the Mac: extracts, frames, verifies, writes steps-r4f2.csv
clean-bench-r4f2.sh   on the box
steps-r4f2.csv        tag,steps — pinned step counts
inputs/               staged by prepare-inputs.sh, gitignored (large, regenerable)
```

This directory ships **inside `cluster/`**, so it lands wherever `cluster/` lands and
`clean-bench-r4f2.sh` locates its own inputs rather than assuming a path. The ELF is not copied here: it
ships from `guests/monad/monad-zkvm-guest-zisk.elf` to `$HOME` beside `zisk-reth.elf`, which is where
the script looks for it. Override with `ELF=` or `INPUTS=` if either moves.

`prepare-inputs.sh` replays every framed input through the ELF with `ziskemu` and requires it to
reproduce the step count recorded in the axis. That check is the point: an ELF paired with the wrong
witness generation produces confident nonsense downstream, and witness filenames carry no generation
marker.

No `.hints` anywhere. The monad guest uses no precompile hints, so `--asm` is not required either —
which is why `zisk-runner` takes `--hints` as optional.

`clean-bench-r4f2.sh` exists because `cluster/clean-bench.sh` cannot run this guest: it hardcodes
`ELF="$HOME/zisk-reth.elf"`, globs its inputs from `$HOME/1-*.bin`, and passes
`--hints "${bin%.bin}.hints"` on both the setup and every prove, which fails with no hints file. The
variant differs in exactly three places — ELF and inputs as parameters, no `--hints`, output to
`~/bench-r4f2` so a zisk-reth run in the same session is not overwritten — and keeps the same 6-column
`timings.csv`, so the runbook's fit reads it unchanged.

## Reading the result

The fitted **Msteps/s/GPU** decides the 1:10 sizing. Thresholds are for a 120 s budget against the r4f2
mean block — 132.3 Msteps on the steps reading, 166.0 on the COST-equivalent:

| fitted rate | verdict for 1:10 on 1 GPU |
|---|---|
| < 1.12 | diverges outright — 1 GPU is not an option |
| 1.12 – 1.41 | diverges on the COST reading; 2 GPU required |
| 1.41 – 1.78 | holds the cadence, but ~half the blocks land a slot late |
| 1.78 – 2.24 | holds, and the p90 fits its slot on the steps reading |
| ≥ 2.24 | holds everything to p90 on both readings |

Anchors: **1.72** is what the current extrapolation assumes. **1.20** is that rate with ZisK's own
single-GPU penalty applied. **2.51** is ZisK's 16-GPU per-GPU rate, i.e. the ceiling if the single-GPU
penalty is entirely someone else's hardware.

Compare the fitted intercept against the 2.12 s of the 16-GPU fit. That fit and the benchmark table in
[`docs/zisk-benchmark.md`](../../../docs/zisk-benchmark.md) disagree by 5-6 % (`2.12 + 0.0364` against a
refit of the table at `2.36 + 0.0380`), so a difference under ~6 % is noise between runs rather than a
property of the GPU count.

**Then check steps against COST.** Predict each block from its steps with the fitted line, and compare
the residuals against a prediction scaled by the 0.681 COST ratio. Whichever tracks decides which column
of the sizing tables to trust, and the divergence is 24 % — enough to move the answer.

## What this does not settle

1:100 holds on 1 GPU under every hypothesis above: the r4f2 mean lands at 8-12 % of a 1200 s budget even
at 1.20 Msteps/s/GPU. That decision needs no measurement.

The 2-3 GPU regime stays unmeasured. A 1-GPU point turns it from an extrapolation off the 16-GPU fit
into an interpolation between two measured anchors.
