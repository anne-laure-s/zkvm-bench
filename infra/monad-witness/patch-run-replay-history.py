#!/usr/bin/env python3
"""patch-run-replay-history.py — forward `--fixed-history-length` from run_replay.py to the node.

The other half of `./patch-fixed-history.py`. That one gives the `monad` binary the flag; this one
lets the orchestrator pass it, because `run_replay.py` builds monad's command line from its own
explicit options and has **no passthrough**:

    cmd = [args.monad_bin, "--chain", ..., "--nthreads", str(args.nthreads)]
    if args.zkvm_witness is not None:
        cmd += ["--zkvm-witness", args.zkvm_witness]

So a flag that is not named here never reaches the node — which is exactly why `--zkvm-witness`
already had to exist on both sides. This adds `--fixed-history-length` in the same shape, and only
appends it when set, so an unpatched node is unaffected by an unpatched invocation.

WHY IT IS WORTH TWO PATCHES. Pinning the history is what makes `monad-mpt --rewind-to` succeed at the
start of a bounded range, which turns "replay a second branch over the same blocks" from a ~66 min
snapshot reload into a metadata operation plus ~2 min of replay.

READ THIS BEFORE TRUSTING A REWIND. `--rewind-to` does not fail when it cannot do the job: it prints

    WARNING: Cannot rewind database to before <n>, ignoring request.

and **carries on with a zero exit status**. A caller that checks only the exit code will believe the
DB was rewound when it was not, and the replay that follows will find nothing to execute and idle.
Verify the OUTCOME — `monad-cli --db <db>` must report `latest_finalized_block_id` back at the start
of the range — never the return code. `witness-backfill again` does exactly that, and falls back to
a full reset+load, loudly, when the rewind was ignored.

The patch is applied by exact-string replacement, is idempotent, and refuses to guess: if the
expected code is not found, it says so and changes nothing.

  ./patch-run-replay-history.py [--file ~/monad/run_replay.py] [--check] [--revert]
"""
import argparse
import os
import shutil
import sys

MARKER = "fixed_history_length"        # present iff the patch is applied

EDITS = [
    # 1. The option, declared next to --zkvm-witness because they are used together: one produces a
    #    corpus, the other is what lets a second corpus be produced without reloading the snapshot.
    ('''    ap.add_argument("--max-batch", type=int, default=50_000)''',
     '''    ap.add_argument(
        "--fixed-history-length", type=int, default=None,
        help="pin the trie history to N versions (passed through to monad). Needed for "
        "`monad-mpt --rewind-to` to reach the start of a bounded replay afterwards; must exceed "
        "the replayed span. Requires a node built with the matching patch.",
    )
    ap.add_argument("--max-batch", type=int, default=50_000)'''),

    # 2. Forward it. Appended only when set, so the default invocation is byte-identical to before
    #    and an unpatched node never sees an unknown flag.
    ('''        if args.zkvm_witness is not None:
            cmd += ["--zkvm-witness", args.zkvm_witness]''',
     '''        if args.zkvm_witness is not None:
            cmd += ["--zkvm-witness", args.zkvm_witness]
        if args.fixed_history_length is not None:
            cmd += ["--fixed-history-length", str(args.fixed_history_length)]'''),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=os.path.expanduser("~/monad/run_replay.py"))
    ap.add_argument("--check", action="store_true", help="report whether it is applied; change nothing")
    ap.add_argument("--revert", action="store_true", help="undo the patch")
    a = ap.parse_args()

    if not os.path.exists(a.file):
        sys.exit(f"patch-run-replay-history: no such file: {a.file} (pass --file)")
    src = open(a.file).read()
    patched = MARKER in src

    if a.check and patched:
        print("patch-run-replay-history: applied")
        return
    if a.revert and not patched:
        print("patch-run-replay-history: not applied — nothing to revert")
        return
    if not a.revert and patched:
        print("patch-run-replay-history: already applied — nothing to do")
        return

    pairs = [(n, o) for o, n in EDITS] if a.revert else list(EDITS)
    probe, missing = src, []
    for i, (o, n) in enumerate(pairs, 1):
        if o in probe:
            probe = probe.replace(o, n, 1)
        else:
            missing.append(i)
    if missing:
        print(f"patch-run-replay-history: cannot {'revert' if a.revert else 'apply'} — edit(s) "
              f"{', '.join(map(str, missing))} of {len(pairs)} do not match this file. The upstream "
              f"script has changed; re-read it and update EDITS rather than forcing anything.",
              file=sys.stderr)
        sys.exit(1)
    if a.check:
        print(f"patch-run-replay-history: NOT applied ({len(pairs)} edit(s) would apply cleanly)")
        return

    out = src
    for old, new in pairs:
        out = out.replace(old, new, 1)
    backup = a.file + ".orig"
    if not os.path.exists(backup):
        shutil.copyfile(a.file, backup)
        print(f"patch-run-replay-history: kept the original at {backup}")
    with open(a.file, "w") as f:
        f.write(out)
    print(f"patch-run-replay-history: {'reverted' if a.revert else 'applied'} {len(pairs)} "
          f"edit(s) in {a.file}")
    if not a.revert:
        print("Patch the node too (./patch-fixed-history.py) — the flag is inert without it.")


if __name__ == "__main__":
    main()
