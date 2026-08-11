#!/usr/bin/env python3
"""patch-run-replay.py — stop the monad tree's run_replay.py from dying on an RPC blip.

Why this exists. `run_replay.py` contains **no try/except at all**, and every iteration of its main
loop calls `rpc_block_number(rpc, "finalized")` — `requests.post(..., timeout=30)` followed by
`raise_for_status()`. A slow RPC, a 5xx, or a malformed `result` therefore raises, and the
orchestrator exits with a traceback. It exits WITHOUT writing `logs/ALERT`, because ALERT is only
written when the `monad` child returns non-zero. The symptom is a replay that "stopped on its own
while everything was fine", with nothing to explain it.

The RPC result is worth even less than that failure mode suggests. It feeds only `gap`, and gap's
one decision use is

    if n_ready < max(1, min(args.min_follow_batch, gap)):

so with `--min-follow-batch 1` the threshold is 1 whatever gap is. In a bounded backfill the call
cannot change any behaviour, and can only end the run.

What this changes: the lookup becomes advisory. On failure it logs a warning, sets `gap = None`, and
the loop carries on. `gap = None` means "unknown", and the threshold then falls back to
`max(1, min_follow_batch)` — the original behaviour minus the near-finality shortcut, which is
exactly what an unknown distance to finality should give you. `status.json` records `null` rather
than a stale number, so a reader can tell "unknown" from "zero".

Deliberately NOT changed: the `timeout=300` around `monad-cli --db` in `last_executed()`. That one
is a local subprocess, and a five-minute hang there is a real fault worth stopping for, not a blip.

The patch is applied by exact-string replacement, is idempotent, and refuses to guess: if the
expected code is not found (the upstream script moved), it says so and changes nothing.

  ./patch-run-replay.py [--file ~/monad/run_replay.py] [--check] [--revert]
  ./patch-run-replay.py --host <box>              # apply on a remote box over ssh
"""
import argparse, os, shutil, subprocess, sys, tempfile

MARKER = "gap is None"   # present iff the patch is applied

EDITS = [
    # 1. The lookup itself becomes advisory.
    ('''        last = last_executed(args.monad_cli_bin, args.db)
        avail = contiguous_head(args.block_db, last + 1)
        head = rpc_block_number(args.rpc, "finalized")
        gap = head - last''',
     '''        last = last_executed(args.monad_cli_bin, args.db)
        avail = contiguous_head(args.block_db, last + 1)
        # The finalized lookup is ADVISORY: it feeds `gap`, which is a display quantity and a
        # near-finality shortcut in the batch threshold below. Letting a 30 s timeout or a 5xx
        # from the RPC kill an otherwise healthy replay — with no ALERT, since ALERT is only
        # written when the monad child fails — costs a whole run to save nothing.
        try:
            head = rpc_block_number(args.rpc, "finalized")
            gap = head - last
        except Exception as exc:
            head, gap = None, None
            print(f"warning: finalized lookup failed ({exc!r}) — continuing, gap unknown",
                  flush=True)'''),

    # 2. The threshold has to mean something when gap is unknown.
    ('''        n_ready = avail - last
        if n_ready < max(1, min(args.min_follow_batch, gap)):''',
     '''        n_ready = avail - last
        # gap is None == "distance to finality unknown". Fall back to min_follow_batch alone: the
        # original behaviour minus the near-finality shortcut, which is what not knowing the
        # distance should give you. (With --min-follow-batch 1 the threshold is 1 either way.)
        need = max(1, args.min_follow_batch if gap is None else min(args.min_follow_batch, gap))
        if n_ready < need:'''),

    # 3. The log line should not print a bare None.
    ('''            f"executing blocks {last + 1}..{last + nblocks} "
            f"(gap to finalized: {gap}) -> {log_path}",''',
     '''            f"executing blocks {last + 1}..{last + nblocks} "
            f"(gap to finalized: {'unknown' if gap is None else gap}) -> {log_path}",'''),
]


def patch_text(src, revert, tag):
    if revert:
        pairs = [(new, old) for old, new in EDITS if new in src]
        if not pairs:
            return None, f"{tag}: already reverted"
        missing = [i for i, (o, _) in enumerate(pairs, 1) if o not in src]
    else:
        pairs = [(old, new) for old, new in EDITS if new not in src]
        if not pairs:
            return None, f"{tag}: already applied"
        probe, missing = src, []
        for i, (o, n) in enumerate(pairs, 1):
            if o in probe:
                probe = probe.replace(o, n, 1)
            else:
                missing.append(i)
    if missing:
        raise SystemExit(
            f"{tag}: cannot {'revert' if revert else 'apply'} — edit(s) "
            f"{', '.join(map(str, missing))} of {len(pairs)} do not match this file. The upstream "
            f"script has changed; re-read it and update EDITS rather than forcing anything.")
    out = src
    for old, new in pairs:
        out = out.replace(old, new, 1)
    return out, f"{len(pairs)} edit(s)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Left unexpanded: with --host it must resolve on the REMOTE box, not here.
    ap.add_argument("--file", default="~/monad/run_replay.py")
    ap.add_argument("--host", help="apply on a remote box over ssh instead of locally")
    ap.add_argument("--check", action="store_true", help="report, change nothing")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    tag = "patch-run-replay"

    if a.host:
        # Read, patch here, write back — so the matching logic lives in one place and the remote
        # box needs nothing installed.
        src = subprocess.run(["ssh", "-n", a.host, f"cat {a.file}"],
                             capture_output=True, text=True, check=True).stdout
        out, msg = patch_text(src, a.revert, tag)
        if out is None: print(f"{tag}: {msg} ({a.host}:{a.file})"); return
        if a.check: print(f"{tag}: NOT applied — {msg} would apply cleanly ({a.host}:{a.file})"); return
        subprocess.run(["ssh", "-n", a.host,
                        f"test -f {a.file}.orig || cp {a.file} {a.file}.orig"], check=True)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(out); tmp = tf.name
        subprocess.run(["scp", "-q", tmp, f"{a.host}:{a.file}"], check=True)
        os.unlink(tmp)
        # Parse it where it now lives: a patch that writes syntactically broken Python would
        # otherwise only surface at the next replay start, hours into a run.
        subprocess.run(["ssh", "-n", a.host,
                        f"python3 - <<'PYEOF'\nimport ast,os\nast.parse(open(os.path.expanduser('{a.file}')).read())\nprint('{tag}: remote file parses')\nPYEOF"],
                       check=True)
        print(f"{tag}: {'reverted' if a.revert else 'applied'} {msg}; original kept at {a.host}:{a.file}.orig")
        return

    a.file = os.path.expanduser(a.file)
    with open(a.file) as f:
        src = f.read()
    out, msg = patch_text(src, a.revert, tag)
    if out is None: print(f"{tag}: {msg} ({a.file})"); return
    if a.check: print(f"{tag}: NOT applied — {msg} would apply cleanly ({a.file})"); return
    backup = a.file + ".orig"
    if not os.path.exists(backup):
        shutil.copyfile(a.file, backup)
        print(f"{tag}: kept the original at {backup}")
    with open(a.file, "w") as f:
        f.write(out)


if __name__ == "__main__":
    main()
