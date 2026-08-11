#!/usr/bin/env python3
"""Write the FULL expected public values for a witness set: 96 bytes per block,
post-state root || pre-state root || block hash — the three values the guest
commits to since the soundness binding.

Provenance of each third, because they do not come from the same place:
  post  the set's own .post_state_root, produced by replay, which validates every
        state root against the canonical mainnet header before writing it.
  pre   the PREVIOUS block's .post_state_root — same source, so this third is as
        independent as the post third. The first block of a set has no local
        parent and is skipped (reported, not silently dropped).
  hash  keccak(header RLP), derived from the block RLP the witness carries. That
        alone is only a format check — so it is cross-checked against the NEXT
        block's parent_hash, giving one independent confirmation per consecutive
        pair, over the whole set, with no RPC.

Usage: ./gen-expected-pv.py <witness-dir> [--check-only]
"""
import os, sys, glob

# keccak-256, bundled. A fixture-generation tool has to run wherever the fixtures
# are, and the Mac's system python has neither pycryptodome nor hashlib's keccak
# (hashlib ships SHA-3, which pads differently and is NOT this). Self-check below.
_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M


def _keccak_f(a):
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y] & _M) & b[(x + 2) % 5][y])
        a[0][0] ^= _RC[rnd]
    return a


def kec(data):
    """keccak-256 (Ethereum's, 0x01 domain byte — not SHA3-256's 0x06)."""
    rate = 136
    a = [[0] * 5 for _ in range(5)]
    pad = bytearray(data)
    pad.append(0x01)
    while len(pad) % rate != 0:
        pad.append(0x00)
    pad[-1] |= 0x80
    for off in range(0, len(pad), rate):
        for i in range(rate // 8):
            w = int.from_bytes(pad[off + 8 * i:off + 8 * i + 8], 'little')
            a[i % 5][i // 5] ^= w
        _keccak_f(a)
    out = b''
    for i in range(4):
        out += a[i % 5][i // 5].to_bytes(8, 'little')
    return out


assert kec(b'').hex() == \
    'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470', 'keccak self-check'


def rlp_item(b, i):
    """-> (payload_start, payload_end, item_end, is_list)"""
    x = b[i]
    if x < 0x80:
        return i, i + 1, i + 1, False
    if x < 0xb8:
        n = x - 0x80
        return i + 1, i + 1 + n, i + 1 + n, False
    if x < 0xc0:
        ln = x - 0xb7
        n = int.from_bytes(b[i + 1:i + 1 + ln], 'big')
        return i + 1 + ln, i + 1 + ln + n, i + 1 + ln + n, False
    if x < 0xf8:
        n = x - 0xc0
        return i + 1, i + 1 + n, i + 1 + n, True
    ln = x - 0xf7
    n = int.from_bytes(b[i + 1:i + 1 + ln], 'big')
    return i + 1 + ln, i + 1 + ln + n, i + 1 + ln + n, True


def witness_block_rlp(w):
    """Section [0] of the witness container is the block RLP, as a string."""
    p0, q0, e0, is_list = rlp_item(w, 0)
    assert is_list and e0 == len(w), 'witness is not a single RLP list'
    ps, qs, es, _ = rlp_item(w, p0)
    return w[ps:qs]


def header_and_parent(block_rlp):
    """(header RLP bytes, parent_hash) — header is the block list's first item,
    parent_hash its own first field."""
    p, q, e, is_list = rlp_item(block_rlp, 0)
    assert is_list, 'block is not a list'
    hs, hq, he, h_is_list = rlp_item(block_rlp, p)
    assert h_is_list, 'header is not a list'
    header = block_rlp[hs - (he - hq) - 0:he] if False else block_rlp[p:he]
    fp, fq, fe, _ = rlp_item(block_rlp, hs)
    return header, block_rlp[fp:fq]


def load_root(path):
    h = open(path).read().strip().lower().removeprefix('0x')
    h = ''.join(c for c in h if c in '0123456789abcdef')
    return bytes.fromhex(h)


def main():
    d = sys.argv[1]
    check_only = '--check-only' in sys.argv
    blocks = sorted(int(os.path.basename(f)[:-8])
                    for f in glob.glob(os.path.join(d, '*.witness')))
    post, bhash, parent = {}, {}, {}
    for b in blocks:
        pr = os.path.join(d, f'{b}.post_state_root')
        if os.path.exists(pr):
            post[b] = load_root(pr)
        w = open(os.path.join(d, f'{b}.witness'), 'rb').read()
        blk = witness_block_rlp(w)
        hdr, par = header_and_parent(blk)
        bhash[b] = kec(hdr)
        parent[b] = par

    # cross-check: hash(N) must equal parent_hash(N+1)
    ok = bad = 0
    for b in blocks:
        if b + 1 in parent:
            (ok := ok + 1) if bhash[b] == parent[b + 1] else (bad := bad + 1)
    print(f'block-hash chain: {ok} consecutive pairs agree, {bad} disagree')
    if bad:
        sys.exit('refusing to write expected values with a broken hash chain')

    written = skipped = 0
    for b in blocks:
        if b not in post or (b - 1) not in post:
            skipped += 1
            continue
        pv = post[b] + post[b - 1] + bhash[b]
        assert len(pv) == 96
        if not check_only:
            open(os.path.join(d, f'{b}.expected_pv'), 'wb').write(pv)
        written += 1
    verb = 'would write' if check_only else 'wrote'
    print(f'{verb} {written} .expected_pv (96 B each); '
          f'{skipped} skipped for want of a local parent post-root')


if __name__ == '__main__':
    main()
