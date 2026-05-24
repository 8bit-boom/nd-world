# N&D World

A self-hosted worldbuilding and lore management system for the **Neon & Dragons** tabletop RPG campaign. Organize your entire game world — characters, locations, factions, events, items, creatures, and more — with built-in AI assistance, image generation, interactive maps, and visual relationship boards.

---

## Features

- **Multi-world support** — create and switch between separate game worlds, each with its own color accent
- **8 entity types** — Characters, Locations, Organizations, Creatures, Events, Items, Feats, Notes — each with TTRPG-specific subtypes
- **Entity relationships** — link any entity to any other; navigate connections from the detail page
- **Folder organization** — hierarchical folders per entity type for large lore collections
- **Image attachments** — upload JPG/PNG/GIF/WebP/SVG images to any entity
- **Rules viewer** — built-in core rules rendered from Markdown with auto-generated table of contents
- **Interactive maps** — add custom markers and region overlays to map images
- **Schematics** — SVG-based canvas editor for drawing station/dungeon layouts
- **Investment boards** — node-and-edge graph boards for plotting organization structures and story threads
- **AI chat** — Ollama LLM integration for lore generation and brainstorming
- **AI image generation** — SwarmUI or ComfyUI backend with sampler/scheduler control, LoRA, VAE, CLIP skip, img2img, and batch output
- **Image Studio** — embedded SwarmUI iframe at `/imagestudio`
- **Full-text search** — across names, tags, summaries, and body text
- **JSON export / import** — complete world backup and restore with embedded images
- **Mobile-responsive UI** — hamburger nav, touch-friendly targets, stacking layouts on phones and tablets

---

## Table of Contents

- [Requirements](#requirements)
- [Install on Linux](#install-on-linux)
- [Install on Windows](#install-on-windows)
- [Install on TrueNAS SCALE](#install-on-truenas-scale)
- [Environment Variables](#environment-variables)
- [AI Setup](#ai-setup)
- [Data & Backups](#data--backups)
- [Project Structure](#project-structure)
- [Ports Reference](#ports-reference)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Docker Engine + Docker Compose v2 | Required for all install methods |
| Ollama | Optional — for AI chat |
| SwarmUI or ComfyUI | Optional — for AI image generation |
| GPU | Optional — CPU-only mode works for both Ollama and SwarmUI |
| Git | For cloning the repository |

---

## Install on Linux

These instructions work on **Ubuntu 22.04/24.04**, **Debian 12**, and most other modern distros.

### Step 1 — Install Docker

```bash
# Remove old versions if present
sudo apt remove docker docker-engine docker.io containerd runc 2>/dev/null

# Install prerequisites
sudo apt update
sudo apt install -y ca-certificates curl gnupg

# Add Docker's GPG key and repository
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and Compose plugin
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow running docker without sudo (log out and back in after this)
sudo usermod -aG docker $USER
newgrp docker
```

Verify the installation:
```bash
docker --version          # Docker version 26.x or newer
docker compose version    # Docker Compose version v2.x or newer
```

### Step 2 — Install Git and clone the repository

```bash
sudo apt install -y git

git clone https://github.com/8bit-boom/nd-world.git
cd nd-world
```

### Step 3 — Configure the stack

Open `docker-compose.yml` in any text editor and adjust the environment section:

```bash
nano docker-compose.yml
```

Key settings to review:

```yaml
services:
  world:
    ports:
      - "8080:8000"       # change 8080 if that port is already in use
    environment:
      DB_PATH: /data/world.db
      OLLAMA_URL: http://host.docker.internal:11434   # if Ollama runs on the host
      OLLAMA_MODEL: gemma4:26b
      IMAGEGEN_TYPE: swarmui
      IMAGEGEN_URL: http://swarmui:7801
      SWARMUI_EXTERNAL_URL: ""   # set to http://<your-linux-machine-ip>:7801
```

> **Note:** `host.docker.internal` resolves to the host machine from inside a container on Linux only if you add `--add-host=host.docker.internal:host-gateway` to your run command, or add the equivalent to your compose file. Alternatively, use your machine's LAN IP (e.g., `http://192.168.1.100:11434`).

To add host gateway resolution, add this under the `world` service:

```yaml
  world:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### Step 4 — Start the stack

```bash
docker compose up -d
```

Watch the logs to confirm both containers started:

```bash
docker compose logs -f
```

SwarmUI performs a first-run setup on initial boot (downloads ComfyUI backend). This can take **5–15 minutes** depending on your connection. Wait until you see `SwarmUI is ready` in the logs before trying to generate images.

### Step 5 — Open the app

- **N&D World:** [http://localhost:8080](http://localhost:8080)
- **SwarmUI:** [http://localhost:7801](http://localhost:7801)

To access from other devices on your network, replace `localhost` with your machine's LAN IP:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
# example output: inet 192.168.1.100/24
```

### Step 6 — Install Ollama (AI chat)

```bash
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama (it runs as a systemd service automatically after install)
ollama pull gemma4:26b
```

Update `OLLAMA_URL` in `docker-compose.yml` to use your LAN IP if running Ollama on the host:
```yaml
OLLAMA_URL: http://192.168.1.100:11434
```

Then restart the world container:
```bash
docker compose restart world
```

### Managing the service

```bash
docker compose stop          # stop all containers
docker compose start         # start them again
docker compose down          # stop and remove containers (data volumes are kept)
docker compose down -v       # ⚠ also removes volumes — deletes all data
docker compose pull          # pull latest images
docker compose up -d --build # rebuild and restart after code changes
```

---

## Install on Windows

These instructions cover **Windows 10 (21H2+) and Windows 11** using Docker Desktop with WSL 2.

### Step 1 — Enable WSL 2

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

Reboot when prompted. After reboot, a terminal will open to finish Ubuntu setup — create a Linux username and password.

Verify WSL 2 is the default:
```powershell
wsl --set-default-version 2
wsl --list --verbose
```

### Step 2 — Install Docker Desktop

1. Download Docker Desktop from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
2. Run the installer — keep **"Use WSL 2 instead of Hyper-V"** checked
3. After installation, start Docker Desktop and wait for the Docker engine to show **Running** in the taskbar
4. Open **Settings → Resources → WSL Integration** and enable integration for your Ubuntu distribution

Verify in a PowerShell or WSL terminal:
```bash
docker --version
docker compose version
```

### Step 3 — Install Git

Download and install Git from [git-scm.com](https://git-scm.com/download/win).

During setup, select **"Use Git from the Windows Command Prompt"** and **"Use WSL Git"** if prompted.

### Step 4 — Clone the repository

Open a terminal (**Git Bash**, **PowerShell**, or **WSL Ubuntu**) and run:

```bash
git clone https://github.com/8bit-boom/nd-world.git
cd nd-world
```

> **Tip:** For best performance, clone inside the WSL filesystem rather than a Windows path. In WSL terminal:
> ```bash
> cd ~
> git clone https://github.com/8bit-boom/nd-world.git
> cd nd-world
> ```

### Step 5 — Configure the stack

Open `docker-compose.yml` in a text editor (Notepad++, VS Code, etc.):

```powershell
code docker-compose.yml     # if VS Code is installed
# or
notepad docker-compose.yml
```

Key settings:

```yaml
services:
  world:
    ports:
      - "8080:8000"
    environment:
      OLLAMA_URL: http://host.docker.internal:11434   # works on Windows — points to the host
      OLLAMA_MODEL: gemma4:26b
      IMAGEGEN_TYPE: swarmui
      IMAGEGEN_URL: http://swarmui:7801
      SWARMUI_EXTERNAL_URL: "http://localhost:7801"   # on Windows localhost works for iframe
```

> **Windows note:** `host.docker.internal` works out of the box on Docker Desktop for Windows — no extra configuration needed.

### Step 6 — Start the stack

In your terminal (Git Bash, PowerShell, or WSL):

```bash
docker compose up -d
```

Check that both containers started:
```bash
docker compose ps
```

You should see `nd-world` and `swarmui` with status `Up`.

Watch logs while SwarmUI does its first-run setup:
```bash
docker compose logs -f swarmui
```

### Step 7 — Open the app

- **N&D World:** [http://localhost:8080](http://localhost:8080)
- **SwarmUI:** [http://localhost:7801](http://localhost:7801)

To make the app accessible to other devices on your network:
1. Find your Windows LAN IP: open **PowerShell** → `ipconfig` → look for IPv4 Address
2. Open Windows Firewall and create inbound rules for ports **8080** and **7801**:
   ```powershell
   # Run as Administrator
   New-NetFirewallRule -DisplayName "nd-world" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
   New-NetFirewallRule -DisplayName "SwarmUI"  -Direction Inbound -Protocol TCP -LocalPort 7801 -Action Allow
   ```

### Step 8 — Install Ollama (AI chat)

1. Download the Windows installer from [ollama.com](https://ollama.com/download/windows)
2. Run the installer — Ollama starts automatically as a background service
3. Open **PowerShell** and pull a model:
   ```powershell
   ollama pull gemma4:26b
   ```

Because Docker Desktop uses `host.docker.internal` to reach the host, no URL changes are needed — Ollama at `http://host.docker.internal:11434` is already configured in the compose file.

### Managing the service on Windows

Use these commands in any terminal:

```bash
docker compose stop          # stop containers
docker compose start         # start them again
docker compose down          # remove containers (volumes kept)
docker compose up -d --build # rebuild after code changes
```

Or manage containers visually via the **Docker Desktop** GUI under the **Containers** tab.

---

## Install on TrueNAS SCALE

TrueNAS SCALE uses `truenas-compose.yml` which pulls pre-built images from GitHub Container Registry and stores data in host bind-mount paths on your ZFS pool.

### Step 1 — Create dataset structure

In the TrueNAS web UI go to **Datasets** and create the following datasets under your pool (e.g., `DeadPool`):

| Dataset path | Purpose |
|---|---|
| `DeadPool/apps/nd-world` | nd-world database and uploads |
| `DeadPool/apps/swarmui` | SwarmUI root |
| `DeadPool/apps/swarmui/data` | SwarmUI configuration |
| `DeadPool/apps/swarmui/models` | Checkpoint/LoRA model files |
| `DeadPool/apps/swarmui/dlbackend` | ComfyUI backend (auto-downloaded) |

> **Or** create them via SSH shell in one command:
> ```bash
> mkdir -p /mnt/DeadPool/apps/nd-world
> mkdir -p /mnt/DeadPool/apps/swarmui/{data,models,dlbackend}
> ```

If your pool is named differently, search and replace `DeadPool` throughout `truenas-compose.yml`.

### Step 2 — Edit truenas-compose.yml

Copy the file and edit it in a text editor or via the TrueNAS shell:

```bash
cp truenas-compose.yml my-truenas-compose.yml
nano my-truenas-compose.yml
```

Set your TrueNAS IP address:

```yaml
services:
  world:
    environment:
      OLLAMA_URL: "http://192.168.1.xxx:11434"       # your TrueNAS or Ollama host IP
      OLLAMA_MODEL: gemma4:26b
      SWARMUI_EXTERNAL_URL: "http://192.168.1.xxx:7801"  # TrueNAS IP for iframe
```

If your datasets are on a different pool or path, update all volume bind mounts:

```yaml
    volumes:
      - /mnt/YourPool/apps/nd-world:/data         # ← change this path

  swarmui:
    volumes:
      - /mnt/YourPool/apps/swarmui/data:/SwarmUI/Data
      - /mnt/YourPool/apps/swarmui/models:/SwarmUI/Models
      - /mnt/YourPool/apps/swarmui/dlbackend:/SwarmUI/dlbackend
```

### Step 3 — Deploy via Custom App

1. In the TrueNAS SCALE web UI, go to **Apps** → **Discover Apps**
2. Click **Custom App** (top right)
3. Fill in the form:
   - **Application Name:** `nd-world`
   - **Custom Config:** switch to **Compose** mode
   - Paste the full contents of your edited `truenas-compose.yml`
4. Click **Install**

TrueNAS will pull the images and start all three containers (`world`, `swarmui`, `watchtower`).

### Step 4 — Open the portals

After deployment, TrueNAS registers portal buttons automatically from the `net.ix-portals.*` labels:

- **nd-world** opens on port **8087** → `http://<truenas-ip>:8087`
- **SwarmUI** opens on port **7801** → `http://<truenas-ip>:7801`

These appear as clickable portal buttons on the app's tile in **Apps → Installed Apps**.

### Step 5 — Install Ollama on TrueNAS (optional)

You can run Ollama directly on TrueNAS inside a Debian VM or as a separate Docker container.

**Option A — Ollama Docker container** (add to `truenas-compose.yml`):

```yaml
  ollama:
    image: ollama/ollama:latest
    restart: unless-stopped
    ports:
      - "11434:11434"
    volumes:
      - /mnt/DeadPool/apps/ollama:/root/.ollama
    # Uncomment if you have a supported NVIDIA GPU:
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: all
    #           capabilities: [gpu]
```

Then update the `world` service env var:
```yaml
OLLAMA_URL: http://ollama:11434
```

Pull a model after the container starts:
```bash
docker exec -it ollama ollama pull gemma4:26b
```

**Option B — Ollama in a Debian VM:**
Install Ollama normally inside the VM (`curl -fsSL https://ollama.com/install.sh | sh`) and set `OLLAMA_URL` to the VM's IP.

### Step 6 — Place model checkpoints

To generate images, you need at least one Stable Diffusion checkpoint (`.safetensors` or `.ckpt`):

1. Download a checkpoint (e.g., from [civitai.com](https://civitai.com) or [huggingface.co](https://huggingface.co))
2. Place it in the models directory:
   ```bash
   cp your-model.safetensors /mnt/DeadPool/apps/swarmui/models/Stable-Diffusion/
   ```
   SwarmUI organizes models in subdirectories — create `Stable-Diffusion/` if it doesn't exist yet.
3. In SwarmUI's web UI (port 7801), click the **refresh models** button to pick it up.

### Step 7 — Install ComfyUI-Manager (optional)

If you want to install custom ComfyUI nodes via the Manager UI, run the included script from the TrueNAS shell:

```bash
bash /path/to/nd-world/install-comfyui-manager.sh
# or specify the SwarmUI path explicitly:
bash install-comfyui-manager.sh /mnt/DeadPool/apps/swarmui/dlbackend/ComfyUI
```

The script auto-detects the correct path and installs ComfyUI-Manager into `custom_nodes/`.

### Auto-updates (Watchtower)

`truenas-compose.yml` includes **Watchtower**, which checks every 5 minutes for new versions of `ghcr.io/8bit-boom/nd-world:latest` and `ghcr.io/mcmonkeyprojects/swarmui:latest`. When a new image is published it pulls and restarts the affected container automatically — no manual intervention needed.

To disable auto-updates, remove or comment out the `watchtower` service block.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `/data/world.db` | Path to the SQLite database file |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | URL of your Ollama instance |
| `OLLAMA_MODEL` | `gemma4:26b` | Default LLM model for AI chat |
| `IMAGEGEN_TYPE` | _(empty)_ | Image generator backend: `swarmui` or `comfyui` |
| `IMAGEGEN_URL` | _(empty)_ | Internal URL of the image generator API |
| `SWARMUI_EXTERNAL_URL` | _(empty)_ | Browser-accessible SwarmUI URL for the Image Studio iframe |
| `ND_ALLOWED_HOSTS` | `*` | Comma-separated allowed `Host` headers (security hardening) |

---

## AI Setup

### Ollama (chat)

1. Install Ollama (see platform-specific instructions above)
2. Pull a model:
   ```bash
   ollama pull gemma4:26b
   ```
3. Set `OLLAMA_URL` and `OLLAMA_MODEL` in your compose file.

The AI dot in the top navigation bar turns **green** when Ollama is reachable. You can manage models (add, pull, remove) from the **AI** page inside the app.

### SwarmUI (image generation)

SwarmUI is included in all compose files and starts automatically. On first boot it downloads the ComfyUI backend — this takes **5–15 minutes** the first time.

Set `SWARMUI_EXTERNAL_URL` to your host's accessible URL to enable the **Image Studio** embedded iframe view.

The **AI → Image Gen** panel supports:
- Checkpoint model selection, size presets (512², 768×512, 1024², and more), custom W×H
- Sampler, scheduler, steps, CFG scale, seed
- Batch generation (1–4 images shown in a grid)
- LoRA name + weight, VAE override, CLIP skip
- Image-to-image (img2img) — upload an init image and set denoising strength

### ComfyUI (alternative backend)

Set `IMAGEGEN_TYPE=comfyui` and `IMAGEGEN_URL` to your ComfyUI instance address (default port 8188). A checkpoint must already be loaded in ComfyUI before sending a request.

---

## Data & Backups

All persistent data lives in the `/data` volume:
- `world.db` — SQLite database with all worlds, entities, and relationships
- `uploads/` — uploaded images and AI-generated images

### Export a world

Click **Export** in the nav bar. The download is a self-contained JSON file with all entities and images embedded as base64. Keep this file as your backup.

### Import a world

Go to **Worlds** → **Manage** → **Import** and upload a previously exported JSON file.

### Manual backup

```bash
# Docker named volume
docker cp nd-world:/data ./backup-$(date +%Y%m%d)

# TrueNAS bind mount — just copy the directory
cp -r /mnt/DeadPool/apps/nd-world ./backup-$(date +%Y%m%d)
```

---

## Project Structure

```
nd-world/
├── app/
│   ├── main.py              # All FastAPI routes
│   ├── models.py            # SQLAlchemy ORM models
│   ├── database.py          # DB init and migrations
│   ├── ai.py                # Ollama + image gen integration
│   ├── routers/
│   │   └── ai.py            # /api/ai/* endpoints
│   ├── templates/           # Jinja2 HTML templates
│   └── core_rules.md        # Game rules source document
├── static/
│   └── style.css            # All app styles
├── docker-compose.yml       # Development / Linux / Windows stack
├── truenas-compose.yml      # TrueNAS SCALE production stack
├── Dockerfile
├── requirements.txt
└── install-comfyui-manager.sh
```

---

## Ports Reference

| Service | Default Port | Compose file |
|---------|-------------|--------------|
| nd-world | 8080 | `docker-compose.yml` |
| nd-world | 8087 | `truenas-compose.yml` |
| SwarmUI | 7801 | both |
| Ollama | 11434 | external / optional container |

---

## Troubleshooting

**AI dot stays grey**
- Check Ollama is running: `curl http://localhost:11434/api/tags`
- On Linux/Windows with Ollama on the host: make sure `OLLAMA_URL` uses your LAN IP or `host.docker.internal`, not `localhost` (which resolves inside the container)
- Restart after changing env vars: `docker compose restart world`

**Image generation returns an error**
- Check backend status at `/api/ai/imagegen/status` in your browser
- Confirm both `IMAGEGEN_TYPE` and `IMAGEGEN_URL` are set
- SwarmUI first-run can take 10–15 minutes — check logs: `docker compose logs -f swarmui`
- Confirm at least one checkpoint model is present in the models directory

**Image Studio iframe is blank or shows connection refused**
- `SWARMUI_EXTERNAL_URL` must be a URL your **browser** can reach — not an internal Docker hostname like `http://swarmui:7801`
- Use your LAN IP: `http://192.168.1.xxx:7801`
- On Windows, ensure the Windows Firewall allows port 7801

**Database locked / app won't start**
- Only one nd-world instance should write to the same `world.db`
- Recover from a crashed container: `docker compose restart world`

**Port already in use on startup**
- Change the host port in `docker-compose.yml` (left side of the mapping):
  ```yaml
  ports:
    - "8090:8000"   # use 8090 instead
  ```

**Uploads not persisting after restart**
- Confirm the `/data` volume is mounted correctly; both `world.db` and `uploads/` must be inside it
- For TrueNAS: check the dataset path exists and the container has write permissions

**SwarmUI models directory is empty after restart (TrueNAS)**
- Make sure the models bind mount is correct in `truenas-compose.yml`
- The bind path must exist on the host before the container starts; create it with `mkdir -p`
