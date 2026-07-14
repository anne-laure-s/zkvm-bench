#!/usr/bin/env bash
# boot.sh — start redis + postgres. Run on EVERY instance start (processes do NOT
# survive a stop/start; the installed packages + data DO). Idempotent. Then run
# ./03-start.sh. On a fresh/destroyed instance, run ./00-install-once.sh first.
set -uo pipefail

echo "== redis =="
mkdir -p "$HOME/cluster-native/run"
# Single box / SSH-tunnel 2×8 → defaults (127.0.0.1, no auth): the worker reaches redis through
# the tunnel's localhost. DIRECT 2×8 (no tunnel) → REDIS_BIND=0.0.0.0 + REDIS_PASSWORD=<pass> so
# the remote worker can connect over the public IP without leaving redis open to the world.
REDIS_BIND="${REDIS_BIND:-127.0.0.1}"
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
RCLI=(redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"})
"${RCLI[@]}" ping >/dev/null 2>&1 || \
  redis-server --daemonize yes --bind "$REDIS_BIND" --port 6379 \
    --maxmemory 96gb --maxmemory-policy noeviction \
    ${REDIS_PASSWORD:+--requirepass "$REDIS_PASSWORD"} \
    --dir "$HOME/cluster-native/run" --logfile "$HOME/cluster-native/run/redis.log"
sleep 1
"${RCLI[@]}" ping || { echo "ERROR: redis not responding"; exit 1; }
"${RCLI[@]}" config set maxmemory 96gb >/dev/null 2>&1 || true
"${RCLI[@]}" config set maxmemory-policy noeviction >/dev/null 2>&1 || true

echo "== postgres =="
PGVER="$(ls /etc/postgresql 2>/dev/null | sort -n | tail -1)"
[[ -n "$PGVER" ]] || { echo "ERROR: postgres not installed — run ./00-install-once.sh first"; exit 1; }
pg_ctlcluster "$PGVER" main start 2>/dev/null || service postgresql start 2>/dev/null || true
for i in $(seq 1 15); do pg_isready -h localhost -p 5432 -q && break; sleep 1; done
pg_isready -h localhost -p 5432 -q || { echo "ERROR: postgres not ready on localhost:5432"; exit 1; }
# (idempotent) ensure the password matches API_DATABASE_URL
sudo -u postgres psql -tAc "ALTER USER postgres PASSWORD 'postgrespassword';" 2>/dev/null || \
  su -s /bin/sh postgres -c "psql -tAc \"ALTER USER postgres PASSWORD 'postgrespassword';\"" 2>/dev/null || true

echo "== services up. next: NUM_GPUS=16 ./03-start.sh =="
