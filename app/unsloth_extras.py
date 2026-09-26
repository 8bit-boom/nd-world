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


class StudioMissing(Exception):
    """No Unsloth backend configured (key empty) — the route should 400."""


class StudioError(Exception):
    """The Studio server rejected or failed a call — carries the server's
    own message so the UI can show exactly what Studio said."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


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
        raise StudioError(message, resp.status_code)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


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
              speed: float = 1.0) -> tuple[bytes, str]:
    """POST /v1/audio/speech → (audio_bytes, content_type). The TTS model
    must be loaded in Studio (or media auto-switch on) — a missing model
    surfaces as StudioError with Studio's own message. `voice` is free
    text (OpenAI-style voice names; Studio's Voice settings page manages
    the actual TTS model/voices)."""
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
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/speech", json=body, headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    audio = resp.content
    if not audio:
        raise StudioError("Studio returned no audio for this TTS request", 502)
    return audio, resp.headers.get("content-type", "audio/mpeg")


async def stt(audio: bytes, filename: str, model: str = "small") -> str:
    """POST /v1/audio/transcriptions (multipart) → transcript text. `model`
    maps to a Studio-managed STT model name (e.g. "small") — a missing one
    409s with Studio's own instructions, surfaced verbatim."""
    if not audio:
        raise StudioError("No audio to transcribe", 400)
    url, key = _base_key()
    try:
        async with _httpx.AsyncClient(timeout=_AUDIO_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/transcriptions", files={"file": (filename, audio)},
                                data={"model": model}, headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code >= 400:
        try:
            body_err = resp.json()
        except ValueError:
            body_err = {}
        message = ((body_err.get("error") or {}).get("message")) or \
                  (body_err.get("detail") if isinstance(body_err.get("detail"), str) else None) or \
                  f"HTTP {resp.status_code}"
        raise StudioError(message, resp.status_code)
    data = resp.json()
    return data.get("text") or ""
