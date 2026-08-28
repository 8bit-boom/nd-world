"""Tests for app.ai's transcript chunking (_split_transcript_into_chunks,
_transcript_chunk_char_budget) and summarize_transcript's chunked path —
added because a raw Whisper transcript of a multi-hour session can easily
run to tens of thousands of tokens, and the previous single-generate_chat-
call implementation silently let Ollama truncate anything that didn't fit,
so the recap quietly covered only part of the session with no signal
anything was lost. A transcript that fits in one context window still goes
through exactly one generate_chat call, unchanged from before.

A long transcript is summarized part-by-part, and the final recap is just
those part-summaries joined together in order — there is NO further LLM
call over the combined result. Two designs were tried here and rejected:

- A single "combine every part summary into one final recap" call: that
  combined blob has to fit in one context window too, and for a long
  enough session (enough chunks) it could overflow the same budget
  chunking exists to avoid in the first place.
- An iterative "refine the recap so far with this next part's events"
  chain, one call per chunk: real models (especially smaller/local ones)
  drift toward whatever was rewritten most recently across repeated
  rewrite passes — a GM reported a recap that covered only the tail of a
  session, everything before the last couple of parts silently dropped.

Appending each part's own independent summary sidesteps both: no call
ever has to see more than one chunk's raw transcript text, and nothing is
ever re-summarized (so nothing already-written can be dropped by a later
pass).
"""
import re

import pytest

from app import ai as ai_module
from app.job_shutdown import JobInterrupted


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


def test_split_prefers_a_single_newline_over_hard_cutting_mid_word():
    # whisper.cpp's real transcript output is one segment per line, joined
    # with a bare "\n" — no blank-line paragraph breaks, no "word. "
    # sentence spacing. Without a "\n" break candidate, every cut on a real
    # transcript lands mid-word. Each line ends with a unique marker so a
    # mid-word cut is easy to detect: a clean cut leaves every non-final
    # chunk ending with a complete "ENDn" marker.
    lines = [f"segment {i} continues with some filler words here END{i}" for i in range(40)]
    text = "\n".join(lines)
    chunks = ai_module._split_transcript_into_chunks(text, 400)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert re.search(r"END\d+$", chunk), f"chunk did not end on a line boundary: {chunk!r}"
    covered = "".join(chunks)
    assert covered.replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


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


def test_chunk_budget_shrinks_for_a_long_system_prompt(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    without_system = ai_module._transcript_chunk_char_budget()
    long_system = "x" * 4000  # a GM's recap_instructions is unbounded free text
    with_system = ai_module._transcript_chunk_char_budget("", long_system)
    assert with_system < without_system


# ── _chars_per_token_estimate ───────────────────────────────────────────────

def test_chars_per_token_defaults_for_english_text():
    assert ai_module._chars_per_token_estimate("just some plain English words here") == ai_module._CHARS_PER_TOKEN_ESTIMATE


def test_chars_per_token_defaults_for_empty_text():
    assert ai_module._chars_per_token_estimate("") == ai_module._CHARS_PER_TOKEN_ESTIMATE


def test_chars_per_token_is_tighter_for_non_ascii_script():
    cyrillic = "Партия вошла в подземелье и обнаружила древний алтарь. " * 20
    assert ai_module._chars_per_token_estimate(cyrillic) == 2


def test_chunk_budget_uses_a_tighter_estimate_for_non_english_transcript(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    english = "just some plain English words here"
    cyrillic = "Партия вошла в подземелье и обнаружила древний алтарь. " * 20
    english_budget = ai_module._transcript_chunk_char_budget(english)
    cyrillic_budget = ai_module._transcript_chunk_char_budget(cyrillic)
    assert cyrillic_budget < english_budget


# ── context_sized_options ───────────────────────────────────────────────────

def test_context_sized_options_scales_with_input_length():
    short_ctx = ai_module.context_sized_options("just a few words")["num_ctx"]
    long_ctx = ai_module.context_sized_options("word " * 5000)["num_ctx"]
    assert long_ctx > short_ctx


def test_context_sized_options_has_a_floor_for_tiny_text():
    assert ai_module.context_sized_options("hi")["num_ctx"] == ai_module._CONTEXT_FIT_FLOOR_TOKENS


def test_context_sized_options_reserves_headroom_beyond_the_raw_token_count():
    text = "word " * 5000
    tokens = ai_module._chars_per_token_estimate(text)
    input_tokens = -(-len(text) // tokens)
    assert ai_module.context_sized_options(text)["num_ctx"] == input_tokens + ai_module._CONTEXT_FIT_RESERVED_TOKENS


def test_context_sized_options_is_tighter_for_non_ascii_script():
    # Same text, but the non-ASCII sample estimates more tokens per
    # character (see _chars_per_token_estimate), so it should ask for a
    # larger context than the same-length English text would.
    english = "just some plain English words here " * 200
    cyrillic = "Партия вошла в подземелье и обнаружила древний алтарь. " * 200
    assert ai_module.context_sized_options(cyrillic)["num_ctx"] > ai_module.context_sized_options(english)["num_ctx"]


def test_context_sized_options_never_mutates_instance_wide_overrides():
    before = ai_module.effective_ollama_options()
    ai_module.context_sized_options("word " * 5000)
    assert ai_module.effective_ollama_options() == before


# ── summarize_transcript's chunked, append-only orchestration ──────────────

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
async def test_long_transcript_summarizes_each_part_and_joins_them(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    calls = []

    async def fake_generate_chat(messages, system="", model=""):
        calls.append({"content": messages[0]["content"], "system": system})
        return f"Summary of part {len(calls)}."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    # One call per chunk, every one using the SAME per-part system prompt —
    # there's no separate "combine" call at all.
    assert len(calls) > 1
    assert all(c["system"] == ai_module._SUMMARIZE_TRANSCRIPT_PART_SYSTEM for c in calls)
    # The final result is exactly the part-summaries joined in order — no
    # further LLM call ever sees the combined result.
    expected = "\n\n".join(f"Summary of part {i}." for i in range(1, len(calls) + 1))
    assert result == expected


@pytest.mark.asyncio
async def test_part_summaries_receive_only_their_own_chunk_never_the_whole_transcript(monkeypatch):
    """The whole point of chunking: no single call ever sees more than one
    chunk's worth of raw transcript text, regardless of session length."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    seen_lengths = []

    async def fake_generate_chat(messages, system="", model=""):
        seen_lengths.append(len(messages[0]["content"]))
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 60).strip()
    await ai_module.summarize_transcript(long_transcript)

    assert len(seen_lengths) > 1
    assert all(length <= 50 for length in seen_lengths)


@pytest.mark.asyncio
async def test_recap_instructions_applied_to_every_part_call(monkeypatch):
    """With no final combine call, a GM's steering (World.recap_instructions
    — tone/language/focus) has to reach every part call directly, since
    each part's summary is final as written — there's no later call where
    it could be applied instead."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    systems_seen = []

    async def fake_generate_chat(messages, system="", model=""):
        systems_seen.append(system)
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    await ai_module.summarize_transcript(long_transcript, extra_instructions="write in French")

    assert len(systems_seen) > 1
    assert all("write in French" in s for s in systems_seen)


@pytest.mark.asyncio
async def test_on_progress_called_once_per_part_before_each_call(monkeypatch):
    """on_progress(current, total) is summarize_transcript's own real,
    measurable progress signal for the TEXT-chunking phase — separate from
    transcribe_audio's own on_progress for audio-chunking (see
    _split_audio_into_chunks). audio_jobs.py persists this one to the job
    row so a GM sees real "part N of M" progress during status=
    "summarizing" instead of a bare placeholder."""
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)

    async def fake_generate_chat(messages, system="", model=""):
        return "part summary"
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
async def test_long_transcript_propagates_a_part_summary_failure(monkeypatch):
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "[AI unavailable: ConnectionError: Failed to connect to Ollama.]"
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[AI unavailable")
    # Stops at the failing part rather than continuing on to join an error
    # string into the recap as if it were content.
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_long_transcript_propagates_an_empty_response_part_failure(monkeypatch):
    """Regression test: generate_chat has TWO failure-sentinel families
    ("[AI ...]" and "[empty response ...]" — see is_failure_sentinel's
    docstring), and the chunked path used to check only the first. A part
    coming back as "[empty response from <model> ...]" (a real, fairly
    common failure — a reasoning model burning its output budget on hidden
    thinking) would get silently joined into the recap as if it were a
    real paragraph, with the job still ending up "done"."""
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "[empty response from gemma4:26b (done_reason=length) — try a different model]"
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[empty response")
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_long_transcript_aborts_on_a_whitespace_only_part(monkeypatch):
    """A technically-successful call whose content is only whitespace isn't
    caught by is_failure_sentinel (it doesn't start with either sentinel
    prefix) — must still abort rather than join a blank paragraph into the
    recap where a whole chunk's events should be."""
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model=""):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return "   \n  "
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    result = await ai_module.summarize_transcript(long_transcript)

    assert result.startswith("[empty response")
    assert "part 2" in result
    assert call_count["n"] == 2


def test_is_failure_sentinel_recognizes_both_families():
    assert ai_module.is_failure_sentinel("[AI error: Ollama 404: model not found]")
    assert ai_module.is_failure_sentinel("[AI unavailable: ConnectionError: x]")
    assert ai_module.is_failure_sentinel("[empty response from x (done_reason=length) — try again]")
    assert not ai_module.is_failure_sentinel("A real recap paragraph.")
    assert not ai_module.is_failure_sentinel("")


# ── summarize_transcript: checkpoint/resume (see app/job_shutdown.py) ──────
#
# Same job-survival contract as transcribe_audio's own (see test_whisper.py)
# — a chunk's checkpoint lets audio_jobs.py persist real progress so a
# routine server restart mid-summarize doesn't discard already-written part
# summaries. chunk_chars (not chunk_seconds/audio_size, transcribe_audio's
# own validation fields) is the thing that must match on resume here: it's
# derived from num_ctx and the system prompt, either of which could differ
# between the checkpoint being written and the process restarting.

_LONG_TRANSCRIPT = ("The party explored the ruins. " * 30).strip()


@pytest.mark.asyncio
async def test_summarize_calls_on_checkpoint_after_every_part(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    calls = []

    async def fake_generate_chat(messages, system="", model=""):
        calls.append(messages[0]["content"])
        return f"Summary {len(calls)}."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    checkpoints = []
    result = await ai_module.summarize_transcript(_LONG_TRANSCRIPT, on_checkpoint=checkpoints.append)

    assert len(checkpoints) == len(calls)
    assert [c["parts_done"] for c in checkpoints] == list(range(1, len(calls) + 1))
    assert all(c["phase"] == "summarize" for c in checkpoints)
    assert all(c["chunk_total"] == len(calls) for c in checkpoints)
    assert all(c["chunk_chars"] == 50 for c in checkpoints)
    assert checkpoints[-1]["text"] == result


@pytest.mark.asyncio
async def test_summarize_on_checkpoint_not_called_for_a_short_unchunked_transcript(monkeypatch):
    async def fake_generate_chat(messages, system="", model=""):
        return "one short summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    checkpoints = []
    result = await ai_module.summarize_transcript("short transcript", on_checkpoint=checkpoints.append)
    assert result == "one short summary"
    assert checkpoints == []


@pytest.mark.asyncio
async def test_summarize_resumes_from_a_matching_checkpoint_and_skips_done_parts(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    chunks = ai_module._split_transcript_into_chunks(_LONG_TRANSCRIPT, 50)
    assert len(chunks) > 1
    seen_chunks = []

    async def fake_generate_chat(messages, system="", model=""):
        seen_chunks.append(messages[0]["content"])
        return f"Summary of {chunks.index(messages[0]['content']) if messages[0]['content'] in chunks else '?'}."

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    resume = {
        "phase": "summarize", "parts_done": 1, "chunk_total": len(chunks), "chunk_chars": 50,
        "text": "Prior summary of part 0.",
    }
    result = await ai_module.summarize_transcript(_LONG_TRANSCRIPT, resume=resume)
    assert seen_chunks == chunks[1:]  # chunk 0 was skipped, not re-summarized
    assert result.startswith("Prior summary of part 0.\n\n")


@pytest.mark.asyncio
async def test_summarize_discards_a_checkpoint_with_a_different_chunk_budget(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    chunks = ai_module._split_transcript_into_chunks(_LONG_TRANSCRIPT, 50)
    seen_chunks = []

    async def fake_generate_chat(messages, system="", model=""):
        seen_chunks.append(messages[0]["content"])
        return "a summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    resume = {
        "phase": "summarize", "parts_done": 1, "chunk_total": len(chunks), "chunk_chars": 999,  # mismatch
        "text": "stale",
    }
    await ai_module.summarize_transcript(_LONG_TRANSCRIPT, resume=resume)
    assert seen_chunks == chunks  # started over — every chunk re-summarized


@pytest.mark.asyncio
async def test_summarize_raises_job_interrupted_at_the_next_part_boundary_when_should_stop(monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)

    async def fake_generate_chat(messages, system="", model=""):
        return "a summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1

    checkpoints = []
    with pytest.raises(JobInterrupted):
        await ai_module.summarize_transcript(_LONG_TRANSCRIPT, should_stop=should_stop, on_checkpoint=checkpoints.append)
    assert len(checkpoints) == 1
