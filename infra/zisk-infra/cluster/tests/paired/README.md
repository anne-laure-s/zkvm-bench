# Paired round — r8 against zisk-reth, same blocks

The r8 and reth arms of Track C ran on **disjoint block sets**: reth had to use the 24.6M sample blocks
because no `.hints` exists inside the r8 axis range. That allowed only a comparison of **slopes**
(`b_r8 / b_reth = 1.140`, implying a 0.670 prove-time ratio), not a per-block one.

This round removes that limitation. Both guests prove the **same seven blocks**, alternating order, so
the ratio is measured per block and the three readings — steps, COST, prove time — sit on the same rows.

**What unblocked it: hints buy nothing.** Measured in `tests/reth/`, with identical trace area on both
arms. zisk-reth can therefore be proven hint-free on the monad corpus, where 530 `.bin` exist and no
`.hints` does.

## The two guests

| | sha256 | steps on these 7 blocks |
|---|---|---|
| `monad-r8-zisk` | `fd39fe8c27533b6d…` | 873 Msteps |
| `zisk-reth` (1.1 build) | `979da60df5826c5e…` | 1445 Msteps |

Both are verified against `profiling/r8-compare.json` [`r8-vs-zisk-reth`] by `prepare-inputs.sh`, which
refuses a wrong sha or a `ziskemu` that is not 1.1.

zisk-reth's inputs need no framing — they are already framed `input.bin` from `input-gen`, unlike the
monad witnesses.

## Files

```
prepare-inputs.sh    on the Mac: stages and verifies the reth side of the seven blocks
paired.sh            on the box: both guests, same blocks, order alternating
pair-guests.py       on the Mac: per-block ratios, and which unit predicts prove time
inputs-reth/         staged, gitignored
steps-reth-paired.csv
```

The r8 side is not restaged: `paired.sh` reads `tests/r8/inputs/`, and refuses to start if the two
sides do not cover the same blocks.

## Method

Order alternates per block. A fixed order lets the first guest of each pair pay cache and scheduler
costs the second does not — that reversed a verdict once already in this repo (`RTP-FINDINGS.md`,
gzip vs zstd).

The setup runs before each prove, outside the clock. Different ELFs get different Hash IDs so both
could coexist, but the coordinator cache has already surprised us once (`tests/reth/README.md`): 2 s of
setup is cheaper than another poisoned run.

`PASSES=2` by default — ~35 min. The r8 arm showed 0.2–2.6 s of inter-pass spread on 8–142 s services,
and pairing already controls the dominant noise source, so a third pass buys little for ~15 min more.

A pair with one failed half is dropped whole by `pair-guests.py`: half a pair is not a measurement.

## Reading it

`pair-guests.py` prints the per-block prove-time ratio beside the steps and COST ratios, then which of
the two predicts the measured one. Both units stay valid for what they measure — steps are the
deterministic, host-independent work-unit; COST is ZisK's trace-area model. The question is only which
to quote when the claim is about **proving time**.

Standing figures to compare against, from the disjoint-set arms: steps **0.588**, prove time **0.670**
(from the slopes), COST **0.729**. On these seven blocks specifically the axis gives steps 0.592 and
COST 0.731.

## The result — measured 2026-08-20

**42/42 rc=0**, three passes, seven blocks, both guests alternating. Inter-pass spread 0.06–0.73 s on
7–68 s services, except `25552167` at 6.32 s whose first pass (31.76) sits outside its own series
(25.44 / 25.69) — a warm-up residue the median absorbs.

### The machine

| | |
|---|---|
| GPU | RTX 5090, 31.8 GB, driver 580.159.03 |
| PCIe | gen 5 x16 (`gen.max 5`, `width 16/16`) |
| **h2d / d2h measured** | **55.49 / 57.29 GB/s**, identical serial and concurrent |
| CPU | AMD Ryzen 9 9950X, 30 of 32 threads allocated |
| RAM | 94 GB total, 84 GB free before the run |
| `/dev/shm` | 47 GB · disk 60 GB free of 125 · memlock 8192 KB, not raisable |
| ZisK | 1.1.0-alpha `[gpu]` (`9a5a1ac`), proving key 53 GB |

### Per block

| block | Msteps | r8 median | reth median | prove-time ratio | steps | COST |
|---|---|---|---|---|---|---|
| 25552266 | 18.3 | 6.97 | 9.10 | 0.766× | 0.545× | 0.733× |
| 25552304 | 72.0 | 17.33 | 22.75 | 0.762× | 0.558× | 0.718× |
| 25552294 | 86.7 | 19.13 | 25.41 | 0.753× | 0.577× | 0.724× |
| 25552158 | 101.1 | 21.90 | 28.20 | 0.777× | 0.599× | 0.732× |
| 25552167 | 128.2 | 25.69 | 34.58 | 0.743× | 0.592× | 0.727× |
| 25552110 | 177.7 | 34.99 | 48.38 | 0.723× | 0.596× | 0.731× |
| 25552376 | 288.9 | 50.34 | 67.64 | 0.744× | 0.644× | 0.748× |

### COST is the unit for a prove-time claim

```
measured prove-time ratio   0.753x     (median, spread 0.723-0.777)
COST ratio                  0.731x     -3.0%
steps ratio                 0.592x    -21.4%
```

**Quote 0.753× for proving time, and COST when only a proxy is available.** Steps are wrong by a fifth
here. The per-block spread is tight, so this is not an averaging artefact.

It also corrects the slope-derived estimate: two fits over **disjoint** block sets gave 0.670×, against
0.753× measured on identical blocks — the derivation was biased by 11 %. That is what this round exists
to catch.

### The fits

| guest | fit | rate | residuals |
|---|---|---|---|
| **r8** | `+5.33 + 0.1594 × Msteps` | **6.28 Msteps/s/GPU** | −1.3 +0.5 −0.0 +0.5 −0.1 +1.3 −1.0 |
| **zisk-reth** | `+4.30 + 0.1425 × Msteps` | **7.02 Msteps/s/GPU** | +0.0 +0.1 −0.3 −0.2 −0.6 +1.6 −0.6 |

Positive intercepts, residuals under ±1.6 s, and cost per Mstep falling cleanly from 0.381 to 0.174 —
the shape of a fixed cost being amortised. Predict inside 18–289 Msteps.

### The cadence answer

| cadence | budget | mean block (109.7 Msteps) | worst block (334.7) |
|---|---|---|---|
| 1:1 | 12 s | 22.8 s — 190 % | 58.7 s |
| **1:10** | **120 s** | **22.8 s — 19 %** | **58.7 s — 49 %** |
| 1:100 | 1200 s | 22.8 s — 2 % | 58.7 s — 5 % |

**No block in the corpus reaches half the 1:10 budget.** One card holds it with no queue, no overrun,
and room for ~10 s of network and a retry.

### An unexplained 3.2× against another box

An earlier run of the r8 arm alone, on an Intel Core Ultra 7 265K box, gave **1.99 Msteps/s/GPU** over
three equally consistent passes — a 3.2× gap, growing with block size (1.18× on the smallest block,
2.84× on the largest). Every host-side cause that could be checked is ruled out: PCIe (48 against
55.5 GB/s), RAM (64 GB with 34 free against 94 with 48), disk (the Intel box's was 3.3× *faster*),
driver (identical), CPU clock (the Intel box's was *higher*), neighbours (both whole machines), asm
clock (same 330–543 MHz range).

The one hypothesis left is untestable: that box was installed twice, the first attempt failing on
incomplete const-trees. It has been destroyed.

**Quote this box.** Three coherent passes, positive intercepts, tight residuals, documented topology.
Treat the 1.99 figure as unexplained and unreproduced rather than averaging the two.
