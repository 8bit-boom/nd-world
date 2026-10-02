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
import os
import re
import secrets
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
# Image generation on a CPU-only box takes tens of minutes (a 1024 px diffusion pass is ~22 s on a
# GPU and 50-100x that on CPU), so it gets its own, longer, env-tunable budget — the fixed 10 minutes
# above killed STT jobs the same way before it was made tunable.
_IMAGE_TIMEOUT = _httpx.Timeout(
    float(__import__("os").environ.get("UNSLOTH_IMAGE_TIMEOUT_SECONDS", "1800")),
    connect=10.0)
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

    @property
    def unreachable(self) -> bool:
        """True for a transport failure (nothing answered) as opposed to
        Studio answering with an error — the status card's 'reachable'."""
        return str(self).startswith("Unsloth Studio unreachable")


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


def _error_message(resp) -> str:
    """Studio's own error text from a failed response, whatever shape it took: the OpenAI dialect
    ({"error": {"message"}}), a bare string ({"error": "..."}), FastAPI ({"detail": "..." | [...]}),
    a JSON list/string, or a non-JSON body. Never raises — a hostile or odd body must not turn a clean
    StudioError into an AttributeError (and an HTTP 500)."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
        elif isinstance(err, list) and err:
            msg = "; ".join(str(e) for e in err)
        if not msg:
            detail = body.get("detail")
            if isinstance(detail, str):
                msg = detail
            elif isinstance(detail, list) and detail:
                msg = "; ".join(str(d.get("msg") if isinstance(d, dict) else d) for d in detail)
            elif isinstance(detail, dict):
                msg = str(detail.get("message") or detail)
        if not msg and body:
            msg = str(body)
    elif isinstance(body, (list, str)) and body:
        msg = str(body)
    if not msg:
        msg = (resp.text or "").strip()
    return (msg[:300]) if msg else f"HTTP {resp.status_code}"


def _is_route_missing(resp) -> bool:
    """True when a 404 means "this Studio build has no such endpoint" (an empty body, an HTML page, or
    FastAPI's bare "Not Found" / "API endpoint not found") rather than "the endpoint exists and the thing
    you asked about doesn't" (e.g. {"detail": "Repository 'x/y' not found"}) — the latter must keep its
    message instead of telling the GM to update Studio."""
    try:
        body = resp.json()
    except ValueError:
        return True
    if not isinstance(body, dict) or not body:
        return True
    msg = _error_message(resp).strip().lower().rstrip(".")
    return msg in ("not found", "api endpoint not found", "endpoint not found") or "endpoint not found" in msg


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
                   headers_extra: dict | None = None, expect_json: bool = False) -> dict:
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
    # Only auth-required surfaces count toward the key-death banner:
    # /version & friends are commonly public (the live Docker build serves
    # /version unauthenticated), so a public 200 there would silently clear
    # a REAL key-death and a public 401 would arm a false one.
    _auth_relevant = path.startswith("/v1/") or path.startswith("/api/inference/")
    if resp.status_code == 401 and _auth_relevant:
        _note_auth_failure()
    if resp.status_code == 404 and _is_route_missing(resp):
        raise StudioEndpointMissing(path)
    if resp.status_code >= 400:
        # OpenAI-dialect {"error":{"message"}} (the /v1 surface) and
        # FastAPI {"detail"} (the /api surface) both occur — _error_message takes whichever.
        raise StudioError(_error_message(resp), resp.status_code, headers=dict(resp.headers))
    if resp.status_code < 300 and _auth_relevant:
        _note_auth_ok()
    if not resp.content:
        if expect_json:
            raise StudioError(f"Studio answered {path} with nothing — is this URL really a Studio API?", 502)
        return {}
    try:
        data = resp.json()
    except ValueError:
        if expect_json:
            raise StudioError(
                f"Studio answered {path} with a page, not JSON — is this URL really a Studio API?", 502)
        return {}
    if expect_json and not isinstance(data, dict):
        raise StudioError(f"Studio answered {path} with an unexpected shape", 502)
    return data


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
    out = {"path": path, "status": resp.status_code, "snippet": snippet}
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            for k in ("version", "build", "app_version", "studio_version"):
                if data.get(k):
                    out["version"] = str(data[k])
                    break
    return out


async def studio_probe() -> dict:
    """Probe the read-only candidates concurrently (short per-path timeout)
    and return {results: [...], version: str|None}. 404s are DATA here, not
    errors — they're the "build doesn't have it" answer. Any 200 JSON body
    with a version-ish field is extracted best-effort."""
    url, key = _base_key()
    async with _httpx.AsyncClient(timeout=_httpx.Timeout(6.0, connect=4.0),
                                  follow_redirects=True) as c:
        import asyncio as _asyncio
        raw = await _asyncio.gather(*[_probe_one(c, url, key, p) for p in _PROBE_PATHS])
    version = next((r["version"] for r in raw if r.get("version")), None)
    return {"results": raw, "version": version}


# ── Model hub ────────────────────────────────────────────────────────────────

async def hub_cached() -> list[dict]:
    """Every GGUF cached in Studio's hub — chat AND image models (verified
    Phase 0.5): [{repo_id, size_bytes, load_id, task, capabilities, ...}].
    `task` separates text-generation / text-to-image / audio rows."""
    data = await _request("GET", "/api/hub/cached-gguf")
    return data.get("cached") or []


async def hub_download_start(repo_id: str, gguf_variant: str = "") -> dict:
    """Start downloading a hub model — returns immediately; poll
    hub_download_progress(). Shape verified Phase 0.5:
    {job_key, state:"running", accepted:true, transport}. `gguf_variant` (the quant, for repos that
    need one) is sent only when given — the field name is the one Studio's image-load flow uses; the
    download side of it was not live-verified, so a Studio that rejects it answers with its own message."""
    body = {"repo_id": repo_id}
    if gguf_variant:
        body["gguf_variant"] = gguf_variant
    return await _request("POST", "/api/hub/download", json_body=body, timeout=_ACTION_TIMEOUT)


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
                          timeout=_IMAGE_TIMEOUT)
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
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code >= 400:
        raise StudioError(f"Fetching generated image failed: HTTP {resp.status_code}", resp.status_code)
    _note_auth_ok()
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

# Studio's /v1/audio/speech rejects every other format with 400 "Unsupported response_format 'mp3'.
# Only 'wav' is supported." (seen live with unsloth/orpheus-3b), so WAV is what nd-world asks for.
TTS_RESPONSE_FORMAT = "wav"


async def tts(text: str, model: str, voice: str = "", response_format: str = TTS_RESPONSE_FORMAT,
              speed: float = 1.0, instructions: str = "", language: str = "") -> tuple[bytes, str]:
    """Speech for `text` as (audio_bytes, content_type): Studio's answer (see _tts_request), converted to
    Ogg Opus when configured and possible (see _finish_tts_audio)."""
    audio, content_type = await _tts_request(text, model, voice, response_format, speed, instructions, language)
    return await _finish_tts_audio(audio, content_type)


async def _tts_request(text: str, model: str, voice: str = "", response_format: str = TTS_RESPONSE_FORMAT,
                       speed: float = 1.0, instructions: str = "", language: str = "") -> tuple[bytes, str]:
    """POST /v1/audio/speech → (audio_bytes, content_type) exactly as Studio sent it. The TTS model
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
        raise StudioError(_error_message(resp), resp.status_code)
    _note_auth_ok()
    audio = resp.content
    if not audio:
        raise StudioError("Studio returned no audio for this TTS request", 502)
    content_type = resp.headers.get("content-type", "")
    if _is_wav(audio):
        # Callers choose the file extension from this; the bytes are the truth, not a generic header.
        content_type = "audio/wav"
    return audio, content_type or "audio/wav"


def _is_wav(audio: bytes) -> bool:
    return audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


async def _finish_tts_audio(audio: bytes, content_type: str) -> tuple[bytes, str]:
    """What gets stored: Studio's WAV converted to Ogg Opus (TTS_OUTPUT_FORMAT=opus, the default), or the
    audio untouched when it is not a WAV, the setting says wav, or ffmpeg cannot do it - a good clip is
    never lost to the conversion."""
    if _is_wav(audio) and _tts_output_format() == "opus":
        opus = await _wav_to_opus(audio)
        if opus:
            return opus, OPUS_CONTENT_TYPE
    return audio, content_type


def channel_label(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels} channels")


def wav_info(audio: bytes) -> dict | None:
    """Channels, sample rate, bit depth and length of a WAV, read from its header (no ffmpeg). None when the
    bytes are not a WAV this can read. Tolerates a streaming header (RIFF/data sizes of 0xFFFFFFFF, written
    by a server that does not know the length up front): the length is then what is actually there."""
    if not _is_wav(audio) or len(audio) < 12:
        return None
    fmt = None
    pos = 12
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        size = int.from_bytes(audio[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + 16 > len(audio):
                return None
            channels = int.from_bytes(audio[body + 2:body + 4], "little")
            rate = int.from_bytes(audio[body + 4:body + 8], "little")
            bits = int.from_bytes(audio[body + 14:body + 16], "little")
            if channels < 1 or rate < 1 or bits < 1:
                return None
            fmt = (channels, rate, bits)
        elif chunk_id == b"data":
            if fmt is None:
                return None
            channels, rate, bits = fmt
            length = min(size, len(audio) - body)          # a streaming header's 0xFFFFFFFF, or a short read
            frame_bytes = channels * max(1, bits // 8)
            return {"channels": channels, "sample_rate": rate, "bits": bits,
                    "seconds": round(length / (rate * frame_bytes), 3)}
        pos = body + size + (size & 1)                      # chunks are word-aligned
    return None


# ── WAV → Ogg Opus ───────────────────────────────────────────────────────────
# Studio can only hand back WAV (uncompressed, ~50 KB/s). Opus is the better codec for speech - far smaller
# than WAV and cleaner than MP3 at any size - so clips are stored as Ogg Opus when ffmpeg (already in the
# image for transcription) can make it. TTS_OUTPUT_FORMAT=wav turns this off, e.g. for a player whose
# browser cannot play Opus; TTS_OPUS_BITRATE (default 48k) trades size for fidelity.

OPUS_CONTENT_TYPE = "audio/ogg; codecs=opus"
_DEFAULT_OPUS_BITRATE = "48k"
_OPUS_TIMEOUT_SECONDS = 120


def _tts_output_format() -> str:
    """"opus" (default) or "wav". Read per call so a changed environment needs no import-time state."""
    return "wav" if (os.environ.get("TTS_OUTPUT_FORMAT") or "").strip().lower() == "wav" else "opus"


_OPUS_MAX_REQUEST_KBPS = 512        # Opus' ceiling for a stereo stream
_OPUS_MAX_KBPS_PER_CHANNEL = 256    # what ffmpeg's libopus accepts per channel (asking for more is an error)


def _requested_opus_kbps() -> int:
    """TTS_OPUS_BITRATE as kbit/s: "48k" ... "512k", or "max" (= 512k). Anything unparseable, or under 6k,
    is the default; above 512k is clamped to 512k. Nothing but digits ever reaches the command line."""
    raw = (os.environ.get("TTS_OPUS_BITRATE") or "").strip().lower()
    if raw == "max":
        return _OPUS_MAX_REQUEST_KBPS
    m = re.fullmatch(r"(\d{1,4})k", raw)
    if not m or int(m.group(1)) < 6:
        return int(_DEFAULT_OPUS_BITRATE[:-1])
    return min(int(m.group(1)), _OPUS_MAX_REQUEST_KBPS)


def _opus_bitrate(channels: int = 1) -> str:
    """The bitrate ffmpeg is actually given: the request, capped at 256k per channel. 512k is therefore
    the stereo maximum - a mono clip (what TTS models produce) is capped at 256k, not failed."""
    return f"{min(_requested_opus_kbps(), _OPUS_MAX_KBPS_PER_CHANNEL * max(1, channels))}k"


async def _wav_to_opus(wav: bytes) -> bytes | None:
    """WAV bytes → Ogg Opus bytes through ffmpeg (stdin → stdout, nothing touches disk). None when ffmpeg
    is missing, fails, times out or produces something that is not an Ogg stream - the caller keeps the WAV."""
    import asyncio as _asyncio
    proc = None
    info = wav_info(wav)
    try:
        proc = await _asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0",
            "-vn", "-c:a", "libopus", "-b:a", _opus_bitrate(info["channels"] if info else 1), "-f", "ogg", "pipe:1",
            stdin=_asyncio.subprocess.PIPE, stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.DEVNULL)
        out, _ = await _asyncio.wait_for(proc.communicate(wav), timeout=_OPUS_TIMEOUT_SECONDS)
    except Exception as exc:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        _log.warning("TTS WAV->Opus conversion failed (%s: %s) - keeping the WAV", type(exc).__name__, exc)
        return None
    if proc.returncode != 0 or not out.startswith(b"OggS"):
        _log.warning("TTS WAV->Opus conversion gave no usable output (ffmpeg exit %s) - keeping the WAV", proc.returncode)
        return None
    return out


def audio_extension(content_type: str | None) -> str:
    """File extension for audio returned by tts(): .opus (Ogg Opus), .wav, else .mp3."""
    ct = (content_type or "").lower()
    if "opus" in ct or "ogg" in ct:
        return ".opus"
    if "wav" in ct:
        return ".wav"
    return ".mp3"


async def verify_key(key: str | None = None) -> dict:
    """Ask Studio one authenticated question and say what it answered: {ok, status, message, url} (+
    `unreachable` when nothing answered). With no argument it checks the key currently in effect and
    feeds the 'Studio rejected the key' banner; with `key` it checks a CANDIDATE without touching any
    state, so a pasted key can be tried before it replaces a working one. Never raises."""
    url = effective_llm_url()
    using_effective = key is None
    key = effective_llm_api_key() if using_effective else key
    if not key:
        return {"ok": False, "status": 0, "url": url, "message": "No Studio API key is set."}
    try:
        async with _httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as c:
            resp = await c.get(f"{url}/api/hub/cached-gguf", headers=_headers(key))
    except _httpx.HTTPError as exc:
        return {"ok": False, "status": 503, "url": url, "unreachable": True,
                "message": f"Unsloth Studio unreachable at {url}: {type(exc).__name__}: {exc}"}
    if resp.status_code == 401:
        if using_effective:
            _note_auth_failure()
        return {"ok": False, "status": 401, "url": url, "message": _error_message(resp)}
    if resp.status_code >= 400:
        return {"ok": False, "status": resp.status_code, "url": url, "message": _error_message(resp)}
    if using_effective:
        _note_auth_ok()
    return {"ok": True, "status": 200, "url": url, "message": "Studio accepted the key."}


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


# Health checks are diagnostic clicks behind the same proxy as everything
# else — bound them well under its ~100 s ceiling so the button always
# ANSWERS (a slow real transcription surfaces as a timeout message, not a
# hung request; audit 2026-09-30, unsloth finding 4).
_HEALTHCHECK_TIMEOUT_SECONDS = float(
    __import__("os").environ.get("UNSLOTH_HEALTHCHECK_TIMEOUT_SECONDS", "75"))

# The Settings "Test TTS / Test STT" buttons run in the BACKGROUND with this much longer budget: the first request
# after Studio starts loads the speech model (possibly unloading the chat model first), which on a slow or
# CPU-only box takes minutes - and no single request can wait that long (a Cloudflare Tunnel closes at ~100 s).
# The page gets "still working" answers and polls; the synchronous 75 s above still guards the live-recording
# pre-flight, which has to answer when a recording starts.
_SLOW_CHECK_TIMEOUT_SECONDS = float(os.environ.get("UNSLOTH_SLOW_CHECK_TIMEOUT_SECONDS", "300"))
_DEFAULT_CHECK_WAIT_SECONDS = 25.0   # how long ONE http request waits before answering "pending" (well under 100 s)
_CHECK_WAIT_SECONDS = _DEFAULT_CHECK_WAIT_SECONDS
_CHECK_KEEP_SECONDS = 600.0     # a finished check can be polled for this long
_CHECKS: dict = {}              # token -> {"token", "kind", "key", "started", "finished", "task"}


def _prune_checks(now: float) -> None:
    for token, e in list(_CHECKS.items()):
        if e["task"].done():
            if e["finished"] is None:
                e["finished"] = now
            if now - e["finished"] > _CHECK_KEEP_SECONDS:
                del _CHECKS[token]


async def _wait_check(entry: dict) -> dict:
    """Wait up to _CHECK_WAIT_SECONDS for the check: its own result dict once finished, otherwise a "pending"
    answer the page polls again. The check keeps running either way (asyncio.wait never cancels it)."""
    import asyncio as _asyncio
    done, _ = await _asyncio.wait({entry["task"]}, timeout=_CHECK_WAIT_SECONDS)
    if entry["task"] in done:
        if entry["finished"] is None:
            entry["finished"] = time.monotonic()
        try:
            return entry["task"].result()
        except Exception as exc:                               # a bug in a check must not become a 500
            _log.warning("health check %s crashed: %s: %s", entry["kind"], type(exc).__name__, exc)
            return {"ok": False, "message": f"The check failed unexpectedly: {type(exc).__name__}: {exc}"}
    return {"ok": None, "pending": True, "token": entry["token"],
            "elapsed": int(time.monotonic() - entry["started"]),
            "message": "Studio is still working - the first request after it starts is loading the speech model, "
                       "which can take a few minutes. Waiting for it..."}


async def run_check(kind: str, key: str, factory) -> dict:
    """Start the `kind` health check (or join the one already running for the same `key`: model, voice, ...) and
    wait briefly for it. `factory()` is the coroutine doing the real work."""
    import asyncio as _asyncio
    now = time.monotonic()
    _prune_checks(now)
    entry = next((e for e in _CHECKS.values() if e["key"] == key and not e["task"].done()), None)
    if entry is None:
        token = secrets.token_urlsafe(12)
        entry = {"token": token, "kind": kind, "key": key, "started": now, "finished": None,
                 "task": _asyncio.ensure_future(factory())}
        _CHECKS[token] = entry
    return await _wait_check(entry)


async def poll_check(token: str) -> dict | None:
    """The state of a check started by run_check, or None if there is no such check (unknown, expired, or nd-world
    restarted since)."""
    _prune_checks(time.monotonic())
    entry = _CHECKS.get(token)
    return await _wait_check(entry) if entry else None


_SNAPSHOT_TIMEOUT_SECONDS = 8.0


async def _studio_snapshot() -> str:
    """After a health check timed out: one plain status question to Studio, so the message can say whether Studio
    is alive and what it has loaded - the difference between "the speech model never finished" and "Studio is
    wedged". Never raises."""
    import asyncio as _asyncio
    t0 = time.monotonic()
    try:
        async with _asyncio.timeout(_SNAPSHOT_TIMEOUT_SECONDS):
            rows = await loaded_models()
    except Exception as exc:
        return ("Studio did not answer a plain status request either "
                f"({type(exc).__name__}), so it looks wedged or still starting - restart the unsloth container.")
    names = [str(r.get("model") or r.get("id") or r.get("name") or "?") for r in (rows or []) if isinstance(r, dict)]
    have = ("has loaded: " + ", ".join(names)) if names else "has nothing loaded"
    return (f"Studio itself is responding ({time.monotonic() - t0:.1f} s) and {have} - so it never finished loading "
            "or running the speech model.")


def _budget_env(timeout: float | None) -> str:
    return "UNSLOTH_SLOW_CHECK_TIMEOUT_SECONDS" if timeout else "UNSLOTH_HEALTHCHECK_TIMEOUT_SECONDS"


def _timeout_message(budget: float, env_name: str) -> str:
    return (f"Studio did not answer within {budget:g} s. The first request after Studio starts has to load the speech "
            "model (it may unload the chat model first), which can take minutes on a slow or CPU-only box - try again "
            "in a minute, or watch it load with `docker logs -f nd-world-unsloth`. If it keeps timing out the "
            f"backend may be wedged. (The wait is {env_name}.)")


async def _tone_webm_bytes(seconds: float = 2.0) -> bytes | None:
    """The same tone as Opus in a WebM container — what a browser's
    MediaRecorder hands the live-recording panel, so a health check can prove
    Studio accepts THAT format and not just a WAV. None when ffmpeg is missing
    or fails (the caller falls back to the WAV)."""
    import asyncio as _asyncio
    try:
        proc = await _asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k", "-f", "webm", "pipe:1",
            stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.DEVNULL)
        out, _ = await _asyncio.wait_for(proc.communicate(), timeout=15)
    except Exception:
        return None
    return out if proc.returncode == 0 and out else None


async def stt_health(model: str, fmt: str = "wav", timeout: float | None = None) -> dict:
    """One-shot 'does this STT model actually work' probe — the exact call
    nd-world's transcription pipeline makes, over a synthesized tone, so a
    'not downloaded' 409 or bad model name surfaces at setup time in
    Settings instead of as a failed background job hours later. Never
    raises: every failure mode comes back as {ok: False, message}."""
    import asyncio as _asyncio
    budget = timeout or _HEALTHCHECK_TIMEOUT_SECONDS
    try:
        # The tone is ~1 s of audio; the budget covers the slowest real
        # first-use load of the model, not a full transcription pass.
        audio, name = None, "nd-health-check.wav"
        if fmt == "webm":
            # The live-recording panel's own chunk format; WAV if ffmpeg can't make it.
            audio = await _tone_webm_bytes()
            name = "nd-health-check.webm"
        if audio is None:
            audio, name = _tone_wav_bytes(), "nd-health-check.wav"
        async with _asyncio.timeout(budget):
            await stt(audio, name, model=model or "small")
        return {"ok": True, "message": "Studio accepted the audio — model is ready.", "format": name.rsplit(".", 1)[-1]}
    except StudioMissing as exc:
        return {"ok": False, "message": str(exc)}
    except _asyncio.TimeoutError:
        return {"ok": False, "message": _timeout_message(budget, _budget_env(timeout)) + " " + await _studio_snapshot()}
    except StudioError as exc:
        message = str(exc)
        if exc.status_code == 409 and "not downloaded" in message.lower():
            message = f"{message} {await _stt_not_downloaded_hint(model)}".strip()
        return {"ok": False, "status": exc.status_code, "message": message}


# The names Studio's /v1/audio/transcriptions accepts for its Whisper models.
_STT_API_NAMES = ("large-v3-turbo", "large-v3", "turbo", "medium", "small", "base", "tiny", "large")


async def _stt_not_downloaded_hint(model: str) -> str:
    """Studio answers 409 "not downloaded" both when a model really is missing and when it is on disk
    but not loadable through the OpenAI-style API (seen with Qwen3-ASR: listed "On Device" in Studio's
    own UI, refused here — docs/UNSLOTH_PHASE0_FINDINGS.md). Tell the two apart by listing what Studio
    has, and name the Whisper models that do work. Best effort: any failure just returns the plain advice."""
    try:
        rows = (await audio_models()).get("stt") or []
    except Exception:
        return "Download it in Studio (Settings → Voice) and try again."
    repo_ids = [str(r.get("repo_id") or "") for r in rows]
    wanted = (model or "").strip().lower()
    listed = any(r.lower() == wanted or r.lower().rsplit("/", 1)[-1] == wanted.rsplit("/", 1)[-1] for r in repo_ids)
    if not listed:
        return "Download it in Studio first (Settings → Voice), then test again."
    # "unsloth/whisper-large-v3-turbo" is spelled "large-v3-turbo" in the API
    on_device = []
    for r in repo_ids:
        base = r.rsplit("/", 1)[-1].lower()
        if "whisper" in base:
            name = base.replace("whisper-", "", 1)
            if name in _STT_API_NAMES and name not in on_device:
                on_device.append(name)
    use = ", ".join(on_device) if on_device else "large-v3-turbo"
    qwen = (" This is how Qwen3-ASR behaves on the Studio builds tested so far." if "qwen" in wanted else "")
    return (f"Studio lists this model as on device, but its OpenAI-compatible API refused it.{qwen} "
            f"Pick a Whisper model instead — on your device: {use} — and test again.")


async def tts_health(model: str, voice: str = "", instructions: str = "", language: str = "",
                     timeout: float | None = None) -> dict:
    """Same idea for TTS: synthesize a two-word clip through the configured
    model/voice/style. Audio is discarded — ok + message is the result."""
    import asyncio as _asyncio
    budget = timeout or _HEALTHCHECK_TIMEOUT_SECONDS
    try:
        async with _asyncio.timeout(budget):
            sent, sent_ct = await _tts_request("Ready.", model=model or "", voice=voice,
                                               instructions=instructions, language=language)
            saved, saved_ct = await _finish_tts_audio(sent, sent_ct)
        message = f"Studio synthesized {len(sent)} bytes — model is ready."
        result: dict = {"ok": True}
        info = wav_info(sent)
        if info:
            label = channel_label(info["channels"])
            message += (f" Studio's audio: WAV, {label}, {info['sample_rate']} Hz, {info['bits']}-bit, "
                        f"{info['seconds']:g} s.")
            result["audio"] = {"format": "wav", "channels": info["channels"], "channel_label": label,
                               "sample_rate": info["sample_rate"], "bits": info["bits"],
                               "seconds": info["seconds"], "bytes": len(sent)}
        else:
            message += f" Studio's audio: {sent_ct or 'unknown format'} (format details are read from WAV only)."
        if saved_ct == OPUS_CONTENT_TYPE:
            channels = info["channels"] if info else 1
            used, wanted = _opus_bitrate(channels), f"{_requested_opus_kbps()}k"
            result["saved_as"] = {"format": "opus", "bytes": len(saved), "bitrate": used, "requested": wanted}
            message += f" Saved as: Ogg Opus at {used}, {len(saved)} bytes."
            if used != wanted:
                message += (f" (TTS_OPUS_BITRATE asks for {wanted}; Opus allows at most 256k per channel, "
                            f"so a {channel_label(channels)} clip is capped at {used}.)")
        else:
            result["saved_as"] = {"format": "wav" if _is_wav(saved) else (saved_ct or "unknown"), "bytes": len(saved)}
            if not _is_wav(saved):
                message += " Saved as: unchanged (not a WAV, so nothing to convert)."
            elif _tts_output_format() == "wav":
                message += f" Saved as: WAV, {len(saved)} bytes, as Studio sent it (TTS_OUTPUT_FORMAT=wav)."
            else:
                message += (f" Saved as: WAV, {len(saved)} bytes - ffmpeg could not convert it to Opus, so clips will be "
                            "saved as WAV (larger files). Is ffmpeg installed in the nd-world container?")
        result["message"] = message
        return result
    except StudioMissing as exc:
        return {"ok": False, "message": str(exc)}
    except _asyncio.TimeoutError:
        return {"ok": False, "message": _timeout_message(budget, _budget_env(timeout)) + " " + await _studio_snapshot()}
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
            if isinstance(data, dict) and data.get(k):
                return str(data[k])
        # /api/about is a page-ish endpoint — if it 200s with HTML, don't
        # keep scanning forever for a field that isn't coming.
        break
    return ""


async def studio_update() -> dict:
    """Ask Studio to update itself — feature-detected via /api/update. This is a MUTATING action, so
    only POST (or PUT, when a 405's standard Allow header names it) is ever tried — never GET, which
    could be a harmless page on a Studio that serves its UI at /update — and the answer must be a JSON
    object: a 200 HTML page is not "an update was requested". Builds without any of these raise
    StudioEndpointMissing and the route tells the GM the manual path; nd-world never shells out to
    Docker."""
    for path in ("/api/update", "/update"):
        try:
            return await _request("POST", path, timeout=_ACTION_TIMEOUT, expect_json=True)
        except StudioEndpointMissing:
            continue
        except StudioError as exc:
            if exc.status_code != 405:
                raise
            allow = str((exc.headers or {}).get("allow") or "").upper()
            if "PUT" in [m.strip() for m in allow.split(",")]:
                return await _request("PUT", path, timeout=_ACTION_TIMEOUT, expect_json=True)
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
    try:
        async with _httpx.AsyncClient(timeout=_STT_TIMEOUT, follow_redirects=True) as c:
            resp = await c.post(f"{url}/v1/audio/transcriptions",
                                files={"file": (filename, audio)},
                                data={"model": model}, headers=_headers(key))
    except _httpx.HTTPError as exc:
        raise StudioError(f"Unsloth Studio unreachable: {type(exc).__name__}: {exc}", 503) from exc
    if resp.status_code == 401:
        _note_auth_failure()
    if resp.status_code >= 400:
        raise StudioError(_error_message(resp), resp.status_code)
    _note_auth_ok()
    try:
        data = resp.json()
    except ValueError:
        raise StudioError("Studio returned a non-JSON transcription response", 502)
    if not isinstance(data, dict):
        raise StudioError("Studio returned an unexpected transcription response", 502)
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


_CTX_KEYS = ("max_seq_length", "context_length", "n_ctx", "ctx_size")


def _ctx_of(entry) -> int | None:
    if isinstance(entry, dict):
        for k in _CTX_KEYS:
            try:
                n = int(entry.get(k))
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
    return None


def context_length_for(data, model_id: str) -> int | None:
    """The context length Studio will load `model_id` with, read out of whatever shape the per-model
    overrides answer has ({"overrides": {id: {...}}}, {"overrides": [{model_id, ...}]}, a bare list or
    a bare {id: {...}}) — only the PUT side of that endpoint was live-verified, so this is tolerant and
    returns None for anything it can't place (no warning is better than a wrong one)."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return None

    def same(x) -> bool:
        return isinstance(x, str) and x.strip().lower() == mid

    node = data
    if isinstance(node, dict) and isinstance(node.get("overrides"), (dict, list)):
        node = node["overrides"]
    if isinstance(node, dict):
        for k, v in node.items():
            if same(k):
                return _ctx_of(v)
        return None
    if isinstance(node, list):
        for e in node:
            if isinstance(e, dict) and any(same(e.get(k)) for k in ("model_id", "model", "repo_id")):
                return _ctx_of(e)
    return None


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
        raise StudioError(_error_message(resp), resp.status_code)
    if not resp.content:
        raise StudioError("Studio returned an empty video file", 502)
    _note_auth_ok()
    return resp.content, resp.headers.get("content-type", "video/mp4")
