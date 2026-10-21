#!/bin/bash
# Starts nd-world. With the Rust gateway (the default, docs/RUST_GATEWAY.md) two processes run in the container: the gateway on the public port
# (8000) and the Python app on 127.0.0.1:8001 behind it. ND_GATEWAY=0 runs the Python app alone on 8000, exactly as before the gateway existed.
set -u
PY=(uvicorn app.main:app --timeout-graceful-shutdown 10 --no-access-log)

if [ "${ND_GATEWAY:-1}" = "0" ] || [ ! -x /usr/local/bin/nd-gateway ]; then
  exec "${PY[@]}" --host 0.0.0.0 --port 8000
fi

# Shared secret: Python only hands a file to the gateway (X-Nd-Accel-Path) when the request carries it. Never leaves the container.
ND_ACCEL_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ND_ACCEL_TOKEN
export ND_UPSTREAM="127.0.0.1:8001"
export ND_GATEWAY_LISTEN="${ND_GATEWAY_LISTEN:-0.0.0.0:8000}"
export ND_STATIC_DIR="${ND_STATIC_DIR:-/app/static}"
export ND_ACCEL_ROOT="${ND_ACCEL_ROOT:-$(dirname "${DB_PATH:-/data/world.db}")}"

# The gateway is the only thing talking to Python, from 127.0.0.1: uvicorn takes the client's address from the gateway's X-Forwarded-For.
"${PY[@]}" --host 127.0.0.1 --port 8001 --proxy-headers --forwarded-allow-ips 127.0.0.1 &
PY_PID=$!
/usr/local/bin/nd-gateway &
GW_PID=$!

stop() {
  kill -TERM "$GW_PID" "$PY_PID" 2>/dev/null
}
trap stop TERM INT

# when either process ends, stop the other and report the first one's status
wait -n "$PY_PID" "$GW_PID"
STATUS=$?
stop
wait
exit "$STATUS"
