#!/bin/bash
# after-builds.sh — wait for the SP1 build run, then measure the new ELFs on the
# 10-block sample and re-render. Chained rather than run in parallel: the builds
# drive git checkouts and the measurement is heavy, and stacking the two is what
# once exhausted the machine.
set -uo pipefail
S="$(cd "$(dirname "$0")" && pwd)"
# Wait on the LOCK, not on `pgrep -f series-build-sp1`: that pattern also matches the shell that
# started this script (`series-build-sp1.sh && after-builds.sh` is exactly how it gets run), so
# the loop watched itself and never exited.
. "$S/tree-lock.sh"
MONAD="${MONAD:-$(series_monad_default)}"
while series_tree_busy "$MONAD"; do sleep 30; done
OUTD="${OUTD:-$(series_repo)/profiling/series-sp1}"
# A real tab, via $'...': BSD grep does not read \t in a basic regex. And anchored on the STATUS
# column — a bare `grep -c OK` also counted every commit whose subject happens to contain "OK".
echo "builds done: $(grep -c $'\tOK\t' "$OUTD/index.tsv" 2>/dev/null || echo 0) entries"
"$S/series-measure-sp1.sh" 10 4
"$S/report.py"
