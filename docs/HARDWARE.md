# Using the server's hardware

Written for a typical home server running nd-world: an 8-core CPU (Ryzen 5800X), 40 GB of RAM and a 16 GB passive GPU (Tesla T10). Most of
it applies to any machine; the numbers in brackets are what nd-world picks on that one.

## CPU

| What | How it uses the cores | Setting |
|---|---|---|
| Building pages | At most `ND_RENDERS` pages at once (default 2). Page building holds Python's interpreter lock, so more parallel pages queue rather than speed up - measured on 4 cores: 2 slots gave 28 pages/s, 8-40 gave 6. Pages take 10-40 ms. | `ND_RENDERS` - measure before changing (below) |
| Picture work: thumbnails, opened-picture copies, the thumbnail remake | Half the usable cores, 1 to 8 (**8** on a 5800X's 16 threads; a Docker `cpus:` limit counts): Pillow releases the lock while it resizes and encodes, so these really run in parallel, and half the cores stay free for pages. Measured: 16 thumbnails remade in 4.4 s with 1 worker, 2.5 s with 3. | `ND_IMAGE_WORKERS` |
| Map tiling of uploaded maps | 2 maps at once with 16 GB of RAM or more (**2**), else 1 - one 72 MP map needs ~0.7 GB while it is cut. | `ND_TILE_WORKERS` |
| Video conversion (AV1, SVT-AV1) | All cores, by ffmpeg itself. | - |

### Should `ND_RENDERS` be higher on an 8-core machine?

Measure it on the server - `scripts/perf_smoke.py` signs in and times pages, including 16 browsers at once:

    ND_RENDERS=2 docker compose up -d world && python scripts/perf_smoke.py --base http://<server>:8000 --email <gm> --password '<pw>'
    ND_RENDERS=4 docker compose up -d world && python scripts/perf_smoke.py ...

Keep the value with more pages per second and the lower worst case in the "16 browsers" line. On the 4-core test machine 2 won; an 8-core
machine may gain a little from 3-4, never from much more.

## GPU (16 GB)

The 16 GB of video memory, not the GPU's speed, decides what runs smoothly: the chat model, speech-to-text (Unsloth Studio) and image
generation (SwarmUI) all want to be in it, and whatever does not fit is unloaded and loaded again (seconds to a minute each time).

- **The default chat model** (`gemma4:26b` / `gemma-4-26B-A4B`) is a mixture-of-experts model: 26 B parameters, about 4 B used per word.
  Quantised it is about as big as the whole card. Because so few parameters work per word, letting Ollama keep part of it in system RAM
  costs much less speed than with an ordinary model - with 40 GB of RAM that is the way to keep speech-to-text on the GPU at the same time.
  Settings -> AI detects the card and sizes the context and offload for it (the "Hardware preset" list is only for planning a card that
  is not installed yet).
- **For the fastest replies instead**, use a model of about 12 B (fits with room to spare) and keep it loaded: `OLLAMA_KEEP_ALIVE=-1` in
  `.env` (never unload) and `OLLAMA_MAX_LOADED_MODELS=1`.
- **On a session night**, what matters is that transcription and the recap do not fight the chat model for the card: with the 26 B model
  partly in RAM (above) both fit; with SwarmUI generating at the same time something has to wait - generate images before the session.
- The T10 is passive: let the System Monitor page's fan control follow its temperature, or it throttles under long transcription runs.

What does not help: the T10's video encoder does H.264 / HEVC only (no AV1 - that needs a newer card), and the image's ffmpeg is a CPU
build; the 5800X encodes AV1 fast enough. Picture resizing on the GPU would cost more in copying than it saves.

## RAM

40 GB is far more than nd-world itself needs (~200 MB). The rest is best spent on the models (above). SQLite's cache is left small on
purpose: the database file is small enough that the operating system keeps it in memory anyway.
