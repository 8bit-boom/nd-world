#!/usr/bin/env bash
# Checks (and, if needed, fixes) SwarmUI's PyTorch install for Volta (V100)
# GPU support — see docs/GPU_SETUP.md §3b for the full explanation.
#
# SwarmUI installs its own PyTorch at first run, and for an NVIDIA GPU its
# install script (launchtools/comfy-install-linux.sh) currently pulls torch
# from the CUDA 13 wheel index. CUDA 13 dropped Volta (compute capability
# 7.0 / sm_70) entirely — a V100 running that build cannot generate images
# at all ("no kernel image is available for execution on the device"), not
# just slowly. `cu126` is the last CUDA-12.x wheel index that still
# includes sm_70; this script reinstalls torch from there if the currently
# installed build doesn't support this card.
#
# Run this on the SwarmUI host (the TrueNAS host, jail, VM, or bare-metal
# box where SwarmUI itself — not necessarily nd-world — lives, in a
# container named like ix-<app-name>-swarmui-1 on TrueNAS, or "swarmui" on
# plain Docker Compose).
#
# Usage:
#   ./fix-swarmui-volta-torch.sh [CONTAINER_NAME]
#
# If CONTAINER_NAME is not supplied the script searches running containers
# for one whose name contains "swarmui".
#
# Safe to re-run any time — it only reinstalls torch when the check finds
# sm_70 missing. Re-run it after: reinstalling/wiping SwarmUI's dlbackend
# folder, or installing a ComfyUI custom node whose own requirements.txt
# lists torch (either can silently pull a CUDA-13 build from PyPI, which
# defaults to CUDA 13 wheels today).

set -euo pipefail

VENV_PYTHON="/SwarmUI/dlbackend/ComfyUI/venv/bin/python"
TARGET_INDEX="https://download.pytorch.org/whl/cu126"

# ── Find the SwarmUI container ────────────────────────────────────────────────

if [[ -n "${1:-}" ]]; then
    CONTAINER="$1"
    echo "==> Using supplied container: $CONTAINER"
else
    echo "==> Searching for a running SwarmUI container..."
    CONTAINER=$(docker ps --format '{{.Names}}' | grep -i swarmui | head -1 || true)
    if [[ -z "$CONTAINER" ]]; then
        echo ""
        echo "ERROR: Could not find a running container with 'swarmui' in its name."
        echo ""
        echo "Currently running containers:"
        docker ps --format '  {{.Names}}'
        echo ""
        echo "If SwarmUI is running under a different name, re-run with it:"
        echo "  ./fix-swarmui-volta-torch.sh <container-name>"
        exit 1
    fi
    echo "==> Found: $CONTAINER"
fi

# ── Validate the venv exists ───────────────────────────────────────────────────

if ! docker exec "$CONTAINER" test -x "$VENV_PYTHON" 2>/dev/null; then
    echo "ERROR: $VENV_PYTHON not found (or not executable) inside $CONTAINER."
    echo "       SwarmUI may not have finished its first-run backend install yet —"
    echo "       start it once and let the ComfyUI backend install complete first."
    exit 1
fi

# ── Check current torch's compute-capability support ─────────────────────────

echo ""
echo "==> Checking torch's compiled architecture list inside $CONTAINER ..."
TORCH_INFO=$(docker exec "$CONTAINER" "$VENV_PYTHON" -c \
    "import torch; print(torch.__version__); print(torch.version.cuda); print(','.join(torch.cuda.get_arch_list()))" \
    2>&1) || {
    echo "ERROR: Could not query torch inside the container. Output:"
    echo "$TORCH_INFO"
    exit 1
}

TORCH_VERSION=$(echo "$TORCH_INFO" | sed -n '1p')
TORCH_CUDA=$(echo "$TORCH_INFO" | sed -n '2p')
ARCH_LIST=$(echo "$TORCH_INFO" | sed -n '3p')

echo "    torch:       $TORCH_VERSION"
echo "    torch CUDA:  $TORCH_CUDA"
echo "    arch list:   $ARCH_LIST"

if echo "$ARCH_LIST" | grep -q "sm_70"; then
    echo ""
    echo "✓ sm_70 (Volta/V100) is already supported by this torch build — nothing to do."
    exit 0
fi

# ── Fix: reinstall torch from the cu126 index ────────────────────────────────

echo ""
echo "==> sm_70 NOT found in the arch list — this torch build cannot use a V100."
echo "==> Reinstalling torch/torchvision from $TARGET_INDEX ..."
docker exec "$CONTAINER" "$VENV_PYTHON" -s -m pip uninstall -y torch torchvision torchaudio || true
docker exec "$CONTAINER" "$VENV_PYTHON" -s -m pip install torch torchvision \
    --index-url "$TARGET_INDEX" --no-cache-dir

echo ""
echo "==> Re-checking ..."
NEW_ARCH_LIST=$(docker exec "$CONTAINER" "$VENV_PYTHON" -c \
    "import torch; print(','.join(torch.cuda.get_arch_list()))")
echo "    arch list: $NEW_ARCH_LIST"

if echo "$NEW_ARCH_LIST" | grep -q "sm_70"; then
    echo ""
    echo "✓ Fixed — sm_70 is now supported."
    echo ""
    echo "Restart SwarmUI now (nd-world's 🔄 restart button on the Image Gen tab,"
    echo "or: docker restart $CONTAINER) for the new torch build to take effect."
else
    echo ""
    echo "ERROR: sm_70 still not present after reinstalling. See docs/GPU_SETUP.md"
    echo "       §3b for manual troubleshooting steps."
    exit 1
fi
