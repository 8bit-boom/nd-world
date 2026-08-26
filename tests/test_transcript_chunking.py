"""Tests for app.ai's transcript chunking (_split_transcript_into_chunks,
_transcript_chunk_char_budget) and summarize_transcript's map-then-refine
path — added because a raw Whisper transcript of a multi-hour session can
easily run to tens of thousands of tokens, and the previous single-
generate_chat-call implementation silently let Ollama truncate anything
that didn't fit, so the recap quietly covered only part of the session
with no signal anything was lost. A transcript that fits in one context
window still goes through exactly one generate_chat call, unchanged from
before. The chunked path used to reduce every chunk's extracted events in
one final combine call, which could itself overflow context once there
were enough chunks — see test_long_transcript_maps_then_refines_incrementally
for why it's now an incremental refine (one call per chunk) instead.
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
async def test_long_transcript_maps_then_refines_incrementally(monkeypatch):
    """Replaces a single "combine every chunk summary in one call" reduce
    step (which could itself overflow the same context budget chunking
    exists to avoid, once there are enough chunks) with an incremental
    refine: each chunk's extracted events are folded into a running recap
    one call at a time, so no single call ever has to ingest more than one
    chunk's worth of new material plus the recap-so-far — see
    summarize_transcript's docstring."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)
    calls = []
    refine_n = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        content = messages[0]["content"]
        calls.append({"content": content, "system": system})
        if system == ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM:
            return f"[events from: {content[:10]}]"
        refine_n["n"] += 1
        return f"Recap draft {refine_n['n']}"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    map_calls = [c for c in calls if c["system"] == ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM]
    refine_calls = [c for c in calls if c["system"] == ai_module._SUMMARIZE_TRANSCRIPT_REFINE_SYSTEM]
    assert len(map_calls) > 1
    # One refine call per chunk (not one big combine call at the end) is
    # exactly what keeps every call's input bounded.
    assert len(refine_calls) == len(map_calls)
    assert result == f"Recap draft {len(refine_calls)}"  # the LAST refine call's output
    # Calls interleave map/refine per chunk, in order — not every map first
    # followed by a single combine call.
    assert [c["system"] for c in calls] == [
        ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM if i % 2 == 0 else ai_module._SUMMARIZE_TRANSCRIPT_REFINE_SYSTEM
        for i in range(len(calls))
    ]
    # The first refine call has no prior recap to build on; each later one
    # is fed the PREVIOUS refine call's own output — a fixed-size "recap so
    # far" — never a concatenation of every part processed up to that point.
    assert "none yet" in refine_calls[0]["content"].lower()
    for i in range(1, len(refine_calls)):
        assert f"Recap draft {i}" in refine_calls[i]["content"]


@pytest.mark.asyncio
async def test_on_progress_called_once_per_chunk_before_each_extraction_call(monkeypatch):
    """on_progress(current, total) is the one real, measurable progress
    signal in this whole pipeline (Whisper's own transcription has none at
    all — see transcribe_audio) — audio_jobs.py persists it to the job row
    so a GM sees real "part N of M" progress instead of a bare
    "summarizing" placeholder."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)

    async def fake_generate_chat(messages, system="", model=""):
        return "[part]" if system == ai_module._SUMMARIZE_TRANSCRIPT_CHUNK_SYSTEM else "Final recap."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    progress_calls = []
    long_transcript = ("The party explored the ruins. " * 30).strip()
    await ai_module.summarize_transcript(long_transcript, on_progress=lambda c, t: progress_calls.append((c, t)))

    assert len(progress_calls) > 1
    total = progress_calls[0][1]
    assert all(t == total for _, t in progress_calls)
    assert [c for c, _ in progress_calls] == list(range(1, total + 1))


@pytest.mark.asyncio
async def test_on_progress_not_called_for_a_short_unchunked_transcript(monkeypatch):
    async def fake_generate_chat(messages, system="", model=""):
        return "A short recap."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    progress_calls = []
    await ai_module.summarize_transcript("We went to the tavern.", on_progress=lambda c, t: progress_calls.append((c, t)))
    assert progress_calls == []


@pytest.mark.asyncio
async def test_long_transcript_propagates_a_map_step_failure(monkeypatch):
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "[AI unavailable: ConnectionError: Failed to connect to Ollama.]"
        return "[events]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[AI unavailable")
    # Stops at the failing chunk's extraction call rather than continuing on
    # to refine a recap out of an error string as if it were content.
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_long_transcript_propagates_a_refine_step_failure(monkeypatch):
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "[AI unavailable: ConnectionError: Failed to connect to Ollama.]"
        return "[events]"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[AI unavailable")
    # The first chunk's extraction (call 1) succeeded; its refine call
    # (call 2) is what fails here — stops immediately rather than feeding
    # the error string back in as "the recap so far" for the next chunk.
    assert call_count["n"] == 2
