#!/usr/bin/env bash
# t6-duty.sh — is the card computing or waiting, and what is it allowed to draw? RUNS ON THE PROVER.
#
# READ-ONLY. It starts nothing, proves nothing, allocates no GPU memory and touches no cluster state,
# so it is safe while blocks are being proved — and a live RTP run is the BEST time to run it, because
# the duty cycle it reports is then measured on the real workload instead of a synthetic one.
#
# It answers the two questions the Intel/Ryzen comparison in RTP-FINDINGS.md left open on this box:
#
#   1. What is the ENFORCED power limit, against the vendor default? A box sitting at 500 W against a
#      575 W default loses 1.15× and no listing shows it. t3-topo.sh does topology and bandwidth; it
#      never queries this field, which is why the Ryzen column of that table says "not captured".
#   2. What fraction of the time is the card actually computing? The Intel box worked 33 % of the
#      time and idled 62 %, clocked HIGHER while idle — starved by a slow return path, not compute
#      bound. Comparable numbers for this box are what turn one anomalous host into a rule.
#
# The window and interval default to the Intel run's (305 s, every 5 s) so the two tables can be read
# side by side. Changing them is fine; the shares stay valid, only the comparability goes.
#
#   bash tests/t6-duty.sh                    # sample this box for 305 s
#   WINDOW=600 INTERVAL=5 bash tests/t6-duty.sh
#   SAMPLES_CSV=<file> bash tests/t6-duty.sh # re-analyse an earlier run's samples, sample nothing
#
# What it CANNOT do during a proof: measure d2h. That needs ~1 GB per GPU and contends with the run —
# `BW=1 bash tests/t3-topo.sh` on an idle box, and BW=0 while one is in flight.
#
# Env: WINDOW=305 · INTERVAL=5 · OUT=~/tests/t6-duty-<stamp> · WORKER_LOG=<path> · SAMPLES_CSV=<file>
set -uo pipefail

WINDOW="${WINDOW:-305}"
INTERVAL="${INTERVAL:-5}"
OUT="${OUT:-$HOME/tests/t6-duty-$(date -u +%Y%m%d-%H%M%SZ)}"
# Self-locating, because the tree is not in the same place on every host: the prover has it at
# ~/zisk-infra/cluster while rtp-up's own example points inside ~/zkvm-bench/infra/. This script lives
# in <tree>/cluster/tests/, so the cluster's log dir is one level up — try that first, then the two
# absolute layouts, and only then give up.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${WORKER_LOG:-}" ]; then
  for c in "$HERE/../logs/worker.log" \
           "$HOME/zisk-infra/cluster/logs/worker.log" \
           "$HOME/zkvm-bench/infra/zisk-infra/cluster/logs/worker.log"; do
    [ -f "$c" ] || continue
    if [ -z "${WORKER_LOG:-}" ] || [ "$c" -nt "$WORKER_LOG" ]; then WORKER_LOG="$c"; fi
  done
  WORKER_LOG="${WORKER_LOG:-$HERE/../logs/worker.log}"
fi
SAMPLES_CSV="${SAMPLES_CSV:-}"

mkdir -p "$OUT"
CSV="$OUT/samples.csv"
R="$OUT/report.txt"

say() { printf '\033[1m==\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. the power limit, which is one query and answers question 1 on its own ───────────────────────
# Three distinct fields, and only their DIFFERENCE is the finding: "Enforced" is what the driver will
# actually allow right now, "Current" what is set, "Default" what the vendor shipped. A box can be
# capped by the host (a PSU-limited chassis, a vast.ai template) with nothing in its listing saying so.

power_block() {
  command -v nvidia-smi >/dev/null 2>&1 || { echo "  nvidia-smi absent — no GPU on this host"; return; }
  # -F: and the value taken from the RIGHT of the colon: with the default field separator, $2 on
  # "Current Power Limit   : 600.00 W" is the word "Power". And there is NO "Enforced Power Limit" line
  # in `-q -d POWER` on driver 580, so keying the print off one printed nothing at all. Stop at
  # "Module Power Readings", which repeats every label with N/A and would overwrite the real values.
  nvidia-smi -q -d POWER 2>/dev/null | awk -F: '
    /Module Power Readings/ { skip = 1 }
    skip { next }
    NF >= 2 { k = $1; v = $2
              gsub(/^[ \t]+|[ \t]+$/, "", k); gsub(/^[ \t]+|[ \t]+$/, "", v)
              val[k] = v }
    END {
      cur = val["Current Power Limit"]; def = val["Default Power Limit"]
      printf "  limit    current %-10s default %-10s (min %s, max %s)\n",
             cur, def, val["Min Power Limit"], val["Max Power Limit"]
      printf "  draw     average %-10s instantaneous %s\n",
             val["Average Power Draw"], val["Instantaneous Power Draw"]
      if (val["Number of Samples"] != "")
        printf "  samples  %s over %s: min %s, avg %s, max %s\n",
               val["Number of Samples"], val["Duration"], val["Min"], val["Avg"], val["Max"]
      # Average Power Draw reads N/A on some driver/card pairs (seen on 580 + RTX 5090); the Power
      # Samples average is the same quantity over a couple of seconds, so fall back to it rather than
      # dropping the verdict silently.
      c = cur + 0; d = def + 0
      a = val["Average Power Draw"] + 0
      src = "average draw"
      if (a == 0 && val["Avg"] != "") { a = val["Avg"] + 0; src = "sampled avg over " val["Duration"] }
      if (c > 0 && d > 0 && c < d)
        printf "  -> CAPPED: enforced %.0f W against a %.0f W default = %.2fx of the power lost\n",
               c, d, d / c
      else if (c > 0 && d > 0)
        printf "  -> not capped: current equals the %.0f W default\n", d
      # Scoped to the instant of the query on purpose: this is a seconds-long reading, so it says
      # what the card is doing NOW, not over any window. The duty-cycle table below is the window.
      if (c > 0 && a >= 0.97 * c)
        printf "  -> at this instant, pinned at the limit: %.0f W of %.0f W (%s)\n", a, c, src
      else if (c > 0 && a > 0)
        printf "  -> at this instant, %.0f W of %.0f W (%.0f%%, %s)\n", a, c, 100 * a / c, src
    }'
  echo
  echo "  per-GPU, as the driver reports it:"
  # A field this driver does not know makes nvidia-smi return NOTHING for the whole query, so the line
  # would vanish silently rather than degrade. enforced.power.limit is the one not on every driver.
  local q="index,name,power.limit,power.default_limit,power.max_limit,enforced.power.limit"
  local out; out="$(nvidia-smi --query-gpu="$q" --format=csv 2>/dev/null || true)"
  if [ -n "$out" ]; then printf '%s\n' "$out" | sed 's/^/    /'
  else
    echo "    (driver rejected one of: $q — reduced set)"
    nvidia-smi --query-gpu=index,name,power.limit,power.default_limit --format=csv 2>/dev/null \
      | sed 's/^/    /'
  fi
}

# Sourced rather than executed: the caller wants power_block and nothing else. up.sh takes it, so the
# enforced limit lands in its health line instead of being a field nobody queries — which is how a box
# capped at 450 W against a 575 W default went a whole campaign unnoticed. Everything below samples for
# WINDOW seconds and is only meaningful UNDER LOAD, so it must not run at bring-up: an idle box would
# report a duty cycle of zero, which reads like an answer and is not one.
(return 0 2>/dev/null) && return 0

# ── 2. sample, or re-analyse ───────────────────────────────────────────────────────────────────────

if [ -n "$SAMPLES_CSV" ]; then
  [ -f "$SAMPLES_CSV" ] || die "SAMPLES_CSV=$SAMPLES_CSV does not exist"
  CSV="$SAMPLES_CSV"
  say "re-analysing $CSV — sampling nothing"
  # The window is IN the file: first and last sample. Without this the phase and in-flight sections
  # are skipped, which defeats the point of re-analysing an existing run against the correct log.
  T_START="$(awk -F, 'NR==2{print $1; exit}' "$CSV")"
  T_END="$(awk -F, 'END{print $1}' "$CSV")"
  [ -n "$T_START" ] && say "window from the file: $T_START -> $T_END"
else
  command -v nvidia-smi >/dev/null 2>&1 \
    || die "nvidia-smi is absent — this samples a GPU, so it only runs on the prover. \
To analyse samples taken elsewhere: SAMPLES_CSV=<file> bash tests/t6-duty.sh"
  say "power limits, before anything else"
  power_block | tee "$OUT/power.txt"

  # Integer-only: $(( WINDOW / INTERVAL )) dies with a bash arithmetic error on INTERVAL=0.2, before
  # any guard below can explain why. Sub-second sampling is `nvidia-smi -lms`, not this script.
  case "$WINDOW$INTERVAL" in *[!0-9]*) die "WINDOW and INTERVAL must be whole seconds (got \
WINDOW=$WINDOW INTERVAL=$INTERVAL). For sub-second sampling use: nvidia-smi -lms 200 --query-gpu=..." ;; esac
  N=$(( WINDOW / INTERVAL ))
  [ "$N" -ge 2 ] || die "WINDOW=$WINDOW at INTERVAL=$INTERVAL gives $N samples — raise WINDOW"
  say "sampling every ${INTERVAL}s for ${WINDOW}s ($N samples). READ-ONLY; a proof in flight is fine."
  T_START="$(date -u +%Y-%m-%dT%H:%M:%S)"
  echo "iso,gpu,util_pct,mem_util_pct,power_w,clock_sm_mhz,temp_c" > "$CSV"
  i=0; MISSED=0
  while [ "$i" -lt "$N" ]; do
    ts="$(date -u +%Y-%m-%dT%H:%M:%S)"
    snap="$(nvidia-smi --query-gpu=index,utilization.gpu,utilization.memory,power.draw,clocks.sm,temperature.gpu \
              --format=csv,noheader,nounits 2>/dev/null | sed 's/, */,/g')"
    i=$(( i + 1 ))
    if [ -z "$snap" ]; then
      # Counted, not silently written: a bare timestamp row would be dropped by the analysis for
      # having too few fields, and the share would then be computed over a smaller n with no trace.
      MISSED=$(( MISSED + 1 ))
    else
      printf '%s\n' "$snap" | sed "s/^/$ts,/" >> "$CSV"
    fi
    # Progress is not decoration. 305 s of silence is indistinguishable from a hang, and the first
    # thing anyone does with a hung script is kill it — losing the window.
    line="$(printf '   [%2d/%d] %4ds left   %s' "$i" "$N" "$(( (N - i) * INTERVAL ))" \
      "$(printf '%s' "$snap" | awk -F, '{printf "gpu%s util %s%% %sW %sMHz   ", $1, $2, $4, $5}')")"
    # One updating line on a terminal; every tenth sample on its own line when piped or logged, where
    # a carriage return would collapse the whole run into one unreadable line.
    if [ -t 1 ]; then printf '\r%s' "$line"
    elif [ $(( i % 10 )) = 0 ] || [ "$i" = "$N" ]; then printf '%s\n' "$line"; fi
    [ "$i" -lt "$N" ] && sleep "$INTERVAL"
  done
  printf '\n'
  T_END="$(date -u +%Y-%m-%dT%H:%M:%S)"
  [ "$MISSED" = 0 ] || warn "$MISSED of $N samples returned nothing from nvidia-smi and were not written"
  say "samples in $CSV"
fi

# ── 3. the bands, in the same shape as the Intel table so the two can be read side by side ─────────
# Three bands, and the middle one is PRINTED even when it is small: the Intel table showed 33 % and
# 62 % and nothing else, so the shares did not sum to 100 and no reader could tell whether samples
# had been dropped or the arithmetic was wrong. Every sample lands in exactly one row here.

say "duty cycle"
awk -F, 'NR>1 && NF>=7 {
    g=$2+0; n[g]++
    u=$3+0; p=$5+0; c=$6+0; m=$4+0
    if (u >= 50)      { b="work"; }
    else if (u >= 10) { b="mid";  }
    else              { b="idle"; }
    cnt[g,b]++; pw[g,b]+=p; ck[g,b]+=c
    if (m > memmax[g]) memmax[g]=m
  }
  END {
    if (!length(n)) { print "  no usable samples in the CSV"; exit }
    printf "  %-4s %-22s %8s %9s %11s %11s\n", "gpu", "utilisation at sample", "samples", "share", "mean power", "mean clock"
    for (g in n) {
      split("work mid idle", ord, " ")
      lbl["work"]=">= 50 %: working"; lbl["mid"]="10-50 %: in between"; lbl["idle"]="< 10 %: idle, waiting"
      for (k=1; k<=3; k++) {
        b=ord[k]; c0=cnt[g,b]+0
        if (c0 == 0) { printf "  %-4s %-22s %8d %8.0f%% %11s %11s\n", g, lbl[b], 0, 0, "-", "-"; continue }
        printf "  %-4s %-22s %8d %8.0f%% %10.0fW %9.0fMHz\n", g, lbl[b], c0, 100*c0/n[g], pw[g,b]/c0, ck[g,b]/c0
      }
      printf "  %-4s %-22s %8d %8.0f%%   peak memory utilisation %.0f%%\n", g, "ALL", n[g], 100, memmax[g]
      w=cnt[g,"work"]+0
      if (w > 0) {
        printf "  -> gpu %s computes %.0f%% of the window; 1/%.2f = %.2fx lost to waiting\n", \
               g, 100*w/n[g], w/n[g], n[g]/w
        print  "     NOTE: over a whole window this measures the CADENCE and the card together. On an"
        print  "     RTP prover, one ~23 s proof every ~60 s is ~39 % busy however well the GPU is fed,"
        print  "     so read the in-flight figure below before concluding anything about starvation."
      }
      else       printf "  -> gpu %s never reached 50%% — nothing was proving in this window\n", g
      print ""
    }
  }' "$CSV" | tee "$R"

# ── 4. what the worker was doing in that window, so the duty cycle is attributable ────────────────
# A duty cycle with no phase beside it is unattributable: 62 % idle means one thing during
# GENERATING_INNER_PROOFS and another between two blocks with nothing to prove.

mtime_of() { stat -c %y "$1" 2>/dev/null || stat -f %Sm "$1" 2>/dev/null || ls -l "$1"; }

if [ -f "$WORKER_LOG" ] && [ -n "$T_START" ]; then
  say "worker phases inside the window ($T_START -> $T_END)"
  PH="$(awk -v a="$T_START" -v b="$T_END" '
    $1 >= a && $1 <= b && /EXECUTE|CALCULATING_CONTRIBUTIONS|GENERATING_INNER_PROOFS|Total weight/ { print "  " $0 }
  ' "$WORKER_LOG" | tail -40)"
  if [ -n "$PH" ]; then
    # To the report, not to the screen: forty raw log lines push the actual result off the top of a
    # terminal that cannot scroll back, and step 5's per-proof table says the same thing in four rows.
    printf '%s\n' "$PH" >> "$R"
    echo "   $(printf '%s\n' "$PH" | grep -c 'GENERATING_INNER_PROOFS') GPU-phase lines, \
$(printf '%s\n' "$PH" | grep -c 'Total weight \[Process') weights — full lines appended to the report"
  else
    # An empty section is NOT "the worker was quiet": it is far more often the wrong file. Say which
    # file and how old, because a duty cycle nobody can attribute to a phase cannot tell inter-block
    # idleness (the pipeline had nothing queued) from starvation (a proof was running and waiting).
    warn "0 phase lines fell inside the window."
    warn "  file : $WORKER_LOG"
    warn "  mtime: $(mtime_of "$WORKER_LOG")"
    warn "  If that timestamp is not from this window, this is a stale or wrong log — the duty cycle"
    warn "  above stands, but it is UNATTRIBUTED. Re-run with WORKER_LOG=<the log the prover writes>."
  fi
elif [ -n "$T_START" ]; then
  warn "no worker log at $WORKER_LOG — the duty cycle above is not attributed to a phase."
  warn "Pass WORKER_LOG=<path> if the prover keeps it elsewhere."
fi

# ── 5. per proof, and the duty cycle DURING one — the only figure comparable across boxes ─────────
# A whole-window duty cycle on an RTP prover measures the CADENCE: one ~23 s proof every ~60 s is 39 %
# busy however well the GPU is fed. The comparable figure is the duty restricted to GENERATING_INNER_
# PROOFS, the GPU phase (85 % of a proof on the Intel box). No RTP pause is needed to get it.
#
# The worker brackets every phase with >>> on entry and <<< on exit, so the interval is read straight
# off two lines. An earlier version derived the start by subtracting the reported duration with
# `date -d "<iso> -N seconds"`, which GNU date parses as a TIMEZONE offset: the intervals landed 17 h
# away, non-empty and matching nothing. Pairing the markers needs no arithmetic and cannot drift.

if [ -f "$WORKER_LOG" ] && [ -n "$T_START" ] && [ -n "${PH:-}" ]; then

  say "per proof, from the worker log"
  awk -v a="$T_START" -v b="$T_END" '
    { t = substr($1, 1, 19) }
    t < a || t > b { next }
    /Total weight \[Process/            { w = $NF + 0 }
    /<<< EXECUTE \(/                    { if (match($0, /\(([0-9]+)ms\)/)) ex = substr($0, RSTART+1, RLENGTH-4) + 0 }
    /<<< CALCULATING_CONTRIBUTIONS \(/  { if (match($0, /\(([0-9]+)ms\)/)) cc = substr($0, RSTART+1, RLENGTH-4) + 0 }
    /<<< GENERATING_INNER_PROOFS \(/ {
      if (match($0, /\(([0-9]+)ms\)/)) ip = substr($0, RSTART+1, RLENGTH-4) + 0
      if (w > 0) {
        n++; sw += w; sip += ip
        if (!hdr++) printf "  %16s %8s %9s %9s %10s\n", "trace weight", "EXECUTE", "CONTRIB", "INNER", "ms/1e9"
        printf "  %16d %7.3fs %8.3fs %8.3fs %10.1f\n", w, ex/1000, cc/1000, ip/1000, ip/(w/1e9)
      }
      w = 0; ex = 0; cc = 0; ip = 0
    }
    END {
      if (!n) { print "  no complete proof inside the window"; exit }
      printf "  %d complete proof(s); INNER mean %.1f ms per 1e9 of trace weight\n", n, sip/(sw/1e9)
      print  "  Intel, block 25552376: 1185.5 ms/1e9 INNER, 203.5 CONTRIB, 7.5 EXECUTE"
    }' "$WORKER_LOG" | tee -a "$R"

  IV="$OUT/inflight.csv"
  awk -v a="$T_START" -v b="$T_END" '
    { t = substr($1, 1, 19) }
    t >= a && t <= b {
      if ($0 ~ />>> GENERATING_INNER_PROOFS/) st = t
      else if ($0 ~ /<<< GENERATING_INNER_PROOFS/ && st != "") { print st "," t; st = "" }
    }' "$WORKER_LOG" > "$IV"

  if [ -s "$IV" ]; then
    say "duty cycle inside GENERATING_INNER_PROOFS (the cross-box comparable figure)"
    awk -F, -v ivf="$IV" '
      BEGIN { while ((getline l < ivf) > 0) { split(l, q, ","); m++; s[m] = q[1]; e[m] = q[2] } }
      NR > 1 && NF >= 7 {
        inside = 0
        for (j = 1; j <= m; j++) if ($1 >= s[j] && $1 <= e[j]) { inside = 1; break }
        if (!inside) { outside++; next }
        n++; u = $3 + 0
        if (u >= 50) { w++; pw += $5; ck += $6 } else if (u >= 10) mid++; else { id++; ipw += $5; ick += $6 }
      }
      END {
        if (n == 0) { print "  no sample fell inside a GPU phase — the phases are short, raise WINDOW"; exit }
        printf "  %d of %d samples fell inside a GPU phase (%d elsewhere), over %d interval(s)\n",
               n, n + outside, outside, m
        printf "  working %3d (%3.0f%%)", w, 100*w/n; if (w) printf "   %.0fW  %.0fMHz", pw/w, ck/w
        printf "\n  mid     %3d (%3.0f%%)\n", mid, 100*mid/n
        printf "  idle    %3d (%3.0f%%)", id, 100*id/n; if (id) printf "   %.0fW  %.0fMHz", ipw/id, ick/id
        printf "\n"
        if (w) printf "  -> %.0f%% duty while the GPU had work queued (Intel: 33%%, idle band 2726 MHz)\n",
                      100*w/n
      }' "$CSV" | tee -a "$R"
  else
    warn "no >>>/<<< GENERATING_INNER_PROOFS pair inside the window in $WORKER_LOG"
  fi
fi

say "report: $R"
echo "   Next, for the numbers this cannot take while a proof is in flight:"
echo "     BW=1 bash tests/t3-topo.sh    # d2h — needs an idle card, it allocates ~1 GB per GPU"
