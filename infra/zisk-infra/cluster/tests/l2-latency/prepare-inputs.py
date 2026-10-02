#!/usr/bin/env python3
"""Stage the L2 latency bench's inputs. RUNS ON THE MAC.

    prepare-inputs.py --elf-l2 L2.elf --elf-control CONTROL.elf --elf-mainnet MAINNET.elf \\
        --l2 L2_CORPORA --control CONTROL_CORPORA --mainnet MAINNET_WITNESSES \\
        [--elf-l2-precompile L2_PRECOMPILE.elf] [--elf-l2-keccak-sw L2_KECCAK_SW.elf] \\
        [--keccak-sw-max-tx 250] \\
        [--l2-poseidon L2_POSEIDON_CORPORA --elf-l2-poseidon L2_POSEIDON.elf \\
         [--elf-l2-poseidon-ksw L2_POSEIDON_KECCAK_SW.elf] [--poseidon-ksw-max-tx N]] \\
        [--l2-poseidon-sig CORPORA --elf-l2-poseidon-sig ELF \\
         [--elf-l2-poseidon-sig-ksw ELF] [--poseidon-sig-ksw-max-tx N]] \\
        [--l2-poseidon-all CORPORA --elf-l2-poseidon-all-ksw ELF] \\
        [--emu ~/.zisk/bin/ziskemu] [--presets 2] [--mainnet-blocks 25815195,25815036,25815092]

--elf-l2-precompile adds a third arm: the L2's witnesses on an L2 guest built with
MONAD_ZKVM_JUMPDEST_SOFTWARE=OFF, so that what the JUMPDEST precompile's instance costs a proof
is measured block for block against the default, which analyses JUMPDESTs in software.

--elf-l2-keccak-sw adds a fourth arm: the L2's witnesses of up to --keccak-sw-max-tx transactions
on an L2 guest built with MONAD_ZKVM_KECCAKF_SOFTWARE=ON (and the memo off), which runs every
Keccak-f in software so that the block plans no Keccakf instance. Its permutations land in Main
and Binary instead, which a block of a hundred transactions already overflows: the larger blocks
would only time that, so they are left out.

--l2-poseidon names corpora generated from the same seeds by a host tree configured with
MONAD_ZKVM_L2_TRIE_HASH=poseidon2: the same blocks, with every trie of the chain built on ZisK's
Poseidon2 precompile instead of keccak. --elf-l2-poseidon proves them on a guest built the same
way, its remaining keccak (signatures, the EVM, code hashes, block hashes) on the Keccak-f
precompile; --elf-l2-poseidon-ksw on one that also runs that remainder in software, on the blocks
of up to --poseidon-ksw-max-tx transactions. A block keeps its pair across the trie hashes, so
every ratio is the same transactions, proven two ways.

--l2-poseidon-sig names corpora of the chain whose signatures are on Poseidon2 as well
(MONAD_ZKVM_L2_SIGNATURE_HASH=poseidon2, with the spoke address that chain derives), and
--elf-l2-poseidon-sig / --elf-l2-poseidon-sig-ksw prove them the two ways the Poseidon2-trie arms
do: the keccak left on the precompile, or in software up to --poseidon-sig-ksw-max-tx.

--l2-poseidon-all names corpora of the chain with everything it defines on Poseidon2 --
MONAD_ZKVM_L2_HASH=poseidon2, the default: tries, signatures, block hash, state blinder, bloom --
and --elf-l2-poseidon-all-ksw proves them with the keccak left (the EVM's, the anchor's, code
hashes) in software, on every block.

L2_CORPORA and CONTROL_CORPORA are monad-zkvm-corpus-gen output directories from the same seeds,
one per arm: a `sweep/` of block sizes and the preset corpora beside it, each with its
manifest.csv, and optionally a `wholesale-sweep/` -- the wholesale preset at more sizes, every
block of which is staged like the sweep's (set `wholesale`, labelled by its distinct count).
MAINNET_WITNESSES is a zkvm-bench generation (<n>.witness beside <n>.blockhash).

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
    """(set, label, manifest) for the sweep points, the presets and the wholesale sweep's points
    under one arm's corpora."""
    out = []
    for m in sorted((root / 'sweep').glob('*/manifest.csv')):
        d = re.search(r'-d(\d+)$', m.parent.name)
        out.append(('sweep', f'd{int(d.group(1)):04d}', m))
    for p in PRESETS:
        m = root / p / 'manifest.csv'
        if m.exists():
            out.append(('preset', p, m))
    for m in sorted((root / 'wholesale-sweep').glob('*/manifest.csv')):
        d = re.search(r'-d(\d+)$', m.parent.name)
        out.append(('wholesale', f'd{int(d.group(1)):04d}', m))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--elf-l2', required=True)
    ap.add_argument('--elf-control', required=True)
    ap.add_argument('--elf-mainnet', required=True)
    ap.add_argument('--elf-l2-precompile')
    ap.add_argument('--elf-l2-keccak-sw')
    ap.add_argument('--keccak-sw-max-tx', type=int, default=250,
                    help='largest block the --elf-l2-keccak-sw arm proves')
    ap.add_argument('--l2-poseidon', help='corpora of the Poseidon2-trie chain, same seeds')
    ap.add_argument('--elf-l2-poseidon')
    ap.add_argument('--elf-l2-poseidon-ksw')
    ap.add_argument('--poseidon-ksw-max-tx', type=int, default=None,
                    help='largest block the --elf-l2-poseidon-ksw arm proves (default: every one)')
    ap.add_argument('--l2-poseidon-sig', help='corpora of the chain with Poseidon2 signatures too')
    ap.add_argument('--elf-l2-poseidon-sig')
    ap.add_argument('--elf-l2-poseidon-sig-ksw')
    ap.add_argument('--poseidon-sig-ksw-max-tx', type=int, default=None,
                    help='largest block the --elf-l2-poseidon-sig-ksw arm proves (default: every one)')
    ap.add_argument('--l2-poseidon-all', help='corpora of the chain with everything on Poseidon2')
    ap.add_argument('--elf-l2-poseidon-all-ksw')
    ap.add_argument('--source', default='', help='where the ELFs were built from, for provenance.txt')
    ap.add_argument('--l2', required=True)
    ap.add_argument('--control', required=True)
    ap.add_argument('--mainnet', required=True)
    ap.add_argument('--emu', default=str(pathlib.Path.home() / '.zisk/bin/ziskemu'))
    ap.add_argument('--presets', type=int, default=2, help='blocks per preset corpus')
    ap.add_argument('--mainnet-blocks', default='25815195,25815036,25815092')
    a = ap.parse_args()
    if (a.elf_l2_poseidon or a.elf_l2_poseidon_ksw) and not a.l2_poseidon:
        sys.exit('the Poseidon2-trie arms prove the Poseidon2-trie corpora: give --l2-poseidon')
    if (a.elf_l2_poseidon_sig or a.elf_l2_poseidon_sig_ksw) and not a.l2_poseidon_sig:
        sys.exit('the Poseidon2-signature arms prove that chain\'s corpora: give --l2-poseidon-sig')
    if a.elf_l2_poseidon_all_ksw and not a.l2_poseidon_all:
        sys.exit('the all-Poseidon2 arm proves that chain\'s corpora: give --l2-poseidon-all')

    v = subprocess.run([a.emu, '--version'], capture_output=True, text=True).stdout
    if '1.3.1-alpha' not in v:
        sys.exit(f'{a.emu} is not ziskemu 1.3.1-alpha ({v.strip()}): the steps recorded here must '
                 'be the ones the prover on the box executes')

    inp = HERE / 'inputs'
    if inp.exists():
        shutil.rmtree(inp)
    (inp / 'bins').mkdir(parents=True)
    elfs = {}
    given = [('l2', a.elf_l2), ('control', a.elf_control), ('mainnet', a.elf_mainnet)]
    if a.elf_l2_precompile:
        given.append(('l2-precompile', a.elf_l2_precompile))
    if a.elf_l2_keccak_sw:
        given.append(('l2-keccak-sw', a.elf_l2_keccak_sw))
    if a.elf_l2_poseidon:
        given.append(('l2-poseidon', a.elf_l2_poseidon))
    if a.elf_l2_poseidon_ksw:
        given.append(('l2-poseidon-ksw', a.elf_l2_poseidon_ksw))
    if a.elf_l2_poseidon_sig:
        given.append(('l2-poseidon-sig', a.elf_l2_poseidon_sig))
    if a.elf_l2_poseidon_sig_ksw:
        given.append(('l2-poseidon-sig-ksw', a.elf_l2_poseidon_sig_ksw))
    if a.elf_l2_poseidon_all_ksw:
        given.append(('l2-poseidon-all-ksw', a.elf_l2_poseidon_all_ksw))
    for arm, src in given:
        dst = inp / f'monad-{arm}.elf'
        shutil.copyfile(src, dst)
        elfs[arm] = dst

    rows = []
    # (arm, corpora, largest block it proves)
    arms = [('l2', pathlib.Path(a.l2), None), ('control', pathlib.Path(a.control), None)]
    if a.elf_l2_precompile:
        arms.append(('l2-precompile', pathlib.Path(a.l2), None))
    if a.elf_l2_keccak_sw:
        arms.append(('l2-keccak-sw', pathlib.Path(a.l2), a.keccak_sw_max_tx))
    if a.elf_l2_poseidon:
        arms.append(('l2-poseidon', pathlib.Path(a.l2_poseidon), None))
    if a.elf_l2_poseidon_ksw:
        arms.append(('l2-poseidon-ksw', pathlib.Path(a.l2_poseidon), a.poseidon_ksw_max_tx))
    if a.elf_l2_poseidon_sig:
        arms.append(('l2-poseidon-sig', pathlib.Path(a.l2_poseidon_sig), None))
    if a.elf_l2_poseidon_sig_ksw:
        arms.append(('l2-poseidon-sig-ksw', pathlib.Path(a.l2_poseidon_sig),
                     a.poseidon_sig_ksw_max_tx))
    if a.elf_l2_poseidon_all_ksw:
        arms.append(('l2-poseidon-all-ksw', pathlib.Path(a.l2_poseidon_all), None))
    for arm, root, max_tx in arms:
        for kind, label, m in manifests(root):
            picked = list(csv.DictReader(open(m)))
            picked.sort(key=lambda r: int(r['number']))
            if kind == 'preset':
                picked = picked[:a.presets]
            for k, row in enumerate(picked, 1):
                if max_tx is not None and int(row['txs']) > max_tx:
                    continue
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
        if a.source:
            f.write(f'source       {a.source}\n')
        for arm, p in elfs.items():
            f.write(f'{p.name:22} sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}\n')
        f.write(f'l2 corpora   {a.l2}\ncontrol      {a.control}\nmainnet      {a.mainnet}\n')
        if a.l2_poseidon:
            f.write(f'l2 poseidon  {a.l2_poseidon}\n')
        if a.l2_poseidon_sig:
            f.write(f'l2 pos. sig. {a.l2_poseidon_sig}\n')
        if a.l2_poseidon_all:
            f.write(f'l2 pos. all  {a.l2_poseidon_all}\n')
    print(f'{len(rows)} inputs -> {inp}')


if __name__ == '__main__':
    main()
