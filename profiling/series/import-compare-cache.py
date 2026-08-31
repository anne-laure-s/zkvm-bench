#!/usr/bin/env python3
"""Append compatible compare-cache RUN entries to a series TSV."""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILING = os.path.dirname(HERE)
sys.path.insert(0, PROFILING)
from cache import Cache, RUN  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--blocks-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--elf-dir", default=os.path.join(HERE, "elf"))
    ap.add_argument("--publish", action="store_true",
                    help="publish missing series rows without replacing richer compare entries")
    args = ap.parse_args()

    shas = []
    with open(args.index) as fh:
        for line in fh:
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 4 and fields[2] == "OK" and fields[3] not in shas:
                shas.append(fields[3])
    blocks = []
    with open(args.blocks_file) as fh:
        for line in fh:
            witness = line.strip()
            if witness:
                blocks.append((os.path.basename(witness).removesuffix(".witness"), witness))

    existing = set()
    try:
        with open(args.out) as fh:
            for line in fh:
                fields = line.split("\t", 2)
                if len(fields) >= 2:
                    existing.add((fields[0], fields[1]))
    except FileNotFoundError:
        pass

    cache = Cache()
    series_rows = {}
    try:
        with open(args.out) as fh:
            for line in fh:
                fields = line.rstrip("\n").split("\t")
                if len(fields) == 4:
                    try:
                        series_rows[(fields[0], fields[1])] = (int(fields[2]), int(fields[3]))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass

    if args.publish:
        published = 0
        for sha in shas:
            elf = os.path.join(args.elf_dir, f"{sha}.elf")
            if not os.path.isfile(elf):
                continue
            for block, witness in blocks:
                values = series_rows.get((sha, block))
                if values is None or cache.get(elf, int(block), RUN, inp=witness) is not None:
                    continue
                work, cost = values
                cache.put(elf, int(block), RUN,
                          {"work": work, "cost": cost, "insz": os.path.getsize(witness)},
                          backend="zisk", inp=witness)
                published += 1
        cache.save()
        print(f"compare cache: {published} new series row(s) published")
        return

    imported = []
    for sha in shas:
        elf = os.path.join(args.elf_dir, f"{sha}.elf")
        if not os.path.isfile(elf):
            continue
        for block, witness in blocks:
            if (sha, block) in existing:
                continue
            hit = cache.get(elf, int(block), RUN, inp=witness)
            if not isinstance(hit, dict) or "error" in hit:
                continue
            work, cost = hit.get("work"), hit.get("cost")
            if not isinstance(work, int) or not isinstance(cost, int):
                continue
            imported.append(f"{sha}\t{block}\t{work}\t{cost}\n")
            existing.add((sha, block))

    if imported:
        with open(args.out, "a") as fh:
            fh.writelines(imported)
    print(f"compare cache: {len(imported)} compatible series row(s) imported")


if __name__ == "__main__":
    main()
