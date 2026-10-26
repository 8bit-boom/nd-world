#!/bin/bash
# The dev server pair in WSL: Python on 9031, the gateway on 9051, a fresh database.
export PATH=/root/.cargo/bin:$PATH
export TZ=UTC  # both servers must share a clock: Python reads naive datetimes as local time
set -e
DATA=/root/devdata
mkdir -p "$DATA/ollama-config" "$DATA/uploads" "$DATA/maps"
for d in /proc/[0-9]*; do
  exe=$(readlink -f "$d/exe" 2>/dev/null || true)
  case "$exe" in
    /root/target/release/nd-gateway|/root/target/release/nd-gateway\ \(deleted\)) kill -9 "$(basename "$d")" 2>/dev/null || true ;;
  esac
done
for d in /proc/[0-9]*; do
  if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q "tests\.[s]erve"; then
    kill -9 "$(basename "$d")" 2>/dev/null || true
  fi
done
sleep 0.5
rm -f "$DATA/world.db" "$DATA/world.db-wal" "$DATA/world.db-shm"
cd /mnt/f/GLM/nd-world
ND_TEST_DATA_DIR=$DATA ND_ACCEL_TOKEN=ci-token SECRET_KEY=test-secret-key-not-for-production \
  /root/venv/bin/python -m tests.serve --port 9031 > "$DATA/serve.log" 2>&1 &
SECRET_KEY=test-secret-key-not-for-production MAX_UPLOAD_BYTES=1048576 MAX_NOTE_IMPORT_BYTES=1048576 \
ND_ACCEL_TOKEN=ci-token OLLAMA_CONFIG_DIR=$DATA/ollama-config ND_TEMPLATES_DIR=/mnt/f/GLM/nd-world/app/templates \
DB_PATH=$DATA/world.db ND_PBKDF2_ITERATIONS=1000 ND_TEST_HOOKS=1 \
ND_RUST_ROUTES=login,dice_roll,live,pages,writes,ai,mcp,fans,auth \
ND_UPSTREAM=127.0.0.1:9031 ND_GATEWAY_LISTEN=127.0.0.1:9051 ND_STATIC_DIR=/mnt/f/GLM/nd-world/static \
ND_ACCEL_ROOT=$DATA /root/target/release/nd-gateway > "$DATA/gateway.log" 2>&1 &
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:9051/__gateway/health 2>/dev/null | grep -q '"upstream":true'; then
    echo "pair up (python 9031, gateway 9051, data $DATA)"
    exit 0
  fi
  sleep 0.5
done
echo "pair did not come up"; tail -5 "$DATA/serve.log" "$DATA/gateway.log"; exit 1
