#!/bin/bash
# The whole test suite over HTTP through the Rust gateway (docs/RUST_GATEWAY.md), split into shards that run side by side.
#
#   scripts/gateway_suite.sh [shards] [group] [test paths / pytest args...]
#
# Test paths given replace the default (all of tests/).
# shards: how many parts the suite is split into (pytest-split, balanced by the recorded times in .test_durations); default 4.
# group:  run only this part (1..shards) - what a CI matrix job does; "all" (the default) runs every part at once on this machine.
# Each part gets its own data folder (tests/_data/shard<N>: database, uploads, maps), its own Python app (tests/serve.py) and its own
# gateway, on ports 8960+N / 8980+N, so no two parts share a database. Needs rust/target/release/nd-gateway (cargo build --release).
# STORE_DURATIONS=1: each part records its tests' times in tests/_data/shard<N>/durations.json (scripts/merge_durations.py makes
# .test_durations from them). Exit status: 0 only when every part passed. Logs: tests/_data/shard<N>/{pytest,serve,gateway}.log
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD
SHARDS=${1:-4}
GROUP=${2:-all}
shift $(( $# > 2 ? 2 : $# ))
PY=${PYTHON:-python}
GW=$ROOT/rust/target/release/nd-gateway
[ -x "$GW" ] || { echo "no gateway binary at $GW - run: (cd rust && cargo build --release)"; exit 2; }

run_shard() {
    local n=$1; shift
    local data=$ROOT/tests/_data/shard$n
    local py_port=$((8960 + n)) gw_port=$((8980 + n))
    mkdir -p "$data"
    # an image model name for both servers: with a Studio key set (tests/test_gateway_ai.py), image jobs run on Unsloth Studio
    export UNSLOTH_IMAGE_MODEL=${UNSLOTH_IMAGE_MODEL-nd-test/image-GGUF}
    # one fake fan-chip tree both servers read (tests/test_gateway_fans.py builds it)
    export FAN_HWMON_ROOT=$data/hwmon
    ND_TEST_DATA_DIR=$data ND_ACCEL_TOKEN=ci-token $PY -m tests.serve --port $py_port > "$data/serve.log" 2>&1 &
    local py_pid=$!
    SECRET_KEY=test-secret-key-not-for-production MAX_UPLOAD_BYTES=1048576 MAX_NOTE_IMPORT_BYTES=1048576 ND_ACCEL_TOKEN=ci-token \
    ND_TEMPLATES_DIR=$ROOT/app/templates DB_PATH=$data/world.db ND_RUST_ROUTES=login,dice_roll,live,pages,writes,ai,mcp,fans \
    ND_UPSTREAM=127.0.0.1:$py_port ND_FORWARD_LOG=$data/forwarded.log ND_GATEWAY_LISTEN=127.0.0.1:$gw_port ND_STATIC_DIR=$ROOT/static ND_ACCEL_ROOT=$data \
        "$GW" > "$data/gateway.log" 2>&1 &
    local gw_pid=$!
    for _ in $(seq 1 90); do curl -sf http://127.0.0.1:$gw_port/__gateway/health | grep -q '"upstream":true' && break; sleep 1; done
    ND_TEST_DATA_DIR=$data ND_GOLDEN_DIRECT_URL=http://127.0.0.1:$py_port ND_TEST_BASE_URL=http://127.0.0.1:$gw_port \
        $PY -m pytest -q -p no:cacheprovider --splits "$SHARDS" --group "$n" ${STORE_DURATIONS:+--store-durations --durations-path "$data/durations.json"} \
        "${@:-tests}" > "$data/pytest.log" 2>&1
    local rc=$?
    kill $gw_pid $py_pid 2>/dev/null
    wait $gw_pid $py_pid 2>/dev/null
    echo "shard $n/$SHARDS: $(grep -E '(passed|failed|error)' "$data/pytest.log" | tail -1) (exit $rc)"
    return $rc
}

if [ "$GROUP" != "all" ]; then
    run_shard "$GROUP" "$@"
    rc=$?
    [ $rc -eq 0 ] || tail -n 60 "$ROOT/tests/_data/shard$GROUP/pytest.log"
    exit $rc
fi

pids=()
for n in $(seq 1 "$SHARDS"); do
    run_shard "$n" "$@" &
    pids+=($!)
done
rc=0
for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || { rc=1; echo "--- shard $((i + 1)) failed:"; grep -E '^(FAILED|ERROR)' "$ROOT/tests/_data/shard$((i + 1))/pytest.log" | head -20; }
done
exit $rc
