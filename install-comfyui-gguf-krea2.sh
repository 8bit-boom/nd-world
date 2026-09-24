#!/usr/bin/env bash
# Installs the RealRebelAI/ComfyUI-GGUF_KREA-2 custom node into the ComfyUI
# backend that SwarmUI uses — the one manual step the "Krea 2 GGUF Quick
# Setup" panel (Image Gen tab in nd-world) can't do for you, because
# nd-world's own container has no filesystem access to SwarmUI's
# custom_nodes folder (only to its Models folder, via SWARMUI_MODELS_DIR).
#
# Why this exists instead of just letting SwarmUI's own "install
# city96/ComfyUI-GGUF?" prompt handle it: that upstream release doesn't
# parse Krea 2's ops yet (city96/ComfyUI-GGUF#464 at the time this was
# written) — generation with a Krea 2 GGUF file fails against it. The
# RealRebelAI fork is a drop-in replacement (same node/class names), which
# is also exactly why it CAN'T coexist with the upstream node: ComfyUI
# would try to register two custom nodes under the same class names. This
# script detects and removes any existing city96/ComfyUI-GGUF install
# before cloning the fork, so you don't have to sort that out by hand.
#
# Run this on the SwarmUI host (the TrueNAS host, jail, VM, or bare-metal
# box where SwarmUI itself — not nd-world — lives), same as
# install-comfyui-manager.sh (this script mirrors its structure).
#
# Usage:
#   ./install-comfyui-gguf-krea2.sh [SWARMUI_PATH]
#
# If SWARMUI_PATH is not supplied the script searches common locations
# automatically and asks you to confirm before proceeding.

set -euo pipefail

GGUF_REPO="https://github.com/RealRebelAI/ComfyUI-GGUF_KREA-2"
UPSTREAM_NODE_NAME="ComfyUI-GGUF"   # city96's — conflicts with the fork below
FORK_NODE_NAME="ComfyUI-GGUF_KREA-2"

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
        echo "      ./install-comfyui-gguf-krea2.sh /path/to/SwarmUI"
        exit 1
    fi
fi

COMFYUI_PATH="$SWARMUI_PATH/dlbackend/ComfyUI"
CUSTOM_NODES="$COMFYUI_PATH/custom_nodes"
UPSTREAM_DIR="$CUSTOM_NODES/$UPSTREAM_NODE_NAME"
FORK_DIR="$CUSTOM_NODES/$FORK_NODE_NAME"

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

# ── Remove any conflicting upstream install ──────────────────────────────────
#
# Same node/class names as the fork we're about to install — if SwarmUI
# already auto-installed city96/ComfyUI-GGUF (its own prompt, the first
# time it saw a .gguf file), leaving both directories in custom_nodes would
# have ComfyUI try to register duplicate node classes on next start. Back
# it up rather than deleting outright, in case you want to revert.

if [[ -d "$UPSTREAM_DIR" ]]; then
    BACKUP_DIR="${UPSTREAM_DIR}.disabled-$(date +%Y%m%d%H%M%S)"
    echo ""
    echo "==> Found existing upstream node at: $UPSTREAM_DIR"
    echo "    It shares node/class names with the fork and would conflict."
    echo "    Moving it aside to: $BACKUP_DIR"
    mv "$UPSTREAM_DIR" "$BACKUP_DIR"
fi

# ── Clone or update the fork ──────────────────────────────────────────────────

if [[ -d "$FORK_DIR/.git" ]]; then
    echo "==> ComfyUI-GGUF_KREA-2 already installed — pulling latest changes..."
    git -C "$FORK_DIR" pull --ff-only
else
    echo "==> Cloning ComfyUI-GGUF_KREA-2..."
    git clone "$GGUF_REPO" "$FORK_DIR"
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

REQ_FILE="$FORK_DIR/requirements.txt"

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
echo "ComfyUI-GGUF_KREA-2 installed successfully at:"
echo "  $FORK_DIR"
echo ""
echo "Next steps:"
echo "  1. Restart SwarmUI (so ComfyUI picks up the new custom node)."
echo "  2. Use the Krea 2 GGUF Quick Setup panel on nd-world's Image Gen tab"
echo "     to download a BASE/TURBO diffusion model + the Qwen3-VL-4B-Instruct"
echo "     text encoder, if you haven't already."
echo "  3. Pick the 'Krea 2 Turbo' Image Gen template in nd-world and generate —"
echo "     the .gguf files now load through this node instead of the upstream one."
