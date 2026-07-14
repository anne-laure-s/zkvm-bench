#!/usr/bin/env bash
# Live dashboard for a ZisK setup/prove running on this box.
#   bash ~/watch.sh [logfile]     (default: newest ~/prove*.log / ~/setup*.log / ~/install*.log)
# Ctrl-C quits the dashboard only — it does NOT stop the detached job.
LOG="${1:-}"
if [ -z "$LOG" ]; then
  LOG="$(ls -t "$HOME"/prove*.log "$HOME"/setup*.log "$HOME"/install*.log 2>/dev/null | head -1)"
fi
PIDF="${LOG%.log}.pid"
while true; do
  clear
  echo "======== ZisK live — $(date -u +%H:%M:%S) UTC ========"
  echo "log: $LOG"
  RUNNING=0
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; then
    RUNNING=1
    echo "STATUS : ● RUNNING (pid $(cat "$PIDF"), etime $(ps -p "$(cat "$PIDF")" -o etime= 2>/dev/null | tr -d ' '))"
  elif grep -qa '__EXIT=0__' "$LOG" 2>/dev/null; then
    echo "STATUS : ✅ DONE (exit 0)"
  elif grep -qa '__EXIT=' "$LOG" 2>/dev/null; then
    echo "STATUS : ❌ FAILED ($(grep -ao '__EXIT=[0-9]*__' "$LOG" | tail -1))"
  else
    echo "STATUS : ○ stopped / unknown"
  fi

  # Phase heuristic — makes the silent OpenMPI init phase legible.
  if [ "$RUNNING" = 1 ]; then
    if grep -qa 'STARTING_ASM_MICROSERVICES' "$LOG" 2>/dev/null && ! grep -qa 'Shutting down stdio' "$LOG" 2>/dev/null; then
      echo "PHASE  : 🔧 ASM microservices / proving"
    elif grep -qa 'Creating proof context' "$LOG" 2>/dev/null; then
      echo "PHASE  : ⚙️  proofman init / proving"
    else
      echo "PHASE  : ⏳ OpenMPI MPI_Init — SILENT ~4 min, GPU idle, this is NORMAL (not a hang)"
    fi
  fi

  echo "ROM asm cache : $(du -sh "$HOME/.zisk/cache" 2>/dev/null | cut -f1)"
  echo "disk /        : $(df -h / | awk 'NR==2{print $3" used, "$4" free ("$5")"}')"
  echo "--- GPUs (util% / mem) — lights up during proving ---"
  nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader 2>/dev/null \
    | awk -F, '{printf "  gpu%s:%s%s ", $1, $2, ($1%4==3?"\n":"")}'; echo
  echo "--- phases (>>> start / <<< end+ms) ---"
  tr -d '\r' < "$LOG" 2>/dev/null | grep -aE '>>>|<<<|ROM SETUP|Verif|VALID|proof|Saved|Error|ERROR|done \(local|__EXIT|^START|^END' | tail -8
  echo "-------------------------------------------------"
  echo "(refreshes every 3s — Ctrl-C does NOT stop the job)"
  sleep 3
done
