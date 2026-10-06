#!/usr/bin/env python3
"""The 1-, 2- and 4-GPU timings of a run side by side, per arm and block size: what poc-run.sh's
question is, read off the results run.sh leaves. RUNS ON THE BOX (poc-run.sh calls it) OR ON THE
MAC (on unpacked archives).

    poc-summary.py <results dir> [<results dir> ...]

Each dir is one ~/l2-latency-<stamp> (or its unpacked archive); the GPU sets of several dirs are
merged by name, so a 1-GPU run and a 4-GPU run of the same key read as one table. Per arm and
size: the median proof time over that size's blocks (each block's own median over its passes,
successful proofs only), each set's speed-up over 1 GPU, and how many GPUs a chain of 50 TPS would
need at that block size if each prover takes the next block as soon as it is done:
G x ceil(50 x secs / txs).
"""
import csv
import math
import pathlib
import statistics as st
import sys

TPS = 50


def load(dirs):
    inputs, sets = {}, {}
    for d in map(pathlib.Path, dirs):
        for r in csv.DictReader(open(d / 'inputs.csv')):
            inputs[r['id']] = r
        for t in sorted(d.glob('stark-*/timings.csv')):
            by = sets.setdefault(t.parent.name[len('stark-'):], {})
            for r in csv.DictReader(open(t)):
                if r['rc'] == '0':
                    by.setdefault(r['id'], []).append(float(r['secs']))
    return inputs, sets


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    inputs, sets = load(sys.argv[1:])
    order = sorted(sets, key=lambda s: (s == 'all', int(s) if s.isdigit() else 0))
    gpus = {s: int(s) if s.isdigit() else None for s in order}
    arms = sorted({inputs[i]['arm'] for s in sets.values() for i in s if i in inputs})
    print(f'# PoC latency by GPU count — {", ".join(pathlib.Path(d).name for d in sys.argv[1:])}\n')
    print(f'Median proof time per block size (s); "x" is the speed-up over 1 GPU; "for {TPS} TPS" is the')
    print(f'GPUs a chain of {TPS} TPS needs at that block size, provers working block after block.\n')
    for arm in arms:
        sizes = sorted({int(inputs[i]['txs']) for s in sets.values() for i in s
                        if i in inputs and inputs[i]['arm'] == arm and inputs[i]['set'] == 'sweep'})
        if not sizes:
            continue
        head = ['tx'] + [f'{s} GPU s' for s in order] \
            + [f'{s} GPU x' for s in order if s != '1' and '1' in sets] \
            + [f'{s} GPU for {TPS} TPS' for s in order if gpus[s]]
        print(f'## {arm}\n')
        print('| ' + ' | '.join(head) + ' |')
        print('|' + '---:|' * len(head))
        for n in sizes:
            med = {}
            for s in order:
                per_block = [st.median(v) for i, v in sets[s].items()
                             if i in inputs and inputs[i]['arm'] == arm
                             and inputs[i]['set'] == 'sweep' and int(inputs[i]['txs']) == n]
                med[s] = st.median(per_block) if per_block else math.nan
            row = [f'{n:,}'] + ['–' if math.isnan(med[s]) else f'{med[s]:.2f}' for s in order]
            if '1' in sets:
                row += ['–' if math.isnan(med[s] / med['1']) else f'{med["1"] / med[s]:.2f}'
                        for s in order if s != '1']
            row += ['–' if math.isnan(med[s]) else str(gpus[s] * math.ceil(TPS * med[s] / n))
                    for s in order if gpus[s]]
            print('| ' + ' | '.join(row) + ' |')
        print()


if __name__ == '__main__':
    main()
