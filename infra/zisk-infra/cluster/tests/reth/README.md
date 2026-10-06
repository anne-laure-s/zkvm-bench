# Track C, reth arm — the slope reference, and what hints cost

Optional second and third steps of Track C, run on the same box after the r8 arm. Two questions, one
guest:

1. **Do steps or COST predict proving time?** r8 is 0.588x zisk-reth in steps but 0.729x in COST — a
   24 % spread that moves the 1:10 answer, and no emulation can settle it. Measuring both guests on one
   box does: compare the **slopes**. If `b_r8 ≈ b_reth`, a step costs the same in both and the steps
   ratio predicts prove time; if `b_r8 > b_reth`, r8's steps are individually dearer and COST is the
   figure to read.
2. **What do ZisK's precompile hints buy?** The monad guest uses none, reth does. That difference sits
   under every r8-vs-reth ratio and has never been priced. `hints-ab.sh` prices it.

**Compare slopes, not blocks.** The two arms run different block sets from different witness corpora,
so no per-block ratio and no intercept comparison is meaningful between them. Only `b` is.

## The guest

`guests/zisk-reth/zisk-reth.elf`, sha256 `979da60df5826c5e…`, rebuilt on **ZisK 1.1** (2026-08-19).
The previous build was `3d2a9db51125ec95`; every figure in `results/` predating the bump is that one,
and the two are not comparable.

`steps-reth.csv` carries the eleven sample blocks re-counted with `ziskemu` 1.1. The rebuild is uniform:
each block lands at **0.766–0.788x** its 1.0 count (mean 0.779), which is what the B-extension build
buys. ⚠️ `cluster/tests/steps.csv` still holds the **1.0** counts for the **previous** binary — fitting
1.1 timings against it silently yields a slope ~28 % off.

## Files

```
steps-reth.csv       tag,steps — eleven blocks, ziskemu 1.1 counts
hints-ab.sh          on the box: the paired with/without-hints A/B
pair.py              on the Mac: the paired read of its CSV
```

The native arm needs no script of its own: `cluster/clean-bench.sh` already hardcodes
`$HOME/zisk-reth.elf`, globs `$HOME/1-*.bin` and passes `--hints` — which is exactly right for this
guest. Ship the ELF and the blocks you want, run it, and fit against `steps-reth.csv`.

## The hints A/B

`hints-ab.sh` runs one small block (58.3 Msteps) and one mid block (110.7 Msteps), several rounds each,
**alternating which arm goes first every round**. That alternation is not decoration: an
interleaved-but-fixed order already produced a reversed verdict in this repo — the first arm of a round
pays cache and scheduler costs the second does not (`infra/monad-witness/RTP-FINDINGS.md`, gzip vs
zstd). Two block sizes, because a fixed per-proof gain and a per-precompile-call gain look identical on
one size.

⚠️ **The setup is per arm and the two cannot coexist.** Both configurations register under the same
Hash ID — it is the ELF's — so a second `remote setup` overwrites the first. Prove with `--hints`
against a cache last set up without them and the job hangs in `CALCULATING_CONTRIBUTIONS` until the
300 s monitor timeout; prove without them against a cache set up with them and it fails immediately
with `Program '<name>' (with_hints=false) not found in cache`. Either failure then drops the coordinator
into a recovery handshake that rejects every later prove, whichever arm it belongs to. So `hints-ab.sh`
re-runs the setup for each arm before each prove, outside the timed region (~5 s with hints, ~65 s
without, the latter being a one-time cache build).

That mechanism is documented nowhere upstream and cost three runs to find.

`pair.py` reports the paired median and how many rounds favour hints, not two medians side by side:
rounds are pairs run back to back, not independent samples. A row with `rc != 0` drops out with its
partner so no pair is half-counted.

```bash
python3 pair.py ~/bench-hints-ab/hints-ab.csv
```

A verdict needs a **contrast** — one arm failing says nothing on its own, since a single failure poisons
the cluster. The script reports `INCONCLUSIVE` when both arms fail rather than reading the null as an
answer.

## The result — measured 2026-08-19

Same box as the r8 arm. 20/20 rc=0.

### zisk-reth on one RTX 5090

Four blocks, three passes:

```
prove_secs = -9.92 + 0.4409 x Msteps          2.27 Msteps/s/GPU
```

| Msteps | median | s/Mstep |
|---|---|---|
| 58.3 | 15.62 | 0.2678 |
| 110.7 | 39.71 | 0.3589 |
| 319.0 | 127.57 | 0.3999 |
| 371.8 | 156.48 | 0.4209 |

Same curvature as r8: cost per Mstep climbs with block size, and the intercept goes negative to
compensate. Predict inside the range, do not extrapolate to zero.

### Steps or COST — COST, and it matters

```
b_r8   = 0.5024 s/Mstep
b_reth = 0.4409 s/Mstep        ->  b_r8 / b_reth = 1.140
```

**A step of r8 costs 14 % more to prove than a step of reth.** So the steps ratio understates, and the
real figure is:

| | |
|---|---|
| ratio in steps (the axis) | 0.588 |
| **ratio in measured prove time** | **0.670** |
| ratio in COST | 0.729 |

**Superseded: the measured figure is 0.753×**, from the paired round on identical blocks
([`../paired/`](../paired/README.md)). The 0.670 below is derived from two fits over **disjoint** block
sets and is biased by 11 % — which is precisely what the paired round was built to catch. COST (0.731×)
lands within 3 % of the measured value; steps (0.588×) are off by a fifth.

The derivation, kept for the record: **0.670 for prove time**, not 0.588. The COST model predicts the direction and most of the
magnitude; the steps ratio is 12 % too flattering to r8.

### Hints buy nothing measurable

| block | hints | hint-free | paired median | rounds favouring hints |
|---|---|---|---|---|
| 58.3 M | 15.65 s | 15.50 s | **−0.09 s** | 3/6 |
| 110.7 M | 39.88 s | 40.44 s | **+0.45 s** | 4/4 |

Between −0.6 % and +1.1 %, with the signs opposed between blocks. **Within-arm spread is 1.27 s and
2.23 s** — three to five times the effect. Recording the noise floor next to the verdict: this
measurement resolves nothing below ~1 s, i.e. 2–6 % of these proves.

**The null is confirmed by trace area, not by proof size.** Every ZisK proof is the same size
(recursive STARK, fixed-size final proof), so identical file sizes prove nothing. What does: the worker's
`PROOF INSTANCES SUMMARY` is **identical across every round of a block, both arms** — same `Keccakf: 7`,
same `ArithSecp256K1: 1`, same `Total weight: 43,397,414,912` on the mid block over eight rounds. Same
trace area means the same proving work.

So `--hints` changes neither the circuit nor the load nor the time. It is consumed in
`COMPUTE_MINIMAL_TRACE` (`··· Processed 702 hints`), a phase worth ~1 s of a 15–40 s prove — structurally
below the noise floor.

**Consequence: the 0.670 r8-vs-reth ratio needs no footnote about precompiles.** reth's hints give it no
measurable advantage, so the gap between the guests is not inflated by them.

Two block sizes also price the curvature independently of the r8 fit: **1.80x the trace weight costs
2.56x the time.**
