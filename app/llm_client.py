"""Unsloth Studio client — speaks the OpenAI-compatible /v1 API verified in
Phase 0 (docs/UNSLOTH_PHASE0_FINDINGS.md) behind an ollama-python-client-shaped
surface, so app/ai.py's ~40 call sites keep working unchanged.

Deliberately NOT the `openai` SDK: nd-world needs non-standard passthroughs
(`chat_template_kwargs`, llama.cpp sampling extras) and already speaks raw
HTTP everywhere else (see the migration plan §6).

Only the subset of the ollama client's surface that app/ai.py actually uses
is implemented:

- ``chat()`` (non-streamed and SSE-streamed), with ollama-style kwargs
  (``options=``, ``think=``, ``format=``, ``keep_alive=``) translated to the
  OpenAI dialect: ``num_predict``→``max_tokens``,
  ``repeat_penalty``→``repetition_penalty``, ``think``→
  ``chat_template_kwargs.enable_thinking`` (Gemma-4 thinks BY DEFAULT on
  Unsloth — the explicit false is load-bearing, see the Phase 0 starvation
  trap), ``format=<json schema dict>``→``response_format``, message
  ``images:[b64]``→``image_url`` data-URL content parts. ``num_ctx`` and
  other load-time knobs are dropped with a log-once line (context is fixed
  at model load in llama.cpp — see the migration plan §6.3).
- ``list()`` → ``GET /v1/models`` (chat models only; size/quant fields
  degrade to None/"" — the Settings recommendation panel's name-based
  fallback already handles that).

Everything else the ollama client has (``show``/``ps``/``generate``/
``create_blob``/``create``/``pull``/``delete``) is intentionally absent:
call sites either guard on the backend first (``unload_model``,
``import_local_gguf_model``) or hit ``AttributeError`` inside an existing
``except Exception`` fallback (``_model_supports_thinking``'s capability
probe — deleted per plan §6.2, KNOWN_MODELS + the GM override checkbox are
now the only thinking sources).

Errors raise ``UnslothResponseError``, a subclass of
``ollama.ResponseError``, so every existing ``except _ollama.ResponseError``
branch in app/ai.py catches backend rejections unchanged.
"""

import json as _json
import logging
import time
from types import SimpleNamespace

import httpx as _httpx
import ollama as _ollama

_log = logging.getLogger("nd.llm")

# Generous hard cap for ONE chat request — thinking models on a 16 GB card
# can take minutes; 10 minutes of pure generation time at V100 speeds covers
# far past any configured num_predict. Connect gets its own short timeout so
# "backend is down" fails fast instead of eating the whole budget.
_CHAT_TIMEOUT = _httpx.Timeout(600.0, connect=10.0)

# Ollama option keys the OpenAI dialect has a direct name for.
_OPTION_RENAMES = {
    "num_predict": "max_tokens",
    "repeat_penalty": "repetition_penalty",
}
# Ollama/llama.cpp sampling keys llama.cpp's server accepts under the same
# name — passed through as extra top-level body keys.
_OPTION_PASSTHROUGH = {"temperature", "top_p", "top_k", "seed", "min_p"}
# Log-once set for dropped keys, so a GM's Settings don't spam every call.
_dropped_options_logged: set[str] = set()


class UnslothResponseError(_ollama.ResponseError):
    """HTTP error from Unsloth, shaped exactly like ollama.ResponseError
    (``error`` message + ``status_code``) so app/ai.py's existing handlers
    catch it via ``except _ollama.ResponseError`` unchanged."""

    def __init__(self, error: str, status_code: int):
        super().__init__(error, status_code)


def _translate_options(options: dict | None) -> dict:
    """ollama-style ``options={...}`` → OpenAI-dialect body keys. Unknown
    keys are dropped (with one log line each, ever) rather than passed
    through: Unsloth 400s on some llama.cpp-only knobs, and a chat call
    failing on a tuning key is worse than the key being ignored."""
    out = {}
    for key, value in (options or {}).items():
        if key in _OPTION_RENAMES:
            out[_OPTION_RENAMES[key]] = value
        elif key in _OPTION_PASSTHROUGH:
            out[key] = value
        elif key == "num_ctx":
            # Fixed at model load in llama.cpp — see migration plan §6.3.
            # Chunk sizing reads LLM_CONTEXT_TOKENS instead; dropping it
            # here is the whole point of the num_ctx migration.
            continue
        else:
            if key not in _dropped_options_logged:
                _dropped_options_logged.add(key)
                _log.info("Unsloth client: dropping unsupported option %r", key)
    return out


def _b64_to_data_url(b64: str) -> str:
    """A raw base64 image (ollama's message ``images`` format) → an
    ``image_url`` data URL. Sniffs the JPEG magic so the part's media type
    is right (vision models are picky about it); defaults to png."""
    if b64.startswith("data:"):
        return b64
    media = "image/jpeg" if b64.startswith("/9j/") else "image/png"
    return f"data:{media};base64,{b64}"


def _translate_messages(messages: list[dict]) -> list[dict]:
    """ollama message list → OpenAI content-part messages. Only two ollama
    shapes exist in app/ai.py: plain ``{"role","content"}`` and the vision
    import shape with a raw-b64 ``images`` list on the user message, which
    becomes text + ``image_url`` parts (verified in Phase 0, findings I-6)."""
    out = []
    for msg in messages or []:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        images = msg.get("images")
        if images:
            parts = []
            if content:
                parts.append({"type": "text", "text": str(content)})
            for b64 in images:
                parts.append({"type": "image_url", "image_url": {"url": _b64_to_data_url(b64)}})
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": content})
    return out


def _error_from_response(resp: _httpx.Response) -> UnslothResponseError:
    """Unsloth's error shape is OpenAI-compatible:
    ``{"error": {"message", "type", ...}}`` — see findings I-8."""
    message = ""
    try:
        payload = resp.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or "")
        elif err:
            message = str(err)
        if not message:
            message = str(payload)[:500]
    except Exception:
        message = (resp.text or "")[:500]
    return UnslothResponseError(message or f"HTTP {resp.status_code}", resp.status_code)


class UnslothClient:
    """Drop-in for the slice of ``ollama.AsyncClient`` app/ai.py uses."""

    def __init__(self, base_url: str, api_key: str):
        self._base = (base_url or "").rstrip("/")
        self._key = api_key or ""

    def _headers(self) -> dict:
        # Every /v1 call requires the Bearer key (findings I-8).
        return {"Authorization": f"Bearer {self._key}"}

    def _http(self) -> _httpx.AsyncClient:
        return _httpx.AsyncClient(timeout=_CHAT_TIMEOUT, follow_redirects=True)

    async def chat(self, model: str, messages: list[dict], stream: bool = False,
                   options: dict | None = None, think: bool | None = None,
                   format=None, keep_alive=None, **_ignored):
        """Mirrors ``ollama.AsyncClient.chat``: returns a ChatResponse-like
        object non-streamed, or an async iterator of chunk objects when
        ``stream=True`` (the caller does ``async for ... in await
        client.chat(...)`` — this coroutine returns the iterator, matching
        ollama's own shape exactly)."""
        body: dict = {
            "model": model,
            "messages": _translate_messages(messages),
            "stream": bool(stream),
        }
        body.update(_translate_options(options))
        if think is not None:
            # ALWAYS explicit — Gemma-4 thinks by default on Unsloth and
            # reasoning eats max_tokens (Phase 0 starvation trap, I-4).
            body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        if format:
            if isinstance(format, dict):
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": format},
                }
            else:
                body["response_format"] = {"type": "json_object"}
        if keep_alive is not None:
            _log.info("Unsloth client: keep_alive ignored (Studio idle auto-unload owns residency)")
        if stream:
            return self._stream_chat(body)
        return await self._chat_once(body)

    async def _chat_once(self, body: dict):
        started = time.monotonic()
        async with self._http() as c:
            resp = await c.post(f"{self._base}/v1/chat/completions", json=body,
                                headers=self._headers())
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        elapsed_ns = int((time.monotonic() - started) * 1e9)
        # ollama's timing fields in nanoseconds, from usage + wall clock
        # (llama.cpp's OpenAI layer reports no server-side durations — see
        # migration plan §6.2 benchmark_model row). eval_duration is the
        # whole request wall time, an upper bound on pure generation time.
        return SimpleNamespace(
            model=data.get("model") or body.get("model", ""),
            message=SimpleNamespace(
                content=msg.get("content") or "",
                thinking=msg.get("reasoning_content") or None,
            ),
            done_reason=choice.get("finish_reason"),
            done=True,
            eval_count=int(usage.get("completion_tokens") or 0),
            prompt_eval_count=int(usage.get("prompt_tokens") or 0),
            eval_duration=elapsed_ns,
            prompt_eval_duration=0,
            load_duration=0,
            total_duration=elapsed_ns,
        )

    async def _stream_chat(self, body: dict):
        """SSE → ollama-style chunk objects. ``delta.content`` maps to
        ``chunk.message.content``, ``delta.reasoning_content`` to
        ``chunk.message.thinking``, ``finish_reason`` to ``done_reason``
        (findings I-4: standard OpenAI chunk shape)."""
        try:
            async with self._http() as c:
                async with c.stream("POST", f"{self._base}/v1/chat/completions",
                                      json=body, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        # Read the body so the error shape parses the same.
                        await resp.aread()
                        raise _error_from_response(resp)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            data = _json.loads(payload)
                        except ValueError:
                            continue
                        for choice in data.get("choices") or []:
                            delta = choice.get("delta") or {}
                            yield SimpleNamespace(
                                message=SimpleNamespace(
                                    content=delta.get("content") or "",
                                    thinking=delta.get("reasoning_content") or None,
                                ),
                                done_reason=choice.get("finish_reason"),
                                done=False,
                            )
        except _httpx.HTTPError as exc:
            # Mirror the ollama client: transport failures surface as a
            # plain exception that app/ai.py's `except Exception` turns
            # into an "[AI unavailable: ...]" sentinel.
            raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc

    async def list(self):
        """GET /v1/models — chat models only (findings I-5). Size/quant
        detail fields don't exist here; the recommendation panel's
        name-based fallback handles their absence (plan §6.2)."""
        async with self._http() as c:
            resp = await c.get(f"{self._base}/v1/models", headers=self._headers())
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        data = resp.json()
        # /v1/models entries carry a `loaded` bool (findings I-5) — exposed
        # so resident_models() can show "currently loaded" under Unsloth
        # the way .ps() did under Ollama.
        models = [
            SimpleNamespace(model=m.get("id", ""), size=None, details=None,
                            loaded=bool(m.get("loaded")))
            for m in data.get("data") or []
            if isinstance(m, dict) and m.get("id")
        ]
        return SimpleNamespace(models=models)

    async def generate(self, *args, **kwargs):
        """ollama's unload idiom (empty generate + keep_alive=0) has no
        equivalent — Studio's idle auto-unload owns residency."""
        raise NotImplementedError(
            "Unsloth Studio manages model residency via idle auto-unload — "
            "there is no per-request unload"
        )
