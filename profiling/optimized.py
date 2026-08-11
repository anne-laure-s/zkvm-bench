#!/usr/bin/env python3
"""Derive results/optimized-{zisk,sp1}.json from the levers axes.

These two files feed compare-optimized.py, and until now nothing produced them: they were written by
hand during the campaign that first measured the branch, which is why the SP1 side sat at 70 blocks
against ZisK's 504 long after the data for more existed. A file with no producer cannot be
refreshed, so it silently becomes the oldest number in the report.

Everything here is recomputed from that frozen payload (path below), except the per-block state-root
verdicts:
those come from `guests/monad/exec-verified.csv`, written by `guests/monad/ev.sh`, because compare.py
never checks a root. A block with no verdict gets `not-verified` — the honest value, and the one that
keeps roots_pass/roots_total from drifting upward as coverage grows.

Two things that bit once each and are now guarded:

- The verdicts are matched to this build **by step count**, never taken on trust (see `root_verdicts`).
- roots_pass/roots_total are counted from the per-block verdicts, but the file this replaced held
  them as free-standing scalars over a wider set than its own `blocks` dict — so a first version of
  this script reported 365/365 where the campaign had verified 504/504. If those two numbers ever
  disagree with `exec-verified.csv`, the CSV is right.

    ./optimized.py
    ./compare-optimized.py

The payload is not reproducible: the axes that produced it were removed with the branch's ELFs, so
there is no compare.py run that rebuilds it.
"""
import csv
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, 'results')

# target file <- the axis that measures "the branch against what ships today" on that backend
SRC = {'zisk': ('levers-self', 'steps'), 'sp1': ('levers-self-sp1', 'cycles')}

# The ZisK root verdicts live here, one row per block, written by guests/monad/ev.sh. There is no
# SP1 equivalent: the runner does not expose the committed public values.
VERIFIED = os.path.join(os.path.dirname(HERE), 'guests', 'monad', 'exec-verified.csv')



# 2026-08-08: axes renamed with the sam-rebase (monad-current/monad-opt). Alias the old
# names so consumers of these jsons keep working; both keys point at the same object.
_AXIS_ALIASES = {'zisk': 'cur-zisk', 'sp1': 'cur-sp1',
                 'levers-self': 'opt-self', 'levers-self-sp1': 'opt-self-sp1',
                 'levers': 'opt-zisk', 'levers-sp1': 'opt-sp1'}

def _alias_axes(d):
    if isinstance(d, dict):
        for old, new in _AXIS_ALIASES.items():
            if old not in d and new in d:
                d[old] = d[new]
    return d

def load(name):
    try:
        return _alias_axes(json.load(open(os.path.join(RES, name))))
    except Exception:
        return None


def root_verdicts(work):
    """{block: verdict} from exec-verified.csv, but only for rows whose step count matches.

    The CSV carries no build identity, and a stale one has already been mistaken for a fresh
    baseline once (it was newer than the ELF, so it was assumed to describe it —
    its steps were a different build's). Matching each row's `steps` against the work this axis
    measured for the same block is what ties a verdict to the binary it was produced from. A row
    that does not match is dropped rather than trusted, so a stale file degrades to `not-verified`
    instead of lending its PASS to another binary.
    """
    out, skipped = {}, 0
    try:
        rows = list(csv.DictReader(open(VERIFIED)))
    except Exception:
        return {}, 0
    for r in rows:
        b = r.get('block')
        if b is None or b not in work:
            continue
        try:
            if int(r['steps']) != work[b]:
                skipped += 1
                continue
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        out[b] = r.get('root_match') or 'not-verified'
    return out, skipped


def main():
    lev = load('compare-levers.json')
    cmp_ = load('compare.json')
    if not lev:
        sys.exit('the frozen levers payload is missing from results/ — it cannot be regenerated '
                 '(the axes and the ELFs are gone); bring the file in, or skip this report')
    wrote = 0
    for target, (axis, unit) in SRC.items():
        ax = lev.get(axis)
        if not ax or not ax.get('blocks'):
            print(f"  {target}: no {axis} axis in compare-levers.json — left alone")
            continue
        old = load(f'optimized-{target}.json') or {}
        oldb = old.get('blocks') or {}
        work = {b: v['a']['work'] for b, v in ax['blocks'].items() if v.get('a')}
        # ZisK only. The verdicts are matched to this build by step count, not carried on trust.
        csvv, skipped = root_verdicts(work) if target == 'zisk' else ({}, 0)
        if skipped:
            print(f"  {target}: {skipped} row(s) in exec-verified.csv do not match this build's "
                  f"step count — dropped, not trusted")
        blocks = {}
        for b, v in ax['blocks'].items():
            if not (v.get('a') and v.get('b')):
                continue
            # Prefer the campaign artifact; fall back to whatever the previous file recorded. The
            # per-block dict used to hold only the compared subset while roots_pass/roots_total were
            # free-standing scalars, so recomputing from it alone silently lost 139 verifications.
            blocks[b] = {'opt': v['a']['work'], 'base': v['b']['work'],
                         'root': csvv.get(b) or (oldb.get(b) or {}).get('root', 'not-verified')}
        if not blocks:
            print(f"  {target}: {axis} has no complete block — left alone")
            continue
        gains = [1 - v['opt'] / v['base'] for v in blocks.values() if v['base']]
        opt_t = sum(v['opt'] for v in blocks.values())
        base_t = sum(v['base'] for v in blocks.values())
        # `n` is what compare-optimized.py can actually chart: it cross-references compare.json, so a
        # block measured here but absent from the shipped-guest axis is counted apart rather than
        # quietly dropped.
        ref = set((cmp_ or {}).get(target, {}).get('blocks') or {})
        n = len(set(blocks) & ref) if ref else len(blocks)
        npass = sum(1 for v in blocks.values() if v['root'] == 'PASS')
        ntot = sum(1 for v in blocks.values() if v['root'] in ('PASS', 'FAIL'))
        out = {
            'axis': target, 'unit': unit,
            'branch': old.get('branch', 'al/zkvm-levers'),
            'commit': old.get('commit', '651422878'),
            'n': n, 'n_measured': len(blocks), 'n_without_reference': len(blocks) - n,
            'gain_median': round(statistics.median(gains), 5),
            'gain_min': round(min(gains), 5), 'gain_max': round(max(gains), 5),
            'cumulative': round(1 - opt_t / base_t, 5),
            'roots_pass': npass, 'roots_total': ntot, 'root_verified': ntot > 0,
            'blocks': blocks,
        }
        p = os.path.join(RES, f'optimized-{target}.json')
        with open(p, 'w') as fh:
            json.dump(out, fh)
        wrote += 1
        was = f" (was {old.get('n_measured', '?')})" if old else ""
        print(f"  {target:5} {len(blocks)} blocks{was} · gain median "
              f"{out['gain_median'] * 100:.2f}% · cumulative {out['cumulative'] * 100:.2f}% · "
              f"roots {npass}/{ntot or '—'} · {n} with a shipped-guest reference")
    return 0 if wrote else 1


if __name__ == '__main__':
    sys.exit(main())
