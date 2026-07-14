#!/usr/bin/env bash
# fetch-runs.sh — repatriate a remote dir (cluster run records / logs) to your Mac in ONE pass:
# compress on the box → scp the archive → verify checksum → extract locally → clean up.
# Generic run-record repatriation (defaults target the ZisK cluster).
# Run from anywhere ON YOUR MAC.
#
#   ./cluster/fetch-runs.sh user@host:port [REMOTE_DIR] [LOCAL_DEST]
#   ./cluster/fetch-runs.sh root@ssh.vast.ai:<port>                         # uses defaults
#
# Defaults:  REMOTE_DIR=zisk-infra/cluster/runs (relative to the remote $HOME, or pass an absolute path)
#            LOCAL_DEST=<zisk-infra>/results   (created/merged — run records have unique names)
# Env:       KEEP_REMOTE=1  keep the remote source dir (only the remote archive is removed)
#            COMPRESS=zstd  faster/smaller than gzip, but needs `zstd` on BOTH ends
set -uo pipefail

EP="${1:?usage: $0 user@host:port [REMOTE_DIR] [LOCAL_DEST]}"
REMOTE_DIR="${2:-zisk-infra/cluster/runs}"
LOCAL_DEST="${3:-$(cd "$(dirname "$0")/.." && pwd)/results}"
KEEP_REMOTE="${KEEP_REMOTE:-}"
COMPRESS="${COMPRESS:-gzip}"

case "$REMOTE_DIR" in
  ""|"."|"./"|".."|"/"|"~"|"~/"|*..*) echo "ERROR: unsafe REMOTE_DIR '$REMOTE_DIR' — pass a real subdir like zisk-infra/cluster/runs" >&2; exit 2 ;;
esac

PORT="${EP##*:}"; UH="${EP%:*}"
[[ "$PORT" != "$EP" && "$PORT" =~ ^[0-9]+$ ]] || { echo "ERROR: pass user@host:port (e.g. root@1.2.3.4:<port>)" >&2; exit 2; }
OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25)
ssh_() { ssh -p "$PORT" "${OPTS[@]}" "$UH" "$@"; }
sha_() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }

case "$COMPRESS" in
  gzip) C_CREATE='-czf'; C_EXTRACT=(-xzf); EXT='tar.gz' ;;
  zstd) C_CREATE='--use-compress-program=zstd -cf'; C_EXTRACT=(--use-compress-program=zstd -xf); EXT='tar.zst'
        command -v zstd >/dev/null 2>&1 || { echo "ERROR: COMPRESS=zstd but zstd not on this Mac (brew install zstd)" >&2; exit 2; } ;;
  *) echo "ERROR: COMPRESS must be gzip or zstd" >&2; exit 2 ;;
esac

arch="repatriate-$(date -u +%Y%m%d-%H%M%SZ).$EXT"
local_arch="$(mktemp -u -t fetch-runs).$EXT"

echo "== 1/4 compress remote '$REMOTE_DIR' → ~/$arch =="
out="$(ssh_ "
  set -e
  dir='$REMOTE_DIR'; case \"\$dir\" in /*) ;; *) dir=\"\$HOME/\$dir\";; esac
  [ -d \"\$dir\" ] || { echo \"ERROR: remote dir not found: \$dir\" >&2; exit 3; }
  tar $C_CREATE \"\$HOME/$arch\" -C \"\$dir\" .
  echo \"DIR=\$dir\"
  echo \"SHA=\$( if command -v sha256sum >/dev/null 2>&1; then sha256sum \"\$HOME/$arch\"; else shasum -a 256 \"\$HOME/$arch\"; fi | awk '{print \$1}' )\"
  du -h \"\$HOME/$arch\" | cut -f1 | sed 's/^/archive size: /' >&2
")" || { echo "ERROR: remote compression failed (dir missing? disk full?)" >&2; exit 1; }
remote_dir_abs="$(printf '%s\n' "$out" | sed -n 's/^DIR=//p')"
remote_sha="$(printf '%s\n' "$out" | sed -n 's/^SHA=//p')"
[[ -n "$remote_sha" ]] || { echo "ERROR: no checksum from remote — aborting" >&2; ssh_ "rm -f \"\$HOME/$arch\""; exit 1; }

echo "== 2/4 transfer + verify checksum =="
scp -P "$PORT" "${OPTS[@]}" "$UH:$arch" "$local_arch" >/dev/null 2>&1 \
  || { echo "ERROR: scp failed — remote archive kept at ~/$arch for retry" >&2; rm -f "$local_arch"; exit 1; }
local_sha="$(sha_ "$local_arch")"
if [[ "$local_sha" != "$remote_sha" ]]; then
  echo "ERROR: checksum mismatch (remote=$remote_sha local=$local_sha) — NOTHING deleted" >&2
  rm -f "$local_arch"; exit 1
fi
echo "  ok ($remote_sha)"

echo "== 3/4 extract → $LOCAL_DEST =="
mkdir -p "$LOCAL_DEST"
tar "${C_EXTRACT[@]}" "$local_arch" -C "$LOCAL_DEST" \
  || { echo "ERROR: extract failed — local archive kept ($local_arch), remote untouched" >&2; exit 1; }
[[ -n "$(ls -A "$LOCAL_DEST" 2>/dev/null)" ]] \
  || { echo "ERROR: extraction produced an empty $LOCAL_DEST — keeping remote, keeping $local_arch" >&2; exit 1; }
rm -f "$local_arch"

echo "== 4/4 clean up remote =="
if [[ -z "$KEEP_REMOTE" ]]; then
  echo "  $(ssh_ "
    dir='$remote_dir_abs'
    case \"\$dir\" in
      \"\$HOME\"/*/*|/*/*/*) rm -rf \"\$dir\" && echo \"removed source dir: \$dir\" || echo \"WARN: failed to remove \$dir\" ;;
      *) echo \"WARN: refusing to delete shallow path: \$dir (kept)\" ;;
    esac
    rm -f \"\$HOME/$arch\"
  ")"
else
  ssh_ "rm -f \"\$HOME/$arch\""
  echo "  removed remote archive; kept remote source (KEEP_REMOTE=1)"
fi

echo "== done: $LOCAL_DEST =="
ls -la "$LOCAL_DEST" | head -20
