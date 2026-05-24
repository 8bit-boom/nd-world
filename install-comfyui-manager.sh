#!/usr/bin/env bash
# Installs ComfyUI-Manager into the ComfyUI backend that SwarmUI uses.
# Run this on the TrueNAS host (or wherever SwarmUI is installed).
#
# Usage:
#   ./install-comfyui-manager.sh [SWARMUI_PATH]
#
# SWARMUI_PATH defaults to /mnt/DeadPool/apps/SwarmUI

set -euo pipefail

SWARMUI_PATH="${1:-/mnt/DeadPool/apps/SwarmUI}"
COMFYUI_PATH="$SWARMUI_PATH/dlbackend/ComfyUI"
CUSTOM_NODES="$COMFYUI_PATH/custom_nodes"
MANAGER_DIR="$CUSTOM_NODES/ComfyUI-Manager"
MANAGER_REPO="https://github.com/ltdrdata/ComfyUI-Manager"

echo "==> SwarmUI path:  $SWARMUI_PATH"
echo "==> ComfyUI path:  $COMFYUI_PATH"

# ── Validate paths ────────────────────────────────────────────────────────────

if [[ ! -d "$SWARMUI_PATH" ]]; then
    echo "ERROR: SwarmUI directory not found: $SWARMUI_PATH"
    echo "       Pass the correct path as the first argument."
    echo "       Example: ./install-comfyui-manager.sh /mnt/MyPool/apps/SwarmUI"
    exit 1
fi

if [[ ! -d "$COMFYUI_PATH" ]]; then
    echo "ERROR: ComfyUI backend not found at $COMFYUI_PATH"
    echo "       Make sure SwarmUI has been launched at least once so it"
    echo "       downloads the ComfyUI backend into dlbackend/ComfyUI."
    exit 1
fi

mkdir -p "$CUSTOM_NODES"

# ── Clone or update ComfyUI-Manager ──────────────────────────────────────────

if [[ -d "$MANAGER_DIR/.git" ]]; then
    echo "==> ComfyUI-Manager already installed — pulling latest changes..."
    git -C "$MANAGER_DIR" pull --ff-only
else
    echo "==> Cloning ComfyUI-Manager..."
    git clone "$MANAGER_REPO" "$MANAGER_DIR"
fi

# ── Install Python requirements ───────────────────────────────────────────────
# SwarmUI creates a venv inside the ComfyUI directory.  Try the most common
# locations; fall back to the system pip if none is found.

PIP_CMD=""
for candidate in \
    "$COMFYUI_PATH/.venv/bin/pip" \
    "$COMFYUI_PATH/venv/bin/pip" \
    "$SWARMUI_PATH/.venv/bin/pip" \
    "$SWARMUI_PATH/venv/bin/pip"; do
    if [[ -x "$candidate" ]]; then
        PIP_CMD="$candidate"
        break
    fi
done

REQ_FILE="$MANAGER_DIR/requirements.txt"

if [[ -f "$REQ_FILE" ]]; then
    if [[ -n "$PIP_CMD" ]]; then
        echo "==> Installing requirements with $PIP_CMD ..."
        "$PIP_CMD" install -r "$REQ_FILE"
    else
        echo "WARNING: Could not find a pip inside the ComfyUI/SwarmUI venv."
        echo "         Attempting system pip install (may require sudo)..."
        pip install -r "$REQ_FILE" || python3 -m pip install -r "$REQ_FILE"
    fi
else
    echo "==> No requirements.txt found — skipping pip install."
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "ComfyUI-Manager installed successfully."
echo ""
echo "Next steps:"
echo "  1. Restart SwarmUI (stop and start the service / container)."
echo "  2. Open the ComfyUI interface inside SwarmUI."
echo "  3. You should see a 'Manager' button in the ComfyUI toolbar."
