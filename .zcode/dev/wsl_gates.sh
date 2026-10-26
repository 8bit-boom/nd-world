#!/bin/bash
# The full gates in WSL: cargo test, then the whole suite through the gateway (4 shards).
export PATH=/root/.cargo/bin:$PATH
export TZ=UTC  # both servers must share a clock: Python reads naive datetimes as local time
cd /mnt/f/GLM/nd-world/rust
echo "== cargo test =="
CARGO_TARGET_DIR=/root/target cargo test --locked 2>&1 | grep -cE 'test result: ok'
echo "== gateway suite =="
cd /mnt/f/GLM/nd-world
rm -f tests/_data/shard*/forwarded.log tests/_data/shard*/pytest.log  # the shards write under tests/_data (ND_TEST_DATA_DIR only covers in-process runs)
mkdir -p /root/devdata
sed 's|/root/devdata|/root/devdata/shardX|' /dev/null >/dev/null  # (placeholder)
PYTHON=/root/venv/bin/python bash scripts/.gs_lf.sh 4 all 2>&1 | tail -5
