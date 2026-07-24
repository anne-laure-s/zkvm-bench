#!/usr/bin/env python3
"""
hotspots — guest hotspot profiler + interactive HTML report for zkVM guests.

Prover-agnostic by design: a backend collects a profile into a common JSON schema,
and one shared renderer (`template.html`) turns any backend's JSON into the same
interactive report (hotspot icicle + cost/opcode breakdowns).

    # profile inputs through a guest ELF  ->  <out>/profile.json + <out>/index.html
    hotspots.py profile --backend zisk --elf guest.elf -i a.bin -i b.bin --out out/ \
        --title "My guest" [--verify-roots DIR] [--top 60]

    # re-render HTML from an existing profile.json (fast; design/template tweaks, --meta)
    hotspots.py render --json out/profile.json --out out/ --title "My guest"

Backends: `zisk` (via ziskemu) and `sp1` (via sp1-runner built with --features profiling).

Common JSON schema (what a backend must produce, per input tag):
    { "<tag>": {
        "meta": {steps, cost, emu, [txs,gas,hash,root,root_ok]},
        "total_count": <int>,                       # total attributed instructions
        "functions": [{name, module, count}, ...],  # top-N, hottest first
        "categories": [{name, cost, pct}, ...],
        "opcodes":    [{name, cost, pct}, ...] } }

To add a backend: write `profile_<name>(args) -> dict` in that schema and register it in BACKENDS.
The renderer, module extraction, --meta/--labels and CLI are already shared.
"""
import argparse, glob, json, os, re, statistics, subprocess, tempfile, time

# ─────────────────────────── common (prover-agnostic) ───────────────────────────

SKIP = {'void','bool','int','unsigned','signed','long','short','char','double','float',
        'auto','const','static','volatile','wchar_t','size_t'}

def demangle(s):
    return re.sub(r'::h[0-9a-f]{16}$', '', re.sub(r'17h[0-9a-f]{16}E?$', '', s))

def module(name):
    """First-level module/crate for a Rust or C++ mangled-ish symbol."""
    s = name.strip()
    m = re.match(r'^<(.+?) as .+?>', s)        # Rust: <T as Trait>::m -> crate of T
    if m: s = m.group(1)
    s = s.lstrip('<').strip()
    parts = s.split(' ')                       # C++: drop leading return-type/qualifiers
    i = 0
    while i < len(parts) and parts[i] in SKIP: i += 1
    if i < len(parts): s = parts[i]
    tok = re.match(r'[A-Za-z_][A-Za-z0-9_]*', s)
    mod = tok.group(0) if tok else 'other'
    if mod == '__gnu_cxx': mod = 'std'
    if mod in ('__bswapdi2','__bswapsi2','__bswapti2'): mod = 'builtins'
    if mod in ('memcpy','memmove','memset','memcmp','bcmp','strlen'): mod = 'mem'
    if mod in ('operator','malloc','free','sys_alloc_aligned','sys_free','_Znwm','_Znam','_ZdlPv'): mod = 'alloc'
    return mod

def _root_file(dirpath, tag):
    """post_state_root file for a tag, tolerant of the `1-` chain prefix (zisk keeps it in the tag,
    sp1 strips it; the files are named 1-<block>.post_state_root). Returns a path or None."""
    base = re.sub(r'^1-', '', tag)
    for cand in (tag, base, '1-' + base):
        p = os.path.join(dirpath, cand + '.post_state_root')
        if os.path.exists(p): return p
    return None

def _aggregate(data, top=200):
    """Fold several per-input profiles (common schema) into ONE mean-per-block profile: functions /
    modules = mean count across the N inputs (absent = 0); categories / opcodes = mean cost; meta =
    mean of numeric fields. Each function also gets `cv` (coeff. of variation across blocks — how
    stable that hotspot is). Used by `profile --aggregate` and by `diff`. No call-tree."""
    entries = [e for e in data.values() if isinstance(e, dict)]
    n = len(entries) or 1
    perentry = [{f['name']: f['count'] for f in e.get('functions', [])} for e in entries]
    fmod = {}
    for e in entries:
        for f in e.get('functions', []): fmod.setdefault(f['name'], f.get('module', 'other'))
    funcs = []
    for nm in fmod:
        vals = [pe.get(nm, 0) for pe in perentry]
        mean = sum(vals) / n
        cv = (statistics.pstdev(vals) / mean) if (mean and len(vals) > 1) else 0.0
        funcs.append({'name': nm, 'module': fmod[nm], 'count': round(mean), 'cv': round(cv, 3)})
    funcs.sort(key=lambda x: -x['count'])
    modtot = {}
    for e in entries:
        for m, c in e.get('modules', {}).items(): modtot[m] = modtot.get(m, 0) + c
    modtot = {m: round(c / n) for m, c in modtot.items()}
    def _meancost(key):
        agg = {}
        for e in entries:
            for x in e.get(key, []): agg[x['name']] = agg.get(x['name'], 0.0) + x.get('cost', 0)
        tot = sum(agg.values()) or 1
        return [{'name': k, 'cost': round(v / n), 'pct': round(100 * v / tot, 1)}
                for k, v in sorted(agg.items(), key=lambda x: -x[1])]
    meta = {'n': len(entries)}
    for k in ('steps', 'cost', 'emu', 'gas', 'txs'):
        vals = [e['meta'][k] for e in entries if isinstance(e.get('meta', {}).get(k), (int, float))]
        if vals: meta[k] = round(sum(vals) / len(vals), 3) if k == 'emu' else round(sum(vals) / len(vals))
    return {'meta': meta, 'total_count': round(sum(e.get('total_count', 0) for e in entries) / n),
            'functions': funcs[:top], 'modules': modtot,
            'categories': _meancost('categories'), 'opcodes': _meancost('opcodes')[:12]}

def render_html(data, args):
    if getattr(args, 'meta', None):
        mm = json.loads(open(args.meta).read()) if os.path.exists(args.meta) else json.loads(args.meta)
        for tag, extra in mm.items():
            if tag in data: data[tag]['meta'].update(extra)
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'template.html')).read()
    cfg = {
        'eyebrow': args.eyebrow or 'zkVM · guest execution profile',
        'title': args.title or 'Where the proving cost goes',
        'subtitle': args.subtitle or 'Guest replayed and profiled by instruction cost and function hotspot.',
    }
    if args.labels:
        cfg['labels'] = json.loads(open(args.labels).read()) if os.path.exists(args.labels) else json.loads(args.labels)
    # per-backend copy: count unit label ('steps'/'cycles'), exec-card label, footer note
    if getattr(args, 'unit', None):       cfg['unit'] = args.unit
    if getattr(args, 'exec_label', None): cfg['execLabel'] = args.exec_label
    if getattr(args, 'note', None):       cfg['note'] = args.note
    # `_display` is a render-time carrier (see cmd_profile/cmd_render), not page data — strip it
    # so the emitted HTML payload is exactly the guest metrics, unchanged from before.
    page = {k: {kk: vv for kk, vv in v.items() if kk != '_display'} for k, v in data.items()}
    html = tpl.replace('__DATA__', json.dumps(page, separators=(',', ':'))).replace('__CFG__', json.dumps(cfg))
    assert '__DATA__' not in html and '__CFG__' not in html, "template placeholders not filled"
    op = os.path.join(args.out, args.name)
    open(op, 'w').write(html)
    print("wrote", op, f"({os.path.getsize(op)} bytes)  — open in a browser")

# ─────────────────────────────── backend: zisk ──────────────────────────────────

_lab = re.compile(r'^[0-9a-f]{8,}\s+<(.+)>:')
_cnt = re.compile(r'^\s*[0-9a-f]{8}:\s+(\d+)\s')

def _zisk_run(emu, elf, inp, extra, disasm=None, out=None):
    cmd = [emu, '-e', elf, '-i', inp] + extra
    if disasm: cmd += ['--disasm', disasm]
    if out: cmd += ['-o', out]
    return subprocess.run(cmd, capture_output=True, text=True)   # caller reads .stdout / checks .returncode

def _zisk_report(txt):
    num = lambda p: (int(re.search(p, txt).group(1).replace(',', '')) if re.search(p, txt) else None)
    meta = {'steps': num(r'STEPS\s+([\d,]+)'), 'cost': num(r'COST\s+([\d,]+)')}
    for k, pat in (('txs', r'Transaction Count:\s*(\d+)'), ('gas', r'Gas Consumed:\s*(\d+)')):
        v = num(pat)
        if v is not None: meta[k] = v
    mh = re.search(r'Block Hash:\s*(0x[0-9a-fA-F]+)', txt)
    if mh: meta['hash'] = mh.group(1)
    cats = []
    for nm in ('Base','Main','Opcodes','Precompiles','Memory'):
        m = re.search(rf'{nm}\s+[█░]+\s+([\d,]+)\s+([\d.]+)%', txt)
        if m: cats.append({'name': nm, 'cost': int(m.group(1).replace(',', '')), 'pct': float(m.group(2))})
    ops = [{'name': m.group(1), 'cost': int(m.group(2).replace(',', '')), 'pct': float(m.group(3))}
           for m in re.finditer(r'║\s*([a-z0-9_]+)\s+[█░]+\s+([\d,]+)\s+([\d.]+)%\s*║', txt)]
    return meta, cats, ops[:12]

def _zisk_disasm(path, top):
    cur = '(prologue)'; agg = {}
    with open(path, 'r', errors='replace') as fh:
        for line in fh:
            m = _lab.match(line)
            if m:
                n = m.group(1)
                if not n.startswith('.L'): cur = n
                continue
            c = _cnt.match(line)
            if c: agg[cur] = agg.get(cur, 0) + int(c.group(1))
    tot = sum(agg.values())
    modtot = {}                                    # full per-module totals over ALL functions
    for n, c in agg.items():
        m = module(n); modtot[m] = modtot.get(m, 0) + c
    funcs = [{'name': demangle(n)[:90], 'module': module(n), 'count': c}
             for n, c in sorted(agg.items(), key=lambda x: -x[1])[:top]]
    return tot, funcs, modtot

def profile_zisk(args):
    """Collect a profile for each input via ziskemu -> common JSON schema."""
    emu = os.path.expanduser(args.emu)
    if not os.path.exists(emu): raise SystemExit(f"ziskemu not found: {emu} (install via ziskup, or pass --emu)")
    data = {}
    for inp in args.input:
        tag = os.path.splitext(os.path.basename(inp))[0]
        print(f"[{tag}] profiling…", flush=True)
        t0 = time.time()                                    # 1) fast pass: emu time + steps
        mtxt = _zisk_run(emu, args.elf, inp, ['-m']).stdout
        md = re.search(r'duration=([\d.]+)', mtxt)
        emu_s = float(md.group(1)) if md else round(time.time() - t0, 3)
        with tempfile.NamedTemporaryFile(suffix='.disasm', delete=False) as tf:
            dpath = tf.name
        outbin = dpath + '.out' if args.verify_roots else None
        p = _zisk_run(emu, args.elf, inp, ['-X','-S','--sdk','--opcodes','-H','12'], disasm=dpath, out=outbin)
        txt = p.stdout
        meta, cats, ops = _zisk_report(txt)
        if p.returncode != 0 or meta.get('steps') is None:
            raise SystemExit(f"[{tag}] ziskemu failed (rc={p.returncode}) or emitted no step count "
                             f"(bad ELF/input?).\n{(p.stderr or txt)[-1500:]}")
        meta['emu'] = emu_s
        if args.verify_roots:
            rf = _root_file(args.verify_roots, tag)
            if rf:
                exp = ''.join(ch for ch in open(rf).read() if ch in '0123456789abcdefABCDEF').lower()[-64:]
                got = open(outbin, 'rb').read()[:32].hex() if outbin and os.path.exists(outbin) else ''
                meta['root'] = '0x' + exp
                meta['root_ok'] = (got == exp)
            if outbin and os.path.exists(outbin): os.remove(outbin)
        tot, funcs, modtot = _zisk_disasm(dpath, args.top)
        os.remove(dpath)
        data[tag] = {'meta': meta, 'total_count': tot, 'functions': funcs,
                     'modules': modtot, 'categories': cats, 'opcodes': ops}
        _n = lambda v: f"{v:,}" if isinstance(v, (int, float)) else "?"
        _top = f"{funcs[0]['module']}/{funcs[0]['count']*100//max(tot,1)}%" if funcs else "(none)"
        print(f"  steps={_n(meta.get('steps'))} cost={_n(meta.get('cost'))} emu={emu_s}s top={_top}", flush=True)
    return data

# ─────────────────────────────── backend: sp1 ───────────────────────────────────
#
# Collects the same schema for SP1 guests:
#   • functions  ← SP1's sampling profiler (Gecko trace via TRACE_FILE; needs an
#                  sp1-runner built with --features profiling). Leaf frame of each
#                  sample = enclosing function (self time), same notion as the zisk icicle.
#   • opcodes/categories/cost ← the ExecutionReport's opcode_counts + syscall_counts,
#                  each weighted by SP1's own trace-area cost model
#                  (sp1-core-executor .../artifacts/rv64im_costs.json, `User` variants —
#                  our guests are untrusted user code). `cost` = Σ count·weight = a
#                  trace-cell proxy, the SP1 analogue of the zisk COST.
# Input is the RAW witness/.bin (the runner wraps it via SP1Stdin::write_slice); no
# LE64 framing (that's zisk-only).

def _default_sp1_costs():
    """Locate rv64im_costs.json in the cargo registry via glob, so it survives a different registry
    hash or an sp1-core-executor version bump (newest match wins). Override with --costs."""
    pat = os.path.expanduser('~/.cargo/registry/src/*/sp1-core-executor-*/src/artifacts/rv64im_costs.json')
    hits = sorted(glob.glob(pat))
    return hits[-1] if hits else pat   # pattern itself if nothing found -> a clear error on open

_SP1_DEFAULT_COSTS = _default_sp1_costs()

# RISC-V mnemonic (opcode_counts key) → (cost-model event [User variant], category)
_SP1_OPMAP = {
 'ADD':('AddUser','Opcodes'),'ADDI':('AddiUser','Opcodes'),'ADDW':('AddwUser','Opcodes'),
 'SUB':('SubUser','Opcodes'),'SUBW':('SubwUser','Opcodes'),
 'AND':('BitwiseUser','Opcodes'),'OR':('BitwiseUser','Opcodes'),'XOR':('BitwiseUser','Opcodes'),
 'SLL':('ShiftLeftUser','Opcodes'),'SLLW':('ShiftLeftUser','Opcodes'),
 'SRL':('ShiftRightUser','Opcodes'),'SRLW':('ShiftRightUser','Opcodes'),
 'SRA':('ShiftRightUser','Opcodes'),'SRAW':('ShiftRightUser','Opcodes'),
 'SLT':('LtUser','Opcodes'),'SLTU':('LtUser','Opcodes'),
 'MUL':('MulUser','Opcodes'),'MULH':('MulUser','Opcodes'),'MULHSU':('MulUser','Opcodes'),
 'MULHU':('MulUser','Opcodes'),'MULW':('MulUser','Opcodes'),
 'DIV':('DivRemUser','Opcodes'),'DIVU':('DivRemUser','Opcodes'),'DIVW':('DivRemUser','Opcodes'),
 'DIVUW':('DivRemUser','Opcodes'),'REM':('DivRemUser','Opcodes'),'REMU':('DivRemUser','Opcodes'),
 'REMW':('DivRemUser','Opcodes'),'REMUW':('DivRemUser','Opcodes'),
 'LB':('LoadByteUser','Memory'),'LBU':('LoadByteUser','Memory'),'LH':('LoadHalfUser','Memory'),
 'LHU':('LoadHalfUser','Memory'),'LW':('LoadWordUser','Memory'),'LWU':('LoadWordUser','Memory'),
 'LD':('LoadDoubleUser','Memory'),
 'SB':('StoreByteUser','Memory'),'SH':('StoreHalfUser','Memory'),'SW':('StoreWordUser','Memory'),
 'SD':('StoreDoubleUser','Memory'),
 'BEQ':('BranchUser','Opcodes'),'BNE':('BranchUser','Opcodes'),'BLT':('BranchUser','Opcodes'),
 'BGE':('BranchUser','Opcodes'),'BLTU':('BranchUser','Opcodes'),'BGEU':('BranchUser','Opcodes'),
 'JAL':('JalUser','Opcodes'),'JALR':('JalrUser','Opcodes'),
 'LUI':('UTypeUser','Opcodes'),'AUIPC':('UTypeUser','Opcodes'),
 'ECALL':('SyscallInstrsUser','Precompiles'),'EBREAK':('SyscallInstrsUser','Opcodes'),
 'UNIMP':(None,'Opcodes'),
}
# syscall_counts key → cost-model event (User variant where present)
_SP1_SYSMAP = {
 'KECCAK_PERMUTE':'KeccakPermute','SHA_COMPRESS':'ShaCompress','SHA_EXTEND':'ShaExtend',
 'SECP256K1_ADD':'Secp256k1AddAssignUser','SECP256K1_DOUBLE':'Secp256k1DoubleAssignUser',
 'SECP256R1_ADD':'Secp256r1AddAssignUser','SECP256R1_DOUBLE':'Secp256r1DoubleAssignUser',
 'BN254_ADD':'Bn254AddAssignUser','BN254_DOUBLE':'Bn254DoubleAssignUser',
 'BN254_FP_ADD':'Bn254FpOpAssignUser','BN254_FP_SUB':'Bn254FpOpAssignUser','BN254_FP_MUL':'Bn254FpOpAssignUser',
 'BN254_FP2_ADD':'Bn254Fp2AddSubAssignUser','BN254_FP2_SUB':'Bn254Fp2AddSubAssignUser','BN254_FP2_MUL':'Bn254Fp2MulAssignUser',
 'BLS12381_ADD':'Bls12381AddAssignUser','BLS12381_DOUBLE':'Bls12381DoubleAssignUser',
 'BLS12381_FP_ADD':'Bls12381FpOpAssignUser','BLS12381_FP_SUB':'Bls12381FpOpAssignUser','BLS12381_FP_MUL':'Bls12381FpOpAssignUser',
 'BLS12381_FP2_ADD':'Bls12381Fp2AddSubAssignUser','BLS12381_FP2_SUB':'Bls12381Fp2AddSubAssignUser','BLS12381_FP2_MUL':'Bls12381Fp2MulAssignUser',
 'ED_ADD':'EdAddAssignUser','ED_DECOMPRESS':'EdDecompressUser','POSEIDON2':'Poseidon2User',
 'UINT256_MUL':'Uint256OpsUser','UINT256_MUL_CARRY':'Uint256OpsUser','UINT256_ADD_CARRY':'Uint256OpsUser',
}

def _sp1_costs(report, costs):
    """ExecutionReport JSON -> (cost, categories, opcodes) using SP1's trace-area weights."""
    er = report['execution_report']
    oc, sc = er['opcode_counts'], er['syscall_counts']
    cyc = report['cycles']
    W = lambda k: costs.get(k, 0)
    # Main = per-instruction fetch backbone (∝ cycles). Validated ~within 10% of the
    # report's gas·191/3 trace-area; adding InstructionDecode roughly doubles it and
    # drifts from gas, so we keep fetch only.
    cat = {'Main': cyc * W('InstructionFetch'), 'Opcodes': 0, 'Memory': 0, 'Precompiles': 0}
    opc = {}
    for op, c in oc.items():
        if not c: continue
        ev, g = _SP1_OPMAP.get(op, (None, 'Opcodes'))
        if not ev: continue
        cost = c * W(ev); cat[g] += cost; opc[op.lower()] = opc.get(op.lower(), 0) + cost
    for sy, c in sc.items():
        if not c: continue
        ev = _SP1_SYSMAP.get(sy)
        if not ev: continue
        cost = c * W(ev); cat['Precompiles'] += cost; opc[sy.lower()] = cost
    total = sum(cat.values()) or 1
    cats = [{'name': k, 'cost': v, 'pct': round(100*v/total, 1)}
            for k, v in sorted(cat.items(), key=lambda x: -x[1])]
    ops = [{'name': k, 'cost': v, 'pct': round(100*v/total, 1)}
           for k, v in sorted(opc.items(), key=lambda x: -x[1])[:12]]
    return total, cats, ops

def _cxxfilt(names):
    """Batch-demangle C++ (Itanium _Z…) names; Rust / plain names pass through unchanged."""
    dm = names
    try:
        out = subprocess.run(['c++filt'], input='\n'.join(names), capture_output=True, text=True)
        if out.returncode == 0:
            lines = out.stdout.split('\n')
            if len(lines) >= len(names): dm = lines[:len(names)]
    except FileNotFoundError:
        pass
    return dict(zip(names, dm))

def _sp1_gecko(gecko_path, top, tree_prune=0.003):
    """Gecko trace -> (flat leaf hotspot: total, top-N funcs, module totals) + a pruned
    call-TREE {name,module,value,children[]} for the flamegraph (value = samples through node)."""
    d = json.load(open(gecko_path))
    th = d['threads'][0]
    strings = th['stringTable']
    stacks = th['stackTable']['data']; frames = th['frameTable']['data']
    fcol = th['stackTable']['schema']['frame']; loc = th['frameTable']['schema']['location']
    pcol = th['stackTable']['schema']['prefix']; scol = th['samples']['schema']['stack']
    # resolve every frame once: frame_idx -> (display name, module)
    demap = _cxxfilt([strings[f[loc]] for f in frames])
    fname = [None] * len(frames); fmod = [None] * len(frames)
    for i, f in enumerate(frames):
        dm = demap[strings[f[loc]]]
        fname[i] = demangle(dm)[:90]; fmod[i] = module(dm)
    # frame chain (root->leaf) per stack via DP over prefix (prefix index always precedes)
    chain = [None] * len(stacks)
    for i, st in enumerate(stacks):
        pre = st[pcol]
        chain[i] = ((chain[pre] if pre is not None else []) + [st[fcol]])
    # count samples per stack, then fold into flat leaf agg + call tree
    hist = {}
    for s in th['samples']['data']:
        si = s[scol]
        if si is not None: hist[si] = hist.get(si, 0) + 1
    agg = {}                                            # flat: leaf frame -> count (self time)
    root = {'name': '(root)', 'module': 'root', 'value': 0, 'children': {}}
    for si, c in hist.items():
        ch = chain[si]
        if not ch: continue
        agg[ch[-1]] = agg.get(ch[-1], 0) + c
        node = root; node['value'] += c
        for fr in ch:
            k = fname[fr]
            nx = node['children'].get(k)
            if nx is None:
                nx = {'name': k, 'module': fmod[fr], 'value': 0, 'children': {}}
                node['children'][k] = nx
            nx['value'] += c; node = nx
    tot = sum(agg.values())
    modtot = {}
    for fr, c in agg.items():
        modtot[fmod[fr]] = modtot.get(fmod[fr], 0) + c
    funcs = [{'name': fname[fr], 'module': fmod[fr], 'count': c}
             for fr, c in sorted(agg.items(), key=lambda x: -x[1])[:top]]
    # prune tiny subtrees and convert children dict -> sorted list (keeps the tree/HTML small)
    thr = max(1, int(root['value'] * tree_prune))
    def conv(n):
        kids = [conv(c) for c in n['children'].values() if c['value'] >= thr]
        kids.sort(key=lambda x: -x['value'])
        return {'name': n['name'], 'module': n['module'], 'value': n['value'], 'children': kids}
    return tot, funcs, modtot, conv(root)

def profile_sp1(args):
    """Collect a profile for each input via the SP1 executor -> common JSON schema."""
    runner = os.path.expanduser(args.runner)
    if not os.path.exists(runner):
        raise SystemExit(f"sp1-runner (profiling build) not found: {runner}\n"
                         f"build it: cd infra/sp1-infra/sp1-runner && "
                         f"cargo build --release --no-default-features --features profiling --target-dir target-prof")
    costs = json.load(open(os.path.expanduser(args.costs)))
    data = {}
    for inp in args.input:
        tag = re.sub(r'^1-', '', os.path.splitext(os.path.basename(inp))[0])
        print(f"[{tag}] profiling…", flush=True)
        with tempfile.TemporaryDirectory() as td:
            gecko = os.path.join(td, 'trace.json'); rep = os.path.join(td, 'report.json')
            pv = os.path.join(td, 'pv.bin')
            env = dict(os.environ, TRACE_FILE=gecko, TRACE_SAMPLE_RATE=str(args.sample_rate),
                       SP1_PROVER='cpu')
            t0 = time.time()
            r = subprocess.run([runner, '--mode', 'execute', '--elf', args.elf, '--input', inp,
                                '--report', rep, '--public-values', pv],
                               env=env, capture_output=True, text=True)
            if not os.path.exists(rep):
                raise SystemExit(f"[{tag}] runner failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
            report = json.load(open(rep))
            cost, cats, ops = _sp1_costs(report, costs)
            # 'steps' holds the guest's native count (cycles for SP1) — the card is
            # relabelled via cfg.unit. Prover gas (PGU) is deliberately NOT put in
            # 'gas': that card is EVM gas, supplied via --meta.
            meta = {'steps': report['cycles'], 'cost': cost,
                    'emu': round(report.get('elapsed_secs', time.time()-t0), 3)}
            if args.verify_roots:
                rf = _root_file(args.verify_roots, tag)
                if rf and os.path.exists(pv):
                    exp = ''.join(ch for ch in open(rf).read() if ch in '0123456789abcdefABCDEF').lower()[-64:]
                    got = open(pv, 'rb').read()[:32].hex()
                    meta['root'] = '0x' + exp; meta['root_ok'] = (got == exp)
            if not os.path.exists(gecko):
                raise SystemExit(f"[{tag}] no Gecko trace at {gecko} — is the runner built with "
                                 f"--features profiling? (expected under target-prof/…/sp1-runner)")
            tot, funcs, modtot, tree = _sp1_gecko(gecko, args.top)
        data[tag] = {'meta': meta, 'total_count': tot, 'functions': funcs,
                     'modules': modtot, 'categories': cats, 'opcodes': ops, 'tree': tree}
        _top = f"{funcs[0]['module']}/{funcs[0]['count']*100//max(tot,1)}%" if funcs else "(none)"
        print(f"  cycles={meta['steps']:,} cost={cost:,} emu={meta['emu']}s top={_top}", flush=True)
    return data

BACKENDS = {'zisk': profile_zisk, 'sp1': profile_sp1}

# ─────────────────────────────────── CLI ────────────────────────────────────────

_BACKEND_CFG = {  # per-backend display defaults (used unless overridden on the CLI)
    'sp1': {'unit': 'cycles', 'exec_label': 'Exec (SP1)',
            'note': 'Flat profile: each sample attributed to its leaf (enclosing) function. '
                    "SP1's sampling profiler (Gecko trace) carries call stacks, so this backend "
                    'also renders a call-tree flamegraph; the icicle here is the flattened hotspot view.'},
}

def cmd_profile(args):
    for k, v in _BACKEND_CFG.get(args.backend, {}).items():
        if getattr(args, k, None) is None: setattr(args, k, v)
    data = BACKENDS[args.backend](args)
    if getattr(args, 'aggregate', None):
        agg = _aggregate(data, top=args.top)
        var = sorted((f for f in agg['functions'] if f.get('cv')), key=lambda x: -x['cv'])[:8]
        print("aggregate over %d inputs — most variable functions (cv across blocks): %s" % (
            len(data), ", ".join(f"{f['name'][:24]}={f['cv']:.2f}" for f in var) or "—"), flush=True)
        data = {args.aggregate: agg}
    if getattr(args, 'tab_prefix', ''):
        data = {args.tab_prefix + k: v for k, v in data.items()}
    # Stamp the resolved display cfg into each entry so `render` reproduces the same
    # unit / exec-label / note without re-passing the flags — else re-rendering an SP1
    # profile.json falls back to the zisk defaults ('steps' / 'Exec (ziskemu)').
    disp = {'unit': getattr(args, 'unit', None), 'execLabel': getattr(args, 'exec_label', None),
            'note': getattr(args, 'note', None)}
    for e in data.values():
        e.setdefault('_display', disp)
    os.makedirs(args.out, exist_ok=True)
    json.dump(data, open(os.path.join(args.out, 'profile.json'), 'w'))
    print("wrote", os.path.join(args.out, 'profile.json'))
    render_html(data, args)

def cmd_render(args):
    os.makedirs(args.out, exist_ok=True)
    data = {}
    for jf in args.json:                       # --json is repeatable: merge several guests' profiles
        d = json.load(open(jf))
        dup = data.keys() & d.keys()
        if dup:
            raise SystemExit(f"render: duplicate tab key(s) {sorted(dup)} across --json files — "
                             f"re-profile the colliding guest with --tab-prefix to namespace it.")
        data.update(d)
    # Recover the display cfg the profile(s) were generated with (unit / exec-label / note),
    # unless overridden on the CLI — keeps an SP1 profile.json labeled as SP1 on re-render
    # instead of falling back to the zisk defaults.
    disp = next((e['_display'] for e in data.values()
                 if isinstance(e, dict) and e.get('_display')), {})
    if getattr(args, 'unit', None) is None:       args.unit = disp.get('unit')
    if getattr(args, 'exec_label', None) is None: args.exec_label = disp.get('execLabel')
    if getattr(args, 'note', None) is None:       args.note = disp.get('note')
    render_html(data, args)

def _diff(dA, dB, la, lb, top):
    """Aggregate two profile-data dicts and print per-module / per-function deltas (A over B)."""
    ea, eb = _aggregate(dA, top=10**9), _aggregate(dB, top=10**9)
    ta, tb = ea['total_count'] or 1, eb['total_count'] or 1
    print(f"=== hotspots diff: {la} vs {lb} ===")
    print(f"total attributed: {la} {ta:,} · {lb} {tb:,} · Δ {(ta - tb) / tb * 100:+.1f}% ({la} over {lb})\n")
    print(f"  {'module':<22}{la[:10]:>12}{lb[:10]:>12}{'Δcount':>13}{'Δ%oftot':>10}")
    rows = [(m, ea['modules'].get(m, 0), eb['modules'].get(m, 0))
            for m in set(ea['modules']) | set(eb['modules'])]
    for m, a, b in sorted(rows, key=lambda x: -abs(x[1] - x[2]))[:top]:
        print(f"  {m[:22]:<22}{a:>12,}{b:>12,}{a - b:>+13,}{(a / ta - b / tb) * 100:>+9.2f}%")
    fa = {f['name']: f['count'] for f in ea['functions']}
    fb = {f['name']: f['count'] for f in eb['functions']}
    print(f"\n  top {top} function movers (Δcount):")
    for nm in sorted(set(fa) | set(fb), key=lambda k: -abs(fa.get(k, 0) - fb.get(k, 0)))[:top]:
        a, b = fa.get(nm, 0), fb.get(nm, 0)
        print(f"  {nm[:44]:<44}{a:>12,}{b:>12,}{a - b:>+13,}")

def cmd_diff(args):
    """Per-module / per-function delta between two EXISTING profiles (each aggregated). Text output —
    e.g. `diff --json <monad>/profile.json --json <rsp>/profile.json --label Monad --label reth`
    shows WHERE one guest spends more trace than the other on the same zkVM."""
    if len(args.json) != 2:
        raise SystemExit("diff needs exactly two --json (A then B)")
    A, B = json.load(open(args.json[0])), json.load(open(args.json[1]))
    lbls = args.label or []
    la = lbls[0] if len(lbls) > 0 else (os.path.basename(os.path.dirname(args.json[0])) or 'A')
    lb = lbls[1] if len(lbls) > 1 else (os.path.basename(os.path.dirname(args.json[1])) or 'B')
    _diff(A, B, la, lb, args.top)

def cmd_compare(args):
    """Profile the SAME inputs through two ELFs (before/after a guest change) and print the diff — the
    one-shot before/after tool. Δ (after over before) < 0 means the change made the guest cheaper."""
    la, lb = args.label_after or 'after', args.label_before or 'before'
    def _run(elf, which):
        args.elf = elf
        print(f"== profiling {which}: {elf} ==", flush=True)
        return BACKENDS[args.backend](args)
    before = _run(args.elf_before, lb)
    after = _run(args.elf_after, la)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        json.dump(before, open(os.path.join(args.out, 'before.profile.json'), 'w'))
        json.dump(after,  open(os.path.join(args.out, 'after.profile.json'), 'w'))
        print(f"profiles -> {args.out}/{{before,after}}.profile.json", flush=True)
    _diff(after, before, la, lb, args.top)

def main():
    p = argparse.ArgumentParser(prog='hotspots', description='zkVM guest hotspot profiler + HTML report.')
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('profile', 'render'):
        sp = sub.add_parser(name)
        sp.add_argument('--out', required=True, help='output directory')
        sp.add_argument('--name', default='index.html', help='HTML filename (default index.html)')
        sp.add_argument('--title'); sp.add_argument('--subtitle'); sp.add_argument('--eyebrow')
        sp.add_argument('--labels', help='JSON map {tag: label} or path to one')
        sp.add_argument('--unit', help="count-card / tab unit label (default 'steps'; sp1 defaults to 'cycles')")
        sp.add_argument('--exec-label', dest='exec_label', help="label for the exec-time card (default 'Exec (ziskemu)')")
        sp.add_argument('--note', help='footer note under the icicle (backend sets a sensible default)')
        sp.add_argument('--meta', help='JSON map {tag:{field:val}} merged into each entry meta (or path); '
                                       'use to add block info (txs/gas/hash/root) a guest does not print')
        if name == 'profile':
            sp.add_argument('--backend', choices=sorted(BACKENDS), default='zisk')
            sp.add_argument('--elf', required=True)
            sp.add_argument('-i', '--input', action='append', required=True, help='framed input (repeatable)')
            sp.add_argument('--emu', default='~/.zisk/bin/ziskemu', help='[zisk] ziskemu path')
            sp.add_argument('--runner', default='../infra/sp1-infra/sp1-runner/target-prof/release/sp1-runner',
                            help='[sp1] sp1-runner built with --features profiling')
            sp.add_argument('--sample-rate', type=int, default=200,
                            help='[sp1] profiler sample period in cycles (TRACE_SAMPLE_RATE, default 200)')
            sp.add_argument('--costs', default=_SP1_DEFAULT_COSTS, help='[sp1] rv64im_costs.json path')
            sp.add_argument('--top', type=int, default=200, help='top-N functions named individually in the icicle; the rest of each module aggregates into a hatched tail (default 200 keeps per-module "rest" well under 1%%)')
            sp.add_argument('--verify-roots', help='dir with <tag>.post_state_root to verify output[:32]')
            sp.add_argument('--tab-prefix', dest='tab_prefix', default='',
                            help='prefix every tab/tag key — namespaces a guest so profiles merge cleanly '
                                 '(e.g. --tab-prefix rsp- , then: render --json a --json b)')
            sp.add_argument('--aggregate', nargs='?', const='aggregate', default=None,
                            help='fold ALL inputs into one mean-per-block profile (optional label, default '
                                 '"aggregate") instead of one tab each — for many blocks; adds per-function cv')
        else:
            sp.add_argument('--json', action='append', required=True,
                            help='profile.json to render; repeat (--json a --json b) to merge several guests into one report')
    spd = sub.add_parser('diff', help='compare two existing profiles per module/function (e.g. Monad vs reth)')
    spd.add_argument('--json', action='append', required=True, help='exactly two profile.json: A then B')
    spd.add_argument('--label', action='append', help='labels for A and B (default: their folder names)')
    spd.add_argument('--top', type=int, default=25, help='rows to show (default 25)')

    spc = sub.add_parser('compare', help='profile the SAME inputs through two ELFs (before/after a change) and diff')
    spc.add_argument('--backend', choices=sorted(BACKENDS), default='zisk')
    spc.add_argument('--elf-before', dest='elf_before', required=True)
    spc.add_argument('--elf-after',  dest='elf_after',  required=True)
    spc.add_argument('-i', '--input', action='append', required=True, help='input (repeatable) — the SAME for both ELFs')
    spc.add_argument('--emu', default='~/.zisk/bin/ziskemu', help='[zisk] ziskemu path')
    spc.add_argument('--runner', default='../infra/sp1-infra/sp1-runner/target-prof/release/sp1-runner',
                     help='[sp1] sp1-runner built with --features profiling')
    spc.add_argument('--sample-rate', dest='sample_rate', type=int, default=200, help='[sp1] TRACE_SAMPLE_RATE')
    spc.add_argument('--costs', default=_SP1_DEFAULT_COSTS, help='[sp1] rv64im_costs.json path')
    spc.add_argument('--verify-roots', dest='verify_roots', help='dir with <tag>.post_state_root to verify output[:32]')
    spc.add_argument('--top', type=int, default=25, help='rows in the diff (default 25)')
    spc.add_argument('--out', help='optional dir to save before/after profile.json')
    spc.add_argument('--label-before', dest='label_before', help='label for the before ELF (default "before")')
    spc.add_argument('--label-after',  dest='label_after',  help='label for the after ELF (default "after")')

    a = p.parse_args()
    {'profile': cmd_profile, 'render': cmd_render, 'diff': cmd_diff, 'compare': cmd_compare}[a.cmd](a)

if __name__ == '__main__':
    main()
