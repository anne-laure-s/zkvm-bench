"""Profile cache — per-block files, builds addressed by content. Format: cache-format.md.

Replaces a single results/compare-cache.json keyed `{axis}/{guest}/{block}/{stamp}`. Two things that
key got wrong and this does not:

  the axis   contributed nothing to a result, so the same execution was stored once per axis that
             asked for it. compare_xget() used to paper over it by borrowing across axes at lookup
             time and writing the hit through — which is what created the copies in the first place.
             Here a block file holds one slot per build, and borrowing is not a concept.

  the stamp  was the ELF's mtime. ELFs built in the same second share one, and a `cp` changes one
             without changing a byte, so it identifies nothing on its own — only the guest NAME kept
             two builds apart, which put a human label in charge of cache correctness. Identity here
             is sha256 of the ELF.

Loading is lazy and per block: a run over 50 blocks reads 50 small files instead of a 155 MB dict.
Writing is per block and merge-then-write, so two runs touching different blocks never contend.
"""
import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_ROOT = os.path.join(HERE, 'cache')

RUN, PROFILE, PROFILE_FULL = 'run', 'profile', 'profile_full'


class Cache:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = root
        self.blocks_dir = os.path.join(root, 'blocks')
        self.index_path = os.path.join(root, 'builds.json')
        self._blocks = {}          # block -> file contents, loaded on demand
        self._dirty = set()
        self._ident = {}           # (abs elf path, mtime) -> identity
        self._inputs = {}          # (path, mtime_ns, size) -> input fingerprint
        self._by_ident = None      # identity -> {block: profile}, built on first use
        try:
            with open(self.index_path) as fh:
                self._index = json.load(fh)
        except Exception:
            self._index = {}
        self._index_dirty = False

    # ── identity ────────────────────────────────────────────────────────────
    def identity(self, elf):
        """sha256:<16 hex> of the ELF. Memoised on (path, mtime), so a rebuild re-hashes and anything
        measured against the old bytes stops resolving — which is the point."""
        p = elf if os.path.isabs(elf) else os.path.join(ROOT, elf)
        st = int(os.path.getmtime(p))
        hit = self._ident.get((p, st))
        if hit:
            return hit
        d = hashlib.sha256()
        with open(p, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                d.update(chunk)
        ident = 'sha256:' + d.hexdigest()[:16]
        self._ident[(p, st)] = ident
        return ident

    def input_fp(self, path):
        """Fingerprint of an INPUT file: {'id': sha256:…, 'sz':…, 'mt':…}.

        A measurement is a function of (build, input), and the input was never part of the key — so a
        re-minted witness silently served the previous one's numbers. Measured case: block 25229951
        has two zisk-reth witnesses 122 kB apart (`fixtures/` vs `inputs/`), and the cache held two
        contradictory `work` values under one key.

        Size and mtime are carried alongside the hash purely as a fast path: on a cache hit they
        usually match and no 10 MB file has to be read. They are never trusted to decide equality —
        a `cp` moves mtime without moving a byte, so a mismatch falls through to the hash."""
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = self._inputs.get(key)
        if hit:
            return hit
        d = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                d.update(chunk)
        fp = {'id': 'sha256:' + d.hexdigest()[:16], 'sz': st.st_size, 'mt': int(st.st_mtime)}
        self._inputs[key] = fp
        return fp

    def _input_matches(self, slot, path):
        """Was this slot measured on the file now at `path`? Unknown (no record) counts as a match:
        entries migrated from the old cache have none, and rejecting them all would mean re-measuring
        everything to learn what is in fact almost always still valid."""
        rec = slot.get('input')
        if not rec or path is None:
            return True
        try:
            st = os.stat(path)
        except OSError:
            return True
        if rec.get('sz') == st.st_size and rec.get('mt') == int(st.st_mtime):
            return True                                  # fast path: nothing moved
        return rec.get('id') == self.input_fp(path)['id']

    def register(self, elf, name=None, backend=None):
        """Record what a build IS. Metadata lives here once, never in the block files: a build appears
        in hundreds of them, so copying it in would give every field a chance to drift."""
        ident = self.identity(elf)
        p = elf if os.path.isabs(elf) else os.path.join(ROOT, elf)
        rel = os.path.relpath(p, ROOT)
        e = self._index.setdefault(ident, {'names': [], 'backend': backend, 'elf': rel, 'mtimes': []})
        if e.get('elf') != rel:
            # `elf` is where this build was LAST SEEN, not part of its identity — a build keeps its
            # sha through a move, so the path must be refreshed or it goes on pointing at a name that
            # no longer exists. (setdefault above only fills it for a new entry.)
            e['elf'] = rel; self._index_dirty = True
        st = str(int(os.path.getmtime(p)))
        if name and name not in e['names']:
            e['names'].append(name); self._index_dirty = True
        if st not in e['mtimes']:
            e['mtimes'].append(st); self._index_dirty = True
        if backend and not e.get('backend'):
            e['backend'] = backend; self._index_dirty = True
        return ident

    # ── block files ─────────────────────────────────────────────────────────
    def _path(self, block):
        return os.path.join(self.blocks_dir, f'{block}.json')

    def _load(self, block):
        b = self._blocks.get(str(block))
        if b is None:
            try:
                with open(self._path(block)) as fh:
                    b = json.load(fh)
            except Exception:
                b = {'v': 1, 'block': int(block), 'chain': {}, 'builds': {}}
            self._blocks[str(block)] = b
        return b

    # ── read / write ────────────────────────────────────────────────────────
    def get(self, elf, block, kind, inp=None):
        """One build's `run` or `profile` for a block, or None. A miss is a miss: there is no
        cross-axis fallback to borrow from, because the axis is not part of the key any more.

        Pass `inp` and an entry measured on a different input is treated as absent, so it is
        re-measured rather than served."""
        try:
            ident = self.identity(elf)
        except OSError:
            return None
        slot = self._load(block)['builds'].get(ident)
        if not slot or not self._input_matches(slot, inp):
            return None
        return slot.get(kind)

    def put(self, elf, block, kind, value, name=None, backend=None, inp=None):
        ident = self.register(elf, name, backend)
        b = self._load(block)
        slot = b['builds'].setdefault(ident, {})
        if inp is not None:
            fp = self.input_fp(inp)
            if slot.get('input') and slot['input'].get('id') != fp['id']:
                # The input changed under this build: `run` and `profile` were both measured on the
                # old one, so neither survives. Drop the slot rather than leave a half-refreshed mix.
                slot.clear()
            slot['input'] = fp
        slot[kind] = value
        if kind == RUN and isinstance(value, dict):
            # gas and tx count describe the BLOCK, not the guest — only the reth ZisK guest measures
            # them, and every axis needs them. Mirrored to the block so no consumer has to know which
            # guest happened to record them; left in the run entry too, so rows behave as before.
            for f in ('gas', 'txs'):
                if value.get(f):
                    b['chain'][f] = value[f]
        self._dirty.add(str(block))

    def builds_by_name(self, name):
        """[(identity, mtime), …] carrying `name`, oldest first.

        By-name lookup exists for one job: reading builds nothing can measure again. `monad-levers` is
        the case — axes retired, ELFs deleted, so there is no file left to hash and its slots keep a
        `legacy:<name>@<mtime>` identity. Anything that CAN be re-measured should go through get(),
        which resolves a build by content."""
        return sorted(((i, max(int(m) for m in e.get('mtimes') or [0]))
                       for i, e in self._index.items() if name in (e.get('names') or [])),
                      key=lambda t: t[1])

    def profiles_for(self, ident):
        """{block: profile} for one build. Never merge two identities into one series: averaging two
        binaries is the silent error this layout exists to prevent."""
        if self._by_ident is None:
            self._by_ident = {}
            try:
                files = sorted(os.listdir(self.blocks_dir))
            except OSError:
                files = []
            for fn in files:
                if not fn.endswith('.json'):
                    continue
                blk = int(fn[:-5])
                for i, slot in self._load(fn[:-5])['builds'].items():
                    p = slot.get(PROFILE)
                    if p:
                        self._by_ident.setdefault(i, {})[blk] = p
        return self._by_ident.get(ident, {})

    def profiles_by_name(self, name):
        """(profiles, block_numbers, mtime) for the NEWEST build carrying `name`."""
        cands = self.builds_by_name(name)
        if not cands:
            return [], [], None
        ident, mt = cands[-1]
        by_blk = self.profiles_for(ident)
        blocks = sorted(by_blk)
        return [by_blk[b] for b in blocks], blocks, mt

    def chain(self, block):
        """Block-level facts (gas, txs), whoever measured them."""
        return self._load(block).get('chain', {})

    # ── persistence ─────────────────────────────────────────────────────────
    def save(self):
        """Flush dirty blocks. Each file is re-read and merged before writing, then replaced
        atomically: two runs on overlapping blocks must not lose each other's slots, and a kill
        mid-write must not leave a truncated file."""
        if self._dirty:
            os.makedirs(self.blocks_dir, exist_ok=True)
        for blk in sorted(self._dirty):
            cur = self._blocks[blk]
            try:
                with open(self._path(blk)) as fh:
                    disk = json.load(fh)
            except Exception:
                disk = {'v': 1, 'block': int(blk), 'chain': {}, 'builds': {}}
            disk.setdefault('chain', {}).update(cur.get('chain', {}))
            for ident, slot in cur.get('builds', {}).items():
                d = disk.setdefault('builds', {}).setdefault(ident, {})
                # A slot cleared in memory (its input changed) must not be resurrected by the merge:
                # drop the disk copy's measurements when the fingerprints disagree.
                if d.get('input') and slot.get('input') and \
                        d['input'].get('id') != slot['input'].get('id'):
                    d.clear()
                d.update(slot)
            disk['v'], disk['block'] = 1, int(blk)
            tmp = self._path(blk) + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(disk, fh)
            os.replace(tmp, self._path(blk))
            self._blocks[blk] = disk
        self._dirty.clear()
        if self._index_dirty:
            os.makedirs(self.root, exist_ok=True)
            try:
                with open(self.index_path) as fh:
                    disk = json.load(fh)
            except Exception:
                disk = {}
            for ident, e in self._index.items():
                d = disk.setdefault(ident, {'names': [], 'backend': None, 'elf': e['elf'],
                                            'mtimes': []})
                d['names'] = sorted(set(d.get('names', [])) | set(e['names']))
                d['mtimes'] = sorted(set(d.get('mtimes', [])) | set(e['mtimes']))
                d['backend'] = d.get('backend') or e.get('backend')
                d['elf'] = e['elf']
            tmp = self.index_path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(disk, fh, indent=1)
            os.replace(tmp, self.index_path)
            self._index, self._index_dirty = disk, False
