#!/usr/bin/env python3
"""The two checks that bracket the timing. RUNS ON THE BOX.

    check.py emu     <inputs dir> <out.csv>     before: ziskemu on every input ONLY selects
    check.py publics <inputs dir> <results dir> after: every kept proof, verified and read back

`emu` runs this box's ziskemu on every input and requires the public output its block's manifest
recorded and the step count prepare-inputs.py recorded on the Mac. A mismatch means the bundle is
not what was staged -- another ELF, another release, a truncated input -- and is cheaper to learn
here than after an hour of GPU time.

`publics` runs zisk-publics on every proof the timing kept: it verifies the proof and prints the
public values it commits to, which must be the block's. A timing is only a timing of THIS block if
its proof says so.
"""
import csv
import os
import re
import pathlib
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ZISK = pathlib.Path(os.environ.get('ZISK_HOME', pathlib.Path.home() / '.zisk')) / 'bin'


def rows(inputs):
    return list(csv.DictReader(open(inputs / 'inputs.csv')))


def emu(inputs, out_csv):
    def one(r):
        with tempfile.TemporaryDirectory() as t:
            out = pathlib.Path(t) / 'out.bin'
            p = subprocess.run([str(ZISK / 'ziskemu'), '-e', str(inputs / r['elf']),
                                '-i', str(inputs / r['bin']), '-m', '-o', str(out)],
                               capture_output=True, text=True)
            got = out.read_bytes() if out.exists() else b''
        txt = p.stdout + p.stderr
        m = re.search(r'steps=(\d+)', txt)
        steps = m.group(1) if m else ''
        want = bytes.fromhex(r['expect'])
        ok = got[:len(want)] == want and steps == r['steps']
        return r['id'], ok, p.returncode, steps, r['steps']

    rs = [r for r in rows(inputs) if re.search(os.environ.get('ONLY', '.'), r['id'])]
    with ThreadPoolExecutor(max(1, (os.cpu_count() or 2) // 2)) as ex:
        res = list(ex.map(one, rs))
    with open(out_csv, 'w') as f:
        f.write('id,ok,rc,steps,steps_staged\n')
        for r in res:
            f.write(','.join(map(str, r)) + '\n')
    bad = [r for r in res if not r[1]]
    print(f'ziskemu: {len(res) - len(bad)}/{len(res)} inputs publish their block and run the staged step count')
    for r in bad[:10]:
        print(f'  BAD {r[0]} rc={r[2]} steps={r[3]} staged={r[4]}')
    return 1 if bad else 0


def publics(inputs, results):
    tool = os.environ.get('ZISK_PUBLICS', 'zisk-publics')
    # zisk-publics verifies a proof against the setup key the proof itself carries, which a proof
    # made with any other setup would satisfy just as well. Each proof is therefore verified a
    # second time pinned to the final circuit's key of the install being timed (ZISK_HOME), so
    # that "verifies" means "verifies under the key this run timed".
    home = pathlib.Path(os.environ.get('ZISK_HOME', pathlib.Path.home() / '.zisk'))
    found = sorted(home.glob('provingKey/zisk/vadcop_final/vadcop_final.verkey.json')) \
        + sorted(home.glob('verifyKey/**/vadcop_final.verkey.json'))
    vk = found[0] if found else None
    if vk is None:
        print(f'  !! no vadcop_final.verkey.json under {home}: proofs are checked unpinned')
    by_id = {r['id']: r for r in rows(inputs)}
    proofs = sorted(p for p in results.rglob('proofs/*.proof') if not p.name.startswith('warm'))
    out = []
    for p in proofs:
        rid = p.name.split('.')[0]
        kind = p.name[len(rid) + 1:-len('.proof')] or 'stark'
        r = by_id.get(rid)
        if r is None:
            continue
        want = bytes.fromhex(r['expect'])
        q = subprocess.run([tool, str(p), str(len(want))], capture_output=True)
        ok = q.returncode == 0 and q.stdout[:len(want)] == want
        if vk is None:
            pinned = 'no-key'
        else:
            v = subprocess.run(['cargo-zisk', 'verify', '-p', str(p), '--setup-vk', str(vk)], capture_output=True)
            pinned = v.returncode == 0
        out.append((p.relative_to(results), rid, kind, ok and pinned is not False, q.returncode, pinned))
        if not ok:
            print(f'  BAD {p.relative_to(results)} rc={q.returncode} '
                  f'{q.stderr.decode(errors="replace").strip()[-200:]}')
        elif pinned is False:
            print(f'  BAD {p.relative_to(results)} does not verify under {vk}')
    with open(results / 'publics.csv', 'w') as f:
        f.write('proof,id,kind,ok,rc,pinned\n')
        for o in out:
            f.write(','.join(map(str, o)) + '\n')
    good = sum(1 for o in out if o[3])
    print(f'zisk-publics: {good}/{len(out)} proofs verify{f" under {vk}" if vk else ""} and commit to their block')
    return 0 if out and good == len(out) else 1


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] not in ('emu', 'publics'):
        sys.exit(__doc__)
    inputs, target = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    sys.exit(emu(inputs, target) if sys.argv[1] == 'emu' else publics(inputs, target))
