#!/usr/bin/env bash
# Installs ComfyUI-Manager into the ComfyUI backend that SwarmUI uses.
# Run this on the TrueNAS host (or inside the Debian VM/jail where SwarmUI lives).
#
# Usage:
#   ./install-comfyui-manager.sh [SWARMUI_PATH]
#
# If SWARMUI_PATH is not supplied the script searches common locations
# automatically and asks you to confirm before proceeding.

set -euo pipefail

MANAGER_REPO="https://github.com/ltdrdata/ComfyUI-Manager"

# ── Auto-detect SwarmUI ───────────────────────────────────────────────────────

SEARCH_PATHS=(
    # TrueNAS pool paths
    /mnt/DeadPool/apps/SwarmUI
    /mnt/DeadPool/SwarmUI
    /mnt/*/apps/SwarmUI
    /mnt/*/SwarmUI
    # Common Linux home / opt paths (for Debian VM / jail)
    /opt/SwarmUI
    /opt/swarmui
    /root/SwarmUI
    /home/*/SwarmUI
    # Docker volume mounts
    /app/SwarmUI
)

find_swarmui() {
    for p in "${SEARCH_PATHS[@]}"; do
        # glob expansion
        for expanded in $p; do
            if [[ -d "$expanded/dlbackend/ComfyUI" ]]; then
                echo "$expanded"
                return 0
            fi
        done
    done
    return 1
}

if [[ -n "${1:-}" ]]; then
    SWARMUI_PATH="$1"
    echo "==> Using supplied path: $SWARMUI_PATH"
else
    echo "==> Searching for SwarmUI installation..."
    if FOUND=$(find_swarmui 2>/dev/null); then
        echo "==> Found SwarmUI at: $FOUND"
        read -rp "    Use this path? [Y/n] " yn
        yn="${yn:-Y}"
        if [[ "$yn" =~ ^[Yy]$ ]]; then
            SWARMUI_PATH="$FOUND"
        else
            read -rp "    Enter the SwarmUI path manually: " SWARMUI_PATH
        fi
    else
        echo ""
        echo "ERROR: Could not find a SwarmUI installation automatically."
        echo ""
        echo "Checked paths:"
        for p in "${SEARCH_PATHS[@]}"; do echo "  $p"; done
        echo ""
        echo "Tips:"
        echo "  - If SwarmUI is inside your Debian VM or jail, SSH into it and"
        echo "    run this script there."
        echo "  - If SwarmUI is not installed yet, install it first:"
        echo "      git clone https://github.com/mcmonkeyprojects/SwarmUI"
        echo "      cd SwarmUI && bash ./install-linux.sh"
        echo "  - Once installed, re-run with the path:"
        echo "      ./install-comfyui-manager.sh /path/to/SwarmUI"
        exit 1
    fi
fi

COMFYUI_PATH="$SWARMUI_PATH/dlbackend/ComfyUI"
CUSTOM_NODES="$COMFYUI_PATH/custom_nodes"
MANAGER_DIR="$CUSTOM_NODES/ComfyUI-Manager"

echo ""
echo "==> SwarmUI path:  $SWARMUI_PATH"
echo "==> ComfyUI path:  $COMFYUI_PATH"

# ── Validate ──────────────────────────────────────────────────────────────────

if [[ ! -d "$SWARMUI_PATH" ]]; then
    echo "ERROR: Directory not found: $SWARMUI_PATH"
    exit 1
fi

if [[ ! -d "$COMFYUI_PATH" ]]; then
    echo "ERROR: ComfyUI backend not found at $COMFYUI_PATH"
    echo "       Launch SwarmUI at least once so it downloads the ComfyUI backend."
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
        echo "WARNING: No venv pip found — trying system pip..."
        pip install -r "$REQ_FILE" || python3 -m pip install -r "$REQ_FILE"
    fi
else
    echo "==> No requirements.txt found — skipping pip install."
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "ComfyUI-Manager installed successfully at:"
echo "  $MANAGER_DIR"
echo ""
echo "Next steps:"
echo "  1. Restart SwarmUI."
echo "  2. Open the ComfyUI interface inside SwarmUI."
echo "  3. You should see a 'Manager' button in the ComfyUI toolbar."
