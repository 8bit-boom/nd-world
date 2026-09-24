"""Regression tests for the Ollama model-pull UI silently reporting success
even when the pull actually failed. POST /api/ai/pull already streams a
distinctly-typed `{"error": ...}` SSE frame on failure (see app.routers.
ai's ai_pull), but none of the four client call sites reliably surfaced it:

- static/js/ai-chat-core.js's pullModel() and forceRepull() never checked
  `obj.error` at all — every SSE frame that parsed as JSON was silently
  absorbed by a bare `catch(_) {}`, so a failed pull still fell through to
  the same "loaded" success path a real one takes (forceRepull's case is
  worse still: it had already deleted the model from Ollama storage before
  the failed re-pull, per the companion Re-dl bug).
- static/js/ai-chat-models.js's mpPullModel() and mpAddAndPull() DID check
  `obj.error`, but only inside the same try/catch as JSON.parse, using a
  fragile `!pe.message.includes('JSON')` heuristic to tell "this was a
  real error" apart from "this was a JSON.parse failure on a partial
  chunk" — a real Ollama error message that happens to mention the word
  "JSON" (e.g. a malformed-manifest error) would be silently swallowed by
  that same heuristic and reported as success.

JS-source assertion tests, reading the static files directly — matches
this repo's established convention for template-JS regression coverage
(see test_ai_chat_pinned_entity_cap.py, test_ai_chat_stream_errors.py)."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CORE_JS = (_ROOT / "static" / "js" / "ai-chat-core.js").read_text()
_MODELS_JS = (_ROOT / "static" / "js" / "ai-chat-models.js").read_text()


def _core_fn(name, next_marker):
    start = _CORE_JS.index(f"async function {name}(")
    end = _CORE_JS.index(next_marker, start)
    return _CORE_JS[start:end]


def _models_fn(name, next_marker):
    start = _MODELS_JS.index(f"async function {name}(")
    end = _MODELS_JS.index(next_marker, start)
    return _MODELS_JS[start:end]


# ── static/js/ai-chat-core.js ────────────────────────────────────────────

def test_pull_model_throws_on_error_frame():
    body = _core_fn("pullModel", "\nasync function forceRepull")
    assert "if (obj.error) throw new Error(obj.error);" in body


def test_force_repull_throws_on_error_frame():
    body = _core_fn("forceRepull", "\nasync function resetModels")
    assert "if (obj.error) throw new Error(obj.error);" in body


# ── static/js/ai-chat-models.js ──────────────────────────────────────────

def test_mp_pull_model_throws_on_error_frame():
    body = _models_fn("mpPullModel", "\nasync function mpDeleteModel")
    assert "if (obj.error) throw new Error(obj.error);" in body


def test_mp_add_and_pull_throws_on_error_frame():
    body = _models_fn("mpAddAndPull", "\nfunction mpQuickPull")
    assert "if (obj.error) throw new Error(obj.error);" in body


# ── The fragile heuristic must be gone entirely ──────────────────────────

def test_fragile_json_substring_heuristic_is_gone():
    """The old `catch(pe) { if (pe.message && !pe.message.includes('JSON'))
    throw pe; }` pattern could silently swallow a real pull error whose
    message happened to mention "JSON" — must not reappear anywhere in
    either file."""
    assert "includes('JSON')" not in _CORE_JS
    assert "includes('JSON')" not in _MODELS_JS


def test_json_parse_failures_are_still_tolerated_separately():
    """A malformed/partial SSE chunk (a real, expected occurrence mid-
    stream) must still be skipped rather than aborting the whole pull —
    just via its own dedicated try/catch around JSON.parse alone, not one
    shared with the error-surfacing check."""
    for body in (
        _core_fn("pullModel", "\nasync function forceRepull"),
        _core_fn("forceRepull", "\nasync function resetModels"),
        _models_fn("mpPullModel", "\nasync function mpDeleteModel"),
        _models_fn("mpAddAndPull", "\nfunction mpQuickPull"),
    ):
        assert "try { obj = JSON.parse(" in body
        assert "} catch (_) { continue; }" in body
