import asyncio
import os
import json as _json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse
from collections.abc import AsyncGenerator
import ollama as _ollama
import httpx as _httpx

from .imaging import make_thumbnail
from .job_shutdown import JobInterrupted

_log = logging.getLogger("nd.ai")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

# Concurrency limits for BACKGROUND-JOB work only (app/audio_jobs.py,
# app/chat_jobs.py) — not the interactive chat/ask-AI/condense routes a GM
# is actively waiting on, which should never queue behind a background job.
# Without these, two session-recap jobs queued together interleave Whisper
# chunks (or Ollama calls) against each other on the same backend, roughly
# doubling wall time for both and thrashing whatever's resident in VRAM.
# Held for the FULL duration of one transcribe_audio/summarize_transcript/
# condense_recap/generate_chat call (including that call's own internal
# per-chunk loop), not just one HTTP request, so a job's chunks always run
# back-to-back rather than interleaved with another job's. Env-tunable in
# case a beefier host can genuinely run more than one at a time.
WHISPER_JOB_CONCURRENCY = max(1, int(os.getenv("WHISPER_JOB_CONCURRENCY", "1")))
OLLAMA_JOB_CONCURRENCY = max(1, int(os.getenv("OLLAMA_JOB_CONCURRENCY", "1")))
whisper_job_semaphore = asyncio.Semaphore(WHISPER_JOB_CONCURRENCY)
ollama_job_semaphore = asyncio.Semaphore(OLLAMA_JOB_CONCURRENCY)

# Optional whisper.cpp server (see the "whisper" Compose profile) for
# transcribing an audio chat attachment into text — blank like IMAGEGEN_URL
# below, since it's an optional add-on with no sane always-on default rather
# than something like Ollama a bare install is expected to reach locally.
WHISPER_URL = os.getenv("WHISPER_URL", "").rstrip("/")

# How long a single /inference call is allowed to run — this is actual
# transcription time, not network latency, and CPU-only whisper.cpp can run
# well under realtime speed depending on the host and model size, so a full
# multi-hour session recording can legitimately take a long time to
# transcribe. Defaults to 8 hours: a too-short timeout costs a lot (a silent
# empty transcript — see transcribe_audio's except block — that looks
# identical to "Whisper isn't configured" from the caller's side, after
# potentially hours of otherwise-successful processing), while a too-long
# one costs almost nothing (it only matters if Whisper is genuinely stuck,
# not just slow). Env-overridable either direction.
WHISPER_TIMEOUT_SECONDS = float(os.getenv("WHISPER_TIMEOUT_SECONDS", str(8 * 3600)))

# Runtime overrides (set from AppSettings via POST /settings/system, without
# needing a restart — see main.py's _refresh_settings_overrides()). Blank means
# "use the env-var default above."
_ollama_url_override: str = ""
_ollama_model_override: str = ""
_whisper_url_override: str = ""


def set_ollama_override(url: str, model: str) -> None:
    global _ollama_url_override, _ollama_model_override
    _ollama_url_override = (url or "").rstrip("/")
    _ollama_model_override = model or ""


def effective_ollama_url() -> str:
    return _ollama_url_override or OLLAMA_URL


def effective_ollama_model() -> str:
    return _ollama_model_override or OLLAMA_MODEL


def set_whisper_override(url: str) -> None:
    global _whisper_url_override
    _whisper_url_override = (url or "").rstrip("/")


def effective_whisper_url() -> str:
    return _whisper_url_override or WHISPER_URL


# Per-request Ollama generation tuning (temperature, num_ctx, mirostat, etc.)
# from AppSettings — see main.py's _refresh_settings_overrides(). `options`
# only ever holds fields the GM actually set (blank/None fields are stripped
# before this is called), so anything unset here just omits that key and lets
# Ollama/the model's own Modelfile default apply. keep_alive is a separate
# top-level kwarg on .chat()/.generate(), not nested inside options.
_ollama_options_override: dict = {}
_ollama_keep_alive_override: str = ""
# Per-model overrides layered on TOP of the two globals above — a GM-editable
# {model_id: {"options": {...}, "keep_alive": "..."}} map (see
# AppSettings.ollama_model_overrides_json's own docstring for the field
# scope/reasoning). A model with no entry here is unaffected; the two
# globals above still apply to it exactly as before this existed.
_ollama_model_overrides: dict = {}


def set_ollama_generation_overrides(options: dict, keep_alive: str = "", model_overrides: dict = None) -> None:
    global _ollama_options_override, _ollama_keep_alive_override, _ollama_model_overrides
    _ollama_options_override = dict(options) if options else {}
    _ollama_keep_alive_override = keep_alive or ""
    _ollama_model_overrides = dict(model_overrides) if model_overrides else {}


def effective_ollama_options(model: str = "") -> dict:
    """The instance-wide generation options, with `model`'s own per-model
    override (if any) layered on top — per-model wins field-by-field, an
    unset field on the per-model side still falls back to the instance-wide
    value, same "layer, don't replace" rule _chat_kwargs already applies for
    a caller's own extra_options."""
    opts = dict(_ollama_options_override)
    if model:
        opts.update(_ollama_model_overrides.get(model, {}).get("options", {}))
    return opts


def effective_ollama_keep_alive(model: str = "") -> str:
    if model:
        per_model = _ollama_model_overrides.get(model, {}).get("keep_alive", "")
        if per_model:
            return per_model
    return _ollama_keep_alive_override


_model_capabilities_cache: dict[str, list[str]] = {}


def _known_model_thinks(model: str) -> bool:
    """True if `model` appears in KNOWN_MODELS with "thinking": True.

    Used as a fallback by _model_supports_thinking when Ollama's own
    /api/show doesn't tag the model with the "thinking" capability — which
    is the normal case for any model pulled as a raw GGUF via the
    hf.co/{user}/{repo}:{filename} tag (including the Unsloth IQ4_NL
    quantisation). Official ollama.com library models carry the tag in
    their Modelfile; raw GGUFs don't, so without this fallback
    _chat_kwargs would silently downgrade think=True to False for them
    even though the model is fully capable of thinking mode."""
    return any(m.get("id") == model and m.get("thinking") for m in KNOWN_MODELS)


async def _model_supports_thinking(model: str) -> bool:
    """Whether `model` supports thinking mode — checked via Ollama's
    /api/show capabilities tag first, then by KNOWN_MODELS' own
    "thinking": True flag as a fallback for raw-GGUF models that Ollama
    won't tag automatically.

    A model pulled as a raw GGUF — including via this app's own Hugging
    Face search/upload features — doesn't reliably carry the "thinking"
    capability tag the way an official ollama.com library model's
    Modelfile does, and Ollama's /api/chat rejects think=True outright
    ("<model> does not support thinking", HTTP 400) for anything not
    tagged, rather than silently ignoring the request. See _chat_kwargs
    below for where this gates a requested think=True back down to False
    instead of letting that 400 reach the user as a raw error.

    The KNOWN_MODELS fallback is authoritative for models we've
    explicitly listed as thinking-capable (e.g. the Unsloth IQ4_NL
    quantisation) — "thinking": True in that list means we've confirmed
    the model handles thinking tokens correctly even though Ollama's own
    tag won't be set.

    Cached per-model for the life of the process — capabilities are static
    for an already-pulled model, and a restart (e.g. after a Watchtower
    deploy) naturally clears this if a model is ever replaced. Only called
    when think is actually truthy (see below), so the common think=False
    path never pays for the extra /api/show round trip at all."""
    if model in _model_capabilities_cache:
        return "thinking" in _model_capabilities_cache[model]
    caps: list[str] = []
    try:
        resp = await _client().show(model)
        caps = list(resp.capabilities or [])
    except Exception:
        pass  # fail soft — check KNOWN_MODELS below before giving up
    if "thinking" not in caps and _known_model_thinks(model):
        caps = list(caps) + ["thinking"]
    _model_capabilities_cache[model] = caps
    return "thinking" in caps


async def _chat_kwargs(extra_options: dict = None, think: bool = False, model: str = "") -> dict:
    """Extra kwargs (options=, keep_alive=, think=) to splat into every
    .chat() call below — built fresh each call so a runtime settings change
    (no server restart needed) takes effect on the very next request.
    `extra_options` (a per-request override — see a chat preset's options,
    app/routers/ai.py) is layered OVER the instance-wide AppSettings
    defaults, not replacing them: an unset key still falls back to whatever
    Settings > System configured, so a preset only has to specify what it
    wants to differ. `model`, if given (every call site below already has
    it resolved as `m` right before calling this), layers that model's own
    Settings > System per-model override between those two — instance-wide
    < per-model < extra_options, each layer only filling in what the one
    before it left unset.

    think defaults to False for every caller that doesn't pass it —
    parse_facts_from_recap/parse_entity_from_text/generate_session_prep
    need clean JSON back and benchmark_model needs a stable timing
    comparison, so none of those ever opt in. Without think=False, a
    thinking-capable model can spend its whole output budget on reasoning
    tokens and return an empty `content` with `thinking` full of text
    instead — see generate_chat's empty-content handling below for what
    happens if that still slips through (a model that doesn't honor
    think=False, or a genuinely empty answer). The session-recap-assist
    family (expand_recap_notes/condense_recap/summarize_transcript/
    summarize_session_from_facts) defaults ITS OWN think to True instead —
    see their docstrings — and is the only thing that ever passes
    think=True down to here; a GM's "Thinking" checkbox on those pages
    controls it per call.

    A requested think=True is downgraded to False when `model` isn't
    actually tagged as thinking-capable — see _model_supports_thinking."""
    if think and model and not await _model_supports_thinking(model):
        think = False
    kwargs = {"think": think}
    opts = {**effective_ollama_options(model), **(extra_options or {})}
    if opts:
        kwargs["options"] = opts
    keep_alive = effective_ollama_keep_alive(model)
    if keep_alive:
        kwargs["keep_alive"] = keep_alive
    return kwargs

_DATA_DIR = Path(os.getenv("DB_PATH", "/data/world.db")).parent
_CUSTOM_MODELS_FILE = _DATA_DIR / "ai_models.json"

KNOWN_MODELS = [
    {"id": "gemma4:26b", "label": "Gemma 4 26B"},
    {
        "id": "hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf",
        "label": "Gemma 4 26B MXFP4",
    },
    {
        "id": "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf",
        "label": "Gemma 4 26B IQ4_NL (Unsloth)",
        "thinking": True,
    },
]


def _client() -> _ollama.AsyncClient:
    return _ollama.AsyncClient(host=effective_ollama_url())


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_data() -> dict:
    try:
        return _json.loads(_CUSTOM_MODELS_FILE.read_text())
    except Exception:
        return {"custom": [], "hidden": []}


def _save_data(data: dict) -> None:
    _CUSTOM_MODELS_FILE.write_text(_json.dumps(data, indent=2))


def load_custom_models() -> list[dict]:
    return _load_data().get("custom", [])


def load_hidden_ids() -> set:
    return set(_load_data().get("hidden", []))


def save_custom_models(models: list[dict]) -> None:
    data = _load_data()
    data["custom"] = models
    _save_data(data)


def hide_builtin(model_id: str) -> None:
    data = _load_data()
    if model_id not in data.setdefault("hidden", []):
        data["hidden"].append(model_id)
    _save_data(data)


def unhide_builtin(model_id: str) -> None:
    data = _load_data()
    data["hidden"] = [i for i in data.get("hidden", []) if i != model_id]
    _save_data(data)


def reset_hidden() -> None:
    data = _load_data()
    data["hidden"] = []
    _save_data(data)


# Per-surface default model — separate from the single system-wide
# OLLAMA_MODEL/effective_ollama_model() fallback, so a GM can e.g. run a
# bigger model for the deliberate "Chat" world-building tool while keeping
# the per-entity "Ask AI" panel on something faster. "image" is a SwarmUI/
# ComfyUI checkpoint name, not an Ollama model — a completely different
# namespace, but stored alongside the other two since all three are
# configured from the same Models tab. "recap" covers session recap/
# condense background jobs (app/audio_jobs.py) — added after the other
# three surfaces already existed, since those jobs previously only fell
# back to the single instance-wide default with no way to pin a different
# model for recap work specifically, unlike every other surface here.
DEFAULT_SURFACES = ("chat", "ask_ai", "image", "recap")


def get_defaults() -> dict:
    d = _load_data().get("defaults", {})
    return {s: d.get(s, "") for s in DEFAULT_SURFACES}


def set_default(surface: str, model_id: str) -> None:
    data = _load_data()
    defaults = data.setdefault("defaults", {})
    defaults[surface] = model_id
    _save_data(data)


# Chat presets — a GM-defined {model, system_extra, options} bundle a
# conversation can switch to on the fly (e.g. "Lorekeeper": low temperature,
# factual; "NPC improv": high temperature, playful) without a trip to
# Settings > System, which is instance-wide. Instance-wide storage like
# everything else in this file (ai_models.json), not per-world — a GM's
# presets are a personal toolkit, not campaign content.
def list_presets() -> list[dict]:
    return _load_data().get("presets", [])


def save_preset(preset: dict) -> None:
    data = _load_data()
    presets = data.setdefault("presets", [])
    label = preset.get("label", "")
    presets[:] = [p for p in presets if p.get("label") != label]
    presets.append(preset)
    _save_data(data)


def delete_preset(label: str) -> None:
    data = _load_data()
    data["presets"] = [p for p in data.get("presets", []) if p.get("label") != label]
    _save_data(data)


def all_models() -> list[dict]:
    hidden = load_hidden_ids()
    custom = load_custom_models()
    seen = {m["id"] for m in KNOWN_MODELS}
    visible_builtins = [m for m in KNOWN_MODELS if m["id"] not in hidden]
    extra = [m for m in custom if m["id"] not in seen and m["id"] not in hidden]
    return visible_builtins + extra


# ── Model resolution ──────────────────────────────────────────────────────────

async def _list_loaded() -> list[str]:
    try:
        resp = await _client().list()
        return [m.model for m in resp.models]
    except Exception:
        return []


async def installed_models_detail() -> list[dict]:
    """Same client.list() call as _list_loaded() above, but keeping the
    size and parameter-count details Ollama's own /api/tags already sends
    — backs the "Detected hardware" recommendation panel (Settings >
    System), which needs a real weight size and parameter count per model
    to size a recommendation and would otherwise need a second round trip
    (or an /api/show call per model) to get them. Returns [] on any
    failure, same as _list_loaded()."""
    try:
        resp = await _client().list()
    except Exception:
        return []
    out = []
    for m in resp.models:
        details = getattr(m, "details", None)
        out.append({
            "model": m.model,
            "size_bytes": int(m.size) if m.size is not None else None,
            "parameter_size": getattr(details, "parameter_size", "") or "",
            "quantization_level": getattr(details, "quantization_level", "") or "",
        })
    return out


# ── Hugging Face model search + pull, and local GGUF import ─────────────────
# Ollama's own /api/pull (see app.routers.ai's existing POST /pull route,
# already wired to the client-side Models tab) understands a model id of the
# form "hf.co/{user}/{repo}:{filename}" natively — pulling a GGUF straight
# from a Hugging Face repo, no separate download step of our own needed. One
# of KNOWN_MODELS above already uses this exact form
# ("hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf"),
# confirmed working — the two functions below just help a GM discover a
# repo/filename to plug into that same tag, they don't reimplement the pull.

_HF_API_BASE = "https://huggingface.co/api"


async def search_huggingface_models(query: str, limit: int = 20) -> list[dict]:
    """Search Hugging Face's public Hub API for GGUF-tagged models (the only
    format Ollama's hf.co pull mechanism understands) — a read-only,
    unauthenticated call to HF's own /api/models. Returns [] on any failure
    (network, malformed response, HF unreachable from this host) rather
    than raising, same as installed_models_detail()/imagegen_models() above
    — a GM without outbound internet from this specific deployment just
    sees an empty result instead of a 500."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            # NOT sending filter="gguf" here (an earlier version did) —
            # that's HF's tag-filter mechanism, and it's not verified
            # whether "gguf" is really a registered tag value there; a
            # filter that matches nothing silently returns zero results
            # rather than erroring, which is indistinguishable from "search
            # is broken" from the GM's side. search/sort/direction/limit
            # match huggingface_hub's own documented list_models() params
            # against this exact endpoint, so those stay. Whether a given
            # result repo actually HAS a .gguf file is checked for real
            # by list_huggingface_gguf_files() once a GM picks one, so
            # this being permissive just means a few non-GGUF repos might
            # show up in results (obvious once expanded — "No .gguf files
            # found in this repo") rather than the search silently
            # dropping real matches.
            r = await c.get(f"{_HF_API_BASE}/models", params={
                "search": query, "sort": "downloads",
                "direction": "-1", "limit": max(1, min(limit, 50)),
            })
            if r.status_code >= 400:
                return []
            data = r.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for m in data:
        if not isinstance(m, dict):
            continue
        repo_id = m.get("id") or m.get("modelId") or ""
        if not repo_id:
            continue
        out.append({
            "id": repo_id,
            "downloads": m.get("downloads") or 0,
            "likes": m.get("likes") or 0,
        })
    return out


async def list_huggingface_gguf_files(repo_id: str) -> list[dict]:
    """The .gguf files actually in `repo_id`, with size — a second call per
    repo (HF's search results above don't include a file listing), used
    once a GM picks a search result so they can see which quantizations
    exist and roughly how big each one is before pulling a possibly
    multi-GB file. Uses HF's tree API (the plain /api/models/{id} endpoint
    doesn't include file sizes). Returns [] on any failure, same reasoning
    as search_huggingface_models above."""
    repo_id = (repo_id or "").strip().strip("/")
    if not repo_id or "/" not in repo_id:
        return []
    try:
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(f"{_HF_API_BASE}/models/{repo_id}/tree/main")
            if r.status_code >= 400:
                return []
            data = r.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        if path.lower().endswith(".gguf"):
            out.append({"filename": path, "size_bytes": entry.get("size")})
    return out


async def import_local_gguf_model(path: Path, model_name: str) -> AsyncGenerator[dict, None]:
    """Push a GGUF file already on local disk (an upload just reassembled
    by app.uploads' chunked-upload pair — see app.routers.ai's /ollama/
    upload/complete) into Ollama as a new named model.

    Ollama's own API for this is two calls (verified against the installed
    ollama==0.6.2 client's actual source, not guessed — its AsyncClient.
    create_blob reads `path` from local disk and streams it in 32KB chunks
    over HTTP to POST /api/blobs/{sha256-digest}, then .create(model=...,
    files={filename: digest}) posts to /api/create referencing that
    digest): create_blob() only needs `path` readable by wherever THIS code
    runs (nd-world's own container) — the file does NOT need to live on a
    volume shared with the "ollama" Compose service the way SWARMUI_MODELS_DIR/
    WHISPER_MODELS_DIR do, since the blob is pushed over the network, not
    read off a shared disk.

    Yields the same {"total":,"completed":}/{"status":"done",...}/
    {"error":} shape download_swarmui_model already uses (so the client-side
    JS can reuse identical progress-bar parsing), except create_blob has no
    byte-level progress callback of its own — the "pushing to Ollama" phase
    is reported as a single indeterminate step rather than granular bytes,
    since by this point the file is already fully on local disk and the
    only remaining unknown-duration work is the blob upload + registration."""
    model_name = (model_name or "").strip()
    if not model_name:
        yield {"error": "No model name given"}
        return
    if not path.is_file():
        yield {"error": "Uploaded file is missing"}
        return
    try:
        client = _client()
        yield {"status": "uploading", "detail": "Pushing file to Ollama…"}
        digest = await client.create_blob(str(path))
        yield {"status": "creating", "detail": "Registering model…"}
        await client.create(model=model_name, files={path.name: digest})
        yield {"status": "done", "model": model_name}
    except _ollama.ResponseError as exc:
        yield {"error": f"Ollama {exc.status_code}: {exc.error}"}
    except Exception as exc:
        _log.warning("import_local_gguf_model failed: %s: %s", type(exc).__name__, exc)
        yield {"error": f"{type(exc).__name__}: {exc}"}


async def resolve_model(requested: str) -> tuple[str, str | None]:
    """Resolve a possibly-short model id (e.g. "llama3") against what's
    actually available (e.g. "llama3:latest"). Returns (model, note) —
    `note` is a short human-readable string set only when the resolved
    model differs from what was requested, so callers can surface the
    substitution instead of it happening invisibly. When nothing available
    even loosely matches, the request is returned UNCHANGED rather than
    falling back to an arbitrary unrelated model — the caller's own Ollama
    call then fails with a clear "model not found" error instead of
    silently answering from the wrong model."""
    target = requested or effective_ollama_model()
    available = await _list_loaded()
    if not available or target in available:
        return target, None
    tl = target.lower()
    for a in available:
        al = a.lower()
        if tl == al or tl in al or al in tl:
            _log.info("resolve_model %r → %r", target, a)
            return a, f"Using {a} (closest match to requested \"{target}\")"
    _log.warning("resolve_model no match for %r among %d available", target, len(available))
    return target, None


_BENCHMARK_PROMPT = "Write a two-sentence description of a rainy city street at night."


async def benchmark_model(model: str) -> dict:
    """Run a short fixed prompt against `model` (non-streamed) and report
    Ollama's own generation timing/token-count metadata (real server-side
    throughput) instead of the chat UI's existing client-side "tokens seen
    over wall-clock SSE time" estimate, which bakes in network/rendering
    overhead. Raises ValueError on any failure, same pattern as
    generate_session_prep/parse_facts_from_recap."""
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[{"role": "user", "content": _BENCHMARK_PROMPT}],
            # Fixed, small cap (overriding whatever num_predict a GM has
            # configured instance-wide) — without it a chatty model can
            # generate paragraphs for what's meant to be a two-sentence
            # timing probe, wasting tokens and making the eval_count-based
            # tokens_per_sec comparison across models less apples-to-apples.
            **(await _chat_kwargs({"num_predict": 128}, model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc

    eval_count = getattr(resp, "eval_count", 0) or 0
    eval_duration = getattr(resp, "eval_duration", 0) or 0
    prompt_eval_count = getattr(resp, "prompt_eval_count", 0) or 0
    prompt_eval_duration = getattr(resp, "prompt_eval_duration", 0) or 0
    load_duration = getattr(resp, "load_duration", 0) or 0
    total_duration = getattr(resp, "total_duration", 0) or 0
    tps = (eval_count / (eval_duration / 1e9)) if eval_duration else 0.0
    prompt_tps = (prompt_eval_count / (prompt_eval_duration / 1e9)) if prompt_eval_duration else 0.0
    return {
        "model": m,
        "tokens_per_sec": round(tps, 1),
        "prompt_tokens_per_sec": round(prompt_tps, 1),
        "eval_count": eval_count,
        "eval_duration_ms": round(eval_duration / 1e6, 1),
        "prompt_eval_count": prompt_eval_count,
        "prompt_eval_duration_ms": round(prompt_eval_duration / 1e6, 1),
        "load_duration_ms": round(load_duration / 1e6, 1),
        "total_duration_ms": round(total_duration / 1e6, 1),
    }


async def resident_models() -> list[dict]:
    """What's actually occupying memory right now — distinct from _list_loaded
    (client.list()/`/api/tags`, which is every model downloaded to disk,
    regardless of whether it's in memory). Backs the Models tab's "Resident
    in VRAM" section, since a 16GB card can't hold an LLM and a diffusion
    model at once and a GM needs to see what's actually using it.

    A model doesn't have to fit in VRAM entirely — Ollama offloads whatever
    doesn't fit to system RAM (running slower, but still working), so
    size_ram_bytes (size minus size_vram) is how much of THIS model is
    sitting in system RAM rather than on the GPU. unload_model() below frees
    both at once — Ollama has no notion of evicting only the RAM-resident
    part of a model that's split across both."""
    try:
        resp = await _client().ps()
    except Exception:
        return []
    result = []
    for m in resp.models:
        size = int(m.size) if m.size is not None else None
        size_vram = int(m.size_vram) if m.size_vram is not None else None
        result.append({
            "model": m.model,
            "size_bytes": size,
            "size_vram_bytes": size_vram,
            "size_ram_bytes": max(0, size - size_vram) if size is not None and size_vram is not None else None,
            "expires_at": m.expires_at.isoformat() if m.expires_at else None,
        })
    return result


async def unload_model(model_id: str) -> bool:
    """Evict a model from VRAM immediately — Ollama's documented idiom for
    this is a generate call with an empty prompt and keep_alive=0 (rather
    than waiting out its normal keep-alive timer). Returns False (not an
    exception) on failure so the caller can show a plain error instead of a
    500 — this is a manual "free up my GPU" action, not something that
    should ever look like a crash."""
    try:
        await _client().generate(model=model_id, keep_alive=0)
        return True
    except Exception as exc:
        _log.warning("unload_model(%r) failed: %s", model_id, exc)
        return False


# ── Chat functions ────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a creative fantasy world-building assistant. "
    "Write vivid, immersive lore. Be concise but evocative. "
    "Keep it under 200 words."
)


def _empty_response_message(model: str, thinking_chars: int, done_reason: str | None) -> str:
    """The exact wording generate_chat's empty-content branch has always
    used, factored out so stream_chat's own empty-stream diagnostic (below)
    can share it verbatim rather than risking the two texts drifting apart
    — is_thinking_starved_sentinel and existing tests both pin this exact
    phrasing (particularly the literal `hidden "thinking"` substring), so
    any caller of this helper automatically stays compatible with both."""
    if thinking_chars:
        return (
            f"[empty response from {model} — it produced {thinking_chars} character(s) of hidden "
            "\"thinking\" output but no final answer (usually means it ran out of output "
            "budget mid-reasoning). Try a shorter prompt, a higher response-length limit, "
            "or a non-reasoning model.]"
        )
    detail = f"done_reason={done_reason}" if done_reason else "no done_reason reported"
    return f"[empty response from {model} ({detail}) — try a different model, or check the Ollama server logs]"


async def generate_chat(messages: list[dict], system: str = "", model: str = "", options: dict = None, think: bool = False) -> str:
    m = model or effective_ollama_model()
    _log.info("generate_chat model=%s msgs=%d", m, len(messages))
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        resp = await _client().chat(model=m, messages=full, **(await _chat_kwargs(options, think, m)))
        content = resp.message.content
        if content:
            return content
        # A successful call with genuinely empty content — not a request/connection
        # error, so it doesn't hit the except branches below. _chat_kwargs() already
        # sends think=False so a "thinking"/reasoning model shouldn't produce hidden
        # reasoning at all, but not every model honors that — if one still burns its
        # whole output budget on reasoning tokens before writing visible text, that
        # shows up here as empty `content` with `thinking` full of text (and usually
        # done_reason=="length"). Surface whichever of those Ollama gave us rather
        # than a bare "[empty response]" with no way to act on it.
        thinking = getattr(resp.message, "thinking", None)
        done_reason = getattr(resp, "done_reason", None)
        eval_count = getattr(resp, "eval_count", None)
        # thinking_chars (not just had_thinking's bool) is what a GM/admin
        # actually needs to calibrate _THINKING_HEADROOM_TOKENS from logs
        # across repeated failures — see that constant's own comment.
        _log.warning(
            "generate_chat model=%s returned empty content (done_reason=%r, eval_count=%r, "
            "had_thinking=%r, thinking_chars=%d)",
            m, done_reason, eval_count, bool(thinking), len(thinking or ""),
        )
        return _empty_response_message(m, len(thinking or ""), done_reason)
    except _ollama.ResponseError as exc:
        _log.error("generate_chat Ollama error: %s %s", exc.status_code, exc.error)
        return f"[AI error: Ollama {exc.status_code}: {exc.error}]"
    except Exception as exc:
        _log.error("generate_chat unavailable: %s: %s", type(exc).__name__, exc)
        return f"[AI unavailable: {type(exc).__name__}: {exc}]"


def is_failure_sentinel(result: str) -> bool:
    """True if `result` is one of generate_chat's two failure-sentinel
    families rather than real model output: "[AI error: ...]"/"[AI
    unavailable: ...]" (a request/connection failure) or "[empty response
    ...]" (a successful call that produced no usable content). Callers
    that chain multiple generate_chat calls together (summarize_transcript
    below; audio_jobs.py's job engine) must check both — checking only the
    first family let a genuine failure get woven into a recap as if it
    were prose, with the job still marked "done"."""
    return result.startswith("[AI ") or result.startswith("[empty response")


def is_thinking_starved_sentinel(result: str) -> bool:
    """True only for the specific empty-response sentinel generate_chat
    returns when a thinking-enabled model burned its whole output budget
    on hidden reasoning and never wrote a visible answer (the "hidden
    \"thinking\" output but no final answer" branch above) — narrower than
    is_failure_sentinel, which also matches every OTHER failure (a
    connection error, a plain "no done_reason reported" empty response,
    and summarize_transcript's own whitespace-only-part sentinel, none of
    which start with "[empty response" AND contain this exact phrase).
    Kept here rather than duplicated in app.audio_jobs/the UI so the job
    engine's auto-retry (see _run_job) and the Background Jobs page's
    one-click "Retry without Thinking" button both key off the identical
    check the sentinel text itself defines."""
    return result.startswith("[empty response") and 'hidden "thinking"' in result


async def stream_chat(
    messages: list[dict], system: str = "", model: str = "", options: dict = None, think: bool = False,
) -> AsyncGenerator[str, None]:
    """`think` defaults to False, same as generate_chat's own plain
    default — most interactive surfaces (AI Chat's World Chat/Image tabs,
    the Chronicler, "Talk to this NPC") have no Thinking toggle at all and
    never pass it. The entity detail page's "Ask AI" panel is the one
    caller that does (see app.routers.ai's ChatBody.think and epSend's own
    Thinking checkbox), letting a GM opt into slower/deeper reasoning for
    that one surface per-request."""
    m = model or effective_ollama_model()
    _log.info("stream_chat model=%s msgs=%d think=%r", m, len(messages), think)
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    yielded_any = False
    thinking_chars = 0
    done_reason = None
    try:
        chat_kwargs = await _chat_kwargs(options, think, m)
        async for chunk in await _client().chat(model=m, messages=full, stream=True, **chat_kwargs):
            token = chunk.message.content
            if token:
                yielded_any = True
                yield token
            # Tracked regardless of whether `think` was requested — even
            # with think=False a model can still ignore that and burn its
            # budget on hidden reasoning (the case generate_chat's own
            # diagnostic exists for); with think=True this is simply the
            # expected/intended reasoning trace, tracked the same way so
            # the same starvation diagnostic below still fires if even a
            # deliberately-thinking model runs out of room before writing
            # a visible answer.
            thinking_chars += len(getattr(chunk.message, "thinking", None) or "")
            done_reason = getattr(chunk, "done_reason", None) or done_reason
        if not yielded_any:
            # Same empty-response case generate_chat handles (see its own
            # comment) — whether from a deliberate think=True request or a
            # model that ignores think=False, hidden reasoning can burn the
            # whole output budget here too; on the streaming path that used
            # to come back as a completely silent reply with nothing for
            # the caller to show at all, instead of generate_chat's own
            # explanatory sentinel.
            _log.warning(
                "stream_chat model=%s yielded no content (done_reason=%r, thinking_chars=%d)",
                m, done_reason, thinking_chars,
            )
            yield _empty_response_message(m, thinking_chars, done_reason)
    except _ollama.ResponseError as exc:
        _log.error("stream_chat Ollama error: %s %s", exc.status_code, exc.error)
        yield f"[AI error: Ollama {exc.status_code}: {exc.error}]"
    except Exception as exc:
        _log.error("stream_chat unavailable: %s: %s", type(exc).__name__, exc)
        yield f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def generate(prompt: str, system: str = _SYSTEM) -> str:
    return await generate_chat([{"role": "user", "content": prompt}], system)


_RECAP_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. The GM will give you an informal, "
    "terse recap of what happened in a session (e.g. \"went to the tavern, met Elyra, "
    "she's actually working for the cult, found a strange clock\"). Turn it into a list "
    "of discrete, well-written facts about what happened.\n\n"
    "For each fact, set \"visible_to_players\" to indicate whether the player characters "
    "(not just the GM) know it:\n"
    "- true: the party witnessed it, was told it in-fiction, or it's public knowledge\n"
    "- false: it's a GM-only secret (a villain's true identity or plan, hidden dice rolls, "
    "monster stats, anything the players have not yet discovered)\n\n"
    "Default to visible_to_players: true unless the recap clearly marks something as secret "
    "or the players wouldn't plausibly know it yet. Split compound sentences into separate "
    "facts where it makes sense. Write each fact as a complete sentence in past tense. Do not "
    "invent details that aren't implied by the recap. Respond with JSON only."
)

_RECAP_FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "visible_to_players": {"type": "boolean"},
                },
                "required": ["content", "visible_to_players"],
            },
        },
    },
    "required": ["facts"],
}


async def parse_facts_from_recap(raw_text: str, model: str = "") -> list[dict]:
    """Turn a rough GM recap into draft facts via the local model, using
    Ollama's JSON-schema-constrained `format` — see ollama.AsyncClient.chat's
    `format` parameter. Raises ValueError on any failure (model unreachable,
    malformed JSON) so the caller can surface a clear error; does not write
    anything to the database itself."""
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _RECAP_SYSTEM},
                {"role": "user", "content": raw_text},
            ],
            format=_RECAP_FACTS_SCHEMA,
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        facts = parsed["facts"]
        if not isinstance(facts, list):
            raise ValueError
        return [
            {"content": str(f["content"]), "visible_to_players": bool(f["visible_to_players"])}
            for f in facts
        ]
    except Exception as exc:
        raise ValueError("Could not parse facts from that recap — try rephrasing it.") from exc


_ENTITY_FROM_TEXT_SYSTEM = (
    "You turn a passage of text from a tabletop RPG GM's AI chat conversation into a "
    "single structured world-building entity — whichever kind the text is actually "
    "describing (a character/NPC, location, organization, creature, event, item, feat, "
    "race, or profession). Extract only what's stated or clearly implied by the text; "
    "do not invent unrelated details. \"body\" should be the entity's full write-up in "
    "Markdown (history, description, stats — whatever's relevant); \"summary\" is a "
    "single-sentence one-liner. Respond with JSON only."
)


def _entity_from_text_schema(kinds: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(kinds)},
            "subtype": {"type": "string"},
            "name": {"type": "string"},
            "summary": {"type": "string"},
            "body": {"type": "string"},
            "tags": {"type": "string"},
            "folder": {"type": "string"},
            "visible_to_players": {"type": "boolean"},
        },
        "required": ["kind", "name"],
    }


async def parse_entity_from_text(raw_text: str, kinds: list[str], model: str = "") -> dict:
    """Turn a passage of text (typically an AI Chat reply) into a draft world
    entity — same JSON-schema-constrained pattern as parse_facts_from_recap.
    Raises ValueError on any failure so the caller can surface a clear error;
    does not write anything to the database itself (see main.py's
    /api/import/execute, which already knows how to write this exact shape)."""
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _ENTITY_FROM_TEXT_SYSTEM},
                {"role": "user", "content": raw_text},
            ],
            format=_entity_from_text_schema(kinds),
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        if not isinstance(parsed, dict) or parsed.get("kind") not in kinds or not str(parsed.get("name") or "").strip():
            raise ValueError
        return {
            "kind": parsed["kind"],
            "subtype": str(parsed.get("subtype") or "").strip(),
            "name": str(parsed["name"]).strip(),
            "summary": str(parsed.get("summary") or "").strip(),
            "body": str(parsed.get("body") or "").strip(),
            "tags": str(parsed.get("tags") or "").strip(),
            "folder": str(parsed.get("folder") or "").strip(),
            "visible_to_players": bool(parsed.get("visible_to_players", True)),
        }
    except Exception as exc:
        raise ValueError("Could not turn that reply into an entity — try rephrasing or picking a shorter passage.") from exc


_SESSION_PREP_SYSTEM = (
    "You are a scribe helping a tabletop RPG GM prepare for their next session. Given a summary "
    "of what happened recently (facts and/or a recap), any open quests, and the party's makeup, "
    "produce a short prep checklist: likely player moves, possible complications, ideas for an "
    "opening scene, and reminders about important NPCs. Each item must be a single, concrete, "
    "actionable checklist entry a GM can glance at before the table starts — not a full paragraph. "
    "Don't invent plot points that aren't implied by the given context. Respond with JSON only."
)

_SESSION_PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tasks"],
}


async def generate_session_prep(context_text: str, model: str = "") -> list[str]:
    """Draft a session-prep checklist from a text summary of world state
    (recent facts/recap, open quests, the party) — same JSON-schema-
    constrained pattern as parse_facts_from_recap. Raises ValueError on any
    failure; does not write anything to the database itself (the caller
    appends confirmed items via the existing prep/add route, one at a
    time — no new write path needed)."""
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _SESSION_PREP_SYSTEM},
                {"role": "user", "content": context_text},
            ],
            format=_SESSION_PREP_SCHEMA,
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        tasks = parsed["tasks"]
        if not isinstance(tasks, list):
            raise ValueError
        return [str(t).strip() for t in tasks if str(t).strip()]
    except Exception as exc:
        raise ValueError("Could not generate a prep checklist — try again.") from exc


_EXPAND_NOTES_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. The GM will give you rough, terse session "
    "notes (e.g. \"went to the tavern, met Elyra, found a clock, fought goblins\"). Expand them "
    "into a well-written, readable session recap in flowing prose — a few short paragraphs, "
    "past tense, third person. Preserve every detail from the notes; don't invent new plot "
    "points, names, or outcomes that aren't implied. Markdown is fine for light formatting "
    "(e.g. **bold** for names) but keep it simple — this is a narrative recap, not a bulleted "
    "list. Respond with the recap text only, no preamble or commentary."
)


async def expand_recap_notes(notes: str, model: str = "", think: bool = True, extra_instructions: str = "") -> str:
    """Expand terse GM notes into a polished narrative session recap. Unlike
    parse_facts_from_recap, this doesn't need JSON-schema-constrained output
    (free-text prose, not discrete structured facts) so it just wraps
    generate_chat directly — same "[AI error: ...]"/"[AI unavailable: ...]"
    inline-string failure convention as every other chat call in this module,
    which the caller can display as-is instead of catching an exception.

    think defaults to True (unlike generate_chat's own plain default of
    False) — this is one of the session-recap-assist functions a GM's
    "Thinking" checkbox on the Sessions page controls; the checkbox is
    checked by default, so the common case is this default applying
    unchanged. See _thinking_num_predict_override's own docstring for why
    a GM-configured num_predict cap is widened when think=True.

    `extra_instructions` is a GM's steering (e.g. World.recap_instructions
    — "write in Spanish"), same _with_instructions convention every other
    recap function in this module uses — added since this was previously
    the one member of the recap-assist family that ignored it, so a GM's
    standing instruction silently didn't apply to Expand notes.

    Also sizes num_ctx via _ctx_override_if_needed when a huge notes paste
    (unbounded free text) would otherwise silently truncate at the
    configured/default context — this used to pass no num_ctx override at
    all, unlike condense_recap/summarize_transcript. The extra reserve only
    applies when num_predict was ACTUALLY widened above (i.e. a configured
    num_predict exists to widen) — gating on bare `think` instead would
    make this fire on nearly every thinking call regardless of input
    length, since the headroom alone already exceeds the unconfigured
    assumed default context."""
    system = _with_instructions(_EXPAND_NOTES_SYSTEM, extra_instructions)
    opts = dict(_thinking_num_predict_override(think))
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if "num_predict" in opts else 0)
    opts.update(_ctx_override_if_needed(system + notes, reserve))
    return await generate_chat(
        [{"role": "user", "content": notes}], system=system, model=model,
        options=opts or None, think=think,
    )


_SUMMARIZE_FACTS_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is a list of discrete facts logged "
    "for one session. Weave them into a short, readable narrative recap in flowing prose — a "
    "few short paragraphs, past tense, third person. Use only the facts given; don't invent "
    "new details, and don't drop any of them. Markdown is fine for light formatting but keep it "
    "simple. Respond with the recap text only, no preamble or commentary."
)


async def summarize_session_from_facts(facts: list[str], model: str = "", extra_instructions: str = "", think: bool = True) -> str:
    """Weave a list of discrete session facts (see the Facts feature, which
    logs these per-session) into a readable narrative recap. `extra_instructions`
    is a GM's steering (e.g. World.recap_instructions — "write in Spanish"),
    same as summarize_transcript's own parameter of the same name. `think`
    defaults to True — see expand_recap_notes's docstring for why this
    family of functions differs from generate_chat's own plain default.

    Also sizes num_ctx via _ctx_override_if_needed — a fact-heavy session
    (the player session-log route sends every fact, uncapped) could
    otherwise silently truncate at the configured/default context; this
    used to pass no num_ctx override at all, unlike condense_recap/
    summarize_transcript. See expand_recap_notes's own docstring for why
    the extra reserve is gated on num_predict actually having been widened,
    not on bare `think`."""
    if not facts:
        return ""
    bullet_list = "\n".join(f"- {f}" for f in facts)
    system = _with_instructions(_SUMMARIZE_FACTS_SYSTEM, extra_instructions)
    opts = dict(_thinking_num_predict_override(think))
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if "num_predict" in opts else 0)
    opts.update(_ctx_override_if_needed(system + bullet_list, reserve))
    return await generate_chat(
        [{"role": "user", "content": bullet_list}], system=system, model=model,
        options=opts or None, think=think,
    )


_COMPACT_CHAT_SYSTEM = (
    "You are compacting an ongoing AI Chat conversation between a GM and their world-building "
    "assistant for a tabletop RPG. Summarize the conversation below into a concise recap of what "
    "was discussed, decided, or created — preserve concrete facts, names, numbers, and decisions "
    "the GM will still need, and drop exploratory back-and-forth, false starts, and small talk. "
    "Write in flowing prose, third person. Respond with the summary only, no preamble or "
    "commentary."
)


def _chat_history_to_text(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = "GM" if m.get("role") == "user" else "Assistant"
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


async def condense_chat_history(messages: list[dict], model: str = "", think: bool = True,
                                 extra_instructions: str = "") -> str:
    """Compact the older turns of an AI Chat conversation into one summary
    message — the same idea as this very CLI's own auto-compaction, applied
    to app/routers/ai.py's chat surface. History there is unbounded by
    design (trimming it loses the GM's own memory of the conversation — see
    docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md item 4.2's own reasoning for
    why the context-usage indicator exists instead of a hard trim), so this
    gives an explicit, on-demand way to shrink it back down once that
    indicator flags it as large.

    `messages` is the {role, content} list app.routers.ai.ChatMessage
    already produces (see _build_ollama_messages) — the CALLER (ai-chat-
    core.js's compactChat()) decides which turns count as "older" and
    passes only those; a handful of the most recent turns stay verbatim in
    `history` alongside this summary rather than being sent here at all, so
    the freshest context is never lossy-compressed away.

    `think` defaults to True, matching every other recap-family function in
    this module (condense_recap/expand_recap_notes/summarize_session_from_
    facts) — a compact summary benefits from the same reasoning budget a
    recap condense gets. `extra_instructions` follows the same
    _with_instructions convention as those siblings, though the chat route
    doesn't currently thread a GM's standing recap instructions through —
    it's a plain keyword-only extension point for now.

    Sizes num_ctx via _ctx_override_if_needed like summarize_session_from_
    facts does, rather than refusing oversized input outright the way
    condense_recap's single-call entry point does (item 3.3) — a long-
    running chat can outgrow even MAX_AUTO_NUM_CTX, and the caller already
    controls how much text lands here by choosing where the "older" cutoff
    falls, unlike a GM free-pasting an arbitrarily large recap."""
    text = _chat_history_to_text(messages)
    if not text:
        return ""
    system = _with_instructions(_COMPACT_CHAT_SYSTEM, extra_instructions)
    opts = dict(_thinking_num_predict_override(think))
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if "num_predict" in opts else 0)
    opts.update(_ctx_override_if_needed(system + text, reserve))
    return await generate_chat(
        [{"role": "user", "content": text}], system=system, model=model,
        options=opts or None, think=think,
    )


_CONDENSE_RECAP_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Condense the following session recap into a "
    "short, tight summary — a few sentences at most, hitting only the key beats a player would "
    "need to remember before the next session. Keep it in flowing prose. Don't invent details "
    "that aren't in the original. Respond with the condensed recap only, no preamble or "
    "commentary."
)


async def condense_recap(
    recap: str, model: str = "", options: dict = None, think: bool = True,
    extra_instructions: str = "", min_tokens: int | None = None, max_tokens: int | None = None,
    world_context: str = "", expanded_thinking: bool = False,
) -> str:
    """Condense an existing recap into a tighter 'previously on...' summary.
    `options` (see generate_chat) is an optional per-call override — the
    caller passes context_sized_options(recap) to force num_ctx to
    comfortably fit the whole pasted recap for this one call only, without
    touching app.ai's instance-wide default the next call falls back to.
    `think` defaults to True — see expand_recap_notes's docstring for why
    this family of functions differs from generate_chat's own plain
    default.

    `extra_instructions` is a GM's steering appended to the system prompt,
    same _with_instructions convention every other recap function here
    uses (e.g. "focus on combat", "write in French").

    `min_tokens`/`max_tokens` are soft length targets for the CONDENSED
    OUTPUT, described to the model in the system prompt using the same
    coarse chars-per-token estimate the rest of this module relies on
    (_chars_per_token_estimate) — Ollama has no native minimum-output-
    length option, so min_tokens is prompt guidance only, honored on a
    best-effort basis like any other free-text instruction. max_tokens
    ALSO sets options["num_predict"], a real hard cap on the Ollama side
    (not just guidance) — a GM wanting a very short condensed version
    still gets one even if the model would otherwise keep writing.

    `world_context` is optional extra world info (e.g. party roster,
    setting one-liner) prepended to the recap before sending — same
    pattern the session-log summarize route already uses, forwarded here
    so a GM's condensed recap can reference "the party" without the model
    having to guess who that is.

    `expanded_thinking` enables Ollama's extended reasoning budget for
    this call (think=True already enables reasoning tokens; expanded_thinking
    additionally sets options["thinking"] = {"type": "enabled",
    "budget_tokens": <large>} to lift the default per-call cap the model
    would otherwise apply). Only meaningful when think=True and the model
    actually supports thinking — ignored otherwise."""
    system = _with_instructions(_CONDENSE_RECAP_SYSTEM, extra_instructions)
    if world_context:
        system = f"{system}\n\nWorld context:\n{world_context}"
    merged = dict(options or {})
    pred_override = _thinking_num_predict_override(think)
    for k, v in pred_override.items():
        merged.setdefault(k, v)
    if max_tokens is not None:
        merged["num_predict"] = max_tokens
    if expanded_thinking and think:
        merged.setdefault("thinking", {"type": "enabled", "budget_tokens": 8192})
    length_instruction = _length_instruction(min_tokens, max_tokens)
    if length_instruction:
        system = f"{system}\n\n{length_instruction}"
    return await generate_chat(
        [{"role": "user", "content": recap}], system=system, model=model,
        options=merged or None, think=think,
    )


# ── Transcript → recap pipeline ───────────────────────────────────────────────

# How many transcript CHARACTERS to feed per summarize chunk — a deliberate
# chars (not tokens) budget so the logic doesn't need a tokenizer. At ~4
# chars/token this is ~1 500 tokens of transcript, leaving generous room
# for the system prompt + output inside a typical 4 096-token context. The
# final merge call gets all the per-chunk summaries concatenated, which is
# much shorter than the original transcript. Env-tunable in case a GM's
# model has a larger context and they want fewer round trips.
_CHUNK_SIZE = int(os.getenv("TRANSCRIPT_CHUNK_SIZE", str(6_000)))

# How many output tokens to reserve for each chunk's summary response —
# the Ollama num_predict cap for per-chunk calls only. 512 tokens (~2 048
# chars) per chunk is enough for a paragraph-level summary without
# truncating mid-sentence on anything except pathologically long chunks,
# and it keeps the per-call wall time predictable. The final merge call
# doesn't set num_predict at all — it gets the instance-wide default,
# which is typically larger and appropriate for a full session summary.
_CHUNK_SUMMARY_MAX_TOKENS = int(os.getenv("TRANSCRIPT_CHUNK_SUMMARY_MAX_TOKENS", "512"))

_CHUNK_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is part of a transcript from a "
    "session recording. Summarize what happened in this segment in a few sentences — the "
    "key events, decisions, and NPC interactions. Keep it terse; it will be merged with "
    "summaries of adjacent segments. Respond with the summary only, no preamble."
)

_MERGE_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below are short summaries of consecutive "
    "segments of a session recording. Weave them into a single, cohesive session recap in "
    "flowing prose — a few paragraphs, past tense, third person. Preserve all key events, "
    "decisions, and NPC interactions. Don't invent details. Respond with the recap only, "
    "no preamble."
)


def _with_instructions(system: str, extra: str) -> str:
    """Append `extra` to `system` when non-blank — a one-liner used by every
    recap function that forwards a GM's standing recap_instructions (or a
    per-call steering string) to the model. The separator is a blank line
    so the extra block reads as a separate paragraph rather than run-on
    prose at the end of the fixed system prompt."""
    extra = (extra or "").strip()
    return f"{system}\n\n{extra}" if extra else system


# Coarse chars-per-token estimate used throughout this module for sizing
# num_ctx overrides — not a real tokenizer, just a conservative approximation
# (English prose is ~4 chars/token; we use 3.5 to bias slightly toward
# over-allocating context rather than silently truncating). Never used for
# billing or exact measurements, only for "is this input likely to fit in
# the configured context window" heuristics.
_CHARS_PER_TOKEN = 3.5


def _chars_per_token_estimate(text: str) -> int:
    """Coarse token count for `text` using the module-level estimate."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


# Tokens reserved for the model's own output when sizing num_ctx — added on
# top of the estimated input token count so the context window comfortably
# fits both prompt AND response without truncating either. 1 024 is generous
# for a short summary but still well within any modern model's context, and
# erring toward over-allocation is cheap (a slightly larger context window)
# while under-allocation silently drops the end of the input.
_CONTEXT_FIT_RESERVED_TOKENS = 1_024

# Extra tokens reserved for thinking/reasoning output when think=True — added
# to _CONTEXT_FIT_RESERVED_TOKENS so _ctx_override_if_needed sizes the window
# wide enough for both visible output AND the model's hidden reasoning trace.
# QwQ-32B at default settings uses ~4 000–8 000 reasoning tokens on a
# moderately complex prompt; 8 192 is a conservative upper bound that still
# leaves headroom for the visible answer. The value is intentionally generous
# — a context window that's 8 K tokens too wide costs almost nothing (a few
# extra MB of KV cache), while one that's too narrow silently truncates the
# reasoning and can produce empty or garbled output (the starvation case
# generate_chat's diagnostic was added for).
_THINKING_HEADROOM_TOKENS = 8_192

# Automatic num_ctx sizing caps — the upper bound prevents pathologically
# large context requests (e.g. a 200K-token transcript paste) from OOM-ing
# Ollama, while the lower bound ensures the override is always at least as
# large as a sane default. Only applied when _ctx_override_if_needed
# actually fires (i.e. the estimated token count would exceed the current
# effective num_ctx), so normal-sized inputs never touch these.
_MIN_AUTO_NUM_CTX = 4_096
_MAX_AUTO_NUM_CTX = 32_768


def _ctx_override_if_needed(text: str, reserved: int = _CONTEXT_FIT_RESERVED_TOKENS) -> dict:
    """Return {"num_ctx": N} if the estimated token count of `text` plus
    `reserved` output tokens would exceed the currently configured num_ctx
    — giving this call a larger context window without touching the
    instance-wide default that every other call falls back to. Returns {}
    when the current effective num_ctx is already large enough, so callers
    can unconditionally merge the result into their options dict and only
    pay for the override when it's actually needed.

    Capped at _MAX_AUTO_NUM_CTX to prevent an arbitrarily large input from
    requesting a context size that OOMs the Ollama backend — a transcript
    that truly exceeds 32 K tokens should go through the chunked
    summarize_transcript pipeline instead of a single generate_chat call."""
    needed = _chars_per_token_estimate(text) + reserved
    current = effective_ollama_options().get("num_ctx") or 0
    if current and needed <= current:
        return {}
    clamped = max(_MIN_AUTO_NUM_CTX, min(needed, _MAX_AUTO_NUM_CTX))
    if current and clamped <= current:
        return {}
    return {"num_ctx": clamped}


def _thinking_num_predict_override(think: bool) -> dict:
    """When think=True, widen a GM-configured num_predict cap by
    _THINKING_HEADROOM_TOKENS so the model has room for both its hidden
    reasoning trace AND its visible answer — without this, a tight cap
    (e.g. 512 tokens for a short recap) eats the whole budget on reasoning
    and returns empty content (the starvation case). Returns {} when
    think=False or when no num_predict is configured instance-wide (the
    model's own default applies in that case, which is already sized for
    thinking by its Modelfile)."""
    if not think:
        return {}
    cap = effective_ollama_options().get("num_predict")
    if not cap:
        return {}
    return {"num_predict": int(cap) + _THINKING_HEADROOM_TOKENS}


def _length_instruction(min_tokens: int | None, max_tokens: int | None) -> str:
    """A natural-language length target appended to the system prompt when
    the caller supplies min_tokens or max_tokens — Ollama has no native
    minimum-output-length option, so min is prompt guidance only. max also
    sets options["num_predict"] (done by the caller), so both the guidance
    AND the hard cap apply for max. Returns "" when neither is set."""
    if min_tokens is None and max_tokens is None:
        return ""
    min_chars = int(min_tokens * _CHARS_PER_TOKEN) if min_tokens is not None else None
    max_chars = int(max_tokens * _CHARS_PER_TOKEN) if max_tokens is not None else None
    if min_chars is not None and max_chars is not None:
        return (
            f"Aim for roughly {min_chars}–{max_chars} characters "
            f"({min_tokens}–{max_tokens} tokens) in your response."
        )
    if min_chars is not None:
        return f"Write at least {min_chars} characters ({min_tokens} tokens) in your response."
    return f"Keep your response under {max_chars} characters ({max_tokens} tokens)."


async def transcribe_audio(audio_path: Path) -> str:
    """Send `audio_path` to the optional whisper.cpp server for
    transcription — returns the transcript text, or "" on any failure
    (server not configured, file unreadable, timeout, HTTP error). The
    caller (summarize_transcript below, and app/audio_jobs.py's
    transcribe_audio wrapper) treats "" as "Whisper isn't configured or
    failed" and handles it accordingly rather than raising here.

    Whisper's /inference endpoint expects multipart/form-data with the
    audio file as "file" — confirmed against the whisper.cpp server's own
    README. The timeout is WHISPER_TIMEOUT_SECONDS (default 8 hours) not
    httpx's default 5 seconds, since CPU-only whisper.cpp can run well
    under realtime speed on large files."""
    url = effective_whisper_url()
    if not url:
        return ""
    try:
        async with _httpx.AsyncClient(timeout=WHISPER_TIMEOUT_SECONDS) as c:
            with audio_path.open("rb") as f:
                r = await c.post(
                    f"{url}/inference",
                    files={"file": (audio_path.name, f, "audio/mpeg")},
                    data={"response_format": "text"},
                )
            if r.status_code >= 400:
                _log.warning("transcribe_audio HTTP %d: %s", r.status_code, r.text[:200])
                return ""
            return r.text.strip()
    except Exception as exc:
        _log.warning("transcribe_audio failed: %s: %s", type(exc).__name__, exc)
        return ""


async def summarize_transcript(
    transcript: str,
    model: str = "",
    extra_instructions: str = "",
    think: bool = True,
    job_interrupted: asyncio.Event | None = None,
) -> str:
    """Chunk a long transcript and summarize it into a session recap.

    Splits `transcript` into _CHUNK_SIZE-character pieces, summarizes each
    separately (so even a multi-hour session fits in a small context
    window), then merges the per-chunk summaries into a single cohesive
    recap. The final merge call gets all chunk summaries concatenated,
    which is much shorter than the original.

    `think` defaults to True — see expand_recap_notes's docstring.

    `job_interrupted` is an asyncio.Event set by app/job_shutdown.py when
    the job is cancelled (e.g. the GM navigates away or the server is
    shutting down). Checked between chunks and before the merge call so a
    cancellation is acted on promptly rather than waiting for the current
    chunk to finish. Raises JobInterrupted (a subclass of Exception, not
    BaseException) when set, which the job engine in audio_jobs.py catches
    and marks the job as interrupted rather than failed."""
    chunks = [transcript[i:i + _CHUNK_SIZE] for i in range(0, len(transcript), _CHUNK_SIZE)]
    summaries: list[str] = []
    system = _with_instructions(_CHUNK_SYSTEM, extra_instructions)
    for i, chunk in enumerate(chunks):
        if job_interrupted and job_interrupted.is_set():
            raise JobInterrupted(f"interrupted before chunk {i + 1}/{len(chunks)}")
        summary = await generate_chat(
            [{"role": "user", "content": chunk}],
            system=system,
            model=model,
            options={"num_predict": _CHUNK_SUMMARY_MAX_TOKENS},
            think=False,  # per-chunk calls always think=False — small summaries,
            # not deep reasoning; the merge call below is where think applies.
        )
        if is_failure_sentinel(summary):
            _log.warning("summarize_transcript chunk %d/%d failed: %s", i + 1, len(chunks), summary[:120])
            summaries.append(f"[chunk {i + 1} failed: {summary}]")
            continue
        # Treat a whitespace-only summary (rare but possible with a very
        # short or silent audio segment) the same as a failure — weaving a
        # blank string into the merge prompt produces a gap in the final
        # recap that's indistinguishable from a real empty segment, while a
        # clearly-labelled placeholder at least tells the GM something went
        # wrong with that segment rather than leaving a silent hole.
        if not summary.strip():
            summaries.append(f"[chunk {i + 1}: no content]")
            continue
        summaries.append(summary)
    if job_interrupted and job_interrupted.is_set():
        raise JobInterrupted("interrupted before merge")
    merged_text = "\n\n".join(f"Segment {i + 1}:\n{s}" for i, s in enumerate(summaries))
    merge_system = _with_instructions(_MERGE_SYSTEM, extra_instructions)
    opts = dict(_thinking_num_predict_override(think))
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if "num_predict" in opts else 0)
    opts.update(_ctx_override_if_needed(merge_system + merged_text, reserve))
    return await generate_chat(
        [{"role": "user", "content": merged_text}],
        system=merge_system,
        model=model,
        options=opts or None,
        think=think,
    )


async def imagegen_models() -> list[str]:
    """Model checkpoint names available in SwarmUI/ComfyUI — used by the
    Models tab's image-model picker. Returns [] on any failure (SwarmUI not
    configured, unreachable, or the endpoint shape changed) rather than
    raising, same as installed_models_detail() above."""
    from .imaging import IMAGEGEN_URL  # avoid circular import at module level
    if not IMAGEGEN_URL:
        return []
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{IMAGEGEN_URL}/API/ListModels", params={"path": "", "depth": "2"})
            if r.status_code >= 400:
                return []
            data = r.json()
    except Exception:
        return []
    files = data.get("files") or []
    return [f.get("name", "") for f in files if isinstance(f, dict) and f.get("name")]


async def download_swarmui_model(url: str, dest_dir: Path) -> AsyncGenerator[dict, None]:
    """Stream a model file from `url` into `dest_dir`, yielding
    {"total": N, "completed": M} progress dicts followed by
    {"status": "done", "path": str(dest)} on success or
    {"error": "..."} on failure.

    This is a direct HTTP download into the SwarmUI models volume (via
    SWARMUI_MODELS_DIR) rather than going through SwarmUI's own API —
    SwarmUI has no download-from-URL endpoint, so we replicate the same
    "drop a .safetensors/.gguf file into the right subdirectory and
    refresh" workflow a GM would do manually. SwarmUI picks up new files
    on its next model refresh (triggered client-side after this completes).

    `dest_dir` is whatever subdirectory the caller resolved from the
    model type (e.g. SWARMUI_MODELS_DIR / "Stable-Diffusion") — we don't
    second-guess it here, just stream the bytes."""
    try:
        filename = Path(urlparse(url).path).name or "model"
        dest = dest_dir / filename
        async with _httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    yield {"error": f"HTTP {r.status_code}"}
                    return
                total = int(r.headers.get("content-length", 0))
                completed = 0
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(65_536):
                        f.write(chunk)
                        completed += len(chunk)
                        yield {"total": total, "completed": completed}
        yield {"status": "done", "path": str(dest)}
    except Exception as exc:
        yield {"error": f"{type(exc).__name__}: {exc}"}
