# N&D World

A self-hosted worldbuilding and lore management system for the **Neon & Dragons** tabletop RPG campaign. Organize your entire game world — characters, locations, factions, events, items, creatures, and more — with built-in AI assistance, image generation, interactive maps, and visual relationship boards.

---

## Features

- **Multi-world support** — create and switch between separate game worlds, each with its own colour accent
- **8 entity types** — Characters, Locations, Organizations, Creatures, Events, Items, Feats, Notes — each with TTRPG-specific subtypes
- **Entity relationships** — link any entity to any other; navigate connections from the detail page
- **Folder organization** — hierarchical folders per entity type for large lore collections
- **Image attachments** — upload JPG/PNG/GIF/WebP/SVG images to any entity
- **Rules viewer** — built-in core rules rendered from Markdown with auto-generated table of contents
- **Interactive maps** — add custom markers and region overlays to map images
- **Schematics** — SVG-based canvas editor for drawing station/dungeon layouts
- **Investment boards** — node-and-edge graph boards for plotting organization structures and story threads
- **AI chat** — Ollama LLM integration with streaming responses and world-lore RAG context
- **AI Models tab** — download, manage, and delete Ollama models directly from the app UI with live progress bars; popular model quick-picks included
- **AI image generation** — SwarmUI or ComfyUI backend with sampler/scheduler, LoRA, VAE, CLIP skip, upscaling, img2img, batch output, and generation history with parameter reuse
- **Image Studio** — embedded SwarmUI iframe at `/imagestudio`
- **Universal character sheets** — fully configurable stats, skills, and currency (N&D defaults: POW/AGI/FOR/INT/PER/SOC); optional secondary resource tracker
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
| Git | For cloning the repository |
| GPU | Optional — CPU-only mode works for Ollama and SwarmUI |

Ollama and SwarmUI are **included in the Docker stack** — no separate installation needed.

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

### Step 2 — Clone the repository

```bash
sudo apt install -y git
git clone https://github.com/8bit-boom/nd-world.git
cd nd-world
```

### Step 3 — Configure the stack

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
      OLLAMA_MODEL: gemma4:26b        # model to use for AI chat
      SWARMUI_EXTERNAL_URL: ""        # set to http://<your-ip>:7801 for Image Studio iframe
```

> **Note:** `OLLAMA_URL` is pre-configured to `http://ollama:11434` pointing at the bundled Ollama container. You only need to change it if you want to use a different Ollama instance.

### Step 4 — Start the stack

```bash
docker compose up -d
```

This starts three services: **nd-world** (app), **SwarmUI** (image gen), and **Ollama** (AI chat).

Watch the logs:
```bash
docker compose logs -f
```

SwarmUI performs a first-run setup on initial boot (downloads the ComfyUI backend). This can take **5–15 minutes** depending on your connection.

### Step 5 — Download an AI model

Open the app at [http://localhost:8080](http://localhost:8080) and go to **AI → 🤖 Models tab**. Click any model in the **Popular Models** grid (e.g. *Llama 3.2 3B* for a fast 2 GB download, or *Gemma 4 26B* for the best quality) — the progress bar shows download percentage and GB transferred. Once complete, the model is ready for chat.

Alternatively, pull from the command line:
```bash
docker compose exec ollama ollama pull gemma4:26b
```

### Step 6 — Open the app

- **N&D World:** [http://localhost:8080](http://localhost:8080)
- **SwarmUI:** [http://localhost:7801](http://localhost:7801)
- **Ollama API:** [http://localhost:11434](http://localhost:11434)

To access from other devices on your network, replace `localhost` with your machine's LAN IP:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
# example: inet 192.168.1.100/24
```

Also set `SWARMUI_EXTERNAL_URL: "http://192.168.1.100:7801"` in `docker-compose.yml` to enable the Image Studio iframe, then restart: `docker compose restart world`.

### Step 7 — Add image generation models

Place `.safetensors` or `.ckpt` checkpoint files into the SwarmUI models volume. With Docker named volumes, the easiest way is to copy via a temporary container:

```bash
# Find the volume mount path
docker volume inspect nd-world_swarmui-models

# Or copy directly via docker
docker run --rm -v nd-world_swarmui-models:/models \
  -v $(pwd):/src alpine cp /src/your-model.safetensors /models/Stable-Diffusion/
```

Then open the **AI → Image Gen** tab, click the model dropdown refresh button — your model will appear.

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

### Step 3 — Install Git and clone the repository

Download and install Git from [git-scm.com](https://git-scm.com/download/win).

Then in Git Bash, PowerShell, or WSL:
```bash
git clone https://github.com/8bit-boom/nd-world.git
cd nd-world
```

> **Tip:** For best performance, clone inside the WSL filesystem: open WSL terminal → `cd ~` → then clone.

### Step 4 — Configure the stack

Open `docker-compose.yml` in VS Code or Notepad:

```yaml
services:
  world:
    ports:
      - "8080:8000"
    environment:
      OLLAMA_MODEL: gemma4:26b
      SWARMUI_EXTERNAL_URL: "http://localhost:7801"   # localhost works on Windows for iframe
```

> **Windows note:** `host.docker.internal` is automatically available on Docker Desktop for Windows — no extra configuration needed. The bundled Ollama container connects internally via `http://ollama:11434`.

### Step 5 — Start the stack

```bash
docker compose up -d
```

Check all three containers started:
```bash
docker compose ps
# nd-world, swarmui, ollama should all show "Up"
```

### Step 6 — Download an AI model

Open [http://localhost:8080](http://localhost:8080) → **AI → 🤖 Models** → click any popular model chip to download it with live progress.

Or via terminal:
```bash
docker compose exec ollama ollama pull gemma4:26b
```

### Step 7 — Open the app

- **N&D World:** [http://localhost:8080](http://localhost:8080)
- **SwarmUI:** [http://localhost:7801](http://localhost:7801)

To make the app accessible to other devices on your network:
1. Find your LAN IP: **PowerShell** → `ipconfig` → look for IPv4 Address under your adapter
2. Open Windows Firewall for the ports:
   ```powershell
   # Run as Administrator
   New-NetFirewallRule -DisplayName "nd-world" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
   New-NetFirewallRule -DisplayName "SwarmUI"  -Direction Inbound -Protocol TCP -LocalPort 7801 -Action Allow
   New-NetFirewallRule -DisplayName "Ollama"   -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
   ```

### Managing the service on Windows

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
| `DeadPool/apps/swarmui/data` | SwarmUI configuration |
| `DeadPool/apps/swarmui/models` | Checkpoint/LoRA model files |
| `DeadPool/apps/swarmui/dlbackend` | ComfyUI backend (auto-downloaded) |
| `DeadPool/apps/ollama` | Ollama model storage |

> **Or** create them all via SSH shell:
> ```bash
> mkdir -p /mnt/DeadPool/apps/nd-world
> mkdir -p /mnt/DeadPool/apps/swarmui/{data,models,dlbackend}
> mkdir -p /mnt/DeadPool/apps/ollama
> ```

If your pool is named differently, search and replace `DeadPool` throughout `truenas-compose.yml`.

### Step 2 — Edit truenas-compose.yml

```bash
nano truenas-compose.yml
```

Set your TrueNAS IP address:

```yaml
services:
  world:
    environment:
      OLLAMA_MODEL: gemma4:26b
      SWARMUI_EXTERNAL_URL: "http://192.168.1.xxx:7801"  # your TrueNAS IP
```

If your datasets are on a different pool or path, update all volume bind mounts (search and replace `/mnt/DeadPool`).

### Step 3 — Deploy via Custom App

1. In the TrueNAS SCALE web UI, go to **Apps** → **Discover Apps**
2. Click **Custom App** (top right)
3. Fill in the form:
   - **Application Name:** `nd-world`
   - **Custom Config:** switch to **Compose** mode
   - Paste the full contents of your edited `truenas-compose.yml`
4. Click **Install**

TrueNAS will pull the images and start all containers (`world`, `swarmui`, `ollama`, `watchtower`).

### Step 4 — Open the portals

After deployment, TrueNAS registers portal buttons automatically from the `net.ix-portals.*` labels:

| Service | Port | URL |
|---------|------|-----|
| nd-world | 8087 | `http://<truenas-ip>:8087` |
| SwarmUI | 7801 | `http://<truenas-ip>:7801` |
| Ollama | 11434 | `http://<truenas-ip>:11434` |

These appear as clickable portal buttons on the app's tile in **Apps → Installed Apps**.

### Step 5 — Download an AI model

Open nd-world → **AI → 🤖 Models tab** → click a popular model to download it, or:

```bash
docker exec -it <ollama-container-name> ollama pull gemma4:26b
```

### Step 6 — Place image generation checkpoints

Copy `.safetensors` or `.ckpt` files into the models directory:
```bash
cp your-model.safetensors /mnt/DeadPool/apps/swarmui/models/Stable-Diffusion/
```

Then open the **AI → Image Gen** tab and refresh the model dropdown.

### Step 7 — Install ComfyUI-Manager (optional)

```bash
bash install-comfyui-manager.sh /mnt/DeadPool/apps/swarmui/dlbackend/ComfyUI
```

The script auto-detects the correct path and installs ComfyUI-Manager into `custom_nodes/`.

### Auto-updates (Watchtower)

`truenas-compose.yml` includes **Watchtower**, which checks every 5 minutes for new image versions and restarts affected containers automatically. To disable auto-updates, remove or comment out the `watchtower` service block.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `/data/world.db` | Path to the SQLite database file |
| `OLLAMA_URL` | `http://ollama:11434` | URL of the Ollama instance (pre-configured to bundled container) |
| `OLLAMA_MODEL` | `gemma4:26b` | Default LLM model for AI chat |
| `IMAGEGEN_TYPE` | `swarmui` | Image generator backend: `swarmui` or `comfyui` |
| `IMAGEGEN_URL` | `http://swarmui:7801` | Internal URL of the image generator API |
| `SWARMUI_EXTERNAL_URL` | _(empty)_ | Browser-accessible SwarmUI URL for the Image Studio iframe |
| `ND_ALLOWED_HOSTS` | `*` | Comma-separated allowed `Host` headers (security hardening) |

---

## AI Setup

### Ollama (chat)

Ollama is included in both `docker-compose.yml` and `truenas-compose.yml` and starts automatically with the stack. No separate installation needed.

**Downloading models:**

The easiest way is via the in-app **🤖 Models tab** (AI page):
- Click any chip in the **Popular Models** grid to start a download with live progress
- Or paste a custom model ID (e.g. `llama3.2:3b`, `hf.co/username/model`) and click **⬇ Pull & Add**
- Downloaded models appear in the model list with a green status dot and a **✓ Use** button to activate them for chat

From the command line:
```bash
# Docker Compose
docker compose exec ollama ollama pull gemma4:26b

# TrueNAS (find container name first)
docker ps | grep ollama
docker exec -it <name> ollama pull gemma4:26b
```

**Recommended models by hardware:**

| Model | Size | Notes |
|-------|------|-------|
| `llama3.2:1b` | ~0.9 GB | Minimum spec — works on anything |
| `llama3.2:3b` | ~2 GB | Good quality, fast on CPU |
| `gemma3:4b` | ~3 GB | Strong reasoning, low RAM |
| `gemma3:12b` | ~8 GB | Best quality under 16 GB RAM |
| `gemma4:26b` | ~17 GB | Best quality — needs 20+ GB RAM or GPU |

**GPU acceleration:**

Uncomment the `deploy` block in the Ollama service to enable NVIDIA GPU offloading:

```yaml
  ollama:
    # ...
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

AMD GPU support requires the ROCm image: change `image: ollama/ollama` to `image: ollama/ollama:rocm`.

### SwarmUI (image generation)

SwarmUI is included in all compose files and starts automatically. On first boot it downloads the ComfyUI backend — this takes **5–15 minutes** the first time.

Set `SWARMUI_EXTERNAL_URL` to your host's accessible URL to enable the **Image Studio** embedded iframe view.

The **AI → Image Gen** panel supports:
- Checkpoint model selection with deep subfolder scanning
- Size presets (512², 768×512, 1024², and more) plus custom W×H
- Sampler, scheduler, steps, CFG scale, seed
- Batch generation (1–4 images)
- LoRA name + weight
- VAE override, CLIP skip
- **Upscaling** — model selector (pulls from SwarmUI Upscale model folder) + ×1.5/×2/×3/×4 scale factor
- Image-to-image (img2img) — upload an init image and set denoising strength
- **Generation history** — last 50 generations stored in browser localStorage with one-click parameter reuse

### ComfyUI (alternative backend)

Set `IMAGEGEN_TYPE=comfyui` and `IMAGEGEN_URL` to your ComfyUI instance address (default port 8188). A checkpoint must already be loaded before sending a request.

---

## Data & Backups

All persistent data lives in the `/data` volume:
- `world.db` — SQLite database with all worlds, entities, and relationships
- `uploads/` — uploaded images and AI-generated images (`uploads/ai-images/`)

### Export a world

Click **Export** in the nav bar. The download is a self-contained JSON file with all entities and images embedded as base64.

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
│   ├── main.py              # FastAPI routes
│   ├── models.py            # SQLAlchemy ORM models
│   ├── database.py          # DB init and migrations
│   ├── ai.py                # Ollama + image gen integration
│   ├── constants.py         # N&D default stats, skills, currency
│   ├── routers/
│   │   ├── ai.py            # /api/ai/* endpoints
│   │   └── characters.py    # /api/characters/* endpoints
│   └── templates/           # Jinja2 HTML templates
│       ├── ai_chat.html     # AI chat + Image Gen + Models tabs
│       ├── imagestudio.html # SwarmUI iframe page
│       └── characters/      # Character sheet, list, form
├── static/
│   └── style.css            # All app styles
├── docker-compose.yml       # Linux / Windows stack (named volumes)
├── truenas-compose.yml      # TrueNAS SCALE stack (bind mounts)
├── Dockerfile
├── requirements.txt
└── install-comfyui-manager.sh
```

---

## Ports Reference

| Service | Port | Compose file |
|---------|------|--------------|
| nd-world | 8080 | `docker-compose.yml` |
| nd-world | 8087 | `truenas-compose.yml` |
| SwarmUI | 7801 | both |
| Ollama | 11434 | both |

---

## Troubleshooting

**AI dot stays grey / chat returns errors**
- Open **AI → 🤖 Models** — the Ollama status indicator shows whether the service is reachable and the URL it's connecting to
- Check the Ollama container is running: `docker compose ps`
- View Ollama logs: `docker compose logs ollama`
- If you changed `OLLAMA_URL` to point at a host Ollama, make sure it's not `localhost` (which resolves inside the container) — use your LAN IP instead

**No models in the Models tab**
- Click **↺ Refresh** or go to the **Popular Models** grid and click a chip to download one
- The model list is empty until at least one model is pulled

**Image generation returns an error**
- Check backend status at `/api/ai/imagegen/status` in your browser
- SwarmUI first-run can take 10–15 minutes — check logs: `docker compose logs -f swarmui`
- Confirm at least one checkpoint model is present in the models directory
- The Image Gen model dropdown refresh button (↺) rescans the SwarmUI model folders

**Image Studio iframe is blank or shows connection refused**
- `SWARMUI_EXTERNAL_URL` must be a URL your **browser** can reach — not an internal Docker hostname like `http://swarmui:7801`
- Use your LAN IP: `SWARMUI_EXTERNAL_URL=http://192.168.1.xxx:7801`
- On Windows, ensure the Windows Firewall allows port 7801

**Models or LoRAs not showing in Image Gen dropdowns**
- The scanner uses `depth: 10` for nested subfolders — make sure the files are inside the SwarmUI models directory
- Click the ↺ refresh button next to the dropdown to force a rescan

**Database locked / app won't start**
- Only one nd-world instance should write to the same `world.db`
- Recover from a crashed container: `docker compose restart world`

**Port already in use on startup**
- Change the host port in your compose file (left side of the mapping):
  ```yaml
  ports:
    - "8090:8000"   # use 8090 instead of 8080
  ```

**Uploads not persisting after restart**
- Confirm the `/data` volume is mounted correctly; both `world.db` and `uploads/` must be inside it
- For TrueNAS: check the dataset path exists and the container has write permissions

**SwarmUI models directory is empty after restart (TrueNAS)**
- Make sure the models bind mount is correct in `truenas-compose.yml`
- The bind path must exist on the host before the container starts: `mkdir -p /mnt/DeadPool/apps/swarmui/models/Stable-Diffusion`
