#!/usr/bin/env python3
"""elf-id.py — name a guest ELF by the PROGRAM it is, not by the bytes of its file.

The two are not the same, and a parallel lineage walk is where that stops being academic. Cargo's
unit hash for a path package includes the package's absolute path, so the same commit built in two
worktrees gets two values of it -- measured here, three concurrent builds of 5c331227b produced
three ELFs of identical length differing in 19 bytes, every one of them inside `.strtab`, all in
the rustc codegen-unit symbol `monad_zkvm_zisk.<hash>-cgu.0`. None of those bytes lies in a PT_LOAD
segment, and the three measured identically: 74,675,348 steps and COST 9,608,869,826 apiece.

The file sha is what names an ELF in the cache and what keys every measurement, so left alone that
would re-measure a lineage from scratch each time the number of workers changed -- hours of
emulator time to relearn numbers already in the table. Hashing what the emulator actually loads
makes the identity independent of the worktree, of the worker count and of any future churn in how
rustc names its codegen units.

The hash covers, for every PT_LOAD segment in address order: its virtual address, its memory size,
and its file-backed contents -- plus the entry point. Not the file offsets: those are layout. The
symbol table stays in the file, because symcost.py and hotspots.py attribute cost through it.

  elf-id.py <elf>                     print the program id
  elf-id.py --dir D --adopt <elf> [--prefer TSV]
                                      print "<sha16> new|reused": the name under which D already
                                      knows this program, moving or discarding <elf> accordingly.
                                      --prefer names an index whose column 4 holds the shas this
                                      lineage already uses, and one of those wins over any other
                                      name for the same program.
"""
import hashlib
import os
import struct
import sys

MAP = '.program-id.tsv'


def program_id(path):
    with open(path, 'rb') as fh:
        f = fh.read()
    if len(f) < 0x40 or f[:4] != b'\x7fELF':
        raise ValueError(f'{path}: not an ELF')
    e_entry, = struct.unpack_from('<Q', f, 0x18)
    phoff, = struct.unpack_from('<Q', f, 0x20)
    phentsize, = struct.unpack_from('<H', f, 0x36)
    phnum, = struct.unpack_from('<H', f, 0x38)
    segs = []
    for i in range(phnum):
        o = phoff + i * phentsize
        p_type, = struct.unpack_from('<I', f, o)
        if p_type != 1:                              # PT_LOAD
            continue
        p_offset, = struct.unpack_from('<Q', f, o + 8)
        p_vaddr, = struct.unpack_from('<Q', f, o + 16)
        p_filesz, = struct.unpack_from('<Q', f, o + 32)
        p_memsz, = struct.unpack_from('<Q', f, o + 40)
        segs.append((p_vaddr, p_offset, p_filesz, p_memsz))
    if not segs:
        raise ValueError(f'{path}: no PT_LOAD segment')
    h = hashlib.sha256()
    h.update(struct.pack('<Q', e_entry))
    for p_vaddr, p_offset, p_filesz, p_memsz in sorted(segs):
        h.update(struct.pack('<QQQ', p_vaddr, p_memsz, p_filesz))
        h.update(f[p_offset:p_offset + p_filesz])
    return h.hexdigest()[:16]


def file_id(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_map(d):
    """program id -> [file sha], dropping entries whose file is gone.

    A LIST and not one name, because one program does live under several names here: 69 of the
    585 programs in this cache do, the same guest rebuilt in another worktree by an earlier
    campaign. Collapsing them to whichever name sorted first would silently rename 66 of r10's
    148 rows to r6's copy of the same binary -- every one of them then a measurement this table
    has already paid for and would pay for again."""
    out = {}
    p = os.path.join(d, MAP)
    if not os.path.exists(p):
        return out
    for line in open(p):
        q = line.rstrip('\n').split('\t')
        if len(q) == 2 and os.path.exists(os.path.join(d, q[1] + '.elf')):
            if q[1] not in out.setdefault(q[0], []):
                out[q[0]].append(q[1])
    return out


def refresh(d, known):
    """Bring the map up to date with the cache. Rewritten whole, and only when something is
    missing: the walk calls this once per build, and rescanning 1,100 ELFs each time would cost
    more than it saves."""
    have = {sha for shas in known.values() for sha in shas}
    merged = {pid: list(shas) for pid, shas in known.items()}
    added = 0
    for name in sorted(os.listdir(d)):
        if not name.endswith('.elf'):
            continue
        sha = name[:-4]
        # tmp-<commit>.elf is a build in flight, not a cache entry.
        if len(sha) != 16 or sha in have:
            continue
        try:
            pid = program_id(os.path.join(d, name))
        except (ValueError, OSError):
            continue
        merged.setdefault(pid, []).append(sha)
        added += 1
    if not added:
        return known
    tmp = os.path.join(d, MAP + f'.tmp.{os.getpid()}')
    with open(tmp, 'w') as fh:
        for pid, shas in sorted(merged.items()):
            for sha in shas:
                fh.write(f'{pid}\t{sha}\n')
    os.replace(tmp, os.path.join(d, MAP))
    return merged


def index_shas(p):
    """Column 4 of a lineage index: the names that lineage already measures under."""
    out = set()
    if not p or not os.path.exists(p):
        return out
    for line in open(p):
        q = line.rstrip('\n').split('\t')
        if len(q) > 3 and len(q[3]) == 16:
            out.add(q[3])
    return out


def pick(shas, prefer):
    for sha in shas:
        if sha in prefer:
            return sha
    return shas[0] if shas else None


def adopt(d, path, prefer_index=None):
    pid = program_id(path)
    prefer = index_shas(prefer_index)
    known = load_map(d)
    sha = pick(known.get(pid, []), prefer)
    if sha is None:
        known = refresh(d, known)
        sha = pick(known.get(pid, []), prefer)
    if sha is not None and os.path.exists(os.path.join(d, sha + '.elf')):
        os.remove(path)
        return sha, 'reused'
    sha = file_id(path)
    os.replace(path, os.path.join(d, sha + '.elf'))
    # Appended, not rewritten: several workers adopt at once, and a rewrite would drop whatever
    # another one recorded between the read and the write. A duplicate line is harmless -- the
    # reader takes the first, and both name a file with this exact program in it.
    with open(os.path.join(d, MAP), 'a') as fh:
        fh.write(f'{pid}\t{sha}\n')
    return sha, 'new'


def main(argv):
    if '--adopt' in argv:
        d = argv[argv.index('--dir') + 1]
        path = argv[argv.index('--adopt') + 1]
        prefer = argv[argv.index('--prefer') + 1] if '--prefer' in argv else None
        sha, how = adopt(d, path, prefer)
        print(f'{sha}\t{how}')
        return 0
    if len(argv) != 1:
        print('usage: elf-id.py <elf> | elf-id.py --dir D --adopt <elf>', file=sys.stderr)
        return 2
    print(program_id(argv[0]))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
