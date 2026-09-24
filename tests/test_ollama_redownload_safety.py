"""Regression test for the "Re-dl" (force re-download) button in the
Models tab deleting a model from Ollama storage BEFORE knowing whether the
re-download would actually succeed (static/js/ai-chat-core.js's
forceRepull). If the subsequent pull then failed for any reason — a
network hiccup, the registry being briefly unreachable, a full disk — the
GM was left with no model at all, strictly worse than the corrupted-but-
still-present file they started with and clicked "Re-dl" to fix.

Ollama's own model store is content-addressed: a normal pull already
re-verifies every layer against the registry and re-fetches anything that
doesn't match, which is the standard way to repair a corrupted model
without ever deleting it first. The fix drops the pre-emptive
POST /api/ai/models/remove call entirely — forceRepull is now just a plain
re-pull, so a failed attempt leaves the existing file untouched.

JS-source assertion test, reading the static file directly — matches this
repo's established convention for template-JS regression coverage (see
test_ollama_pull_error_surfacing.py)."""
from pathlib import Path

_CORE_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "ai-chat-core.js").read_text()


def _force_repull_body():
    start = _CORE_JS.index("async function forceRepull(")
    end = _CORE_JS.index("\nasync function resetModels", start)
    return _CORE_JS[start:end]


def test_force_repull_never_deletes_the_model_first():
    body = _force_repull_body()
    assert "/api/ai/models/remove" not in body
    assert "delete_from_ollama" not in body


def test_force_repull_only_calls_the_plain_pull_route():
    body = _force_repull_body()
    assert body.count("fetch(") == 1
    assert "/api/ai/pull" in body


def test_force_repull_restores_the_loaded_dot_state_on_failure():
    """A failed re-pull must leave the model looking exactly as it did
    before the GM clicked the button (it was never deleted) — not stuck
    showing the "unloaded" dot state set right before the pull started,
    which would now wrongly suggest the model is gone."""
    body = _force_repull_body()
    catch_block = body.split("} catch(e) {", 1)[1]
    assert "dot.className = 'model-dot model-dot--loaded';" in catch_block
