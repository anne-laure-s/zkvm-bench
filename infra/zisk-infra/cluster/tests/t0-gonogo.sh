#!/usr/bin/env bash
# t0-gonogo.sh — run this on a FRESHLY RENTED box, BEFORE installing anything.
#
# Why it exists: vast.ai listings do not tell you the one thing t1 depends on — how the GPUs are
# split across NUMA nodes. You find out after `00-install-once.sh`, which is a 30-60 minute, 30 GB
# commitment. This probe answers it in two seconds, so a wrong box costs cents instead of an hour.
#
# Self-contained ON PURPOSE: it sources nothing and needs no files shipped. Either scp this one file,
# or paste its body into the instance's shell the moment it boots.
#
#   bash t0-gonogo.sh
#
# It prints a verdict per test: the GPUS= value t1 can actually use on this box, and whether t4's
# locked arm / t5's mpi-numa arm are reachable at all (they need privileges vast.ai does not grant).
set -u

hr() { printf '\n──── %s ────\n' "$1"; }
N=0; NODES=""; MINUS_ONE=0

hr "GPUs"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L 2>/dev/null | sed 's/^/  /'
  N="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')"
  nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | sed 's/^/  driver /'
else
  echo "  ✗ no nvidia-smi — this is not a GPU box"
fi
echo "  count: $N"

# ── GPU → NUMA node, from sysfs (nothing else exposes it) ─────────────────────────────────────
hr "GPU → NUMA node   (THE criterion vast.ai does not show you)"
gpu_nodes() {
  nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader 2>/dev/null | while IFS=, read -r i b; do
    i="${i// /}"; b="${b// /}"
    local rest; rest="$(printf '%s' "${b#*:}" | tr 'A-Z' 'a-z')"   # 8-digit domain → 4-digit sysfs
    local d="/sys/bus/pci/devices/0000:$rest"
    [[ -r "$d/numa_node" ]] || d="$(echo /sys/bus/pci/devices/*:"$rest" | awk '{print $1}')"
    printf '%s %s\n' "$i" "$( [[ -r "$d/numa_node" ]] && cat "$d/numa_node" || echo '?' )"
  done
}
GN="$(gpu_nodes)"
[[ -n "$GN" ]] && awk '{printf "  gpu%-3s node=%s\n", $1, $2}' <<<"$GN"
NODES="$(awk '$2!="?"{print $2}' <<<"$GN" | sort | uniq -c | awk '{print $2":"$1}')"
echo "  → per node: ${NODES:-none}"
grep -q ' -1$' <<<"$GN" && MINUS_ONE=1

hr "CPU / sockets"
lscpu 2>/dev/null | grep -iE '^(Model name|Socket\(s\)|NUMA node\(s\)|Core\(s\) per socket|CPU\(s\)|CPU max MHz):' | sed 's/^/  /' \
  || echo "  (no lscpu)"
echo "  # A 16-GPU box is essentially always dual-socket: single-socket EPYC has 128 PCIe lanes,"
echo "  # which is 8 GPUs at x16. So asking for 16 GPUs also selects for 2 NUMA nodes."
echo "  # CPU CLOCK matters more than core count here: our asm trace runs at 790 MHz vs 1.5 GHz"
echo "  # advertised, and that phase is single-threaded."

hr "PCIe — link width MAX is a hard bandwidth ceiling"
nvidia-smi --query-gpu=index,pcie.link.gen.max,pcie.link.width.max --format=csv,noheader 2>/dev/null \
  | sed 's/^/  /' || echo "  (unavailable)"
echo "  # width max < 16 → host↔device transfers are capped for every proof, forever."
nvidia-smi topo -m 2>/dev/null | head -25 | sed 's/^/  /' || true
echo "  # many PIX/PXB pairs = GPUs behind shared PCIe switches (oversubscribed uplink)."

hr "memlock — decides whether t4's locked arm and t5's mpi-numa arm can run at all"
echo "  ulimit -l = $(ulimit -l) KB"
LOCKABLE=0
if ( ulimit -l unlimited 2>/dev/null ); then echo "  raisable  : YES — privileged enough, t4/t5 get their second arm"; LOCKABLE=1
else echo "  raisable  : NO  — expected on vast.ai (memlock hard-capped, no CAP_SYS_RESOURCE)"; fi

hr "space"
df -h / 2>/dev/null | awk 'NR==2{print "  disk /   : "$4" free"}'
free -g 2>/dev/null | awk 'NR==2{print "  RAM      : "$2" GB"}'
df -h /dev/shm 2>/dev/null | awk 'NR==2{print "  /dev/shm : "$2}'
DISK="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"

# ── verdict ───────────────────────────────────────────────────────────────────────────────────
# t1 needs BOTH a local set (n GPUs on one node) and a split set (n/2 per node), at the SAME n.
# So the usable n is min(largest node, 2 × smallest node) — 8+8 gives 8, 4+4 gives only 4.
hr "VERDICT"
if [[ "$N" -lt 2 || -z "$NODES" ]]; then
  echo "  t1  NO-GO — no usable GPU/NUMA information."
elif [[ "$MINUS_ONE" == 1 ]]; then
  echo "  t1  NO-GO — some GPUs report numa_node = -1: the kernel exposes no affinity here"
  echo "              (NUMA off in BIOS, or the container hides it). A 'split' arm built from"
  echo "              unknown-affinity GPUs is not a cross-socket set. DESTROY THIS INSTANCE."
else
  MAXN="$(awk -F: '{if($2>m) m=$2} END{print m+0}' <<<"$(tr ' ' '\n' <<<"$NODES")")"
  MINN="$(awk -F: 'BEGIN{m=1e9} {if($2<m) m=$2} END{print m+0}' <<<"$(tr ' ' '\n' <<<"$NODES")")"
  NNODES="$(tr ' ' '\n' <<<"$NODES" | grep -c .)"
  if [[ "$NNODES" -lt 2 ]]; then
    echo "  t1  NO-GO — a single NUMA node: no cross-socket arm can be built, so the [ALARM]"
    echo "              cannot be reproduced or disproved here. Look for a 16-GPU listing."
  else
    T1N=$(( MAXN < 2*MINN ? MAXN : 2*MINN ))
    T1N=$(( T1N / 2 * 2 ))     # even, so the split arm divides
    echo "  t1  GO    — run it as:  GPUS=$T1N bash tests/t1-numa.sh"
    [[ "$T1N" -ge 8 ]] \
      && echo "              ($T1N is also the shipping config → this run doubles as the 8-GPU bring-up)" \
      || echo "              (only $T1N — answers NUMA, but BELOW the 8-GPU config you want to ship;"
    [[ "$T1N" -ge 8 ]] || echo "               a 16-GPU box in 8+8 would give you both at once)"
    echo "  t2  GO    — GPUS=$T1N bash tests/t2-tune.sh"
  fi
fi
[[ "$LOCKABLE" == 1 ]] \
  && echo "  t4  GO    — both arms (this box can lock memory)" \
  || echo "  t4  PARTIAL — unlocked arm only; the locked arm needs a privileged/bare-metal host."
[[ "$LOCKABLE" == 1 ]] \
  && echo "  t5  GO    — mpi-numa can be attempted" \
  || echo "  t5  PARTIAL — mpi-numa will fail 'failed to bind memory'. Run it anyway: that failure,"
[[ "$LOCKABLE" == 1 ]] || echo "               recorded, IS the evidence that the 16-GPU ALARM is structural on vast.ai."
[[ -n "${DISK:-}" && "$DISK" -ge 64 ]] \
  && echo "  disk GO   — ${DISK} GB free" \
  || echo "  disk ⚠️   — ${DISK:-?} GB free; ZisK 1.3.1-alpha's key is 21 GB + a per-ELF ROM cache. Want ≥64."
echo
echo "  t3-topo.sh records all of the above properly, and also runs post-install."
