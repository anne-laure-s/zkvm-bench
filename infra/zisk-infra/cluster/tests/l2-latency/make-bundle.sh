#!/usr/bin/env bash
# make-bundle.sh — the one file to ship to the prover box. RUNS ON THE MAC, after prepare-inputs.py.
#
#   bash make-bundle.sh [out.tar.gz]          # default ~/l2-latency-bundle.tar.gz
#   ZISK_PUBLICS_BIN=<binary> bash make-bundle.sh
#
# Laid out as ~/zisk-infra/ on the box, which is where run.sh expects its siblings:
#   cluster/            install, up, start, stop, the memlock shim, tests/lib.sh and t3-topo.sh
#                       (up.sh's bandwidth gate on a fresh box), and this directory with its inputs
#   zisk-publics/       the source, and the binary given in ZISK_PUBLICS_BIN as bin/zisk-publics
#                       (built for glibc 2.35, so it runs on Ubuntu 22.04 and later); without one,
#                       run.sh builds it on the box once the timing is over
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="$(cd "$HERE/../.." && pwd)"
ZI="$(cd "$CLUSTER/.." && pwd)"
OUT="${1:-$HOME/l2-latency-bundle.tar.gz}"
[ -f "$HERE/inputs/inputs.csv" ] || { echo "ERROR: no inputs — run prepare-inputs.py first" >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"

STAGE="$(mktemp -d)/zisk-infra"
mkdir -p "$STAGE/cluster/tests/l2-latency" "$STAGE/zisk-publics"
for f in 00-install-once.sh up.sh start.sh stop.sh watch.sh nolock.c mlocktest.c \
         fix-memlock-patch.sh README.md; do
  cp "$CLUSTER/$f" "$STAGE/cluster/"
done
cp "$CLUSTER/tests/lib.sh" "$CLUSTER/tests/t3-topo.sh" "$STAGE/cluster/tests/"
cp -R "$HERE/README.md" "$HERE/RUNBOOK.md" "$HERE/run.sh" "$HERE/bench-l2.sh" "$HERE/check.py" "$HERE/summarize.py" \
      "$HERE/poc-install.sh" "$HERE/inputs" "$STAGE/cluster/tests/l2-latency/"
cp -R "$ZI/zisk-publics/Cargo.toml" "$ZI/zisk-publics/Cargo.lock" "$ZI/zisk-publics/src" \
      "$STAGE/zisk-publics/"
if [ -n "${ZISK_PUBLICS_BIN:-}" ]; then
  mkdir -p "$STAGE/zisk-publics/bin"
  cp "$ZISK_PUBLICS_BIN" "$STAGE/zisk-publics/bin/zisk-publics"
  chmod +x "$STAGE/zisk-publics/bin/zisk-publics"
fi

# No macOS extended attributes in the archive: GNU tar on the box warns on every one of them.
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "$OUT" -C "$(dirname "$STAGE")" zisk-infra 2>/dev/null \
  || COPYFILE_DISABLE=1 tar -czf "$OUT" -C "$(dirname "$STAGE")" zisk-infra
rm -rf "$(dirname "$STAGE")"
N="$(($(wc -l < "$HERE/inputs/inputs.csv") - 1))"
echo "bundle: $OUT ($(du -h "$OUT" | cut -f1), $N inputs)"
echo
echo "On the prover box (root, NVIDIA driver present, 80 GB of disk for the proving key):"
echo "  scp -P <port> $OUT root@<host>:~/"
echo "  ssh -p <port> root@<host> 'tar xzf $(basename "$OUT") && bash zisk-infra/cluster/tests/l2-latency/run.sh'"
