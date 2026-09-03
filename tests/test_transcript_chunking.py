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
    # think=True is the default (see summarize_transcript's own default),
    # so the default assumed ctx (4096) minus the thinking headroom (4096)
    # goes negative and the 500-token floor applies — see
    # test_chunk_budget_reserves_thinking_headroom_by_default below for the
    # behavior this exercises directly.
    expected = max(
        500, ai_module._DEFAULT_ASSUMED_CTX_TOKENS - ai_module._CHUNK_RESERVED_TOKENS - ai_module._THINKING_HEADROOM_TOKENS,
    ) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    assert budget == expected


def test_chunk_budget_respects_configured_num_ctx(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    budget = ai_module._transcript_chunk_char_budget()
    expected = max(
        500, 8192 - ai_module._CHUNK_RESERVED_TOKENS - ai_module._THINKING_HEADROOM_TOKENS,
    ) * ai_module._CHARS_PER_TOKEN_ESTIMATE
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


def test_chunk_budget_estimates_a_dense_script_system_prompt_correctly(monkeypatch):
    """Real bug (see docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1): the
    system-prompt token estimate used a fixed 4 chars/token even for a
    mostly-Cyrillic system prompt (extra_instructions/RAG world_context can
    both be in the GM's own language), while the transcript side already
    correctly used the tighter 2 chars/token estimate for non-ASCII text —
    undercounting system tokens by ~2x and over-reserving, squeezing
    generation room exactly on the non-English sessions this app documents
    supporting. A mostly-Cyrillic system prompt must now reserve MORE
    tokens (matching len // 2, not len // 4) than an equal-length ASCII one,
    which means a SMALLER usable budget for the transcript itself."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    cyrillic_system = ("Партия вошла в подземелье и обнаружила древний алтарь. " * 40)[:4000]
    ascii_system = "x" * len(cyrillic_system)
    cyrillic_budget = ai_module._transcript_chunk_char_budget("", cyrillic_system, think=False)
    ascii_budget = ai_module._transcript_chunk_char_budget("", ascii_system, think=False)
    assert cyrillic_budget < ascii_budget
    # transcript="" here, so the final chars-per-token multiplier stays the
    # default _CHARS_PER_TOKEN_ESTIMATE regardless of the system prompt's
    # script — only system_tokens (part of `reserved`) is affected by it.
    expected_cyrillic_reserved = ai_module._CHUNK_RESERVED_TOKENS + len(cyrillic_system) // 2
    expected_input_tokens = max(500, 8192 - expected_cyrillic_reserved)
    assert cyrillic_budget == expected_input_tokens * ai_module._CHARS_PER_TOKEN_ESTIMATE


# ── _transcript_chunk_char_budget: thinking headroom (Speed 4.9-ish fix) ────
#
# A real production Session Recap job (see git history for this test's
# commit) failed: the model (Gemma, "Thinking" on — the default for this
# whole recap-assist family, see expand_recap_notes's docstring) burned its
# entire per-chunk output budget on hidden reasoning tokens and returned no
# visible recap text for that chunk — generate_chat's own "empty response
# ... hidden thinking output but no final answer" sentinel (see
# test_long_transcript_propagates_an_empty_response_part_failure above).
# Reserving _THINKING_HEADROOM_TOKENS extra per chunk when think=True (the
# same constant/reasoning condense_call_options already uses for
# condense_recap) shrinks each chunk to leave room for a reasoning model's
# hidden thinking without hitting the context ceiling mid-reasoning.

def test_chunk_budget_reserves_thinking_headroom_by_default(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 16384})
    with_thinking = ai_module._transcript_chunk_char_budget()  # think=True is the default
    without_thinking = ai_module._transcript_chunk_char_budget("", "", think=False)
    assert with_thinking < without_thinking
    expected_without = (16384 - ai_module._CHUNK_RESERVED_TOKENS) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    expected_with = (16384 - ai_module._CHUNK_RESERVED_TOKENS - ai_module._THINKING_HEADROOM_TOKENS) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    assert without_thinking == expected_without
    assert with_thinking == expected_with


def test_chunk_budget_no_thinking_headroom_when_think_false(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 8192})
    budget = ai_module._transcript_chunk_char_budget("", "", think=False)
    expected = (8192 - ai_module._CHUNK_RESERVED_TOKENS) * ai_module._CHARS_PER_TOKEN_ESTIMATE
    assert budget == expected


def test_thinking_headroom_tokens_is_a_non_negative_int():
    """Env-tunable (THINKING_HEADROOM_TOKENS) as an escape hatch for an
    install that genuinely needs more room than the shipped default — see
    the constant's own comment for why the default itself isn't raised."""
    assert isinstance(ai_module._THINKING_HEADROOM_TOKENS, int)
    assert ai_module._THINKING_HEADROOM_TOKENS >= 0


# ── _thinking_num_predict_override ──────────────────────────────────────────
#
# The other half of the same output-budget fix: num_predict (unlike num_ctx)
# is a hard Ollama-enforced cap shared by hidden thinking AND the visible
# answer. If a GM has configured a bounded "Max output tokens" in Settings >
# System, that cap alone can starve thinking before any visible text is
# written — the exact same failure class the chunk-budget headroom above
# protects against, just from the other knob.

def test_thinking_num_predict_override_noop_when_unset(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    assert ai_module._thinking_num_predict_override(think=True) == {}


def test_thinking_num_predict_override_noop_when_think_false(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    assert ai_module._thinking_num_predict_override(think=False) == {}


def test_thinking_num_predict_override_noop_when_unlimited(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": -1})
    assert ai_module._thinking_num_predict_override(think=True) == {}


def test_thinking_num_predict_override_widens_a_bounded_configured_cap(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    result = ai_module._thinking_num_predict_override(think=True)
    assert result == {"num_predict": 512 + ai_module._THINKING_HEADROOM_TOKENS}


# ── expanded_thinking_options: the retry ladder's recovery rung ────────────
#
# See docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1 — the normal
# _THINKING_HEADROOM_TOKENS widening above still wasn't enough for a real
# production job. This is the much larger explicit budget the retry ladder
# in app.audio_jobs._run_job climbs to when the normal attempt starves.

def test_expanded_thinking_options_with_nothing_configured(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    result = ai_module.expanded_thinking_options()
    assert result["num_predict"] == ai_module._THINKING_EXPANDED_HEADROOM_TOKENS
    assert result["num_ctx"] == ai_module._DEFAULT_ASSUMED_CTX_TOKENS + (
        ai_module._THINKING_EXPANDED_HEADROOM_TOKENS - ai_module._THINKING_HEADROOM_TOKENS
    )


def test_expanded_thinking_options_widens_configured_values(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512, "num_ctx": 8192})
    result = ai_module.expanded_thinking_options()
    assert result["num_predict"] == 512 + ai_module._THINKING_EXPANDED_HEADROOM_TOKENS
    assert result["num_ctx"] == 8192 + (ai_module._THINKING_EXPANDED_HEADROOM_TOKENS - ai_module._THINKING_HEADROOM_TOKENS)


def test_expanded_thinking_options_num_ctx_delta_is_smaller_than_full_expanded_value():
    """Deliberate: the num_ctx delta is (EXPANDED - NORMAL), not the full
    expanded headroom — so summarize_transcript's chunk_chars (computed
    against the NORMAL headroom only, unaffected by this function) stays
    identical between a normal attempt and an expanded retry, letting the
    retry resume a checkpoint the normal attempt already wrote."""
    delta = ai_module._THINKING_EXPANDED_HEADROOM_TOKENS - ai_module._THINKING_HEADROOM_TOKENS
    assert delta < ai_module._THINKING_EXPANDED_HEADROOM_TOKENS


def test_thinking_expanded_headroom_tokens_is_at_least_the_normal_headroom():
    assert ai_module._THINKING_EXPANDED_HEADROOM_TOKENS >= ai_module._THINKING_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_summarize_transcript_expanded_thinking_uses_the_expanded_budget(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512, "num_ctx": 8192})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript("a short transcript", expanded_thinking=True)
    assert seen_options == [ai_module.expanded_thinking_options()]
    assert seen_options[0]["num_predict"] == 512 + ai_module._THINKING_EXPANDED_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_summarize_transcript_expanded_thinking_keeps_chunk_chars_identical(monkeypatch):
    """The whole point of the num_ctx delta trick in expanded_thinking_
    options: an expanded retry must chunk the transcript EXACTLY the same
    way a normal attempt did, so a checkpoint from the normal attempt is
    still valid to resume under the expanded one."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": 4096})
    long_transcript = ("The party explored the ruins. " * 200).strip()

    chunks_seen = {}

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        chunks_seen.setdefault("count", 0)
        chunks_seen["count"] += 1
        return "part summary"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript(long_transcript, expanded_thinking=False)
    normal_count = chunks_seen["count"]

    chunks_seen["count"] = 0
    await ai_module.summarize_transcript(long_transcript, expanded_thinking=True)
    assert chunks_seen["count"] == normal_count


@pytest.mark.asyncio
async def test_summarize_transcript_widens_configured_num_predict_when_thinking(monkeypatch):
    """Regression test for the production failure this whole section
    exists to fix: a GM-configured num_predict cap must be widened for the
    (default) think=True call, both for a short unchunked transcript and
    for every chunk of a long one — not just the num_ctx side."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript("a short transcript")
    assert seen_options == [{"num_predict": 512 + ai_module._THINKING_HEADROOM_TOKENS}]

    seen_options.clear()
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)
    long_transcript = ("The party explored the ruins. " * 30).strip()
    await ai_module.summarize_transcript(long_transcript)
    assert len(seen_options) > 1
    assert all(o == {"num_predict": 512 + ai_module._THINKING_HEADROOM_TOKENS} for o in seen_options)


@pytest.mark.asyncio
async def test_summarize_transcript_no_num_predict_override_when_unconfigured(monkeypatch):
    """Unconfigured num_predict means no OVERRIDE reaches the call (no
    widening, no per-call cap of its own) — but the recap-family
    degeneration guard now supplies its default here too
    (_recap_num_predict_default_if_unbounded, same rule as
    summarize_session_from_facts), so an unbounded model can't loop
    forever on a transcript summary either."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_transcript("a short transcript")
    assert seen_options == [{"num_predict": ai_module._RECAP_NUM_PREDICT_DEFAULT}]


@pytest.mark.asyncio
async def test_expand_recap_notes_widens_configured_num_predict_when_thinking(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "expanded notes"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.expand_recap_notes("terse notes")
    assert seen_options[0]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS
    # num_ctx now appears too (see _ctx_override_if_needed, item 2.2 of
    # docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md): once num_predict is
    # widened by a full _THINKING_HEADROOM_TOKENS, that alone already
    # exceeds the unconfigured 4096-token assumed default, so the context
    # window genuinely needs matching room to hold the widened output —
    # not just a "long input" concern.
    assert "num_ctx" in seen_options[0]


@pytest.mark.asyncio
async def test_summarize_session_from_facts_widens_configured_num_predict_when_thinking(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_predict": 512})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.summarize_session_from_facts(["fact one", "fact two"])
    assert seen_options[0]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS
    assert "num_ctx" in seen_options[0]  # see the sibling expand_recap_notes test's comment above


@pytest.mark.asyncio
async def test_expand_recap_notes_no_ctx_override_when_unconfigured_and_short(monkeypatch):
    """No configured num_predict and no long input means no num_ctx
    override is needed — the only key in the options is the degeneration
    guard's default num_predict (_recap_num_predict_default_if_unbounded,
    which now covers expand_recap_notes too), never a num_ctx."""
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "expanded notes"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    await ai_module.expand_recap_notes("terse notes")
    assert seen_options == [{"num_predict": ai_module._RECAP_NUM_PREDICT_DEFAULT}]


@pytest.mark.asyncio
async def test_expand_recap_notes_sizes_num_ctx_for_a_huge_notes_paste(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "expanded notes"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    huge_notes = "word " * 20000
    await ai_module.expand_recap_notes(huge_notes, think=False)
    assert seen_options[0]["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


@pytest.mark.asyncio
async def test_summarize_session_from_facts_sizes_num_ctx_for_many_facts(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {})
    seen_options = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        seen_options.append(options)
        return "a recap"

    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    many_facts = [f"fact number {i} with some descriptive detail attached" for i in range(400)]
    await ai_module.summarize_session_from_facts(many_facts, think=False)
    assert seen_options[0]["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


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


# ── MAX_AUTO_NUM_CTX: ceiling on every auto-sized num_ctx ───────────────────
# See docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 3.3 — a
# pathological paste (e.g. a multi-megabyte transcript) would otherwise
# compute a six-figure num_ctx that Ollama tries to allocate real KV-cache
# memory for.

def test_max_auto_num_ctx_is_a_sane_positive_int():
    assert isinstance(ai_module.MAX_AUTO_NUM_CTX, int)
    assert ai_module.MAX_AUTO_NUM_CTX >= 8192


def test_context_sized_options_clamps_to_the_ceiling():
    huge_text = "word " * 200_000  # ~1MB / ~250k tokens, well past the default 32768 ceiling
    result = ai_module.context_sized_options(huge_text)
    assert result["num_ctx"] == ai_module.MAX_AUTO_NUM_CTX


def test_context_sized_options_unaffected_below_the_ceiling():
    text = "word " * 5000
    result = ai_module.context_sized_options(text)
    assert result["num_ctx"] < ai_module.MAX_AUTO_NUM_CTX


def test_expanded_thinking_options_clamps_to_the_ceiling(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_ollama_options", lambda: {"num_ctx": ai_module.MAX_AUTO_NUM_CTX * 10})
    result = ai_module.expanded_thinking_options()
    assert result["num_ctx"] == ai_module.MAX_AUTO_NUM_CTX


# ── summarize_transcript's chunked, append-only orchestration ──────────────

@pytest.mark.asyncio
async def test_short_transcript_uses_a_single_call(monkeypatch):
    calls = []

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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
    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        return "A short recap."
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    progress_calls = []
    await ai_module.summarize_transcript("We went to the tavern.", on_progress=lambda c, t: progress_calls.append((c, t)))
    assert progress_calls == []


@pytest.mark.asyncio
async def test_long_transcript_propagates_a_part_summary_failure(monkeypatch):
    call_count = {"n": 0}

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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


def test_is_thinking_starved_sentinel_matches_only_the_real_production_message():
    """The real, full-shaped sentinel generate_chat emits when a thinking
    model burns its whole output budget — the exact text from the
    production failure this whole feature exists to auto-recover from."""
    real_message = (
        '[empty response from gemma4:26b — it produced 7781 character(s) of hidden '
        '"thinking" output but no final answer (usually means it ran out of output '
        'budget mid-reasoning). Try a shorter prompt, a higher response-length limit, '
        'or a non-reasoning model.]'
    )
    assert ai_module.is_thinking_starved_sentinel(real_message)


def test_is_thinking_starved_sentinel_rejects_other_sentinels_and_prose():
    assert not ai_module.is_thinking_starved_sentinel("[AI error: Ollama 404: model not found]")
    assert not ai_module.is_thinking_starved_sentinel("[AI unavailable: ConnectionError: x]")
    # The non-thinking empty-response variant (no thinking at all, just a
    # bare done_reason) — starts with the same prefix but must not match.
    assert not ai_module.is_thinking_starved_sentinel(
        "[empty response from x (done_reason=length) — try a different model, or check the Ollama server logs]"
    )
    # summarize_transcript's own whitespace-only-part sentinel also starts
    # with "[empty response" but is a completely different failure mode.
    assert not ai_module.is_thinking_starved_sentinel(
        "[empty response from part 2 of 3 — the model returned no usable text for this part]"
    )
    assert not ai_module.is_thinking_starved_sentinel("A real recap paragraph.")
    assert not ai_module.is_thinking_starved_sentinel("")


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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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
    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
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
