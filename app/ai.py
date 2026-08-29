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


def set_ollama_generation_overrides(options: dict, keep_alive: str = "") -> None:
    global _ollama_options_override, _ollama_keep_alive_override
    _ollama_options_override = dict(options) if options else {}
    _ollama_keep_alive_override = keep_alive or ""


def effective_ollama_options() -> dict:
    return dict(_ollama_options_override)


def effective_ollama_keep_alive() -> str:
    return _ollama_keep_alive_override


def _chat_kwargs(extra_options: dict = None, think: bool = False) -> dict:
    """Extra kwargs (options=, keep_alive=, think=) to splat into every
    .chat() call below — built fresh each call so a runtime settings change
    (no server restart needed) takes effect on the very next request.
    `extra_options` (a per-request override — see a chat preset's options,
    app/routers/ai.py) is layered OVER the instance-wide AppSettings
    defaults, not replacing them: an unset key still falls back to whatever
    Settings > System configured, so a preset only has to specify what it
    wants to differ.

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
    controls it per call."""
    kwargs = {"think": think}
    opts = {**effective_ollama_options(), **(extra_options or {})}
    if opts:
        kwargs["options"] = opts
    keep_alive = effective_ollama_keep_alive()
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
            return a, f"Using {a} (closest match to requested “{target}”)"
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
            **_chat_kwargs(),
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


async def generate_chat(messages: list[dict], system: str = "", model: str = "", options: dict = None, think: bool = False) -> str:
    m = model or effective_ollama_model()
    _log.info("generate_chat model=%s msgs=%d", m, len(messages))
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        resp = await _client().chat(model=m, messages=full, **_chat_kwargs(options, think))
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
        _log.warning(
            "generate_chat model=%s returned empty content (done_reason=%r, eval_count=%r, had_thinking=%r)",
            m, done_reason, eval_count, bool(thinking),
        )
        if thinking:
            return (
                f"[empty response from {m} — it produced {len(thinking)} character(s) of hidden "
                "\"thinking\" output but no final answer (usually means it ran out of output "
                "budget mid-reasoning). Try a shorter prompt, a higher response-length limit, "
                "or a non-reasoning model.]"
            )
        detail = f"done_reason={done_reason}" if done_reason else "no done_reason reported"
        return f"[empty response from {m} ({detail}) — try a different model, or check the Ollama server logs]"
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


async def stream_chat(messages: list[dict], system: str = "", model: str = "", options: dict = None) -> AsyncGenerator[str, None]:
    m = model or effective_ollama_model()
    _log.info("stream_chat model=%s msgs=%d", m, len(messages))
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    try:
        async for chunk in await _client().chat(model=m, messages=full, stream=True, **_chat_kwargs(options)):
            token = chunk.message.content
            if token:
                yield token
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
            **_chat_kwargs(),
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
            **_chat_kwargs(),
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
            **_chat_kwargs(),
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


async def expand_recap_notes(notes: str, model: str = "", think: bool = True) -> str:
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
    a GM-configured num_predict cap is widened when think=True."""
    return await generate_chat(
        [{"role": "user", "content": notes}], system=_EXPAND_NOTES_SYSTEM, model=model,
        options=_thinking_num_predict_override(think) or None, think=think,
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
    family of functions differs from generate_chat's own plain default."""
    if not facts:
        return ""
    bullet_list = "\n".join(f"- {f}" for f in facts)
    system = _with_instructions(_SUMMARIZE_FACTS_SYSTEM, extra_instructions)
    return await generate_chat(
        [{"role": "user", "content": bullet_list}], system=system, model=model,
        options=_thinking_num_predict_override(think) or None, think=think,
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
    world_context: str = "",
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
    ALSO sets options["num_predict"], a real Ollama-enforced hard cap —
    layered onto whatever `options` the caller already computed (e.g.
    context_sized_options for fit_context) rather than replacing it — but
    ONLY when think=False. With think=True, hidden reasoning tokens share
    that same num_predict budget with the visible answer; forcing
    num_predict down to exactly max_tokens risks the model spending its
    entire budget on reasoning and writing no visible answer at all — a
    real, reported failure (see generate_chat's own empty-content/
    "had_thinking" diagnostic for exactly what that looks like: content
    empty, thinking full of text, usually done_reason=="length"). So with
    think=True, max_tokens becomes prompt guidance only too, same
    best-effort contract min_tokens already has — the model isn't
    hard-stopped, just asked nicely via the "Length target" text above.
    See condense_call_options' own docstring for how the caller should
    widen num_ctx to match, giving thinking generous room instead of a
    hard cap.

    `world_context`, if given, is RAG-retrieved World lore/Notes text
    (see app.audio_jobs._build_rag_context) prepended ahead of everything
    else — see _with_world_context's own docstring."""
    system = _with_world_context(_with_instructions(_CONDENSE_RECAP_SYSTEM, extra_instructions), world_context)
    chars_per_token = _chars_per_token_estimate(recap)
    length_notes = []
    if min_tokens:
        length_notes.append(
            f"at least ~{min_tokens} tokens (~{min_tokens * chars_per_token} characters) — "
            "don't cut it any shorter than that even if you could say it in fewer words"
        )
    if max_tokens:
        length_notes.append(f"no more than ~{max_tokens} tokens (~{max_tokens * chars_per_token} characters)")
    if length_notes:
        system += "\n\nLength target for the condensed recap: " + " and ".join(length_notes) + "."
    opts = dict(options) if options else {}
    if max_tokens and not think:
        opts["num_predict"] = max_tokens
    return await generate_chat(
        [{"role": "user", "content": recap}], system=system, model=model,
        options=opts or None, think=think,
    )


# Reserved for the system prompt (_CONDENSE_RECAP_SYSTEM is short, but this
# also covers the condensed output itself sharing the same context window)
# plus margin — same reasoning as _CHUNK_RESERVED_TOKENS above, just a
# smaller budget since condensing produces a few sentences, not a chunked
# summary. Never request less than _CONTEXT_FIT_FLOOR_TOKENS even for a
# one-line paste — a tiny context window has no headroom for the model's
# own response.
_CONTEXT_FIT_RESERVED_TOKENS = 512
_CONTEXT_FIT_FLOOR_TOKENS = 1024

# Generous assumed budget for a thinking-enabled model's hidden reasoning,
# on top of the visible answer's own max_tokens target — see condense_recap's
# own docstring for why max_tokens can't safely double as num_predict's hard
# cap once think=True, and condense_call_options' docstring for why num_ctx
# needs matching headroom.
_THINKING_HEADROOM_TOKENS = 4096


def _thinking_num_predict_override(think: bool) -> dict:
    """If the GM has configured a bounded num_predict (Settings > System >
    "Max output tokens"), it's a hard Ollama-enforced cap on the TOTAL
    tokens generated for a call — hidden thinking tokens and the visible
    answer share that one budget. A cap sized for a normal visible answer
    can starve a reasoning model's thinking before it ever writes visible
    text, surfacing as generate_chat's own "empty response ... hidden
    thinking output but no final answer" failure — this is exactly the
    risk condense_recap's own docstring describes, but nothing previously
    widened num_predict to protect against it. Adds _THINKING_HEADROOM_TOKENS
    on top of the GM's configured value for this one call only (never
    mutates the saved setting), same "extra room for thinking, layered on
    top of not instead of" reasoning condense_call_options already uses for
    num_ctx. A no-op when num_predict is unset (Ollama's own default, -1 =
    unlimited) or think=False (nothing sharing the budget to protect)."""
    if not think:
        return {}
    configured = effective_ollama_options().get("num_predict")
    if not configured or configured < 0:
        return {}
    return {"num_predict": configured + _THINKING_HEADROOM_TOKENS}


def context_sized_options(text: str, reserve_tokens: int = _CONTEXT_FIT_RESERVED_TOKENS) -> dict:
    """A one-off num_ctx override sized to comfortably fit `text` for a
    single AI call, instead of relying on whatever num_ctx the GM has
    configured (or Ollama/the model's own Modelfile default — commonly as
    low as 2048-4096 tokens) — a long pasted recap that exceeds that gets
    silently truncated before the model ever reads all of it. Reuses the
    same chars-per-token heuristic _transcript_chunk_char_budget already
    relies on (_chars_per_token_estimate) rather than a fixed assumption.

    `reserve_tokens` defaults to _CONTEXT_FIT_RESERVED_TOKENS (system
    prompt + a short response + margin) — a caller expecting a longer
    response than that (e.g. condense_recap's own max_tokens set well
    above the default reserve) should pass a larger value so num_ctx
    still leaves room for the model to actually use that output budget,
    instead of the response getting squeezed by a context window sized
    for a much shorter answer.

    Pass the result as generate_chat's/condense_recap's `options` kwarg — a
    per-call override layered on top of app.ai's instance-wide default for
    that one request (see _chat_kwargs). It never mutates
    set_ollama_generation_overrides' own state, so the very next call keeps
    using the GM's configured/default context size — there's nothing to
    "set back to normal" because the instance-wide setting was never
    touched in the first place."""
    chars_per_token = _chars_per_token_estimate(text)
    input_tokens = -(-len(text) // chars_per_token)  # ceil division
    return {"num_ctx": max(_CONTEXT_FIT_FLOOR_TOKENS, input_tokens + reserve_tokens)}


_SUMMARIZE_TRANSCRIPT_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is a raw Whisper transcript of an "
    "actual-play session recording — expect filler words, misheard names, and no punctuation "
    "structure. Turn it into a short, readable narrative recap in flowing prose — a few "
    "paragraphs, past tense, third person. Use your judgment to skip out-of-character chatter, "
    "rules discussion, and filler, keeping only what happened in the story. Don't invent details "
    "that aren't in the transcript. Respond with the recap text only, no preamble or commentary."
)

_SUMMARIZE_TRANSCRIPT_PART_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is ONE PART of a longer raw Whisper "
    "transcript of an actual-play session recording — expect filler words, misheard names, no "
    "punctuation structure, and this excerpt starting and ending mid-scene. Turn just this part "
    "into a short, readable narrative summary in flowing prose (past tense, third person) — it "
    "will be appended directly after the summaries of the earlier parts (in order) to form the "
    "full session recap, so don't add your own preamble, conclusion, or reference to \"the rest "
    "of the summary\" — just narrate what happened in this part. Skip out-of-character chatter, "
    "rules discussion, and filler. Don't invent details that aren't in the text. Respond with "
    "this part's summary only, no preamble or commentary."
)

# A transcript longer than fits comfortably in one context window (a
# multi-hour session can easily be tens of thousands of tokens) is silently
# truncated by Ollama otherwise — the recap would quietly cover only part
# of the session with no signal anything was lost. We can't know the
# model's actual usable context at runtime (the GM may not have set
# ollama_num_ctx at all, in which case Ollama/the model's own Modelfile
# default applies — commonly as low as 2048-4096 tokens on a locally-run
# quantized model), so these deliberately err toward smaller chunks: the
# failure mode of chunking unnecessarily is a few extra AI calls, the
# failure mode of not chunking is losing most of a session's transcript.
_CHARS_PER_TOKEN_ESTIMATE = 4
_DEFAULT_ASSUMED_CTX_TOKENS = 4096
_CHUNK_RESERVED_TOKENS = 1200  # system prompt + response budget + margin


def _chars_per_token_estimate(text: str) -> int:
    """English averages ~4 chars/token, which is what _CHARS_PER_TOKEN_ESTIMATE
    assumes — but scripts without ASCII word-spacing (Cyrillic, CJK, etc.)
    tokenize much denser, commonly ~1.5-2.5 chars/token. Sampling the start
    of the text for non-ASCII content keeps the char budget from silently
    overshooting the model's real context window on exactly the non-English
    sessions this app is built to support."""
    sample = text[:4000]
    if not sample:
        return _CHARS_PER_TOKEN_ESTIMATE
    non_ascii = sum(1 for c in sample if ord(c) > 127)
    if non_ascii / len(sample) > 0.3:
        return 2
    return _CHARS_PER_TOKEN_ESTIMATE


def _transcript_chunk_char_budget(transcript: str = "", system: str = "", think: bool = True) -> int:
    """`transcript` (a sample of it) drives the chars-per-token estimate;
    `system` is the system prompt that will accompany each chunk — the GM's
    World.recap_instructions is free text with no length limit, so a long
    standing instruction could itself eat meaningfully into the context
    window that _CHUNK_RESERVED_TOKENS budgets for. Both are optional and
    default to the same fixed English/no-system-prompt assumption this
    function used before they were accounted for.

    `think` (default True, matching summarize_transcript's own default)
    reserves an extra _THINKING_HEADROOM_TOKENS on top of
    _CHUNK_RESERVED_TOKENS — the same constant/reasoning
    condense_call_options already uses for condense_recap. Without this, a
    reasoning-capable model can burn _CHUNK_RESERVED_TOKENS' worth (or
    more) of hidden thinking tokens before writing any visible text for a
    chunk, hit the context ceiling mid-reasoning, and return generate_chat's
    own "empty response ... hidden thinking output but no final answer"
    failure for that chunk — observed in production on a real session
    recap job. Reserving more headroom shrinks each chunk (a long
    transcript needs a few more chunks/AI calls), which is the same
    tradeoff this module's other budgets already choose deliberately: a
    little over-chunking is cheap, silently losing a chunk's summary to a
    starved thinking budget is not."""
    ctx_tokens = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    chars_per_token = _chars_per_token_estimate(transcript)
    system_tokens = (len(system) // _CHARS_PER_TOKEN_ESTIMATE) if system else 0
    reserved = _CHUNK_RESERVED_TOKENS + system_tokens + (_THINKING_HEADROOM_TOKENS if think else 0)
    input_tokens = max(500, ctx_tokens - reserved)
    return input_tokens * chars_per_token


def condense_call_options(
    transcript: str, extra_instructions: str = "", world_context: str = "",
    max_tokens: int | None = None, think: bool = True, force_fit: bool = False,
) -> dict | None:
    """The `options` a Condense call (job-based or the blocking route)
    should pass to condense_recap, so a long input — further lengthened by
    a GM's extra_instructions and/or RAG's world_context, both of which
    land in the system prompt ahead of the model ever reading `transcript`
    — can't silently exceed the model's real usable context.

    Unlike summarize_transcript's map-reduce chunking (already defended by
    _transcript_chunk_char_budget's _DEFAULT_ASSUMED_CTX_TOKENS fallback —
    see its own comment), condense_recap is always a single unchunked call
    with no chunking to fall back on. Ollama silently truncates a prompt
    that overflows num_ctx instead of raising — observed in practice (with
    Gemma) to corrupt the prompt badly enough that the model responds with
    a run of reserved/unused vocabulary tokens (e.g. "<unused49>") instead
    of an error or a sensible answer, which still reads back as a
    successful, "done" job since it's real (if garbage) text, not one of
    generate_chat's own failure sentinels — is_failure_sentinel has no way
    to catch it.

    `think`, together with `max_tokens`, widens the reserve by
    _THINKING_HEADROOM_TOKENS: condense_recap stops treating max_tokens as
    a hard num_predict cap once think=True (see its own docstring for why
    — hidden reasoning tokens would otherwise compete with the visible
    answer for that same budget), so num_ctx needs generous matching
    headroom instead, or a long reasoning-plus-answer generation could
    still hit the SAME kind of context-overflow corruption this function
    exists to prevent — just from an uncapped generation instead of an
    oversized prompt. No widening when max_tokens isn't set at all:
    condense_recap never touches num_predict in that case either way, so
    there's nothing extra to make room for here.

    `force_fit=True` (the "Condense (fit context)" button) always returns
    the computed size — a deliberate override even of a GM's own larger
    configured num_ctx, e.g. to save VRAM on a short recap, same behavior
    this had before this function existed. Otherwise (plain Condense) this
    only steps in when the computed requirement exceeds BOTH the GM's
    configured num_ctx (if any) and _DEFAULT_ASSUMED_CTX_TOKENS — the same
    "we can't know the real default, so assume the conservative low end"
    reasoning _transcript_chunk_char_budget already uses — so an ordinary
    short recap keeps using the GM's configured/default context unchanged
    (returns None, same as always), and only a genuinely oversized call
    gets the protection."""
    chars_per_token = _chars_per_token_estimate(transcript)
    extra_chars = len(extra_instructions or "") + len(world_context or "")
    thinking_headroom = _THINKING_HEADROOM_TOKENS if (think and max_tokens) else 0
    reserve = (
        max(_CONTEXT_FIT_RESERVED_TOKENS, (max_tokens or 0) + 256)
        + extra_chars // chars_per_token + thinking_headroom
    )
    needed = context_sized_options(transcript, reserve_tokens=reserve)["num_ctx"]
    if force_fit:
        return {"num_ctx": needed}
    baseline = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    return {"num_ctx": needed} if needed > baseline else None


def _split_transcript_into_chunks(transcript: str, chunk_chars: int) -> list[str]:
    """Split on a paragraph, line, or sentence boundary near the end of each
    window where one exists, so a chunk doesn't get cut mid-sentence — falls
    back to a hard cut at chunk_chars if no such boundary is found late
    enough in the window to still make meaningful progress.

    A single "\\n" is checked between the paragraph and sentence-punctuation
    candidates because that's the real per-segment separator whisper.cpp
    writes into a transcript — a raw Whisper transcript essentially never
    contains a blank-line paragraph break or "word. " sentence spacing, so
    without this candidate every long transcript hard-cut mid-word."""
    if len(transcript) <= chunk_chars:
        return [transcript]
    chunks = []
    pos = 0
    n = len(transcript)
    min_break = chunk_chars // 2
    while pos < n:
        end = min(pos + chunk_chars, n)
        if end < n:
            window = transcript[pos:end]
            break_at = window.rfind("\n\n")
            if break_at < min_break:
                idx = window.rfind("\n")
                if idx > break_at:
                    break_at = idx
            if break_at < min_break:
                for sep in (". ", "! ", "? "):
                    idx = window.rfind(sep)
                    if idx > break_at:
                        break_at = idx + len(sep) - 1
            if break_at >= min_break:
                end = pos + break_at + 1
        chunk = transcript[pos:end].strip()
        if chunk:
            chunks.append(chunk)
        pos = end
    return chunks


def _with_instructions(system: str, extra_instructions: str) -> str:
    """Append a GM's free-text steering (World.recap_instructions — e.g.
    "write the summary in Spanish", "focus only on combat") onto a base
    system prompt."""
    if not extra_instructions:
        return system
    return f"{system}\n\nAdditional instructions from the GM (follow these too): {extra_instructions}"


def _with_world_context(system: str, world_context: str) -> str:
    """Prepend RAG-retrieved World lore/Notes (see app.audio_jobs._build_
    rag_context — the same entity/notes retrieval AI Chat's own RAG uses)
    ahead of the rest of the system prompt, so the model has established
    names/places/facts on hand for accuracy — e.g. spelling an NPC's name
    correctly in a condensed recap instead of guessing from the transcript
    alone.

    The explicit call-out for a differently-spelled/transliterated/
    translated name exists for a reported real case: a GM's sessions are
    recorded (and transcribed) in Russian, but their World's entities are
    named in English — left to its own judgment, the model translated a
    character's Russian name into a plausible but wrong English rendering
    ("Crimson Puppet") instead of the one actually established in the
    World ("Crimson Doll"). A bare "for accuracy" framing doesn't reliably
    stop a model from confidently inventing its own translation of a name
    it doesn't recognize as already having a canonical English form — this
    has to ask for that explicitly.

    Still purely reference material, not a GM instruction, so it's labeled
    and kept separate from _with_instructions' own GM-steering block."""
    if not world_context:
        return system
    return (
        "Relevant world lore and notes (for accuracy only — don't invent beyond what's here). "
        "If the input text refers to a character or place differently than this list — a "
        "different spelling, transliteration, or translation, e.g. because the input is in "
        "another language — use the exact name from this list instead; don't invent your own "
        "translation or transliteration of it:\n"
        f"{world_context}\n\n{system}"
    )


async def summarize_transcript(transcript: str, model: str = "", extra_instructions: str = "", on_progress=None,
                                on_checkpoint=None, should_stop=None, resume: dict | None = None,
                                think: bool = True, world_context: str = "") -> str:
    """Turn a raw Whisper transcript (see transcribe_audio) of a session
    recording into a narrative recap. Transcripts that fit in one context
    window go through a single generate_chat call, same as before.

    A longer transcript is split into chunks (see
    _transcript_chunk_char_budget) and each chunk is summarized into its
    own readable prose paragraph(s) independently; the final recap is just
    those part-summaries joined together IN ORDER, with no further LLM
    call over the combined result. Two designs were tried and rejected
    before landing here:

    - A single "combine every part summary into one final recap" call:
      that combined blob has to fit in one context window too, and for a
      long enough session (enough chunks) it could overflow the same
      budget chunking exists to avoid in the first place.
    - An iterative "refine the recap so far with this next part's events"
      chain, one call per chunk: real models (especially smaller/local
      ones) drift toward whatever was rewritten most recently across
      repeated rewrite passes — a GM reported a recap that covered only
      the tail of a session, everything before the last couple of parts
      silently dropped.

    Neither problem can happen here: nothing ever asks a model to look at
    the whole recap at once. The tradeoff is a recap built from N
    independently-written paragraphs rather than one seamlessly blended
    narrative — transitions between parts can read a little abruptly, but
    nothing from any part is ever at risk of being silently dropped or
    truncated, at any transcript length.

    This is chunking purely over the resulting TEXT so a long transcript
    doesn't blow the model's context window — a separate concern from
    transcribe_audio's own audio-level chunking (see
    _split_audio_into_chunks), which splits the recording itself before any
    of this ever runs. `on_progress(current, total)`, if given, is
    called before each part's summarize call (current is 1-based —
    "currently on part 2 of 5") so a caller (audio_jobs.py) can persist
    real progress instead of a bare "summarizing" placeholder. Never
    called at all for a short, unchunked transcript.

    `on_checkpoint(state)`, `should_stop`, and `resume` are the same
    checkpoint/resume contract transcribe_audio uses (see its own
    docstring) — also only exercised on the chunked path, since an
    unchunked transcript is one call with nothing to checkpoint between.
    `should_stop()` is polled before each part; if it goes true,
    JobInterrupted is raised instead of continuing (see app.job_shutdown).
    `resume`, if given and its "phase"/"chunk_total"/"chunk_chars" match
    this call's own chunking exactly, skips the parts already summarized
    and continues from resume["text"] (the prior parts already joined) —
    a mismatch (e.g. num_ctx or extra_instructions changed since the
    checkpoint was written, changing chunk_chars) is logged and discarded
    rather than risking a spliced-together recap from two different
    chunkings.

    `think` defaults to True (unlike generate_chat's own plain default of
    False) and is forwarded to every generate_chat call this makes,
    chunked or not — see expand_recap_notes's docstring for why this
    family of functions differs from generate_chat's own default. It also
    widens both output-budget knobs the chunked path relies on to fit a
    reasoning model's hidden thinking alongside the visible answer — see
    _transcript_chunk_char_budget's and _thinking_num_predict_override's
    own docstrings.

    `world_context`, if given, is RAG-retrieved World lore/Notes text (see
    app.audio_jobs._build_rag_context) prepended ahead of everything else —
    see _with_world_context's own docstring."""
    transcript = (transcript or "").strip()
    if not transcript:
        return ""
    # Budgeted against the PART system prompt (used once chunking is
    # decided) since that's the one whose length actually matters here —
    # extra_instructions is free text the GM controls and can be long, and
    # world_context (RAG lore/notes) can be too — both must be included
    # here so chunk sizing accounts for the system prompt's real length.
    part_system = _with_world_context(_with_instructions(_SUMMARIZE_TRANSCRIPT_PART_SYSTEM, extra_instructions), world_context)
    chunk_chars = _transcript_chunk_char_budget(transcript, part_system, think)
    # See _thinking_num_predict_override's own docstring — widens a
    # GM-configured num_predict cap so hidden thinking tokens don't compete
    # with the visible recap for the same hard budget. Computed once since
    # `think` doesn't change across this call's chunked/unchunked branches.
    predict_override = _thinking_num_predict_override(think) or None
    chunks = _split_transcript_into_chunks(transcript, chunk_chars)
    if len(chunks) <= 1:
        system = _with_world_context(_with_instructions(_SUMMARIZE_TRANSCRIPT_SYSTEM, extra_instructions), world_context)
        return await generate_chat(
            [{"role": "user", "content": transcript}], system=system, model=model,
            options=predict_override, think=think,
        )

    _log.info("summarize_transcript: chunking into %d part(s) (%d chars total)", len(chunks), len(transcript))
    system = part_system

    start = 0
    part_summaries = []
    if resume and resume.get("phase") == "summarize" and resume.get("chunk_total") == len(chunks) \
            and resume.get("chunk_chars") == chunk_chars:
        start = resume.get("parts_done", 0)
        part_summaries = [resume.get("text", "")]
    elif resume:
        _log.warning(
            "discarding a summarization checkpoint that no longer matches this transcript's chunking "
            "(chunk_total=%s vs %s, chunk_chars=%s vs %s) — the recap's context/instructions likely "
            "changed since the checkpoint was written",
            resume.get("chunk_total"), len(chunks), resume.get("chunk_chars"), chunk_chars,
        )

    for i in range(start, len(chunks)):
        chunk = chunks[i]
        if should_stop and should_stop():
            raise JobInterrupted(f"stopped before summarizing part {i + 1} of {len(chunks)}")
        if on_progress:
            on_progress(i + 1, len(chunks))
        part = await generate_chat(
            [{"role": "user", "content": chunk}], system=system, model=model,
            options=predict_override, think=think,
        )
        if is_failure_sentinel(part):
            return part  # propagate the failure rather than weaving an error string into the recap
        part = part.strip()
        if not part:
            # generate_chat's own empty-content sentinel starts with
            # "[empty response" (caught above) — this is the separate case
            # of a technically-successful call whose content was only
            # whitespace, which is_failure_sentinel can't see. Treat it the
            # same way: abort with a clear reason rather than silently
            # joining a blank paragraph into the recap where a whole
            # chunk's events should be.
            return f"[empty response from part {i + 1} of {len(chunks)} — the model returned no usable text for this part]"
        part_summaries.append(part)
        if on_checkpoint:
            on_checkpoint({
                "phase": "summarize", "parts_done": i + 1, "chunk_total": len(chunks),
                "chunk_chars": chunk_chars, "text": "\n\n".join(part_summaries),
            })
    return "\n\n".join(part_summaries)


# base.html polls POST /api/ai/status once per open tab per page load — a
# GM with several tabs open (or repeatedly navigating) re-hits Ollama's
# /api/tags every time for a value that almost never changes second to
# second. A short cache collapses that into one real call per window.
_STATUS_CACHE_TTL = 15.0
_status_cache: tuple[float, dict] | None = None


async def status() -> dict:
    global _status_cache
    now = time.monotonic()
    if _status_cache and now - _status_cache[0] < _STATUS_CACHE_TTL:
        return _status_cache[1]
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        result = {"status": "ok", "model": effective_ollama_model(), "loaded_models": models}
    except Exception:
        result = {"status": "unavailable", "model": effective_ollama_model()}
    _status_cache = (now, result)
    return result


async def debug_info() -> dict:
    whisper = await whisper_status()
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": True,
            "loaded_models": models,
            "default_model": effective_ollama_model(),
            "whisper": whisper,
        }
    except Exception as exc:
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
            "default_model": effective_ollama_model(),
            "whisper": whisper,
        }


# ── Image generation ──────────────────────────────────────────────────────────

_IMAGEGEN_TYPE = os.environ.get("IMAGEGEN_TYPE", "").lower()   # "swarmui" or "comfyui"
_IMAGEGEN_URL  = os.environ.get("IMAGEGEN_URL", "").rstrip("/")

# Runtime overrides (set by /admin/imagegen/install without needing a restart)
_imagegen_type_override: str = ""
_imagegen_url_override:  str = ""


def _get_type() -> str:
    return _imagegen_type_override or _IMAGEGEN_TYPE


def _get_url() -> str:
    return (_imagegen_url_override or _IMAGEGEN_URL).rstrip("/")


def set_imagegen_override(itype: str, url: str) -> None:
    global _imagegen_type_override, _imagegen_url_override
    _imagegen_type_override = itype.lower()
    _imagegen_url_override  = url.rstrip("/")

_COMFYUI_WORKFLOW = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "{model}"}},
    "2": {"class_type": "CLIPTextEncode",         "inputs": {"text": "{prompt}",   "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode",         "inputs": {"text": "{negative}", "clip": ["1", 1]}},
    "4": {"class_type": "EmptyLatentImage",        "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "5": {"class_type": "KSampler",               "inputs": {"model": ["1", 0], "positive": ["2", 0],
                                                              "negative": ["3", 0], "latent_image": ["4", 0],
                                                              "seed": 0, "steps": 20, "cfg": 7,
                                                              "sampler_name": "euler", "scheduler": "normal",
                                                              "denoise": 1.0}},
    "6": {"class_type": "VAEDecode",              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage",              "inputs": {"images": ["6", 0], "filename_prefix": "ndworld"}},
}


async def _swarmui_session(u: str, c: _httpx.AsyncClient) -> str:
    try:
        r = await c.post(f"{u}/API/GetNewSession", json={})
        return r.json().get("session_id", "ndworld")
    except Exception:
        return "ndworld"


async def _swarmui_refresh_models(u: str, c: _httpx.AsyncClient, session_id: str) -> bool:
    """Best-effort: make SwarmUI rescan its Models folder so a file we just
    wrote straight into the shared volume (see SWARMUI_MODELS_DIR below)
    actually shows up in /API/ListModels. SwarmUI has no dedicated "rescan
    now" route — it only rebuilds its in-memory model list at startup, or
    as a side effect of /API/ChangeServerSettings when the request touches
    a paths.* key. So this reads back the current SD model folder setting
    and immediately re-saves it unchanged, purely to trigger that refresh.

    Requires the calling session to hold SwarmUI's `edit_server_settings`
    permission — true by default for the single bundled-SwarmUI instance
    this app's docker-compose spins up, but not guaranteed on an externally
    managed one. Returns False (never raises) if anything about this trick
    doesn't pan out; the caller falls back to telling the GM to restart
    SwarmUI, so a False here never leaves a downloaded file silently
    unusable."""
    try:
        r = await c.post(f"{u}/API/ListServerSettings", json={"session_id": session_id})
        value = r.json()["settings"]["paths.sdmodelfolder"]["value"]
        r2 = await c.post(f"{u}/API/ChangeServerSettings", json={
            "session_id": session_id,
            "rawData": {"settings": {"paths.sdmodelfolder": value}},
        })
        return "error" not in r2.json()
    except Exception:
        return False


async def swarmui_refresh_after_local_change() -> bool:
    """Public best-effort wrapper around _swarmui_refresh_models, for any
    caller that just changed a file under SWARMUI_MODELS_DIR directly on
    disk (a download or a delete) and wants SwarmUI's own model list to
    notice. Never raises; returns False if not configured for SwarmUI, or
    if the refresh trick itself didn't work."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return False
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            sid = await _swarmui_session(u, c)
            return await _swarmui_refresh_models(u, c, sid)
    except Exception:
        return False


# ── Audio transcription (whisper.cpp server) ────────────────────────────────
# See app/routers/ai.py's /attachments/upload — an uploaded audio attachment
# is transcribed here (regardless of its original format; the server itself
# transcodes via ffmpeg, see the "--convert" flag on the "whisper" Compose
# service) so its content reaches the chat model as plain text, the same
# reliable path a document attachment already uses — independent of whether
# the chat model itself has any native audio understanding.

async def whisper_status() -> dict:
    url = effective_whisper_url()
    if not url:
        return {"ok": False, "reason": "not configured"}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{url}/health")
            return {"ok": r.status_code == 200 and r.json().get("status") == "ok", "url": url}
    except Exception as e:
        return {"ok": False, "reason": str(e), "url": url}


class WhisperError(Exception):
    """Raised by transcribe_audio when the request to Whisper itself failed
    (not configured, unreachable, timed out, or returned a non-200/
    unreadable response) — distinct from a successful transcription that
    just happens to be empty (a genuinely silent clip), which is NOT an
    error and returns "" normally. Callers that need real detail for the
    GM (audio jobs, session recap routes) catch this and surface str(exc);
    callers where a failed transcription should just quietly leave an
    attachment without transcript text (_finish_attachment_upload) catch
    and swallow it instead.

    `partial_transcript`, when non-empty, is every chunk successfully
    transcribed before the failing one, already joined and collapsed —
    set only by transcribe_audio's chunked path, when at least one prior
    chunk succeeded. A whisper container restart 3 hours into a 4-hour
    session used to discard all 3 hours of already-completed work along
    with the error; a caller that saves this (audio_jobs.py) lets the GM
    resummarize from the salvaged partial instead of re-uploading and
    re-transcribing the whole recording from scratch."""

    def __init__(self, message: str, partial_transcript: str = ""):
        super().__init__(message)
        self.partial_transcript = partial_transcript


def _collapse_repeated_transcript_lines(text: str, min_repeat: int = 4) -> str:
    """whisper.cpp emits one newline-separated line per decoded segment
    (see output_str() in examples/server/server.cpp — `result << text <<
    "\\n"` per segment), so a degenerate repetition loop (see
    transcribe_audio's docstring) shows up as the exact same line repeated
    many times in a row. beam_size/entropy_thold already cut this down a
    lot, but short runs (roughly 4-25 repeats observed in practice) still
    slip through — a real conversation essentially never produces the
    exact same segment text 4+ times back to back, so collapsing any such
    run down to one copy is a safe, purely mechanical cleanup that needs
    no model call and can't accidentally remove genuine short exchanges
    (a person actually saying "Yes." a few times across a session doesn't
    do it consecutively in the same breath)."""
    lines = text.split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        j = i
        while j < n and lines[j] == lines[i]:
            j += 1
        run_len = j - i
        if run_len >= min_repeat:
            out.append(lines[i])
            _log.info("collapsed a %d-line repeated transcript run: %r", run_len, lines[i][:80])
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


# How long a clip has to be before it's worth paying ffmpeg's split
# overhead — see _split_audio_into_chunks's docstring for why chunking
# exists at all. 15 min: long enough that a typical short chat-attachment
# voice memo or a live-transcript chunk never takes this path (no wasted
# ffprobe/ffmpeg round trip for the common case), short enough that a
# multi-hour session recording still gets split into a meaningful number
# of pieces.
WHISPER_CHUNK_SECONDS = max(60.0, float(os.getenv("WHISPER_CHUNK_SECONDS", str(10 * 60))))
# Always comfortably above WHISPER_CHUNK_SECONDS itself, so a clip just
# over the threshold still splits into at least two real chunks instead of
# producing a single-segment "split" that's really just the original file
# with extra ffmpeg overhead (transcribe_audio handles that case safely
# either way, but there's no reason to configure it into existence).
_WHISPER_CHUNK_MIN_DURATION = max(15 * 60, WHISPER_CHUNK_SECONDS * 1.5)


async def _probe_audio_duration(path: Path) -> float | None:
    """ffprobe's own duration read, in seconds. None (not raised) if
    ffprobe isn't installed or the file couldn't be probed — the caller
    treats that identically to "short clip, don't bother chunking" rather
    than failing transcription over a diagnostic step that was always
    optional. (A pre-image-rebuild deployment without ffmpeg keeps
    working exactly like before this feature existed.)"""
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return float(out.decode().strip())
    except Exception:
        return None


async def _split_audio_into_chunks(path: Path, chunk_seconds: float) -> tuple[list[Path], Path | None]:
    """Split a long recording into ~chunk_seconds pieces via ffmpeg's
    segment muxer (stream copy, no re-encode — fast and lossless) so a
    whisper.cpp repetition loop (see transcribe_audio's docstring) can
    only ever ruin one chunk's worth of audio instead of consuming the
    rest of a multi-hour file, and so a caller can report real per-chunk
    progress instead of one opaque multi-hour call.

    Returns (chunk_paths, tmpdir_to_clean_up_or_None). On any failure —
    ffmpeg missing, a crash, an unreadable output — returns ([path], None):
    the ORIGINAL path, unchanged, with no tmpdir (so the caller must never
    try to clean up a directory it didn't create; see the None sentinel).
    Falling back to whole-file transcription is far better than failing
    the job over a splitting step that was always meant to be a bonus."""
    import asyncio
    import shutil
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="nd-whisper-chunks-"))
    pattern = tmpdir / f"chunk_%04d{path.suffix}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(path), "-f", "segment",
            "-segment_time", str(int(chunk_seconds)), "-reset_timestamps", "1",
            "-c", "copy", str(pattern),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            _log.warning("ffmpeg audio split failed (rc=%s): %s", proc.returncode, stderr.decode(errors="replace")[:500])
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [path], None
        chunks = sorted(tmpdir.glob(f"chunk_*{path.suffix}"))
        if not chunks:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [path], None
        return chunks, tmpdir
    except Exception as exc:
        # Includes FileNotFoundError (ffmpeg not installed).
        _log.warning("ffmpeg audio split errored: %s: %s", type(exc).__name__, exc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return [path], None


# ── Speech enhancement (DeepFilterNet) ──────────────────────────────────────
# Optional: a real ML denoising model run over each audio file before it
# reaches Whisper, meaningfully better against sustained background audio
# (music, hum, HVAC) than a browser's own echo-cancellation/noise-gate
# heuristics (see ndMicRecorder in base.html). NOT a base dependency —
# torch alone is hundreds of MB — see requirements-denoise.txt and the
# Dockerfile's INSTALL_DENOISE build arg. Every entry point below degrades
# to a no-op (raw audio, unchanged) if the dependency isn't installed or
# anything about denoising fails, so enabling this can never be the reason
# a transcription fails outright.

_speech_enhancement_available_cache: bool | None = None
_denoise_model_cache = None  # (model, df_state) tuple, loaded lazily once per process


def speech_enhancement_available() -> bool:
    """Whether this container actually has the DeepFilterNet dependency
    stack installed. Feature-detected once via try/except ImportError and
    cached — importing torch is itself slow (hundreds of ms) and this is
    checked on every denoise-enabled transcription plus the settings
    route that lets a GM toggle World.whisper_denoise on."""
    global _speech_enhancement_available_cache
    if _speech_enhancement_available_cache is None:
        try:
            import torch  # noqa: F401
            import torchaudio  # noqa: F401
            import df.enhance  # noqa: F401
        except ImportError:
            _speech_enhancement_available_cache = False
        else:
            _speech_enhancement_available_cache = True
    return _speech_enhancement_available_cache


def _init_denoise_model():
    """Lazily loads and caches the DeepFilterNet model + DF state (the
    STFT/filtering state paired with it). Model loading is slow (downloads
    ~50MB to XDG_CACHE_HOME on first run — see the Dockerfile) and must
    only happen once per process, not once per audio file."""
    global _denoise_model_cache
    if _denoise_model_cache is None:
        from df.enhance import init_df
        model, df_state, _ = init_df()
        _denoise_model_cache = (model, df_state)
    return _denoise_model_cache


def _denoise_audio_file_sync(path: Path) -> Path:
    """Blocking DeepFilterNet enhancement of one audio file, writing the
    result to a new sibling file and returning its path. Always run via
    asyncio.to_thread (see denoise_audio_file) — this is CPU-bound (a
    small neural net forward pass) and would otherwise stall the event
    loop for however long the clip takes to process."""
    from df.enhance import enhance
    from df.io import load_audio, save_audio, resample
    from df.model import ModelParams
    model, df_state = _init_denoise_model()
    df_sr = ModelParams().sr
    audio, meta = load_audio(str(path), sr=df_sr)
    enhanced = enhance(model, df_state, audio)
    enhanced = resample(enhanced, df_sr, meta.sample_rate)
    # Always .wav regardless of the input's own container/codec (e.g. a
    # browser mic recording is typically .webm/opus) — torchaudio's save
    # path (used by save_audio) can't reliably ENCODE every container it
    # can decode, and .wav is the one format every backend can always
    # write. whisper.cpp accepts it natively either way (no conversion
    # needed on that end, unlike webm/opus, which it transcodes via
    # --convert).
    out_path = path.with_name(f"{path.stem}.denoised.wav")
    save_audio(str(out_path), enhanced, sr=meta.sample_rate, log=False)
    return out_path


async def denoise_audio_file(path: Path) -> Path:
    """Run speech enhancement over an audio file and return the path to
    the enhanced version — the ORIGINAL path, unchanged, if the
    dependency isn't installed or anything about denoising fails (a
    corrupt/unsupported file, a model load error, out of memory, ...).
    A denoising failure must never block transcription, since the raw
    audio would have transcribed fine before this feature existed. The
    caller owns cleanup of the returned path when it differs from the
    input (see _transcribe_one_file)."""
    if not speech_enhancement_available():
        return path
    try:
        import asyncio
        return await asyncio.to_thread(_denoise_audio_file_sync, path)
    except Exception as exc:
        _log.warning("speech enhancement failed, using raw audio instead: %s: %s", type(exc).__name__, exc)
        return path


async def _transcribe_one_file(path: Path, glossary: str, language: str, denoise: bool = False) -> str:
    """The actual whisper.cpp /inference call for a single audio file —
    see transcribe_audio's docstring for the parameters this sends and
    why. Kept separate from transcribe_audio so the chunking orchestrator
    below can call it once per chunk without duplicating any of this."""
    url = effective_whisper_url()
    if not url:
        raise WhisperError("Whisper isn't configured (no Whisper URL set) — see the AI page's 🎙 Whisper tab.")
    if not path.is_file():
        # Without this check, path.open() below raises FileNotFoundError,
        # which the generic "Could not reach Whisper" handler catches and
        # reports as a network/server problem — misleading for what's
        # actually a local file that's missing or already cleaned up.
        raise WhisperError(f"Audio file not found: {path.name}")
    send_path = path
    if denoise:
        send_path = await denoise_audio_file(path)
    data = {
        "response_format": "json",
        "language": language.strip() or "auto",
        "beam_size": "5",
        "entropy_thold": "2.6",
    }
    if glossary.strip():
        data["prompt"] = glossary.strip()
        # Without this, whisper.cpp only loads `prompt` into its rolling
        # 30-second decode context (prompt_past1), which gets overwritten
        # by decoded tokens after the very first window — so the glossary
        # only actually biased the first ~30s of a recording. This flag
        # (verified against whisper.cpp's source, src/whisper.cpp: the
        # carry_initial_prompt branch keeps it in the static prompt_past0,
        # prepended to every window instead) makes it apply for the whole
        # file. Older whisper.cpp servers that don't recognize this field
        # simply ignore it — no compatibility risk.
        data["carry_initial_prompt"] = "true"
    try:
        try:
            async with _httpx.AsyncClient(timeout=WHISPER_TIMEOUT_SECONDS) as c:
                with send_path.open("rb") as f:
                    r = await c.post(
                        f"{url}/inference",
                        files={"file": (send_path.name, f, "application/octet-stream")},
                        data=data,
                    )
        except (_httpx.TimeoutException, TimeoutError) as exc:
            # httpx's own timeout exceptions (ReadTimeout/ConnectTimeout/...)
            # derive from httpx.TimeoutException, NOT the builtin TimeoutError —
            # catching only TimeoutError here meant a real Whisper timeout fell
            # through to the generic "Could not reach Whisper" branch below
            # (with an often-empty message, since httpx timeouts commonly
            # stringify to "") instead of naming the actual timeout.
            _log.warning("whisper transcription timed out: %s", exc)
            raise WhisperError(f"Whisper timed out after {WHISPER_TIMEOUT_SECONDS}s — the clip may be too long, or the server is overloaded.") from exc
        except Exception as exc:
            _log.warning("whisper transcription unreachable: %s: %s", type(exc).__name__, exc)
            raise WhisperError(f"Could not reach Whisper: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            _log.warning("whisper transcription failed: HTTP %s: %s", r.status_code, r.text[:300])
            raise WhisperError(f"Whisper returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return (r.json().get("text") or "").strip()
        except Exception as exc:
            _log.warning("whisper returned an unreadable response: %s", exc)
            raise WhisperError(f"Whisper returned an unreadable response: {exc}") from exc
    finally:
        if send_path != path:
            send_path.unlink(missing_ok=True)


async def transcribe_audio(path: Path, glossary: str = "", language: str = "", on_progress=None,
                            on_checkpoint=None, should_stop=None, resume: dict | None = None,
                            denoise: bool = False) -> str:
    """Transcribe an audio file via whisper.cpp's /inference endpoint,
    transparently splitting a long recording into chunks first (see
    _split_audio_into_chunks) and collapsing any residual repetition-loop
    runs (see _collapse_repeated_transcript_lines) before returning.
    Returns "" for a successfully-transcribed silent clip. Raises
    WhisperError — with the actual reason, not a generic message — if a
    request to Whisper itself failed (a chunk's failure fails the whole
    call; there's no partial-success return today).

    `glossary` (a world's whisper_glossary — campaign NPC/place names and
    invented terms) is passed through as whisper.cpp's "prompt" field, which
    biases decoding toward those spellings/vocabulary without being
    transcribed itself (this is whisper_full's initial_prompt, not a chat
    prompt) — blank by default, so most callers are unaffected. Sent
    alongside carry_initial_prompt=true so the bias applies for the whole
    recording, not just its first ~30s window (see _transcribe_one_file);
    a non-empty glossary is re-sent identically for every audio chunk, so
    it stays in effect across chunk boundaries too.

    `language` (an ISO-639-1 code like "ru", or "auto"/"" for auto-detect) is
    always sent as whisper.cpp's "language" field, even when blank — omitting
    it entirely is NOT the same as auto-detect: whisper.cpp's server hardcodes
    `language = "en"` as its own default (see examples/server/server.cpp) and
    only overrides it when the client explicitly sends this field. Without
    this, every clip gets silently forced through English decoding regardless
    of what's actually being spoken — the likely cause of a non-English
    session producing a garbled, looping transcript rather than a WhisperError
    (Whisper "succeeds" throughout, it's just decoding the wrong language).

    Also always sends "beam_size"/"entropy_thold" overrides (see
    _transcribe_one_file) to reduce a different, language-independent
    failure mode: whisper.cpp's default greedy decoding can fall into a
    degenerate loop repeating the same phrase — and once inside that loop
    the model becomes MORE confident in repeating itself, so whisper.cpp's
    own low-confidence fallback rarely fires to escape it (it doesn't check
    compression-ratio/repetitiveness the way openai/whisper's reference
    decoder does — confirmed by reading examples/server/server.cpp).
    beam_size=5 and a slightly raised entropy_thold are the two settings
    the whisper.cpp community consistently cites for this — see
    ggml-org/whisper.cpp discussion #2286 and issue #1507. Splitting the
    audio itself (this function) and collapsing repeated lines afterward
    are this app's own additional mitigations on top of those, since even
    with both settings tuned a short repetition run can still slip through
    on a long enough recording.

    `on_progress(current, total)`, if given, is called before each
    chunk's /inference call (current is 1-based) — same shape
    summarize_transcript's own on_progress already uses, so a caller
    (audio_jobs.py) can persist real progress with the same DB fields for
    either phase. Never called at all for a clip short enough to skip
    chunking.

    `on_checkpoint(state)`, if given, is called after each chunk
    transcribes successfully, with enough to both resume and to show a
    partial result: {"phase": "transcribe", "chunks_done", "chunk_total",
    "chunk_seconds" (WHISPER_CHUNK_SECONDS at split time), "audio_size"
    (path.stat().st_size), "text" (everything transcribed so far,
    collapsed)}. `should_stop`, if given, is polled before each chunk;
    when it returns true, JobInterrupted is raised instead of continuing
    — the caller's already-persisted checkpoint is the resume point, not
    this call's return value. `resume`, if given, is a previous
    checkpoint to continue from: chunks already covered by
    resume["chunks_done"] are skipped and resume["text"] seeds the
    accumulated result, but ONLY if chunk_total/chunk_seconds/audio_size
    all still match this exact call — a mismatch (different audio, or
    WHISPER_CHUNK_SECONDS changed since the checkpoint was written) means
    the chunk boundaries themselves may differ, so splicing old and new
    text could silently duplicate or drop audio; discarded (logged, not
    raised) and transcription starts over from chunk 0 instead. Only the
    multi-chunk loop below checkpoints/resumes — the two single-call
    paths above have nothing to checkpoint between.

    `denoise`, if true, runs each audio file (the whole clip, or each
    chunk individually once split) through DeepFilterNet speech
    enhancement before it's sent to Whisper — see denoise_audio_file's
    own docstring for the no-op fallback if the dependency isn't
    installed or enhancement fails on a given file."""
    duration = await _probe_audio_duration(path)
    if not duration or duration <= _WHISPER_CHUNK_MIN_DURATION:
        text = await _transcribe_one_file(path, glossary, language, denoise)
        return _collapse_repeated_transcript_lines(text)

    chunks, tmpdir = await _split_audio_into_chunks(path, WHISPER_CHUNK_SECONDS)
    try:
        if len(chunks) == 1:
            # Still reachable with a real tmpdir (e.g. WHISPER_CHUNK_SECONDS
            # configured above _WHISPER_CHUNK_MIN_DURATION can produce a
            # single-segment split) — this branch must stay inside the same
            # try/finally as the multi-chunk loop below, not return before
            # it, or the tmpdir (a full stream-copy of the recording) is
            # never cleaned up.
            text = await _transcribe_one_file(chunks[0], glossary, language, denoise)
            return _collapse_repeated_transcript_lines(text)

        audio_size = path.stat().st_size
        start = 0
        parts = []
        if resume and resume.get("phase") == "transcribe" and resume.get("chunk_total") == len(chunks) \
                and resume.get("chunk_seconds") == WHISPER_CHUNK_SECONDS and resume.get("audio_size") == audio_size:
            start = resume.get("chunks_done", 0)
            parts = [resume.get("text", "")]
        elif resume:
            _log.warning(
                "discarding a transcription checkpoint that no longer matches this audio "
                "(chunk_total=%s vs %s, chunk_seconds=%s vs %s, audio_size=%s vs %s)",
                resume.get("chunk_total"), len(chunks), resume.get("chunk_seconds"), WHISPER_CHUNK_SECONDS,
                resume.get("audio_size"), audio_size,
            )

        for i in range(start, len(chunks)):
            chunk_path = chunks[i]
            if should_stop and should_stop():
                raise JobInterrupted(f"stopped before transcribing part {i + 1} of {len(chunks)}")
            if on_progress:
                on_progress(i + 1, len(chunks))
            try:
                parts.append(await _transcribe_one_file(chunk_path, glossary, language, denoise))
            except WhisperError as exc:
                if parts:
                    partial = _collapse_repeated_transcript_lines("\n".join(p for p in parts if p))
                    raise WhisperError(
                        f"Whisper failed on part {i + 1} of {len(chunks)}: {exc}. The first {i} part(s) "
                        "were transcribed and have been saved — you can re-summarize from the partial "
                        "transcript, or re-upload to redo the whole recording.",
                        partial_transcript=partial,
                    ) from exc
                raise
            if on_checkpoint:
                on_checkpoint({
                    "phase": "transcribe", "chunks_done": i + 1, "chunk_total": len(chunks),
                    "chunk_seconds": WHISPER_CHUNK_SECONDS, "audio_size": audio_size,
                    "text": _collapse_repeated_transcript_lines("\n".join(p for p in parts if p)),
                })
        return _collapse_repeated_transcript_lines("\n".join(p for p in parts if p))
    finally:
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Whisper model download ──────────────────────────────────────────────────
# nd-world's own container and the "whisper" Compose service both mount the
# same host directory (see docker-compose.yml/truenas-compose.yml), just at
# different internal paths — the same pattern SWARMUI_AC_DIR already uses to
# share the tag-autocomplete folder between `world` and `swarmui`. That lets
# a GM download a model file through nd-world instead of SSHing into the
# host, without nd-world needing any access to the whisper.cpp container
# itself (which has no download-a-model API of its own, unlike Ollama).

WHISPER_MODELS_DIR = Path(os.getenv("WHISPER_MODELS_DIR", "/data/whisper-models"))
# The "whisper" Compose service's fallback default — see active_whisper_model()
# below, which prefers a GM-set marker file over this once one exists. Still
# what a deployment on the OLD (pre-marker-aware) entrypoint actually loads,
# since that entrypoint only ever reads this env var.
WHISPER_MODEL_FILENAME = os.getenv("WHISPER_MODEL_FILE", "ggml-large-v3-turbo.bin")
# ggerganov/whisper.cpp's own official model repo — confirmed against a real
# deployment to be the format that actually loads. An earlier version of
# this pointed at a third-party GGUF-format mirror instead, on the
# assumption that ghcr.io/ggml-org/whisper.cpp:main's GGUF support (still an
# open PR as of ollama/ollama#15243 and this being written — see
# _build_ollama_messages' docstring in app/routers/ai.py, an unrelated
# feature that hit the same open-PR situation) had landed for *audio*
# models specifically; it hadn't — that image's whisper-server rejected the
# GGUF file with "invalid model data (bad magic)" and crash-looped on every
# restart. The classic ggml .bin format ggerganov/whisper.cpp itself
# distributes doesn't have that risk, since it's what the image's own
# loader has always targeted.
DEFAULT_WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
)

# A curated subset of ggerganov/whisper.cpp's official model list (see its
# models/README.md) spanning the speed/accuracy range — not the full ~16
# variants, same "curated, not exhaustive" choice as KNOWN_MODELS below for
# Ollama. Sizes are the real download sizes from that README, not estimates.
# Every filename here is downloaded from
# f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}" —
# nd-world only ever fetches from that one trusted host for these, unlike
# the free-text custom-URL field download_whisper_model also accepts.
WHISPER_KNOWN_MODELS = [
    {"filename": "ggml-tiny.bin", "label": "Tiny", "size": "75 MiB"},
    {"filename": "ggml-tiny.en.bin", "label": "Tiny (English only)", "size": "75 MiB"},
    {"filename": "ggml-base.bin", "label": "Base", "size": "142 MiB"},
    {"filename": "ggml-base.en.bin", "label": "Base (English only)", "size": "142 MiB"},
    {"filename": "ggml-small.bin", "label": "Small", "size": "466 MiB"},
    {"filename": "ggml-small.en.bin", "label": "Small (English only)", "size": "466 MiB"},
    {"filename": "ggml-medium.bin", "label": "Medium", "size": "1.5 GiB"},
    {"filename": "ggml-medium.en.bin", "label": "Medium (English only)", "size": "1.5 GiB"},
    {"filename": "ggml-large-v3.bin", "label": "Large v3", "size": "2.9 GiB"},
    {"filename": "ggml-large-v3-turbo.bin", "label": "Large v3 Turbo", "size": "1.5 GiB"},
    {"filename": "ggml-large-v3-turbo-q5_0.bin", "label": "Large v3 Turbo (quantized)", "size": "547 MiB"},
]
_WHISPER_KNOWN_FILENAMES = {m["filename"] for m in WHISPER_KNOWN_MODELS}

# Filename prefix as the whisper.cpp CONTAINER sees WHISPER_MODELS_DIR — the
# two only match by coincidence (both happening to be /data/whisper-models
# on a from-scratch docker-compose deployment); an externally-hosted whisper
# instance, or one with a differently-mounted volume, needs this set
# separately. Only used to build the path sent to /load (a server-side
# path) — never for anything nd-world reads/writes on its own side.
WHISPER_SERVER_MODELS_DIR = os.getenv("WHISPER_SERVER_MODELS_DIR", "/models")

# The "whisper" Compose service's entrypoint reads this file (if present) to
# decide which downloaded model to load at container start/restart — see
# docker-compose.yml. Written by set_active_whisper_model(), read by
# active_whisper_model(); an un-migrated deployment (old entrypoint, no
# marker support) just never sees this file, so WHISPER_MODEL_FILENAME
# keeps working as the sole source of truth exactly like it did before this
# existed — switching to the marker-aware entrypoint is the one manual,
# one-time step nd-world genuinely can't do for a GM (see docs/DEPLOYMENT.md).
_WHISPER_ACTIVE_MARKER = "active-model.txt"


def active_whisper_model() -> str:
    """The model filename nd-world currently considers "active" — the
    marker file if one exists and names a real known model, else
    WHISPER_MODEL_FILENAME (the env-var default, and what an un-migrated
    "whisper" Compose service is still actually loading). Never raises —
    a missing, unreadable, or garbage marker just falls back."""
    try:
        raw = (WHISPER_MODELS_DIR / _WHISPER_ACTIVE_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return WHISPER_MODEL_FILENAME
    if raw and raw in _WHISPER_KNOWN_FILENAMES:
        return raw
    return WHISPER_MODEL_FILENAME


def set_active_whisper_model(filename: str) -> None:
    """Record `filename` as the active model — persists across a "whisper"
    Compose service restart (its entrypoint reads this same file) even if
    load_whisper_model() below isn't called or fails. Raises ValueError on
    an unknown or not-yet-downloaded filename."""
    if filename not in _WHISPER_KNOWN_FILENAMES:
        raise ValueError(f"Unknown model filename: {filename!r}")
    if not (WHISPER_MODELS_DIR / filename).is_file():
        raise ValueError(f"{filename} hasn't been downloaded yet.")
    WHISPER_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    marker = WHISPER_MODELS_DIR / _WHISPER_ACTIVE_MARKER
    tmp = marker.with_name(marker.name + ".part")
    tmp.write_text(filename, encoding="utf-8")
    tmp.replace(marker)


def _looks_like_ggml(path: Path) -> bool:
    """Best-effort sanity check before calling load_whisper_model — NOT a
    full format validator (that's whisper.cpp's own job when it actually
    parses the file), just a guard against the one failure mode this app
    has already hit in production: a GGUF-format file (a different,
    incompatible model format whose files start with the literal bytes
    "GGUF") landing in WHISPER_MODELS_DIR — most plausibly via the
    free-text custom-URL download field, since every filename-based
    download here only ever fetches from the correct official host. A
    file this rejects is never sent to /load, since a load with a file
    that fails to parse leaves the whisper.cpp server's /health endpoint
    permanently reporting "loading model" until the container is
    restarted (see load_whisper_model)."""
    try:
        if path.stat().st_size < 1_000_000:  # every real model here is >= 75 MiB
            return False
        with path.open("rb") as f:
            head = f.read(4)
        return head != b"GGUF"
    except OSError:
        return False


async def load_whisper_model(filename: str) -> dict:
    """Ask the running whisper.cpp server to hot-swap to `filename` via its
    /load endpoint, without waiting for a container restart. Returns
    {"ok": bool, "detail": str} — never raises.

    Two things make this safe enough to call automatically (see
    docs/DEPLOYMENT.md for the full reasoning): /load validates the file
    exists before doing anything destructive, and the "whisper" Compose
    service runs with restart: unless-stopped, so even the exit(1)-on-
    unparseable-file case self-heals in a restart cycle rather than
    requiring a manual one. What does NOT self-heal on its own: a 400
    response (missing/invalid file) leaves the server's own /health
    endpoint stuck reporting "loading model" — never ready — until the
    container is restarted by hand, even though /inference keeps working
    fine on whatever was loaded before. That's why _looks_like_ggml is
    checked by the caller before this is ever invoked — not because
    /load itself is unsafe to call on a real model file.

    The path sent to whisper.cpp must be resolved on ITS side, not
    nd-world's — WHISPER_SERVER_MODELS_DIR, not WHISPER_MODELS_DIR — and
    must arrive as a multipart field (whisper.cpp's req.has_file("model")
    check only recognizes multipart parts; a urlencoded body is silently
    treated as "no file given" and 400s)."""
    url = effective_whisper_url()
    if not url:
        return {"ok": False, "detail": "Whisper isn't configured (no Whisper URL set)."}
    server_path = f"{WHISPER_SERVER_MODELS_DIR.rstrip('/')}/{filename}"
    try:
        async with _httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{url}/load", files={"model": (None, server_path)})
    except Exception as exc:
        _log.warning("whisper /load unreachable: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "detail": f"Could not reach Whisper: {type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        _log.warning("whisper /load failed: HTTP %s: %s", r.status_code, r.text[:300])
        return {
            "ok": False,
            "detail": (
                f"Whisper rejected the load (HTTP {r.status_code}) — its /health endpoint may now be "
                f"stuck reporting \"loading model\" until the whisper Compose service is restarted, "
                f"even though transcription itself should still work on the previously loaded model."
            ),
        }
    return {"ok": True, "detail": f"Switched to {filename}."}


def whisper_model_status() -> dict:
    """Whether a model file nd-world can see is already sitting in the
    shared volume — checked from nd-world's own side (WHISPER_MODELS_DIR),
    not by asking the whisper.cpp server itself (that's whisper_status()),
    since the file needs to exist before the server can even be pointed at
    it. Doesn't mean the *running* server has loaded it yet — that only
    happens on container start/restart (see download_whisper_model).

    "downloaded"/"filename"/"bytes" describe the *active* model — see
    active_whisper_model() (the marker file if set, else
    WHISPER_MODEL_FILENAME) — kept as top-level keys for whatever already
    reads this shape. "models" is the fuller picture: every known model's
    own download state and whether it's the currently-active one, so a GM
    can download several without any of them clobbering another.
    "active_source" is "marker" once a GM has explicitly activated
    something via POST /whisper/activate, or "env" while still on
    whatever WHISPER_MODEL_FILE happens to default to — lets the UI
    explain why nothing looks "chosen" yet on a fresh deployment."""
    active_filename = active_whisper_model()
    active = WHISPER_MODELS_DIR / active_filename
    models = []
    for m in WHISPER_KNOWN_MODELS:
        p = WHISPER_MODELS_DIR / m["filename"]
        downloaded = p.is_file()
        models.append({
            **m,
            "downloaded": downloaded,
            "bytes": p.stat().st_size if downloaded else 0,
            "active": m["filename"] == active_filename,
        })
    return {
        "downloaded": active.is_file(),
        "filename": active_filename,
        "bytes": active.stat().st_size if active.is_file() else 0,
        "active_source": "marker" if (WHISPER_MODELS_DIR / _WHISPER_ACTIVE_MARKER).is_file() else "env",
        "models": models,
    }


async def download_whisper_model(url: str = "", filename: str = "") -> AsyncGenerator[dict, None]:
    """Stream a whisper.cpp-compatible model file into WHISPER_MODELS_DIR,
    yielding {"total":, "completed":} progress dicts as bytes arrive — same
    shape Ollama's own /api/pull progress already uses (app/routers/ai.py's
    /pull), so the client-side JS can reuse the same parsing — and a final
    {"status": "done", ...} or {"error": "..."}.

    `filename`, when given, must be one of WHISPER_KNOWN_MODELS' filenames
    (checked here, not just trusted from the caller — this becomes a
    filesystem path, so anything else is rejected outright rather than
    risking a path-traversal write) — it's downloaded from the same
    official ggerganov/whisper.cpp host every known model uses, to a file
    of its own, so it can coexist with whatever's currently active. `url`
    is ignored in that case. With no `filename`, behavior is unchanged from
    before this parameter existed: `url` (or DEFAULT_WHISPER_MODEL_URL if
    blank) downloads to active_whisper_model(), the currently-active slot
    — note this is a free-text URL, so unlike the filename path above
    there's no guarantee it's even the right file format; see
    _looks_like_ggml, checked before a downloaded file is ever offered a
    hot-swap via POST /whisper/activate.

    Written to a "<filename>.part" file and only renamed into place once
    fully downloaded, so an interrupted/failed download can never leave a
    corrupt file behind for the whisper.cpp server to trip over — which
    matters more here than most partial-download cases: whisper.cpp's own
    /load endpoint calls exit(1) (killing the whole server process) if the
    model file it's given fails to parse, rather than returning an error.
    See load_whisper_model()/set_active_whisper_model() for how a GM
    switches which downloaded model is active — a hot-swap via /load where
    possible, falling back to "persists for next restart" (the marker file
    the "whisper" Compose service's entrypoint reads) either way, so this
    is no longer the fully manual step it once was."""
    if filename:
        if filename not in _WHISPER_KNOWN_FILENAMES:
            yield {"error": f"Unknown model filename: {filename!r}"}
            return
        fetch_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}"
        target_filename = filename
    else:
        fetch_url = (url or "").strip() or DEFAULT_WHISPER_MODEL_URL
        target_filename = active_whisper_model()
    WHISPER_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = WHISPER_MODELS_DIR / target_filename
    tmp = dest.with_name(dest.name + ".part")
    try:
        async with _httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
            async with c.stream("GET", fetch_url) as resp:
                if resp.status_code >= 400:
                    yield {"error": f"HTTP {resp.status_code} fetching model file"}
                    return
                total = int(resp.headers.get("content-length") or 0)
                completed = 0
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)
                        completed += len(chunk)
                        yield {"total": total, "completed": completed}
        tmp.replace(dest)
        yield {"status": "done", "filename": target_filename, "bytes": dest.stat().st_size}
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        _log.warning("whisper model download failed: %s: %s", type(exc).__name__, exc)
        yield {"error": f"{type(exc).__name__}: {exc}"}


async def imagegen_status() -> dict:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return {"ok": False, "reason": "not configured"}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            if t == "swarmui":
                r = await c.post(f"{u}/API/GetNewSession", json={})
                return {"ok": r.status_code < 400, "type": t, "url": u}
            else:
                r = await c.get(f"{u}/system_stats")
            return {"ok": r.status_code < 400, "type": t, "url": u}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


async def imagegen_loras() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "LoRA", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/LoraLoader")
                data = r.json()
                return data["LoraLoader"]["input"]["required"]["lora_name"][0]
    except Exception:
        return []


async def imagegen_samplers_schedulers() -> dict:
    t, u = _get_type(), _get_url()
    if u:
        try:
            async with _httpx.AsyncClient(timeout=8) as c:
                if t == "comfyui":
                    r = await c.get(f"{u}/object_info/KSampler")
                else:
                    # SwarmUI proxies the ComfyUI API at /comfyui/
                    r = await c.get(f"{u}/comfyui/object_info/KSampler")
                data = r.json()
                req = data["KSampler"]["input"]["required"]
                return {
                    "samplers": req["sampler_name"][0],
                    "schedulers": req["scheduler"][0],
                }
        except Exception:
            pass
    return {
        "samplers": [
            # Euler family
            "euler", "euler_ancestral", "euler_cfg_pp", "euler_ancestral_cfg_pp",
            # Heun
            "heun", "heunpp2",
            # DPM-2
            "dpm_2", "dpm_2_ancestral",
            # LMS / DPM fast/adaptive
            "lms", "dpm_fast", "dpm_adaptive",
            # DPM++ family
            "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_sde_gpu",
            "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
            "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
            # DDPM / DDIM / LCM
            "ddpm", "ddim", "lcm",
            # UniPC
            "uni_pc", "uni_pc_bh2",
            # IPNDM
            "ipndm", "ipndm_v",
            # Misc newer samplers
            "deis", "res_multistep", "res_multistep_cfg_pp",
            "sa_solver", "er_sde", "gradient_estimation", "restart",
        ],
        "schedulers": [
            "normal", "karras", "exponential", "sgm_uniform",
            "simple", "ddim_uniform", "beta",
            "linear_quadratic", "kl_optimal", "ays", "gits",
        ],
    }


async def imagegen_models() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/CheckpointLoaderSimple")
                data = r.json()
                return data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        return []


async def imagegen_upscalers() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "Upscale", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/UpscaleModelLoader")
                data = r.json()
                return data["UpscaleModelLoader"]["input"]["required"]["model_name"][0]
    except Exception:
        return []


async def imagegen_ipadapter_models() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "IPAdapter", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                return []
    except Exception:
        return []


async def imagegen_refiners() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "Refiner", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                return []
    except Exception:
        return []


async def imagegen_progress() -> dict:
    t, u = _get_type(), _get_url()
    if t != "swarmui":
        return {"step": 0, "total": 0, "preview": ""}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{u}/API/GetCurrentStatus", json={"session_id": "ndworld"})
            data = r.json()
            return {
                "step": data.get("current_step", 0),
                "total": data.get("total_steps", 0),
                "preview": data.get("preview", ""),
            }
    except Exception:
        return {"step": 0, "total": 0, "preview": ""}


# ── SwarmUI model downloads ─────────────────────────────────────────────────
# Lets a GM pull a checkpoint/VAE/text-encoder/etc. straight into SwarmUI's
# own Models folder through nd-world's UI, same idea as download_whisper_model
# above — only reachable at all because docker-compose.yml/truenas-compose.yml
# mount the SAME host directory into both nd-world (here, at
# SWARMUI_MODELS_DIR) and the "swarmui" Compose service (at /SwarmUI/Models):
# nd-world writes a file, SwarmUI already sees it at the same relative path,
# no API call to SwarmUI itself involved. On an install where that shared
# mount doesn't exist (an externally-run SwarmUI/ComfyUI not managed by this
# repo's own Compose files), SWARMUI_MODELS_DIR just won't be a real,
# writable directory — downloads here fail the same way a bad path always
# would, rather than silently doing nothing.

SWARMUI_MODELS_DIR = Path(os.getenv("SWARMUI_MODELS_DIR", "/data/swarmui-models"))

# Subfolder names this app already asks SwarmUI's own API for elsewhere
# (imagegen_models/_loras/_upscalers/_ipadapter_models/_refiners above use
# these exact path= values against SwarmUI's ListModels endpoint) — real,
# code-verified values, not guesses. VAE/clip/ControlNet/Embedding are
# SwarmUI's own documented convention but aren't independently re-verified
# against a live instance here. Offered as suggestions only (a <datalist>,
# not an enum) — a GM running a SwarmUI version with different folder names
# isn't blocked by nd-world guessing wrong, since they can just type whatever
# their own installation actually uses.
SWARMUI_MODEL_FOLDER_SUGGESTIONS = [
    "", "LoRA", "VAE", "clip", "ControlNet", "Upscale", "IPAdapter", "Refiner", "Embedding",
]


def _swarmui_model_path(subfolder: str, filename: str) -> Path:
    """SWARMUI_MODELS_DIR / subfolder / filename, rejecting anything that
    could escape SWARMUI_MODELS_DIR (.., an absolute subfolder) — this
    becomes a filesystem write path built from GM-supplied free text, so
    path traversal has to be rejected outright rather than merely
    discouraged. Raises ValueError on anything unsafe."""
    rel = Path(subfolder or "", filename)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Invalid subfolder/filename")
    return SWARMUI_MODELS_DIR / rel


def list_downloaded_swarmui_models() -> list[dict]:
    """Every file nd-world can see under SWARMUI_MODELS_DIR, for the
    "already downloaded" panel — reads the shared volume directly rather
    than asking SwarmUI itself (that's imagegen_models() et al, which needs
    SwarmUI actually reachable), so this still works even while SwarmUI is
    down or still starting up."""
    if not SWARMUI_MODELS_DIR.is_dir():
        return []
    out = []
    for p in SWARMUI_MODELS_DIR.rglob("*"):
        if p.is_file() and not p.name.endswith(".part"):
            rel = p.relative_to(SWARMUI_MODELS_DIR)
            subfolder = str(rel.parent) if rel.parent != Path(".") else ""
            out.append({"subfolder": subfolder, "filename": rel.name, "bytes": p.stat().st_size})
    return out


async def download_swarmui_model(url: str, subfolder: str = "", filename: str = "") -> AsyncGenerator[dict, None]:
    """Stream a model file into SWARMUI_MODELS_DIR, yielding {"total":,
    "completed":} progress dicts as bytes arrive and a final {"status":
    "done", ...} or {"error": "..."} — same shape download_whisper_model
    above uses, so the client-side JS can reuse identical parsing.

    Unlike Whisper's curated known-model list, there's no one canonical
    trusted host for Stable-Diffusion-family checkpoints/VAEs/text-encoders
    — this is a free-text URL by design (HuggingFace, CivitAI, wherever the
    GM sources it from), same trust model download_whisper_model's own
    free-text custom-URL fallback already has.

    Written to a "<filename>.part" file and only renamed into place once
    fully downloaded, so an interrupted/failed download can never leave a
    corrupt file behind for SwarmUI to trip over."""
    url = (url or "").strip()
    if not url:
        yield {"error": "No URL given"}
        return
    if not filename:
        filename = Path(urlparse(url).path).name
    if not filename or "/" in filename or "\\" in filename:
        yield {"error": "Could not determine a safe filename from that URL — provide one explicitly"}
        return
    try:
        dest = _swarmui_model_path(subfolder, filename)
    except ValueError:
        yield {"error": "Invalid subfolder/filename"}
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        async with _httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    yield {"error": f"HTTP {resp.status_code} fetching model file"}
                    return
                total = int(resp.headers.get("content-length") or 0)
                completed = 0
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        f.write(chunk)
                        completed += len(chunk)
                        yield {"total": total, "completed": completed}
        tmp.replace(dest)
        refreshed = await swarmui_refresh_after_local_change()
        yield {"status": "done", "subfolder": subfolder, "filename": filename,
               "bytes": dest.stat().st_size, "model_list_refreshed": refreshed}
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        _log.warning("swarmui model download failed: %s: %s", type(exc).__name__, exc)
        yield {"error": f"{type(exc).__name__}: {exc}"}


def delete_downloaded_swarmui_model(subfolder: str, filename: str) -> bool:
    """Remove a previously-downloaded file — lets a GM free disk space
    without SSHing into wherever the shared volume actually lives. Returns
    False (not an error) if it doesn't exist, or the path is unsafe."""
    try:
        path = _swarmui_model_path(subfolder, filename)
    except ValueError:
        return False
    if not path.is_file():
        return False
    path.unlink()
    return True


async def imagegen_generate(prompt: str, negative: str, model: str,
                            width: int, height: int, steps: int,
                            cfg: float, seed: int, uploads_dir: Path,
                            sampler: str = "euler",
                            scheduler: str = "normal",
                            batch_size: int = 1,
                            loras: str = "",
                            lora_weights: str = "",
                            vae: str = "",
                            clip_skip: int = -1,
                            init_image: str = "",
                            init_strength: float = 0.6,
                            upscale_model: str = "",
                            upscale_factor: float = 1.0,
                            controlnet_image: str = "",
                            controlnet_strength: float = 0.8,
                            controlnet_preprocessor: str = "",
                            controlnet_model: str = "",
                            hiresfix: bool = False,
                            hireswidth: int = 0,
                            hiresheight: int = 0,
                            hiresdenoisestrength: float = 0.5,
                            hiressteps: int = 0,
                            refiner_model: str = "",
                            refiner_control: float = 0.8,
                            seamless_x: bool = False,
                            seamless_y: bool = False,
                            variation_seed: int = -1,
                            variation_strength: float = 0.0,
                            freeu_enabled: bool = False,
                            freeu_b1: float = 1.3,
                            freeu_b2: float = 1.4,
                            freeu_s1: float = 0.9,
                            freeu_s2: float = 0.2,
                            dynthresh_enabled: bool = False,
                            dynthresh_mimic_scale: float = 7.0,
                            dynthresh_percentile: float = 0.999,
                            cfg_rescale: float = 0.0,
                            ipadapter_image: str = "",
                            ipadapter_strength: float = 0.6,
                            ipadapter_model: str = "") -> list[str]:
    import copy, random, asyncio, base64 as _b64, uuid as _uuid
    t, u = _get_type(), _get_url()
    ai_img_dir = Path(uploads_dir) / "ai-images"
    ai_img_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []

    async with _httpx.AsyncClient(timeout=600) as c:
        if t == "swarmui":
            sr = await c.post(f"{u}/API/GetNewSession", json={})
            session_id = sr.json().get("session_id", "ndworld")
            model_name = model.rsplit(".", 1)[0] if model.endswith((".safetensors", ".ckpt", ".bin")) else model
            payload: dict = {
                "session_id": session_id,
                "images": max(1, min(batch_size, 8)),
                "prompt": prompt,
                "negativeprompt": negative,
                "model": model_name,
                "width": width,
                "height": height,
                "steps": steps,
                "cfgscale": cfg,
                "seed": seed if seed >= 0 else -1,
                "sampler": sampler or "euler",
                "scheduler": scheduler or "normal",
                "donotsave": False,
            }
            if loras:
                payload["loras"] = loras
                payload["loraweights"] = lora_weights or "1"
            if vae:
                payload["vae"] = vae
            if clip_skip > 0:
                payload["clipstop"] = -clip_skip
            if init_image:
                payload["initimage"] = init_image
                payload["initimagecreativity"] = init_strength
            if upscale_model:
                payload["upscalemodel"] = upscale_model
                payload["upscalemultiplier"] = upscale_factor
            if controlnet_image:
                payload["controlnetimage"] = controlnet_image
                payload["controlnetstrength"] = controlnet_strength
                if controlnet_preprocessor:
                    payload["controlnetpreprocessor"] = controlnet_preprocessor
                if controlnet_model:
                    payload["controlnetmodel"] = controlnet_model
            if hiresfix and hireswidth > 0:
                payload["hireswidth"] = hireswidth
                payload["hiresheight"] = hiresheight
                payload["hiresdenoisestrength"] = hiresdenoisestrength
                if hiressteps > 0:
                    payload["hiressteps"] = hiressteps
            if refiner_model:
                payload["refinermodel"] = refiner_model
                payload["refinercontrolpercentage"] = refiner_control
            if seamless_x:
                payload["seamlessx"] = True
            if seamless_y:
                payload["seamlessy"] = True
            if variation_seed >= 0 and variation_strength > 0:
                payload["variationseed"] = variation_seed
                payload["variationseedstrength"] = variation_strength
            if freeu_enabled:
                payload["freeu_b1"] = freeu_b1
                payload["freeu_b2"] = freeu_b2
                payload["freeu_s1"] = freeu_s1
                payload["freeu_s2"] = freeu_s2
            if dynthresh_enabled:
                payload["dynamicthresh_enabled"] = True
                payload["dynamicthresh_mimic_scale"] = dynthresh_mimic_scale
                payload["dynamicthresh_threshold_percentile"] = dynthresh_percentile
            if cfg_rescale > 0:
                payload["cfgrescale"] = cfg_rescale
            if ipadapter_image:
                payload["ipadapterimage"] = ipadapter_image
                payload["ipadapterstrength"] = ipadapter_strength
                if ipadapter_model:
                    payload["ipadaptermodel"] = ipadapter_model

            gr = await c.post(f"{u}/API/GenerateText2Image", json=payload)
            _log.info("SwarmUI generate status=%s body=%.400s", gr.status_code, gr.text)
            data = gr.json()
            images = data.get("images") or []
            if not images:
                err = data.get("error") or data.get("errorid") or data.get("message") or str(data)
                raise ValueError(f"SwarmUI returned no image: {err}")
            for img_raw in images:
                if img_raw.startswith("data:"):
                    img_raw = img_raw.split(",", 1)[1]
                fname = str(_uuid.uuid4()) + ".png"
                out_path = ai_img_dir / fname
                out_path.write_bytes(_b64.b64decode(img_raw))
                make_thumbnail(out_path)  # best-effort — the Image tab's history/starred grids fall back to this full PNG if it fails
                urls.append(f"/uploads/ai-images/{fname}")

        else:  # comfyui
            wf = copy.deepcopy(_COMFYUI_WORKFLOW)
            wf["1"]["inputs"]["ckpt_name"] = model
            wf["2"]["inputs"]["text"] = prompt
            wf["3"]["inputs"]["text"] = negative
            wf["4"]["inputs"].update({"width": width, "height": height, "batch_size": max(1, min(batch_size, 8))})
            wf["5"]["inputs"].update({
                "steps": steps, "cfg": cfg,
                "seed": seed if seed >= 0 else random.randint(0, 2**32),
                "sampler_name": sampler or "euler",
                "scheduler": scheduler or "normal",
            })
            if upscale_model:
                wf["8"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": upscale_model}}
                wf["9"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["8", 0], "image": ["6", 0]}}
                wf["7"]["inputs"]["images"] = ["9", 0]
            pr = await c.post(f"{u}/prompt", json={"prompt": wf})
            pid = pr.json()["prompt_id"]
            for _ in range(120):
                await asyncio.sleep(1)
                hr = await c.get(f"{u}/history/{pid}")
                hist = hr.json().get(pid, {})
                if hist.get("outputs"):
                    imgs = list(hist["outputs"].values())[0].get("images", [])
                    for img_info in imgs:
                        ir = await c.get(f"{u}/view",
                                         params={"filename": img_info["filename"],
                                                 "subfolder": img_info.get("subfolder", ""),
                                                 "type": "output"})
                        fname = str(_uuid.uuid4()) + ".png"
                        out_path = ai_img_dir / fname
                        out_path.write_bytes(ir.content)
                        make_thumbnail(out_path)
                        urls.append(f"/uploads/ai-images/{fname}")
                    break

    return urls
