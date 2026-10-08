# GPU Setup Guide — Unsloth Studio, Ollama/SwarmUI (legacy), and used-market NVIDIA cards (Turing, Ampere, Volta)

How to give nd-world's bundled AI stack a real GPU. **Unsloth Studio** is
the current default backend for chat/recaps/facts AND image generation —
the legacy **Ollama** (chat) + **SwarmUI** (image generation) pair is kept
running behind its own Compose profiles as a rollback path, not deleted, so
most of this guide covers both. Speech-to-text (transcription) runs on
Unsloth Studio too, so it uses the same GPU as chat. Dedicated sections for the
cards worth buying used for local AI: the **Tesla T10** (Turing, 16 GB) — the
cheap default, fully inside the support envelope of every current driver/CUDA
stack; the **Quadro RTX 8000** (Turing, 48 GB) — the big-model card, the whole
default chat model resident on one GPU; the **NVIDIA A16** (Ampere, but *four*
separate 16 GB GPUs on one board — read §3c before buying it for this); and the
**Tesla V100** (Volta), the previous pick, now carrying real end-of-life caveats
(§2's Volta callout).

nd-world's own container never needs the GPU for AI inference — it talks to
whichever backend is active over HTTP (`UNSLOTH_URL`, or the legacy
`OLLAMA_URL`/`IMAGEGEN_URL`). Only the `unsloth` service (or, on the legacy
path, `ollama` and `swarmui`) need full GPU access.
`docker-compose.gpu.yml` also optionally gives nd-world's own container
minimal, `utility`-only GPU access (no real CUDA compute) — just enough for
`nvidia-smi` to work inside it, so Settings → System's "Detected hardware"
panel can auto-detect your real card instead of relying on a manual VRAM
override or hardware preset.

---

## 1. Which card do you have?

Check on the GPU host (the machine/VM that runs the `unsloth` container):

```sh
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
```

**Tesla T10 16GB — the recommended card (Turing, sm_75):**

| Output name | VRAM | Notes |
|---|---|---|
| `Tesla T10 16GB` / `NVIDIA Tesla T10` | 16 GB | Turing OEM datacenter card (~150 W), passive cooled; commonly on secondary markets. Compute capability 7.5 — the *oldest* architecture current CUDA/toolkit builds still fully support |

Turing is the sweet spot for this app on the used market: supported by the
current (and foreseeable) NVIDIA driver branches and CUDA toolkits, gets
llama.cpp's flash attention and its native-INT8 MMQ quantized-kernel path
(Volta doesn't — see §7), and the current `unsloth/unsloth` image ships
sm_75 kernels — it's the first architecture in that build's arch list
(`UNSLOTH_PHASE0_FINDINGS.md` I-7's arch list starts at sm_75). No image
tag pinning, no driver gymnastics: `:latest` just works.

**Tesla V100 — the previous pick (Volta, sm_70):**

| Output name | VRAM | Notes |
|---|---|---|
| `Tesla V100-PCIE-16GB` | 16 GB | PCIe card, 300 W, active cooler or blower |
| `Tesla V100-SXM2-16GB` | 16 GB | SXM2 module — needs a server mainboard or an SXM2→PCIe adapter |
| `Tesla V100-SXM2-32GB` / `PCIE-32GB` | 32 GB | The LLM sweet spot at current used prices |
| `TITAN V` | 12 GB | Same Volta architecture, consumer board |

Both 16 GB cards fit the same models — see §5 (a T10 16GB reads the same
column as a V100 16GB). The V100 generates tokens faster (higher memory
bandwidth); the T10 wins on software longevity and flash attention. If you
have the choice, prefer the T10.

**Quadro RTX 8000 — the big-model card (Turing, sm_75):**

| Output name | VRAM | Notes |
|---|---|---|
| `Quadro RTX 8000` | 48 GB | Turing, 384-bit GDDR6 at 672 GB/s, 295 W. Workstation card with a blower; a passive server variant exists too. Same software story as the T10 (current drivers/CUDA, no driver gymnastics) with three times the memory — the default 26B chat model fits on ONE of these |

**NVIDIA A16 — four GPUs on one board (Ampere, sm_86):**

| Output name | VRAM | Notes |
|---|---|---|
| `NVIDIA A16` — listed **four times** by `nvidia-smi -L` (under NVIDIA vGPU the name usually carries a profile suffix, e.g. `A16-16C`) | 4 × 16 GB, **not pooled** | Four separate GA107 GPUs, each ~200 GB/s, joined by an on-board PCIe Gen4 switch (no NVLink), passive, 250 W. Ampere software (bf16, FlashAttention 2, cuDNN attention) but small, slow GPUs — **and the stack gives Studio only one of the four by default.** See §3c |

**Which one?**

| Card | Arch | Memory | Bandwidth | bf16 / PyTorch FA2 / cuDNN attention | Power / cooling |
|---|---|---|---|---|---|
| Tesla T10 | Turing sm_75 | 16 GB | ~400 GB/s | no / no / no | ~150 W, passive |
| Quadro RTX 8000 | Turing sm_75 | 48 GB | 672 GB/s | no / no / no | 295 W, blower |
| NVIDIA A16 | Ampere sm_86 | 4 × 16 GB | 4 × ~200 GB/s | yes / yes / yes | 250 W, passive |
| Tesla V100 | Volta sm_70 | 16 or 32 GB | ~900 GB/s | no / no / no | 300 W |

Token generation is memory-bandwidth-bound, so bandwidth and *capacity per
GPU* matter more here than architecture: the RTX 8000 is the comfortable one
(everything resident, decent speed), the T10 the cheap one, the V100 the fast
one with an end-of-life clock, and the A16 the awkward one — plenty of total
memory, but in 16 GB pieces on slow GPUs. "FlashAttention 2 / cuDNN attention"
are the *PyTorch-side* kernels; llama.cpp's own flash attention (what GGUF chat
uses) also runs on Turing — see §4.

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

3. **Verify**: `docker run --rm --gpus all nvidia-smi` should list your card.

### ⚠️ Driver upgrades — Volta (V100) owners only, read before you upgrade

**Turing and Ampere cards (T4, T10, Quadro RTX 8000, A16): nothing to do
here — skip this callout entirely.** Turing and Ampere are supported by
current NVIDIA driver branches (no 580 ceiling), by CUDA 13 (which only
dropped Maxwell/Pascal/Volta), and by the current `unsloth/unsloth` image
(sm_75 kernels ship in it; Ampere is newer still). Unpinned `:latest` is safe
on both.

**V100 owners:** NVIDIA has **sunset the Volta architecture at the driver
level**: the
**580 driver branch is the last one to support V100**. Do not blindly
upgrade to the next major driver branch — check the release notes first.
Similarly, **CUDA 13 removed Volta (compute capability 7.0)**. The Unsloth
Studio image currently ships CUDA 12 builds with Volta kernels, but if a
future `unsloth/unsloth:latest` moves to CUDA 13-only and your GPU
disappears from the logs, **pin the last working image tag** in your compose
file:

```yaml
services:
  unsloth:
    image: unsloth/unsloth:<known-good-tag>   # pin before the CUDA 13 cutoff
```

This is the single biggest V100 risk in the migration to Unsloth — finding
I-7 in [UNSLOTH_PHASE0_FINDINGS.md](UNSLOTH_PHASE0_FINDINGS.md): verify that
the tag you deploy actually contains sm_70 kernels before relying on it, and
prefer a pinned tag over `:latest`. (A T10/T4/RTX 8000/A16 makes this entire
callout moot — sm_75 and Ampere kernels ship in every current image.)

Watchtower users (TrueNAS): by default Watchtower auto-updates *every*
running container — on a V100, an unpinned `unsloth/unsloth:latest` (or, on
the legacy path, `ollama/ollama:latest`/`swarmui:latest`) moving to a newer
CUDA version would otherwise silently break inference on this card between
one restart and the next, with no error until the next generation attempt.
`truenas-compose.yml`'s `unsloth`, `ollama`, and `swarmui` services all
already carry a `com.centurylinklabs.watchtower.enable: "false"` label for
exactly this reason, so pinning the image tag above is what actually decides
the version you run — Watchtower won't override it.

## 3. Wiring it up

### Plain Linux Docker

```sh
docker compose --profile unsloth up -d
```

Unsloth's own GPU reservation is unconditional in `docker-compose.yml`
itself, not the `docker-compose.gpu.yml` overlay (Unsloth needs a GPU to be
worth running at all, unlike the optional legacy Ollama/SwarmUI pair) — no
extra `-f` flag needed just to get the card passed through to it. Layer
`docker-compose.gpu.yml` on top only if you also want nd-world's own
container to see the card directly for the "Detected hardware" panel:

```sh
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile unsloth up -d
```

Then do the one-time Studio setup (full detail in
[DEPLOYMENT.md](DEPLOYMENT.md)):

1. Open the Studio UI (port 8000) → log in → Settings → API → create an
   API key.
2. Paste the key into nd-world → ⚙️ Settings → Unsloth backend. AI chat
   and image generation switch over immediately — no restart.
3. Download the chat and image models in Studio's Model Hub.

**Legacy Ollama/SwarmUI path** (rollback, or if you haven't switched yet):

```sh
docker compose -f docker-compose.yml --profile ollama --profile swarmui \
  -f docker-compose.gpu.yml up -d
docker compose exec ollama ollama pull gemma4:26b
docker compose logs ollama | grep -i "inference compute"   # should say CUDA / 0
docker compose logs swarmui | grep -i cuda                 # should mention your GPU, not "cpu"
```

Here `docker-compose.gpu.yml` is required — it's what contains the NVIDIA
device reservation for `ollama` and `swarmui` (a matching `utility`-only
reservation for `world` either way — see above), SwarmUI installs its own backend on first start (the
"performs a first-run setup" step in the README) — that first-run install is
what actually detects and uses the passed-through GPU, so give it a few
minutes before checking the logs above.

### TrueNAS SCALE

**Turing and Ampere cards (T4, T10, Quadro RTX 8000, A16): the stock NVIDIA
driver app on any current TrueNAS — 25.10 "Goldeye" included — supports your
card** (its open kernel modules cover Turing and newer). Skip the following
block entirely and go straight to the deployment steps.

**⚠️ Do NOT put the GPU in "Isolated GPU PCI Ids"** (System → Advanced Settings,
the dialog that lists e.g. `NVIDIA Corporation TU102GL [Tesla T10 16GB …]`).
Isolating hands the card to the VM passthrough driver (vfio) and hides it from
the host, so the NVIDIA driver and every container (Unsloth Studio, Ollama,
SwarmUI) can no longer see it. Isolation is only for giving the whole card to a
virtual machine. For apps: leave the list empty (a display GPU such as a
GT 1030 should stay out of it too), install the driver under **Apps → Settings
→ Install NVIDIA Drivers**, reboot, and check `nvidia-smi` on the TrueNAS shell
lists the T10. If you already isolated it, remove it from that list and reboot.

**⚠️ V100 only: TrueNAS 25.10 "Goldeye" and later dropped Volta support from
the official *Nvidia Driver* app** — it now ships NVIDIA's open-source
kernel modules, which only support Turing-and-newer GPUs. On 25.10+ you
have three options before anything else here will work (Unsloth or legacy
— this is a host driver issue, not backend-specific):

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
the V100), deploying via **Apps → Discover Apps → Custom App**, pasting
`truenas-compose.yml` directly under "Compose" mode (this repo's own
documented path):

1. **Unsloth already gets the GPU with no extra step.** Unlike the legacy
   pair below, `truenas-compose.yml`'s `unsloth` service's
   `deploy.resources.reservations.devices` block is uncommented by default
   — a pasted-YAML Custom App picks it up as soon as the driver/toolkit are
   in place, nothing to uncomment. (If you instead built the app through
   TrueNAS's catalog/wizard flow, which *does* offer a per-app
   Resources → GPU(s) picker screen for apps created that way, assign the
   GPU there instead and comment this block out — don't do both, or you'll
   have two reservations for one container.) That block reserves
   `count: 1` — **one GPU**. On a multi-GPU box, and on an A16 (four GPUs), see
   §3c to give Studio more.
2. **Legacy Ollama + SwarmUI: give both containers the GPU** — these are
   two separate reservations, not one shared setting, and unlike Unsloth's
   block, both start **commented** in `truenas-compose.yml` (a pasted-YAML
   Custom App does NOT get the Resources → GPU(s) picker screen, so nothing
   assigns it for you automatically the way it might for Unsloth's own
   already-uncommented block). Uncomment the `deploy:` block scaffolded
   under **both** the `ollama` and `swarmui` services yourself, in the YAML
   you paste — same block under each:

   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: all
             capabilities: [gpu]
   ```

   A single V100 can be assigned to more than one container this way (all
   three, if you're running Unsloth alongside a legacy rollback instance);
   see §3a below for what that means for VRAM.
3. **Verify**: shell into each container (find its real name with
   `docker ps` — TrueNAS names a Custom App's containers
   `ix-<app-name>-<service>-1`) and run `nvidia-smi` inside it — it must
   list the V100. Then check `docker logs <that container>` for GPU
   detection on startup: the Studio UI's resource panel after loading a
   model (unsloth), `inference compute` (ollama), or a CUDA/GPU mention
   rather than "cpu" (swarmui, after its first-run backend install
   finishes — give it a few minutes). Also confirm `CUDA_VISIBLE_DEVICES`
   isn't set to an empty string inside any of them (`docker exec
   <container> env | grep ^CUDA_VISIBLE`) — an explicitly-empty value
   hides every GPU from CUDA even with the deploy block correctly in
   place; `truenas-compose.yml` no longer sets this by default for exactly
   that reason (see its `ollama` service's own `environment:` comment),
   but a `.env` override could still reintroduce it.
4. The nd-world **app container itself gets no GPU by default**, and
   there's no `utility`-only overlay for it on TrueNAS the way
   `docker-compose.gpu.yml` provides for plain Docker hosts — Settings →
   System's "Detected hardware" panel will show no GPU here regardless.
   Set **VRAM override (MB)** manually — `16384` (T10 / V100 16 / one A16
   GPU), `32768` (V100 32), `49152` (Quadro RTX 8000) — or pick the matching
   card preset (Tesla T10 16GB, V100 16GB or V100 32GB; there is no preset for
   the RTX 8000 or A16) in that same panel, so the tuning recommendations size
   correctly without needing `nvidia-smi` access from nd-world's own container.
   Use the VRAM of the GPUs **Studio is given**, not of the whole card.
5. **Watchtower**: `truenas-compose.yml`'s `unsloth`, `ollama`, and
   `swarmui` services all carry a
   `com.centurylinklabs.watchtower.enable: "false"` label — Watchtower
   auto-updates every other container by default, and an unpinned image
   update silently moving to a newer CUDA version would otherwise break
   inference on this card with no error until the next generation attempt.
   Pin the image tag once you've confirmed a version works (see the
   callout above §2).

### Separate GPU box

Nothing about nd-world changes — point `UNSLOTH_URL` at the GPU machine
(`http://gpu-box:8000`) and set up that machine like a plain Linux host
above. The Settings → System URL override works too. On the legacy path,
point `OLLAMA_URL` (`http://gpu-box:11434`) and, if SwarmUI is on that same
box, `IMAGEGEN_URL` (`http://gpu-box:7801`) there instead.

## 3a. Sharing one V100 between Ollama and SwarmUI (legacy backend)

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

Neither one keeps VRAM reserved forever, though — both sides have their own
idle-eviction timer, and **both default to holding VRAM well past the last
actual generation**, not just while a request is in flight:

- **Ollama** evicts an idle model after `OLLAMA_KEEP_ALIVE` (§4, default
  `5m` unless you've raised it — nd-world's own recommended `30m` in §4
  holds it noticeably longer). Set it shorter if you regularly alternate
  between chatting and generating images and want the previous model's
  VRAM back sooner, at the cost of a reload delay on the next chat message.
- **SwarmUI does the same, not "only while actively rendering."**
  (Confirmed against SwarmUI's own source, `src/Core/Settings.cs`: the
  `BackendData.ClearVRAMAfterMinutes` setting, default **10 minutes**,
  keeps the last-used checkpoint resident in VRAM for that long after the
  *last* generation, precisely to avoid a reload delay on the *next* one —
  functionally the same tradeoff as `OLLAMA_KEEP_ALIVE`, not an
  always-releases-immediately behavior.) It's in SwarmUI's own **Server →
  Settings → Backends** section if you want to lower it — set it to `1` or
  `2` (minutes) to free that VRAM back for Ollama sooner, or `-1` to
  disable the hold entirely and always reload from disk. There's also a
  matching `ClearSystemRAMAfterMinutes` (default 60) for system RAM, not
  VRAM — irrelevant to a CUDA out-of-memory error but worth knowing exists.

If real simultaneous use (a GM generating an NPC portrait mid-chat) is
common at your table and you hit OOM errors, the practical fixes are: pick
a smaller/quantized SwarmUI checkpoint, drop to an 8–9B Ollama model
instead of 12B, or lower `OLLAMA_KEEP_ALIVE` and/or SwarmUI's
`ClearVRAMAfterMinutes` so the two rarely hold VRAM at the same time in
practice — lowering only one side still leaves the other's hold window to
collide with it. A 32 GB V100 removes this concern almost entirely — see §5.

## 3b. SwarmUI's PyTorch on a V100 (legacy backend) — read this before assuming it "just works"

**Turing (T4/T10) users: this section doesn't apply to you** — the current
image stack ships sm_75 torch kernels, so no wheel surgery is needed.
Everything below is about Volta (sm_70) being absent from modern torch
builds.

**A GPU device reservation alone is not enough for SwarmUI.** SwarmUI
installs its own PyTorch at first run (`launchtools/comfy-install-linux.sh`,
run automatically the first time SwarmUI starts with no `dlbackend/ComfyUI`
present yet) — and for an NVIDIA GPU, that script currently does:

```sh
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
```

**CUDA 13 wheels have no Volta (compute capability 7.0 / sm_70) support at
all.** PyTorch dropped Volta from its CUDA 13 builds outright, and even
CUDA 12's own coverage has been narrowing release over release —
**`cu126` is the last CUDA-12.x wheel index that still includes sm_70, and
PyTorch 2.14 is the last version published under it.** A stock SwarmUI
install on a V100, right now, installs a build that flatly cannot use this
card — not a slow fallback, a hard failure. (An earlier version of this
guide said Volta "has remained inside PyTorch's default compiled
compute-capability set… this should just work" — that was wrong for any
SwarmUI install done after this cu130 switch; see below for the fix.)

**Failure signatures:**

| Symptom | Cause | Fix |
|---|---|---|
| `…sm_70 is not compatible with the current PyTorch installation` in SwarmUI/ComfyUI logs at startup | This section | Reinstall torch below |
| `CUDA error: no kernel image is available for execution on the device` on the first generation attempt | Same — the warning above was ignored/missed | Reinstall torch below |
| Ollama: `CUDA error: device kernel image is invalid` | Driver older than 550 (Ollama's CUDA 12 build needs ≥550) | Upgrade the driver — see §2 |

**Check** (run on the SwarmUI host; find the container name with `docker ps`
— TrueNAS names it `ix-<app-name>-swarmui-1`):

```sh
C=$(docker ps --format '{{.Names}}' | grep -i swarmui | head -1)
docker exec "$C" /SwarmUI/dlbackend/ComfyUI/venv/bin/python -c \
  "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_arch_list())"
```

If `sm_70` is **not** in the printed list, fix it:

```sh
docker exec "$C" /SwarmUI/dlbackend/ComfyUI/venv/bin/python -s -m pip uninstall -y torch torchvision torchaudio
docker exec "$C" /SwarmUI/dlbackend/ComfyUI/venv/bin/python -s -m pip install torch torchvision \
  --index-url https://download.pytorch.org/whl/cu126 --no-cache-dir
```

(Only reinstall `torchaudio` too if it was present in the uninstall output —
SwarmUI's own install doesn't request it.) Then restart SwarmUI (nd-world's
🔄 restart button on the Image Gen tab, or `docker restart "$C"`) and
re-run the check to confirm `sm_70` now appears.

A repo-root script does the check-then-fix for you:

```sh
./fix-swarmui-volta-torch.sh
```

**This is not a one-time fix — it doesn't survive everything:**

- **Pinning the SwarmUI image tag does NOT protect you.** Unlike Ollama's
  CUDA version (baked into its image), SwarmUI's torch install lives in
  the `dlbackend` volume/bind-mount, installed by a script the container
  runs at startup — the image tag barely matters here. The
  `com.centurylinklabs.watchtower.enable: "false"` label on `swarmui` in
  `truenas-compose.yml` still matters (an image update changing that
  install script would matter), but it's not sufficient on its own the way
  it is for Ollama.
- **Deleting/reinstalling the `dlbackend` folder** (a fresh SwarmUI
  install, or troubleshooting steps that involve wiping it) re-runs the
  cu130 install and re-breaks it — re-run the check after.
- **Installing any ComfyUI custom node or feature that pulls its own
  torch from PyPI** (rather than SwarmUI's own installer) can also
  silently upgrade to a CUDA-13 build, since PyPI's default `pip install
  torch` now resolves to CUDA 13 wheels. Re-run the check after installing
  new custom nodes, especially ones with their own `requirements.txt`
  that lists `torch`.
- The `cu126`/2.14 pin isn't going anywhere on its own — PyTorch won't
  publish a newer cu126 build that drops Volta, since cu126 itself is
  Volta's last home. It just won't get any NEWER either; that's the real
  tradeoff of keeping this card running at all going forward.

## 3c. Cards with more than one GPU (NVIDIA A16) and multi-card hosts

The **A16 is not one 64 GB card.** It is four GA107 GPUs on one board, each
with its own 16 GB of GDDR6 (~200 GB/s each), joined by an on-board PCIe
Gen4 switch rather than NVLink ([NVIDIA A16](https://www.nvidia.com/en-us/data-center/a16)).
`nvidia-smi -L` lists **four** GPUs, and their memory is not pooled: a model
has to fit on one GPU unless the runtime splits it across several.

**By default Studio gets exactly one of them.** The `unsloth` service in
`docker-compose.yml` and `truenas-compose.yml` reserves `count: 1`, so on an
A16 it sees a single 16 GB GPU and the other three sit idle. Choose on purpose:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 2            # or `all`; OR pick specific GPUs with device_ids — never both:
              # device_ids: ["0", "1"]    # indices or UUIDs from `nvidia-smi -L`
              capabilities: [gpu]
```

A pasted-YAML TrueNAS Custom App takes the same block; the catalog wizard's
Resources → GPU(s) picker lists the A16's four GPUs separately. To hide GPUs
from *inside* the container instead, set `CUDA_VISIBLE_DEVICES` to a
comma-separated list of indices — never an empty string (see §3 step 3). The
same applies to any host with two or more cards (two T10s, two RTX 8000s).

| GPUs given to Studio | What fits (Q4 GGUF) | Notes |
|---|---|---|
| 1 (the default) | up to 12–14B with context | one 16 GB GPU |
| 2 | the 26B-class default chat model (~15–17 GB + context) | llama.cpp can split its layers across two GPUs |
| 4 | 32B, and 70B-class at short context (~40–43 GB) | more capacity, **not** more speed |

**What to expect — and what is not verified here:**

- **Splitting adds capacity, not speed.** llama.cpp spreads a model's layers
  across GPUs (`--split-mode layer`, `--tensor-split`), but each token still
  passes through them one after another, so decode speed stays near one GPU's
  ~200 GB/s. Layer splitting moves little data between GPUs, so the shared
  PCIe uplink should not be the limit. A mixture-of-experts model such as the
  default `gemma-4-26B-A4B` (about 4B active parameters per token, going by its
  name) reads far fewer weights per token than a dense 26B, which is the one
  thing that helps on slow GPUs.
- **nd-world runs its AI tasks one at a time** (the single AI queue), so extra
  GPUs cannot make two requests run in parallel. The only gain is keeping more
  models resident (chat, image, speech) so they don't evict each other.
- **Where Studio puts each model across several GPUs was not checked.** Load
  a model and watch per-GPU memory in `nvidia-smi` to see what it does; if it
  puts everything on GPU 0, narrow its view with `device_ids` /
  `CUDA_VISIBLE_DEVICES` to the GPUs you want it to use.
- **"Detected hardware" adds up every GPU it can see.** From nd-world's
  container an A16 reads as 64 GB while Studio may be using 16 — set **VRAM
  override (MB)** to the VRAM of the GPUs Studio is actually given
  (`16384` per GPU).
- **vGPU:** the A16's main job is virtual desktops (NVIDIA vGPU). If a
  hypervisor already slices it into vGPU profiles, CUDA compute needs a
  compute-capable profile and a vGPU licence; for this stack, hand the whole
  GPUs to the VM/container by passthrough instead (check NVIDIA's vGPU
  documentation — not verified here).

Fine-tuning on several GPUs is a separate matter that nd-world does not use:
Unsloth documents it through `accelerate launch` / `torchrun` (DDP, one model
copy per GPU) or `device_map="balanced"` (one model split across GPUs) — see
[Unsloth's multi-GPU guide](https://unsloth.ai/docs/basics/multi-gpu-training-with-unsloth).

## 4. Optimization — the Turing/Volta profile (fp16 everywhere; flash attention per-arch) and notes for Ampere (A16)

The migration-plan profile, applied in the **Studio UI's model-load
(expert) settings** for each downloaded model:

| Setting | Value | Why |
|---|---|---|
| Load dtype / cache dtype | `fp16` | Volta and Turing accelerate FP16 only — bf16/fp8 kernels don't exist on sm_70 (Volta) *or* sm_75 (Turing; T10, Quadro RTX 8000). **The A16 (Ampere) has bf16** — GGUF chat models run quantized kernels where the choice barely matters, and for PyTorch-side models (image generation, speech) leave Studio's default |
| Attention implementation | **V100:** non-flash (SDPA/pytorch) — FA2/FA3 binaries aren't built for sm_70. **T4/T10/Quadro RTX 8000:** llama.cpp's flash attention (what GGUF chat uses) is fine (sm_75 is where its FA support starts) — leave it enabled. **A16:** leave Studio's default (`auto` picks cuDNN attention on NVIDIA) | Per-architecture — see the note below |
| Max loaded models | `1` (16 GB) / `2` (32–48 GB) | Keep the big model resident instead of thrashing |
| Context length | match `LLM_CONTEXT_TOKENS` (default 16384) | llama.cpp fixes context at load time; the app sizes recap chunking from this value |

**Turing is "flash attention yes, PyTorch flash attention no."** The PyTorch-side
kernels — FlashAttention 2, cuDNN attention (needs Ampere/SM80+) and
SageAttention (official kernels: Ampere, Ada, Hopper) — don't run on
Turing, so anything Unsloth runs through PyTorch there (image generation,
speech-to-text, training) falls back to xformers / PyTorch's memory-efficient
attention. It works and saves the same memory; it just isn't the fast kernel.
Unsloth's `auto` attention setting picks cuDNN on NVIDIA and falls back per
call when a card or call can't use it ([Unsloth PR #12641](https://github.com/unslothai/unsloth/pull/12641)).
An A16 has all of them.

**VRAM eviction expectation:** with 16 GB, expect the Studio to evict or
partially offload when a second large model is loaded — keep the chat model
as the resident one and treat image models as load-on-demand, or accept the
reload latency.

**Training is gated off** on the Turing/Volta profile: full fine-tuning and
LoRA training workloads assume Ampere+ features; use the chat/inference
path only. (An A16 *can* train, and nd-world never does — this guide is about
inference.)

**Numbers to expect** (fp16 GGUF, fully offloaded): on a V100 PCIe, a
12B-class model runs ~30–45 tok/s and a 26B (fully resident on 32 GB)
~15–20 tok/s; partial offload on 16 GB (26B split with RAM) drops to
~2–6 tok/s — usable for overnight recaps, painful interactively. A T10
has notably less memory bandwidth than a V100 (256-bit GDDR6, ~400 GB/s, vs
V100's ~900 GB/s HBM2), so expect token generation roughly in the "half to a
third of the V100 figure" range — its flash-attention and INT8-MMQ
advantages don't change the decode-bandwidth bound. A fully-resident
12B–14B Q4/Q5 is the comfortable interactive tier on either 16 GB card.

Two more, **estimated from memory bandwidth, not measured**: a **Quadro RTX
8000** (672 GB/s) should reach up to about three-quarters of the V100
figures — and, with 48 GB, keeps the 26B-class model fully resident where a
16 GB card must offload, so on that model it is far ahead of a 16 GB V100.
An **A16** GPU (~200 GB/s) decodes at roughly a fifth of the V100 figures on
anything confined to one GPU, and splitting a model over several GPUs does
not speed it up (§3c); budget it as a capacity card for background jobs
(recaps, facts) more than an interactive one.

For nd-world's own app container, keep the AI job queue serialized:

```
OLLAMA_JOB_CONCURRENCY=1   # serialize recap/facts/assist jobs behind the one GPU
MAX_AUTO_NUM_CTX=16384     # cap auto-sized recap/facts/condense context (default 32768)
```

`MAX_AUTO_NUM_CTX` bounds the auto-sized context window background jobs
(session recaps, facts parsing, transcript condensing) pin per-call — its
32768 default is sized for a card with nothing else competing for VRAM. On
a shared 16 GB V100 running an image-generation backend too (see §3a for
the legacy Ollama+SwarmUI case), lowering it to 16384 keeps a worst-case
background job from momentarily claiming enough KV-cache VRAM to starve a
concurrent image generation; on a 32 GB card the default is fine as-is.
This is separate from `OLLAMA_CONTEXT_LENGTH`/Unsloth's own per-model
context setting and the per-request `num_ctx` — those apply per model call,
this is the ceiling those auto-sizing jobs are allowed to pin. (Both env
var names — `OLLAMA_JOB_CONCURRENCY` and `MAX_AUTO_NUM_CTX` — are
historical; both apply to whichever backend is active, Unsloth included.)

The legacy **Detected hardware** panel (Settings → System) also reserves
~6 GB of VRAM in its own per-model recommendations automatically once
image generation is configured for SwarmUI, so the *suggested* `num_ctx`
already accounts for a concurrently-loaded checkpoint — `MAX_AUTO_NUM_CTX`
covers the background jobs that recommendation doesn't size. On Unsloth,
the equivalent per-model context/precision knobs live in Studio's own
model-load settings (see §4's table above), not this panel.

Legacy per-model overrides (Settings → System → per-model, Ollama only):
`num_gpu` 999 (offload everything) once the model fits; leave blank when
it doesn't and let Ollama's splitter place layers.

## 5. Which models fit (fp16 ≈ 2 GB per billion params, GGUF Q4 ≈ 0.6 GB)

| Model class | Weights (Q4 GGUF) | 16 GB (T10, V100 16, one A16 GPU) | 32 GB (V100 32, two A16 GPUs ¹) | 48 GB (Quadro RTX 8000) |
|---|---|---|---|---|
| 8–9B (gemma-class small) | ~5–6 GB | ✅ fully + 32k context | ✅ trivially | ✅ trivially |
| 12–14B | ~7–9 GB | ✅ fully + 8–16k context | ✅ + 32k | ✅ + 32k |
| 24–27B (`gemma-4-26B-A4B-it`) | ~15–17 GB | ⚠️ partial offload — slow, or use a Q3/IQ4_XS quant | ✅ fully + 8–16k context | ✅ fully + 32k |
| 32B | ~19–20 GB | ❌ | ⚠️ barely, short context | ✅ + 16k |
| 70B-class | ~40–43 GB | ❌ | ❌ | ⚠️ tight — short context only |

¹ Two A16 GPUs hold a 32 GB-class model only by splitting its layers across
them (§3c): the capacity is real, the speed stays that of one ~200 GB/s GPU.

VRAM fit depends on capacity, not architecture — a T10 16GB reads the same
column as a V100 16GB and as one GPU of an A16 (the cards differ in *speed*,
§4, not in what fits).

The nd-world default chat model (`unsloth/gemma-4-26B-A4B-it-GGUF`) wants
**32 GB or more** — a V100 32 GB, a Quadro RTX 8000, two A16 GPUs (§3c) — or a
smaller quant. On 16 GB, a 12B-class model at Q4/Q5 gives a dramatically
better experience than a strangled 26B.

The **Models tab** on the AI page shows each installed model's size next to
your VRAM.

## 5a. SwarmUI / image generation on the V100 (legacy backend)

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
- **FP16, not BF16 — and you can check which one actually ran, per model
  load.** Volta's tensor cores only accelerate FP16 (§7); newer cards
  default to BF16 for numerical stability, which Volta has no hardware
  path for at all (it falls back to slow FP32 math instead of erroring,
  so a wrong-precision run doesn't fail loudly, it just runs far slower
  than it should). There's no settings toggle for this — ComfyUI picks it
  automatically per-architecture — but it logs exactly what it picked on
  every load (confirmed against ComfyUI's own source):
  - `model weight dtype torch.float16, manual cast: None` (`comfy/
    model_base.py`) — the main diffusion model; `float16` is correct here.
  - `CLIP/text encoder model load device: ..., dtype: torch.float16`
    (`comfy/sd.py`) — same check for the text encoder.
  - `VAE load device: ..., dtype: torch.float32` (`comfy/sd.py`) — **this
    one showing `float32` is normal, not a Volta problem.** Many VAE
    architectures (including the classic SD1.5/SDXL VAE) produce
    NaN/black-image output in FP16 regardless of GPU generation, so
    ComfyUI defaults their decode to FP32 on every card, not just Volta.
  If the *main model's* line ever shows `torch.bfloat16` instead, that's
  the real "unexpectedly slow" signal — check SwarmUI's console/log
  output for it directly, there's no UI indicator for this.
- **Attention backend: you almost certainly don't need to touch this.**
  ComfyUI already defaults to PyTorch's own `scaled_dot_product_attention`
  (SDPA) on any NVIDIA GPU running PyTorch 2.x with no xformers install
  present (confirmed against `comfy/model_management.py`) — this is the
  out-of-the-box behavior on a stock SwarmUI/ComfyUI install, not
  something you need to opt into. PyTorch's own SDPA dispatcher then
  automatically skips its FlashAttention backend on Volta (that one needs
  Ampere+/sm_80) and falls back to its memory-efficient (CUTLASS-based)
  backend instead — transparently, with nothing to configure either way.
  SwarmUI's **Backends → (your ComfyUI backend) → Extra Args** field maps
  straight to ComfyUI's own CLI flags (`--use-pytorch-cross-attention`,
  `--use-split-cross-attention`, etc. — confirmed against
  `comfy/cli_args.py`) for the rare case you need to force a specific
  backend while troubleshooting, but **leave it blank for a stock Volta
  install** — setting `--use-pytorch-cross-attention` explicitly gains
  nothing when it's already the default. xformers wheels are hit-or-miss
  for sm_70 depending on which one gets installed, so don't add
  `--use-flash-attention` here expecting a free speedup without first
  checking SwarmUI's own log for a FlashAttention/xFormers install
  warning at startup.
- **VAE decode tiling**: ComfyUI automatically retries a VAE decode with
  tiling if it hits a CUDA out-of-memory error decoding at full
  resolution, logging `"Ran out of memory when regular VAE decoding,
  retrying with tiled VAE decoding"` when it does (confirmed against
  `comfy/sd.py`). If you see that warning repeatedly on a shared V100,
  SwarmUI's own **VAE Tile Size** generation parameter (Advanced Sampling
  group) lets you pick a tile size proactively — trading a bit of decode
  time for lower peak VRAM — instead of relying on the automatic OOM
  retry every time.
- **One GPU, two consumers**: see §3a for VRAM-sharing guidance if you run
  Ollama and SwarmUI on the same V100 (the normal single-card setup).

## 6. Speech-to-text on the GPU

Transcription (audio attachments, session recordings, library clips) runs on
**Unsloth Studio** through its `/v1/audio/transcriptions`, so it uses whatever
GPU the `unsloth` service has — there is no separate transcription container to
pass a GPU to. (The whisper.cpp sidecar and its Volta-specific CUDA image have
been removed.)

- **Pick a Whisper model in Studio** (Settings → Voice) and set the same name
  in nd-world (Settings → System → *Speech-to-text model*, then **▷ Test STT**).
  `large-v3-turbo` (~1.6 GB) gives most of `large-v3`'s accuracy at several
  times the speed; `large-v3` (~3.1 GB) is the most accurate.
- **VRAM sharing:** how Studio shares one card between the speech model and the
  chat model is governed by its own auto-switch / idle-unload settings (not
  verified here). If chat answers slow down right after a transcription, or the
  other way round, the models may be swapping — an idle auto-unload of a few
  minutes in Studio, and keeping long transcriptions out of the middle of play,
  are the first things to try.
- **Several GPUs (A16):** a Whisper model is small (~1.6–3 GB) and fits on a
  16 GB GPU next to a mid-size chat model; where Studio actually places it
  across GPUs was not checked — see §3c.
- **Volta (V100):** the same image caveat as chat applies — see §2's Volta
  callout about pinning `UNSLOTH_IMAGE` to a tag that still ships sm_70 kernels.
- A V100 transcribes `large-v3-turbo` several times faster than a typical NAS
  CPU — worth it if you record sessions.

## 7. Hardware notes (used cards)

**Tesla T10 16GB (Turing):**
- Passive-cooled OEM datacenter card — like an SXM2 V100, it needs a
  shroud + high-static-pressure fans or it thermal-throttles; there is no
  blower variant.
- Turing's advantages over Volta are structural: flash attention works
  (§4), llama.cpp's native-INT8 MMQ kernel path runs on its tensor cores
  (see the Volta note below — Turing+ gets the most-optimized quantized
  path), and — the decisive one — it's inside current driver/CUDA support
  with no end-of-life horizon.
- **Memory bandwidth is the trade**: 256-bit GDDR6 (~400 GB/s) vs the
  V100's HBM2 (~900 GB/s) — token generation is decode-bandwidth-bound, so
  expect roughly half to a third of the V100's tok/s figures. Image
  generation and model *loading* are less affected.

**Quadro RTX 8000 (Turing):**
- 48 GB GDDR6 on a 384-bit bus (672 GB/s), compute capability 7.5, up to
  295 W — plan the power connectors and the PSU. The usual card is a
  workstation board with a blower; a *passive* server variant exists, and
  that one needs the same shroud + high-static-pressure fans as the T10.
- Same Turing caveats as the T10: fp16 only (no bf16), llama.cpp flash
  attention yes, PyTorch FlashAttention 2 / cuDNN attention / SageAttention no
  (§4).
- It supports an optional NVLink bridge (100 GB/s) between two cards for 96 GB
  in total. llama.cpp splits a model across two cards over plain PCIe as well,
  so the bridge is not needed for this stack (not verified here).
- The point of this card is capacity: the default 26B chat model and a
  32B-class one both stay fully resident (§5), at a decode speed between the
  T10's and the V100's.

**NVIDIA A16 (Ampere, four GPUs):**
- **Four GPUs, not one** — read §3c first; by default Studio is given only one.
- Passive 250 W datacenter card on a single PCIe Gen4 x16 slot (all four GPUs
  share it through the on-board switch): it needs a server chassis's airflow,
  or a shroud + high-static-pressure fans, or it throttles.
- Ampere software support is the best of the cards here — bf16,
  FlashAttention 2, cuDNN attention, official SageAttention — but each GPU
  has only 1,280 CUDA cores, 16 GB and ~200 GB/s, so it is a capacity card
  for background jobs, not a fast interactive one.
- Meant for virtual desktops: under NVIDIA vGPU the profile you assign decides
  whether CUDA compute works at all (§3c).

**Tesla V100 (Volta):**
- **300 W** under load — plan PCIe cabling (8-pin EPS/PCIe adapters on
  many SXM2→PCIe adapters) and case airflow.
- **SXM2 modules are passive**: without the server's fan wall they need
  a shroud + high-static-pressure fans pointed at the heatsink, or they
  thermal-throttle within minutes. PCIe V100s usually have their own
  blower.
- Volta tensor cores accelerate **FP16 only** — GGUF inference's
  quantized kernels still get a tensor-core-accelerated path here
  (confirmed in llama.cpp's own CUDA source, `ggml/src/ggml-cuda/
  common.cuh`: a distinct `volta_mma_available()` check exists alongside
  `turing_mma_available()`, so Volta isn't just falling back to plain CUDA
  cores), but **it's not literally "nothing lost" vs newer cards.**
  Turing (sm_75) added native INT8 tensor core instructions that Volta's
  hardware has no equivalent for at all — llama.cpp's most-optimized
  quantized (MMQ) kernel path can use those directly on Turing+, where
  Volta gets its own separate (still tensor-core-accelerated, just
  FP16-based rather than native INT8) path instead. In practice this
  shows up as a like-for-like Turing+ card outperforming a Volta card on
  quantized models by more than clock speed and CUDA core count alone
  would predict — not a reason to avoid a Volta card, just not a "you
  lose nothing but newer instructions" situation either.
- Check `nvidia-smi -q -d TEMPERATURE,POWER` under load; sustained
  throttling means cooling, not configuration.

---

Back to [README](../README.md) · nd-world docs: [API reference](API_REFERENCE.md)
· [AI entity guide](AI_ENTITY_GUIDE.md) · [AI-everywhere audit](AI_EVERYWHERE_AUDIT.md)


## System Monitor (GPU, CPU, RAM) inside nd-world

The GM menu has **AI Tools → 🖥 System Monitor** (`/system`): live VRAM use, temperature, power draw, GPU load, which programs hold VRAM,
plus CPU load / per-core / temperature and RAM (with the ZFS cache shown separately on TrueNAS). It refreshes every 1-5 s while the tab
is visible.

CPU and RAM need nothing. The **GPU** part runs `nvidia-smi`, which is not in nd-world's image - the NVIDIA container runtime adds it
to any container that is given the GPU. Add this to the `world` service in your compose file (TrueNAS: edit the Custom App YAML):

```yaml
  world:
    environment:
      NVIDIA_DRIVER_CAPABILITIES: utility      # only nvidia-smi is mounted - nd-world never runs CUDA
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Sharing the card with Unsloth Studio like this is fine: a reservation does not lock the GPU, both containers can use it. If the page says
"nvidia-smi is not available inside this container", the block is missing or the container was not redeployed.

### Power limit (keeping a passively cooled card cool)

Each GPU card on the monitor has a **Power limit** slider (inside the card's own minimum-maximum range, e.g. 60-150 W for a Tesla T10)
with **Apply** and **Default**. A lower limit makes the card draw less power and run cooler at the cost of speed - useful while a
passive card has no fan of its own yet (try 90-110 W and watch the temperature). It lasts until the driver reloads or the machine reboots.

Changing it needs more permission than reading it. If the app answers "Insufficient Permissions", either set it from the TrueNAS shell
(`sudo nvidia-smi -i 0 -pl 100`) and make it permanent with **System → Advanced → Init/Shutdown Scripts → Add → Command, When: Post Init**
using the same command, or - less safe - give the `world` service `cap_add: [SYS_ADMIN]`, which grants the whole container a broad privilege
and is not recommended just for this.
