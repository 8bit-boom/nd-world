# Deploying nd-world for your table

This walks through running nd-world on your own machine/server (Part A), then
making it reachable from the internet so you can invite players (Part B).

If you just want the short version: `bash scripts/setup.sh` handles Part A —
installing Docker, creating `.env`, setting up your GM login, and starting the
stack. This doc explains what that does and covers Part B in detail.

---

## Part A — Run it locally

1. Install [Docker](https://docs.docker.com/get-docker/) (with the Compose
   plugin — recent installs include it automatically).
2. Clone this repo and run the setup script:
   ```bash
   git clone https://github.com/8bit-boom/nd-world.git
   cd nd-world
   bash scripts/setup.sh
   ```
3. The script creates `.env` (a random `SECRET_KEY`, and it'll ask for the
   email/password you want to use as the GM), then builds and starts
   nd-world and Unsloth Studio (the AI backend for chat + image generation;
   the legacy Ollama/SwarmUI pair is still available for rollback).
4. Once it prints a URL, open it and log in with the GM email/password you
   set. At this point the app works for anyone on your home WiFi/network, but
   not yet from the internet — that's Part B.

**Manual setup** (if you'd rather not use the script): copy `.env.example` to
`.env`, fill in `SECRET_KEY` (`openssl rand -hex 32`) and `GM_EMAIL`/
`GM_PASSWORD`, then run `docker compose up -d --build`.

**A note on the optional AI service ports**: `docker-compose.yml`/
`truenas-compose.yml` publish Ollama (`11434`) and SwarmUI (`7801`) on every
network interface (`0.0.0.0`), not just `localhost` — anyone on your LAN can
reach them directly, and neither has its own authentication. nd-world itself only ever needs them
over the internal Compose network (`http://ollama:11434` etc.), never the
published host port — that's only there so you can open SwarmUI's own UI
in a browser, or (on TrueNAS SCALE) so the Apps page's own portal links
work. If that's more exposure than you want on a shared network, bind them
to `127.0.0.1:` instead of leaving the host address blank in each
service's `ports:` list — just be aware that on TrueNAS this also breaks
the **Apps → Discover Apps → &lt;app&gt; → Web Portal** shortcut for that
service, since it relies on reaching the published port directly.

### One-time Unsloth setup (AI chat + images)

The `unsloth` profile starts Unsloth Studio alongside the app — the single
backend for both AI chat and image generation. After `docker compose up -d`:

1. **Log in to Studio** — open `http://<host>:8000` and sign in with
   username `unsloth` and the password from `UNSLOTH_STUDIO_PASSWORD`.
2. **Create an API key** — Studio → Settings → API → create a key.
3. **Save it in nd-world** — ⚙️ Settings → **Unsloth (AI backend)** →
   *API key*. The moment a key is saved, AI chat **and** image generation
   (including media generated inline in chat) switch to Unsloth
   automatically — no restart. The field is write-only: the page never
   shows the saved key (only its last 4 characters); leave it blank to keep
   the saved key, tick *remove the saved key* to clear it, which falls back
   to the legacy Ollama/SwarmUI backends (kept for rollback until the
   migration cutover).
4. **Download models** — in Studio's Model Hub, fetch the chat model
   (`unsloth/gemma-4-26B-A4B-it-GGUF` by default, override with
   `UNSLOTH_MODEL`) and the image model (`unsloth/z-image-turbo-GGUF`,
   override with `UNSLOTH_IMAGE_MODEL`). Consider enabling Studio's idle
   unload (~300 s) so VRAM is released between sessions. Set the chat
   model's **context length** in Studio to the same number as
   `LLM_CONTEXT_TOKENS` (default 16384) — nd-world sizes its prompts from
   that value, and the AI page's status card warns when the two differ.
5. **On a Tesla V100** — load models with fp16 + non-flash attention and
   pin the Studio image tag (see [GPU_SETUP.md](GPU_SETUP.md) §4; the
   sm_70 kernel risk is finding I-7 in
   [UNSLOTH_PHASE0_FINDINGS.md](UNSLOTH_PHASE0_FINDINGS.md)).

Remember: Studio API keys do not survive container recreation — after a
Studio rebuild, create a fresh key and update Settings.
The key box has its own **Save & verify key** button — it is not part of the big
Settings Save or of *Save Studio settings*. Studio is asked about a pasted key
first: a key it rejects (*Invalid or expired API key*) is **not** saved, so a
mistyped paste can't replace a working key, and the status line under the box says
which Studio URL was asked and which key is in use. A key saved in Settings
**overrides** `UNSLOTH_API_KEY` from `.env` — if you put a fresh key in `.env` after
recreating Studio, press **Remove saved key** (or paste it into the box) or the old,
dead one stays in force.

`docker-compose.yml` takes the Studio image from `UNSLOTH_IMAGE` (default
`unsloth/unsloth:latest`, a moving tag — pin one that works for you once you
have it) and publishes Studio's port on `UNSLOTH_BIND` (default `127.0.0.1`;
set `0.0.0.0` to open the Studio console to other machines). nd-world itself
reaches Studio over the compose network and doesn't use that mapping. A single
image generation may run up to `UNSLOTH_IMAGE_TIMEOUT_SECONDS` (default 1800).

The other profiles: `android` and `editor` (see below), and the legacy
`ollama`/`swarmui` pair (don't combine with `unsloth`). Speech-to-text — audio
attachments, session recordings, library-clip transcripts — runs on Unsloth
Studio too; there is no separate transcription service (see "Speech-to-text"
below).

---

## Part B — Make it reachable from the internet

You have two options. **Option 1 (Cloudflare Tunnel) is strongly recommended
if you've never done this before** — it needs no router changes and no
technical networking knowledge. Option 2 is more advanced.

### Option 1: Cloudflare Tunnel (recommended)

This creates a secure, private connection from your server out to Cloudflare,
who then gives you a public web address. Nobody needs to open any doors into
your home network for this to work.

**Step 1 — Try it instantly, no account needed (good for a first test):**

In your server's terminal (replace the port if you changed `APP_PORT`):
```bash
docker run --rm cloudflare/cloudflared:latest tunnel --url http://localhost:8080
```
On TrueNAS SCALE, run this from **System Settings → Shell**, using your
TrueNAS box's LAN IP and nd-world's mapped port instead of `localhost` (e.g.
`http://192.168.1.50:8087` — check **Apps → nd-world → Workloads → Ports**
for the exact port).

After a few seconds you'll see a line containing a web address ending in
`.trycloudflare.com` — that's a public link to your app, live right now.
Share it with a player and try logging in (or joining, if you've sent them an
invite link). This link stops working as soon as you close/stop this command,
and a new random link is generated each time — it's only meant for a quick
test, not permanent use.

Press `Ctrl+C` in the terminal to stop it when you're done testing.

**Step 2 — Set up a permanent link (once you're happy with the test):**

This needs a free Cloudflare account and a domain name (a domain costs a
small yearly fee from any registrar — e.g. Namecheap, Cloudflare Registrar —
if you don't already have one; a subdomain like `campaign.yourdomain.com`
works too).

1. Sign up at [cloudflare.com](https://cloudflare.com) (free tier is fine).
   In the dashboard, go to **Domains → Add a domain**, enter your domain, and
   follow Cloudflare's setup wizard (it will ask you to change your domain's
   "nameservers" at your registrar — follow their instructions; this can
   take anywhere from a few minutes to a few hours to take effect).
2. In the left sidebar, go to **Networks → Tunnels** (or click **Deploy a
   tunnel** from the account Overview page's "Recommendations" panel) and
   click **Create a tunnel**. Choose connector type **Cloudflared**, then
   name it something like `nd-world`.
3. On the next screen, pick any OS tab — you only need the token, not the
   full install command shown there. Copy just the long string after
   `install` (starts with `eyJ...`) — that's your **tunnel token**. Click
   **Next**.
4. On the "Public Hostname" screen: pick a subdomain (e.g. `world`), your
   domain, Service type `HTTP`, and URL `localhost:8080` (or
   `<your-server-ip>:8080` if cloudflared will run on a different machine —
   e.g. as its own TrueNAS app, see below). Click **Save**.
5. Run cloudflared with that token (see below for your platform). Within
   about 30 seconds the tunnel shows **Healthy** in the Cloudflare
   dashboard, and `https://world.yourdomain.com` (or whatever you chose)
   shows the nd-world login page — permanently, with a free HTTPS padlock
   included.

**Running cloudflared:**

- **Plain Docker host** (same machine as nd-world):
  ```bash
  docker run -d --name cloudflared --restart unless-stopped \
    cloudflare/cloudflared:latest tunnel run --token <your-tunnel-token>
  ```
- **TrueNAS SCALE:** run it as its own app, separate from nd-world — go to
  **Apps → Discover Apps → Custom App**, name it `cloudflared`, and paste
  this into Custom Config:
  ```yaml
  services:
    cloudflared:
      image: cloudflare/cloudflared:latest
      container_name: cloudflared
      restart: unless-stopped
      command: tunnel run
      environment:
        TUNNEL_TOKEN: "<your-tunnel-token>"
  ```
  Since cloudflared and nd-world are separate apps, use your TrueNAS box's
  LAN IP (not `localhost`) for the Public Hostname's Service URL in step 4
  above — e.g. `192.168.1.50:8087`, matching nd-world's host port from
  **Apps → nd-world → Workloads → Ports**.

**Once this is live**, edit `.env` and set `COOKIE_SECURE=true`, then run
`docker compose up -d` again to apply it. (This matters — without it, logins
won't stay signed in when accessed over `https://`.)

**Upload size limit:** every request routed through Cloudflare — tunnel or
not — passes through Cloudflare's edge first, which rejects any request body
over a size that depends on your Cloudflare plan (Free is capped at 100 MB
with no way to raise it; paid plans can raise the cap from the dashboard via
**Rules → Configuration Rules → Maximum Upload Size** — see
[Cloudflare's own docs](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-4xx-errors/error-413/)
for current numbers). A big upload (a large map image, most Audio Library
uploads) rejected with a page that says **"413 Payload Too Large" /
cloudflare** at the bottom hit that edge limit before it ever reached
nd-world — it's not something `.env` or nd-world's own settings can raise.
nd-world's own per-upload caps sit *below* whatever Cloudflare allows
through (see `MAX_UPLOAD_BYTES`, `MAX_AUDIO_UPLOAD_BYTES`,
`MAX_AI_ATTACHMENT_BYTES`, and `MAX_MODEL_UPLOAD_BYTES` in `.env.example`),
so raise the relevant one if
you need bigger files end to end. Every audio upload path is the exception:
the Audio Library (`/audio`), a voice-memo attachment on the AI Chat compose
bar / an entity's Ask AI panel / the AI page's Speech tab, and a Session's audio
recap upload/mic recording all automatically split a file over 100 MB into
smaller parts in the browser before upload and reassemble it on the server,
so a long session recording or ambiance track gets through Cloudflare's
fixed cap regardless of plan — nothing to configure for these. Gallery
album uploads (`/images`) get the same automatic treatment: the browser
splits any album image over 100 MB into parts and reassembles it on the
server, with the total bounded by `MAX_GALLERY_UPLOAD_BYTES` (500 MB by
default) instead of by Cloudflare. Any other large non-audio upload (a map
image, a portrait) has no such split and is still subject to Cloudflare's
raw cap.

### The 🧠 Studio Console page and `http://unsloth:8000`

`UNSLOTH_URL=http://unsloth:8000` is the address nd-world's *server* uses; `unsloth` is a
Compose service name that only resolves inside the Docker network, so a browser pointed at it
shows "Server Not Found". The Studio Console page therefore opens Studio at the host you
reached nd-world on plus Studio's port (nd-world at `http://192.168.1.216:8087` → Studio at
`http://192.168.1.216:8000`) and says so on the page. If Studio is published somewhere
else — another port, a hostname, a tunnel — set **Studio Console URL** in Settings → System;
that always wins (typed without a scheme, `192.168.1.216:8000` is saved as
`http://192.168.1.216:8000`).

### Studio Console over Cloudflare Tunnel (optional)

The 🧠 **Studio Console** page (`/studio`) embeds the Unsloth Studio web UI —
projects, fine-tuning/recipe workflows, agent skills, voice settings, the
model hub, video generation. Out of the box its embed URL falls back to
`UNSLOTH_URL` (e.g. `http://unsloth:8000`) — a **Docker-internal hostname
that only resolves between containers**, so the frame shows a
"Server Not Found" error in any browser. To make it work remotely:

1. **Give Studio its own public hostname on the same tunnel.** In the
   Cloudflare Zero Trust dashboard → Networks → Tunnels → your tunnel →
   Public Hostname → Add: subdomain `studio` (or your choice), your domain,
   Service **HTTP**, URL `<TrueNAS-LAN-IP>:8000` (the `unsloth` container
   publishes port 8000 on the host).

2. **Strip Studio's frame-blocking headers with a Worker.** Studio sends
   `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors
   'none'` — it refuses to be embedded by *any* site, so the iframe stays
   blank until you relax those. Workers & Pages → Create Worker:

   ```js
   export default {
     async fetch(request) {
       const response = await fetch(request);
       const headers = new Headers(response.headers);
       headers.delete("x-frame-options");
       const csp = headers.get("content-security-policy");
       if (csp) {
         headers.set("content-security-policy",
           csp.replace("frame-ancestors 'none'",
                       "frame-ancestors https://world.YOURSITE.com"));
       }
       return new Response(response.body,
         { status: response.status, statusText: response.statusText, headers });
     },
   };
   ```

   Then add a route `studio.YOURSITE.com/*` for it (Worker → Settings →
   Domains & Routes). The `frame-ancestors` replacement scopes embedding to
   your site only — every other referrer still gets blocked.

3. **Point nd-world at it.** ⚙️ Settings → System → **Studio Console URL**
   → `https://studio.YOURSITE.com` → Save Studio settings.

   ⚠️ The override **must be https** when your site is. An `http://` URL
   (e.g. a raw LAN IP) gets silently auto-upgraded to https by the browser,
   which fails against Studio's plain-HTTP server with
   `SSL_ERROR_RX_RECORD_TOO_LONG` — the /studio page detects this and shows
   a warning in-place.

4. **Gate it (recommended).** Studio's own login (with rate limiting) is
   now a public brute-force surface. Zero Trust → Access → Applications →
   self-hosted app for `studio.YOURSITE.com` with an email-OTP "Allow"
   policy puts a second door in front of it. Studio auth uses
   localStorage-held JWT tokens (not cookies), so sign-in works fine inside
   the cross-origin iframe; with Access enabled you authenticate to Access
   once per session and then to Studio itself.

### Option 2: Port forwarding + reverse proxy (more advanced)

Only do this if Option 1 doesn't work for your situation. This method opens a
door directly into your home network, which needs to be configured carefully.

1. Install a reverse proxy (e.g. **Nginx Proxy Manager** or **Caddy**) to get
   a free HTTPS certificate for a domain name and forward traffic to
   `http://<your-server-ip>:8080`.
2. Point that domain's DNS at your home internet connection's public IP
   address (your internet provider's website or a service like No-IP/DuckDNS
   can tell you this address, since most home internet IPs change over time).
3. Forward the port your reverse proxy listens on (usually 443) from your
   router to the machine running it.
4. Once traffic is flowing over HTTPS, set `COOKIE_SECURE=true` in `.env` and
   restart (`docker compose up -d`), same as Option 1.

---

## Optional: embed NeonDragonsApp in the browser (self-hosted emulation)

nd-world's `/androidapp` page can show the real NeonDragonsApp (the Android
character sheet app) running live in the browser, via a self-hosted Android
emulator — useful for a player who doesn't want to install the APK, or for
demoing it at the table on a shared screen. This is a heavier, opt-in add-on,
not something most installs need.

**Before enabling this, check your hardware**: it needs an **x86_64** host
with **`/dev/kvm`** available (check with `ls /dev/kvm` — without it the
emulator still runs, but painfully slowly) and a few GB of free RAM on top
of everything else you're running. It will **not** run acceptably on
ARM-based NAS/SBC hardware (e.g. a Raspberry Pi, or ARM-based TrueNAS mini
devices).

**1. Enable the profile** — add `android` to `COMPOSE_PROFILES` in `.env`
(comma-separated with any other profiles you're already running), then:
```bash
docker compose up -d
```
This starts an emulator + noVNC web viewer
([budtmo/docker-android](https://github.com/budtmo/docker-android)) on port
`6080`. Give it a couple of minutes on first boot — the emulator image is
large and needs to fully start before the viewer shows anything.

**2. Point nd-world at it** — log in as the GM, go to **Settings → System**,
and set **Android emulator URL** to `http://<your-server>:6080` (same host
you use to reach nd-world itself, just port `6080`). `/androidapp` now shows
the live emulator in an iframe; leaving this blank keeps the page showing a
"not configured" message instead, same as Image Studio does when SwarmUI
isn't set up.

**3. Install the app** — download the latest debug APK from CI (see the
project's `CLAUDE.md` for the exact `gh run download` command), then:
```bash
bash scripts/android-provision.sh path/to/neon-dragons-debug.apk
```
Re-run this any time you want to push a newer build — it replaces the
existing install in place. This step is manual by design: automating a pull
from GitHub Actions would mean storing a GitHub token in your deployment for
what's a nice-to-have feature, not the primary way players get the app.

**Known limitation**: this is one shared emulator for the whole install, not
a separate session per player — if two people open `/androidapp` at once,
they're looking at (and controlling) the same Android session. Fine for a
GM demoing something or a shared table device; not a substitute for players
installing the app on their own phones.

---

## Optional: embed NeonDragonsEditor in the browser (containerized desktop)

nd-world's `/editor` page (GM-only) can show the real NeonDragonsEditor
desktop app — the tool used to build races/professions/feats/items — running
live in the browser, via a containerized session (Xvfb + noVNC, not an
emulator, so it's much lighter than the Android option above). Like Android
emulation, this is an opt-in add-on: it has **no automated test coverage**
(no CI job builds or exercises this container) and is a first pass — expect
to need some iteration, especially around the two tabs that embed a Chromium
view (HTML sheet preview, city map).

**1. Enable the profile** — add `editor` to `COMPOSE_PROFILES` in `.env`
(comma-separated with any other profiles you're already running), then:
```bash
docker compose up -d
```
This builds the `NeonDragonsEditor/Dockerfile` image from the
UoY-Neon-Dragons repo and starts a noVNC web viewer on port `6081`.

**2. Point nd-world at it** — log in as the GM, go to **Settings → System**,
and set **Content editor URL** to `http://<your-server>:6081`. `/editor` now
shows the live editor session in an iframe; leaving this blank keeps the
page showing a "not configured" message instead.

**3. Give it content to edit** — the container mounts a volume at
`/data/rulebook`, and the editor starts with `--portable /data/rulebook` so
it skips the first-run folder picker. Populate that volume with a checkout
of the content markdown (the `character creation/`/`equipment/`/`lore/`
trees) before or after first start:
```bash
docker compose exec editor git clone <your-fork-url> /data/rulebook
```

**Known limitation**: the Editor has no git integration at all — edits made
through the embedded session persist in the mounted volume, but getting them
back into a real git repo's history is a manual
`docker compose exec editor git ...` step. For most GMs, the
**"Export to nd-world..."** feature (in the Editor's Data menu) should be
the primary save path when using the remote editor — it pushes
races/professions/feats/items/characters straight into a running nd-world
world and needs no git at all.

---

## Speech-to-text (Unsloth Studio)

Audio is transcribed by **Unsloth Studio** — the same service that runs chat
and image generation — through its OpenAI-compatible
`/v1/audio/transcriptions`. There is nothing extra to deploy: once the Studio
API key is saved (⚙️ Settings → System) the following all work.

- **Audio attachments** on an AI Chat message or an entity's Ask AI panel (the
  📎 button / drag-and-drop) and the 🎤 mic buttons are transcribed at upload
  time, so the transcript reaches **any** chat model as plain text.
- **Session recordings** — background jobs, a one-shot upload, and the live
  recording panel — are transcribed chunk by chunk, with a resumable
  checkpoint after each chunk.
- **Audio / video library clips** get a transcript from their *Transcribe*
  button (Studio returns plain text without timestamps, so no subtitle track
  is generated and an existing one is left alone).

**1. Download a speech model in Studio** — open Studio → Settings → Voice (or
the model picker) and download a Whisper model. `large-v3-turbo` is a good
default: most of `large-v3`'s accuracy at several times the speed. Studio's
API only accepts the standard Whisper model names (`tiny`, `base`, `small`,
`medium`, `turbo`, `large-v3-turbo`, `large-v3`, `large`) and, on the builds
tested so far, **not** `unslothai/Qwen3-ASR-1.7B-GGUF`: it shows as
"On Device" in Studio's own UI but the API answers *"STT model … is not
downloaded"*.

**2. Pick it in nd-world** — ⚙️ Settings → System → *Unsloth Studio server* →
*Speech-to-text model*, then press **▷ Test STT**. The test pushes a short clip
through the exact request the pipeline makes, so a missing or unusable model
shows up there instead of as a failed job hours later. The Sessions page's live
recording panel runs the same check (with a WebM/Opus clip, the format the
browser records) every time a recording starts.

**Tuning**

- `UNSLOTH_STT_TIMEOUT_SECONDS` (default 1800) — the longest one transcription
  request may run. On a Studio that transcribes slower than realtime (CPU-only),
  keep chunks short or raise this.
- `STT_CHUNK_SECONDS` (default 600) — a recording longer than 15 minutes is
  split into pieces of about this length, each transcribed as its own request.
- `STT_JOB_CONCURRENCY` (default 1) — how many background jobs may talk to the
  speech model at once. (`WHISPER_CHUNK_SECONDS` / `WHISPER_JOB_CONCURRENCY` are
  still read as the old names.)
- Studio rejects a request body over 25 MiB: larger files are re-encoded to a
  compact mono MP3 and split automatically (needs `ffmpeg`, which the image
  includes).

**Coming from the whisper.cpp sidecar?** It has been removed, along with its
settings: the `whisper` Compose profile and service, `WHISPER_URL` and the
Settings field, the model download/activate buttons, the world-level *spoken
language*, *name glossary* and *speech enhancement* (DeepFilterNet) options, and
the `latest-denoise` image. Those options were whisper.cpp features that Studio
ignores. You can delete `<AI_MODELS_DIR>/whisper/` and any `WHISPER_*` lines from
`.env`. Old databases keep the now-unused `whisper_*` columns; they are harmless.

---

## Optional: tuning Ollama from the browser (no `.env` editing)

Ollama has two different kinds of settings. Per-request options — temperature,
context length, sampling, and the rest — apply to the very next AI request and
have always lived on nd-world's own **Settings → System** tab, no restart ever
needed. Server-level settings — flash attention, KV cache quantization,
parallelism, GPU selection — configure the Ollama *process itself*, which only
reads them once at start-up; historically that meant editing `OLLAMA_*` values
in `.env` and running `docker compose up -d ollama` by hand. As of this
version, both live on the same Settings → System page.

**How it works**: saving the "Ollama server tuning" section writes an
`ollama.env` file into a small shared volume (`ollama-config` in
`docker-compose.yml` / `/mnt/DeadPool/apps/nd-world-ollama-config` in
`truenas-compose.yml`) that only the `world` and `ollama` containers can see.
The `ollama` service's entrypoint sources that file into its own environment
on every start, where it wins over anything set directly in `.env` — those
`.env` values still work exactly as before and now just act as the
pre-GM-interaction default, or what a field falls back to when left blank on
the Settings page. Either way, Ollama still only reads its environment at
start-up, so a value saved here doesn't take effect until the `ollama`
container actually restarts — nd-world can write the file, but deliberately
does **not** restart the container for you (that would need Docker socket
access mounted into the `world` container, i.e. root-equivalent host access,
just to save you one command — not a trade this app makes). The Settings page
tracks this honestly: it compares the file it last wrote against a copy the
entrypoint stamps back (`ollama.env.applied`) once it's actually loaded, and
shows a banner with the exact command to run when the two differ:
```bash
docker compose restart ollama
```
If you're on an existing deployment from before this volume existed, the
banner instead tells you the `ollama` service isn't wired up yet — pull the
latest `docker-compose.yml`/`truenas-compose.yml` (or re-apply the two
`volumes:` entries and the `entrypoint:`/`command:` override on the `ollama`
service by hand if you've customized yours) and restart it once to pick up
the new entrypoint; the shared volume itself is created automatically.

**Hardware detection**: the same tab also shows a best-effort read of the
host's CPU, RAM, and GPU (via `nvidia-smi` or AMD's sysfs, whichever is
visible from inside the `world` container — which is normally neither, since
only the `ollama` service is given GPU access in `docker-compose.yml`; enter
your card's VRAM manually in that case, or mirror `ollama`'s GPU access onto
`world` if you'd rather this auto-detect) plus a "recommend settings for"
picker over whatever models you've actually pulled. Recommendations are a
coarse starting point (context length, KV cache quantization, flash
attention, and similar), not a precise sizing tool — click **Apply
recommended** to fill the relevant fields, review them, then Save as normal.

---

## Updating without losing in-flight jobs

A routine `docker compose up -d --build` (or a Watchtower auto-update, or a
plain container restart) used to just kill whatever background job — audio
transcription/summarization, image generation, a chat completion — was
mid-flight, leaving it stuck at "Interrupted by a server restart" with no
way to continue. It doesn't anymore.

**What actually happens on shutdown**: the container gets `SIGTERM`. nd-world
stops accepting new work, gives any job that's about to finish a few seconds
to reach its next checkpoint, then cancels whatever's still running and
records exactly how far it got. This is deliberately NOT "wait for every job
to finish" — a single speech-to-text chunk can take minutes on CPU, far longer than
any sane shutdown window — the checkpoint from the last completed step is
what actually survives, not the wait itself.

**What "resumed automatically" means, per job type**:

- **Audio transcription/summarization** (Session Recap, the AI page's Speech
  tab, an AI Chat voice-memo attachment) is a **true resume**: it picks back
  up from the exact chunk it left off on, not from the beginning. A session
  recording interrupted 80% of the way through transcribing doesn't
  re-transcribe the first 80% — it only has to (re)do the rest.
- **Image generation and chat completions** **restart** from the same saved
  request rather than truly resuming — both are one opaque call to
  SwarmUI/ComfyUI or Ollama with no intermediate progress to checkpoint, so
  the only options are "wait for it" (not viable for an update) or "run it
  again."

Either way, this happens automatically on the next boot — no GM action
needed for the common case of an update landing mid-job.

**The 3-attempt cap**: if a job keeps getting interrupted on every single
restart (extremely unusual — normally means something about that specific
job is itself crashing the server, not just an unrelated deploy catching it
mid-flight), auto-resume gives up after 3 attempts and marks it as an error
instead, so a bad job can't turn into an infinite crash-loop-and-retry
across every future restart. Whatever transcript was already salvaged is
kept either way.

**Manual resume**: an audio job that hit the cap, or was interrupted while
Studio happened to be down, can still be continued by hand —
open **🎧 Audio → Background Jobs**, find the job (shown as "⏸
Interrupted"), and click **▶ Resume**. This also resets the 3-attempt
counter, since a manual click is a deliberate decision, not another
automatic retry. Image and chat jobs don't have a manual resume button —
past the cap, just start a new one.

**Deployment requirements**: `docker-compose.yml`/`truenas-compose.yml` set
`stop_grace_period: 30s` on the `world` service, and the Dockerfile's `CMD`
sets `--timeout-graceful-shutdown 10` — both are needed for any of this to
actually run (see either file's own comments for the exact time budget).
If you've forked/customized either file, carry both settings over. You can
tune how long a shutdown waits for a job to reach a checkpoint boundary via
`ND_JOB_STOP_GRACE_SECONDS` (see `.env.example`) — raising it much past the
stop_grace_period budget above is pointless, since the container gets
SIGKILLed before a longer wait would ever pay off.

---

## When the site stops responding: the diagnostics watchdog

A hung request logs nothing — the app is a single uvicorn worker, and when its
event loop wedges (starved DB connection pool, SQLite write-lock pileup) the
process is alive but silent, and `docker restart` destroys the in-process state
that would explain it. An always-on watchdog (`app/diagnostics.py`, zero config)
captures the evidence instead:

- **Event-loop stall** (no heartbeat for `ND_DIAG_STALL_SECONDS`, default 15s)
  or **DB-pool saturation** → a full dump (all thread stacks, the asyncio task
  snapshot, pool status, RSS) is written to `/data/diagnostics/` right then,
  rate-limited to one dump per `ND_DIAG_DUMP_COOLDOWN_SECONDS` (default 300s).
- Every **startup/shutdown** is journaled to `/data/diagnostics/events.log` —
  a `startup` line with no `shutdown` before it means the process was killed
  (OOM/SIGKILL/watchtower), which is invisible in docker logs. Stall, recovery,
  and pool events go to the same journal, so one file reconstructs the whole
  incident.

On a bind-mounted `/data` the dumps survive container restarts. Read them from
**⚙ Settings → Diagnostics** (GM-only) — watcher status, journal tail, dump
list — or `GET /admin/diagnostics/events` / `GET /admin/diagnostics/dump/{name}`
as plain text. `ND_DIAG_DISABLED=1` turns it off; every knob is in
`.env.example`.

---

## Inviting players

Once nd-world is reachable (locally or over the internet):

1. Log in as the GM.
2. Open the world switcher → **⚙ Manage worlds** → **Edit** on the world you
   want to invite someone to.
3. Under **Invite Links**, click **+ Create Invite Link** (optionally set an
   expiry or a max number of uses — leave blank for an unlimited, permanent
   link).
4. Copy the `/join/...` link it creates and send it to your player. Opening
   it lets them create their own account (or log in, if they already have
   one) and joins them to that world as a player.

Players only see worlds they've been invited to, and lore is filtered by each
entity's **Visibility** setting (on the entity's edit page): **Everyone**,
**GM only**, or **Specific players** — pick the last one to share a secret
with just one or two party members instead of the whole table. They manage
one character each (via the character creation wizard, or
directly on their sheet), and — if you leave **Players can see each other's
characters** checked on the world's Edit page — can see the rest of the party
read-only.

Manage existing players (remove access) and revoke invite links from the same
world Edit page.
