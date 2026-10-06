#!/usr/bin/env bash
# ship.sh — push the harness (cluster/ + zisk-runner) to a box in ONE stream. Run ON YOUR MAC.
#
#   ./cluster/ship.sh user@host:port [REMOTE_DIR]
#   ./cluster/ship.sh root@1.2.3.4:43707                      # -> ~/zisk-infra/ on the box
#
# Why not `scp -r cluster`: measured at 146 ms RTT on a rented box, scp pays ~38 ms PER FILE
# whatever its size — 541 B and 15 KB both took ~38 ms — so 65 files spend ~2.5 s on overhead
# before a useful byte moves. One tar stream pays it once. The upload is also window-limited to
# ~600 kB/s per TCP flow (three parallel flows reach ~2 MB/s, so the limit is the flow, not the
# pipe), which makes the second rule the important one: do not send what the box does not need.
#
# Here that is almost everything. cluster/ is 156 MB and 155 MB of it is witnesses (`.bin`) under
# tests/{r4f2,r8,paired}/inputs*, which `prepare-inputs.sh` rebuilds on the box — 4 minutes of
# upload for files that are already there. Excluded, the payload is ~250 KB and lands in under a
# second.
#
# Env:
#   EXTRA="a b"   extra paths to ship, relative to zisk-infra/ (anything outside cluster/ and
#                 zisk-runner, which are always included)
#   WITH_INPUTS=1 ship the test corpora too (+155 MB, ~4 min) — only when the box cannot rebuild them
#   PRUNE=1       delete remote files the ship does not carry (default: list them, delete nothing)
#   FORCE=1       ship even when the box already holds this exact content
#   DRY=1         list what would go and compare digests; transfer nothing
set -uo pipefail

EP="${1:?usage: $0 user@host:port [REMOTE_DIR]}"
REMOTE_DIR="${2:-zisk-infra}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # zisk-infra/
PORT="${EP##*:}"; UH="${EP%:*}"
[[ "$PORT" != "$EP" && "$PORT" =~ ^[0-9]+$ ]] || { echo "ERROR: pass user@host:port" >&2; exit 2; }
# Prune deletes under $REMOTE_DIR, so it may never be the remote $HOME itself, nor escape it.
case "$REMOTE_DIR" in
  ""|"."|"./"|".."|"~"|"~/"|/*|*..*) echo "ERROR: REMOTE_DIR must be a relative subdir of the remote \$HOME (got '$REMOTE_DIR')" >&2; exit 2 ;;
esac

# One ssh connection for the whole run: the handshake alone is ~0.5 s at this RTT and this script
# makes four round trips (digest, stream, prune, verify).
CM="/tmp/.ship-$(printf '%s' "$EP" | tr -c 'a-zA-Z0-9' '_')"
OPTS=(-p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25
      -o ControlMaster=auto -o ControlPath="$CM" -o ControlPersist=60)
sshx() { ssh "${OPTS[@]}" "$UH" "$@"; }
WIREF="$(mktemp -t shipwire)"
cleanup() { rm -f "$WIREF"; ssh "${OPTS[@]}" -O exit "$UH" 2>/dev/null || true; }
trap cleanup EXIT

# ── what goes, and what does not ──────────────────────────────────────────────────────────────
# One list, used three times: to select, to exclude from the tar, and to bound the prune. That is
# deliberate — a path excluded here is BOTH not sent AND never deleted on the box, which is what
# keeps a ship from wiping the cluster's live state (run/ logs/ runs/) or the corpora the box built
# for itself.
PATHS=(cluster zisk-runner)
[[ -n "${EXTRA:-}" ]] && for p in $EXTRA; do PATHS+=("$p"); done

# `._*`: bsdtar stores each file's xattrs in an AppleDouble companion, so a Mac-made archive
# litters the box with ._00-install-once.sh and friends. COPYFILE_DISABLE=1 stops it being
# written; the predicate stops one already on disk being counted on either side.
export COPYFILE_DISABLE=1
PRED=(! -name .DS_Store ! -name '._*' ! -name '*.pyc' ! -name '*.bin' ! -name '*.hints' ! -name '*.elf'
      ! -name '*.pid' ! -name '*.log' ! -name '*.zst' ! -name '*.tar.gz'
      ! -path '*/.git/*' ! -path '*/__pycache__/*' ! -path '*/run/*' ! -path '*/logs/*'
      ! -path '*/runs/*' ! -path '*/out/*' ! -path '*/results/*')
EX=(--exclude .DS_Store --exclude '._*' --exclude .git --exclude __pycache__ --exclude '*.pyc'
    --exclude run --exclude logs --exclude runs --exclude '*.pid' --exclude '*.log'
    --exclude '*.bin' --exclude '*.hints' --exclude '*.elf' --exclude '*.zst' --exclude '*.tar.gz'
    --exclude out --exclude results)
if [[ -z "${WITH_INPUTS:-}" ]]; then
  PRED+=(! -path '*/inputs/*' ! -path '*/inputs-*/*')
  EX+=(--exclude inputs --exclude 'inputs-*')
fi

cd "$ROOT"
for p in "${PATHS[@]}"; do [[ -e "$p" ]] || { echo "ERROR: $ROOT/$p does not exist" >&2; exit 2; }; done

# The manifest is content-only — hashes over relative paths, C collation, nothing about mtime or
# mode, so a fresh clone of the same commit ships as "already there".
FILES=()
while IFS= read -r f; do FILES+=("$f"); done < <(find "${PATHS[@]}" -type f "${PRED[@]}" | LC_ALL=C sort)
[[ ${#FILES[@]} -gt 0 ]] || { echo "ERROR: nothing to ship" >&2; exit 2; }
# The manifest is newline-delimited on both ends, so a path with whitespace would silently split it
# into two entries that hash to nothing. None exist here; say so rather than ship a false digest.
for f in "${FILES[@]}"; do
  case "$f" in *[$' \t\n']*) echo "ERROR: whitespace in a path breaks the manifest: $f" >&2; exit 2 ;; esac
done

PREDQ="$(printf '%q ' "${PRED[@]}")"              # same predicates, replayed on the box
PATHSQ="$(printf '%q ' "${PATHS[@]}")"
DIGEST="$(printf '%s\n' "${FILES[@]}" | tr '\n' '\0' | xargs -0 shasum -a 256 | LC_ALL=C sort | shasum -a 256 | cut -c1-16)"
BYTES="$(printf '%s\n' "${FILES[@]}" | tr '\n' '\0' | xargs -0 stat -f %z | awk '{s+=$1} END{print s+0}')"
printf '== %d files, %d KB raw, digest %s\n' "${#FILES[@]}" "$(( BYTES / 1024 ))" "$DIGEST"

# ── skip when the box already holds exactly this ──────────────────────────────────────────────
REMOTE_STATE="$REMOTE_DIR/.ship-digest"
HAVE="$(sshx "cat ~/$REMOTE_STATE 2>/dev/null" || true)"
if [[ "$HAVE" == "$DIGEST" && -z "${FORCE:-}" ]]; then
  echo "== box already holds $DIGEST — nothing to send (FORCE=1 to ship anyway)"; exit 0
fi
[[ -n "$HAVE" ]] && echo "   box holds $HAVE"

# zstd on both ends, else gzip: at 600 kB/s the CPU is free and the wire is not.
if command -v zstd >/dev/null 2>&1 && sshx 'command -v zstd >/dev/null 2>&1'; then
  CZ=(zstd -19 -T0 -q -c); DZ="zstd -dq -c"; CNAME=zstd
else
  CZ=(gzip -9 -c); DZ="gzip -dc"; CNAME=gzip
fi

if [[ -n "${DRY:-}" ]]; then
  printf '%s\n' "${FILES[@]}" | sed 's/^/   /'
  echo "== DRY: would send ${#FILES[@]} files ($CNAME) to $UH:~/$REMOTE_DIR/"; exit 0
fi

# Shipping onto a box whose cluster is coming up is normal — up.sh takes an hour on a fresh one —
# so this reports and continues. It is safe because of how the files land, below.
RUNNING="$(sshx "pgrep -af '[u]p[.]sh|[0]0-install-once[.]sh|[s]tart[.]sh' 2>/dev/null | head -3" || true)"
[[ -n "$RUNNING" ]] && { echo "!! running on the box (each keeps the file it was started from):";
                         printf '%s\n' "$RUNNING" | sed 's/^/   /'; }

# ── the stream ────────────────────────────────────────────────────────────────────────────────
# Extract into a staging dir, then rename each file into place — NOT a plain in-place extract.
# bash re-reads a running script at a byte offset after each child, so overwriting up.sh or
# 00-install-once.sh underneath a live run makes it resume mid-token in the new bytes. A rename
# leaves the running shell on its original inode and gives the next run the new file.
STAGE="$REMOTE_DIR/.ship-stage.$$"
T0=$(date +%s)
OUT="$(tar "${EX[@]}" -cf - "${PATHS[@]}" | "${CZ[@]}" | tee "$WIREF" | sshx "set -e
  mkdir -p ~/'$STAGE' ~/'$REMOTE_DIR'
  $DZ | tar -xf - -C ~/'$STAGE'
  cd ~/'$STAGE'
  find . -type d -exec mkdir -p ~/'$REMOTE_DIR'/{} \;
  find . -type f -exec mv -f {} ~/'$REMOTE_DIR'/{} \;
  cd ~ && rm -rf ~/'$STAGE'
  echo SHIPOK" 2>&1 | tail -1)"
[[ "$OUT" == SHIPOK ]] || { echo "ERROR: transfer failed: $OUT" >&2; exit 1; }
ELAPSED=$(( $(date +%s) - T0 ))
SENT="$(wc -c < "$WIREF" | tr -d ' ')"

# ── divergence: report always, delete only when asked ─────────────────────────────────────────
# A stale script on the box is a silent wrong answer, so the difference is always printed. It is
# NOT deleted by default: a box carries work that exists only there — this one held six files
# under cluster/tests/l2-latency/ that are in no clone — and a ship is not the moment to discover
# that. PRUNE=1 deletes, bounded by the same predicates, so run/, logs/, runs/ and the corpora are
# out of reach by construction.
if true; then
  REMOTE_LIST="$(sshx "cd ~/'$REMOTE_DIR' 2>/dev/null || exit 0; find $PATHSQ -type f $PREDQ 2>/dev/null | LC_ALL=C sort" || true)"
  STALE="$(LC_ALL=C comm -13 <(printf '%s\n' "${FILES[@]}") <(printf '%s\n' "$REMOTE_LIST") | sed '/^$/d')"
  if [[ -n "$STALE" ]]; then
    if [[ "${PRUNE:-0}" == 1 ]]; then
      printf '%s\n' "$STALE" | sed 's/^/   box-only, REMOVED: /'
      printf '%s\n' "$STALE" | sshx "cd ~/'$REMOTE_DIR' && tr '\n' '\0' | xargs -0 -r rm -f"
    else
      printf '%s\n' "$STALE" | sed 's/^/   box-only, kept: /'
      echo "   (PRUNE=1 to delete these — check first that none of it exists only there)"
    fi
  fi
fi

# ── verify by re-reading what landed ──────────────────────────────────────────────────────────
# Hashed over the SHIPPED list, not a remote find: the box holds files of its own (work done
# there, see "box-only" above), so a find-based digest would compare two different sets and fail
# every time. A truncated transfer that still exits 0 is what this catches and an exit status
# does not.
REMOTE_DIGEST="$(printf '%s\n' "${FILES[@]}" | sshx "cd ~/'$REMOTE_DIR' && tr '\n' '\0' \
  | xargs -0 sha256sum | LC_ALL=C sort | sha256sum | cut -c1-16" || true)"
if [[ "$REMOTE_DIGEST" != "$DIGEST" ]]; then
  echo "ERROR: digest mismatch after transfer (local $DIGEST, box ${REMOTE_DIGEST:-none}) — not recorded" >&2
  exit 1
fi
sshx "printf '%s' '$DIGEST' > ~/'$REMOTE_STATE'"

printf '== %d KB on the wire (%s), %ss, verified %s -> %s:~/%s/\n' \
  "$(( SENT / 1024 ))" "$CNAME" "$ELAPSED" "$DIGEST" "$UH" "$REMOTE_DIR"
