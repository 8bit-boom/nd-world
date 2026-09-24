# GPU Setup Guide — Ollama, SwarmUI, Whisper, and the NVIDIA V100

How to give nd-world's bundled AI stack (Ollama for chat/recaps/facts,
SwarmUI for image generation, whisper.cpp for transcription) a real GPU —
with a dedicated section for the **Tesla V100**, the Volta-era datacenter
card that is now the cheapest way to get serious local-AI performance.

nd-world's own container never needs the GPU for AI inference — it talks
to Ollama and SwarmUI over HTTP (`OLLAMA_URL`/`IMAGEGEN_URL`). Only the
`ollama`, `swarmui`, and (optionally) `whisper` services need full GPU
access. `docker-compose.gpu.yml` also optionally gives nd-world's own
container minimal, `utility`-only GPU access (no real CUDA compute) —
just enough for `nvidia-smi` to work inside it, so Settings → System's
"Detected hardware" panel can auto-detect your real card instead of
relying on a manual VRAM override or hardware preset.

**Only ollama's hardware detection reads the card from `nvidia-smi`.**
SwarmUI has no equivalent panel in nd-world — it manages its own backend
and VRAM usage independently once it has GPU access, so there's nothing
further to configure on the nd-world side for it beyond the passthrough
itself.

---

## 1. Which V100 do you have?

Check on the GPU host (the machine/VM that runs the `ollama` container):

```sh
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
```

| Output name | VRAM | Notes |
|---|---|---|
| `Tesla V100-PCIE-16GB` | 16 GB | PCIe card, 300 W, active cooler or blower |
| `Tesla V100-SXM2-16GB` | 16 GB | SXM2 module — needs a server mainboard or an SXM2→PCIe adapter |
| `Tesla V100-SXM2-32GB` / `PCIE-32GB` | 32 GB | The LLM sweet spot at current used prices |
| `TITAN V` | 12 GB | Same Volta architecture, consumer board |

The 16 GB vs 32 GB answer changes which models fit fully in VRAM — see
§5.

## 2. Host prerequisites (any Linux Docker host)

1. **NVIDIA driver ≥ 550** installed on the host (`nvidia-smi` must work
   before Docker gets involved). On TrueNAS SCALE, install the driver via
   the NVIDIA system app; on Ubuntu, `sudo ubuntu-drivers install nvidia:580-server`
   or the `.run` file from NVIDIA.
2. **nvidia-container-toolkit**:

   ```sh
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt update && sudo apt install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   ```

3. **Verify**: `docker run --rm --gpus all nvidia-smi` should list your
   card.

### ⚠️ Driver upgrades and Volta — read before you upgrade

NVIDIA has **sunset the Volta architecture at the driver level**: the
**580 driver branch is the last one to support V100**. Do not blindly
upgrade to the next major driver branch — check the release notes first.
Similarly, **CUDA 13 removed Volta (compute capability 7.0)**. Ollama's
current release images still build on CUDA 12 and support Volta
(Ollama's documented floor is compute capability 5.0, with V100
explicitly listed), but if a future `ollama/ollama` image moves to CUDA
13 and your GPU disappears from the logs, **pin the last CUDA 12-based
image tag** in your compose file:

```yaml
services:
  ollama:
    image: ollama/ollama:0.12.9   # example — use the newest tag that still works
```

Watchtower users (TrueNAS): by default Watchtower auto-updates *every*
running container, `ollama`/`swarmui` included — an unpinned
`ollama/ollama:latest` or `swarmui:latest` moving to a newer CUDA version
would otherwise silently break inference on an older card between one
restart and the next, with no error until the next generation attempt.
`truenas-compose.yml`'s `ollama` and `swarmui` services both carry a
`com.centurylinklabs.watchtower.enable: "false"` label for exactly this
reason, so pinning the image tag above is what actually decides the
version you run — Watchtower won't override it.

## 3. Wiring it up

### Plain Linux Docker

```sh
docker compose -f docker-compose.yml --profile ollama --profile swarmui \
  -f docker-compose.gpu.yml up -d
docker compose exec ollama ollama pull gemma4:26b
docker compose logs ollama | grep -i "inference compute"   # should say CUDA / 0
docker compose logs swarmui | grep -i cuda                 # should mention your GPU, not "cpu"
```

`docker-compose.gpu.yml` (in this repo) contains exactly this: the NVIDIA
device reservation for `ollama` and `swarmui`, a matching `utility`-only
reservation for `world` (nd-world's own container, so its hardware
detector can see the card — see above), plus a commented CUDA whisper
switch. SwarmUI installs its own backend on first start (the "performs a
first-run setup" step in the README) — that first-run install is what
actually detects and uses the passed-through GPU, so give it a few
minutes before checking the logs above.

### TrueNAS SCALE

**⚠️ TrueNAS 25.10 "Goldeye" and later dropped Volta (V100) support from
the official *Nvidia Driver* app** — it now ships NVIDIA's open-source
kernel modules, which only support Turing-and-newer GPUs. On 25.10+ you
have three options before anything else here will work:

- **Stay on, or roll back to, TrueNAS 25.04 "Fangtooth" or earlier**,
  where the official driver app still supports Volta. Simplest if you
  haven't already moved past it.
- **Use the community driver sysext** —
  [truenas-community-sysexts/nvidia-driver-support](https://github.com/truenas-community-sysexts/nvidia-driver-support)
  builds the proprietary `legacy-580` driver branch (the last one to
  support Volta) for 25.10+. Unofficial, and needs re-running with
  `--rebuild` after any TrueNAS update that changes the kernel. Confirm it
  worked with `docker info | grep -i runtimes` — you should see `nvidia`
  listed.
- **Pass the card through to a separate Ubuntu/Debian VM** and run
  `docker-compose.yml` + `docker-compose.gpu.yml` there instead (see
  "Separate GPU box" below) — sidesteps TrueNAS's driver story entirely at
  the cost of a VM to manage.

Once the driver is sorted (`nvidia-smi` on the TrueNAS host itself lists
the V100):

1. **Give the ollama AND swarmui containers the GPU** — these are two
   separate reservations, not one shared setting. This repo's own README
   documents deploying via **Apps → Discover Apps → Custom App**, pasting
   `truenas-compose.yml` in directly under "Compose" mode — **that pasted-
   YAML Custom App does NOT get a per-app Resources → GPU(s) picker
   screen** (that picker exists for apps built through TrueNAS's own
   catalog/image-launch wizard, a different flow from pasting a full
   compose file). Uncomment the `deploy:` block already scaffolded under
   **both** the `ollama` and `swarmui` services in `truenas-compose.yml`
   yourself, in the YAML you paste — same block under each:

   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: all
             capabilities: [gpu]
   ```

   If you instead built the app through TrueNAS's catalog/wizard flow
   (which does offer that screen for apps created that way), assign the
   GPU to **each** service there individually and leave both YAML blocks
   commented — don't do both for the same service. A single V100 can be
   assigned to more than one container this way; see §3a below for what
   that means for VRAM.
2. **Verify**: shell into each container (find its real name with
   `docker ps` — TrueNAS names a Custom App's containers
   `ix-<app-name>-<service>-1`) and run `nvidia-smi` inside it — it must
   list the V100 — then check `docker logs <that container>` for
   `inference compute` detection on startup (ollama) or a CUDA/GPU mention
   rather than "cpu" (swarmui, after its first-run backend install
   finishes — give it a few minutes). Also confirm `CUDA_VISIBLE_DEVICES`
   isn't set to an empty string inside either container (`docker exec
   <container> env | grep ^CUDA_VISIBLE`) — an explicitly-empty value
   hides every GPU from CUDA even with the deploy block correctly in
   place; `truenas-compose.yml` no longer sets this by default for
   exactly that reason (see its `ollama` service's own `environment:`
   comment), but a `.env` override could still reintroduce it.
3. The nd-world **app container itself gets no GPU by default**, and
   there's no `utility`-only overlay for it on TrueNAS the way
   `docker-compose.gpu.yml` provides for plain Docker hosts — Settings →
   System's "Detected hardware" panel will show no GPU here regardless.
   Set **Ollama VRAM (MB)** to `16384`/`32768` manually (or pick the
   matching V100 preset in that same panel) so the tuning recommendations
   size correctly without needing `nvidia-smi` access from nd-world's own
   container.
4. **Watchtower**: `truenas-compose.yml`'s `ollama` and `swarmui` services
   both carry a `com.centurylinklabs.watchtower.enable: "false"` label —
   Watchtower auto-updates every other container by default, and an
   unpinned image update silently moving to a newer CUDA version would
   otherwise break inference on this card with no error until the next
   generation attempt. Pin the image tag once you've confirmed a version
   works (see the callout above §3).

### Separate GPU box

Nothing about nd-world changes — point `OLLAMA_URL` at the GPU machine
(`http://gpu-box:11434`) and set up that machine like a plain Linux host
above. The Settings → System URL override works too. If SwarmUI is on
that same separate box, point `IMAGEGEN_URL` at it too
(`http://gpu-box:7801`) — same idea.

## 3a. Sharing one V100 between Ollama and SwarmUI

A device reservation (`capabilities: [gpu]`) doesn't reserve the card
exclusively — it just grants that container access to it. Assigning the
same physical V100 to both `ollama` and `swarmui` (the normal single-GPU
setup) works: CUDA lets multiple processes share one GPU, each with its
own context. What's actually shared and finite is **VRAM**, not access.

On a **16 GB** card, running a chat model and generating an image at the
*exact same moment* can hit a CUDA out-of-memory error if both together
exceed 16 GB:

- A 12B-class Ollama model (the §5 recommendation for 16 GB) typically
  resident at ~7–9 GB with `q8_0` KV cache.
- An SDXL-class SwarmUI checkpoint is commonly ~6–8 GB; Flux-class models
  run noticeably higher (often 12+ GB) unless SwarmUI is using a quantized
  build.

Neither one keeps VRAM reserved forever, though:

- **Ollama** evicts an idle model after `OLLAMA_KEEP_ALIVE` (§4) — set it
  shorter (e.g. `5m`) if you regularly alternate between chatting and
  generating images and want the previous one's VRAM back sooner, at the
  cost of a reload delay on the next chat message.
- **SwarmUI** only loads a checkpoint into VRAM while actively rendering
  (or per its own backend idle-unload setting, if you've changed it) —
  check its Backends tab if you want to tune that further.

If real simultaneous use (a GM generating an NPC portrait mid-chat) is
common at your table and you hit OOM errors, the practical fixes are: pick
a smaller/quantized SwarmUI checkpoint, drop to an 8–9B Ollama model
instead of 12B, or shorten `OLLAMA_KEEP_ALIVE` so the two rarely overlap
in practice. A 32 GB V100 removes this concern almost entirely — see §5.

## 4. Optimization — what to actually set

The single best place is **Settings → System → "Ollama server
tuning"** in nd-world (written to the ollama container's env on save —
no restart of nd-world):

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_FLASH_ATTENTION` | `1` | Works on Volta (llama.cpp ships Volta mma kernels) — faster and enables KV quantization |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves KV-cache memory for <5% speed loss — effectively doubles the context that fits in VRAM |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time; a V100 is fast per-token, not wide — parallel slots multiply KV memory |
| `OLLAMA_MAX_LOADED_MODELS` | `1` (16 GB) / `2` (32 GB) | Keep the big model resident instead of thrashing |
| `OLLAMA_KEEP_ALIVE` | `30m` | Model stays warm between session pages |

Also in nd-world's `.env` for the **app** container:

```
OLLAMA_JOB_CONCURRENCY=1   # serialize recap/facts/assist jobs behind the one GPU
```

Per-model overrides (Settings → System → per-model): `num_gpu` 999
(offload everything) once the model fits; leave blank when it doesn't
and let Ollama's splitter place layers.

**Numbers to expect** (single V100 PCIe, Q4_K_M, flash attention on):
a 12B-class model runs ~30–45 tok/s fully offloaded; a 26B (only fully
resident on 32 GB) runs ~15–20 tok/s. Partial offload on 16 GB (26B
split with RAM) drops to ~2–6 tok/s — usable for overnight recaps,
painful interactively.

## 5. Which models fit (Q4_K_M ≈ 0.6 GB per billion params)

| Model class | Weights (Q4) | V100 16 GB | V100 32 GB |
|---|---|---|---|
| 8–9B (gemma-class small) | ~5–6 GB | ✅ fully + 32k context | ✅ trivially |
| 12–14B | ~7–9 GB | ✅ fully + 8–16k context (q8_0 KV) | ✅ + 32k |
| 24–27B (`gemma4:26b`, Qwen 32B is over) | ~15–17 GB | ⚠️ partial offload — slow, or use a Q3/IQ4_XS quant | ✅ fully + 8–16k context |
| 32B+ | ~19 GB+ | ❌ | ⚠️ Q4 32B barely; 27B is the sweet spot |

The nd-world defaults (`gemma4:26b`) want the **32 GB** variant or a
smaller quant. On 16 GB, a 12B-class model at Q4/Q5 with `q8_0` KV gives
a dramatically better experience than a strangled 26B.

The **Models tab** on the AI page shows each installed model's size next
to your VRAM; the benchmark button measures real tok/s after any change.

## 5a. SwarmUI / image generation on the V100

nd-world has no equivalent of Ollama's per-model tuning panel for
SwarmUI — it manages its own ComfyUI backend and VRAM usage independently
once it has GPU access (see §3). What actually moves the needle on a V100
is mostly at the model/quantization level, not a settings panel:

- **GGUF quantization is the single biggest VRAM lever for image models
  on 16 GB**, the same way Q4_K_M is for Ollama. A Krea 2 Turbo GGUF
  build (Q4–Q6) fits comfortably where the full-precision `Comfy-Org/
  Krea-2` checkpoint would not, alongside whatever Ollama model is also
  loaded (see §3a). nd-world's Image Gen tab has a **"⚡ Krea 2 GGUF Quick
  Setup"** panel that downloads a matching diffusion-model + text-encoder
  GGUF pair automatically — the one step it can't do (no filesystem
  access into SwarmUI's own backend) is installing the ComfyUI custom
  node that reads Krea 2's specific GGUF ops: run
  `install-comfyui-gguf-krea2.sh` (repo root) once, on the machine where
  SwarmUI itself runs, not on the nd-world host if they differ. It finds
  your SwarmUI install, removes any conflicting `city96/ComfyUI-GGUF`
  SwarmUI may have already auto-installed (same node/class names — the
  two can't coexist), and clones the Krea-2-aware fork in its place.
- **FP16, not BF16.** Volta's tensor cores only accelerate FP16 (§7) —
  newer cards default to BF16 for numerical stability, which Volta has no
  hardware path for at all (it falls back to slow FP32 math instead of
  erroring, so a wrong-precision run doesn't fail loudly, it just runs far
  slower than it should). SwarmUI/ComfyUI generally auto-detect this
  correctly for a V100, but if a generation is unexpectedly slow, check
  whichever precision setting SwarmUI's backend configuration exposes and
  confirm it's not forcing BF16.
- **Attention backend**: newer flash-attention builds commonly target
  Ampere+ (sm_80+) and either skip Volta or fall back to a slower path —
  the same category of "prebuilt binary silently doesn't cover sm_70" gap
  whisper.cpp's CUDA image hits (§6). If SwarmUI's ComfyUI backend exposes
  extra launch arguments, `--use-pytorch-cross-attention` is the safe,
  universally-supported choice on Volta when the default attention path
  underperforms; xformers wheels are hit-or-miss for sm_70 depending on
  which one gets installed, so don't assume it's faster here the way it
  usually is on newer cards.
- **One GPU, two consumers**: see §3a for VRAM-sharing guidance if you run
  Ollama and SwarmUI on the same V100 (the normal single-card setup).

## 6. Whisper on the GPU

**The prebuilt `ghcr.io/ggml-org/whisper.cpp:main-cuda` image does NOT
work on a V100.** Its own build compiles for
`CMAKE_CUDA_ARCHITECTURES='75;80;86;90'` — Turing and newer only, which
skips Volta (sm_70) entirely. This guide previously said "whisper.cpp's
CUDA build supports Volta," which is true of the *project* but not of
that *specific prebuilt image* — worth calling out explicitly since it's
an easy image to reach for and it fails silently rather than refusing to
start (whisper-server just never finds a usable GPU kernel).

Build the bundled Volta-targeted image instead (already commented in
`docker-compose.gpu.yml`) — a source build of whisper.cpp with
`GGML_CUDA=ON` and `CMAKE_CUDA_ARCHITECTURES=70` pinned for the V100:

```yaml
whisper:
  build: ./docker/whisper-cuda
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
```

(TrueNAS SCALE: use the git-context `build:` line already commented in
`truenas-compose.yml` instead, same as the CPU AVX-512 fix.)

If you ever add a newer card alongside the V100, widen
`CMAKE_CUDA_ARCHITECTURES` in `docker/whisper-cuda/Dockerfile` (e.g.
`"70;75;80;86"`) rather than switching to the prebuilt image — mixing
"prebuilt for everything else, source build just for the V100" isn't
worth the complexity when one build covers both.

**Model file format:** only `ggml-*.bin` files (whisper.cpp's own
format) work here — **not** `.gguf` files, even ones named
"whisper-*-gguf" on Hugging Face. whisper.cpp does not read the GGUF
container format at all (confirmed by its own maintainer); this project
already hit that exact wall once in production (a GGUF file loaded into
`WHISPER_MODELS_DIR` made whisper-server crash-loop with "invalid model
data (bad magic)"), which is why `_looks_like_ggml()` in `app/ai.py` now
refuses to hand one to `/load` at all. For the accuracy of `large-v3` at
roughly half the size/VRAM, use `ggml-large-v3-q8_0.bin` instead — it's
in the Models tab's "⬇ Download Whisper Model" list (Whisper tab on the
AI page), fetched from the same official `ggerganov/whisper.cpp` repo as
every other listed model.

A V100 transcribes `whisper-large-v3-turbo` (or the more accurate
`ggml-large-v3-q8_0.bin` above, once GPU-accelerated) several times
faster than a typical NAS CPU — worth it if you record sessions.

## 7. V100 hardware notes (used cards)

- **300 W** under load — plan PCIe cabling (8-pin EPS/PCIe adapters on
  many SXM2→PCIe adapters) and case airflow.
- **SXM2 modules are passive**: without the server's fan wall they need
  a shroud + high-static-pressure fans pointed at the heatsink, or they
  thermal-throttle within minutes. PCIe V100s usually have their own
  blower.
- Volta tensor cores accelerate **FP16 only** — that's exactly what
  GGUF inference uses, so nothing is lost vs newer cards except their
  newer kernels.
- Check `nvidia-smi -q -d TEMPERATURE,POWER` under load; sustained
  throttling means cooling, not configuration.

---

Back to [README](../README.md) · nd-world docs: [API reference](API_REFERENCE.md)
· [AI entity guide](AI_ENTITY_GUIDE.md) · [AI-everywhere audit](AI_EVERYWHERE_AUDIT.md)
