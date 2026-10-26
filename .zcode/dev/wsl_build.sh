#!/bin/bash
# Build the gateway in WSL (target in the native fs for speed).
export PATH=/root/.cargo/bin:$PATH
cd /mnt/f/GLM/nd-world/rust
CARGO_TARGET_DIR=/root/target cargo build --release --locked 2>&1 | grep -E '^(error|warning: unused)' -A 10 | head -40
CARGO_TARGET_DIR=/root/target cargo build --release --locked 2>&1 | tail -1
ls -la /root/target/release/nd-gateway 2>/dev/null
mkdir -p /mnt/f/GLM/nd-world/rust/target/release
cp /root/target/release/nd-gateway /mnt/f/GLM/nd-world/rust/target/release/nd-gateway
