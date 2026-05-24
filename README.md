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

## Requirements

| Requirement | Notes |
|-------------|-------|
| Docker + Docker Compose | v2+ recommended |
| Ollama | For AI chat — run separately or use the bundled compose |
| SwarmUI or ComfyUI | Optional — for AI image generation |
| GPU | Optional — CPU-only mode works for both Ollama and SwarmUI |

---

## Quick Start (Docker Compose)

### 1. Clone the repository

```bash
git clone https://github.com/8bit-boom/nd-world.git
cd nd-world
```

### 2. Configure environment variables

Open `docker-compose.yml` and edit the `environment` section under the `world` service:

```yaml
environment:
  DB_PATH: /data/world.db

  # Ollama (AI chat)
  OLLAMA_URL: http://host.docker.internal:11434   # point to your Ollama instance
  OLLAMA_MODEL: gemma4:26b                         # model to use by default

  # Image generation (optional)
  IMAGEGEN_TYPE: swarmui        # "swarmui" or "comfyui"
  IMAGEGEN_URL: http://swarmui:7801   # internal URL (container-to-container)
  SWARMUI_EXTERNAL_URL: ""      # set to http://<your-host>:7801 for the iframe
```

> **Tip:** `SWARMUI_EXTERNAL_URL` is the URL your *browser* uses to reach SwarmUI. It must be reachable from your device, not just from inside Docker.

### 3. Start the stack

```bash
docker compose up -d
```

This starts:
- **nd-world** on [http://localhost:8080](http://localhost:8080)
- **SwarmUI** on [http://localhost:7801](http://localhost:7801)

### 4. Open the app

Navigate to [http://localhost:8080](http://localhost:8080) and create your first world.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `/data/world.db` | Path to the SQLite database file |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | URL of your Ollama instance |
| `OLLAMA_MODEL` | `gemma4:26b` | Default LLM model for AI chat |
| `IMAGEGEN_TYPE` | _(empty)_ | Image generator: `swarmui` or `comfyui` |
| `IMAGEGEN_URL` | _(empty)_ | Internal URL of the image generator API |
| `SWARMUI_EXTERNAL_URL` | _(empty)_ | Browser-accessible SwarmUI URL for the Image Studio iframe |
| `ND_ALLOWED_HOSTS` | `*` | Comma-separated list of allowed `Host` headers (security) |

---

## TrueNAS SCALE Setup

Use `truenas-compose.yml` instead of the default compose file. It uses pre-built images from GHCR and bind-mounts host paths for persistent storage.

### 1. Create storage directories

```bash
mkdir -p /mnt/DeadPool/apps/nd-world
mkdir -p /mnt/DeadPool/apps/swarmui/{data,models,dlbackend}
```

Adjust the base path (`/mnt/DeadPool/apps/`) to match your pool and dataset layout.

### 2. Edit the compose file

Open `truenas-compose.yml` and update:

```yaml
services:
  world:
    environment:
      SWARMUI_EXTERNAL_URL: "http://<truenas-ip>:7801"   # your TrueNAS IP
```

### 3. Deploy via Custom App

In TrueNAS SCALE → **Apps** → **Discover Apps** → **Custom App**:

1. Set the app name (e.g., `nd-world`)
2. Paste the contents of `truenas-compose.yml` into the compose editor
3. Click **Install**

TrueNAS will register portal links for both nd-world (port 8087) and SwarmUI (port 7801) automatically via the `net.ix-portals.*` labels.

### 4. Auto-updates (Watchtower)

The `truenas-compose.yml` includes a **Watchtower** service that checks for new image versions every 5 minutes and restarts containers automatically.

---

## Running Without Docker (Development)

### 1. Install Python 3.12+

```bash
python --version   # must be 3.12 or newer
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

```bash
export DB_PATH=./data/world.db
export OLLAMA_URL=http://localhost:11434
export OLLAMA_MODEL=gemma4:26b
```

### 4. Run the server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The app will be available at [http://localhost:8000](http://localhost:8000). The database and `uploads/` directory are created automatically on first run.

---

## AI Setup

### Ollama (chat)

1. Install Ollama from [ollama.com](https://ollama.com)
2. Pull a model:
   ```bash
   ollama pull gemma4:26b
   ```
3. Set `OLLAMA_URL` to point to your Ollama instance and `OLLAMA_MODEL` to the model name.

The AI dot in the top-right of the nav bar shows green when Ollama is reachable.

You can manage models from the **AI** page in the app (add custom models, pull from Ollama registry, remove).

### SwarmUI (image generation)

SwarmUI is included in both compose files and starts automatically. On first boot it will download its backend (ComfyUI) — this can take several minutes.

To enable the **Image Studio** embedded view, set:
```
SWARMUI_EXTERNAL_URL=http://<host-ip>:7801
```

The image gen panel at **AI → Image Gen** supports:
- Model selection (checkpoint), size presets, custom W×H
- Sampler, scheduler, steps, CFG scale, seed
- Batch generation (1–4 images)
- LoRA, VAE, CLIP skip
- Image-to-image (img2img) with denoising strength

### ComfyUI (alternative)

Set `IMAGEGEN_TYPE=comfyui` and point `IMAGEGEN_URL` at your ComfyUI instance (default port 8188). ComfyUI must have a checkpoint loaded at startup.

### ComfyUI-Manager on SwarmUI

If SwarmUI is installed on a TrueNAS host (outside Docker), run the included helper script to install ComfyUI-Manager:

```bash
bash install-comfyui-manager.sh
# or specify the SwarmUI path manually:
bash install-comfyui-manager.sh /path/to/SwarmUI
```

The script auto-detects SwarmUI across common install locations and clones ComfyUI-Manager into `dlbackend/ComfyUI/custom_nodes/`.

---

## Data & Backups

All data is stored in a single SQLite file (`world.db`) and an `uploads/` directory alongside it. Both live in the `/data` volume.

### Export a world

Navigate to **Export** in the nav bar. The export is a self-contained JSON file with all entities and images encoded in base64. Download it and keep it safe.

### Import a world

On the **Worlds** management page, use the Import option and upload a previously exported JSON file.

### Manual backup (Docker)

```bash
docker cp nd-world:/data ./backup-$(date +%Y%m%d)
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
├── docker-compose.yml       # Development stack
├── truenas-compose.yml      # TrueNAS SCALE production stack
├── Dockerfile
├── requirements.txt
└── install-comfyui-manager.sh
```

---

## Ports Reference

| Service | Default Port | Notes |
|---------|-------------|-------|
| nd-world (dev) | 8080 | Maps to internal port 8000 |
| nd-world (TrueNAS) | 8087 | Pre-built image from GHCR |
| SwarmUI | 7801 | Image generation UI |

---

## Troubleshooting

**AI dot stays grey**
- Check that Ollama is running: `curl http://localhost:11434/api/tags`
- Verify `OLLAMA_URL` is correct and reachable from inside the container

**Image generation returns an error**
- Open `/api/ai/imagegen/status` in your browser to see the backend status
- Make sure `IMAGEGEN_TYPE` and `IMAGEGEN_URL` are both set
- For SwarmUI: check that the SwarmUI container has finished its first-run setup

**Image Studio iframe is blank**
- `SWARMUI_EXTERNAL_URL` must be the URL your browser can reach, not the internal Docker hostname
- Check that port 7801 is accessible from your device

**Database locked / app won't start**
- Only one instance of nd-world should write to the same `world.db` at a time
- If the container crashed mid-write, try restarting: `docker compose restart world`

**Uploads not persisting after restart**
- Make sure the `/data` volume is mounted; both `world.db` and `uploads/` live there
