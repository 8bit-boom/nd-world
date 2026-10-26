#!/bin/bash
set -e
export PATH=/root/.cargo/bin:$PATH
echo "== venv fix =="
apt-get install -y -qq python3.12-venv 2>&1 | tail -1
rm -rf /root/venv
python3 -m venv /root/venv
/root/venv/bin/pip install -q -r /mnt/f/GLM/nd-world/requirements-dev.txt 2>&1 | grep -v "notice" | tail -1
/root/venv/bin/python -c "import fastapi, pytest, pytest_split; print('venv ok')"
echo "== node 22 (ESM auto-detection; the minifier test needs it) =="
if [ ! -x /root/node22/bin/node ]; then
  curl -fsSL -o /tmp/node22.tar.xz https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
  mkdir -p /root/node22
  tar -xJf /tmp/node22.tar.xz -C /root/node22 --strip-components=1
fi
ln -sf /root/node22/bin/node /usr/local/bin/node
ln -sf /root/node22/bin/npm /usr/local/bin/npm 2>/dev/null || true
node --version
echo "== rust =="
rustc --version
echo "== build =="
cd /mnt/f/GLM/nd-world/rust
CARGO_TARGET_DIR=/root/target cargo build --release --locked 2>&1 | tail -2
ls -la /root/target/release/nd-gateway
