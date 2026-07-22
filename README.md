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
- **Rules-driven Player Character creation wizard** — guided Race → Profession → Stats (point-buy) → Feats → Equipment flow implementing the Neon & Dragons Core Rules character creation procedure, backed by the same race/profession/feat/equipment catalog used by the NeonDragonsApp Android app and NeonDragonsEditor desktop tool
- **`.ndc` character export** — export any Player Character as a `.ndc` file importable directly into both the NeonDragonsApp Android app and the NeonDragonsEditor desktop editor (see [Character creation wizard & export](#character-creation-wizard--export))
- **GM & player accounts** — one GM account per deployment; invite players by link, each managing their own character. GMs can hide spoiler content, toggle party-wide character visibility per world, and send private per-player notes (see [Accounts, Invites & Going Public](#accounts-invites--going-public))
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
- [Character Creation Wizard & Export](#character-creation-wizard--export)
- [Accounts, Invites & Going Public](#accounts-invites--going-public)
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

Ollama and SwarmUI are **included in the Docker stack but off by default** — see
[AI Setup](#ai-setup) to enable one or both; no separate installation needed either way.

---

## Install on Linux

These instructions work on **Ubuntu 22.04/24.04**, **Debian 12**, and most other modern distros.

> **Fast path:** if Docker is already installed, `bash scripts/setup.sh` does Steps 2–6
> below for you (creates `.env`, sets up your GM login, starts the stack) — see
> [Accounts, Invites & Going Public](#accounts-invites--going-public) and
> [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

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

By default this starts just **nd-world** (the app) — **SwarmUI** (image gen) and
**Ollama** (AI chat) are optional and skipped unless enabled (see
[AI Setup](#ai-setup) — set `COMPOSE_PROFILES` in `.env`, or use `bash
scripts/setup.sh`, which asks).

Watch the logs:
```bash
docker compose logs -f
```

If you enabled SwarmUI, it performs a first-run setup on initial boot (downloads the ComfyUI backend). This can take **5–15 minutes** depending on your connection.

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
| `SECRET_KEY` | _(random each restart)_ | Signs session cookies — set a fixed value in production (`openssl rand -hex 32`), or logins won't survive a restart |
| `GM_EMAIL` / `GM_PASSWORD` | _(empty)_ | Bootstraps the GM account on first start. Leave blank if the GM account already exists |
| `GM_NAME` | `GM` | Display name for the bootstrapped GM account |
| `COOKIE_SECURE` | `false` | Set `true` once served over HTTPS (see [Accounts, Invites & Going Public](#accounts-invites--going-public)) |
| `COMPOSE_PROFILES` | _(empty)_ | Not read by the app itself — Docker Compose reads it to decide which optional services to start. Empty starts just `world`; set `ollama`, `swarmui`, or `ollama,swarmui` to also start those containers |

---

## AI Setup

Ollama (AI chat) and SwarmUI (AI image generation) are **optional** — nd-world runs
fine without either and just shows those features as unavailable (grey status dot,
"AI unavailable"). They're skipped by default; `bash scripts/setup.sh` asks whether
to enable them, or set `COMPOSE_PROFILES` in `.env` yourself (see table above) and
run `docker compose up -d`. Both are sizeable downloads and Ollama in particular
wants a decent CPU/GPU, so it's worth leaving off if you don't plan to use AI chat.

### Ollama (chat)

Ollama is defined in both `docker-compose.yml` and `truenas-compose.yml` behind the
`ollama` Compose profile — it only starts if that profile is active.

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

SwarmUI is defined in all compose files behind the `swarmui` Compose profile — see
[AI Setup](#ai-setup) above to enable it. On first boot it downloads the ComfyUI backend — this takes **5–15 minutes** the first time.

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

## Character Creation Wizard & Export

`/characters/new` is a guided, rules-driven Player Character creation wizard implementing the
Neon & Dragons Core Rules character creation procedure:

1. **Name** — character/player name, portrait
2. **Race** — pick from the Standard / Advanced / Exceptional tiers (Consumed by Yellow also picks a Base Race)
3. **Profession** — one of the six N&D professions
4. **Stats** — 20-point allocation across the 8 stats using one of the three Physical/Mental splits
   (Eldritch uses a single 16-point pool; Advent AI allocates 10 points across mental stats only,
   with physical stats locked at 0), with a live derived-stats preview (HP/Shock/CA/Speed/PP/MP)
5. **Feats** — required Race, Profession, and Common feat picks plus Free Feat slot(s) (2 for Humans),
   auto-granted Edge-rank Race/Profession feats, Child of the Black Goat's 16 Ritual feats, and
   Psyonic's 4 initial Rank 0 psy powers
6. **Equipment** — spend a 5000-credit starting budget, with Weapons/Armor restricted to
   creation-eligible rarities (Simple/Standard)

The wizard is backed by a bundled copy of the same race/profession/feat/equipment catalog
(`app/game_data/*.json`) used by the **NeonDragonsApp** Android app and **NeonDragonsEditor**
desktop tool (from the sibling `UoY-Neon-Dragons` rules repo), so characters created here use the
same content IDs as those two apps.

### Exporting to NeonDragonsApp / NeonDragonsEditor

Every character sheet has an **⬇ Export .ndc** button (`GET /characters/{id}/export.ndc`) that
downloads a `.ndc` file — the same interchange format used by NeonDragonsApp and NeonDragonsEditor
for their own character import/export. The file can be imported directly into:

- **NeonDragonsApp** (Android) — Character List → import, or via the app's file share/open intent
- **NeonDragonsEditor** (desktop) — Manage Characters → Import

No changes are required in either app — both already accept a bare JSON array of character objects
in this schema (NeonDragonsApp's legacy decode path, NeonDragonsEditor's native multi-character
format), so nd-world only needs to produce a matching file.

### Refreshing the bundled game-data catalog

`app/game_data/*.json` is a point-in-time copy of `NeonDragonsApp/app/src/main/assets/data/*.json`
from the `UoY-Neon-Dragons` repo. When race/profession/feat/equipment content changes there
(after running `extract_all_data.py`), refresh the copy here:

```bash
cp /path/to/UoY-Neon-Dragons/NeonDragonsApp/app/src/main/assets/data/*.json app/game_data/
```

---

## Accounts, Invites & Going Public

There is no public signup. One **GM account** runs the whole deployment, bootstrapped
from the `GM_EMAIL`/`GM_PASSWORD` environment variables on first start (`bash
scripts/setup.sh` prompts for these interactively and writes them to `.env`). Every
other account is a **player**, created by redeeming a GM-issued invite link — there's
no way to sign up otherwise.

**As the GM**, open a world's Edit page (world switcher → ⚙ Manage worlds → Edit) to:
- Create/revoke **Invite Links** (optionally time- or use-limited) and share the
  `/join/<code>` URL with a player — opening it lets them create an account (or log
  in) and joins them to that world
- View and remove **Members**, and open **🔒 Notes** next to any member for a
  private note thread with that player (e.g. session hooks meant only for them —
  visible to you and them, never to the rest of the party)
- Toggle whether **players can see each other's characters** (party roster, read-only)
  for that world

**As a player**, once joined to a world you can:
- Browse its lore (anything the GM hasn't marked "🔒 Hide from players" on the
  entity's edit page — spoilers, secrets, and unrevealed content stay GM-only)
- Create and manage **one character** via the [creation wizard](#character-creation-wizard--export),
  including live HP/Shock/PP/MP tracking and `.ndc` export
- View party members' characters read-only, if the GM has enabled that
- Read private notes the GM has written to you (**🔒 My Notes** in the nav)

GM-only tools (AI chat, image generation, world/entity editing, maps, schematics,
investigation boards) stay restricted to the GM account regardless. Session logs and
campaign-wide notes shared with players don't need a separate feature — use a
**Note** entity (kind "note", subtype "session note"/"lore"/etc.) like any other piece
of world content; toggle "Hide from players" off to share it with the party.

**Going public:** nd-world is private by default (every page requires login) — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full walkthrough of running it
locally and then exposing it to the internet via a **Cloudflare Tunnel** (recommended
— no router changes needed) so you can send invite links to players who aren't on
your home network.

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
│   ├── auth.py              # Password hashing, session/permission dependencies
│   ├── game_catalog.py      # Race/profession/feat/equipment catalog loader for the PC wizard
│   ├── game_data/           # Bundled races/professions/feats/equipment JSON (from UoY-Neon-Dragons)
│   ├── routers/
│   │   ├── ai.py            # /api/ai/* endpoints
│   │   ├── auth.py          # /login, /logout, /join/{code} (invite redemption)
│   │   └── characters.py    # /api/characters/* endpoints, .ndc export
│   └── templates/           # Jinja2 HTML templates
│       ├── ai_chat.html     # AI chat + Image Gen + Models tabs
│       ├── imagestudio.html # SwarmUI iframe page
│       ├── auth/            # Login, join/signup pages
│       └── characters/      # Character sheet, list, creation wizard, edit form
├── static/
│   └── style.css            # All app styles
├── scripts/
│   └── setup.sh             # One-command install: Docker check, .env + GM account, start stack
├── docs/
│   └── DEPLOYMENT.md        # Local setup + Cloudflare Tunnel walkthrough
├── docker-compose.yml       # Linux / Windows stack (named volumes)
├── truenas-compose.yml      # TrueNAS SCALE stack (bind mounts)
├── .env.example             # SECRET_KEY / GM_EMAIL / GM_PASSWORD / COOKIE_SECURE template
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
