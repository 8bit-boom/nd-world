"""Unit tests for app.ai.resolve_model — a requested model id gets matched
against what's actually pulled in Ollama (_list_loaded), tolerating a short
id like "llama3" matching a tagged one like "llama3:latest". Added after a
bug where a total mismatch fell back to an arbitrary "available[0]" model
instead of leaving the request alone, so a GM could silently get answers
from the wrong model with no indication anywhere in the response.
"""
import pytest

from app import ai as ai_module


@pytest.mark.asyncio
async def test_exact_match_returns_no_note(monkeypatch):
    monkeypatch.setattr(ai_module, "_list_loaded", lambda: _async(["llama3:latest", "gemma:7b"]))
    model, note = await ai_module.resolve_model("llama3:latest")
    assert model == "llama3:latest"
    assert note is None


@pytest.mark.asyncio
async def test_fuzzy_match_surfaces_a_note(monkeypatch):
    monkeypatch.setattr(ai_module, "_list_loaded", lambda: _async(["llama3:latest"]))
    model, note = await ai_module.resolve_model("llama3")
    assert model == "llama3:latest"
    assert note and "llama3:latest" in note and "llama3" in note


@pytest.mark.asyncio
async def test_no_models_available_passes_request_through(monkeypatch):
    monkeypatch.setattr(ai_module, "_list_loaded", lambda: _async([]))
    model, note = await ai_module.resolve_model("whatever-model")
    assert model == "whatever-model"
    assert note is None


@pytest.mark.asyncio
async def test_no_match_does_not_fall_back_to_an_arbitrary_model(monkeypatch):
    """The historical bug: a total mismatch used to silently return
    available[0] — an unrelated model — instead of leaving the request
    alone for Ollama's own "model not found" error to surface clearly."""
    monkeypatch.setattr(ai_module, "_list_loaded", lambda: _async(["totally-unrelated-model:latest"]))
    model, note = await ai_module.resolve_model("gemma:7b")
    assert model == "gemma:7b"
    assert note is None


@pytest.mark.asyncio
async def test_blank_requested_uses_effective_default(monkeypatch):
    monkeypatch.setattr(ai_module, "_list_loaded", lambda: _async(["default-model:latest"]))
    monkeypatch.setattr(ai_module, "effective_ollama_model", lambda: "default-model:latest")
    model, note = await ai_module.resolve_model("")
    assert model == "default-model:latest"
    assert note is None


async def _async(value):
    return value
