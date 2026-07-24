#!/usr/bin/env python3
"""
results — aggregate the deterministic EXECUTION work-units across guests into a
self-contained HTML report (profiling/results/results.html).

Sibling of hotspots.py: where hotspots profiles *where* a guest's cost goes, this
reports *how much* work each guest does per block, side by side. Reads each reth
block guest's `../guests/<name>/inputs/*.exec-report.json`.

Work-units — SP1 `cycles`, ZisK `steps`, OpenVM `instructions` — are DETERMINISTIC
(a function of block × ELF commit, machine-independent) but **NOT comparable across
zkVMs** (different VMs). The report says so and never mixes units on one scale.
Host-dependent `elapsed_secs` is omitted; proving times are NOT aggregated here
(run/box/tuning-dependent — see ../cli/report-schema.md).

    profiling/results.py            # -> profiling/results/results.html
    profiling/results.py --out foo.html
"""
import json, glob, os, sys, html

HERE = os.path.dirname(os.path.abspath(__file__))   # profiling/
REPO = os.path.dirname(HERE)                         # repo root

# reth block-guests to compare, in fixed column order (guest dir, unit label, short label, palette slot)
STACKS = [
    ("rsp",         "cycles",       "SP1 · RSP",   1),
    ("zisk-reth",   "steps",        "ZisK",        2),
    ("openvm-reth", "instructions", "OpenVM",      3),
]

def scan(guest):
    """(block -> work-unit value, set of ELF commits seen) from that guest's exec-report.json files."""
    out, commits = {}, set()
    for f in sorted(glob.glob(os.path.join(REPO, f"guests/{guest}/inputs/*.exec-report.json"))):
        tag = os.path.basename(f)[:-len(".exec-report.json")]
        blk = tag.split("-")[-1]
        if not blk.isdigit():
            continue
        j = json.load(open(f))
        val = j.get("cycles", j.get("steps"))   # SP1/OpenVM: cycles · ZisK: steps
        if val:                                  # truthy: skips None AND a 0 from an SP1 --no-gas run
            out[blk] = int(val)
            commits.add(j.get("commit"))          # None for legacy reports without the field
    return out, commits

def main():
    out_path = os.path.join(HERE, "results", "results.html")
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    scanned = {name: scan(name) for name, *_ in STACKS}
    data = {name: v[0] for name, v in scanned.items()}
    commits = {name: v[1] for name, v in scanned.items()}
    sets = [set(data[name]) for name, *_ in STACKS if data[name]]
    common = sorted(set.intersection(*sets), key=int) if len(sets) == len(STACKS) else []
    union = sorted(set().union(*sets), key=int) if sets else []

    if not union:
        print("results: no exec-report.json under guests/*/inputs/ — run `./run execute` per block "
              "first (they are git-ignored / regenerated locally).", file=sys.stderr)

    # ELF commit each column was built from; warn if a guest mixed commits (work-units are only
    # comparable within one ELF version — see cli/report-schema.md).
    commit_note = {}
    for name, *_ in STACKS:
        cs = {c for c in commits[name] if c}
        if len(cs) > 1:
            print(f"WARNING: {name} exec-reports span MULTIPLE ELF commits {sorted(c[:12] for c in cs)} — "
                  f"not comparable; regenerate them from a single commit.", file=sys.stderr)
        commit_note[name] = (sorted(cs)[0][:12] if len(cs) == 1 else ("mixed" if cs else "n/a"))

    SLOT = {1: ("#2a78d6", "#3987e5"), 2: ("#1baf7a", "#199e70"),
            3: ("#eda100", "#c98500"), 5: ("#4a3aa7", "#9085e9")}
    css_light = ["--plane:#f9f9f7", "--surface:#fcfcfb", "--ink:#0b0b0b", "--ink2:#52514e",
                 "--muted:#898781", "--grid:#e1e0d9", "--ring:rgba(11,11,11,0.10)",
                 "--common:rgba(42,120,214,0.07)"]
    css_dark  = ["--plane:#0d0d0d", "--surface:#1a1a19", "--ink:#ffffff", "--ink2:#c3c2b7",
                 "--muted:#898781", "--grid:#2c2c2a", "--ring:rgba(255,255,255,0.10)",
                 "--common:rgba(57,135,229,0.13)"]
    for name, unit, label, slot in STACKS:
        css_light.append(f"--s{slot}:{SLOT[slot][0]}")
        css_dark.append(f"--s{slot}:{SLOT[slot][1]}")

    def fmt(v):
        return f"{v:,}".replace(",", " ") if v is not None else "—"   # thin-space grouping

    tiles = "".join(
        f'<div class="tile"><div class="tile-dot" style="background:var(--s{slot})"></div>'
        f'<div class="tile-v">{len(data[name])}</div>'
        f'<div class="tile-l">{html.escape(label)}<span class="u">{unit}</span></div></div>'
        for name, unit, label, slot in STACKS
    )
    tiles += (f'<div class="tile accent"><div class="tile-v">{len(common)}</div>'
              f'<div class="tile-l">blocks common to all three<span class="u">apples-to-apples set</span></div></div>')

    heads = '<th class="blk">Block</th>' + "".join(
        f'<th><span class="dot" style="background:var(--s{slot})"></span>{html.escape(label)}'
        f'<span class="u">{unit}</span></th>' for name, unit, label, slot in STACKS)
    rows = []
    for blk in union:
        is_common = blk in common
        cells = f'<td class="blk">{blk}{" ★" if is_common else ""}</td>'
        for name, *_ in STACKS:
            v = data[name].get(blk)
            cells += f'<td class="{"num" if v is not None else "num none"}">{fmt(v)}</td>'
        rows.append(f'<tr class="{"common" if is_common else ""}">{cells}</tr>')
    tbody = "\n".join(rows)
    commit_footer = " · ".join(f"{html.escape(lbl)} <code>{html.escape(commit_note[name])}</code>"
                               for name, unit, lbl, slot in STACKS)

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>zkVM execution benchmark — results</title>
<style>
  :root {{ {"; ".join(css_light)}; color-scheme: light dark; }}
  @media (prefers-color-scheme: dark) {{ :root {{ {"; ".join(css_dark)} }} }}
  :root[data-theme="dark"]  {{ {"; ".join(css_dark)} }}
  :root[data-theme="light"] {{ {"; ".join(css_light)} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--plane); color:var(--ink);
    font-family: system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.5;
    -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width: 60rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
  h1 {{ font-size: 1.6rem; font-weight: 650; letter-spacing:-0.01em; margin:0 0 .25rem; }}
  .sub {{ color:var(--ink2); margin:0 0 1.5rem; max-width:46rem; }}
  .note {{ border:1px solid var(--ring); border-left:3px solid var(--s3);
    background:var(--surface); border-radius:10px; padding:.85rem 1rem; margin:0 0 1.75rem;
    color:var(--ink2); font-size:.9rem; }}
  .note b {{ color:var(--ink); font-weight:600; }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
    gap:.75rem; margin:0 0 2rem; }}
  .tile {{ background:var(--surface); border:1px solid var(--ring); border-radius:12px;
    padding:1rem 1.1rem; position:relative; }}
  .tile.accent {{ border-color:var(--s1); }}
  .tile-dot {{ width:9px; height:9px; border-radius:50%; position:absolute; top:1rem; right:1rem; }}
  .tile-v {{ font-size:2rem; font-weight:660; letter-spacing:-0.02em; }}
  .tile-l {{ color:var(--ink2); font-size:.82rem; margin-top:.15rem; }}
  .tile-l .u {{ display:block; color:var(--muted); font-size:.72rem; }}
  .scroll {{ overflow-x:auto; border:1px solid var(--ring); border-radius:12px; background:var(--surface); }}
  table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
  th, td {{ padding:.6rem .9rem; text-align:right; white-space:nowrap; border-bottom:1px solid var(--grid); }}
  thead th {{ position:sticky; top:0; background:var(--surface); color:var(--ink2);
    font-weight:600; font-size:.8rem; vertical-align:bottom; z-index:1; }}
  th.blk, td.blk {{ text-align:left; }}
  th .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:.4rem; }}
  th .u {{ display:block; color:var(--muted); font-weight:400; font-size:.72rem; }}
  td.num {{ font-variant-numeric: tabular-nums; }}
  td.none {{ color:var(--muted); }}
  td.blk {{ font-variant-numeric: tabular-nums; color:var(--ink2); }}
  tr.common {{ background:var(--common); }}
  tr.common td.blk {{ color:var(--ink); font-weight:600; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  tbody tr:hover {{ background:var(--common); }}
  footer {{ color:var(--muted); font-size:.8rem; margin-top:1.5rem; }}
  footer code {{ color:var(--ink2); }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>zkVM execution benchmark</h1>
    <p class="sub">Deterministic execution work-units per mainnet block, across the reth
    block guests. Generated from each stack's <code>exec-report.json</code>.</p>

    <div class="note">
      <b>Read this first.</b> Work-units are <b>deterministic</b> (a function of block × ELF
      commit — identical on any machine), so these numbers are reproducible. But they are
      <b>not comparable across zkVMs</b>: SP1 <i>cycles</i> ≠ ZisK <i>steps</i> ≠ OpenVM
      <i>instructions</i> — different virtual machines. Compare within a column, or as a ratio
      against the same client on the same zkVM. Proving times are <b>not</b> shown (run-, box-
      and tuning-dependent — see <code>../cli/report-schema.md</code>).
    </div>

    <div class="tiles">{tiles}</div>

    <div class="scroll">
      <table>
        <thead><tr>{heads}</tr></thead>
        <tbody>
{tbody}
        </tbody>
      </table>
    </div>

    <footer>★ = block present in all three stacks ({len(common)} of {len(union)}).
    Values are the deterministic work-unit; “—” = not generated for that stack.
    ELF commit per guest: {commit_footer}.
    Regenerate with <code>profiling/results.py</code>.</footer>
  </div>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    open(out_path, "w").write(doc)
    print(f"wrote {out_path}")
    print("  stacks: " + " · ".join(f"{lbl} {len(data[n])}" for n, u, lbl, s in STACKS))
    print("  commit: " + " · ".join(f"{lbl} {commit_note[n]}" for n, u, lbl, s in STACKS))
    print(f"  {len(common)} common blocks / {len(union)} union")

if __name__ == "__main__":
    main()
