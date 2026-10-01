#!/usr/bin/env python3
"""Stage the L2 latency bench's inputs. RUNS ON THE MAC.

    prepare-inputs.py --elf-l2 L2.elf --elf-control CONTROL.elf --elf-mainnet MAINNET.elf \\
        --l2 L2_CORPORA --control CONTROL_CORPORA --mainnet MAINNET_WITNESSES \\
        [--emu ~/.zisk/bin/ziskemu] [--presets 2] [--mainnet-blocks 25815195,25815036,25815092]

L2_CORPORA and CONTROL_CORPORA are monad-zkvm-corpus-gen output directories from the same seeds,
one per arm: a `sweep/` of block sizes and the preset corpora beside it, each with its
manifest.csv. MAINNET_WITNESSES is a zkvm-bench generation (<n>.witness beside <n>.blockhash).

Writes inputs/ next to this script: the three ELFs, every witness framed the way the prover reads
it (LE64 length, the witness, zero padding to 8), and inputs.csv -- one row per input with its
arm, its pair (the same block on the other arm), its size and the public output it must prove.

Every input is replayed through ziskemu first, and a run that does not reproduce what its manifest
recorded stops here: a latency measured on the wrong block is a number about nothing. The steps it
counts are the ones the fits use.
"""
import argparse
import csv
import hashlib
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PRESETS = ('wholesale', 'wholesale-cbdc', 'worker-payouts', 'payouts')


def frame(witness: bytes) -> bytes:
    return struct.pack('<Q', len(witness)) + witness + b'\x00' * ((-(8 + len(witness))) % 8)


def hx(s):
    return bytes.fromhex(s[2:] if s.startswith('0x') else s)


def l2_expect(row):
    return (hx(row['parent_hash']) + hx(row['block_hash']) + hx(row['anchor'])
            + struct.pack('>Q', int(row['number'])))


def replay(emu, elf, framed, expect):
    with tempfile.TemporaryDirectory() as t:
        inp, out = pathlib.Path(t) / 'in.bin', pathlib.Path(t) / 'out.bin'
        inp.write_bytes(framed)
        r = subprocess.run([emu, '-e', str(elf), '-i', str(inp), '-m', '-o', str(out)],
                           capture_output=True, text=True)
        got = out.read_bytes() if out.exists() else b''
    m = re.search(r'steps=(\d+)', r.stdout + r.stderr)
    if got[:len(expect)] != expect or not m:
        sys.exit(f'replay failed (rc={r.returncode}): got {got[:len(expect)].hex()} '
                 f'want {expect.hex()}\n{(r.stdout + r.stderr)[-600:]}')
    return int(m.group(1))


def manifests(root):
    """(set, label, manifest) for the sweep points and the presets under one arm's corpora."""
    out = []
    for m in sorted((root / 'sweep').glob('*/manifest.csv')):
        d = re.search(r'-d(\d+)$', m.parent.name)
        out.append(('sweep', f'd{int(d.group(1)):04d}', m))
    for p in PRESETS:
        m = root / p / 'manifest.csv'
        if m.exists():
            out.append(('preset', p, m))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--elf-l2', required=True)
    ap.add_argument('--elf-control', required=True)
    ap.add_argument('--elf-mainnet', required=True)
    ap.add_argument('--l2', required=True)
    ap.add_argument('--control', required=True)
    ap.add_argument('--mainnet', required=True)
    ap.add_argument('--emu', default=str(pathlib.Path.home() / '.zisk/bin/ziskemu'))
    ap.add_argument('--presets', type=int, default=2, help='blocks per preset corpus')
    ap.add_argument('--mainnet-blocks', default='25815195,25815036,25815092')
    a = ap.parse_args()

    v = subprocess.run([a.emu, '--version'], capture_output=True, text=True).stdout
    if '1.3.1-alpha' not in v:
        sys.exit(f'{a.emu} is not ziskemu 1.3.1-alpha ({v.strip()}): the steps recorded here must '
                 'be the ones the prover on the box executes')

    inp = HERE / 'inputs'
    if inp.exists():
        shutil.rmtree(inp)
    (inp / 'bins').mkdir(parents=True)
    elfs = {}
    for arm, src in (('l2', a.elf_l2), ('control', a.elf_control), ('mainnet', a.elf_mainnet)):
        dst = inp / f'monad-{arm}.elf'
        shutil.copyfile(src, dst)
        elfs[arm] = dst

    rows = []
    for arm, root in (('l2', pathlib.Path(a.l2)), ('control', pathlib.Path(a.control))):
        for kind, label, m in manifests(root):
            picked = list(csv.DictReader(open(m)))
            picked.sort(key=lambda r: int(r['number']))
            if kind == 'preset':
                picked = picked[:a.presets]
            for k, row in enumerate(picked, 1):
                w = m.parent / f"{row['scenario']}-{int(row['number']):08d}.witness"
                framed = frame(w.read_bytes())
                expect = l2_expect(row)
                pair = f'{kind}-{label}-b{k}'
                rid = f'{arm}-{pair}'
                (inp / 'bins' / f'{rid}.bin').write_bytes(framed)
                steps = replay(a.emu, elfs[arm], framed, expect)
                rows.append(dict(id=rid, arm=arm, pair=pair, set=kind, label=label,
                                 number=row['number'], txs=row['txs'], gas=row['gas_used'],
                                 witness_bytes=row['witness_bytes'], steps=steps,
                                 elf=elfs[arm].name, bin=f'bins/{rid}.bin', expect=expect.hex()))
                print(f'{rid:36} {int(row["txs"]):>5} tx {steps:>11,} steps', flush=True)

    mroot = pathlib.Path(a.mainnet)
    for n in a.mainnet_blocks.split(','):
        w = mroot / f'{n}.witness'
        h = (mroot / f'{n}.blockhash').read_text().strip()
        framed = frame(w.read_bytes())
        expect = hx(h)
        rid = f'mainnet-{n}'
        (inp / 'bins' / f'{rid}.bin').write_bytes(framed)
        steps = replay(a.emu, elfs['mainnet'], framed, expect)
        rows.append(dict(id=rid, arm='mainnet', pair=rid, set='mainnet', label=n, number=n,
                         txs='', gas='', witness_bytes=w.stat().st_size, steps=steps,
                         elf=elfs['mainnet'].name, bin=f'bins/{rid}.bin', expect=expect.hex()))
        print(f'{rid:36} {"":>5}    {steps:>11,} steps', flush=True)

    with open(inp / 'inputs.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(inp / 'provenance.txt', 'w') as f:
        f.write(f'ziskemu      {v.strip()}\n')
        for arm, p in elfs.items():
            f.write(f'{p.name:22} sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}\n')
        f.write(f'l2 corpora   {a.l2}\ncontrol      {a.control}\nmainnet      {a.mainnet}\n')
    print(f'{len(rows)} inputs -> {inp}')


if __name__ == '__main__':
    main()
