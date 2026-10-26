#!/bin/bash
# Start the pair, run pytest, tear down. wsl_run.sh '<k-filter>' [pytest args / file...]
export PATH=/root/.cargo/bin:$PATH
FILTER="$1"; shift || true
bash /mnt/f/GLM/nd-world/.zcode/dev/wsl_pair.sh >/dev/null || exit 1
cd /mnt/f/GLM/nd-world
export ND_TEST_DATA_DIR=/root/devdata
export ND_GOLDEN_DIRECT_URL=http://127.0.0.1:9031
export ND_TEST_BASE_URL=http://127.0.0.1:9051
if [ $# -gt 0 ]; then
  /root/venv/bin/python -m pytest -q -p no:cacheprovider "$@" -k "$FILTER"
else
  /root/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_gateway_golden.py -k "$FILTER"
fi
rc=$?
for d in /proc/[0-9]*; do
  exe=$(readlink -f "$d/exe" 2>/dev/null || true)
  case "$exe" in
    /root/target/release/nd-gateway) kill "$(basename "$d")" 2>/dev/null || true ;;
  esac
done
for d in /proc/[0-9]*; do
  if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q "tests\.[s]erve"; then
    kill "$(basename "$d")" 2>/dev/null || true
  fi
done
exit $rc
