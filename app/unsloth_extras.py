"""Typed helpers for the Unsloth Studio server endpoints beyond chat —
model-hub management, image-model load/progress, auto-switch settings, TTS
and STT. Everything here was live-verified against a running Studio
(docs/UNSLOTH_PHASE0_FINDINGS.md, "Phase 0.5" appendix); each helper is
feature-detected so an older/newer Studio build degrades to a clean error
instead of a stack trace.

Backend resolution follows the same single switch as everywhere else:
``effective_llm_api_key()`` non-empty = Unsloth active. Callers (all
GM-only routes) convert ``StudioMissing``/``StudioError`` into HTTP
errors; ``_ollama.ResponseError`` shapes are reused so existing error
handling stays uniform.
"""
import logging
import time

import httpx as _httpx

from .ai import effective_llm_api_key, effective_llm_url

_log = logging.getLogger("nd.unsloth_extras")

# Discovery probes (hub cache listing, progress polls) are quick; the hub
# itself can be slow to answer the first time (Phase 0.5: /v1/models took
# ~10 s cold), so reads get a moderate budget. Downloads/loads are started
# asynchronously server-side — the POST returns immediately.
_PROBE_TIMEOUT = _httpx.Timeout(30.0, connect=8.0)
_ACTION_TIMEOUT = _httpx.Timeout(60.0, connect=8.0)
# TTS synthesizes (seconds-to-minutes for a paragraph); STT runs a full
# transcription pass over the uploaded audio.
_AUDIO_TIMEOUT = _httpx.Timeout(600.0, connect=10.0)
# STT's own read budget: transcription of a long piece on a CPU-only box
# (the GM's TrueNAS has no GPU yet) can far exceed TTS's 10 minutes —
# observed live: a Qwen3-ASR job died at exactly the old 600 s read
# timeout after 10m16s. 30 min default, env-tunable.
_STT_TIMEOUT = _httpx.Timeout(
    float(__import__("os").environ.get("UNSLOTH_STT_TIMEOUT_SECONDS", "1800")),
    connect=10.0)


class StudioMissing(Exception):
    """No Unsloth backend configured (key empty) — the route should 400."""


class StudioError(Exception):
    """The Studio server rejected or failed a call — carries the server's
    own message so the UI can show exactly what Studio said."""

    def __init__(self, message: str, status_code: int = 502, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class StudioEndpointMissing(StudioError):
    """404 from Studio — this Studio build doesn't have that endpoint.
    Callers degrade gracefully (hide the button / show "not available")."""

    def __init__(self, path: str):
        super().__init__(f"This Studio build doesn't provide {path} — update Studio to use it", 404)


def _base_key() -> tuple[str, str]:
    url = effective_llm_url().rstrip("/")
    key = effective_llm_api_key()
    if not key:
        raise StudioMissing("No Unsloth backend configured — set UNSLOTH_API_KEY (Settings → System)")
    return url, key


# ── API-key death detection ──────────────────────────────────────────────────
# Live-verified quirk (Phase 0 findings I-8): Studio API keys survive a
# container restart but NOT a recreation — and TrueNAS app updates recreate
# containers. When that happens every AI feature silently breaks with 401
# "Not authenticated" while nothing in nd-world explains why. These two
# timestamps let the UI tell the GM exactly what happened and how to fix it.
# In-memory on purpose: a restart clears the banner, but the next failing
# call re-arms it within seconds, and no DB state can go stale this way.
_last_auth_failure: float = 0.0
_last_auth_ok: float = 0.0


def _note_auth_failure() -> None:
    global _last_auth_failure
    _last_auth_failure = time.time()


def _note_auth_ok() -> None:
    global _last_auth_ok
    _last_auth_ok = time.time()


def auth_status() -> dict:
    """Snapshot for the 'Studio key died' banner: whether the failure is
    still standing (no successful call since it was recorded) and when it
    happened. `url` is included so the banner can link the right Studio."""
    try:
        url, _key = _base_key()
        configured = True
    except StudioMissing:
        url, configured = "", False
    standing = configured and _last_auth_failure > 0 and _last_auth_failure > _last_auth_ok
    return {
        "configured": configured,
        "url": url,
        "key_failed": standing,
        "failed_at": _last_auth_failure if standing else None,
        "ok_at": _last_auth_ok if _last_auth_ok else None,
        "hint": ("Studio rejected the API key (401). Keys do not survive Studio container "
                 "recreations (TrueNAS app updates recreate it) — re-create the key in "
                 "Studio → Settings → API, then paste it into nd-world Settings → System.")
        if standing else "",
    }


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _request(method: str, path: str, *, json_body=None, params=None,
                   timeout=_PROBE_TIMEOUT, content=None, files=None,
                   headers_extra: dict | None = None) -> dict:
    url, key = _base_key()
    headers = _headers(key)
    if headers_extra:
        headers.update(headers_extra)
    try:
        async with _httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.request(method, f"{url}{path}", json=json_body,
                                   params=params, content=content, files=files,
                                   headers=headers)
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code == 404:
        raise StudioEndpointMissing(path)
    if resp.status_code >= 400:
        # OpenAI-dialect {"error":{"message"}} (the /v1 surface) and
        # FastAPI {"detail"} (the /api surface) both occur — take whichever.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        message = (
            ((body.get("error") or {}).get("message"))
            or (body.get("detail") if isinstance(body.get("detail"), str) else None)
            or str(body)[:300]
        ) or f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code, headers=dict(resp.headers))
    if resp.status_code < 300:
        _note_auth_ok()
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


# ── Studio diagnostic probe ──────────────────────────────────────────────────
# Read-only candidate paths, probed to answer "what does THIS Studio build
# actually expose?" in one click — the live Docker latest exposes neither
# /api/version nor a usable /api/update (Desktop builds run ahead), and the
# Settings card previously could only say "not exposed" per feature.
# NEVER add a mutating path here (e.g. /api/update): a probe must be side-
# effect-free by construction.
_PROBE_PATHS = (
    "/api/version", "/version", "/api/about", "/api/status",
    "/api/health", "/health", "/api/hub/cached-gguf", "/v1/models",
    # 404 on every Studio build verified so far (Phase 0.6) — probed so a
    # future build that exposes a Voice-model API gets DISCOVERED here
    # the day it ships, unlocking nd-world-side STT/TTS model downloads.
    "/api/settings/voice",
)


async def _probe_one(client: "_httpx.AsyncClient", url: str, key: str, path: str) -> dict:
    try:
        resp = await client.get(f"{url}{path}", headers=_headers(key))
    except _httpx.HTTPError as exc:
        return {"path": path, "status": None, "note": f"unreachable: {type(exc).__name__}"}
    snippet = resp.text[:120].replace("\n", " ").strip()
    return {"path": path, "status": resp.status_code, "snippet": snippet}


async def studio_probe() -> dict:
    """Probe the read-only candidates concurrently (short per-path timeout)
    and return {results: [...], version: str|None}. 404s are DATA here, not
    errors — they're the "build doesn't have it" answer. Any 200 JSON body
    with a version-ish field is extracted best-effort."""
    import json as _json
    url, key = _base_key()
    async with _httpx.AsyncClient(timeout=_httpx.Timeout(6.0, connect=4.0),
                                  follow_redirects=True) as c:
        import asyncio as _asyncio
        raw = await _asyncio.gather(*[_probe_one(c, url, key, p) for p in _PROBE_PATHS])
    version = None
    for r in raw:
        if r.get("status") == 200 and r.get("snippet"):
            try:
                body = _json.loads(r["snippet"] + ("}" if r["snippet"].count("{") > r["snippet"].count("}") else ""))
            except ValueError:
                continue
            if isinstance(body, dict):
                for k in ("version", "build", "app_version", "studio_version"):
                    if body.get(k):
                        version = str(body[k])
                        break
        if version:
            break
    return {"results": raw, "version": version}


# ── Model hub ────────────────────────────────────────────────────────────────

async def hub_cached() -> list[dict]:
    """Every GGUF cached in Studio's hub — chat AND image models (verified
    Phase 0.5): [{repo_id, size_bytes, load_id, task, capabilities, ...}].
    `task` separates text-generation / text-to-image / audio rows."""
    data = await _request("GET", "/api/hub/cached-gguf")
    return data.get("cached") or []


async def hub_download_start(repo_id: str) -> dict:
    """Start downloading a hub model — returns immediately; poll
    hub_download_progress(). Shape verified Phase 0.5:
    {job_key, state:"running", accepted:true, transport}."""
    return await _request("POST", "/api/hub/download", json_body={"repo_id": repo_id},
                          timeout=_ACTION_TIMEOUT)


async def hub_download_progress(repo_id: str) -> dict:
    """{downloaded_bytes, completed_bytes, complete_on_disk, expected_bytes,
    progress, cache_path} — real per-repo progress (unlike the old hub
    progress endpoint that lied, per findings' quirks list)."""
    return await _request("GET", "/api/hub/download-progress", params={"repo_id": repo_id})


# ── Image models: load + generation progress ────────────────────────────────

async def image_load(model_path: str, gguf_filename: str = "") -> dict:
    """Load an image-diffusion model. Single-file GGUF repos require
    `gguf_filename` (the server 400s without it — Phase 0.5). Returns the
    status object immediately ({loaded, engine, fallback_reason, ...}) —
    loading continues server-side; poll image_load's `loaded` via this
    same call or rely on generation-time auto-switch."""
    body: dict = {"model_path": model_path, "model_kind": "gguf"}
    if gguf_filename:
        body["gguf_filename"] = gguf_filename
    return await _request("POST", "/api/inference/images/load", json_body=body,
                          timeout=_ACTION_TIMEOUT)


async def image_generate_progress() -> dict:
    """{active, step, total_steps, fraction, eta_seconds} — real generation
    progress for the Image Gen tab's progress bar."""
    return await _request("GET", "/api/inference/images/generate-progress")


async def image_generate_native(body: dict) -> list[dict]:
    """POST /api/inference/images/generate — the richer native endpoint
    (verified via Studio's own OpenAPI schema): negative_prompt, steps,
    guidance, seed, batch_size, init_image/mask_image/strength (img2img),
    upscale, loras. `model` in the body triggers Studio's media
    auto-switch load-by-name when media auto-switch is enabled.

    Returns the batch's persisted Studio-gallery records
    ({images: [GalleryImage{id, url, seed, width, ...}]}) — Studio saves
    them into its own gallery and serves the bytes at each record's url.
    Raises StudioEndpointMissing on Studio builds without it."""
    data = await _request("POST", "/api/inference/images/generate", json_body=body,
                          timeout=_AUDIO_TIMEOUT)
    return data.get("images") or []


async def image_gallery_file(image_or_url: dict | str) -> bytes:
    """The PNG bytes for one Studio-gallery image record (or its url).
    GalleryImage.url is the server-served path for the rendered file."""
    if isinstance(image_or_url, str):
        url_path = image_or_url
    else:
        url_path = image_or_url.get("url") or ""
    if not url_path:
        raise StudioError("Gallery record has no file url", 502)
    if url_path.startswith("http"):
        # absolute — split off the origin, keep our own resolved base/key
        from urllib.parse import urlparse
        url_path = urlparse(url_path).path
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as c:
            resp = await c.get(f"{url}{url_path}", headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code >= 400:
        raise StudioError(f"Fetching generated image failed: HTTP {resp.status_code}", resp.status_code)
    return resp.content


async def image_unload() -> dict:
    """Unload the loaded diffusion model (frees VRAM) — same status-object
    shape as image_load, with loaded:false."""
    return await _request("POST", "/api/inference/images/unload", timeout=_ACTION_TIMEOUT)


async def image_status() -> dict:
    """The currently-loaded diffusion model's full status object
    ({loaded, repo_id, device, dtype, gguf_filename, ...})."""
    return await _request("GET", "/api/inference/images/status")


async def image_load_progress() -> dict:
    """Progress while a diffusion model is being loaded."""
    return await _request("GET", "/api/inference/images/load-progress")


async def gguf_variants(repo_id: str) -> dict:
    """Available quant variants for a hub GGUF: {repo_id, variants:[...],
    default_variant, has_vision, loadable, ...} — the filename list
    image_load's gguf_filename wants."""
    return await _request("GET", "/api/hub/gguf-variants", params={"repo_id": repo_id})


async def hub_download_cancel(repo_id: str) -> dict:
    return await _request("POST", "/api/hub/download/cancel", json_body={"repo_id": repo_id},
                          timeout=_ACTION_TIMEOUT)


async def loaded_models() -> list[dict]:
    """GET /api/inference/loaded-models — what's resident right now, across
    chat and diffusion (the Models tab's "resident" section under Unsloth)."""
    data = await _request("GET", "/api/inference/loaded-models")
    return data.get("models") if isinstance(data, dict) and isinstance(data.get("models"), list) else data


# ── Server settings (auto-switch) ────────────────────────────────────────────

async def auto_switch_get() -> dict:
    """Full auto-switch settings object (verified Phase 0.5)."""
    return await _request("GET", "/api/settings/openai-auto-switch")


async def auto_switch_update(enabled: bool | None = None, media_auto_switch_model: bool | None = None,
                             auto_unload_idle_seconds: int | None = None) -> dict:
    """PUT any subset; Studio returns the merged settings object."""
    body: dict = {}
    if enabled is not None:
        body["enabled"] = bool(enabled)
    if media_auto_switch_model is not None:
        body["media_auto_switch_model"] = bool(media_auto_switch_model)
    if auto_unload_idle_seconds is not None:
        body["auto_unload_idle_seconds"] = max(0, int(auto_unload_idle_seconds))
    return await _request("PUT", "/api/settings/openai-auto-switch", json_body=body,
                          timeout=_ACTION_TIMEOUT)


# ── TTS / STT ────────────────────────────────────────────────────────────────

async def tts(text: str, model: str, voice: str = "", response_format: str = "mp3",
              speed: float = 1.0, instructions: str = "", language: str = "") -> tuple[bytes, str]:
    """POST /v1/audio/speech → (audio_bytes, content_type). The TTS model
    must be loaded in Studio (or media auto-switch on) — a missing model
    surfaces as StudioError with Studio's own message. `voice` is free
    text (OpenAI-style voice names; Studio's Voice settings page manages
    the actual TTS model/voices). `instructions` (delivery style — "gruff,
    tired dockworker") and `language` are schema-verified optional fields
    (Phase 0 findings I-9) sent only when set, so older Studios that
    reject unknown keys never see them."""
    if not text.strip():
        raise StudioError("No text to speak", 400)
    if not model.strip():
        raise StudioError(
            "No TTS model configured — set one in Settings → System (Studio "
            "manages TTS models under its own Settings → Voice)", 400)
    body: dict = {"model": model, "input": text, "response_format": response_format,
                  "speed": float(speed)}
    if voice:
        body["voice"] = voice
    if instructions.strip():
        body["instructions"] = instructions.strip()
    if language.strip():
        body["language"] = language.strip()
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/speech", json=body, headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    _note_auth_ok()
    audio = resp.content
    if not audio:
        raise StudioError("Studio returned no audio for this TTS request", 502)
    return audio, resp.headers.get("content-type", "audio/mpeg")


def _tone_wav_bytes(seconds: float = 1.0) -> bytes:
    """A tiny in-memory WAV (16 kHz mono, soft sine tone) for health checks
    — no fixture file to ship, transcribes on Studio in ~a second even on
    a CPU-only box. ASR models hallucinate a word or return empty text for
    a pure tone; the HTTP status is the signal, never the transcript."""
    import io
    import math
    import struct
    import wave
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    n = int(16000 * seconds)
    w.writeframes(b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / 16000)))
        for i in range(n)))
    w.close()
    return buf.getvalue()


async def stt_health(model: str) -> dict:
    """One-shot 'does this STT model actually work' probe — the exact call
    nd-world's transcription pipeline makes, over a synthesized tone, so a
    'not downloaded' 409 or bad model name surfaces at setup time in
    Settings instead of as a failed background job hours later. Never
    raises: every failure mode comes back as {ok: False, message}."""
    try:
        await stt(_tone_wav_bytes(), "nd-health-check.wav", model=model or "small")
        return {"ok": True, "message": "Studio accepted the audio — model is ready."}
    except StudioMissing as exc:
        return {"ok": False, "message": str(exc)}
    except StudioError as exc:
        return {"ok": False, "status": exc.status_code, "message": str(exc)}


async def tts_health(model: str, voice: str = "", instructions: str = "", language: str = "") -> dict:
    """Same idea for TTS: synthesize a two-word clip through the configured
    model/voice/style. Audio is discarded — ok + message is the result."""
    try:
        audio, _ct = await tts("Ready.", model=model or "", voice=voice,
                               instructions=instructions, language=language)
        return {"ok": True, "message": f"Studio synthesized {len(audio)} bytes — model is ready."}
    except StudioMissing as exc:
        return {"ok": False, "message": str(exc)}
    except StudioError as exc:
        return {"ok": False, "status": exc.status_code, "message": str(exc)}


_TTS_TASK_HINTS = ("text-to-speech", "tts", "voice")
_STT_TASK_HINTS = ("speech-recognition", "speech-to-text", "automatic-speech",
                   "transcri", "whisper", "asr")


def _classify_audio_model(row: dict) -> str:
    """'tts' | 'stt' | ''. Studio's task taxonomy for AUDIO rows is not in
    the verified Phase-0 set (only text-generation / text-to-image were
    confirmed), so this matches tolerantly on task + repo_id — STT first
    (its hints are the more specific strings), then TTS. Unrecognized rows
    come back "" and surface via tasks_seen for the Settings UI."""
    hay = (str(row.get("task") or "") + " " + str(row.get("repo_id") or "")).lower()
    if any(h in hay for h in _STT_TASK_HINTS):
        return "stt"
    if any(h in hay for h in _TTS_TASK_HINTS):
        return "tts"
    return ""


async def audio_models() -> dict:
    """Cached hub models classified as TTS / STT, for the Settings pickers
    and the audio library's per-generation model chooser. Tolerant on
    purpose: anything whose task/repo_id doesn't read as speech-related is
    not dropped silently — its task lands in tasks_seen."""
    cached = await hub_cached()
    out = {"tts": [], "stt": [], "tasks_seen": []}
    for row in cached:
        task = str(row.get("task") or "")
        if task and task not in out["tasks_seen"]:
            out["tasks_seen"].append(task)
        kind = _classify_audio_model(row)
        if kind:
            out[kind].append({
                "repo_id": row.get("repo_id", ""),
                "task": task,
                "size_bytes": row.get("size_bytes"),
                "capabilities": row.get("capabilities") or {},
            })
    return out


_VERSION_PATHS = ("/api/version", "/version", "/api/about")


async def studio_version() -> str:
    """Studio's version — probed across the version-ish endpoints different
    builds expose (the live Docker latest serves /version; /api/version
    404s there). First 200 with a version-ish field wins; builds without
    any of them raise StudioEndpointMissing (the route degrades to a clean
    "not available" instead of an error)."""
    import json as _json
    for path in _VERSION_PATHS:
        try:
            data = await _request("GET", path)
        except StudioEndpointMissing:
            continue
        for k in ("version", "build", "app_version", "studio_version"):
            if data.get(k):
                return str(data[k])
        # /api/about is a page-ish endpoint — if it 200s with HTML, don't
        # keep scanning forever for a field that isn't coming.
        break
    return ""


async def studio_update() -> dict:
    """Ask Studio to update itself — feature-detected via /api/update. A
    live Studio 405s POST and 404s GET, so the real method is discovered
    from the 405 response's standard Allow header (falling back to GET
    when the header is absent). Builds without any /api/update raise
    StudioEndpointMissing and the route tells the GM the manual path;
    nd-world never shells out to Docker."""
    # Try the known update paths/methods in order. A 404 = this build
    # doesn't have that path — move on. A 405 = the path exists but wrong
    # method: retry once with the method the standard Allow header names
    # (the only truthful discovery mechanism), then move on.
    for path, method in (("/api/update", "POST"), ("/api/update", "GET"),
                         ("/update", "POST"), ("/update", "GET")):
        try:
            return await _request(method, path, timeout=_ACTION_TIMEOUT)
        except StudioEndpointMissing:
            continue
        except StudioError as exc:
            if exc.status_code != 405:
                raise
            allow = str((exc.headers or {}).get("allow") or "")
            alt = next((m2.strip().upper() for m2 in allow.split(",")
                        if m2.strip().upper() not in ("HEAD", "OPTIONS", method)), "")
            if alt:
                return await _request(alt, path, timeout=_ACTION_TIMEOUT)
            continue
    raise StudioEndpointMissing("/api/update (or /update)")


async def stt(audio: bytes, filename: str, model: str = "small") -> str:
    """POST /v1/audio/transcriptions (multipart) → transcript text. `model`
    is sent verbatim — the full hub repo id ("unslothai/Qwen3-ASR-1.7B-GGUF")
    is the form Studio's format check wants (observed live: the bare
    basename is rejected as "not owner/model form"). A model Studio hasn't
    downloaded for Voice 409s with its own fix-it instructions; surfaced
    verbatim rather than retried under another name."""
    if not audio:
        raise StudioError("No audio to transcribe", 400)
    url, key = _base_key()
    async with _httpx.AsyncClient(timeout=_STT_TIMEOUT, follow_redirects=True) as c:
        resp = await c.post(f"{url}/v1/audio/transcriptions",
                            files={"file": (filename, audio)},
                            data={"model": model}, headers=_headers(key))
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or \
                  (body_err.get("detail") if isinstance(body_err.get("detail"), str) else None) or \
                  f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    _note_auth_ok()
    data = resp.json()
    return data.get("text") or ""


# ── Per-model load overrides ─────────────────────────────────────────────────
# Studio's own per-model knobs (max_seq_length, llama_extra_args, …) —
# PUT shape verified in Phase 0 findings I-5; the GET side is passthrough
# because only the PUT was live-verified, so unknown/changed shapes flow
# through untouched instead of breaking on a schema guess.

async def auto_switch_overrides_get() -> dict:
    """GET /api/settings/openai-auto-switch/overrides — current per-model
    overrides, Studio's own shape. Raises StudioEndpointMissing on builds
    without the subpath (the route degrades to 'not available')."""
    return await _request("GET", "/api/settings/openai-auto-switch/overrides",
                          timeout=_ACTION_TIMEOUT)


async def auto_switch_overrides_set(body: dict) -> dict:
    """PUT /api/settings/openai-auto-switch/overrides — one override entry
    ({model_id, max_seq_length, …}); body is passed through verbatim so
    fields this build doesn't know about survive the round-trip."""
    return await _request("PUT", "/api/settings/openai-auto-switch/overrides",
                          json_body=body, timeout=_ACTION_TIMEOUT)


# ── Video generation ─────────────────────────────────────────────────────────
# /v1/videos exists on current Studio builds (Phase 0 findings I-9: list
# shape {object:"list", data:[]} verified; generation POST and content
# fetch are shape-defensive until live-verified on the GM's box). All
# polling/assembly decisions live in app/video_jobs.py — these helpers
# only speak HTTP.

async def video_generate(body: dict) -> dict:
    """POST /v1/videos — start a generation; body (model, prompt, size,
    seconds, …) passes through verbatim. Returns Studio's response as-is:
    the video id is extracted defensively by the caller because the exact
    key has not been live-verified."""
    return await _request("POST", "/v1/videos", json_body=body,
                          timeout=_ACTION_TIMEOUT)


async def video_list() -> dict:
    """GET /v1/videos — the jobs/generations list (OpenAI list shape)."""
    return await _request("GET", "/v1/videos", timeout=_PROBE_TIMEOUT)


async def video_content(video_id: str) -> tuple[bytes, str]:
    """GET /v1/videos/{id}/content → (video_bytes, content_type). A long
    read budget: the bytes ARE the deliverable and video files are large."""
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(900.0, connect=10.0),
                                      follow_redirects=True) as c:
            resp = await c.get(f"{url}/v1/videos/{video_id}/content",
                               headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    if not resp.content:
        raise StudioError("Studio returned an empty video file", 502)
    _note_auth_ok()
    return resp.content, resp.headers.get("content-type", "video/mp4")
