#!/bin/bash
# Run the golden tests through the WSL pair: wsl_pytest.sh '<-k filter>'
export PATH=/root/.cargo/bin:$PATH
cd /mnt/f/GLM/nd-world
export ND_TEST_DATA_DIR=/root/devdata
export ND_GOLDEN_DIRECT_URL=http://127.0.0.1:9031
export ND_TEST_BASE_URL=http://127.0.0.1:9051
exec /root/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_gateway_golden.py -k "$1"
