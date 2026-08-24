"""Tests for app.ai's transcript chunking (_split_transcript_into_chunks,
_transcript_chunk_char_budget) and summarize_transcript's map-reduce path —
added because a raw Whisper transcript of a multi-hour session can easily
run to tens of thousands of tokens, and the previous single-generate_chat-
call implementation silently let Ollama truncate anything that didn't fit,
so the recap quietly covered only part of the session with no signal
anything was lost. A transcript that fits in one context window still goes
through exactly one generate_chat call, unchanged from before.
"""
import pytest

from app import ai as ai_module


# ── _split_transcript_into_chunks ───────────────────────────────────────────

def test_split_returns_whole_transcript_when_it_fits():
    text = "short transcript"
    assert ai_module._split_transcript_into_chunks(text, 1000) == [text]


def test_split_never_loses_content_paragraph_boundaries():
    paragraphs = [f"Paragraph {i}. " + ("word " * 30) for i in range(10)]
    text = "\n\n".join(paragraphs)
    chunks = ai_module._split_transcript_into_chunks(text, 200)
    assert len(chunks) > 1
    # Every character of the source survives somewhere across the chunks —
    # only whitespace at the exact cut points may be trimmed (.strip()).
    covered = "".join(chunks)
    assert covered.replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


def test_split_no_boundaries_hard_cuts_without_losing_data():
    text = "a" * 5000  # no whitespace/punctuation anywhere to break on
    chunks = ai_module._split_transcript_into_chunks(text, 1000)
    assert len(chunks) == 5
    assert "".join(chunks) == text


def test_split_each_chunk_within_budget_or_is_a_forced_single_run():
    text = ("Sentence one. Sentence two! Sentence three? " * 50) + ("nobreaklongrun" * 300)
    chunks = ai_module._split_transcript_into_chunks(text, 800)
    assert len(chunks) > 1
    # No words lost or duplicated across the cut points (per-chunk .strip()
    # legitimately drops a little boundary whitespace, so compare content
    # with whitespace normalized rather than requiring byte-exact equality).
    covered = "".join(chunks)
    assert covered.replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


# ── _transcript_chunk_char_budget ───────────────────────────────────────────

def test_chunk_budget_uses_default_when_num_ctx_unset(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    budget = ai_module._transcript_chunk_char_budget()
    expected = (ai_module._DEFAULT_ASSUMED_CTX_TOKENS - ai_module._CHUNK_RESERVED_TOKENS) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    assert budget == expected


def test_chunk_budget_respects_configured_num_ctx(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    budget = ai_module._transcript_chunk_char_budget()
    expected = (8192 - ai_module._CHUNK_RESERVED_TOKENS) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    assert budget == expected


def test_chunk_budget_has_a_floor_for_a_tiny_configured_num_ctx(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 100})
    budget = ai_module._transcript_chunk_char_budget()
    assert budget == 500 * ai_module._CHARS_PER_TOKEN_ESTIMATE


# ── summarize_transcript map-reduce orchestration ───────────────────────────

@pytest.mark.asyncio
async def test_short_transcript_uses_a_single_call(monkeypatch):
    calls = []

    async def fake_generate_chat(messages, system="", model=""):
        calls.append({"messages": messages, "system": system})
        return "A short recap."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    result = await ai_module.summarize_transcript("We went to the tavern.")
    assert result == "A short recap."
    assert len(calls) == 1
    assert calls[0]["system"] == ai_module._SUMMARIZE_TRANSCRIPT_SYSTEM


@pytest.mark.asyncio
async def test_long_transcript_maps_then_reduces(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)
    calls = []

    async def fake_generate_chat(messages, system="", model=""):
        calls.append({"content": messages[0]["content"], "system": system})
        if system == ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM:
            return f"[events from: {messages[0]['content'][:10]}]"
        return "Final combined recap."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result == "Final combined recap."
    map_calls = [c for c in calls if c["system"] == ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM]
    reduce_calls = [c for c in calls if c["system"] == ai_module._SUMMARIZE_TRANSCRIPT_REDUCE_SYSTEM]
    assert len(map_calls) > 1
    assert len(reduce_calls) == 1
    # The reduce call's input is built from the map calls' outputs, in order.
    for i in range(len(map_calls)):
        assert f"Part {i + 1}:" in reduce_calls[0]["content"]


@pytest.mark.asyncio
async def test_long_transcript_propagates_a_map_step_failure(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "[AI unavailable: ConnectionError: Failed to connect to Ollama.]"
        return "[events]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[AI unavailable")
    # Stops at the failing chunk rather than continuing on to a reduce call
    # that would weave the error string into the recap as if it were content.
    assert call_count["n"] == 2
