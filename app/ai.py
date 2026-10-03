import asyncio
import base64 as _b64
import os
import json as _json
import logging
import re
import struct as _struct
import time
from pathlib import Path
from urllib.parse import urlparse
from collections.abc import AsyncGenerator
import ollama as _ollama
import httpx as _httpx
import websockets as _websockets

from .imaging import make_thumbnail
from .job_shutdown import JobInterrupted

_log = logging.getLogger("nd.ai")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

# Unsloth Studio — the single AI backend for chat (and, per Phase 3, image
# generation) once the migration cuts over; see docs/UNSLOTH_PHASE0_FINDINGS.md
# for the verified /v1 API surface. When UNSLOTH_API_KEY is set, every LLM call goes through
# app.llm_client's OpenAI-dialect shim; when unset, nd-world keeps talking to
# the legacy Ollama backend below (kept until the Phase 7 cutover, so this
# dual-mode bridge exists only for the transition window, not as a permanent
# two-backend design).
UNSLOTH_URL = os.getenv("UNSLOTH_URL", "").rstrip("/")
UNSLOTH_API_KEY = os.getenv("UNSLOTH_API_KEY", "")
UNSLOTH_MODEL = os.getenv("UNSLOTH_MODEL", "")
# Image-generation model downloaded in Studio's Model Hub (e.g. z-image-turbo
# or Krea 2 Turbo). When this AND the API key are set, Unsloth also owns
# image generation (migration plan §7) — the legacy IMAGEGEN_TYPE backend
# below only runs while the swarmui/comfyui profiles are still in use.
UNSLOTH_IMAGE_MODEL = os.getenv("UNSLOTH_IMAGE_MODEL", "")

# The context window the chat model was loaded with in Studio. Unlike
# Ollama's per-request num_ctx, llama.cpp fixes context at model load, so
# this is now the single source the chunk-sizing math budgets against
# (migration plan §6.3) — it MUST match the load-time setting in Studio or
# chunks will mis-size. Clamped to MAX_AUTO_NUM_CTX like any computed ctx.
LLM_CONTEXT_TOKENS = max(1024, int(os.getenv("LLM_CONTEXT_TOKENS", "16384")))

# NOTE: since app/ai_queue.py, every background AI task also takes its turn in ONE global first-come queue (one AI task
# at a time across STT, LLM, TTS and image work); the per-backend limits below still bound the calls INSIDE a task and
# anything that does not go through that queue.
# Concurrency limits for BACKGROUND-JOB work only (app/audio_jobs.py,
# app/chat_jobs.py) — not the interactive chat/ask-AI/condense routes a GM
# is actively waiting on, which should never queue behind a background job.
# Without these, two session-recap jobs queued together interleave speech-to-text
# chunks (or Ollama calls) against each other on the same backend, roughly
# doubling wall time for both and thrashing whatever's resident in VRAM.
# Held for the FULL duration of one transcribe_audio/summarize_transcript/
# condense_recap/generate_chat call (including that call's own internal
# per-chunk loop), not just one HTTP request, so a job's chunks always run
# back-to-back rather than interleaved with another job's. Env-tunable in
# case a beefier host can genuinely run more than one at a time.
# (WHISPER_JOB_CONCURRENCY is still honoured as the old name of STT_JOB_CONCURRENCY.)
STT_JOB_CONCURRENCY = max(1, int(os.getenv("STT_JOB_CONCURRENCY") or os.getenv("WHISPER_JOB_CONCURRENCY") or "1"))
OLLAMA_JOB_CONCURRENCY = max(1, int(os.getenv("OLLAMA_JOB_CONCURRENCY", "1")))
# Unlike speech-to-text/Ollama above, SwarmUI/ComfyUI queue generations internally
# on their own end — this exists to keep app.image_jobs' queued jobs from
# racing a concurrent direct /api/ai/imagegen/generate call (or each
# other) at the httpx-client/timeout layer, not to protect the GPU itself.
IMAGEGEN_JOB_CONCURRENCY = max(1, int(os.getenv("IMAGEGEN_JOB_CONCURRENCY", "1")))
stt_job_semaphore = asyncio.Semaphore(STT_JOB_CONCURRENCY)
ollama_job_semaphore = asyncio.Semaphore(OLLAMA_JOB_CONCURRENCY)
imagegen_job_semaphore = asyncio.Semaphore(IMAGEGEN_JOB_CONCURRENCY)

# Runtime overrides (set from AppSettings via POST /settings/system, without
# needing a restart — see main.py's _refresh_settings_overrides()). Blank means
# "use the env-var default above."
_llm_url_override: str = ""
_llm_model_override: str = ""
_llm_api_key_override: str = ""
_llm_context_tokens_override: int = 0


def set_llm_override(url: str = "", model: str = "", api_key: str = "", context_tokens: int = 0) -> None:
    global _llm_url_override, _llm_model_override, _llm_api_key_override, _llm_context_tokens_override
    global _status_cache, _status_inflight
    _llm_url_override = (url or "").rstrip("/")
    _llm_model_override = model or ""
    _llm_api_key_override = api_key or ""
    _llm_context_tokens_override = max(0, int(context_tokens or 0))
    # The cached /api/ai/status answer describes the OLD backend; leaving it
    # for up to a poll window made a freshly saved Studio key look "offline".
    _status_cache = None
    _status_inflight = None


def effective_llm_api_key() -> str:
    """The Unsloth Studio API key (Bearer on every /v1 call). Empty = the
    legacy Ollama backend is in use — this is the ONE switch that decides
    which dialect _client() speaks, so gating here keeps every other
    effective_* helper honest."""
    return _llm_api_key_override or UNSLOTH_API_KEY


def key_hint(key: str | None) -> str:
    """'…ab78' for a secret — its last 4 characters and nothing else (empty for no
    key; a very short value shows no characters at all). Enough to recognise a key
    against Studio's key list, never enough to use it."""
    key = (key or "").strip()
    if not key:
        return ""
    return "…" + key[-4:] if len(key) >= 12 else "…"


def llm_key_sources() -> dict:
    """Which Studio API key is in effect and where it came from. A key saved in
    Settings wins over UNSLOTH_API_KEY from the environment (see
    effective_llm_api_key), so a fresh key put in .env is ignored while an old one
    is still saved in Settings — worth showing, not guessing at."""
    saved, env = _llm_api_key_override or "", UNSLOTH_API_KEY or ""
    in_use = "settings" if saved else ("env" if env else "none")
    return {"in_use": in_use, "saved_hint": key_hint(saved), "env_hint": key_hint(env)}


def effective_llm_url() -> str:
    if _llm_url_override:
        return _llm_url_override
    if effective_llm_api_key():
        # A Studio key must NEVER be sent to the Ollama address (it would be
        # a Bearer header to the wrong service, and Ollama's address is not
        # where Studio lives). With no URL configured, use the compose
        # service name the shipped docker-compose.yml gives Studio.
        return UNSLOTH_URL or "http://unsloth:8000"
    return OLLAMA_URL


def effective_llm_model() -> str:
    if _llm_model_override:
        return _llm_model_override
    if effective_llm_api_key() and UNSLOTH_MODEL:
        return UNSLOTH_MODEL
    return OLLAMA_MODEL


def llm_context_tokens() -> int:
    """The configured load-time context window (see LLM_CONTEXT_TOKENS
    above) — runtime-overridable from Settings like the url/model."""
    return _llm_context_tokens_override or LLM_CONTEXT_TOKENS


def llm_backend_name() -> str:
    """"unsloth" or "ollama" — used in error/sentinel strings and status
    payloads so a mixed-deployment GM can tell which backend answered."""
    return "unsloth" if effective_llm_api_key() else "ollama"


def _backend_label() -> str:
    return "Unsloth" if effective_llm_api_key() else "Ollama"


# Legacy names, kept as one-release shims per migration plan §6.1 — every
# caller still saying "ollama" keeps working until the Phase 4 wiring
# renames them for real.
set_ollama_override = set_llm_override


def effective_ollama_url() -> str:
    return effective_llm_url()


def effective_ollama_model() -> str:
    return effective_llm_model()


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
    # A GM may have just ticked (or unticked) a model's "thinking" checkbox
    # (see _model_override_thinks below) — drop any cached capability so the
    # next call re-checks instead of keeping a stale answer until restart,
    # same "takes effect on the very next request" promise every other
    # setting on this page already makes. The prompt-token fallback set
    # clears with it: an unticked override must stop the <|think|> injection
    # immediately, and a re-ticked one gets a fresh real think=true probe.
    _model_capabilities_cache.clear()
    _prompt_token_thinking_models.clear()


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

# Models where the LAST actual think=true request to Ollama came back with
# "does not support thinking" — meaning some fallback (KNOWN_MODELS, or a
# GM's own per-model override checkbox — see _known_model_thinks/
# _model_override_thinks) told _model_supports_thinking to trust the model,
# but Ollama's real runtime behavior disagreed. Surfaced as a warning next
# to that model's per-model override in Settings > System (see main.py's
# _settings_context) so a GM who ticked the box for a model that turns out
# not to actually think can see their override didn't work, instead of only
# finding out from a raw chat error buried in the conversation. In-memory
# only, same as _model_capabilities_cache — a GM rediscovers this quickly
# enough on next real use that persisting it isn't worth a schema change.
#
# A confirmed rejection ALSO poisons _model_capabilities_cache (strips the
# "thinking" tag) so _model_supports_thinking stops sending think=true for
# this model at all — see _record_thinking_result below. That's the active
# gate (short-circuits every further call, including each chunk of a
# chunked summarize); this set stays purely advisory. Both share the same
# GM-visible reset points — set_ollama_generation_overrides clears the
# capability cache on every Settings save, and a restart clears both — so
# a GM who fixes the model (or just wants to re-probe it) gets a fresh
# real think=true attempt next time, whose success clears this set again.
_model_thinking_failures: set[str] = set()


# Models where a think=true rejection already happened AND nd-world's own
# records (KNOWN_MODELS, or a GM's per-model override checkbox) still say the
# model thinks — meaning the rejection is Ollama's missing capability tag on
# hf.co-imported GGUFs (ollama#16936: the import path never reports
# "thinking", so /api/chat 400s an explicit think=true) rather than a model
# that genuinely can't reason. Future think=True requests for these are sent
# as think=False with the gemma-4-style <|think|> prompt token prepended to
# the system message instead (see _messages_with_prompt_think_token) — the
# chat template's documented manual trigger, which the model honors even
# though Ollama won't gate it via the API flag. In-memory, cleared on every
# Settings save (same reset point as the capability cache) and on restart;
# a successful real think=true call (e.g. after Ollama was updated to tag
# the model) also retires it.
_prompt_token_thinking_models: set[str] = set()

# Gemma 4's documented manual thinking trigger — the chat template injects
# this token at the start of the system turn when thinking is enabled, and
# it works just as well supplied as literal system-message text, which is
# what makes the fallback above possible for untagged imports.
_PROMPT_THINK_TOKEN = "<|think|>"


def _messages_with_prompt_think_token(full: list[dict]) -> list[dict]:
    """Return `full` (generate_chat/stream_chat's [system?]+messages list)
    as a copy with the <|think|> trigger prepended to the first system
    message's content — inserting a system message if none was passed — so
    the model reasons even though Ollama refused the think=true flag."""
    out = list(full)
    if out and out[0].get("role") == "system":
        out[0] = {**out[0], "content": _PROMPT_THINK_TOKEN + (out[0].get("content") or "")}
    else:
        out.insert(0, {"role": "system", "content": _PROMPT_THINK_TOKEN})
    return out


_THINK_TAG_OPEN = "<think>"
_THINK_TAG_CLOSE = "</think>"


class _InlineThinkSplitter:
    """Incrementally splits a raw token stream into ("thinking", text)/
    ("content", text) pieces around a literal <think>...</think> block.

    A real gap the <|think|> prompt-token fallback (see
    _messages_with_prompt_think_token) otherwise has: that retry
    deliberately sends think=False to Ollama — sending think=true again
    would just repeat the identical rejection that triggered the fallback
    — so Ollama never populates the native message.thinking field for any
    of it. But a model reasoning because of the injected token still
    typically wraps that reasoning in literal <think>...</think> markup in
    its own raw generation (the same convention Ollama's OWN think=true
    splitting relies on for any compatible model — it isn't inventing
    separate channels, it's parsing this exact tag out of the model's
    output for you). Without this, that whole block — tags and all —
    lands in the visible answer verbatim, and stream_chat's separate
    "thinking" pieces (see chunk.message.thinking below) never fire at
    all for a model on this fallback, which is exactly the reported "the
    imported Gemma 4 GGUF never shows a reasoning trace" bug.

    Wired in unconditionally in stream_chat whenever emit_thinking is
    True (not just for the fallback case) — harmless for a model whose
    reasoning Ollama already separated via the native field, since its
    `content` stream is already clean and has no <think> tag left to
    find; this only ever does something for a model whose raw output
    still contains one.

    Buffers a short tail of text whenever it could be the START of the
    tag currently being looked for, so a tag split across two separate
    stream chunks (Ollama's own chunking is arbitrary token-by-token, not
    aligned to any of this) is still recognized correctly rather than
    slipping through as literal visible text."""

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, token: str) -> list[tuple[str, str]]:
        self._buf += token
        out = []
        while self._buf:
            tag = _THINK_TAG_CLOSE if self._in_think else _THINK_TAG_OPEN
            idx = self._buf.find(tag)
            if idx == -1:
                keep = 0
                for k in range(min(len(tag) - 1, len(self._buf)), 0, -1):
                    if tag.startswith(self._buf[-k:]):
                        keep = k
                        break
                if keep:
                    emit, self._buf = self._buf[:-keep], self._buf[-keep:]
                else:
                    emit, self._buf = self._buf, ""
                if emit:
                    out.append(("thinking" if self._in_think else "content", emit))
                break
            before, after = self._buf[:idx], self._buf[idx + len(tag):]
            if before:
                out.append(("thinking" if self._in_think else "content", before))
            self._in_think = not self._in_think
            self._buf = after
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Call once the stream ends — whatever's still held back (either
        genuinely trailing text, or a suspected tag-start that never
        completed and so was never really a tag) is real content/thinking
        and must not be silently dropped."""
        if not self._buf:
            return []
        piece = [("thinking" if self._in_think else "content", self._buf)]
        self._buf = ""
        return piece


_THINK_BLOCK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)


def _strip_inline_think_tags(content: str) -> tuple[str, int]:
    """generate_chat's non-streaming counterpart to _InlineThinkSplitter
    (see its own docstring for the full "why") — generate_chat has no
    typed-piece return the way stream_chat does, so there's nothing
    sensible to DO with reasoning text here except discard it, the same
    way generate_chat already silently discards reasoning delivered via
    Ollama's own native `message.thinking` field on every call. Without
    this, a model on the <|think|> prompt-token fallback would leave its
    raw <think>...</think> block sitting in the returned string verbatim
    — and generate_chat's callers are exactly the session-recap-assist
    family (expand_recap_notes/condense_recap/summarize_transcript/
    summarize_session_from_facts), which SAVE that return value as actual
    session content, not just display it live. Returns (stripped_content,
    removed_char_count) — the count feeds the empty-response diagnostic
    below so a response that was ENTIRELY a think block (nothing left
    after stripping) is still correctly reported as hidden reasoning
    rather than a silent, unexplained empty string."""
    stripped = _THINK_BLOCK_RE.sub('', content).strip()
    return stripped, len(content) - len(stripped)


def model_rejected_thinking(model: str = "") -> bool:
    """True if `model` (default: effective_ollama_model()) is the subject
    of a currently-live thinking rejection — same resolution as
    generate_chat/stream_chat's own `m`, so a caller outside this module
    (audio_jobs.py, to decide whether to label a job's result) can ask the
    identical question those functions already answered internally."""
    return (model or effective_ollama_model()) in _model_thinking_failures


def model_thinks_via_prompt_token(model: str = "") -> bool:
    """True if `model` (default: effective_ollama_model()) is currently on
    the <|think|> prompt-token fallback — Ollama rejected its think=true
    (see model_rejected_thinking above) but nd-world's own records vouch
    for the model's thinking, so every think=True request to it is being
    served reasoning via the chat-template token instead (see
    _prompt_token_thinking_models). Lets callers that LABEL results based
    on model_rejected_thinking (audio_jobs.py's job rows) distinguish
    "reasoning genuinely didn't run" from "reasoning ran, just not through
    the API flag" — the GM-facing guidance differs in exactly one way:
    only the first case should tell the GM to untick the override."""
    return (model or effective_ollama_model()) in _prompt_token_thinking_models


def _record_thinking_result(model: str, think: bool, failed: bool) -> None:
    """Called from generate_chat/stream_chat's own try/except with the
    EFFECTIVE think value actually sent to Ollama (after _chat_kwargs may
    have downgraded it) — see their call sites for exactly when. Only
    meaningful when thinking was actually requested; a plain think=False
    call proves nothing about whether the model can think, so it's a
    no-op either way. Using the effective value (not the caller's
    requested one) matters once a rejection has poisoned the capability
    cache below: a later think=True *request* against that poisoned model
    gets silently downgraded to think=False by _chat_kwargs, and a
    request that was never actually sent as think=true must not be
    treated as a successful thinking call that clears the failure."""
    if not think:
        return
    if failed:
        _model_thinking_failures.add(model)
        # Stop sending think=true to this model at all until a Settings
        # save or restart clears the cache (see _model_supports_thinking,
        # which consults this cache before ever trying KNOWN_MODELS/the
        # override) — otherwise every further call (each chunk of a
        # chunked summarize, for instance) repeats the same failing
        # round-trip to Ollama.
        _model_capabilities_cache[model] = [
            c for c in _model_capabilities_cache.get(model, []) if c != "thinking"
        ]
    else:
        # A successful think=true call proves the model genuinely handles
        # it — clear any earlier failure (a GM fixed it, e.g. by properly
        # registering the model with Ollama, or it was transient). That
        # also retires the prompt-token fallback: if Ollama now accepts
        # think=true, the API flag is the cleaner mechanism again.
        _model_thinking_failures.discard(model)
        _prompt_token_thinking_models.discard(model)


def _known_model_thinks(model: str) -> bool:
    """True if `model` appears in KNOWN_MODELS with "thinking": True.

    A curated, code-level fallback for models we ship pre-registered
    (currently just the Unsloth IQ4_NL quantisation) — see
    _model_override_thinks just below for the GM-editable equivalent that
    covers everything else without needing a code change."""
    return any(m.get("id") == model and m.get("thinking") for m in _builtin_models())


def _model_override_thinks(model: str) -> bool:
    """True if a GM has ticked "This model supports thinking" for `model`
    in Settings > System's per-model overrides (Settings.html's pmo-thinking
    checkbox, saved into AppSettings.ollama_model_overrides_json).

    This is the general-purpose escape hatch _known_model_thinks can't be:
    KNOWN_MODELS is a short, curated, code-level list that needs a commit
    and a deploy to extend, but any model a GM pulls via the Hugging Face
    search/upload feature or uploads straight from their PC is, by
    definition, something this codebase has never seen before and can't
    have pre-registered. Ollama's own /api/show won't tag a raw GGUF as
    thinking-capable either way (see _model_supports_thinking) — so
    without this, thinking mode would only ever work for the one model
    KNOWN_MODELS happens to list. A GM who knows their own model's
    behavior can just tick the box instead of waiting on a code change."""
    return bool(_ollama_model_overrides.get(model, {}).get("thinking"))


async def _model_supports_thinking(model: str) -> bool:
    """Whether `model` supports thinking mode — checked via Ollama's
    /api/show capabilities tag first, then by KNOWN_MODELS' and the
    per-model override's own "thinking": True flags as fallbacks for
    models Ollama won't tag automatically.

    A model pulled as a raw GGUF — including via this app's own Hugging
    Face search/upload features — doesn't reliably carry the "thinking"
    capability tag the way an official ollama.com library model's
    Modelfile does, and Ollama's /api/chat rejects think=True outright
    ("<model> does not support thinking", HTTP 400) for anything not
    tagged, rather than silently ignoring the request. See _chat_kwargs
    below for where this gates a requested think=True back down to False
    instead of letting that 400 reach the user as a raw error.

    Both fallbacks are authoritative when set — KNOWN_MODELS for the
    handful of models this codebase ships pre-registered (e.g. the
    Unsloth IQ4_NL quantisation), the per-model override for anything a
    GM has confirmed themselves (see _model_override_thinks) — either one
    means we trust the model handles thinking tokens correctly even
    though Ollama's own tag won't be set.

    Cached per-model for the life of the process — capabilities are static
    for an already-pulled model, and a restart (e.g. after a Watchtower
    deploy), or a settings save (see set_ollama_generation_overrides),
    naturally clears this if a model is ever replaced or an override
    changes. Only called when think is actually truthy (see below), so
    the common think=False path never pays for the extra /api/show round
    trip at all."""
    # Unsloth backend parity: Studio models think BY DEFAULT (Gemma-4 emits
    # reasoning_content un­prompted; enable_thinking is a per-request flag,
    # findings I-4) and the shim has no /api/show capability probe to query —
    # so trust every Unsloth model with thinking rather than downgrading
    # think=True to False for any hub model beyond the one pre-registered
    # here (which silently hid the reasoning trace from every other model).
    # A model that genuinely rejects the flag surfaces its own error, which
    # callers already show verbatim.
    if effective_llm_api_key():
        return True
    if model in _model_capabilities_cache:
        return "thinking" in _model_capabilities_cache[model]
    caps: list[str] = []
    try:
        resp = await _client().show(model)
        caps = list(resp.capabilities or [])
    except Exception:
        pass  # fail soft — check the fallbacks below before giving up
    if "thinking" not in caps and (_known_model_thinks(model) or _model_override_thinks(model)):
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


def _is_thinking_rejection(exc: Exception) -> bool:
    """True if `exc` is an upfront backend rejection of a think=true request
    (migration plan §6.4). Ollama's exact wording is the historical trigger;
    llama.cpp/Unsloth says something else entirely, so under the Unsloth
    backend ANY 400 on a request that carried the thinking kwarg means the
    same thing. Only the status code is trusted there — never the message —
    and the retry path in generate_chat/stream_chat is guarded by think=
    False on the recursion, so a same-cause 400 during the retry surfaces
    as a normal sentinel instead of looping."""
    if "does not support thinking" in (getattr(exc, "error", None) or ""):
        return True
    if not (bool(effective_llm_api_key()) and getattr(exc, "status_code", None) == 400):
        return False
    # Under Unsloth, a 400 that NAMES a non-thinking cause must not be
    # swallowed by the broad any-400 rule (audit 2026-09-30, chat finding
    # 3): a model-not-found or prompt-too-large 400 used to be recorded as
    # a thinking failure (poisoning per-model bookkeeping), retried once at
    # double cost, and for vouched models re-sent with a literal <|think|>
    # token appended. Markers are unambiguous non-thinking causes only —
    # bare "model" is deliberately NOT one, since a thinking rejection can
    # legitimately name the model too.
    msg = (getattr(exc, "error", None) or "").lower()
    _NON_THINKING_400_MARKERS = (
        "not found", "not loaded", "no model", "context", "num_ctx",
        "too large", "too many", "exceed", "grammar", "response_format",
        "invalid request",
    )
    return not any(marker in msg for marker in _NON_THINKING_400_MARKERS)

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

# The same registry for the Unsloth backend — Studio model ids (the /v1
# layer names models by repo id, with the quant picked at download/load
# time — see findings I-5). Kept as a SEPARATE list from KNOWN_MODELS (not
# merged) because the two backends' id namespaces are disjoint: a legacy
# ollama-style id means nothing to Studio and vice versa (migration plan
# §11: unknown stored ids degrade to "unavailable", never crash).
UNSLOTH_KNOWN_MODELS = [
    {
        "id": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "label": "Gemma 4 26B (Unsloth, IQ4_NL)",
        "thinking": True,
    },
]


def _builtin_models() -> list[dict]:
    """The builtin model registry for whichever LLM backend is active."""
    return UNSLOTH_KNOWN_MODELS if effective_llm_api_key() else KNOWN_MODELS


def _client():
    """The LLM backend client for this process. Unsloth mode (an API key is
    configured) returns app.llm_client's OpenAI-dialect shim over httpx;
    otherwise the legacy ollama AsyncClient — the dual-mode bridge the
    migration plan's rollback story needs until the Phase 7 cutover (see
    module header)."""
    if effective_llm_api_key():
        from .llm_client import UnslothClient
        return UnslothClient(effective_llm_url(), effective_llm_api_key())
    return _ollama.AsyncClient(host=effective_llm_url())


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
# "assist" covers the shared AI-assist engine (app/ai_assist.py) — the
# expand/improve/summarize/analyze/suggest panel every editor surface
# embeds — so a GM can pin a fast small model for quick editorial ops
# while keeping the bigger chat/recap models where depth matters.
DEFAULT_SURFACES = ("chat", "ask_ai", "image", "recap", "assist")


def get_defaults() -> dict:
    d = _load_data().get("defaults", {})
    return {s: d.get(s, "") for s in DEFAULT_SURFACES}


def set_default(surface: str, model_id: str) -> None:
    data = _load_data()
    defaults = data.setdefault("defaults", {})
    defaults[surface] = model_id
    _save_data(data)


# A dedicated embedding-model setting, not folded into DEFAULT_SURFACES —
# every one of those picks between ordinary CHAT-capable models for a
# generation surface (chat/ask_ai/image/recap/assist); an embedding model
# (nomic-embed-text, mxbai-embed-large, ...) is a completely different
# kind of model that can't serve any of those, and none of those models
# can serve embed requests either. Instance-wide like every other setting
# in this file, stored the same way (ai_models.json via _load_data/
# _save_data) rather than per-world: which embedding model is installed
# is a deployment fact, not campaign content. nomic-embed-text is a small,
# widely-available Ollama embedding model — a reasonable default for a
# GM who hasn't pulled anything else, but this only matters once they
# actually configure a World.obsidian_vault_path (see app.vault_sync);
# nothing calls embed_text otherwise.
DEFAULT_EMBED_MODEL = "nomic-embed-text"
# Unsloth's /v1/embeddings needs a hub model id, not an Ollama tag. Verified
# live (findings I-2 + Phase 0.5 appendix) with unsloth's bge-small GGUF —
# the embeddings model is downloaded from Studio's hub on first use, so a
# cold call may take a while (the client's embed timeout covers that).
DEFAULT_UNSLOTH_EMBED_MODEL = os.getenv("UNSLOTH_EMBED_MODEL", "") or "unsloth/bge-small-en-v1.5"


def get_embed_model() -> str:
    # An explicitly-saved choice always wins; otherwise the default depends
    # on which backend is active (an Ollama tag can't resolve on Studio's
    # /v1/embeddings and vice versa).
    stored = _load_data().get("embed_model")
    if stored:
        return stored
    if effective_llm_api_key():
        return DEFAULT_UNSLOTH_EMBED_MODEL
    return DEFAULT_EMBED_MODEL


def set_embed_model(model_id: str) -> None:
    data = _load_data()
    data["embed_model"] = model_id
    _save_data(data)


async def embed_text(text: str, model: str = "") -> list[float]:
    """Embeds `text` via the active backend (Studio /v1/embeddings, or
    Ollama's /api/embed on the legacy path), for app.retrieval.
    vector_search and app.vault_sync's ingestion (both call this the same
    way — a query and a vault chunk need the same embedding space to be
    comparable at all). Raises _ollama.ResponseError untouched (e.g. the
    configured embedding model isn't pulled) — callers are batch/sync
    operations (a vault rebuild, one query-time embed) that need to know
    embedding genuinely failed rather than silently getting back a
    zero-vector that would then "match" everything equally under cosine
    similarity."""
    m = model or get_embed_model()
    resp = await _client().embed(model=m, input=text)
    return list(resp.embeddings[0])


# ── Studio extras preferences (TTS / STT / console) ─────────────────────────
# Stored in ai_models.json like embed_model — deployment facts, not campaign
# content, so no per-world rows and no AppSettings migration.

DEFAULT_TTS_MODEL = "unsloth/orpheus-3b"
DEFAULT_TTS_VOICE = ""
DEFAULT_STT_MODEL = "small"


def get_tts_model() -> str:
    return _load_data().get("tts_model") or DEFAULT_TTS_MODEL


def set_tts_model(model_id: str) -> None:
    data = _load_data()
    data["tts_model"] = model_id
    _save_data(data)


def get_tts_voice() -> str:
    return _load_data().get("tts_voice") or DEFAULT_TTS_VOICE


def set_tts_voice(voice: str) -> None:
    data = _load_data()
    data["tts_voice"] = voice
    _save_data(data)


def get_tts_instructions() -> str:
    """Default delivery style for generated speech (Studio's
    `instructions` field — "gruff, tired dockworker"). Overridable per
    call; blank = the model's natural delivery."""
    return _load_data().get("tts_instructions") or ""


def set_tts_instructions(text: str) -> None:
    data = _load_data()
    data["tts_instructions"] = text
    _save_data(data)


def get_tts_language() -> str:
    """Language hint for TTS (blank = model default). Sent only when set —
    Studios that don't know the key never see it."""
    return _load_data().get("tts_language") or ""


def set_tts_language(language: str) -> None:
    data = _load_data()
    data["tts_language"] = language
    _save_data(data)


def get_stt_model() -> str:
    return _load_data().get("stt_model") or DEFAULT_STT_MODEL


def set_stt_model(model_id: str) -> None:
    data = _load_data()
    data["stt_model"] = model_id
    _save_data(data)


def get_studio_console_url() -> str:
    """Explicit override for where the Studio web UI lives (the /studio
    console embed). Empty = fall back to the AI backend's own URL — the same
    server in the standard deployment. Needed when Studio runs on a
    different host/port than UNSLOTH_URL exposes (e.g. the Desktop app binds
    127.0.0.1:8888 while nd-world runs elsewhere)."""
    return _load_data().get("studio_console_url") or ""


def set_studio_console_url(url: str) -> None:
    data = _load_data()
    data["studio_console_url"] = url
    _save_data(data)


def _is_internal_host(host: str) -> bool:
    """A hostname only the Docker network can resolve: a bare Compose service name ("unsloth") or
    Docker's host.docker.internal. A real DNS name, an IP and localhost are all fine to hand a browser."""
    host = (host or "").lower()
    if not host or host == "localhost" or ":" in host:        # ":" -> an IPv6 literal
        return False
    if host.replace(".", "").isdigit():                          # IPv4 literal
        return False
    return "." not in host or host.endswith(".internal")


_BARE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::\d{1,5})?(?:/\S*)?$")


def normalize_console_url(url: str) -> str:
    """A Studio Console URL typed without a scheme ("192.168.1.216:8000", "studio.lan") gets "http://" -
    a browser would otherwise treat it as a path on nd-world's own page. Anything that is not a plain
    host[:port][/path] (javascript:, data:, //host, ftp://) is returned unchanged for the caller to refuse."""
    url = (url or "").strip()
    host = url.split("/", 1)[0]
    if url and "://" not in url and _BARE_HOST_RE.match(url) and ("." in host or ":" in host):
        return "http://" + url
    return url


def studio_browser_url(page_host: str = "") -> str:
    """Where a person's BROWSER can open Unsloth Studio (the /studio console iframe and its "open Studio
    directly" link) - not where nd-world's server talks to it. UNSLOTH_URL is normally the Compose
    service name (http://unsloth:8000), which resolves only inside the Docker network, so for a browser
    the host is swapped for the one the page itself was reached on (`page_host`, keeping Studio's
    scheme/port - Compose publishes them side by side). An explicit "Studio Console URL" (Settings ->
    System) always wins; "" = Studio isn't configured."""
    from urllib.parse import urlsplit, urlunsplit
    explicit = normalize_console_url(get_studio_console_url()).rstrip("/")
    if explicit:
        return explicit
    if not effective_llm_api_key():
        return ""
    url = (effective_llm_url() or "").rstrip("/")
    parts = urlsplit(url)
    if page_host and _is_internal_host(parts.hostname or ""):
        host = f"[{page_host}]" if ":" in page_host and not page_host.startswith("[") else page_host
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    return url



def pack_embedding(vec: list) -> str:
    """Packs an embedding vector into a compact, DB-storable string
    (base64 of little-endian float32 bytes) — VaultChunk.embedding's own
    storage format (see app/models.py). A plain JSON list of floats would
    work too but costs several times the bytes for the same precision a
    similarity search actually needs; this lives here (not app.retrieval
    or app.vault_sync) so both of those leaf modules — which can't import
    from EACH OTHER (vault_sync imports retrieval's section-splitter, so
    the reverse would be circular) — can each import this shared codec
    from the one module neither has any reason to avoid."""
    return _b64.b64encode(_struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def unpack_embedding(data: str) -> list:
    raw = _b64.b64decode(data)
    n = len(raw) // 4
    return list(_struct.unpack(f"<{n}f", raw))


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
    builtins = _builtin_models()
    seen = {m["id"] for m in builtins}
    visible_builtins = [m for m in builtins if m["id"] not in hidden]
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


async def list_huggingface_repo_files_recursive(repo_id: str, suffix: str = ".gguf") -> list[dict]:
    """Every file in `repo_id` (any revision path, not just the repo root)
    whose name ends in `suffix`, each as {"path": <full repo-relative path>,
    "size_bytes":} — for image-gen models like a Krea 2 GGUF build, where a
    publisher commonly splits quantizations into subfolders (e.g. this
    repo's own TURBO/ and BASE/ — see city96/ComfyUI-GGUF#464) rather than
    dumping every file at the repo root the way list_huggingface_gguf_files
    above assumes.

    A DELIBERATELY SEPARATE function from list_huggingface_gguf_files, not
    a shared "add recursive=True" flag on it: that one backs the Ollama
    "pull from HF" flow, which builds a `hf.co/{repo}:{filename}` pull
    string Ollama resolves itself — untested here whether Ollama's own
    resolution accepts a filename containing a "/" for a nested path, so
    that function is left exactly as it already works (root-level files
    only) rather than risking a regression for a use case (Ollama LLM
    pulls) this function was never written to serve. This one instead
    hands its `path` straight to app.ai.download_swarmui_model's own `url`
    (as .../resolve/main/{path}) — a plain HTTP download has no such
    per-consumer ambiguity about what a "/" in the path means.

    Uses HF's tree API with recursive=true (per huggingface_hub's own
    documented list_repo_tree(..., recursive=True) parameter for this
    exact endpoint) so one call covers every subfolder — not independently
    verified against a live call from this environment (outbound access to
    huggingface.co is unavailable here; see this module's other HF
    functions for the same "returns [] on any failure" fallback shape,
    which already covers an unexpected response to this param too).
    Returns [] on any failure, same reasoning as search_huggingface_models
    above."""
    repo_id = (repo_id or "").strip().strip("/")
    if not repo_id or "/" not in repo_id:
        return []
    suffix = (suffix or "").lower()
    try:
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(f"{_HF_API_BASE}/models/{repo_id}/tree/main", params={"recursive": "true"})
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
        if entry.get("type") == "file" and path.lower().endswith(suffix):
            out.append({"path": path, "size_bytes": entry.get("size")})
    return out


_GGUF_PUSH_MAX_ATTEMPTS = 3
_GGUF_PUSH_RETRY_DELAY_SECONDS = 3.0


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
    volume shared with the "ollama" Compose service the way SWARMUI_MODELS_DIR
    does, since the blob is pushed over the network, not
    read off a shared disk.

    Yields the same {"total":,"completed":}/{"status":"done",...}/
    {"error":} shape download_swarmui_model already uses (so the client-side
    JS can reuse identical progress-bar parsing), except create_blob has no
    byte-level progress callback of its own — the "pushing to Ollama" phase
    is reported as a single indeterminate step rather than granular bytes,
    since by this point the file is already fully on local disk and the
    only remaining unknown-duration work is the blob upload + registration.

    create_blob streams the WHOLE file over one HTTP connection (32KB
    chunks, but still one connection for the entire multi-GB transfer) —
    far more exposed to a transient network blip than any other call this
    module makes, and the ollama client sets timeout=None (no client-side
    timeout at all, so this never gives up on its own), which means the
    only way a dropped connection surfaces is httpx raising a bare
    TransportError (ReadError/WriteError/ConnectError/RemoteProtocolError)
    once the peer actually closes it. A single such blip used to fail the
    whole import outright — expensive to retry by hand for a many-GB file
    — so this retries create_blob itself a few times with a short delay
    before giving up for good."""
    model_name = (model_name or "").strip()
    if not model_name:
        yield {"error": "No model name given"}
        return
    if effective_llm_api_key():
        # Ollama's blob-push upload has no Unsloth equivalent — models are
        # added through Studio's own Model Hub. Clear error instead of the
        # AttributeError the shim would raise (see app.llm_client).
        yield {"error": "Upload-from-PC needs the Ollama backend. With Unsloth, add the GGUF via Studio's Model Hub instead."}
        return
    if not path.is_file():
        yield {"error": "Uploaded file is missing"}
        return
    try:
        client = _client()
        digest = None
        for attempt in range(1, _GGUF_PUSH_MAX_ATTEMPTS + 1):
            if attempt == 1:
                yield {"status": "uploading", "detail": "Pushing file to Ollama…"}
            else:
                yield {
                    "status": "uploading",
                    "detail": f"Connection to Ollama dropped — retrying push ({attempt - 1}/{_GGUF_PUSH_MAX_ATTEMPTS - 1})…",
                }
            try:
                digest = await client.create_blob(str(path))
                break
            except _httpx.TransportError as exc:
                if attempt == _GGUF_PUSH_MAX_ATTEMPTS:
                    raise
                _log.warning(
                    "import_local_gguf_model: create_blob dropped (attempt %d/%d): %s: %s",
                    attempt, _GGUF_PUSH_MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                await asyncio.sleep(_GGUF_PUSH_RETRY_DELAY_SECONDS)
        yield {"status": "creating", "detail": "Registering model…"}
        await client.create(model=model_name, files={path.name: digest})
        yield {"status": "done", "model": model_name}
    except _ollama.ResponseError as exc:
        yield {"error": f"Ollama {exc.status_code}: {exc.error}"}
    except Exception as exc:
        _log.warning("import_local_gguf_model failed: %s: %s", type(exc).__name__, exc)
        # str(exc) is empty for some httpx transport errors (the underlying
        # cause is on __cause__/__context__ instead, e.g. a bare
        # ConnectionResetError) — repr() at least names the exception type
        # clearly instead of rendering as a bare, unhelpful "ReadError: ".
        detail = str(exc) or repr(exc)
        yield {"error": f"{type(exc).__name__}: {detail}"}


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
    import asyncio as _asyncio
    try:
        # Bounded like the /models route: resolve_model runs BEFORE the
        # SSE heartbeat starts on every chat surface, and an unbounded
        # _list_loaded here (LLM_CHAT_TIMEOUT, default 1 h) is a zero-byte
        # freeze the proxy kills at ~100 s with no error event (audit
        # 2026-09-30, chat finding 4). On timeout: return the request
        # unchanged — the chat call itself then fails with a clear
        # model-not-found instead of a hang.
        available = await _asyncio.wait_for(_list_loaded(), 10)
    except _asyncio.TimeoutError:
        _log.warning("resolve_model: model list timed out — passing %r through unresolved", target)
        return target, ""
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

    Under Unsloth there is no .ps() equivalent — but /v1/models tags each
    entry with a `loaded` bool (findings I-5), so the same panel degrades to
    "currently loaded models" (no per-model size/VRAM split — Studio's own
    UI shows that). Under the legacy Ollama backend this is .ps() exactly as
    before.

    A model doesn't have to fit in VRAM entirely — Ollama offloads whatever
    doesn't fit to system RAM (running slower, but still working), so
    size_ram_bytes (size minus size_vram) is how much of THIS model is
    sitting in system RAM rather than on the GPU. unload_model() below frees
    both at once — Ollama has no notion of evicting only the RAM-resident
    part of a model that's split across both."""
    if effective_llm_api_key():
        try:
            resp = await _client().list()
        except Exception:
            return []
        return [
            {"model": m.model, "size_bytes": None, "size_vram_bytes": None,
             "size_ram_bytes": None, "expires_at": None}
            for m in resp.models
            if getattr(m, "loaded", False)
        ]
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
    should ever look like a crash. Under Unsloth there is no equivalent
    call at all — Studio's idle auto-unload owns residency — so this is a
    documented no-op returning False there."""
    if effective_llm_api_key():
        _log.info("unload_model(%r): no-op under Unsloth — idle auto-unload owns residency", model_id)
        return False
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
    return f"[empty response from {model} ({detail}) — try a different model, or check the {_backend_label()} server logs]"


async def generate_chat(messages: list[dict], system: str = "", model: str = "", options: dict = None, think: bool = False, format=None) -> str:
    m = model or effective_ollama_model()
    _log.info("generate_chat model=%s msgs=%d", m, len(messages))
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        chat_kwargs = await _chat_kwargs(options, think, m)
        if format is not None:
            # Structured output (findings I-3): a JSON schema dict or
            # "json" — passed through to the backend untouched.
            chat_kwargs["format"] = format
        effective_think = chat_kwargs["think"]
        if think and not effective_think and m in _prompt_token_thinking_models:
            # This model already rejected think=true once, but nd-world's
            # own records still vouch for its thinking — the downgrade
            # above would silently drop to instruct mode, so re-enable
            # reasoning via the chat-template's own trigger instead.
            full = _messages_with_prompt_think_token(full)
        resp = await _client().chat(model=m, messages=full, **chat_kwargs)
        # The EFFECTIVE think (post _chat_kwargs downgrade), not the
        # caller's requested one — see _record_thinking_result's own
        # docstring for why that distinction matters once a rejection has
        # poisoned this model's capability cache.
        _record_thinking_result(m, effective_think, failed=False)
        content = resp.message.content
        # See _strip_inline_think_tags' own docstring: a model on the
        # <|think|> prompt-token fallback gets no native message.thinking
        # separation from Ollama at all, so its raw <think>...</think>
        # reasoning (if any) is still sitting in `content` here — a no-op
        # for every other model, whose content never contains that tag.
        inline_thinking_chars = 0
        if content:
            content, inline_thinking_chars = _strip_inline_think_tags(content)
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
        thinking_chars = len(thinking or "") + inline_thinking_chars
        # thinking_chars (not just had_thinking's bool) is what a GM/admin
        # actually needs to calibrate _THINKING_HEADROOM_TOKENS from logs
        # across repeated failures — see that constant's own comment.
        _log.warning(
            "generate_chat model=%s returned empty content (done_reason=%r, eval_count=%r, "
            "had_thinking=%r, thinking_chars=%d)",
            m, done_reason, eval_count, bool(thinking_chars), thinking_chars,
        )
        return _empty_response_message(m, thinking_chars, done_reason)
    except _ollama.ResponseError as exc:
        _log.error("generate_chat Ollama error: %s %s", exc.status_code, exc.error)
        if format is not None and "response_format" in (getattr(exc, "error", None) or ""):
            # This backend has no grammar engine for structured output
            # (Studio: "response_format needs the llama.cpp grammar
            # engine; load a GGUF model to use it") — retry in plain mode;
            # the caller's defensive JSON extraction handles free-form
            # replies that still follow the system prompt.
            return await generate_chat(messages, system=system, model=model,
                                       options=options, think=think, format=None)
        if think and _is_thinking_rejection(exc):
            _record_thinking_result(m, think, failed=True)
            # Ollama flatly refused think=true for this model — not a
            # transient error, so retrying the identical call would just
            # 400 again. Recover instead of hard-failing the caller (a
            # background job, or an interactive chat reply): redo the
            # exact same request with think=False. Safe to recurse — a
            # think=False request can never re-enter this branch, and no
            # partial content was produced yet (this was an upfront
            # rejection, not a mid-stream failure).
            #
            # But when nd-world's OWN records vouch for this model's
            # thinking (KNOWN_MODELS, or a GM's override checkbox), the
            # rejection is almost certainly ollama#16936 — hf.co-imported
            # GGUFs never get the capability tag — so falling back to
            # plain instruct mode would silently lose the reasoning the
            # GM asked for. Switch those to the chat template's manual
            # trigger (<|think|> system-prompt token) instead, and keep
            # using it for future calls (see _prompt_token_thinking_models).
            retry_full = full
            if _known_model_thinks(m) or _model_override_thinks(m):
                _prompt_token_thinking_models.add(m)
                retry_full = _messages_with_prompt_think_token(full)
                _log.warning("generate_chat model=%s: does not support thinking — retrying with the %s system-prompt token", m, _PROMPT_THINK_TOKEN)
            else:
                _log.warning("generate_chat model=%s: does not support thinking — retrying with think=False", m)
            return await generate_chat(retry_full, system="", model=m, options=options, think=False)
        return f"[AI error: {_backend_label()} {exc.status_code}: {exc.error}]"
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
    emit_thinking: bool = False, format=None,
) -> AsyncGenerator[str | dict, None]:
    """`think` defaults to False, same as generate_chat's own plain
    default — most interactive surfaces (AI Chat's World Chat/Image tabs,
    "Talk to this NPC") have no Thinking toggle at all and never pass it.
    The entity detail page's "Ask AI" panel and the Chronicler are the two
    callers that do (see app.routers.ai's ChatBody.think/epSend's own
    Thinking checkbox, and app.routers.chronicler's own), letting a GM
    opt into slower/deeper reasoning for those surfaces per-request.

    emit_thinking defaults to False and changes what this generator
    yields: False (every existing caller that predates this flag) yields
    plain content strings exactly as before, silently dropping any
    reasoning text the model produced — unchanged behavior. True yields
    dicts instead — {"type": "content"|"thinking"|"error", "text": str} —
    so a caller that wants to show the model's reasoning live (alongside
    the Thinking toggle that requests it) can tell the pieces apart on the
    wire. "thinking" is never emitted when the model produced no reasoning
    at all (a non-thinking model, or a thinking one Ollama silently
    downgraded). "error" is this generator's diagnostic sentinels (an
    empty response, an Ollama ResponseError, an unexpected exception) —
    always this function's LAST piece, and always distinct from "content"
    so a caller never mistakes a failure for a real answer worth
    displaying/saving as one (see app.routers.ai's ai_stream and
    app.routers.chronicler's chronicler_ask, which both forward it as its
    own SSE `error` field rather than folding it into `token`). Callers
    that keep emit_thinking=False are unaffected — they still just get the
    plain sentinel string back, exactly as before."""
    m = model or effective_ollama_model()
    _log.info("stream_chat model=%s msgs=%d think=%r", m, len(messages), think)
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    yielded_any = False
    thinking_chars = 0
    done_reason = None

    def _piece(text: str):
        # See emit_thinking's own docstring paragraph above — every
        # existing caller (emit_thinking=False) keeps getting plain
        # strings; only a caller that opted in to seeing reasoning gets
        # the {"type": "content", ...} wrapper.
        return {"type": "content", "text": text} if emit_thinking else text

    # See _InlineThinkSplitter's own docstring for why this exists: a
    # model on the <|think|> prompt-token fallback gets no native
    # message.thinking field from Ollama at all (that retry deliberately
    # sends think=False), so a model that still wraps its reasoning in
    # literal <think>...</think> markup would otherwise dump that whole
    # block into the visible answer with no separate reasoning piece ever
    # emitted. Only allocated when a caller actually wants to see
    # reasoning at all.
    splitter = _InlineThinkSplitter() if emit_thinking else None

    def _emit_split(kind: str, text: str):
        nonlocal yielded_any
        if kind == "thinking":
            return {"type": "thinking", "text": text}
        yielded_any = True
        return _piece(text)

    try:
        chat_kwargs = await _chat_kwargs(options, think, m)
        if format is not None:
            chat_kwargs["format"] = format
        if think and not chat_kwargs["think"] and m in _prompt_token_thinking_models:
            # Same prompt-token fallback as generate_chat: a poisoned
            # capability cache downgraded think to False, but this model's
            # reasoning is still wanted and still available via the token.
            full = _messages_with_prompt_think_token(full)
        async for chunk in await _client().chat(model=m, messages=full, stream=True, **chat_kwargs):
            token = chunk.message.content
            piece_thinking = getattr(chunk.message, "thinking", None)
            if emit_thinking and piece_thinking:
                yield {"type": "thinking", "text": piece_thinking}
            if token:
                if splitter:
                    for kind, text in splitter.feed(token):
                        if kind == "thinking":
                            thinking_chars += len(text)
                        yield _emit_split(kind, text)
                else:
                    yielded_any = True
                    yield _piece(token)
            # Tracked regardless of whether `think` was requested — even
            # with think=False a model can still ignore that and burn its
            # budget on hidden reasoning (the case generate_chat's own
            # diagnostic exists for); with think=True this is simply the
            # expected/intended reasoning trace, tracked the same way so
            # the same starvation diagnostic below still fires if even a
            # deliberately-thinking model runs out of room before writing
            # a visible answer.
            thinking_chars += len(piece_thinking or "")
            done_reason = getattr(chunk, "done_reason", None) or done_reason
        if splitter:
            for kind, text in splitter.flush():
                if kind == "thinking":
                    thinking_chars += len(text)
                yield _emit_split(kind, text)
        # The EFFECTIVE think (post _chat_kwargs downgrade), not the
        # caller's requested one — see generate_chat's identical call and
        # _record_thinking_result's own docstring for why.
        _record_thinking_result(m, chat_kwargs["think"], failed=False)
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
            msg = _empty_response_message(m, thinking_chars, done_reason)
            yield {"type": "error", "text": msg} if emit_thinking else msg
    except _ollama.ResponseError as exc:
        _log.error("stream_chat Ollama error: %s %s", exc.status_code, exc.error)
        if format is not None and "response_format" in (getattr(exc, "error", None) or ""):
            # This backend has no grammar engine for structured output —
            # retry in plain mode; the caller's defensive JSON extraction
            # handles a free-form reply that still follows the prompt.
            async for piece in stream_chat(messages, system=system, model=model,
                    options=options, think=False, emit_thinking=emit_thinking,
                    format=None):
                yield piece
            return
        if think and _is_thinking_rejection(exc) and not yielded_any:
            _record_thinking_result(m, think, failed=True)
            # Same recovery as generate_chat — an upfront rejection means
            # no tokens have been yielded yet (guarded above defensively:
            # if some content is already out the door, don't restart the
            # stream and risk duplicating it), so redo the identical
            # request with think=False instead of surfacing the sentinel.
            # And the same prompt-token fallback as generate_chat when
            # nd-world's own records vouch for the model (ollama#16936
            # imports): the retry re-enables reasoning via the template's
            # <|think|> system-prompt token rather than dropping to
            # instruct mode.
            retry_full = full
            if _known_model_thinks(m) or _model_override_thinks(m):
                _prompt_token_thinking_models.add(m)
                retry_full = _messages_with_prompt_think_token(full)
                _log.warning("stream_chat model=%s: does not support thinking — retrying with the %s system-prompt token", m, _PROMPT_THINK_TOKEN)
            else:
                _log.warning("stream_chat model=%s: does not support thinking — retrying with think=False", m)
            async for piece in stream_chat(
                retry_full, system="", model=m, options=options, think=False, emit_thinking=emit_thinking,
            ):
                yield piece
            return
        msg = f"[AI error: {_backend_label()} {exc.status_code}: {exc.error}]"
        yield {"type": "error", "text": msg} if emit_thinking else _piece(msg)
    except Exception as exc:
        _log.error("stream_chat unavailable: %s: %s", type(exc).__name__, exc)
        msg = f"[AI unavailable: {type(exc).__name__}: {exc}]"
        yield {"type": "error", "text": msg} if emit_thinking else _piece(msg)


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
    "invent details that aren't implied by the recap.\n\n"
    "For each fact, also set \"tags\": a short list (0-4) of lowercase, single-or-two-word "
    "keywords to help the GM find this fact later — named characters or creatures it "
    "involves, the location, and/or a category like combat, loot, investigation, npc, "
    "plot, or romance. Reuse the same tag spelling for the same person/place/category "
    "across facts (e.g. always \"elyra\", never \"Elyra\" and \"elyra the enchanter\" in "
    "different facts) so facts about the same thing can be found together. Leave tags "
    "empty for a fact with nothing worth tagging rather than inventing one.\n\n"
    "The text may also contain out-of-character discussion — rules questions, setup, table "
    "talk. Ignore out-of-character discussion entirely and extract only facts about what "
    "happened in the story; if a passage contains no in-story events, return an empty facts "
    "list. Never describe the text itself or your extraction process — respond with the "
    "facts JSON only. Write every fact in English, regardless of the language the recap or "
    "transcript is written in."
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
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content", "visible_to_players", "tags"],
            },
        },
    },
    "required": ["facts"],
}


# Extra context-window headroom every parse chunk reserves for the model's
# JSON response, on top of _CHUNK_RESERVED_TOKENS (which already budgets for
# a system prompt + a generic response + margin) and the world_context
# reserve. A facts list for one chunk is short, but it's the model's ENTIRE
# visible answer (format= binds _RECAP_FACTS_SCHEMA to `content`), and a
# scene-dense chunk can legitimately yield a dozen sentences — 512 tokens
# keeps that answer from colliding with the chunk's own input tokens.
_FACTS_PARSE_RESPONSE_RESERVE_TOKENS = 512

# parse_facts_from_recap sizes its chunks by INPUT TARGET, not by squeezing
# them into a fixed window: each chunk aims for this many input tokens and
# the per-call num_ctx pin is grown to reserves + chunk instead (see
# _facts_parse_chunk_plan for the full story of why the old
# "derive chunk size from an assumed window" model collapsed to 500-token
# chunks and 26 AI calls for one ~11k-token recap). Think=True reserves a
# much larger thinking headroom, so its default target is smaller to keep
# the pinned window (reserves + chunk) in the same ballpark either way.
# Env-tunable (same idiom as THINKING_HEADROOM_TOKENS) for an install that
# wants bigger chunks per call or has very little memory to pin windows
# with; floored at _FACTS_PARSE_MIN_CHUNK_INPUT_TOKENS so a typo'd value
# can't re-create the 500-token-chunk pathology it replaces.
_FACTS_PARSE_MIN_CHUNK_INPUT_TOKENS = 1024


def _facts_parse_target_input_tokens(think: bool) -> int:
    """The per-chunk INPUT target (in tokens) parse_facts_from_recap sizes
    chunks to — one env var, two defaults: 3072 when think=True (whose
    thinking reserve is huge and would otherwise pin every window past
    10k tokens) and 4096 when think=False. See _facts_parse_chunk_plan."""
    default = "3072" if think else "4096"
    return max(_FACTS_PARSE_MIN_CHUNK_INPUT_TOKENS, int(os.getenv("FACTS_PARSE_TARGET_INPUT_TOKENS", default)))


# The floor for parse_facts_from_recap's per-call num_ctx pin: reserves +
# a chunk estimate should always clear this, but a tiny one-chunk parse
# must not pin a window smaller than the model could legitimately want
# (below this, Ollama would squeeze the reserves themselves).
_FACTS_PARSE_MIN_WINDOW_TOKENS = 2048


def _normalized_fact_key(content: str) -> str:
    """The dedup key parse_facts_from_recap merges chunk results by:
    lowercase, collapsed whitespace, trailing punctuation stripped. Adjacent
    chunks overlap at their sentence-boundary split points, and a small local
    model re-extracting the same scene event from both sides ("The party met
    Elyra." vs "the party met Elyra!") used to be impossible before chunking
    — now it would surface as a duplicate row in the GM's review list, so
    near-identical wordings of one event collapse to the first-seen one."""
    return re.sub(r"\s+", " ", content.strip().lower()).rstrip(".!?…")


async def _parse_facts_chat_call(m: str, messages: list[dict], think: bool, options: dict) -> dict:
    """One schema-constrained chat request for parse_facts_from_recap — the
    whole request/retry machinery factored out of the chunk loop so the
    <|think|> rejection recovery below lives in ONE place instead of being
    duplicated per chunk. Raises ValueError on any failure (same contract
    the pre-chunking parse had), returns the raw ollama response on success
    (the CALLER owns the JSON parsing, so a malformed chunk is a skip-one-
    chunk problem rather than a whole-parse failure).

    `options` is the per-chunk num_ctx pin computed by the caller (see
    parse_facts_from_recap) — layered over the GM's configured options by
    _chat_kwargs like any per-request override, on both the original call
    and the rejection retry below.

    A think=true request that Ollama rejects outright ("<model> does not
    support thinking", HTTP 400) recovers exactly like generate_chat's own
    rejection branch instead of failing the chunk: retry once with
    think=False — for a model nd-world itself vouches for (KNOWN_MODELS, or
    a GM's per-model override — the hf.co-imported-GGUF case, ollama#16936)
    with the <|think|> system-prompt token prepended so the reasoning the
    GM asked for still happens — and the JSON constraint stays on the retry
    too, since it binds `content` while token-triggered thinking lands in
    that separate `thinking` field. See _prompt_token_thinking_models and
    generate_chat's matching block for the full reasoning."""
    chat_kwargs = await _chat_kwargs(options, think, m)
    if think and not chat_kwargs["think"] and m in _prompt_token_thinking_models:
        # Same pre-call injection as generate_chat/stream_chat: the
        # capability cache a rejection poisoned downgraded the requested
        # think=True above, but this model's reasoning is still wanted
        # and still available via the template's <|think|> token —
        # without this, every chunk AFTER the first rejection would
        # silently drop to instruct mode, and unlike chat nothing
        # downstream labels the result, so the GM would just get
        # shallower facts with no sign thinking ever stopped.
        messages = _messages_with_prompt_think_token(messages)
    try:
        resp = await _client().chat(model=m, messages=messages, format=_RECAP_FACTS_SCHEMA, **chat_kwargs)
        # The EFFECTIVE think (post _chat_kwargs downgrade), not the caller's
        # requested one — same call generate_chat/stream_chat make after a
        # successful response, so a genuinely successful think=true parse
        # clears the advisory failure and retires the prompt-token fallback
        # exactly like a successful chat does (a downgraded think=False
        # parse records nothing — see _record_thinking_result's docstring).
        _record_thinking_result(m, chat_kwargs["think"], failed=False)
    except _ollama.ResponseError as exc:
        _log.error("parse_facts_from_recap Ollama error: %s %s", exc.status_code, exc.error)
        if think and _is_thinking_rejection(exc):
            # Ollama flatly refused think=true for this model — recover the
            # same way generate_chat/stream_chat do instead of failing the
            # chunk. Not a transient error, so retrying the identical call
            # would just 400 again; the Facts page's Thinking checkbox is
            # exactly as deliberate as AI Chat's, and this ResponseError
            # becoming the plain ValueError it used to be failed every
            # parse for hf.co-imported GGUFs Ollama never tagged as
            # thinking-capable (ollama#16936). The rejection also poisons
            # the capability cache (see _record_thinking_result), so later
            # chunks skip the doomed flag via the pre-call injection above
            # instead of repeating this round-trip.
            _record_thinking_result(m, think, failed=True)
            retry_messages = messages
            if _known_model_thinks(m) or _model_override_thinks(m):
                # nd-world's OWN records still vouch for this model's
                # thinking, so the rejection is almost certainly the missing
                # capability tag rather than a model that can't reason —
                # retry with the chat template's manual trigger (<|think|>
                # system-prompt token) instead of silently dropping to
                # instruct mode, and keep using it for future calls (see
                # _prompt_token_thinking_models).
                _prompt_token_thinking_models.add(m)
                retry_messages = _messages_with_prompt_think_token(messages)
                _log.warning("parse_facts_from_recap model=%s: does not support thinking — retrying with the %s system-prompt token", m, _PROMPT_THINK_TOKEN)
            else:
                _log.warning("parse_facts_from_recap model=%s: does not support thinking — retrying with think=False", m)
            # format=_RECAP_FACTS_SCHEMA stays on the retry: Ollama binds the
            # JSON constraint to the final `content`, while token-triggered
            # thinking lands in the response's separate `thinking` field — so
            # the retry's content is still exactly the JSON blob parsed by
            # the caller. think=False can never re-enter this branch
            # (_chat_kwargs skips its capability check for a falsy think
            # entirely), so this really is a single retry; any failure IT
            # raises still becomes the same ValueError the original call
            # would have produced.
            try:
                resp = await _client().chat(
                    model=m,
                    messages=retry_messages,
                    format=_RECAP_FACTS_SCHEMA,
                    **(await _chat_kwargs(options, False, m)),
                )
            except _ollama.ResponseError as retry_exc:
                raise ValueError(f"Ollama error {retry_exc.status_code}: {retry_exc.error}") from retry_exc
            except Exception as retry_exc:
                raise ValueError(f"AI unavailable: {type(retry_exc).__name__}: {retry_exc}") from retry_exc
        else:
            raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    return resp


def _facts_parse_chunk_plan(raw_text: str, think: bool, world_context_tokens: int, extra_instructions: str = "") -> tuple[int, int]:
    """parse_facts_from_recap's chunk sizing, factored pure so tests can
    force tiny chunks by stubbing it the way _transcript_chunk_char_budget
    used to be stubbed. Returns (chunk_chars, reserve_tokens): the per-chunk
    INPUT char budget for _split_transcript_into_chunks, and the token
    reserve (everything a chunk call carries besides the chunk text itself —
    _chunk_reserve_tokens over _RECAP_SYSTEM plus any `extra_instructions`
    (the Facts page's own one-off steering note, folded in via
    _with_instructions same as every other recap function here — a long
    note is real system-prompt tokens too and must be reserved for, not
    just the base prompt), plus the RAG lore and the JSON response room)
    that the per-call num_ctx pin adds back on top of each chunk's
    estimated tokens.

    The old sizing worked the other way around and had the window first: it
    derived the chunk size from an ASSUMED window (the GM's configured
    num_ctx, else _DEFAULT_ASSUMED_CTX_TOKENS = 4096) minus stacked reserves
    (system prompt, think headroom, world_context, JSON response), flooring
    at 500 input tokens when the reserves overflowed it. With think=True the
    thinking headroom alone (4096) ate the whole unconfigured default
    window, every paste floored to 500-token chunks, and a real ~11k-token
    recap split into 26 parts — 40+ minutes of AI calls for one parse —
    while the model's REAL window (n_ctx 9728 observed live) sat mostly
    idle. The window is ours to choose on this path (every chunk call
    already pinned options={"num_ctx": ...} explicitly), so now the chunk
    comes first — a sane input target, _facts_parse_target_input_tokens —
    and the WINDOW is pinned per call to fit it:

        num_ctx = reserve_tokens + this chunk's estimated input tokens,
                  floored at _FACTS_PARSE_MIN_WINDOW_TOKENS,
                  ceiled at MAX_AUTO_NUM_CTX.

    One deliberate consequence: the GM's configured num_ctx no longer caps
    a facts parse (it was exactly the bug — the cap shrank chunks toward
    the 500-token floor, yet never bound the enforced window, since the
    per-call pin overrides the configured value anyway). The configured
    num_ctx still governs summarize_transcript's chunking unchanged, where
    there is no per-call pin to grow the window with.

    If the reserves ALONE overflow MAX_AUTO_NUM_CTX (an enormous RAG lore
    blob — practically never, it would take ~30k tokens of lore), they
    can't be honored at any pinnable window, so — as under the old model —
    the world_context reserve is dropped with a warning and the (clamped)
    oversized-window pin carries the call: Ollama truncates rather than
    erroring, which degrades one parse instead of failing it mid-paste."""
    # extra_instructions folds into the SAME system string every chunk call
    # actually sends (see parse_facts_from_recap) so its own token cost is
    # reserved for too — a long GM note is real system-prompt tokens, not
    # free text riding along outside the budget this function computes.
    system_for_sizing = _with_instructions(_RECAP_SYSTEM, extra_instructions)
    reserve_tokens = _chunk_reserve_tokens(
        system_for_sizing, think, world_context_tokens + _FACTS_PARSE_RESPONSE_RESERVE_TOKENS,
    )
    if reserve_tokens >= MAX_AUTO_NUM_CTX:
        _log.warning(
            "parse_facts_from_recap: world_context (~%d tokens) pushes the per-call reserves "
            "(~%d tokens) past the MAX_AUTO_NUM_CTX ceiling (%d) — proceeding without reserving "
            "for it, context may overflow",
            world_context_tokens, reserve_tokens, MAX_AUTO_NUM_CTX,
        )
        reserve_tokens = _chunk_reserve_tokens(system_for_sizing, think, _FACTS_PARSE_RESPONSE_RESERVE_TOKENS)
    chars_per_token = _chars_per_token_estimate(raw_text)
    target_input_tokens = _facts_parse_target_input_tokens(think)
    chunk_input_tokens = min(target_input_tokens, MAX_AUTO_NUM_CTX - reserve_tokens)
    if chunk_input_tokens < target_input_tokens:
        # Reserves leave less room than the target under the pin ceiling —
        # shrink the chunk to match rather than pin a window that truncates
        # every chunk mid-input.
        _log.info(
            "parse_facts_from_recap: reserves (~%d tokens) leave only %d input tokens under "
            "MAX_AUTO_NUM_CTX (%d) — shrinking the chunk target to match",
            reserve_tokens, max(chunk_input_tokens, 0), MAX_AUTO_NUM_CTX,
        )
    chunk_input_tokens = max(chunk_input_tokens, _FACTS_PARSE_MIN_CHUNK_INPUT_TOKENS)
    return chunk_input_tokens * chars_per_token, reserve_tokens


async def parse_facts_from_recap(
    raw_text: str, model: str = "", think: bool = False, world_context: str = "",
    extra_instructions: str = "", on_progress=None,
) -> list[dict]:
    """Turn a rough GM recap into draft facts via the local model, using
    Ollama's JSON-schema-constrained `format` — see ollama.AsyncClient.chat's
    `format` parameter. Raises ValueError ONLY when every chunk failed (see
    the merge loop below) so the caller can surface a clear error; does not
    write anything to the database itself.

    A paste longer than one comfortably-sized chunk is split into chunks
    (the same _split_transcript_into_chunks splitter summarize_transcript
    uses for session recordings) and each chunk is extracted independently;
    the results merge into one list, deduplicated by _normalized_fact_key.
    Before chunking, the whole paste (+RAG world_context) went out as ONE
    unconstrained-size request, and a real GM paste of ~12k prompt tokens
    against a ~9.7k-token model context came back as a hard Ollama 400 — no
    facts at all, no matter how long the GM waited. Chunking trades that
    cliff for a few extra AI calls, and each call stays small enough that a
    local model actually extracts well instead of degrading on a huge mixed
    transcript.

    `on_progress(current, total)`, if given, is called before each chunk's
    extraction (1-based, same "currently on part 2 of 5" contract
    summarize_transcript gives audio_jobs.py) so the job runner can persist
    real progress to the job row instead of an undifferentiated
    "summarizing" placeholder for a many-minute parse.

    Each chunk call pins its own context window explicitly:
    options={"num_ctx": reserve_tokens + that chunk's estimated input
    tokens} (see _facts_parse_chunk_plan for the reserve math and for why
    the sizing works chunk-first instead of window-first). Pinning means
    chunk + system prompt + world_context + JSON response room always fit
    the ENFORCED window even when the model's own Modelfile default is
    smaller — which a bare "hope the default is big enough" leaves to
    chance — and that the window GROWS with the reserves instead of the
    chunks shrinking under them: under the old window-first sizing, a
    think=True parse under the unconfigured default window floored every
    chunk to 500 input tokens and split one ~11k-token recap into 26 parts.

    `think` defaults to False, same clean-JSON default every other
    schema-constrained caller in this module uses (see _chat_kwargs' own
    docstring) — it's a parameter only since the Facts page grew its own
    "Thinking" checkbox: a GM opting in gets deeper extraction at the usual
    cost (slower, and a model that spends its whole output budget reasoning
    can come back with empty content — see generate_chat's empty-content
    handling). JSON `format` and think=True coexist fine in Ollama: hidden
    reasoning lands in the response's separate `thinking` field, so
    resp.message.content below is still exactly the JSON blob. The per-chunk
    "does not support thinking" rejection recovery lives in
    _parse_facts_chat_call and works identically per chunk.

    `world_context`, if given, is RAG-retrieved World lore/Notes text (see
    app.audio_jobs._build_rag_context). It's prepended ahead of EACH chunk
    in the USER message by reusing _with_world_context itself — same
    "reference material for name accuracy, not content" framing condense_
    recap puts on its system prompt, kept word-for-word in one place so the
    two surfaces can't drift (and chunk 3 still spells names right even
    though the lore text isn't part of any one chunk). Its token cost is
    part of every chunk call's reserve (see _facts_parse_chunk_plan), so
    adding RAG grows each call's pinned window instead of shrinking the
    chunks. The lore rides in the user message, not the system prompt,
    because the extraction instructions already live in _RECAP_SYSTEM and
    the lore is context FOR the text being split, not a behavior change.

    `extra_instructions` is the Facts page's own one-off steering note (e.g.
    "only extract facts about the Thornwood Syndicate", "ignore combat
    mechanics talk") — same _with_instructions convention every other recap
    function here uses, folded into _RECAP_SYSTEM once up front and sent
    identically on every chunk (so instruction adherence doesn't fade out
    on a later chunk of a long paste). Its own token cost is reserved for
    via _facts_parse_chunk_plan, same as the base system prompt."""
    m = model or effective_ollama_model()
    system = _with_instructions(_RECAP_SYSTEM, extra_instructions)
    # Same len // chars-per-token estimate _transcript_chunk_char_budget
    # applies to its `system` arg — the lore rides EVERY chunk call, so its
    # cost is part of every chunk's reserve, not just the first one's.
    world_context_tokens = (
        len(world_context) // _chars_per_token_estimate(world_context)
    ) if world_context else 0
    chunk_chars, reserve_tokens = _facts_parse_chunk_plan(raw_text, think, world_context_tokens, extra_instructions)
    chunks = _split_transcript_into_chunks(raw_text, chunk_chars)
    _log.info("parse_facts_from_recap: model=%s chunking into %d part(s) (%d chars total)", m, len(chunks), len(raw_text))
    merged_facts: list[dict] = []
    seen_keys: set[str] = set()
    chunk_errors: list[ValueError] = []
    for i, chunk in enumerate(chunks):
        # Same 1-based "currently on part N" timing summarize_transcript
        # uses — BEFORE the call, so the job card shows the in-flight part
        # rather than a number that only updates after minutes of work.
        if on_progress:
            on_progress(i + 1, len(chunks))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _with_world_context(chunk, world_context)},
        ]
        # THE window pin (the ONE rule, see _facts_parse_chunk_plan):
        # reserves + THIS chunk's estimated input tokens (ceil, per-chunk
        # chars-per-token so a dense-script chunk gets a proportionally
        # wider window), floored at _FACTS_PARSE_MIN_WINDOW_TOKENS and
        # ceiled at MAX_AUTO_NUM_CTX. Computed per chunk, not once, since
        # the splitter's boundary-fitting makes real chunks shorter than
        # the target and pinning every call at the target's window would
        # waste KV-cache on the last (undersized) part of the paste.
        chunk_tokens = -(-len(chunk) // _chars_per_token_estimate(chunk))
        num_ctx = min(MAX_AUTO_NUM_CTX, max(_FACTS_PARSE_MIN_WINDOW_TOKENS, reserve_tokens + chunk_tokens))
        try:
            resp = await _parse_facts_chat_call(m, messages, think, {"num_ctx": num_ctx})
        except ValueError as exc:
            # One chunk's model failure must not throw away every OTHER
            # chunk's already-extracted facts (a 40-part parse dying on part
            # 37 would have nothing to show for 36 successful calls) — log,
            # remember, and keep going; the error is only re-raised below if
            # NO chunk succeeded.
            _log.error("parse_facts_from_recap: chunk %d/%d failed (%s) — continuing with the rest", i + 1, len(chunks), exc)
            chunk_errors.append(exc)
            continue
        try:
            parsed = _json.loads(resp.message.content or "")
            facts = parsed["facts"]
            if not isinstance(facts, list):
                raise ValueError
            for f in facts:
                # Stored (and sent onward) as one comma-joined string, same
                # shape Fact.tags/Entity.tags persist in — the schema keeps
                # them a JSON array only because that's what constrains the
                # model to one tag per element instead of a single run-on
                # string.
                raw_tags = f.get("tags") if isinstance(f, dict) else None
                tags = ", ".join(
                    t.strip() for t in raw_tags if isinstance(t, str) and t.strip()
                ) if isinstance(raw_tags, list) else ""
                item = {
                    "content": str(f["content"]), "visible_to_players": bool(f["visible_to_players"]),
                    "tags": tags,
                }
                key = _normalized_fact_key(item["content"])
                if key in seen_keys:
                    continue  # the same event extracted from an adjacent chunk
                seen_keys.add(key)
                merged_facts.append(item)
        except Exception as exc:
            # A chunk whose content isn't valid schema JSON (a small model
            # failing its one job for that part) contributes nothing — but a
            # meta-description or garbage answer for one part must not fail
            # the whole parse the way the single-call version's hard
            # ValueError did. Counted as a failed chunk for the raise-below
            # decision, same as a chat-level failure.
            _log.error("parse_facts_from_recap: chunk %d/%d returned unusable facts JSON (%s) — continuing with the rest", i + 1, len(chunks), exc)
            chunk_errors.append(ValueError("Could not parse facts from that recap — try rephrasing it."))
            continue
    if chunk_errors and len(chunk_errors) == len(chunks):
        # EVERY chunk failed — the parse genuinely produced nothing
        # explainable as "some parts were empty", so preserve the old
        # single-call ValueError contract (the first error, usually worded
        # for the actual failure: Ollama down vs unusable JSON). A clean
        # all-chunks-empty result ({"facts": []} everywhere — pure
        # out-of-character chatter) never reaches this: nothing errored,
        # so it falls through to the empty-list success the Facts page's
        # UI explains to the GM.
        raise chunk_errors[0]
    return merged_facts


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


async def parse_entity_from_text(raw_text: str, kinds: list[str], model: str = "",
                                 kind_hint: str = "") -> dict:
    """Turn a passage of text (typically an AI Chat reply, or — with
    `kind_hint` — a short from-scratch description on the new-entity form)
    into a draft world entity — same JSON-schema-constrained pattern as
    parse_facts_from_recap. Raises ValueError on any failure so the caller
    can surface a clear error; does not write anything to the database
    itself (see main.py's /api/import/execute, which already knows how to
    write this exact shape).

    `kind_hint` (a kind from `kinds`) flips the framing from EXTRACTION
    ("only what the text states") to GENERATION ("invent a coherent draft
    of this kind from the description") — the new-entity form's ✨ Draft
    button; the schema and validation are unchanged."""
    m = model or effective_ollama_model()
    system = _ENTITY_FROM_TEXT_SYSTEM
    if kind_hint:
        system += (
            "\n\nThe GM has selected the kind \"" + kind_hint + "\" — frame the entity as a "
            + kind_hint + " (the kind field MUST be \"" + kind_hint + "\"). The text is a short "
            "creative BRIEF, not source material: invent coherent, evocative, "
            "setting-neutral details consistent with it (names, history, hooks) instead of "
            "only extracting what is stated."
        )
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": system},
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


_RELATION_SUGGEST_SYSTEM = (
    "You read one page from a tabletop RPG GM's worldbuilding notes and find relationships "
    "it EXPLICITLY states between two of the KNOWN entities listed in the schema below — "
    "never a relationship you have to infer or guess, and never an entity outside that list. "
    "For each relationship you find, give \"source\" and \"target\" (the exact known-entity "
    "names involved) and \"relation\": a short snake_case label for what connects them, "
    "written from source to target (e.g. located_in, member_of, owns, allied_with, enemy_of, "
    "parent_of, works_for, rules). Do not report a relationship just because two names appear "
    "in the same paragraph — only ones the text actually states. If the page states no "
    "relationships between known entities, return an empty list. Respond with JSON only."
)


def _relation_suggest_schema(known_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": known_names},
                        "target": {"type": "string", "enum": known_names},
                        "relation": {"type": "string"},
                    },
                    "required": ["source", "target", "relation"],
                },
            },
        },
        "required": ["relations"],
    }


async def suggest_relations_from_text(text: str, known_names: list[str], model: str = "") -> list[dict]:
    """Turn one vault note's body text into draft graph edges between
    entities the GM already has — same JSON-schema-constrained pattern as
    parse_entity_from_text, and just as side-effect-free: raises ValueError
    on any failure so the caller can surface a clear error, otherwise
    returns a plain list of {"source", "target", "relation"} dicts (entity
    NAMES, not ids — app.vault_sync.suggest_relations_for_vault resolves
    those against this world's entities). Nothing is written to the
    database here or by that caller until a GM explicitly confirms a
    suggestion via POST /api/knowledge/relations/bulk.

    `known_names` is constrained into the response schema itself as a JSON
    Schema `enum` on both "source" and "target" — Ollama's grammar-
    constrained decoding then makes it structurally impossible for the
    model to name an entity that doesn't exist, rather than just asking it
    nicely in the system prompt (the same enum trick parse_entity_from_text
    already uses for `kind`). Still worth a defensive re-check below in
    case a given model/runtime doesn't enforce the grammar as strictly as
    Ollama's own docs promise.

    Deliberately no chunking (unlike parse_facts_from_recap): a single
    vault note is already a bounded unit — a GM's worldbuilding page, not a
    whole session transcript — so one call is enough, same assumption
    ai_assist._structured_call makes for editor content."""
    if len(known_names) < 2:
        return []  # nothing to relate
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _RELATION_SUGGEST_SYSTEM},
                {"role": "user", "content": text},
            ],
            format=_relation_suggest_schema(known_names),
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        relations = parsed["relations"]
        if not isinstance(relations, list):
            raise ValueError
    except Exception as exc:
        raise ValueError("Could not extract relations from that note — try again or switch models.") from exc
    known_set = set(known_names)
    out = []
    for r in relations:
        if not isinstance(r, dict):
            continue
        source = str(r.get("source") or "").strip()
        target = str(r.get("target") or "").strip()
        relation = str(r.get("relation") or "").strip()
        if not source or not target or not relation or source == target:
            continue
        if source not in known_set or target not in known_set:
            continue  # defensive — see docstring; the schema enum should already prevent this
        out.append({"source": source, "target": target, "relation": relation})
    return out


# Photos of physical/scanned character sheets or handouts → structured drafts.
# Both functions below reuse the exact same JSON-schema-constrained-chat
# contract as parse_entity_from_text (ValueError on any failure; a plain dict
# on success; nothing written to the database — see /api/import/execute for
# that). The only difference is the user message carries base64 "images"
# instead of raw text, which requires a vision-capable Ollama model (e.g.
# llama3.2-vision, llava, qwen2-vl, gemma3) — a text-only model will either
# error or simply ignore the images and return a near-empty draft; there's no
# reliable way to detect vision capability up front (KNOWN_MODELS doesn't tag
# it), so the caller just surfaces whatever the model returns.
MAX_VISION_IMPORT_IMAGES = 6  # a multi-page/multi-photo sheet at most — bounds prompt size and latency


def _images_to_b64(images: list[bytes]) -> list[str]:
    return [_b64.b64encode(img).decode("ascii") for img in images[:MAX_VISION_IMPORT_IMAGES]]


_CHARACTER_FROM_IMAGES_SYSTEM = (
    "You are transcribing one or more photos of a tabletop RPG character sheet "
    "(the pages may be handwritten, printed, or a mix, and may be out of order) "
    "into structured data for a character-tracking app. Read every visible field "
    "carefully, including handwritten annotations, cross-outs, and margin notes — "
    "prefer the handwritten correction over a crossed-out printed value. Extract "
    "only what is actually shown; leave a field blank rather than inventing or "
    "guessing. \"notes\" must be a complete, well-organized Markdown transcription "
    "of everything on the sheet that doesn't fit the other fields — abilities, "
    "resources/tracks, gear, tools, backstory prompts, session/hunt records, "
    "whatever the sheet contains — grouped under short headings, so nothing on "
    "the sheet is lost even if the game system doesn't match the other fields "
    "below. Respond with JSON only."
)

_CHARACTER_FROM_IMAGES_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "player_name": {"type": "string"},
        "race": {"type": "string"},
        "char_class": {"type": "string"},
        "level": {"type": "integer"},
        "xp": {"type": "integer"},
        "backstory": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["name"],
}


async def parse_character_from_images(images: list[bytes], hint: str = "", model: str = "") -> dict:
    """Turn photo(s) of a character sheet into a draft PlayerCharacter — same
    JSON-schema-constrained pattern as parse_entity_from_text, with the images
    attached to the user message instead of raw text. Raises ValueError on any
    failure. Deliberately maps only the handful of PlayerCharacter fields that
    are meaningful across every game system (name/race/class/level/xp/
    backstory) plus a catch-all "notes" transcription — see
    _CHARACTER_FROM_IMAGES_SYSTEM's own reasoning for why homebrew resource
    tracks/abilities are transcribed into notes rather than force-fit onto
    fixed D&D-shaped columns. Does not write anything to the database itself
    (see main.py's /api/import/execute, kind="player_character", which already
    knows how to write this exact shape via _upsert_player_character)."""
    if not images:
        raise ValueError("No images provided.")
    m = model or effective_ollama_model()
    user_text = hint.strip() or "Transcribe this character sheet."
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _CHARACTER_FROM_IMAGES_SYSTEM},
                {"role": "user", "content": user_text, "images": _images_to_b64(images)},
            ],
            format=_CHARACTER_FROM_IMAGES_SCHEMA,
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        if not isinstance(parsed, dict) or not str(parsed.get("name") or "").strip():
            raise ValueError
        return {
            "name": str(parsed["name"]).strip(),
            "player_name": str(parsed.get("player_name") or "").strip(),
            "race": str(parsed.get("race") or "").strip(),
            "char_class": str(parsed.get("char_class") or "").strip(),
            "level": max(1, min(20, int(parsed.get("level") or 1))) if str(parsed.get("level") or "").strip() else 1,
            "xp": max(0, int(parsed.get("xp") or 0)) if str(parsed.get("xp") or "").strip() else 0,
            "backstory": str(parsed.get("backstory") or "").strip(),
            "notes": str(parsed.get("notes") or "").strip(),
        }
    except Exception as exc:
        raise ValueError("Could not read a character off that photo — try a clearer/closer picture, or a different model.") from exc


_ENTITY_FROM_IMAGES_SYSTEM = (
    "You turn photo(s) of a document — a handout, map key, printed page, or "
    "handwritten notes — into a single structured world-building entity for a "
    "tabletop RPG GM's toolkit, whichever kind the document is actually "
    "describing (a character/NPC, location, organization, creature, event, "
    "item, feat, race, or profession). Read every visible detail carefully, "
    "including handwritten annotations. Extract only what's shown or clearly "
    "implied; do not invent unrelated details. \"body\" should be the entity's "
    "full write-up in Markdown (history, description, stats — whatever's "
    "relevant, transcribed from the photo); \"summary\" is a single-sentence "
    "one-liner. Respond with JSON only."
)


async def parse_entity_from_images(images: list[bytes], kinds: list[str], hint: str = "", model: str = "") -> dict:
    """Turn photo(s) of a document into a draft world Entity — the vision
    sibling of parse_entity_from_text, reusing the exact same schema/response
    shape (see _entity_from_text_schema) so callers/consumers of the draft
    (the review UI, /api/import/execute kind="entity_single") don't need to
    know which path produced it. Raises ValueError on any failure; writes
    nothing itself."""
    if not images:
        raise ValueError("No images provided.")
    m = model or effective_ollama_model()
    user_text = hint.strip() or "Read this document and draft a world entity from it."
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _ENTITY_FROM_IMAGES_SYSTEM},
                {"role": "user", "content": user_text, "images": _images_to_b64(images)},
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
        raise ValueError("Could not turn that photo into an entity — try a clearer/closer picture, or a different model.") from exc


_FIND_REPLACE_SYSTEM = (
    "You turn a tabletop RPG GM's plain-language content-edit instruction into "
    "a literal find-and-replace pair that a program will apply verbatim across "
    "the world's entities, notes, and characters — you do NOT rewrite or "
    "paraphrase any content yourself, and you never see the content being "
    "changed. Extract the exact text to search for (\"find\") and the exact "
    "text to replace it with (\"replace\"), preserving the instruction's own "
    "spelling and capitalization exactly — do not correct, retitle, or "
    "rephrase either one. If the instruction genuinely reduces to one literal "
    "find-and-replace, set \"understood\" to true. If it asks for something "
    "more than that — rewriting prose, several unrelated changes, a "
    "conditional or context-dependent edit — set \"understood\" to false and "
    "briefly say what's unsupported in \"note\". Respond with JSON only."
)

_FIND_REPLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "understood": {"type": "boolean"},
        "find": {"type": "string"},
        "replace": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["understood"],
}


async def parse_find_replace_instruction(instruction: str, model: str = "") -> dict:
    """Turns a natural-language content-edit instruction (e.g. "change the
    race name Skinwalker to Ashwalker everywhere") into a literal {find,
    replace} pair for app.routers.bulk_edit's GM/Assistant "AI-assisted find
    & replace" tool. This is the model's ONLY job here — it never sees the
    world's actual entities/notes/characters and never rewrites their
    content; the search and substitution themselves are a deterministic
    string operation the caller performs (see that module's _apply_replace)
    against a GM-reviewed preview, precisely so a hallucinated or malformed
    reply can't corrupt content — at worst it produces a wrong/empty find
    term, which the preview step will simply show as "no matches." Raises
    ValueError on any failure; writes nothing itself."""
    m = model or effective_ollama_model()
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _FIND_REPLACE_SYSTEM},
                {"role": "user", "content": instruction},
            ],
            format=_FIND_REPLACE_SCHEMA,
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        if not isinstance(parsed, dict):
            raise ValueError
        find = str(parsed.get("find") or "").strip()
        understood = bool(parsed.get("understood")) and bool(find)
        return {
            "understood": understood,
            "find": find,
            "replace": str(parsed.get("replace") or "").strip(),
            "note": str(parsed.get("note") or "").strip(),
        }
    except Exception as exc:
        raise ValueError("The model returned malformed JSON — try again or switch models.") from exc


# Multi-category batch extraction — the sibling of parse_entity_from_text for
# "paste a whole document (session notes, a homebrew page, a wiki dump) and
# get back everything worth tracking as separate world content" rather than
# one passage -> one entity. Same draft-then-confirm contract: raises
# ValueError on failure, writes nothing — the caller reviews the batch, then
# POSTs the confirmed set to /api/import/execute (kind="batch").
MAX_BATCH_ENTITIES = 25  # bounds the response size and keeps the review list human-reviewable
MAX_BATCH_TEXT_CHARS = 20000  # generous for a session's worth of notes, bounded against blowing the model's context

_ENTITIES_BATCH_SYSTEM = (
    "You read a passage of tabletop RPG text — session notes, a homebrew "
    "document, a wiki page, anything — and extract EVERY distinct thing it "
    "describes that's worth tracking as separate world content: NPCs/"
    "characters, locations, organizations, creatures, events, items, feats, "
    "races, professions, and player characters. Put lore/NPC content in "
    "\"entities\" — each with a \"kind\" (character, location, organization, "
    "creature, event, item, feat, race, or profession), \"name\", a "
    "one-sentence \"summary\", and the full write-up in Markdown as \"body\". "
    "Put anything that's clearly a PLAYER's own character sheet in "
    "\"player_characters\" instead (name, race, class, level, backstory, "
    "notes) — never the same thing in both. Extract only what's stated or "
    "clearly implied by the text; do not invent unrelated details, and do "
    "not split one thing into several entries or merge distinct things into "
    "one. If nothing in the text is worth extracting, return empty arrays. "
    "Respond with JSON only."
)


def _entities_batch_schema(kinds: list[str]) -> dict:
    entity_item = {
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
    pc_item = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "player_name": {"type": "string"},
            "race": {"type": "string"},
            "char_class": {"type": "string"},
            "level": {"type": "integer"},
            "xp": {"type": "integer"},
            "backstory": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["name"],
    }
    return {
        "type": "object",
        "properties": {
            "entities": {"type": "array", "items": entity_item},
            "player_characters": {"type": "array", "items": pc_item},
        },
        "required": ["entities", "player_characters"],
    }


async def parse_entities_batch_from_text(raw_text: str, kinds: list[str], model: str = "") -> dict:
    """Extracts every distinct entity/player-character described in a
    passage of text in ONE call — see this module's own note above. Returns
    {"entities": [...], "player_characters": [...]} (either may be empty);
    each entity dict has the exact same shape parse_entity_from_text
    returns, and each player_character dict the same shape
    parse_character_from_images returns, so the review UI/commit path
    (POST /api/import/execute) can treat a batch item identically to a
    single-item draft. Raises ValueError on any failure."""
    m = model or effective_ollama_model()
    text = raw_text[:MAX_BATCH_TEXT_CHARS]
    try:
        resp = await _client().chat(
            model=m,
            messages=[
                {"role": "system", "content": _ENTITIES_BATCH_SYSTEM},
                {"role": "user", "content": text},
            ],
            format=_entities_batch_schema(kinds),
            **(await _chat_kwargs(model=m)),
        )
    except _ollama.ResponseError as exc:
        raise ValueError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        if not isinstance(parsed, dict):
            raise ValueError
        entities = []
        for item in (parsed.get("entities") or [])[:MAX_BATCH_ENTITIES]:
            if not isinstance(item, dict) or item.get("kind") not in kinds or not str(item.get("name") or "").strip():
                continue
            entities.append({
                "kind": item["kind"],
                "subtype": str(item.get("subtype") or "").strip(),
                "name": str(item["name"]).strip(),
                "summary": str(item.get("summary") or "").strip(),
                "body": str(item.get("body") or "").strip(),
                "tags": str(item.get("tags") or "").strip(),
                "folder": str(item.get("folder") or "").strip(),
                "visible_to_players": bool(item.get("visible_to_players", True)),
            })
        player_characters = []
        for item in (parsed.get("player_characters") or [])[:MAX_BATCH_ENTITIES]:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            player_characters.append({
                "name": str(item["name"]).strip(),
                "player_name": str(item.get("player_name") or "").strip(),
                "race": str(item.get("race") or "").strip(),
                "char_class": str(item.get("char_class") or "").strip(),
                "level": max(1, min(20, int(item.get("level") or 1))) if str(item.get("level") or "").strip() else 1,
                "xp": max(0, int(item.get("xp") or 0)) if str(item.get("xp") or "").strip() else 0,
                "backstory": str(item.get("backstory") or "").strip(),
                "notes": str(item.get("notes") or "").strip(),
            })
        return {"entities": entities, "player_characters": player_characters}
    except Exception as exc:
        raise ValueError("Could not extract anything usable from that text — try a shorter passage, or a different model.") from exc


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
    assumed default context.

    Finally the degeneration guard applies here too
    (_recap_num_predict_default_if_unbounded): when nothing else bounded the
    generation, _RECAP_NUM_PREDICT_DEFAULT caps it — an expanded recap is
    the same few-paragraphs-length prose a facts recap is, so the same
    reported loop-forever failure mode applies here unchanged."""
    system = _with_instructions(_EXPAND_NOTES_SYSTEM, extra_instructions)
    thinking_opts = _thinking_num_predict_override(think)
    opts = dict(thinking_opts)
    _recap_num_predict_default_if_unbounded(opts)
    # `thinking_opts` (not `opts`, which the guard just ensured carries a
    # num_predict one way or another) keeps the reserve gated on the
    # THINKING widening actually firing, same semantics as
    # summarize_session_from_facts above.
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if thinking_opts else 0)
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
    "simple. Respond with the recap text only, no preamble or commentary. Write the recap in "
    "English, regardless of the language the facts are written in."
)

# Hard cap on this function's generation length when NOTHING else supplied a
# num_predict — neither the GM's Settings > System "Max output tokens" nor
# _thinking_num_predict_override's widening of one. Ollama's own default is
# -1 (unlimited), and a recap call without a cap has a reported, ugly
# failure mode: one GM's Session Log recap degenerated into the same
# sentence repeated dozens of times, leaked a raw "<|im_start|>user"
# chat-template token, then spiraled into infinite digit strings
# ("a147c503573688711905232000000…", "…9999999…") for thousands of tokens —
# nothing stopped the loop except the context window filling up. A recap is
# a few short paragraphs by definition, so 1024 tokens (~4x a normal recap,
# roughly the same chars-per-token math _CONTEXT_FIT_RESERVED_TOKENS uses)
# is generous room for prose and hidden thinking alike while guaranteeing
# the generation actually ENDS. Deliberately a fallback, never an override:
# a GM's configured num_predict reaches Ollama through _chat_kwargs' options
# merge and must not be clobbered, and _thinking_num_predict_override's
# widened value (configured + _THINKING_HEADROOM_TOKENS) wins over both so
# a thinking model keeps its headroom.
_RECAP_NUM_PREDICT_DEFAULT = 1024


def _recap_num_predict_default_if_unbounded(opts: dict) -> None:
    """Degeneration guard (see _RECAP_NUM_PREDICT_DEFAULT): set the default
    cap on `opts` in place when NOTHING else supplied a num_predict — not
    the caller (an opts["num_predict"] that arrived via a per-call
    `options=` dict, condense_recap's max_tokens hard cap, or the expanded-
    thinking rung), not _thinking_num_predict_override's widening of a
    configured value, and not the GM's own Settings > System "Max output
    tokens" (checked here so a configured cap still reaches Ollama through
    _chat_kwargs' options merge unmolested — clobbering a lower configured
    value with this default would silently RAISE the GM's cap, and the
    widened configured+headroom value legitimately wants to win over both).
    The configured-value test mirrors _thinking_num_predict_override's own
    "is a bounded num_predict configured?" check exactly. Shared by every
    recap-family function (summarize_session_from_facts, expand_recap_notes,
    condense_recap, summarize_transcript) so the four call sites can never
    drift apart on when the fallback applies."""
    if "num_predict" in opts:
        return
    configured = effective_ollama_options().get("num_predict")
    if not configured or configured < 0:
        opts["num_predict"] = _RECAP_NUM_PREDICT_DEFAULT


async def summarize_session_from_facts(
    facts: list[str], model: str = "", extra_instructions: str = "", think: bool = True,
    world_context: str = "", min_tokens: int | None = None, max_tokens: int | None = None,
    strictness: str = "guideline",
) -> str:
    """Weave a list of discrete session facts (see the Facts feature, which
    logs these per-session) into a readable narrative recap. `extra_instructions`
    is a GM's steering (e.g. World.recap_instructions — "write in Spanish"),
    same as summarize_transcript's own parameter of the same name. `think`
    defaults to True — see expand_recap_notes's docstring for why this
    family of functions differs from generate_chat's own plain default.

    `world_context`, if given, is RAG-retrieved World lore/Notes text (see
    app.audio_jobs._build_rag_context), prepended ahead of everything else
    via _with_world_context — exactly how condense_recap frames its own.
    The recap is generated FROM the session's facts, so the retrieved lore
    is purely supplementary reference material (spelling an established
    name right), never a second content source — the framing text says so.

    `min_tokens`/`max_tokens`/`strictness` are condense_recap's own
    length-target/steering knobs, given the Session Log recap the same
    customization the Condense feature already has — see condense_recap's
    own docstring for the full guideline-vs-firm/strict rationale and the
    max_tokens/think num_predict interaction, both shared verbatim via
    _strictness_base_system/_length_target_addendum. Unlike condense_recap,
    there's no `options` passthrough or expanded-thinking retry rung here
    (this function has never had either), so the num_predict merge below is
    a shorter version of condense_recap's own.

    Also sizes num_ctx via _ctx_override_if_needed — a fact-heavy session
    (the player session-log route sends every fact, uncapped) could
    otherwise silently truncate at the configured/default context; this
    used to pass no num_ctx override at all, unlike condense_recap/
    summarize_transcript. See expand_recap_notes's own docstring for why
    the extra reserve is gated on num_predict actually having been widened,
    not on bare `think`.

    Finally, an unbounded run is prevented outright: when neither the GM's
    configured options nor _thinking_num_predict_override supply a
    num_predict, _RECAP_NUM_PREDICT_DEFAULT is set so a degenerating model
    cannot loop forever (see that constant's comment for the reported
    failure). The same guard now covers this function's recap-family
    siblings (expand_recap_notes/condense_recap/summarize_transcript) via
    _recap_num_predict_default_if_unbounded — each slots it in at its own
    merge point, standing down whenever its max_tokens/expanded-rung/caller
    machinery already set a num_predict."""
    if not facts:
        return ""
    _validate_strictness(strictness)
    bullet_list = "\n".join(f"- {f}" for f in facts)
    base_system = _strictness_base_system(_SUMMARIZE_FACTS_SYSTEM, strictness, extra_instructions)
    system = _with_world_context(_with_instructions(base_system, extra_instructions), world_context)
    system += _length_target_addendum(
        min_tokens, max_tokens, strictness, _chars_per_token_estimate(bullet_list),
        noun="session recap", source_noun="facts",
    )
    thinking_opts = _thinking_num_predict_override(think)
    opts = {}
    if max_tokens and not think:
        opts["num_predict"] = max_tokens
    opts.update(thinking_opts)
    _recap_num_predict_default_if_unbounded(opts)
    # `thinking_opts` (not "num_predict" in opts — the guard just guaranteed
    # that is now always true) preserves the pre-guard reserve semantics:
    # the extra num_ctx headroom applies only when the THINKING widening
    # actually fired.
    reserve = _CONTEXT_FIT_RESERVED_TOKENS + (_THINKING_HEADROOM_TOKENS if thinking_opts else 0)
    opts.update(_ctx_override_if_needed(system + bullet_list, reserve))
    recap = await generate_chat(
        [{"role": "user", "content": bullet_list}], system=system, model=model,
        options=opts or None, think=think,
    )
    # A degenerating local model (repetition loops, leaked chat-template
    # tokens like <|im_start|>, endless digit runs, a hallucinated new
    # conversation once it runs out of real facts to narrate — the reported
    # failures) produces output that is worse than no recap. Clean the
    # mechanical artifacts, and if the result STILL reads as degenerate
    # (repetitive, or had to be cut short), retry once with a raised
    # repeat_penalty (only when the GM hasn't configured one) and keep
    # whichever attempt reads less degenerate. Best-effort: never raises,
    # and a model that loops even under the penalty still returns its
    # (bounded) output.
    recap, truncated = _clean_degenerate_recap(recap)
    if (truncated or _recap_degeneration_ratio(recap) > 0.4) and "repeat_penalty" not in opts:
        retry_opts = dict(opts)
        retry_opts["repeat_penalty"] = 1.2
        retry_recap, retry_truncated = _clean_degenerate_recap(await generate_chat(
            [{"role": "user", "content": bullet_list}], system=system, model=model,
            options=retry_opts, think=think,
        ))
        retry_ratio, orig_ratio = _recap_degeneration_ratio(retry_recap), _recap_degeneration_ratio(recap)
        if retry_ratio < orig_ratio or (retry_ratio == orig_ratio and not retry_truncated and truncated):
            recap, truncated = retry_recap, retry_truncated
    return recap


# Any <|...|>-shaped special token a GGUF chat template might leak
# (<|im_start|>, <|im1_start|>, <|endoftext|>, <|channel|>, <|think|>, and
# any model- or export-specific variant not seen yet) — a regex sweep
# instead of a fixed list, since a hardcoded list only ever catches tokens
# someone already reported.
_CHAT_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^<>|]{0,40}\|>")
# A bare role-marker line — the shape a chat template renders a turn
# boundary as once its special tokens are stripped (see
# _clean_degenerate_recap's own docstring for the failure this catches).
_CHAT_ROLE_MARKER_RE = re.compile(r"^(system|user|assistant|model)$", re.IGNORECASE)


def _clean_degenerate_recap(recap: str) -> tuple[str, bool]:
    """Strip the mechanical artifacts of a degenerating local model from a
    generated recap, and report whether doing so had to cut the output
    short. Two failure shapes handled:

    - Leaked chat-template special tokens (_CHAT_TEMPLATE_TOKEN_RE's <|...|>
      shape, plus the handful of Gemma-style angle-bracket turn markers)
      are simply removed wherever they appear.
    - A bare "system"/"user"/"assistant"/"model" line, once real content
      already precedes it, marks the start of a HALLUCINATED new
      conversation: a thin session (few facts logged) gives the model
      little to narrate, and rather than stopping, it fills the rest of
      its num_predict budget by simulating an entirely different, unrelated
      exchange — reported as a recap that ended mid-sentence, then
      continued into a fake "extract eat/drink sentences" system prompt
      with invented user/assistant turns and unrelated C code. Everything
      from that marker onward is not the recap, so it's dropped rather
      than shown to the GM; the caller treats this as worth a retry (see
      summarize_session_from_facts) even when nothing repeats, since a
      truncated-but-non-repetitive recap wouldn't otherwise trip
      _recap_degeneration_ratio.

    Also collapses any run of 2+ identical consecutive lines (the older,
    already-reported repetition-loop failure) — line-level, whitespace-
    normalized comparison, so formatting variations of the same looped
    line still collapse.

    Returns (cleaned_text, truncated)."""
    if not recap:
        return recap, False
    recap = _CHAT_TEMPLATE_TOKEN_RE.sub("", recap)
    for token in ("<start_of_turn>", "<end_of_turn>"):
        recap = recap.replace(token, "")
    lines = recap.split("\n")
    cleaned = []
    truncated = False
    for line in lines:
        stripped = line.strip()
        if _CHAT_ROLE_MARKER_RE.match(stripped) and any(ln.strip() for ln in cleaned):
            truncated = True
            break
        # Collapse any run of 2+ identical consecutive lines to one.
        if stripped and cleaned and cleaned[-1].strip() == stripped:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).rstrip(), truncated


def _recap_degeneration_ratio(recap: str) -> float:
    """Fraction of non-empty lines that duplicate an earlier non-empty line
    (order-preserving, whitespace-normalized). ~0 is a healthy recap;
    >0.4 is the repetition-loop failure the reported screenshots showed.
    Purely heuristic — used only to pick between two generation attempts,
    never to reject content."""
    lines = [ln.strip() for ln in recap.split("\n") if ln.strip()]
    if len(lines) < 4:
        return 0.0
    seen, dupes = set(), 0
    for line in lines:
        key = re.sub(r"\s+", " ", line.lower())
        if key in seen:
            dupes += 1
        else:
            seen.add(key)
    return dupes / len(lines)


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
    # Same degeneration guard its four recap-family siblings apply (audit
    # 2026-09-30, chat finding 10): with think=False and nothing configured,
    # an unbounded num_predict lets a repetition loop run until the context
    # fills — for minutes on a CPU box.
    _recap_num_predict_default_if_unbounded(opts)
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

# Shared by every recap function with a strictness/length-target knob
# (condense_recap, summarize_session_from_facts) — factored out once two
# callers needed the identical wording/behavior, rather than the "different
# reimplementation of the other" carve-out app.audio_jobs uses for its own
# purpose-specific job-runner branches (those differ in real ways; this
# doesn't).
_STRICT_COMPLIANCE_NOTE = "\n\nTreat the extra instructions below as binding requirements, not suggestions."


def _validate_strictness(strictness: str) -> None:
    if strictness not in ("guideline", "firm", "strict"):
        raise ValueError(f"strictness must be guideline, firm, or strict, got {strictness!r}")


def _strictness_base_system(base_system: str, strictness: str, extra_instructions: str) -> str:
    """For "firm"/"strict" strictness with a non-blank extra_instructions,
    appends a compliance line telling the model those instructions are
    binding — see condense_recap's own docstring for why a model told a
    length is mandatory otherwise still treats a softer instruction as
    license to undershoot. Must run BEFORE _with_instructions folds
    extra_instructions in, so the compliance line's "below" is literally
    true."""
    if strictness in ("firm", "strict") and (extra_instructions or "").strip():
        return base_system + _STRICT_COMPLIANCE_NOTE
    return base_system


def _length_target_addendum(
    min_tokens: int | None, max_tokens: int | None, strictness: str,
    chars_per_token: int, noun: str, source_noun: str,
) -> str:
    """Builds the trailing "Length target/requirements for the {noun}: ..."
    system-prompt addition for min_tokens/max_tokens — see condense_recap's
    own docstring for the full guideline-vs-firm/strict wording rationale.
    `source_noun` names what the model should pull more detail from to
    reach a min_tokens floor (e.g. "recap" for condense, "facts" for a
    facts-based session recap). Returns "" when neither bound is set."""
    length_notes = []
    if min_tokens:
        if strictness == "guideline":
            length_notes.append(
                f"at least ~{min_tokens} tokens (~{min_tokens * chars_per_token} characters) — "
                "don't cut it any shorter than that even if you could say it in fewer words"
            )
        else:
            length_notes.append(
                f"REQUIRED: the {noun} MUST be at least ~{min_tokens} tokens "
                f"(~{min_tokens * chars_per_token} characters) — a shorter output is a failed "
                f"request; expand with specific detail (scene beats, names, consequences) from "
                f"the {source_noun} to reach it"
            )
    if max_tokens:
        if strictness == "guideline":
            length_notes.append(f"no more than ~{max_tokens} tokens (~{max_tokens * chars_per_token} characters)")
        else:
            length_notes.append(
                f"REQUIRED: stay at or below ~{max_tokens} tokens (~{max_tokens * chars_per_token} "
                "characters); trim detail rather than exceeding it"
            )
    if not length_notes:
        return ""
    heading = "Length target" if strictness == "guideline" else "Length requirements"
    return f"\n\n{heading} for the {noun}: " + " and ".join(length_notes) + "."


async def condense_recap(
    recap: str, model: str = "", options: dict = None, think: bool = True,
    extra_instructions: str = "", min_tokens: int | None = None, max_tokens: int | None = None,
    world_context: str = "", expanded_thinking: bool = False,
    strictness: str = "guideline",
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

    With think=True, a GM-configured instance-wide num_predict (Settings >
    System > "Max output tokens") still reaches Ollama unwidened via
    _chat_kwargs' own options merge — unlike max_tokens above, this
    function never asked for that one to be capped at all, so leaving it
    alone would repeat the exact failure this whole family already guards
    against elsewhere (see _thinking_num_predict_override's docstring).
    _thinking_num_predict_override(think) is applied unconditionally below
    to close that gap; it's already a no-op for think=False or an unset/
    unlimited configured value, so it never interferes with the max_tokens
    branch above.

    The degeneration guard applies as the LAST step of that merge
    (_recap_num_predict_default_if_unbounded): when neither max_tokens'
    hard cap, the expanded-thinking rung, a caller-supplied `options`
    entry, a GM-configured cap, nor the thinking widening set a
    num_predict, _RECAP_NUM_PREDICT_DEFAULT does — a condensed recap is a
    few sentences, so an unbounded degenerating loop has no legitimate
    use case here (see that constant's comment for the reported failure).

    `world_context`, if given, is RAG-retrieved World lore/Notes text
    (see app.audio_jobs._build_rag_context) prepended ahead of everything
    else — see _with_world_context's own docstring.

    `expanded_thinking` (default False) is the retry-ladder's recovery rung
    (see app.audio_jobs._run_job and docs/DYNAMIC_THINKING_AND_PIPELINE_
    PLAN.md Part 1): when True, num_predict is set from
    expanded_thinking_options() instead of _thinking_num_predict_override's
    normal headroom, overriding max_tokens' own num_predict branch above
    too — the expanded rung only ever runs because a prior attempt already
    starved, so the usual "max_tokens caps num_predict when think=False"
    behavior is deliberately bypassed here in favor of guaranteed room.

    `strictness` (default "guideline") picks how hard the length targets
    (and the GM's extra instructions) are phrased: "guideline" is the
    original soft "at least ~X tokens ... don't cut it any shorter"
    guidance, worded as a suggestion the model may weigh against its own
    sense of a good summary; "firm"/"strict" reword the same targets as
    mandatory requirements ("MUST be at least...", "REQUIRED: stay at or
    below...") and — when extra_instructions is present — add an explicit
    "these are binding, not suggestions" compliance line, since a model
    that's been told a length is mandatory will otherwise still treat a
    softer instruction ("keep it tight") as license to undershoot.
    "firm" and "strict" build the SAME prompt here; the difference is
    purely out-of-band: the job runner (_run_job) estimates the finished
    recap's tokens and auto-retries once when a "strict" job's result
    lands outside the requested range — see app.audio_jobs._run_job.
    Anything else raises ValueError, so a typo'd caller fails fast
    instead of silently degrading to best-effort."""
    _validate_strictness(strictness)
    base_system = _strictness_base_system(_CONDENSE_RECAP_SYSTEM, strictness, extra_instructions)
    system = _with_world_context(_with_instructions(base_system, extra_instructions), world_context)
    system += _length_target_addendum(
        min_tokens, max_tokens, strictness, _chars_per_token_estimate(recap),
        noun="condensed recap", source_noun="recap",
    )
    opts = dict(options) if options else {}
    if max_tokens and not think:
        opts["num_predict"] = max_tokens
    if expanded_thinking:
        opts["num_predict"] = expanded_thinking_options()["num_predict"]
    else:
        opts.update(_thinking_num_predict_override(think))
    # Degeneration guard, deliberately AFTER every other num_predict source
    # above so each of them wins over the default (see
    # _recap_num_predict_default_if_unbounded).
    _recap_num_predict_default_if_unbounded(opts)
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
# Ceiling for every AUTO-SIZED num_ctx this module computes (context_sized_
# options, expanded_thinking_options — anything that derives num_ctx from
# input length rather than a GM's own explicit setting). Without this, a
# pathological paste (e.g. a multi-megabyte transcript dropped into a
# Condense/Summarize field) computes a six-figure num_ctx that Ollama will
# try to allocate real KV-cache memory for. A GM's own explicit Settings >
# System num_ctx is NOT clamped here — only what this module derives on its
# own. Env-tunable (same idiom as THINKING_HEADROOM_TOKENS) for an install
# with the VRAM to genuinely want more; floored at 8192 so lowering it can't
# accidentally make every auto-sized call smaller than a normal request.
MAX_AUTO_NUM_CTX = max(8192, int(os.getenv("MAX_AUTO_NUM_CTX", "32768")))

# Generous assumed budget for a thinking-enabled model's hidden reasoning,
# on top of the visible answer's own max_tokens target — see condense_recap's
# own docstring for why max_tokens can't safely double as num_predict's hard
# cap once think=True, and condense_call_options' docstring for why num_ctx
# needs matching headroom.
#
# Deliberately NOT raised as a blanket default, and deliberately not a
# Settings field or a per-model adaptive value — bigger headroom means
# SMALLER transcript chunks in _transcript_chunk_char_budget (more AI calls
# per session recap), and under the common unconfigured
# _DEFAULT_ASSUMED_CTX_TOKENS (4096) the chunk budget already sits on its
# 500-token floor at this value; doubling it wouldn't shrink chunks further
# there but WOULD at any larger configured num_ctx (e.g. 8192 collapses from
# ~2900 usable input tokens per chunk to the same 500-token floor — roughly
# 6x more calls for the same transcript). 4096 already comfortably covers
# the thinking length observed in the production failure that motivated
# this whole mechanism (7781 chars ≈ 1,945-3,890 tokens depending on
# script — see _chars_per_token_estimate). Env-tunable (same idiom as
# STT_JOB_CONCURRENCY above) as an escape hatch for an install that
# genuinely needs more, without changing the shipped default for everyone
# — check generate_chat's own "thinking_chars=" log line (see its
# _log.warning call) across a few failures before raising this.
_THINKING_HEADROOM_TOKENS = max(0, int(os.getenv("THINKING_HEADROOM_TOKENS", "4096")))

# The recovery rung for when _THINKING_HEADROOM_TOKENS above still wasn't
# enough — see docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1 for the
# production failure that motivated this (a model that ignored think=False
# and starved again, on a fallback attempt whose budget was smaller than
# the one that had just failed). 12288 ≈ 3x the normal headroom — 3-6x the
# worst observed failure. Env-tunable, same idiom as THINKING_HEADROOM_TOKENS
# above; deliberately not a Settings field (an escape hatch for a rare
# recovery rung, not a knob meant to be tuned per run). This rung costs
# real VRAM/RAM: widening num_ctx by the same delta adds roughly 8k tokens
# of KV cache (~1.5-2.5 GB at f16 on a ~26B model) for that one call —
# Ollama offloads to system RAM if VRAM is short rather than failing, so
# the rung runs slower instead of erroring, which is the accepted price of
# a last-resort recovery path.
_THINKING_EXPANDED_HEADROOM_TOKENS = max(
    _THINKING_HEADROOM_TOKENS,
    int(os.getenv("THINKING_EXPANDED_HEADROOM_TOKENS", "12288")),
)


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


def expanded_thinking_options() -> dict:
    """Options for a retry after is_thinking_starved_sentinel already fired
    once at the normal headroom (_thinking_num_predict_override) — public
    since both app.audio_jobs's retry ladder and app.routers.ai call it,
    same promotion reasoning as merge_glossary.

    Uses a large EXPLICIT num_predict rather than Ollama's -1 ("generate
    until natural stop or context exhausted"): -1's behavior once context
    actually runs out is Ollama-version-dependent (older versions
    context-shift and keep going, newer ones stop with
    done_reason="length"), which is exactly the kind of version-dependent
    behavior this codebase avoids relying on. An explicit cap gives the
    same effective headroom with a guaranteed stop, so a recovery attempt
    can never "hang with no signal" longer than the cap allows.

    Also returns an explicitly enlarged num_ctx, sized off whatever num_ctx
    is actually configured (or _DEFAULT_ASSUMED_CTX_TOKENS if not) — both
    num_predict AND num_ctx can independently starve a thinking model (see
    docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1), so a real recovery
    rung has to widen both knobs, not just the output cap.

    The num_ctx delta is (EXPANDED - NORMAL), not the full expanded value —
    deliberate, so that on summarize_transcript's chunked path, chunk_chars
    (computed against the NORMAL headroom, unaffected by this function)
    stays byte-identical between a normal attempt and an expanded retry,
    letting an expanded retry resume a checkpoint the normal attempt already
    wrote instead of redoing already-summarized parts."""
    opts = effective_ollama_options()
    base_ctx = opts.get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    configured_predict = opts.get("num_predict") or 0
    return {
        "num_predict": max(configured_predict, 0) + _THINKING_EXPANDED_HEADROOM_TOKENS,
        "num_ctx": min(MAX_AUTO_NUM_CTX, base_ctx + (_THINKING_EXPANDED_HEADROOM_TOKENS - _THINKING_HEADROOM_TOKENS)),
    }


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
    touched in the first place.

    Clamped to MAX_AUTO_NUM_CTX — an unbounded paste would otherwise
    compute a num_ctx Ollama tries to allocate real KV-cache memory for.
    Clamping alone reintroduces the silent-truncation risk this function
    exists to prevent for a caller whose actual input exceeds the ceiling
    (Ollama truncates the prompt instead of erroring) — see
    docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 3.3: the
    condense entry points refuse oversized input outright instead of
    relying on this clamp alone."""
    chars_per_token = _chars_per_token_estimate(text)
    input_tokens = -(-len(text) // chars_per_token)  # ceil division
    needed = max(_CONTEXT_FIT_FLOOR_TOKENS, input_tokens + reserve_tokens)
    return {"num_ctx": min(MAX_AUTO_NUM_CTX, _round_up_num_ctx(needed))}


def _round_up_num_ctx(tokens: int) -> int:
    """Rounds `tokens` up to the next power-of-two bucket starting at
    _CONTEXT_FIT_FLOOR_TOKENS (1024, 2048, 4096, ...) instead of returning
    the exact byte-for-byte-fitted value context_sized_options would
    otherwise compute.

    The real bug this fixes: Ollama allocates a model's KV-cache at
    load time, sized to the exact num_ctx it was asked for — a SECOND
    call with even a slightly different num_ctx (one token longer input
    is enough) doesn't reuse that allocation, it reloads the model from
    scratch to reallocate it, the multi-second-plus cost keep_alive/
    "keep the model warm between calls" exists specifically to avoid.
    Every caller of context_sized_options computes num_ctx from that
    CALL's own input length, and a real conversation's input only ever
    grows turn to turn (more history each time) — so once a chat crosses
    the GM's configured baseline at all, the un-rounded exact-fit value
    used to come out different on nearly every subsequent turn, forcing
    a reload on nearly every message. Bucketing means most turns land on
    the SAME num_ctx as the turn before (only crossing into the next
    bucket occasionally, still well ahead of ever truncating), so the
    model stays loaded and warm the way it's supposed to."""
    bucket = _CONTEXT_FIT_FLOOR_TOKENS
    while bucket < tokens:
        bucket *= 2
    return bucket


def _ctx_override_if_needed(text: str, reserve_tokens: int) -> dict:
    """Like condense_call_options' own "only step in when actually needed"
    behavior, but for the two recap-family callers (summarize_session_from_
    facts, expand_recap_notes) that previously passed NO num_ctx override at
    all — a fact-heavy session (the player session-log route sends every
    fact, uncapped) or a huge notes paste could silently truncate at the
    configured/default context, the exact garbage-output failure condense_
    call_options' own docstring documents for condense_recap. Returns {}
    (no override — the GM's configured/default context keeps applying
    unchanged) unless the computed requirement genuinely exceeds it."""
    needed = context_sized_options(text, reserve_tokens=reserve_tokens)["num_ctx"]
    baseline = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    return {"num_ctx": needed} if needed > baseline else {}


_SUMMARIZE_TRANSCRIPT_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is a raw speech-to-text transcript of an "
    "actual-play session recording — expect filler words, misheard names, and no punctuation "
    "structure. Turn it into a short, readable narrative recap in flowing prose — a few "
    "paragraphs, past tense, third person. Use your judgment to skip out-of-character chatter, "
    "rules discussion, and filler, keeping only what happened in the story. Don't invent details "
    "that aren't in the transcript. Respond with the recap text only, no preamble or commentary. "
    "Write the recap in English, regardless of the language the transcript is in."
)

_SUMMARIZE_TRANSCRIPT_PART_SYSTEM = (
    "You are a scribe for a tabletop RPG campaign. Below is ONE PART of a longer raw speech-to-text "
    "transcript of an actual-play session recording — expect filler words, misheard names, no "
    "punctuation structure, and this excerpt starting and ending mid-scene. Turn just this part "
    "into a short, readable narrative summary in flowing prose (past tense, third person) — it "
    "will be appended directly after the summaries of the earlier parts (in order) to form the "
    "full session recap, so don't add your own preamble, conclusion, or reference to \"the rest "
    "of the summary\" — just narrate what happened in this part. Skip out-of-character chatter, "
    "rules discussion, and filler. Don't invent details that aren't in the text. Respond with "
    "this part's summary only, no preamble or commentary. Write the part summary in English, "
    "regardless of the language the transcript is in."
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


def _effective_ctx_tokens() -> int:
    """The context window window-first chunk callers budget against. Under
    Unsloth this is the load-time window (llm_context_tokens() — context is
    fixed at model load in llama.cpp, unlike Ollama's per-request num_ctx;
    see the migration plan §6.3), clamped to MAX_AUTO_NUM_CTX. Under the
    legacy Ollama backend it stays the GM's configured num_ctx (Settings >
    System) if set, else the conservative low-end default. Kept as one
    helper (not inlined) because every summarize-path budget goes through
    _transcript_chunk_char_budget, which must derive its chunk size from
    exactly this window — a second copy of the expression could silently
    drift and put a chunk that no longer fits into the enforced context.
    (parse_facts_from_recap is deliberately NOT on this list anymore: its
    chunk calls pin their own num_ctx via _facts_parse_chunk_plan, so a
    small configured/default window shrank its chunks to the 500-token
    floor instead of only bounding what the pin needed to cover — see
    _facts_parse_chunk_plan.)"""
    if effective_llm_api_key():
        return min(MAX_AUTO_NUM_CTX, llm_context_tokens())
    return effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS


def _chunk_reserve_tokens(system: str = "", think: bool = True, extra_reserve_tokens: int = 0) -> int:
    """The context tokens every chunk call must reserve for everything that
    is NOT the chunk's own input: _CHUNK_RESERVED_TOKENS' generic system +
    response + margin budget, the actual system prompt's own tokens (see
    _transcript_chunk_char_budget for why it's estimated, not assumed),
    _THINKING_HEADROOM_TOKENS when think=True, and any caller-specific
    extras (RAG world_context, a JSON response reserve). Factored as ONE
    pure expression so _transcript_chunk_char_budget's budget math and
    parse_facts_from_recap's num_ctx pin (via _facts_parse_chunk_plan) are
    computed from the same numbers — two copies of this arithmetic could
    silently drift apart and put a chunk that no longer fits into the
    enforced context."""
    system_tokens = (len(system) // _chars_per_token_estimate(system)) if system else 0
    reserved = _CHUNK_RESERVED_TOKENS + system_tokens + (_THINKING_HEADROOM_TOKENS if think else 0) + max(0, extra_reserve_tokens)
    return reserved


def _transcript_chunk_char_budget(transcript: str = "", system: str = "", think: bool = True, extra_reserve_tokens: int = 0) -> int:
    """`transcript` (a sample of it) drives the chars-per-token estimate;
    `system` is the system prompt that will accompany each chunk — the GM's
    World.recap_instructions is free text with no length limit, so a long
    standing instruction could itself eat meaningfully into the context
    window that _CHUNK_RESERVED_TOKENS budgets for. Both are optional and
    default to the same fixed English/no-system-prompt assumption this
    function used before they were accounted for.

    `extra_reserve_tokens` (default 0 — summarize_transcript never passes
    it, so its chunk sizes are byte-identical to before) lets a caller
    reserve additional per-chunk context on top of _CHUNK_RESERVED_TOKENS
    for input that rides EVERY chunk call but isn't part of `system` —
    parse_facts_from_recap budgets its RAG world_context (re-sent with
    every chunk) and its JSON response room this way, so chunk + lore +
    response always fits the window the chunk was sized against.

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
    ctx_tokens = _effective_ctx_tokens()
    chars_per_token = _chars_per_token_estimate(transcript)
    reserved = _chunk_reserve_tokens(system, think, extra_reserve_tokens)
    input_tokens = max(500, ctx_tokens - reserved)
    return input_tokens * chars_per_token


def condense_call_options(
    transcript: str, extra_instructions: str = "", world_context: str = "",
    max_tokens: int | None = None, think: bool = True, force_fit: bool = False,
    expanded: bool = False,
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

    `think`, together with `max_tokens` OR a GM-configured instance-wide
    num_predict (Settings > System > "Max output tokens"), widens the
    reserve by _THINKING_HEADROOM_TOKENS: condense_recap stops treating
    max_tokens as a hard num_predict cap once think=True (see its own
    docstring for why — hidden reasoning tokens would otherwise compete
    with the visible answer for that same budget) and ALSO widens a
    configured num_predict the same way (via _thinking_num_predict_
    override), so num_ctx needs generous matching headroom in either case,
    or a long reasoning-plus-answer generation could still hit the SAME
    kind of context-overflow corruption this function exists to prevent —
    just from an uncapped generation instead of an oversized prompt. No
    widening when neither is set: condense_recap never touches num_predict
    in that case either way, so there's nothing extra to make room for
    here.

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
    gets the protection.

    `expanded=True` is the retry-ladder's recovery rung (see
    app.audio_jobs._run_job and docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md
    Part 1): reserves _THINKING_EXPANDED_HEADROOM_TOKENS unconditionally
    (bypassing the think/max_tokens gate above — the expanded rung only
    ever runs because something already starved) and, like force_fit,
    always returns the computed num_ctx even if it doesn't exceed the
    baseline, since the whole point is guaranteeing extra room this time."""
    chars_per_token = _chars_per_token_estimate(transcript)
    extra_chars = len(extra_instructions or "") + len(world_context or "")
    num_predict_configured = effective_ollama_options().get("num_predict")
    if expanded:
        thinking_headroom = _THINKING_EXPANDED_HEADROOM_TOKENS
    else:
        thinking_headroom = (
            _THINKING_HEADROOM_TOKENS
            if think and (max_tokens or (num_predict_configured and num_predict_configured > 0))
            else 0
        )
    reserve = (
        max(_CONTEXT_FIT_RESERVED_TOKENS, (max_tokens or 0) + 256)
        + extra_chars // chars_per_token + thinking_headroom
    )
    needed = context_sized_options(transcript, reserve_tokens=reserve)["num_ctx"]
    if force_fit or expanded:
        return {"num_ctx": needed}
    baseline = effective_ollama_options().get("num_ctx") or _DEFAULT_ASSUMED_CTX_TOKENS
    return {"num_ctx": needed} if needed > baseline else None


def _split_transcript_into_chunks(transcript: str, chunk_chars: int) -> list[str]:
    """Split on a paragraph, line, or sentence boundary near the end of each
    window where one exists, so a chunk doesn't get cut mid-sentence — falls
    back to a hard cut at chunk_chars if no such boundary is found late
    enough in the window to still make meaningful progress.

    A single "\\n" is checked between the paragraph and sentence-punctuation
    candidates because that's the real per-segment separator a speech-to-text
    transcript is joined with — a raw transcript essentially never
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
                                think: bool = True, world_context: str = "",
                                expanded_thinking: bool = False) -> str:
    """Turn a raw speech-to-text transcript (see transcribe_audio) of a session
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
    see _with_world_context's own docstring.

    `expanded_thinking` (default False) is the retry-ladder's recovery rung
    (see app.audio_jobs._run_job and docs/DYNAMIC_THINKING_AND_PIPELINE_
    PLAN.md Part 1): when True, every generate_chat call below uses
    expanded_thinking_options() instead of _thinking_num_predict_override's
    normal headroom. Deliberately does NOT affect chunk_chars below — chunk
    sizing stays computed against the NORMAL headroom regardless, so a
    checkpoint written by a normal (non-expanded) attempt has the identical
    chunk_total/chunk_chars an expanded retry needs to resume it, and
    already-summarized parts are never redone just because thinking starved
    on a later part."""
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
    # expanded_thinking replaces this with a much larger explicit budget
    # instead — see this function's own docstring.
    predict_override = expanded_thinking_options() if expanded_thinking else (_thinking_num_predict_override(think) or None)
    # Degeneration guard (see _RECAP_NUM_PREDICT_DEFAULT), same rule as the
    # recap-family siblings: when neither the expanded rung, the thinking
    # widening, nor a GM-configured cap supplied a num_predict, cap each
    # generate_chat call below (single-call and every chunk part alike) so a
    # degenerating model can't loop forever — a chunk part's summary is even
    # shorter prose than a full recap, so the default covers both paths
    # generously. The expanded rung is skipped: it only ever runs as a
    # recovery from a starved budget, and its own explicit num_predict is
    # the whole point.
    if not expanded_thinking:
        _guarded_predict = dict(predict_override or {})
        _recap_num_predict_default_if_unbounded(_guarded_predict)
        predict_override = _guarded_predict or None
    chunks = _split_transcript_into_chunks(transcript, chunk_chars)
    if len(chunks) <= 1:
        system = _with_world_context(_with_instructions(_SUMMARIZE_TRANSCRIPT_SYSTEM, extra_instructions), world_context)
        return await generate_chat(
            [{"role": "user", "content": transcript}], system=system, model=model,
            options=predict_override, think=think,
        )

    _log.info("summarize_transcript: chunking into %d part(s) (%d chars total)", len(chunks), len(transcript))
    # `system` (part_system, computed once above) is reused byte-for-byte
    # for every chunk's generate_chat call below — this already maximizes
    # Ollama's KV-prefix cache reuse across the whole map-reduce loop (each
    # call's prompt shares an identical prefix with the last, so only the
    # new chunk's own tokens need prefilling) and ollama_job_semaphore
    # holds the entire loop, so nothing else interleaves to evict that
    # cached prefix between chunks. Do NOT add per-part variation (e.g.
    # "this is part 3 of 7") to this system prompt — it would defeat that
    # reuse for a token-visibility gain the model doesn't need (chunk order
    # is already implicit in how the part summaries get joined).
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
# Studio's GET /v1/models takes ~10 s on a cold start; without a bound a
# wedged backend stalls every page's status poll, and without sharing, each
# tab's poll fired its own probe. One bounded probe is shared by all callers.
_STATUS_TIMEOUT_SECONDS = 15.0
_status_inflight: "asyncio.Task | None" = None


async def _status_probe() -> dict:
    try:
        resp = await asyncio.wait_for(_client().list(), timeout=_STATUS_TIMEOUT_SECONDS)
        models = [m.model for m in resp.models]
        return {"status": "ok", "model": effective_ollama_model(), "loaded_models": models,
                "backend": llm_backend_name()}
    except Exception:
        return {"status": "unavailable", "model": effective_ollama_model(),
                "backend": llm_backend_name()}


async def status() -> dict:
    global _status_cache, _status_inflight
    now = time.monotonic()
    if _status_cache and now - _status_cache[0] < _STATUS_CACHE_TTL:
        return _status_cache[1]
    task = _status_inflight
    loop = asyncio.get_running_loop()
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_status_probe())
        _status_inflight = task
    try:
        # shield: one waiter being cancelled must not kill the probe the
        # other waiters are sharing.
        result = await asyncio.shield(task)
    finally:
        if task.done() and _status_inflight is task:
            _status_inflight = None
    _status_cache = (time.monotonic(), result)
    return result


async def debug_info() -> dict:
    stt = await stt_status()
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": True,
            "backend": llm_backend_name(),
            "loaded_models": models,
            "default_model": effective_ollama_model(),
            "stt": stt,
        }
    except Exception as exc:
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": False,
            "backend": llm_backend_name(),
            "error": f"{type(exc).__name__}: {exc}",
            "default_model": effective_ollama_model(),
            "stt": stt,
        }


# ── Image generation ──────────────────────────────────────────────────────────

_IMAGEGEN_TYPE = os.environ.get("IMAGEGEN_TYPE", "").lower()   # "swarmui" or "comfyui"
# Read timeout for one generation request. Studio generates synchronously and
# loads the image model on first use (minutes on a Volta card), so the old
# fixed 10 minutes cut off slow-but-working generations.
_IMAGEGEN_HTTP_TIMEOUT = _httpx.Timeout(
    float(os.environ.get("IMAGEGEN_TIMEOUT_SECONDS")
          or os.environ.get("UNSLOTH_IMAGE_TIMEOUT_SECONDS") or 1800),
    connect=10.0,
)
_IMAGEGEN_URL  = os.environ.get("IMAGEGEN_URL", "").rstrip("/")


def _get_type() -> str:
    # Unsloth wins when it's active (same rule as the chat backend) — the
    # IMAGEGEN_TYPE env only describes the legacy add-on backends.
    if effective_llm_api_key() and UNSLOTH_IMAGE_MODEL:
        return "unsloth"
    return _IMAGEGEN_TYPE


def _get_url() -> str:
    if _get_type() == "unsloth":
        return effective_llm_url()
    return _IMAGEGEN_URL


def _unsloth_image_headers() -> dict:
    return {"Authorization": f"Bearer {effective_llm_api_key()}"}


# Live progress for whatever SwarmUI generation is currently in flight —
# updated in place by _swarmui_generate_via_ws (below) as it consumes
# gen_progress events over SwarmUI's GenerateText2ImageWS websocket, read
# by imagegen_progress() for the GET /api/ai/imagegen/progress route the
# UI polls. SwarmUI's plain HTTP /API/GetCurrentStatus (the route this
# used to poll) never actually returns step/total/preview fields at all —
# only waiting_gens/loading_models/waiting_backends/live_gens/
# backend_status — so this was always reporting fabricated zeros; real
# per-step progress only exists on the websocket path. Module-level,
# single-flight: this app only ever runs one image generation at a time
# (see imagegen_job_semaphore / the direct-generate route not being
# separately locked against it), so there's no need to key this by
# request/session.
_imagegen_progress_state: dict = {"active": False, "percent": 0.0, "current_percent": 0.0, "preview": ""}


def _reset_imagegen_progress() -> None:
    _imagegen_progress_state.update({"active": False, "percent": 0.0, "current_percent": 0.0, "preview": ""})


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


async def swarmui_check_for_updates() -> dict:
    """Best-effort: ask SwarmUI's Admin API (/API/CheckForUpdates) whether
    it has a pending update to itself, any extension, or any backend —
    read-only, changes nothing. Returns SwarmUI's own response shape
    verbatim: {"server": {"count": N, "preview": [...]}, "extensions":
    {name: {"count":, "preview":}, ...}, "backends": {...}}. Returns {} if
    not configured for SwarmUI, unreachable, or the calling session lacks
    the `restart` permission CheckForUpdates itself requires — the caller
    (api_imagegen_updates) treats an empty dict as "couldn't check", not
    "nothing to update"."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return {}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            sid = await _swarmui_session(u, c)
            r = await c.post(f"{u}/API/CheckForUpdates", json={"session_id": sid})
            data = r.json()
            return data if isinstance(data, dict) and "server" in data else {}
    except Exception:
        return {}


async def swarmui_restart(update_server: bool = False) -> dict:
    """Ask SwarmUI to restart its own process via its Admin API
    (/API/UpdateAndRestart) instead of nd-world needing Docker control over
    a sibling container to do it — same "don't give this app docker.sock
    access, SwarmUI's own API already covers it" reasoning as
    swarmui_refresh_after_local_change's settings-toggle trick above.

    `update_server=False` (the default — the plain "🔄 Restart SwarmUI"
    button) sets force=True: UpdateAndRestart's own `force` parameter means
    "rebuild/restart regardless of detected changes," and WITHOUT it a
    restart request with nothing new to pull just reports back
    success=False ("No changes found.") instead of actually restarting —
    exactly the wrong behavior for a "just restart it" button, which is
    usually clicked precisely when nothing changed but the model list is
    stuck anyway (see this function's own reason for existing: the
    fallback for when swarmui_refresh_after_local_change's rescan trick
    doesn't pan out).

    `update_server=True` (the "⬆ Update & Restart" button, used once
    swarmui_check_for_updates reports a pending server update) instead
    sets doUpdateServer=True and leaves force off — a real update is
    already "detected changes," so UpdateAndRestart pulls it and restarts
    on its own without needing to be forced.

    NOTE on parameter names: SwarmUI's actual C# signature is
    UpdateAndRestart(session, raw, doUpdateServer=false, aggressive=false,
    force=false) — earlier code here used updateExtensions/updateBackends,
    which aren't real parameters of that method at all and would have
    silently done nothing (or failed with "No changes found" every time,
    since force was never set). Fixed to match the real signature.

    Never raises; returns {"ok": False} if not configured for SwarmUI,
    unreachable, or the calling session lacks the `restart` permission.
    On success: {"ok": True, "result": <SwarmUI's own status message>}."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return {"ok": False}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            sid = await _swarmui_session(u, c)
            r = await c.post(f"{u}/API/UpdateAndRestart", json={
                "session_id": sid, "raw": {}, "doUpdateServer": update_server,
                "aggressive": False, "force": not update_server,
            })
            data = r.json()
            return {"ok": bool(data.get("success")), "result": data.get("result", "")}
    except Exception:
        return {"ok": False}


async def swarmui_backends() -> list:
    """Best-effort: ask SwarmUI's Admin API (/API/ListBackends) which
    generation backends it has and what each is currently doing —
    status, and (with full_data=True) the model presently loaded on it —
    the SwarmUI-side counterpart to this app's own Ollama "VRAM cockpit"
    (Models tab residency view). Read-only, changes nothing.

    NOTE: the exact request/response shape here (full_data=True as the
    param name, and reading each entry's status/current_model/id fields)
    is this codebase's best understanding of SwarmUI's Admin API, NOT
    independently verified against a live instance or SwarmUI's own docs
    in this environment (network access to external docs is restricted
    here) — same caveat as swarmui_check_for_updates and swarmui_restart
    above already carry for their own endpoints, which WERE verified this
    way historically. If this comes back empty against a real SwarmUI
    install, that's this endpoint/shape guess being wrong, not a sign the
    rest of the SwarmUI integration is broken.

    Returns SwarmUI's own backend list verbatim (a list of dicts), or []
    if not configured for SwarmUI, unreachable, the calling session lacks
    the permission this needs, or the response isn't in the expected
    shape — the caller (api_imagegen_backends) treats an empty list as
    "nothing to show," not "zero backends configured"."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            sid = await _swarmui_session(u, c)
            r = await c.post(f"{u}/API/ListBackends", json={"session_id": sid, "full_data": True})
            data = r.json()
            return list(data.values()) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception:
        return []


async def swarmui_resource_info() -> dict:
    """Best-effort: ask SwarmUI's Admin API (/API/GetServerResourceInfo)
    for host-level resource usage (CPU/RAM, and per-GPU VRAM where SwarmUI
    can read it). Confirmed against SwarmUI's own AdminAPI.cs source (not
    just inferred, unlike swarmui_backends' endpoint shape above): `gpus`
    is a JSON OBJECT keyed by GPU id string (e.g. {"0": {...}}), not a
    list, and each entry's total_memory/used_memory/free_memory are raw
    bytes, not MB — callers (ollama_tuning._detect_swarmui_gpus,
    ai-chat-image.js's igLoadBackendStatus) must convert both the
    dict-vs-list shape and the units. Returns SwarmUI's own response shape
    verbatim, or {} if not configured for SwarmUI, unreachable, or the
    session lacks permission."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return {}
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            sid = await _swarmui_session(u, c)
            r = await c.post(f"{u}/API/GetServerResourceInfo", json={"session_id": sid})
            data = r.json()
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def swarmui_free_memory() -> dict:
    """Ask SwarmUI to unload models and free backend VRAM/system RAM
    (/API/FreeBackendMemory) — the SwarmUI-side counterpart to this app's
    Ollama "unload" button. `backend="all"`/`system_ram=True` frees
    everything rather than targeting one backend id, matching the
    single "Free VRAM" button this backs rather than a per-backend
    control. Same "not independently verified against a live instance"
    caveat as swarmui_backends/swarmui_resource_info above.

    Never raises; returns {"ok": False} if not configured for SwarmUI,
    unreachable, or the session lacks permission."""
    t, u = _get_type(), _get_url()
    if t != "swarmui" or not u:
        return {"ok": False}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            sid = await _swarmui_session(u, c)
            r = await c.post(f"{u}/API/FreeBackendMemory", json={
                "session_id": sid, "backend": "all", "system_ram": True,
            })
            data = r.json()
            return {"ok": "error" not in data if isinstance(data, dict) else False}
    except Exception:
        return {"ok": False}


# ── Audio transcription (Unsloth Studio) ───────────────────────────────────
# See app/routers/ai.py's /attachments/upload - an uploaded audio attachment
# is transcribed here (regardless of its original format; Studio decodes it,
# and files over its request limit are re-encoded/split first) so its content
# reaches the chat model as plain text, the same reliable path a document
# attachment already uses - independent of whether the chat model itself has
# any native audio understanding. Studio's /v1/audio/transcriptions is the
# only speech-to-text backend.

async def stt_status() -> dict:
    """Health probe for speech-to-text: a cheap authenticated Studio call
    (Studio has no /health; /api/hub/cached-gguf answers quickly and exercises
    the same auth path the transcription call uses)."""
    if not effective_llm_api_key():
        return {"ok": False, "reason": "no Unsloth Studio API key is configured (Settings → System)", "backend": "unsloth"}
    from . import unsloth_extras as _unsloth_extras
    try:
        await _unsloth_extras.hub_cached()
        return {"ok": True, "backend": "unsloth", "url": effective_llm_url()}
    except Exception as e:
        return {"ok": False, "reason": str(e), "backend": "unsloth", "url": effective_llm_url()}


def _status_is_transient(status: int | None) -> bool:
    """HTTP statuses where the same request may succeed later: the server is
    overloaded, restarting or timed out (408/425/429/5xx). 401/404/409/422 and
    other 4xx are setup problems."""
    try:
        status = int(status or 0)
    except (TypeError, ValueError):
        return False
    return status in (408, 425, 429) or status >= 500


class SttError(Exception):
    """Raised by transcribe_audio when speech-to-text itself failed (no Studio
    key, Studio unreachable or timed out, an STT model that isn't downloaded, an
    unreadable response) - distinct from a successful transcription that just
    happens to be empty (a genuinely silent clip), which is NOT an error and
    returns "" normally. Callers that need real detail for the GM (audio jobs,
    session recap routes) catch this and surface str(exc); callers where a
    failed transcription should just quietly leave an attachment without
    transcript text (_finish_attachment_upload) catch and swallow it instead.

    `partial_transcript`, when non-empty, is every chunk successfully
    transcribed before the failing one, already joined and collapsed -
    set only by transcribe_audio's chunked path, when at least one prior
    chunk succeeded. A Studio restart 3 hours into a 4-hour session used to
    discard all 3 hours of already-completed work along with the error; a
    caller that saves this (audio_jobs.py) lets the GM resummarize from the
    salvaged partial instead of re-uploading and re-transcribing the whole
    recording from scratch."""

    def __init__(self, message: str, partial_transcript: str = "", *, retryable: bool = False):
        super().__init__(message)
        self.partial_transcript = partial_transcript
        # True when trying the SAME audio again later can succeed (the backend is
        # unreachable, timed out, overloaded or restarting) as opposed to a
        # setup problem only the GM can fix (no key, model not downloaded, bad
        # audio). The live-recording route turns it into 503 vs 400 so the
        # browser knows whether to wait or to stop and show the reason.
        self.retryable = retryable


def _collapse_repeated_transcript_lines(text: str, min_repeat: int = 4) -> str:
    """Whisper-family speech models (which is what Studio runs) can fall into
    a degenerate repetition loop on music or silence, which shows up as the
    exact same line repeated many times in a row (transcript pieces are
    joined one per line). Short runs (roughly 4-25 repeats observed in
    practice) are common — a real conversation essentially never produces the
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
STT_CHUNK_SECONDS = max(60.0, float(os.getenv("STT_CHUNK_SECONDS") or os.getenv("WHISPER_CHUNK_SECONDS") or (10 * 60)))
# Always comfortably above STT_CHUNK_SECONDS itself, so a clip just
# over the threshold still splits into at least two real chunks instead of
# producing a single-segment "split" that's really just the original file
# with extra ffmpeg overhead (transcribe_audio handles that case safely
# either way, but there's no reason to configure it into existence).
_STT_CHUNK_MIN_DURATION = max(15 * 60, STT_CHUNK_SECONDS * 1.5)


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
    speech-model repetition loop (see transcribe_audio's docstring) can
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
    tmpdir = Path(tempfile.mkdtemp(prefix="nd-stt-chunks-"))
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


# Studio's /v1/audio/transcriptions rejects request bodies over 25 MiB
# (observed live: 26,214,400 bytes). Stay under it with margin for the
# multipart framing.
_UNSLOTH_STT_MAX_BYTES = 23 * 1024 * 1024
# Max MINUTES of audio per transcription request — duration cap alongside
# the byte cap (see _transcribe_one_file).
_UNSLOTH_STT_CHUNK_SECONDS = 10 * 60
# Audio is only re-split when it is clearly longer than one piece. The cut at
# _UNSLOTH_STT_CHUNK_SECONDS is a stream copy, so the pieces it produces (and
# the generic pipeline's own STT_CHUNK_SECONDS pieces) measure a few
# milliseconds OVER it — a strict "> 600" re-encoded every one of them and sent
# Studio a second request carrying a sliver. 1.5x mirrors the generic
# pipeline's _STT_CHUNK_MIN_DURATION rule, and guarantees any remainder is
# at least half a piece long.
_UNSLOTH_STT_SPLIT_ABOVE_SECONDS = _UNSLOTH_STT_CHUNK_SECONDS * 1.5


def _plan_unsloth_chunks(file_size: int, duration: float | None) -> float | None:
    """Chunk length in seconds so each piece stays under Studio's request-
    size limit, or None when the file fits whole. Pure math — the async/
    subprocess work lives in the callers."""
    if file_size <= _UNSLOTH_STT_MAX_BYTES:
        return None
    if not duration or duration <= 0:
        return None  # no duration → can't size chunks; caller raises clearly
    return max(30.0, duration * (_UNSLOTH_STT_MAX_BYTES / file_size))


async def _transcode_audio_to_mp3(path: Path, tmpdir: Path) -> Path:
    """Re-encode to 16 kHz mono MP3 at 64 kbps — transcription-grade audio
    at ~8 KB/second, which shrinks a music-grade FLAC roughly 10x. The
    fallback for _transcode_audio_to_opus (STT_UPLOAD_FORMAT=mp3, or Studio
    refused Opus). Raises SttError with the actionable reason if ffmpeg is
    missing or lacks the MP3 encoder."""
    import asyncio
    out = tmpdir / (path.stem + "-nd-stt.mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        _, _ = await proc.communicate()
    except FileNotFoundError:
        raise SttError(
            "ffmpeg isn't available in this deployment, so the oversized audio "
            "can't be prepared for Studio's 25 MiB limit — convert it to MP3/OGG "
            "manually.")
    if proc.returncode != 0 or not out.is_file():
        raise SttError(
            f"Re-encoding {path.name} for Studio's 25 MiB limit failed "
            "(ffmpeg returned an error — it may be built without the MP3 "
            "encoder). Convert it to MP3/OGG manually.")
    return out


# What oversized or long audio is re-encoded to before it is sent to Studio. Opus is the best lossy format for
# speech: measured against MP3, AAC, Vorbis and Speex on the same speech, Opus at 24 kbps is ~2.9x smaller than
# the old 64 kbps MP3 with higher intelligibility (STOI .995 vs 1.000 on clean speech, i.e. indistinguishable,
# and a lower spectral distortion), 16 kbps is ~4x smaller and still ~.99. Studio has not been verified to
# decode Ogg Opus uploads, so a refusal falls back to the MP3 path (see _OpusUnusable) and is remembered.
# STT_UPLOAD_FORMAT=mp3 skips Opus; STT_OPUS_BITRATE (8k-128k, default 24k) is the size/fidelity trade.
_DEFAULT_STT_OPUS_KBPS = 24
# Studio statuses that can mean "I could not read that file" - tried again as MP3. Setup problems (401, 409
# model not downloaded), overload (429) and unreachable (503) are NOT format problems and are never retried.
_UPLOAD_REJECTION_STATUSES = (400, 415, 422, 500)
_stt_opus_rejected = False   # set once MP3 got through where Opus did not; until restart, go straight to MP3


class _OpusUnusable(Exception):
    """The Opus re-encode could not be made or Studio refused it - the same audio is retried as MP3."""


def _stt_upload_format() -> str:
    if _stt_opus_rejected:
        return "mp3"
    return "mp3" if (os.getenv("STT_UPLOAD_FORMAT") or "").strip().lower() == "mp3" else "opus"


def _stt_opus_kbps() -> int:
    m = re.fullmatch(r"(\d{1,3})k", (os.getenv("STT_OPUS_BITRATE") or "").strip().lower())
    if not m or int(m.group(1)) < 8:
        return _DEFAULT_STT_OPUS_KBPS
    return min(int(m.group(1)), 128)


async def _transcode_audio_to_opus(path: Path, tmpdir: Path) -> Path:
    """Re-encode to 16 kHz mono Opus in an Ogg container (.ogg - the extension a decoder is most likely to
    recognise; browsers' own Firefox recordings arrive the same way), ~3 KB/s at the default 24k. Audio only
    (-vn), so a video container works. Raises _OpusUnusable when ffmpeg or its libopus encoder is missing -
    the caller then uses MP3, whose own failure is the one reported."""
    import asyncio
    out = tmpdir / (path.stem + "-nd-stt.ogg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", f"{_stt_opus_kbps()}k",
            "-application", "voip", "-f", "ogg", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        _, _ = await proc.communicate()
    except FileNotFoundError as exc:
        raise _OpusUnusable("ffmpeg is not installed") from exc
    if proc.returncode != 0 or not out.is_file():
        raise _OpusUnusable("ffmpeg could not encode Opus (is it built with libopus?)")
    return out


async def _transcode_audio_for_stt(path: Path, tmpdir: Path, fmt: str) -> Path:
    return await (_transcode_audio_to_opus if fmt == "opus" else _transcode_audio_to_mp3)(path, tmpdir)


async def _stt_send_parts(parts: list, opus: bool) -> str:
    """Each piece to Studio in order, newline-joined. For an Opus upload, a status that can mean "could not
    read that file" raises _OpusUnusable (caller retries as MP3); every other failure is the SttError it
    always was."""
    from . import unsloth_extras as _unsloth_extras
    texts = []
    for part in parts:
        try:
            text = await _unsloth_extras.stt(part.read_bytes(), part.name, model=get_stt_model())
        except _unsloth_extras.StudioError as exc:
            if opus and exc.status_code in _UPLOAD_REJECTION_STATUSES:
                raise _OpusUnusable(f"Studio answered {exc.status_code}: {exc}") from exc
            raise SttError(f"Unsloth Studio STT: {exc}",
                           retryable=exc.unreachable or _status_is_transient(exc.status_code)) from exc
        texts.append((text or "").strip())
    return chr(10).join(t for t in texts if t)


async def _transcribe_reencoded(path: Path, duration: float, fmt: str) -> str:
    """Re-encode `path` to `fmt` ("opus" | "mp3"), split what is still too big or too long, send the pieces."""
    import shutil
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="nd-stt-"))
    cleanup_dirs = [tmpdir]
    try:
        small = await _transcode_audio_for_stt(path, tmpdir, fmt)
        # Piece length is capped by DURATION as well as size: a single
        # request carrying an hour of audio can outrun even a long read
        # timeout on a CPU-only box (observed live: a Qwen3-ASR job
        # died at the old 600 s read timeout). ~10 min per request
        # keeps each call comfortably inside the budget and makes
        # retries cheap (the audio-jobs pipeline re-runs pieces).
        if small.stat().st_size <= _UNSLOTH_STT_MAX_BYTES and duration <= _UNSLOTH_STT_SPLIT_ABOVE_SECONDS:
            parts = [small]
        else:
            size_plan = _plan_unsloth_chunks(small.stat().st_size, duration)
            chunk_seconds = min(size_plan or _UNSLOTH_STT_CHUNK_SECONDS, _UNSLOTH_STT_CHUNK_SECONDS)
            pieces, split_dir = await _split_audio_into_chunks(small, chunk_seconds)
            if split_dir:
                cleanup_dirs.append(split_dir)
            parts = pieces
        return await _stt_send_parts(parts, opus=(fmt == "opus"))
    finally:
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)


async def _transcribe_one_file(path: Path) -> str:
    """One audio file through Studio's /v1/audio/transcriptions (OpenAI
    multipart dialect — file + model, verified Phase 0.5). The `model`
    name maps to an STT model managed in Studio's own Settings → Voice;
    a missing one 409s with Studio's instructions, surfaced verbatim via
    SttError so the existing job pipeline shows it to the GM. Decoding
    options (language, vocabulary hints) are Studio's own STT settings;
    nd-world sends only the file and the model.

    Studio rejects request bodies over 25 MiB, so oversized files (the
    typical case: a long session recorded as high-bitrate FLAC) are first
    re-encoded to compact mono Opus (MP3 as the fallback, see
    _transcode_audio_to_opus) — lossless FLAC at music-grade bitrate
    shrinks ~30x for speech — and then, if that is still over the
    limit or longer than ~15 minutes, split into ~10 minute pieces with the
    existing ffmpeg segment machinery. All pieces are transcribed in order
    and joined with newlines, so a multi-hour recording transcribes as one
    transcript. A file that fits is sent exactly as it is."""
    global _stt_opus_rejected
    if not effective_llm_api_key():
        raise SttError("STT backend is set to Unsloth but no UNSLOTH_API_KEY is configured (Settings → System).")
    if not path.is_file():
        raise SttError(f"Audio file not found: {path.name}")

    size = path.stat().st_size
    # A file can be under the byte limit and still be hours long (a
    # low-bitrate recording): one request carrying that much audio
    # outruns the read timeout, so duration decides as well as size.
    duration = await _probe_audio_duration(path)
    if not (size > _UNSLOTH_STT_MAX_BYTES or (duration and duration > _UNSLOTH_STT_SPLIT_ABOVE_SECONDS)):
        return await _stt_send_parts([path], opus=False)
    if not duration or duration <= 0:
        raise SttError(
            f"{path.name} is {(size + 1048575) // 1048576} MiB — over Unsloth "
            "Studio's 25 MiB transcription request limit — and its duration "
            "couldn't be read to split it (ffmpeg/ffprobe missing?). Convert it "
            "to MP3/OGG first.")
    fmt = _stt_upload_format()
    if fmt == "opus":
        try:
            return await _transcribe_reencoded(path, duration, "opus")
        except _OpusUnusable as exc:
            _log.warning("Opus upload for %s not usable (%s) - retrying as MP3", path.name, exc)
        text = await _transcribe_reencoded(path, duration, "mp3")
        _stt_opus_rejected = True      # MP3 got through where Opus did not: stop trying Opus until restart
        _log.warning("Studio took the MP3 but not the Opus re-encode: using MP3 for oversized audio from now on")
        return text
    return await _transcribe_reencoded(path, duration, "mp3")


async def transcribe_audio(path: Path, on_progress=None, on_checkpoint=None, should_stop=None,
                            resume: dict | None = None) -> str:
    """Transcribe an audio file through Unsloth Studio's speech-to-text,
    transparently splitting a long recording into chunks first (see
    _split_audio_into_chunks) and collapsing any residual repetition-loop
    runs (see _collapse_repeated_transcript_lines) before returning.
    Returns "" for a successfully-transcribed silent clip. Raises
    SttError - with the actual reason, not a generic message - if the
    request to Studio itself failed (a chunk's failure fails the whole
    call, carrying the earlier chunks as `partial_transcript`).

    Splitting the audio and collapsing repeated lines are this app's own
    mitigations for the degenerate "repeat the same phrase forever" loop
    Whisper-family models can fall into on music or silence: a loop can then
    only ever ruin one chunk's worth of audio instead of the rest of a
    multi-hour file. Decoding options (language, vocabulary hints) belong to
    Studio's own STT settings; nd-world sends the file and the model name.

    `on_progress(current, total)`, if given, is called before each
    chunk's request (current is 1-based) - same shape
    summarize_transcript's own on_progress already uses, so a caller
    (audio_jobs.py) can persist real progress with the same DB fields for
    either phase. Never called at all for a clip short enough to skip
    chunking.

    `on_checkpoint(state)`, if given, is called after each chunk
    transcribes successfully, with enough to both resume and to show a
    partial result: {"phase": "transcribe", "chunks_done", "chunk_total",
    "chunk_seconds" (STT_CHUNK_SECONDS at split time), "audio_size"
    (path.stat().st_size), "text" (everything transcribed so far,
    collapsed)}. `should_stop`, if given, is polled before each chunk;
    when it returns true, JobInterrupted is raised instead of continuing
    - the caller's already-persisted checkpoint is the resume point, not
    this call's return value. `resume`, if given, is a previous
    checkpoint to continue from: chunks already covered by
    resume["chunks_done"] are skipped and resume["text"] seeds the
    accumulated result, but ONLY if chunk_total/chunk_seconds/audio_size
    all still match this exact call - a mismatch (different audio, or
    STT_CHUNK_SECONDS changed since the checkpoint was written) means
    the chunk boundaries themselves may differ, so splicing old and new
    text could silently duplicate or drop audio; discarded (logged, not
    raised) and transcription starts over from chunk 0 instead. Only the
    multi-chunk loop below checkpoints/resumes - the two single-call
    paths above have nothing to checkpoint between."""
    duration = await _probe_audio_duration(path)
    if not duration or duration <= _STT_CHUNK_MIN_DURATION:
        text = await _transcribe_one_file(path)
        return _collapse_repeated_transcript_lines(text)

    chunks, tmpdir = await _split_audio_into_chunks(path, STT_CHUNK_SECONDS)
    try:
        if len(chunks) == 1:
            # Still reachable with a real tmpdir (e.g. STT_CHUNK_SECONDS
            # configured above _STT_CHUNK_MIN_DURATION can produce a
            # single-segment split) — this branch must stay inside the same
            # try/finally as the multi-chunk loop below, not return before
            # it, or the tmpdir (a full stream-copy of the recording) is
            # never cleaned up.
            text = await _transcribe_one_file(chunks[0])
            return _collapse_repeated_transcript_lines(text)

        audio_size = path.stat().st_size
        start = 0
        parts = []
        if resume and resume.get("phase") == "transcribe" and resume.get("chunk_total") == len(chunks) \
                and resume.get("chunk_seconds") == STT_CHUNK_SECONDS and resume.get("audio_size") == audio_size:
            start = resume.get("chunks_done", 0)
            parts = [resume.get("text", "")]
        elif resume:
            _log.warning(
                "discarding a transcription checkpoint that no longer matches this audio "
                "(chunk_total=%s vs %s, chunk_seconds=%s vs %s, audio_size=%s vs %s)",
                resume.get("chunk_total"), len(chunks), resume.get("chunk_seconds"), STT_CHUNK_SECONDS,
                resume.get("audio_size"), audio_size,
            )

        for i in range(start, len(chunks)):
            chunk_path = chunks[i]
            if should_stop and should_stop():
                raise JobInterrupted(f"stopped before transcribing part {i + 1} of {len(chunks)}")
            if on_progress:
                on_progress(i + 1, len(chunks))
            try:
                parts.append(await _transcribe_one_file(chunk_path))
            except SttError as exc:
                if parts:
                    partial = _collapse_repeated_transcript_lines("\n".join(p for p in parts if p))
                    raise SttError(
                        f"Speech-to-text failed on part {i + 1} of {len(chunks)}: {exc}. The first {i} part(s) "
                        "were transcribed and have been saved — you can re-summarize from the partial "
                        "transcript, or re-upload to redo the whole recording.",
                        partial_transcript=partial,
                        retryable=exc.retryable,
                    ) from exc
                raise
            if on_checkpoint:
                on_checkpoint({
                    "phase": "transcribe", "chunks_done": i + 1, "chunk_total": len(chunks),
                    "chunk_seconds": STT_CHUNK_SECONDS, "audio_size": audio_size,
                    "text": _collapse_repeated_transcript_lines("\n".join(p for p in parts if p)),
                })
        return _collapse_repeated_transcript_lines("\n".join(p for p in parts if p))
    finally:
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Transcript for library clips ────────────────────────────────────────────
# "Generate transcript" on an audio/video library clip (app/routers/audio.py,
# video.py). Studio's OpenAI-dialect endpoint returns plain text without segment
# timestamps, so there is never a subtitle track to build: the second element of
# the pair is always "" and callers treat that as "transcript only" (the clip's
# subtitle column is untouched). Long clips are split the same way a session
# recording is (see _transcribe_one_file), without the checkpoint/resume a
# durable background job gets - this is a bounded, attended click.

async def transcribe_audio_with_subtitles(path: Path) -> tuple:
    """Transcribe `path` (audio OR video - ffmpeg decodes a video container's
    audio track) and return (plain_text, ""). Raises SttError exactly like
    transcribe_audio; an empty transcript is not an error (a genuinely silent
    clip transcribes fine)."""
    text = await _transcribe_one_file(path)
    return _collapse_repeated_transcript_lines(text), ""


async def imagegen_status() -> dict:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return {"ok": False, "reason": "not configured"}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            if t == "unsloth":
                # Reachability of the Studio /v1 layer (auth required) — the
                # image model itself loads on first request via media
                # auto-switch (see the compose provisioning comments).
                r = await c.get(f"{u}/v1/models", headers=_unsloth_image_headers())
                return {"ok": r.status_code < 400, "type": t, "url": u}
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
    if t == "unsloth":
        # The native endpoint accepts loras (findings I-1) but exposes no
        # discovery shape we verified — report none rather than guessing.
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


async def unsloth_cached_image_models() -> list:
    """Image-generation GGUFs Studio actually has on disk and can run, per
    its own /api/hub/cached-gguf (Studio-internal, not part of the
    documented /v1 surface — /v1/models lists chat models only, findings
    I-1). Each cached entry carries a "task" field; verified against a real
    Studio instance across several downloaded checkpoints that the values
    aren't a simple "image-diffusion" / "image-diffusion-unsupported" pair
    as first assumed — a working, natively-supported model (Krea 2 Turbo)
    reports "text-to-image", while an unrunnable one (an SDXL-family anime
    finetune) reports "image-diffusion-unsupported", and a not-yet-
    classified one reports task: null. Rather than chase every exact label
    Studio might use, this includes anything mentioning "image" that
    doesn't end in "unsupported" — so a chat model ("text-generation")
    stays excluded, an explicitly-unsupported checkpoint stays excluded,
    and an unclassified one (null) is excluded too since Studio hasn't
    itself vouched it'll run, but any real image-capable label passes."""
    u = effective_llm_url()
    if not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{u}/api/hub/cached-gguf", headers=_unsloth_image_headers())
            cached = r.json().get("cached", [])
    except Exception:
        return []
    out = []
    for m in cached:
        task = (m.get("task") or "").lower()
        if m.get("repo_id") and "image" in task and not task.endswith("unsupported"):
            out.append(m["repo_id"])
    return out


async def imagegen_models() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    if t == "unsloth":
        cached = await unsloth_cached_image_models()
        if cached:
            return cached
        # Nothing cached (or the query failed) — fall back to the
        # configured default so the picker isn't left completely empty.
        return [UNSLOTH_IMAGE_MODEL] if UNSLOTH_IMAGE_MODEL else []
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
    if t == "unsloth":
        return []  # no verified discovery shape (same reasoning as loras)
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
    if t == "unsloth":
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
    if t == "unsloth":
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
    """Live progress for the generation currently in flight, if any. Only
    the legacy SwarmUI websocket path has real per-step progress (see
    _imagegen_progress_state's own docstring); every other backend —
    ComfyUI and Unsloth alike — returns the inactive contract, and the UI
    already falls back to an indeterminate/elapsed-time display for that
    (see ai-chat-image.js)."""
    if _get_type() != "swarmui":
        return {"active": False, "percent": 0.0, "current_percent": 0.0, "preview": ""}
    return dict(_imagegen_progress_state)


# ── SwarmUI model downloads ─────────────────────────────────────────────────
# Lets a GM pull a checkpoint/VAE/text-encoder/etc. straight into SwarmUI's
# own Models folder through nd-world's UI, same idea as the other model downloads
# here — only reachable at all because docker-compose.yml/truenas-compose.yml
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
# code-verified values, not guesses. VAE/clip/ControlNet/Embedding/
# diffusion_models are SwarmUI's own documented convention (docs/Model
# Support.md) but aren't independently re-verified against a live instance
# here. "diffusion_models" holds split/quantized checkpoints — Flux-style
# UNet-only files, and GGUF-quantized models generally (SwarmUI auto-detects
# the .gguf extension there and offers to install city96/ComfyUI-GGUF; see
# the "krea2" entry in imagegen_templates.py for a GGUF model family that
# needs a fork of that node instead of the mainline release). Offered as
# suggestions only (a <datalist>, not an enum) — a GM running a SwarmUI
# version with different folder names isn't blocked by nd-world guessing
# wrong, since they can just type whatever their own installation actually
# uses.
SWARMUI_MODEL_FOLDER_SUGGESTIONS = [
    "", "LoRA", "VAE", "clip", "diffusion_models", "ControlNet", "Upscale", "IPAdapter", "Refiner", "Embedding",
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
    "done", ...} or {"error": "..."} — the same progress shape the other
    model downloads use, so the client-side JS can reuse identical parsing.

    There's no curated known-model list here and no one canonical
    trusted host for Stable-Diffusion-family checkpoints/VAEs/text-encoders
    — this is a free-text URL by design (HuggingFace, CivitAI, wherever the
    GM sources it from).

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
        _log.warning("swarmui model download failed: %s: %s", type(exc).__name__, exc)
        yield {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        # In a finally, not the except above: a client disconnect raises
        # GeneratorExit (a BaseException) at the yield, which except
        # Exception does NOT catch — the .part file used to be orphaned
        # where the model list's .part filter made it invisible and
        # undeletable (audit 2026-09-30, imagegen finding 4). A no-op
        # after the success path's replace().
        tmp.unlink(missing_ok=True)


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


async def _try_swarmui_ws_generate(u: str, payload: dict, save_image) -> list[str] | None:
    """Best-effort: generate via SwarmUI's GenerateText2ImageWS websocket
    instead of the plain HTTP /API/GenerateText2Image call, so
    _imagegen_progress_state gets real per-step updates as SwarmUI's own
    `gen_progress` events arrive (see that state's own docstring for why
    the plain HTTP status route can't provide this at all).

    Returns the saved image URLs on success, or None if the websocket
    path failed for ANY reason — connect failure (older SwarmUI without
    this route, wrong scheme, network hiccup), a protocol/JSON error, or
    a `gen_progress`/`image` message shape this code doesn't recognize.
    The caller (imagegen_generate) falls back to the existing, already
    battle-tested plain HTTP call in that case — so a mismatch between
    this code's assumptions about SwarmUI's websocket wire format (which,
    same as the caveat on the fields below, could not be independently
    verified against a live SwarmUI instance while writing this) can only
    ever cost the live progress display, never break generation itself.

    `save_image(img_raw) -> url` is the same per-image decode/save/
    thumbnail helper imagegen_generate's own HTTP path uses — SwarmUI
    returns individual images in the same string shapes (data URL / raw
    base64 / a saved-file path) on both the HTTP and websocket paths.

    NOTE on `gen_progress` field names/scale (overall_percent/
    current_percent as 0-1 fractions, converted to 0-100 here) — this
    could not be confirmed against a live SwarmUI instance or its docs
    from this environment. If the progress bar this feeds ends up showing
    an obviously-wrong number (frozen at some fixed value, jumping straight
    to 100%, etc.) against a real SwarmUI install, that's this mapping
    being wrong, not a sign anything else here is broken — the generation
    itself always still completes via the HTTP fallback path regardless."""
    ws_url = u.replace("https://", "wss://").replace("http://", "ws://") + "/API/GenerateText2ImageWS"
    urls: list[str] = []
    _imagegen_progress_state.update({"active": True, "percent": 0.0, "current_percent": 0.0, "preview": ""})
    try:
        # Short open_timeout — an older SwarmUI without this route, or a
        # reverse proxy in front of it that doesn't pass through websocket
        # upgrades, must fail fast into the HTTP fallback rather than
        # adding the default ~10s connect timeout to every generation.
        async with _websockets.connect(ws_url, open_timeout=4) as ws:
            await ws.send(_json.dumps(payload))
            async for raw in ws:
                msg = _json.loads(raw)
                if msg.get("error"):
                    raise ValueError(str(msg["error"]))
                gp = msg.get("gen_progress")
                if gp:
                    _imagegen_progress_state.update({
                        "active": True,
                        "percent": float(gp.get("overall_percent") or 0) * 100,
                        "current_percent": float(gp.get("current_percent") or 0) * 100,
                        "preview": gp.get("preview") or _imagegen_progress_state.get("preview", ""),
                    })
                img_raw = msg.get("image")
                if img_raw:
                    urls.append(await save_image(img_raw))
        if not urls:
            raise ValueError("websocket closed with no image")
        return urls
    except Exception as exc:
        _log.info(
            "SwarmUI websocket generate unavailable/failed, falling back to plain HTTP: %s: %s",
            type(exc).__name__, exc,
        )
        return None
    finally:
        _reset_imagegen_progress()


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
    import copy, random, asyncio, base64 as _b64, binascii as _binascii, uuid as _uuid
    # Lazy: unsloth_extras imports this module back (for the effective_*
    # resolvers) — a module-level import here would be circular.
    from . import unsloth_extras as _unsloth_extras
    t, u = _get_type(), _get_url()
    if not t or not u:
        raise ValueError("Image generation is not configured — set IMAGEGEN_TYPE/IMAGEGEN_URL (see docker-compose.yml).")
    ai_img_dir = Path(uploads_dir) / "ai-images"
    ai_img_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []
    # Used in every error message below so a GM sees which backend failed
    # and why, matching the bar generate_chat's own Ollama errors already
    # set (see its ResponseError handling) — a bare "connection refused"
    # or a stray dict-key KeyError previously reached the GM instead.
    backend_label = {"swarmui": "SwarmUI", "comfyui": "ComfyUI"}.get(t, "Unsloth")

    try:
        async with _httpx.AsyncClient(timeout=_IMAGEGEN_HTTP_TIMEOUT) as c:
            if t == "unsloth":
                # Preferred: the native /api/inference/images/generate —
                # unlocks negative prompt, steps/guidance, and img2img
                # (init_image + strength), none of which /v1 accepts. Its
                # response is Studio-gallery records ({images:[{url,...}]});
                # bytes are fetched per record. Falls back to the
                # Phase-0-verified /v1/images/generations on Studio builds
                # without the native endpoint (404).
                native_model = model or UNSLOTH_IMAGE_MODEL
                native_body = {
                    "model": native_model,
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                    "steps": steps if steps > 0 else 9,
                    "batch_size": max(1, min(batch_size, 8)),
                }
                if negative:
                    native_body["negative_prompt"] = negative
                if cfg and cfg > 0:
                    native_body["guidance"] = float(cfg)
                if seed >= 0:
                    native_body["seed"] = seed
                if init_image:
                    # init_image arrives as an /uploads/... URL — inline it
                    # as a data URL (the format Studio's own UI sends).
                    # Containment-checked like _swarmui_model_path: the
                    # field is free-form GM input, and an unchecked join
                    # turns "../../app/world.db" (or a Windows absolute
                    # path, which replaces the join base) into an
                    # arbitrary-file read exfiltrated to the remote Studio
                    # (audit 2026-09-30, imagegen finding 2).
                    _rel = init_image.split("/uploads/", 1)[-1]
                    # Subpaths are legitimate (init images live under
                    # ai-images/); containment is verified by resolve(),
                    # not by banning separators — a ".." or an absolute
                    # Windows path (which replaces the join base) must
                    # never read outside uploads_dir.
                    _candidate = (Path(uploads_dir) / _rel)
                    try:
                        _contained = _candidate.resolve().is_relative_to(Path(uploads_dir).resolve())
                    except (OSError, ValueError):
                        _contained = False
                    init_path = _candidate if _contained else None
                    if init_path is not None and init_path.is_file():
                        native_body["init_image"] = (
                            "data:image/png;base64,"
                            + _b64.b64encode(init_path.read_bytes()).decode()
                        )
                        native_body["strength"] = float(init_strength)
                used_native = True
                try:
                    gallery_records = await _unsloth_extras.image_generate_native(native_body)
                except _unsloth_extras.StudioEndpointMissing:
                    used_native = False
                    gallery_records = []
                except _unsloth_extras.StudioError as exc:
                    hint = ""
                    if "No diffusion model is loaded" in str(exc) or exc.status_code == 409:
                        hint = (" — enable Studio's media auto-switch (Settings → API, or "
                                "Settings → System → Studio server here) or load the image model "
                                "once from the Models tab")
                    raise ValueError(f"Unsloth Studio: {exc}{hint}") from exc
                if used_native:
                    _log.info("Unsloth imagegen: native generate, %d image(s)", len(gallery_records))
                    for rec in gallery_records:
                        raw = await _unsloth_extras.image_gallery_file(rec)
                        fname = str(_uuid.uuid4()) + ".png"
                        out_path = ai_img_dir / fname
                        out_path.write_bytes(raw)
                        make_thumbnail(out_path)
                        urls.append(f"/uploads/ai-images/{fname}")
                else:
                    # POST /v1/images/generations — the OpenAI dialect endpoint
                    # verified end-to-end in Phase 0 (findings I-1): b64_json in,
                    # PNG out, image model auto-loaded by name when Studio's
                    # media auto-switch is on (the compose provisioning steps).
                    # v1 deliberately sends only the verified field set — prompt,
                    # size, seed, batch, model; the turbo-family templates this
                    # backend targets barely use steps/guidance anyway.
                    _log.warning("Unsloth imagegen: native endpoint missing — falling back to "
                                 "/v1/images/generations (a reduced param set; a negative prompt "
                                 "is dropped there)")
                    v1_body = {
                        # The GM's PICKED model when one was given — the env
                        # default is the fallback, not the override (audit
                        # 2026-09-30, imagegen finding 6: this used to
                        # always send the env default, silently generating
                        # from a different model than the picker showed).
                        "model": model or UNSLOTH_IMAGE_MODEL,
                        "prompt": prompt,
                        "size": f"{width}x{height}",
                        "n": max(1, min(batch_size, 8)),
                        "response_format": "b64_json",
                    }
                    if seed >= 0:
                        v1_body["seed"] = seed
                    if negative:
                        _log.info("Unsloth imagegen: negative prompt not supported by the /v1 images endpoint — ignoring")
                    _imagegen_progress_state.update({"active": True, "percent": 0.0, "current_percent": 0.0, "preview": ""})
                    try:
                        gr = await c.post(f"{u}/v1/images/generations", json=v1_body,
                                          headers=_unsloth_image_headers())
                    finally:
                        _reset_imagegen_progress()
                    if gr.status_code >= 400:
                        # `error` may be an object, a bare string or a list
                        # depending on the Studio build — the shared parser
                        # copes with all of them (a string used to crash here).
                        detail = _unsloth_extras._error_message(gr)
                        hint = ""
                        if gr.status_code == 503 and "No image model loaded" in detail:
                            hint = " — enable Studio's media auto-switch (Settings → API) or load the image model once"
                        raise ValueError(f"Unsloth returned HTTP {gr.status_code}: {detail}{hint}")
                    try:
                        data = gr.json()
                    except ValueError as exc:
                        raise ValueError(f"Unsloth returned an unreadable response: {gr.text[:300]}") from exc
                    images = data.get("data") or []
                    if not images:
                        raise ValueError(f"Unsloth returned no image: {str(data)[:300]}")
                    for entry in images:
                        b64 = entry.get("b64_json") if isinstance(entry, dict) else None
                        if not b64:
                            raise ValueError("Unsloth returned an image entry without b64_json data")
                        fname = str(_uuid.uuid4()) + ".png"
                        out_path = ai_img_dir / fname
                        out_path.write_bytes(_b64.b64decode(b64))
                        make_thumbnail(out_path)  # best-effort, same as the SwarmUI path
                        urls.append(f"/uploads/ai-images/{fname}")
            elif t == "swarmui":
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
                    # Real T2IParamType ID is "clipstopatlayer" (Name "CLIP
                    # Stop At Layer") — verified against SwarmUI's own
                    # src/Text2Image/T2IParamTypes.cs, whose IDs are always
                    # the human-readable Name with spaces/punctuation
                    # stripped and lowercased (CleanTypeName). The bare
                    # "clipstop" this used to send matches no real param, so
                    # CLIP Skip silently never took effect.
                    payload["clipstopatlayer"] = -clip_skip
                if init_image:
                    payload["initimage"] = init_image
                    payload["initimagecreativity"] = init_strength
                if upscale_model:
                    # Real IDs are "refinerupscale" (a plain multiplier) and
                    # "refinerupscalemethod" (a curated dropdown whose
                    # file-backed entries are "model-<filename>", built from
                    # the same Models/Upscale folder imagegen_upscalers()
                    # already lists bare filenames from — see SwarmUI's
                    # ComfyUIBackendExtension.cs UpscalerModels/
                    # ConcatDropdownValsClean). "upscalemodel"/
                    # "upscalemultiplier" match no real params. This also
                    # means Upscale and the separate Refiner Model/Control
                    # fields below now correctly compose into ONE real
                    # upscale-then-refine pass (SwarmUI's actual design),
                    # instead of two silently-inert, unrelated-seeming
                    # panels.
                    payload["refinerupscalemethod"] = f"model-{upscale_model}"
                    payload["refinerupscale"] = upscale_factor
                if controlnet_image:
                    # Real ID is "controlnetimageinput" (Name "ControlNet
                    # Image Input") — "controlnetimage" matches no real
                    # param. "controlnetmodel"/"controlnetstrength" below
                    # are already correct. There is no real "preprocessor"
                    # param at all (verified: SwarmUI's ControlNet group
                    # registers only Image/Model/Strength/Start/End) — never
                    # sent, since it never did anything.
                    payload["controlnetimageinput"] = controlnet_image
                    payload["controlnetstrength"] = controlnet_strength
                    if controlnet_model:
                        payload["controlnetmodel"] = controlnet_model
                # NOTE: Hi-Res Fix (hiresfix/hireswidth/hiresheight/
                # hiresdenoisestrength/hiressteps) is a KNOWN, still-open
                # bug — verified against the real SwarmUI source that NONE
                # of these keys correspond to an actual param; there is no
                # width/height-target-based hires mechanism at all. The
                # real equivalent is the same "Refiner Upscale" multiplier
                # Upscale now correctly uses above, plus the existing
                # Refiner Model/Control Percentage/Steps fields — but
                # reconciling three GM-facing panels (Upscale, Hi-Res Fix,
                # Refiner) that would all write the same underlying keys
                # needs a real precedence decision, not a one-line rename,
                # so this is intentionally left broken rather than guessed
                # at and shipped half-right. Tracked as a follow-up.
                if hiresfix and hireswidth > 0:
                    payload["hireswidth"] = hireswidth
                    payload["hiresheight"] = hiresheight
                    payload["hiresdenoisestrength"] = hiresdenoisestrength
                    if hiressteps > 0:
                        payload["hiressteps"] = hiressteps
                if refiner_model:
                    payload["refinermodel"] = refiner_model
                    payload["refinercontrolpercentage"] = refiner_control
                # Real ID is "seamlesstileable", a single string enum
                # ("true"/"X-Only"/"Y-Only") — not two separate booleans.
                # "seamlessx"/"seamlessy" match no real param.
                if seamless_x and seamless_y:
                    payload["seamlesstileable"] = "true"
                elif seamless_x:
                    payload["seamlesstileable"] = "X-Only"
                elif seamless_y:
                    payload["seamlesstileable"] = "Y-Only"
                if variation_seed >= 0 and variation_strength > 0:
                    payload["variationseed"] = variation_seed
                    payload["variationseedstrength"] = variation_strength
                if freeu_enabled:
                    # Real IDs spell out "One"/"Two", not "1"/"2" — Names
                    # are "[FreeU] Block One"/"Block Two"/"Skip One"/"Skip
                    # Two", and CleanTypeName keeps letters only (digits are
                    # stripped too), so "freeu_b1" etc. match no real param.
                    payload["freeublockone"] = freeu_b1
                    payload["freeublocktwo"] = freeu_b2
                    payload["freeuskipone"] = freeu_s1
                    payload["freeuskiptwo"] = freeu_s2
                if dynthresh_enabled:
                    # Real IDs are "dtmimicscale"/"dtthresholdpercentile"
                    # (Names "[DT] Mimic Scale"/"[DT] Threshold
                    # Percentile", from the DynamicThresholding built-in
                    # extension — a separate, OPTIONAL installable comfy
                    # node, not core SwarmUI). There is no "enabled" param
                    # at all; per the real param's own description, "1"
                    # disables and anything below it enables, so merely
                    # sending a real percentile value (already this
                    # function's own contract — nd-world's own enabled
                    # toggle gates whether to send it at all) is what
                    # "enables" it — the old "dynamicthresh_enabled" key
                    # matched no real param.
                    payload["dtmimicscale"] = dynthresh_mimic_scale
                    payload["dtthresholdpercentile"] = dynthresh_percentile
                if cfg_rescale > 0:
                    # Real ID is "rescalecfgmultiplier" (Name "Rescale CFG
                    # Multiplier") — "cfgrescale" matches no real param.
                    payload["rescalecfgmultiplier"] = cfg_rescale
                if ipadapter_image:
                    # Real IDs: the reference image goes under
                    # "promptimages" (SwarmUI's shared Image-Prompting/
                    # ReVision/IP-Adapter image list — verified as a normal
                    # List<Image> param accepting the same base64/data-URL
                    # string format as initimage, just hidden from the
                    # interactive UI in favor of drag-and-drop), strength is
                    # "ipadapterweight" (not "ipadapterstrength"), and the
                    # model selector is "useipadapterforrevision" whose
                    # file-backed values are "file:<filename>" (verified
                    # against SwarmUI's ComfyUIBackendExtension.cs
                    # IPAdapterModelLoader handling — the same Models/
                    # IPAdapter folder imagegen_ipadapter_models() already
                    # lists bare filenames from). "ipadapterimage"/
                    # "ipadapterstrength"/"ipadaptermodel" matched no real
                    # params, so this feature never took effect at all.
                    payload["promptimages"] = ipadapter_image
                    payload["ipadapterweight"] = ipadapter_strength
                    if ipadapter_model:
                        payload["useipadapterforrevision"] = f"file:{ipadapter_model}"

                async def _save_swarmui_image(img_raw: str) -> str:
                    """Decode/save one SwarmUI image entry to ai_img_dir
                    and return its /uploads/ai-images/<file> URL — shared
                    by this HTTP path and _try_swarmui_ws_generate above,
                    since SwarmUI returns images in the same string shapes
                    (data URL / raw base64 / a saved-file path) on both."""
                    if img_raw.startswith("data:"):
                        img_bytes = _b64.b64decode(img_raw.split(",", 1)[1])
                    else:
                        # Depending on SwarmUI version/config, a non-data-URL
                        # entry is either raw base64 or a saved-file path
                        # (e.g. "View/local/raw/2024-.../x.png") — try
                        # base64 first, fall back to fetching the path as
                        # bytes if it isn't valid base64 at all.
                        try:
                            img_bytes = _b64.b64decode(img_raw, validate=True)
                        except (_binascii.Error, ValueError):
                            ir = await c.get(f"{u}/{img_raw.lstrip('/')}")
                            if ir.status_code >= 400:
                                raise ValueError(
                                    f"SwarmUI returned no image: could not fetch {img_raw!r} (HTTP {ir.status_code})"
                                )
                            img_bytes = ir.content
                    fname = str(_uuid.uuid4()) + ".png"
                    out_path = ai_img_dir / fname
                    out_path.write_bytes(img_bytes)
                    make_thumbnail(out_path)  # best-effort — the Image tab's history/starred grids fall back to this full PNG if it fails
                    return f"/uploads/ai-images/{fname}"

                # Try the websocket path first for live per-step progress —
                # see _try_swarmui_ws_generate's own docstring for exactly
                # what makes it fall back (returning None) to the plain
                # HTTP call below instead of raising.
                ws_urls = await _try_swarmui_ws_generate(u, payload, _save_swarmui_image)
                if ws_urls is not None:
                    urls.extend(ws_urls)
                else:
                    gr = await c.post(f"{u}/API/GenerateText2Image", json=payload)
                    _log.info("SwarmUI generate status=%s body=%.400s", gr.status_code, gr.text)
                    if gr.status_code >= 400:
                        raise ValueError(f"SwarmUI returned HTTP {gr.status_code}: {gr.text[:300]}")
                    try:
                        data = gr.json()
                    except ValueError as exc:
                        raise ValueError(f"SwarmUI returned an unreadable response: {gr.text[:300]}") from exc
                    images = data.get("images") or []
                    if not images:
                        err = data.get("error") or data.get("errorid") or data.get("message") or str(data)
                        raise ValueError(f"SwarmUI returned no image: {err}")
                    for img_raw in images:
                        urls.append(await _save_swarmui_image(img_raw))

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
                try:
                    pr_body = pr.json()
                except ValueError:
                    pr_body = {}
                if pr.status_code >= 400 or not pr_body.get("prompt_id"):
                    err = pr_body.get("error") or pr_body.get("node_errors") or pr.text[:300]
                    raise ValueError(f"ComfyUI rejected the workflow (HTTP {pr.status_code}): {err}")
                pid = pr_body["prompt_id"]
                # Matches the 600s client timeout above — the previous 120s
                # cap could expire (and silently return zero images) well
                # before a large batch/upscale genuinely finished.
                for _ in range(580):
                    await asyncio.sleep(1)
                    hr = await c.get(f"{u}/history/{pid}")
                    hist = hr.json().get(pid, {})
                    status = hist.get("status") or {}
                    if status.get("status_str") == "error":
                        raise ValueError(f"ComfyUI generation failed: {status.get('messages') or status}")
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
                else:
                    raise ValueError("ComfyUI generation timed out waiting for a result.")
    except _httpx.HTTPError as exc:
        # Covers connection-refused/timeout/DNS-failure/etc — httpx.HTTPError
        # is the base class for all of those (see httpx's own hierarchy).
        # Anything ABOVE this except clause (a deliberate ValueError raised
        # for a bad/error response body) is a real backend reply, not a
        # transport failure, and passes through unchanged.
        raise ValueError(f"{backend_label} unreachable at {u}: {exc}") from exc

    if not urls:
        # Safety net: should be unreachable given the raises above, but an
        # empty success (HTTP 200, no error field, zero images/outputs)
        # must never silently look like it worked — see this app's own
        # "200-with-error-body" precedent this guards against reintroducing.
        raise ValueError(f"{backend_label} returned no images.")
    return urls
