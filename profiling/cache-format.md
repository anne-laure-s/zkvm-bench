# Profile cache format

One file per block, git-ignored, regenerable. Written and read by `compare.py`, `levers.py` and
`inline-robust.py`, all through [`cache.py`](cache.py).

```
profiling/cache/
├── blocks/<block>.json     one per block
└── builds.json             index: identity -> names, backend, elf path, mtimes
```

## Block file

```json
{
  "v": 1,
  "block": 25551991,
  "chain":  { "gas": 24247944, "txs": 258 },
  "builds": {
    "sha256:f5cc8205c002fcfc": {
      "input":   { "id": "sha256:9c1f4e77a0b32d15", "sz": 10090560, "mt": 1752754560 },
      "run":     { "work": 275184844, "secs": 41.2, "cost": "…", "cats": "…" },
      "profile": { "fns": [["sym", 37277132]], "total": 275184844, "trunc": 120 },
      "profile_full": { "run": { "functions": "…", "modules": "…", "tree": "…" },
                        "trunc": 200, "rate": 200 }
    }
  }
}
```

| field | |
|---|---|
| `chain` | properties of the block itself, guest-independent. Written by whichever guest measures them (`gas` and `txs` come from the reth ZisK guest). Read them from here, not from a build. |
| `builds` | keyed by **identity** (below), never by name. Nothing else about the build is stored here — join on `builds.json` |
| `input` | fingerprint of the file this slot was measured on. A measurement is a function of (build, **input**), so a slot whose input no longer matches is a miss, not a hit. `sz`/`mt` are a fast path only — on a mismatch the hash decides, because a `cp` moves mtime without moving a byte |
| `run` | one execution's work-units and derived counters |
| `profile` | the symbol profile of that same execution |
| `trunc` | how many symbols the list keeps. **A consumer needing more must re-measure**, not extrapolate; a consumer needing fewer truncates, which is exact |
| `profile_full` | the complete profiler output `hotspots.py` produces — functions at `--top`, plus modules, categories, opcodes and the call tree. `rate` is SP1's sample rate, part of what makes two profiles comparable |

`profile` is the projection of the same execution `compare.py` reads. `hotspots.py` writes **both**, so
a block it profiles is a cache hit for `compare.py` and never re-executed.

`run` and `profile` are two views of one execution and share a slot — so when the input changes, both
go: the slot is dropped, never half-refreshed.

A slot with no `input` was migrated from the old cache, which did not record one. It is served as-is;
rejecting every such entry would mean re-measuring everything to learn what is almost always still
valid.

## builds.json

Identity → what it is. One entry per build, not per (build, block): a build appears in hundreds of
block files, so this is the only place its metadata is written and the only place it can be corrected.

```json
{ "sha256:cb97b1f29f5c0a8e": { "names":   ["rsp"],
                               "backend": "sp1",
                               "elf":     "guests/rsp/rsp.elf",
                               "mtimes":  ["1782062805"] } }
```

`names` are labels for display, free to change — renaming invalidates nothing. `mtimes` lists the ELF
mtimes this build has been seen under: traceability back to the old keys, never an identity.

## Identity

`sha256:<first 16 hex>` of the ELF — computed only when the file at `elf` still carries the entry's
mtime, so the hash provably describes the binary that was measured.

`legacy:<name>@<mtime>` otherwise: the ELF was overwritten (rebuilt in place, or replaced by
`guests/monad/use-gen` on a generation switch) or deleted, and its bytes are unrecoverable. Legacy
entries are read and written normally; they simply cannot be matched across renames.

Never key on a name or an mtime alone: ELFs built in the same second share an mtime, and a `cp`
changes an mtime without changing the binary.

## Not stored here

**Family counts.** `profile.fns` is the cached form; families are folded from it at read time by
`hotspots.family()`, which is memoised. Never cache a family sum: the taxonomy is a module constant
that gets edited, and a cached sum then keeps the old grouping without saying so.

Two earlier cache generations did cache the sums — `fam`, then `fam1` with a taxonomy-hash key
component to invalidate them. Both were dropped rather than carried forward.
