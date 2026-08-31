#!/usr/bin/env python3
"""Select a deterministic, nested sample spread independently of block order."""
import argparse
import hashlib
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", required=True, help="file containing one witness path per line")
    ap.add_argument("--count", required=True, type=int)
    args = ap.parse_args()

    with open(args.all) as fh:
        paths = [line.strip() for line in fh if line.strip()]
    if not 0 < args.count <= len(paths):
        ap.error(f"count must be between 1 and {len(paths)}")
    names = [os.path.basename(path) for path in paths]
    if len(names) != len(set(names)):
        ap.error("duplicate witness basenames")

    # The domain prefix freezes the permutation independently of any other use
    # of SHA-256. Taking a prefix makes every larger sample a strict superset.
    def rank(path):
        name = os.path.basename(path).encode()
        return hashlib.sha256(b"zkvm-bench-series-v1\0" + name).digest()

    selected = sorted(sorted(paths, key=rank)[:args.count], key=os.path.basename)
    print("\n".join(selected))


if __name__ == "__main__":
    main()
