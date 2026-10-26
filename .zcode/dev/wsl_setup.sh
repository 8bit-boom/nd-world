#!/bin/bash
# One-time WSL setup: rustup, the Python venv, the build.
set -e
export PATH=/root/.cargo/bin:$PATH
echo "== rustup =="
if ! command -v rustc >/dev/null; then
  curl -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal 2>&1 | tail -1
fi
rustc --version
echo "== venv =="
if [ ! -x /root/venv/bin/python ]; then
  python3 -m venv /root/venv
fi
/root/venv/bin/pip install -q -r /mnt/f/GLM/nd-world/requirements-dev.txt 2>&1 | tail -1
/root/venv/bin/python -c "import fastapi, pytest, pytest_split; print('venv ok')"
echo "== build =="
cd /mnt/f/GLM/nd-world/rust
CARGO_TARGET_DIR=/root/target cargo build --release --locked 2>&1 | tail -2
