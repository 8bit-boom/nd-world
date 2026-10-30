#!/bin/bash
# The Python-free check (docs/RUST_SWITCH_OFF.md): boot the gateway alone on a fresh data folder (ND_PYTHON_FREE=1, no upstream), sign the seeded GM in,
# and GET every parameterless route the Python app has. What answers 501 (or 502) is a route only Python serves. Needs rust/target/release/nd-gateway and
# a Python with the app's requirements (PYTHON=...) to list the routes.   usage: scripts/python_free_crawl.sh [port]
set -u
cd "$(dirname "$0")/.."
PORT=${1:-8990}; D=$(mktemp -d); PY=${PYTHON:-python}
ND_PYTHON_FREE=1 DB_PATH=$D/world.db ND_STATIC_DIR=$PWD/static ND_TEMPLATES_DIR=$PWD/app/templates ND_BUNDLED_MAPS_DIR=$PWD/app/maps \
  ND_GATEWAY_LISTEN=127.0.0.1:$PORT ND_RUST_ROUTES=login,dice_roll,live,pages,writes,ai,mcp,fans,auth GM_EMAIL=gm@example.org GM_PASSWORD=crawl-password-1 \
  ND_UPSTREAM=127.0.0.1:1 rust/target/release/nd-gateway > $D/gw.log 2>&1 &
GW=$!
trap 'kill $GW 2>/dev/null; rm -rf $D' EXIT
sleep 3
curl -s -c $D/c.txt -o /dev/null -X POST -d "email=gm@example.org&password=crawl-password-1&next=/" http://127.0.0.1:$PORT/login
DB_PATH=$D/imp.db $PY - <<'P' 2>/dev/null > $D/routes.txt
from app.main import _fastapi_app
seen = set()
for r in _fastapi_app.routes:
    if "GET" in (getattr(r, "methods", None) or ()) and "{" not in r.path and r.path not in seen:
        seen.add(r.path); print(r.path)
P
n=0; bad=0
while read -r p; do
  n=$((n+1)); code=$(curl -s -b $D/c.txt -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:$PORT$p")
  { [ "$code" = "501" ] || [ "$code" = "502" ]; } && { echo "$code $p"; bad=$((bad+1)); }
done < $D/routes.txt
echo "$bad of $n parameterless GET routes need Python"
