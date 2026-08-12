#!/usr/bin/env python3
"""patch-fixed-history.py — expose `--fixed-history-length` on the monad node, so a bounded backfill
can be REWOUND and replayed by a second guest instead of reloading the snapshot.

Why this exists. Generating witnesses for two branches over the same block set costs two snapshot
loads today — ~66 min and ~350 GB each — while the replay itself is ~2 min for 504 blocks. The load is
the whole cost, and it is paid twice only because there is no way back to the start of the range.

There is a way back: `monad-mpt --rewind-to <n>`. It is a metadata operation, not a reload. It is
refused for one reason only (category/mpt/cli_tool_impl.cpp):

    if (*rewind_database_to < aux.metadata_ctx().db_history_min_valid_version())
        "Cannot rewind database to before ..., ignoring request."

So the rewind works iff the DB still holds history back to the first block of the range. It normally
does not: history length is self-regulating, and a manual `monad-mpt --reset-history-length N` is
reverted within seconds of the writer restarting.

The knob to stop that already exists and is already honoured — `OnDiskDbConfig::fixed_history_length`
(category/mpt/ondisk_db_config.hpp), passed to UpdateAux by category/mpt/db.cpp, and setting it clears
`enable_dynamic_history_length_` so nothing shrinks it back. It is simply not reachable from the
command line: `cmd/monad/main.cpp` builds its OnDiskDbConfig without it, and today only the mpt tests
set it.

This patch adds the flag and wires it to that field. Nothing else changes: omit the flag and the node
behaves exactly as before.

    monad --fixed-history-length 600 ... --zkvm-witness dirA     # replay branch A
    monad-mpt --storage ~/triedb.db --rewind-to <FIRST-1>        # seconds, not an hour
    monad --fixed-history-length 600 ... --zkvm-witness dirB     # replay branch B

HALF THE CHAIN, NOT ALL OF IT. This reaches the `monad` binary. The backfill does not invoke it
directly — it goes through `run_replay.py`, which builds monad's command line from its OWN flags
(that is why `--zkvm-witness` had to exist there too). So a second, matching edit is needed in
`run_replay.py` to forward this one, and that file is not in this repo: it lives in the monad tree on
`sam/osaka_witness_gen`. Read it before writing that patch — do not guess a passthrough flag; there
is no evidence one exists.

Until both halves are in place, `witness-backfill` is unchanged and still pays the second load.

SIZE IT ABOVE THE RANGE, and mind the disk. The history has to span the whole replay, so
`--fixed-history-length` must exceed LAST-FIRST; `witness-backfill` passes span+96. Retaining more
history also retains more data: the slow-ring compaction starts at 60% disk usage
(`usage_limit_start_compact_slow`, category/mpt/update_aux.cpp), and once it runs the history it just
freed is gone. A 350 GB snapshot in a 600 G file sits at 0.58 — under, but not by much. Size the DB
so the whole replay stays below 0.6, or the rewind will be refused at exactly the moment you need it.

The patch is applied by exact-string replacement, is idempotent, and refuses to guess: if the expected
code is not found, it says so and changes nothing.

  ./patch-fixed-history.py [--file ~/monad/cmd/monad/main.cpp] [--check] [--revert]
"""
import argparse
import os
import shutil
import sys

MARKER = "fixed_history_length"        # present iff the patch is applied

EDITS = [
    # 1. The variable, declared beside the other db knobs.
    ('''    fs::path zkvm_witness;''',
     '''    fs::path zkvm_witness;
    std::optional<uint64_t> fixed_history_length;'''),

    # 2. The flag. Placed on the same option that already documents the witness dump, so the two
    #    read together: one produces a corpus, the other is what lets you produce a second one
    #    without paying the snapshot load again.
    ('''        "--zkvm-witness,--zkvm_witness",''',
     '''        "--fixed-history-length,--fixed_history_length",
        fixed_history_length,
        "pin the trie history to N versions instead of letting it self-regulate. Needed to "
        "`monad-mpt --rewind-to` the start of a bounded replay and run it again with another "
        "guest: a rewind below db_history_min_valid_version is refused, and the dynamic length "
        "will not keep that far back on its own. Must exceed the replayed span.")
        ->capture_default_str();
    cli.add_option(
        "--zkvm-witness,--zkvm_witness",'''),

    # 3. Wire it into the writable db config. This is the only construction the replay path uses;
    #    the two ReadOnlyOnDiskDbConfig sites are transient helpers (block-hash buffer, snapshot
    #    dump) and hold no history policy.
    #    AFTER .dbname_paths, not before: C++20 requires designated initialisers in
    #    declaration order, and OnDiskDbConfig declares fixed_history_length below
    #    dbname_paths. The other order is a compile error, not a style question.
    ('''                .sq_thread_cpu = disable_sq_thread_cpu
                                     ? std::optional<unsigned>{}
                                     : std::optional<unsigned>{sq_thread_cpu},
                .dbname_paths = dbname_paths}};''',
     '''                .sq_thread_cpu = disable_sq_thread_cpu
                                     ? std::optional<unsigned>{}
                                     : std::optional<unsigned>{sq_thread_cpu},
                .dbname_paths = dbname_paths,
                .fixed_history_length = fixed_history_length}};'''),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=os.path.expanduser("~/monad/cmd/monad/main.cpp"))
    ap.add_argument("--check", action="store_true", help="report whether it is applied; change nothing")
    ap.add_argument("--revert", action="store_true", help="undo the patch")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        sys.exit(f"patch-fixed-history: no such file: {a.file} (pass --file)")
    src = open(a.file).read()
    patched = MARKER in src

    if a.check and patched:
        print("patch-fixed-history: applied")
        return
    if a.revert and not patched:
        print("patch-fixed-history: not applied — nothing to revert")
        return
    if not a.revert and patched:
        print("patch-fixed-history: already applied — nothing to do")
        return

        # Reversed: undoing in forward order is only correct when no edit extends what an earlier
        # one inserted. It silently is not, otherwise -- every `new` string is still found in the
        # ORIGINAL text, so the loop reports success and leaves part of the patch behind, giving a
        # file that is neither patched nor pristine. Reverse order costs nothing when the edits are
        # independent, so it is simply the correct default.
    pairs = [(n, o) for o, n in reversed(EDITS)] if a.revert else list(EDITS)
    probe, missing = src, []
    for i, (o, n) in enumerate(pairs, 1):
        if o in probe:
            probe = probe.replace(o, n, 1)
        else:
            missing.append(i)
    if missing:
        print(f"patch-fixed-history: cannot {'revert' if a.revert else 'apply'} — edit(s) "
              f"{', '.join(map(str, missing))} of {len(pairs)} do not match this file. The upstream "
              f"tree has changed; re-read it and update EDITS rather than forcing anything.",
              file=sys.stderr)
        sys.exit(1)
    if a.check:
        print(f"patch-fixed-history: NOT applied ({len(pairs)} edit(s) would apply cleanly)")
        return

    out = src
    for old, new in pairs:
        out = out.replace(old, new, 1)
    backup = a.file + ".orig"
    if not os.path.exists(backup):
        shutil.copyfile(a.file, backup)
        print(f"patch-fixed-history: kept the original at {backup}")
    with open(a.file, "w") as f:
        f.write(out)
    print(f"patch-fixed-history: {'reverted' if a.revert else 'applied'} {len(pairs)} edit(s) in {a.file}")
    if not a.revert:
        print("Rebuild the node, then replay with --fixed-history-length <span+margin>.")


if __name__ == "__main__":
    main()
