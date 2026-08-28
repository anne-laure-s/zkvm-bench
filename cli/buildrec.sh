# cli/buildrec.sh — the build record. One shape for every built ELF. Sourced, never run.
#
#   guests/<name>/<name>.build.json
#
# Required keys, always present, same meaning everywhere:
#
#   schema       1
#   commit       the source commit the ELF was built from (full sha), or null if unknown
#   elf          repo-relative path of the ELF this record describes
#   elf_sha256   sha256 of that ELF — the identity profiling/cache keys on — or null if absent
#
# Anything a particular builder knows on top goes in beside them. `source` holds the pins when one
# commit does not name the build (ziskethone needs two: its own submodule and the driver that sets
# its flags); `evidence` and `features` are what an upstream auditor asserted. Extra keys are
# preserved on rewrite, so a builder that knows more never loses it to one that knows less.
#
# WHY ONE SHAPE. This replaced five: a bare `<name>.commit` for the reth guests, a sha table in
# guests/monad-variants/README.md, a KEY=value file for ziskethone, and a JSON of its own for the
# Monad guests. They overlapped by accident and diverged in the direction of the arrow — one was
# read by the build as authority, another written by it as a finding — so nothing could read "which
# build produced this number" without knowing which guest it was asking about.
#
# What is DELIBERATELY not this, because it describes something else than one built ELF:
#
#   guests/monad/gen/<G>/PROVENANCE.md      a corpus + the prose about which generations are
#                                           mutually incompatible. No JSON replaces that.
#   profiling/series/<lineage>-index.tsv    82 builds of one lineage. A table, not 82 files.
#
# Use:
#   . "$REPO/cli/buildrec.sh"
#   brec_file <name>                                  -> the record's path
#   brec_get  <file> <key>                            -> value, empty if absent
#   brec_stamp <file> <elf> <commit> [key=value ...]  -> write/merge; computes elf_sha256

# Every caller is bash, where BASH_SOURCE resolves. Under zsh it does not, and the root would come
# out as the repo's PARENT — every path then silently resolves one level too high. Checked rather
# than assumed: a wrong root here would put records outside the repo and hash nothing.
BREC_ROOT="${BREC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)}"
[ -f "$BREC_ROOT/cli/buildrec.sh" ] || {
    echo "buildrec.sh: cannot locate the repo root (got '${BREC_ROOT:-}')." >&2
    echo "  BASH_SOURCE only resolves under bash; set BREC_ROOT=<repo> to source this elsewhere." >&2
    return 1 2>/dev/null || exit 1
}

brec_file() { printf '%s/guests/%s/%s.build.json' "$BREC_ROOT" "$1" "$1"; }

# Empty rather than an error when the key or the file is missing: a caller that wants a default
# should say so, and a record is allowed to be partial (a fresh clone has the commit, not the sha).
brec_get() {
    [ -f "$1" ] || return 0
    BREC_F="$1" BREC_K="$2" python3 - <<'PY'
import json, os, sys
try:
    v = json.load(open(os.environ["BREC_F"])).get(os.environ["BREC_K"])
except Exception:
    sys.exit(0)
if v is not None and not isinstance(v, (dict, list)):
    sys.stdout.write(str(v))
PY
}

# One call, so a builder cannot half-fill the required keys. <elf> may be absolute or
# repo-relative; it is stored repo-relative and hashed if it exists. <commit> may be empty.
# Extra key=value pairs are stored as strings, except `null`, `true`, `false` and bare integers.
brec_stamp() {
    local file="$1" elf="$2" commit="$3"; shift 3
    BREC_F="$file" BREC_ELF="$elf" BREC_COMMIT="$commit" BREC_ROOT="$BREC_ROOT" \
        python3 - "$@" <<'PY'
import hashlib, json, os, sys

root = os.environ["BREC_ROOT"]
path = os.environ["BREC_F"]
elf = os.environ["BREC_ELF"]
absolute = elf if os.path.isabs(elf) else os.path.join(root, elf)
try:
    rel = os.path.relpath(os.path.realpath(absolute), root)
except ValueError:                      # a different volume: keep what we were given
    rel = elf

rec = {}
if os.path.exists(path):
    try:
        rec = json.load(open(path))     # keep what another builder knew
    except Exception:
        rec = {}

sha = None
if os.path.isfile(absolute):
    h = hashlib.sha256()
    with open(absolute, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()

def coerce(v):
    if v == "null":
        return None
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        return v

rec.update({"schema": 1, "commit": os.environ["BREC_COMMIT"] or None,
            "elf": rel, "elf_sha256": sha})
for kv in sys.argv[1:]:
    k, _, v = kv.partition("=")
    rec[k] = coerce(v)

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(rec, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}
