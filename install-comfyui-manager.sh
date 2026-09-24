#!/usr/bin/env bash
# Installs ComfyUI-Manager into the ComfyUI backend that SwarmUI uses.
# Run this on the machine where SwarmUI itself runs — the TrueNAS host, a
# Debian VM/jail, or wherever `docker compose ... swarmui` (or the
# equivalent TrueNAS Custom App) is actually running.
#
# Usage:
#   ./install-comfyui-manager.sh [SWARMUI_PATH_OR_CONTAINER]
#
# If nothing is supplied, the script first looks for a running Docker
# container with "swarmui" in its name (the normal case for this repo's
# docker-compose.yml/truenas-compose.yml deployments) and, if found, does
# everything through `docker exec` — this is REQUIRED when SwarmUI runs in
# a container: the ComfyUI venv is created with a Python interpreter whose
# absolute path (baked into the venv's shebang lines) only exists inside
# that container, so running the venv's pip from a bind-mounted host path
# fails with "bad interpreter" even though the files are visible there.
# `docker-compose.yml`'s SwarmUI uses a named Docker volume for dlbackend
# (not a host bind mount at all), so docker exec is the ONLY way to reach
# it from the host for that deployment.
#
# If no running container is found, falls back to searching common host
# filesystem locations for a non-containerized (bare VM/jail) install.

set -euo pipefail

MANAGER_REPO="https://github.com/ltdrdata/ComfyUI-Manager"
CONTAINER_SWARMUI_PATH="/SwarmUI"
CONTAINER_VENV_PYTHON="/SwarmUI/dlbackend/ComfyUI/venv/bin/python"

# ── Try a running Docker container first ─────────────────────────────────────

find_swarmui_container() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -i swarmui | head -1 || true
}

install_via_docker() {
    local container="$1"
    echo "==> Found running SwarmUI container: $container"
    if ! docker exec "$container" test -d "$CONTAINER_SWARMUI_PATH/dlbackend/ComfyUI" 2>/dev/null; then
        echo "ERROR: $CONTAINER_SWARMUI_PATH/dlbackend/ComfyUI not found inside $container."
        echo "       Launch SwarmUI at least once so it downloads the ComfyUI backend."
        exit 1
    fi
    local custom_nodes="$CONTAINER_SWARMUI_PATH/dlbackend/ComfyUI/custom_nodes"
    local manager_dir="$custom_nodes/ComfyUI-Manager"

    docker exec "$container" mkdir -p "$custom_nodes"

    if docker exec "$container" test -d "$manager_dir/.git" 2>/dev/null; then
        echo "==> ComfyUI-Manager already installed — pulling latest changes..."
        docker exec "$container" git -C "$manager_dir" pull --ff-only
    else
        echo "==> Cloning ComfyUI-Manager..."
        docker exec "$container" git clone "$MANAGER_REPO" "$manager_dir"
    fi

    local req_file="$manager_dir/requirements.txt"
    if docker exec "$container" test -f "$req_file" 2>/dev/null; then
        echo "==> Installing requirements inside the container..."
        docker exec "$container" "$CONTAINER_VENV_PYTHON" -s -m pip install -r "$req_file"
    else
        echo "==> No requirements.txt found — skipping pip install."
    fi

    echo ""
    echo "ComfyUI-Manager installed successfully inside $container at:"
    echo "  $manager_dir"
    echo ""
    echo "Next steps:"
    echo "  1. Restart SwarmUI (nd-world's 🔄 button, or: docker restart $container)."
    echo "  2. Open the ComfyUI interface inside SwarmUI."
    echo "  3. You should see a 'Manager' button in the ComfyUI toolbar."
    exit 0
}

if [[ -z "${1:-}" ]]; then
    FOUND_CONTAINER=$(find_swarmui_container)
    if [[ -n "$FOUND_CONTAINER" ]]; then
        install_via_docker "$FOUND_CONTAINER"
    fi
    echo "==> No running Docker container with 'swarmui' in its name found."
    echo "    Falling back to a host-filesystem search (non-containerized install)."
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; then
    # An exact container name was supplied directly.
    install_via_docker "$1"
fi

# ── Fall back: host filesystem search (bare VM/jail, no Docker) ─────────────

SEARCH_PATHS=(
    # TrueNAS pool paths — both casings, since a host bind-mount path is
    # whatever the compose file's volumes: line actually says (this repo's
    # own truenas-compose.yml uses lowercase "swarmui").
    /mnt/DeadPool/apps/swarmui
    /mnt/DeadPool/apps/SwarmUI
    /mnt/DeadPool/swarmui
    /mnt/DeadPool/SwarmUI
    /mnt/*/apps/swarmui
    /mnt/*/apps/SwarmUI
    /mnt/*/swarmui
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
        echo "ERROR: Could not find a running SwarmUI container or a local installation."
        echo ""
        echo "Checked host paths:"
        for p in "${SEARCH_PATHS[@]}"; do echo "  $p"; done
        echo ""
        echo "Tips:"
        echo "  - If SwarmUI runs in Docker under a different container name, re-run"
        echo "    with that name: ./install-comfyui-manager.sh <container-name>"
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
#
# This host-run venv/bin/pip is only correct here because we've fallen back
# to a genuinely non-containerized install — the venv's own python was
# created ON THIS HOST, so its shebang paths resolve. Do NOT reuse this
# path against a Docker bind-mount; see install_via_docker() above for why.

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
