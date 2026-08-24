import os
import json as _json
import logging
from pathlib import Path
from collections.abc import AsyncGenerator
import ollama as _ollama
import httpx as _httpx

_log = logging.getLogger("nd.ai")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

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


def _chat_kwargs(extra_options: dict = None) -> dict:
    """Extra kwargs (options=, keep_alive=) to splat into every .chat() call
    below — built fresh each call so a runtime settings change (no server
    restart needed) takes effect on the very next request. `extra_options`
    (a per-request override — see a chat preset's options, app/routers/ai.py)
    is layered OVER the instance-wide AppSettings defaults, not replacing
    them: an unset key still falls back to whatever Settings > System
    configured, so a preset only has to specify what it wants to differ."""
    kwargs = {}
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
# configured from the same Models tab.
DEFAULT_SURFACES = ("chat", "ask_ai", "image")


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


async def generate_chat(messages: list[dict], system: str = "", model: str = "", options: dict = None) -> str:
    m = model or effective_ollama_model()
    _log.info("generate_chat model=%s msgs=%d", m, len(messages))
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        resp = await _client().chat(model=m, messages=full, **_chat_kwargs(options))
        content = resp.message.content
        return content if content else "[empty response]"
    except _ollama.ResponseError as exc:
        _log.error("generate_chat Ollama error: %s %s", exc.status_code, exc.error)
        return f"[AI error: Ollama {exc.status_code}: {exc.error}]"
    except Exception as exc:
        _log.error("generate_chat unavailable: %s: %s", type(exc).__name__, exc)
        return f"[AI unavailable: {type(exc).__name__}: {exc}]"


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


async def expand_recap_notes(notes: str, model: str = "") -> str:
    """Expand terse GM notes into a polished narrative session recap. Unlike
    parse_facts_from_recap, this doesn't need JSON-schema-constrained output
    (free-text prose, not discrete structured facts) so it just wraps
    generate_chat directly — same "[AI error: ...]"/"[AI unavailable: ...]"
    inline-string failure convention as every other chat call in this module,
    which the caller can display as-is instead of catching an exception."""
    return await generate_chat([{"role": "user", "content": notes}], system=_EXPAND_NOTES_SYSTEM, model=model)


_SUMMARIZE_FACTS_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is a list of discrete facts logged "
    "for one session. Weave them into a short, readable narrative recap in flowing prose — a "
    "few short paragraphs, past tense, third person. Use only the facts given; don't invent "
    "new details, and don't drop any of them. Markdown is fine for light formatting but keep it "
    "simple. Respond with the recap text only, no preamble or commentary."
)


async def summarize_session_from_facts(facts: list[str], model: str = "") -> str:
    """Weave a list of discrete session facts (see the Facts feature, which
    logs these per-session) into a readable narrative recap."""
    if not facts:
        return ""
    bullet_list = "\n".join(f"- {f}" for f in facts)
    return await generate_chat([{"role": "user", "content": bullet_list}], system=_SUMMARIZE_FACTS_SYSTEM, model=model)


_CONDENSE_RECAP_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Condense the following session recap into a "
    "short, tight summary — a few sentences at most, hitting only the key beats a player would "
    "need to remember before the next session. Keep it in flowing prose. Don't invent details "
    "that aren't in the original. Respond with the condensed recap only, no preamble or "
    "commentary."
)


async def condense_recap(recap: str, model: str = "") -> str:
    """Condense an existing recap into a tighter 'previously on...' summary."""
    return await generate_chat([{"role": "user", "content": recap}], system=_CONDENSE_RECAP_SYSTEM, model=model)


_SUMMARIZE_TRANSCRIPT_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is a raw Whisper transcript of an "
    "actual-play session recording — expect filler words, misheard names, and no punctuation "
    "structure. Turn it into a short, readable narrative recap in flowing prose — a few "
    "paragraphs, past tense, third person. Use your judgment to skip out-of-character chatter, "
    "rules discussion, and filler, keeping only what happened in the story. Don't invent details "
    "that aren't in the transcript. Respond with the recap text only, no preamble or commentary."
)

_SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is ONE PART of a longer raw Whisper "
    "transcript of an actual-play session recording — expect filler words, misheard names, no "
    "punctuation structure, and this excerpt starting and ending mid-scene. Extract what "
    "happened in this part as a terse, factual list of events (who did what, what was learned, "
    "what changed) — not polished prose yet, since this will be combined with summaries of the "
    "other parts afterward. Skip out-of-character chatter, rules discussion, and filler. Don't "
    "invent details that aren't in the text. Respond with the extracted events only, no preamble."
)

_SUMMARIZE_TRANSCRIPT_REDUCE_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below are chronological, terse event "
    "summaries of consecutive parts of one session — turn them into a single, short, readable "
    "narrative recap in flowing prose (a few paragraphs, past tense, third person), preserving "
    "every concrete event/name/detail from the parts. Don't invent anything not implied by the "
    "parts. Respond with the recap text only, no preamble or commentary."
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


def _transcript_chunk_char_budget() -> int:
    ctx_tokens = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    input_tokens = max(500, ctx_tokens - _CHUNK_RESERVED_TOKENS)
    return input_tokens * _CHARS_PER_TOKEN_ESTIMATE


def _split_transcript_into_chunks(transcript: str, chunk_chars: int) -> list[str]:
    """Split on a paragraph or sentence boundary near the end of each window
    where one exists, so a chunk doesn't get cut mid-sentence — falls back
    to a hard cut at chunk_chars if no such boundary is found late enough
    in the window to still make meaningful progress."""
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


async def summarize_transcript(transcript: str, model: str = "") -> str:
    """Turn a raw Whisper transcript (see transcribe_audio) of a session
    recording into a narrative recap. Transcripts that fit in one context
    window go through a single generate_chat call, same as before; longer
    ones are map-reduced — summarized in chunks, then the chunk summaries
    combined into one final recap — see _transcript_chunk_char_budget."""
    transcript = (transcript or "").strip()
    if not transcript:
        return ""
    chunks = _split_transcript_into_chunks(transcript, _transcript_chunk_char_budget())
    if len(chunks) <= 1:
        return await generate_chat([{"role": "user", "content": transcript}], system=_SUMMARIZE_TRANSCRIPT_SYSTEM, model=model)

    _log.info("summarize_transcript: chunking into %d part(s) (%d chars total)", len(chunks), len(transcript))
    part_summaries = []
    for i, chunk in enumerate(chunks):
        part = await generate_chat([{"role": "user", "content": chunk}], system=_SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM, model=model)
        if part.startswith("[AI "):
            return part  # propagate the failure rather than weaving an error string into the recap
        part_summaries.append(f"Part {i + 1}:\n{part}")
    combined = "\n\n".join(part_summaries)
    return await generate_chat([{"role": "user", "content": combined}], system=_SUMMARIZE_TRANSCRIPT_REDUCE_SYSTEM, model=model)


async def status() -> dict:
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        return {"status": "ok", "model": effective_ollama_model(), "loaded_models": models}
    except Exception:
        return {"status": "unavailable", "model": effective_ollama_model()}


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


async def transcribe_audio(path: Path) -> str:
    """POST an audio file to whisper.cpp's /inference endpoint and return the
    transcript, or "" if Whisper isn't configured or the request fails for
    any reason (network, model still loading, unreadable audio, ...) — a
    failed transcription should never block the rest of the attachment
    upload, just mean this one attachment stays without transcript text."""
    url = effective_whisper_url()
    if not url:
        return ""
    try:
        async with _httpx.AsyncClient(timeout=WHISPER_TIMEOUT_SECONDS) as c:
            with path.open("rb") as f:
                r = await c.post(
                    f"{url}/inference",
                    files={"file": (path.name, f, "application/octet-stream")},
                    data={"response_format": "json"},
                )
            if r.status_code != 200:
                _log.warning("whisper transcription failed: HTTP %s: %s", r.status_code, r.text[:300])
                return ""
            return (r.json().get("text") or "").strip()
    except Exception as exc:
        _log.warning("whisper transcription unavailable: %s: %s", type(exc).__name__, exc)
        return ""


# ── Whisper model download ──────────────────────────────────────────────────
# nd-world's own container and the "whisper" Compose service both mount the
# same host directory (see docker-compose.yml/truenas-compose.yml), just at
# different internal paths — the same pattern SWARMUI_AC_DIR already uses to
# share the tag-autocomplete folder between `world` and `swarmui`. That lets
# a GM download a model file through nd-world instead of SSHing into the
# host, without nd-world needing any access to the whisper.cpp container
# itself (which has no download-a-model API of its own, unlike Ollama).

WHISPER_MODELS_DIR = Path(os.getenv("WHISPER_MODELS_DIR", "/data/whisper-models"))
# Must match the "-m /models/<this>" filename the "whisper" Compose service
# loads by default (its WHISPER_MODEL_FILE env var) — downloading under any
# other name would still need a manual step to line the two up.
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


def whisper_model_status() -> dict:
    """Whether a model file nd-world can see is already sitting in the
    shared volume — checked from nd-world's own side (WHISPER_MODELS_DIR),
    not by asking the whisper.cpp server itself (that's whisper_status()),
    since the file needs to exist before the server can even be pointed at
    it. Doesn't mean the *running* server has loaded it yet — that only
    happens on container start/restart (see download_whisper_model).

    "downloaded"/"filename"/"bytes" describe the *active* model — the one
    the "whisper" Compose service is actually configured to load (i.e.
    WHISPER_MODEL_FILENAME, its own WHISPER_MODEL_FILE env var mirrored
    here) — kept as top-level keys for whatever already reads this shape.
    "models" is the fuller picture: every known model's own download state
    and whether it's the currently-active one, so a GM can download several
    without any of them clobbering another."""
    active = WHISPER_MODELS_DIR / WHISPER_MODEL_FILENAME
    models = []
    for m in WHISPER_KNOWN_MODELS:
        p = WHISPER_MODELS_DIR / m["filename"]
        downloaded = p.is_file()
        models.append({
            **m,
            "downloaded": downloaded,
            "bytes": p.stat().st_size if downloaded else 0,
            "active": m["filename"] == WHISPER_MODEL_FILENAME,
        })
    return {
        "downloaded": active.is_file(),
        "filename": WHISPER_MODEL_FILENAME,
        "bytes": active.stat().st_size if active.is_file() else 0,
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
    blank) downloads to WHISPER_MODEL_FILENAME, the currently-active slot.

    Written to a "<filename>.part" file and only renamed into place once
    fully downloaded, so an interrupted/failed download can never leave a
    corrupt file behind for the whisper.cpp server to trip over — which
    matters more here than most partial-download cases: whisper.cpp's own
    /load endpoint calls exit(1) (killing the whole server process) if the
    model file it's given fails to parse, rather than returning an error.
    This app deliberately never calls that endpoint itself for exactly that
    reason (see docs/DEPLOYMENT.md) — a downloaded model only takes effect
    on the *next* container start/restart of the "whisper" service, and
    only if WHISPER_MODEL_FILE is (re)pointed at it — nd-world has no way
    to change a sibling container's env vars or restart it itself, so
    switching which downloaded model is active is still a manual step the
    GM does once, same as installing any other model."""
    if filename:
        if filename not in _WHISPER_KNOWN_FILENAMES:
            yield {"error": f"Unknown model filename: {filename!r}"}
            return
        fetch_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}"
        target_filename = filename
    else:
        fetch_url = (url or "").strip() or DEFAULT_WHISPER_MODEL_URL
        target_filename = WHISPER_MODEL_FILENAME
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
                (ai_img_dir / fname).write_bytes(_b64.b64decode(img_raw))
                urls.append(f"/uploads/ai-images/{fname}")

        else:  # comfyui
            wf = copy.deepcopy(_COMFYUI_WORKFLOW)
            wf["1"]["inputs"]["ckpt_name"] = model
            wf["2"]["inputs"]["text"] = prompt
            wf["3"]["inputs"]["text"] = negative
            wf["4"]["inputs"].update({"width": width, "height": height})
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
                        (ai_img_dir / fname).write_bytes(ir.content)
                        urls.append(f"/uploads/ai-images/{fname}")
                    break

    return urls
