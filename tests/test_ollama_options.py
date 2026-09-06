"""Tests for the Ollama per-request generation tuning added to Settings >
System (temperature, top_p, num_ctx, mirostat, keep_alive, ...) — covers the
app.ai override plumbing (set_ollama_generation_overrides/effective_*/
_chat_kwargs, and that generate_chat/stream_chat/parse_facts_from_recap
actually splat those kwargs into the (mocked) Ollama client call), plus the
Settings save/validation round-trip and that a save pushes the new values
into app.ai without a restart (mirroring test_settings_system.py's existing
ollama_url/ollama_model override tests).

Also covers POST /settings/system's handling of the server-level ("Bucket
A") Ollama env vars and GET /api/ai/hardware, at the route level — the pure
module functions behind both (app/ollama_tuning.py's sanitize_server_env/
detect_hardware/recommend_settings) have their own dedicated test files
(tests/test_ollama_server_env.py, tests/test_hardware_detect.py,
tests/test_ollama_recommend.py).
"""
import json
import os
import types

import ollama
import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import AppSettings

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


class _FakeResp:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeChatClient:
    """Records every kwargs dict passed to .chat() so tests can assert
    options=/keep_alive= were (or weren't) included. show_capabilities
    controls what .show() reports — used by _chat_kwargs' own think=True →
    False downgrade for a model untagged as thinking-capable (see
    app.ai._model_supports_thinking). Defaults to thinking-capable so
    existing think=True tests don't need to know this plumbing exists."""

    def __init__(self, calls, show_capabilities=("thinking",)):
        self._calls = calls
        self._show_capabilities = list(show_capabilities)
        self.show_calls = 0

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeResp("hi")
            return _gen()
        if kwargs.get("format"):
            return _FakeResp('{"facts": []}')
        return _FakeResp("hi")

    async def show(self, model):
        self.show_calls += 1
        return types.SimpleNamespace(capabilities=self._show_capabilities)


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    ai_module._model_capabilities_cache.clear()
    ai_module._model_thinking_failures.clear()
    ai_module._prompt_token_thinking_models.clear()
    yield
    ai_module.set_ollama_generation_overrides({})
    ai_module._model_capabilities_cache.clear()
    ai_module._model_thinking_failures.clear()
    ai_module._prompt_token_thinking_models.clear()


# ── app.ai override plumbing ────────────────────────────────────────────────

def test_effective_options_empty_by_default():
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


def test_set_generation_overrides_roundtrip():
    ai_module.set_ollama_generation_overrides({"temperature": 0.5, "num_ctx": 8192}, "10m")
    assert ai_module.effective_ollama_options() == {"temperature": 0.5, "num_ctx": 8192}
    assert ai_module.effective_ollama_keep_alive() == "10m"


def test_set_generation_overrides_clears_back_to_empty():
    ai_module.set_ollama_generation_overrides({"temperature": 0.5}, "10m")
    ai_module.set_ollama_generation_overrides({})
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


@pytest.mark.asyncio
async def test_generate_chat_omits_kwargs_when_unset(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "hi"
    assert "options" not in calls[0]
    assert "keep_alive" not in calls[0]
    assert calls[0]["think"] is False


@pytest.mark.asyncio
async def test_generate_chat_passes_options_and_keep_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"temperature": 0.2, "top_k": 40}, "5m")
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert result == "hi"
    assert calls[0]["options"] == {"temperature": 0.2, "top_k": 40}
    assert calls[0]["keep_alive"] == "5m"


@pytest.mark.asyncio
async def test_generate_chat_per_call_options_override_instance_default_for_that_call_only(monkeypatch):
    """A per-call `options=` kwarg (e.g. context_sized_options's num_ctx)
    layers over the instance-wide default — overriding a shared key, adding
    a new one — and, since it's never written into
    set_ollama_generation_overrides' own state, the very next call reverts
    to the plain instance default with no explicit "reset" step needed."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"temperature": 0.2, "num_ctx": 4096})

    await ai_module.generate_chat([{"role": "user", "content": "hi"}], options={"num_ctx": 20000})
    assert calls[0]["options"] == {"temperature": 0.2, "num_ctx": 20000}

    await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert calls[1]["options"] == {"temperature": 0.2, "num_ctx": 4096}


@pytest.mark.asyncio
async def test_condense_recap_forwards_options_to_generate_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a long recap", options=ai_module.context_sized_options("a long recap"))
    # The caller's options arrive intact, plus the degeneration-guard
    # default num_predict (_recap_num_predict_default_if_unbounded — nothing
    # else bounded this call, see _RECAP_NUM_PREDICT_DEFAULT's comment).
    assert calls[0]["options"] == {
        **ai_module.context_sized_options("a long recap"),
        "num_predict": ai_module._RECAP_NUM_PREDICT_DEFAULT,
    }


@pytest.mark.asyncio
async def test_condense_recap_extra_instructions_reach_the_system_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", extra_instructions="focus on combat")
    system = calls[0]["messages"][0]["content"]
    assert "focus on combat" in system


@pytest.mark.asyncio
async def test_condense_recap_max_tokens_sets_num_predict_when_not_thinking(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", max_tokens=150, think=False)
    assert calls[0]["options"]["num_predict"] == 150
    system = calls[0]["messages"][0]["content"]
    assert "150" in system


@pytest.mark.asyncio
async def test_condense_recap_max_tokens_is_prompt_only_when_thinking(monkeypatch):
    """think=True means hidden reasoning tokens share num_predict's budget
    with the visible answer — a real, reported failure was the model
    spending its whole num_predict budget on reasoning and writing no
    visible answer at all. So with thinking on, max_tokens becomes prompt
    guidance only, same contract min_tokens already has — max_tokens sets
    no num_predict cap of its own. (What DOES appear when nothing is
    configured: the degeneration-guard default — _RECAP_NUM_PREDICT_
    DEFAULT, see _recap_num_predict_default_if_unbounded — which is not a
    max_tokens-derived cap and applies to thinking and non-thinking calls
    alike so a degenerating model can't loop forever.)"""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", max_tokens=150, think=True)
    assert calls[0]["options"]["num_predict"] == ai_module._RECAP_NUM_PREDICT_DEFAULT
    system = calls[0]["messages"][0]["content"]
    assert "150" in system


@pytest.mark.asyncio
async def test_condense_recap_widens_a_configured_num_predict_when_thinking(monkeypatch):
    """The gap test_condense_recap_max_tokens_is_prompt_only_when_thinking
    doesn't cover: with think=True and NO max_tokens at all, a GM-configured
    instance-wide num_predict (Settings > System > "Max output tokens")
    used to reach Ollama completely unwidened — the exact failure class
    this whole recap-assist family already guards against everywhere else
    (see _thinking_num_predict_override)."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    await ai_module.condense_recap("a recap", think=True)
    assert calls[0]["options"]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_condense_recap_widened_num_predict_wins_over_max_tokens_when_thinking(monkeypatch):
    """max_tokens never sets a hard cap when think=True (see
    test_condense_recap_max_tokens_is_prompt_only_when_thinking) — a
    configured num_predict must still be the one thing widened, not 150."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    await ai_module.condense_recap("a recap", max_tokens=150, think=True)
    assert calls[0]["options"]["num_predict"] == 512 + ai_module._THINKING_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_condense_recap_max_tokens_hard_cap_unaffected_when_not_thinking(monkeypatch):
    """think=False is unchanged by this fix: max_tokens still wins outright
    as the literal num_predict value, ignoring any configured instance-wide
    cap — same contract test_condense_recap_max_tokens_sets_num_predict_
    when_not_thinking already pins, just with a configured override present
    too, to prove the new widening never fires for think=False."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    await ai_module.condense_recap("a recap", max_tokens=150, think=False)
    assert calls[0]["options"]["num_predict"] == 150


@pytest.mark.asyncio
async def test_condense_recap_max_tokens_layers_onto_existing_options(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", options={"num_ctx": 4096}, max_tokens=150, think=False)
    assert calls[0]["options"] == {"num_ctx": 4096, "num_predict": 150}


@pytest.mark.asyncio
async def test_condense_recap_min_tokens_is_prompt_only_no_options_change(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", min_tokens=80)
    # The degeneration guard supplies a default num_predict when nothing
    # else did (min_tokens is prompt-only guidance) — an unbounded
    # condense was the digit-loop/repetition failure mode.
    assert calls[0]["options"] == {"num_predict": 1024}
    system = calls[0]["messages"][0]["content"]
    assert "80" in system


@pytest.mark.asyncio
async def test_condense_recap_no_length_notes_when_neither_bound_given(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap")
    system = calls[0]["messages"][0]["content"]
    assert "Length target" not in system


# ── condense_recap: strictness (guideline vs firm/strict length wording) ────
# "guideline" is the original best-effort wording (pinned here so a reword
# can't silently drift); "firm"/"strict" reword the same targets as
# mandatory requirements and mark the GM's extra instructions binding.

@pytest.mark.asyncio
async def test_condense_recap_guideline_keeps_the_original_soft_wording(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", min_tokens=80, max_tokens=200, strictness="guideline")
    system = calls[0]["messages"][0]["content"]
    assert "don't cut it any shorter" in system
    assert "no more than ~200 tokens" in system
    # The mandatory phrasing must not leak into the soft default.
    assert "MUST be at least" not in system
    assert "REQUIRED" not in system


@pytest.mark.asyncio
async def test_condense_recap_firm_strictness_makes_min_max_requirements(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", min_tokens=80, max_tokens=200, strictness="firm")
    system = calls[0]["messages"][0]["content"]
    assert "MUST be at least ~80 tokens" in system
    assert "stay at or below ~200 tokens" in system
    # The soft wording must be fully replaced, not appended alongside.
    assert "don't cut it any shorter" not in system


@pytest.mark.asyncio
async def test_condense_recap_firm_marks_extra_instructions_binding(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", extra_instructions="focus on combat", strictness="firm")
    system = calls[0]["messages"][0]["content"]
    assert "Treat the extra instructions below as binding requirements, not suggestions." in system
    assert "focus on combat" in system
    # The compliance line sits before the instructions it binds — its
    # "below" is only true in that order.
    assert system.index("binding requirements") < system.index("focus on combat")


@pytest.mark.asyncio
async def test_condense_recap_guideline_never_marks_instructions_binding(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", extra_instructions="focus on combat")
    system = calls[0]["messages"][0]["content"]
    assert "binding requirements" not in system
    assert "focus on combat" in system


@pytest.mark.asyncio
async def test_condense_recap_no_binding_line_when_no_extra_instructions(monkeypatch):
    """The compliance sentence only makes sense when there ARE instructions
    below it — with none, firm/strict must not append a dangling reference."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", min_tokens=80, strictness="strict")
    system = calls[0]["messages"][0]["content"]
    assert "binding requirements" not in system
    assert "MUST be at least ~80 tokens" in system


@pytest.mark.asyncio
async def test_condense_recap_rejects_an_unknown_strictness():
    with pytest.raises(ValueError):
        await ai_module.condense_recap("a recap", strictness="bogus")


# ── condense_recap: expanded_thinking (the retry ladder's recovery rung) ───
# See docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1.

@pytest.mark.asyncio
async def test_condense_recap_expanded_thinking_uses_the_expanded_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    try:
        await ai_module.condense_recap("a recap", think=True, expanded_thinking=True)
    finally:
        ai_module.set_ollama_generation_overrides({})
    assert calls[0]["options"]["num_predict"] == 512 + ai_module._THINKING_EXPANDED_HEADROOM_TOKENS


@pytest.mark.asyncio
async def test_condense_recap_expanded_thinking_overrides_max_tokens_num_predict(monkeypatch):
    """The expanded rung only ever runs because a prior attempt already
    starved — max_tokens' own think=False num_predict branch must not win
    over the guaranteed-larger expanded budget."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", max_tokens=150, think=False, expanded_thinking=True)
    assert calls[0]["options"]["num_predict"] == ai_module.expanded_thinking_options()["num_predict"]
    assert calls[0]["options"]["num_predict"] != 150


def test_context_sized_options_reserve_tokens_widens_num_ctx():
    text = "word " * 2000
    default_reserve = ai_module.context_sized_options(text)
    wider_reserve = ai_module.context_sized_options(text, reserve_tokens=5000)
    assert wider_reserve["num_ctx"] > default_reserve["num_ctx"]


# ── condense_call_options (context-overflow safety net for plain Condense) ──
#
# A long transcript/recap that overflows the model's real context gets
# silently truncated by Ollama instead of raising — in practice (Gemma)
# this can corrupt the prompt badly enough that the model responds with a
# run of reserved/unused vocabulary tokens instead of an error, which still
# reads back as a "done" job since it's real (if garbage) text. Unlike
# summarize_transcript's chunking, condense_recap is a single unchunked
# call with nothing else protecting it — these tests cover the safety net
# that closes that gap.

def test_condense_call_options_none_for_a_short_recap_no_configured_ctx():
    assert ai_module.condense_call_options("short recap") is None


def test_condense_call_options_widens_when_input_exceeds_assumed_default():
    long_transcript = "word " * 20000  # ~20000 tokens, well past _DEFAULT_ASSUMED_CTX_TOKENS
    options = ai_module.condense_call_options(long_transcript)
    assert options is not None
    assert options["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


def test_condense_call_options_accounts_for_extra_instructions_and_world_context_length():
    long_transcript = "word " * 20000
    bare = ai_module.condense_call_options(long_transcript)
    padded = ai_module.condense_call_options(
        long_transcript,
        extra_instructions="x" * 20000,
        world_context="y" * 20000,
    )
    assert padded["num_ctx"] > bare["num_ctx"]


def test_condense_call_options_accounts_for_max_tokens_headroom():
    long_transcript = "word " * 20000
    without_max = ai_module.condense_call_options(long_transcript)
    with_max = ai_module.condense_call_options(long_transcript, max_tokens=8000)
    assert with_max["num_ctx"] > without_max["num_ctx"]


def test_condense_call_options_respects_a_larger_gm_configured_ctx():
    long_transcript = "word " * 5000  # would otherwise exceed the 4096 assumed default
    ai_module.set_ollama_generation_overrides({"num_ctx": 32768})
    try:
        assert ai_module.condense_call_options(long_transcript) is None
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_condense_call_options_still_widens_past_a_too_small_gm_configured_ctx():
    long_transcript = "word " * 20000
    ai_module.set_ollama_generation_overrides({"num_ctx": 2048})
    try:
        options = ai_module.condense_call_options(long_transcript)
        assert options is not None
        assert options["num_ctx"] > 2048
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_condense_call_options_force_fit_always_returns_a_value_even_for_a_short_recap():
    options = ai_module.condense_call_options("short recap", force_fit=True)
    assert options is not None
    assert options["num_ctx"] == ai_module._CONTEXT_FIT_FLOOR_TOKENS


def test_condense_call_options_force_fit_overrides_a_larger_gm_configured_ctx():
    ai_module.set_ollama_generation_overrides({"num_ctx": 32768})
    try:
        options = ai_module.condense_call_options("short recap", force_fit=True)
        assert options["num_ctx"] < 32768
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_condense_call_options_widens_further_for_thinking_plus_max_tokens():
    """A thinking model's hidden reasoning shares condense_recap's
    num_predict budget with the visible answer (see condense_recap's own
    docstring) — num_ctx needs matching extra headroom, or reasoning plus
    an uncapped answer could still overflow the window."""
    long_transcript = "word " * 20000  # long enough that both sides widen
    thinking_off = ai_module.condense_call_options(long_transcript, max_tokens=500, think=False)
    thinking_on = ai_module.condense_call_options(long_transcript, max_tokens=500, think=True)
    assert thinking_off is not None and thinking_on is not None
    assert thinking_on["num_ctx"] > thinking_off["num_ctx"]


def test_condense_call_options_no_thinking_headroom_without_max_tokens():
    """Nothing extra to make room for when neither max_tokens NOR a
    configured num_predict is set — thinking or not, a short recap still
    returns None."""
    assert ai_module.condense_call_options("short recap", think=True) is None


def test_condense_call_options_expanded_always_returns_a_value_even_for_a_short_recap():
    """Like force_fit — the expanded rung only ever runs because something
    already starved, so it always returns computed room even when the
    computed requirement wouldn't otherwise exceed the baseline."""
    options = ai_module.condense_call_options("short recap", expanded=True)
    assert options is not None


def test_condense_call_options_expanded_reserves_more_than_normal_thinking_headroom():
    long_transcript = "word " * 20000
    normal = ai_module.condense_call_options(long_transcript, think=True, max_tokens=500)
    expanded = ai_module.condense_call_options(long_transcript, think=True, max_tokens=500, expanded=True)
    assert expanded["num_ctx"] > normal["num_ctx"]


def test_condense_call_options_expanded_bypasses_the_think_max_tokens_gate():
    """Unlike the normal path (test_condense_call_options_no_thinking_
    headroom_without_max_tokens), expanded=True reserves headroom
    unconditionally — no max_tokens/configured num_predict needed."""
    assert ai_module.condense_call_options("short recap", think=True, expanded=True) is not None


def test_condense_call_options_widens_for_thinking_plus_configured_num_predict():
    """A GM-configured instance-wide num_predict (not just an explicit
    max_tokens argument) now also gets widened by condense_recap when
    think=True (see test_condense_recap_widens_a_configured_num_predict_
    when_thinking) — condense_call_options' num_ctx headroom must widen to
    match, or that wider generation could overflow the context window."""
    long_transcript = "word " * 20000
    ai_module.set_ollama_generation_overrides({"num_predict": 512})
    try:
        without_thinking = ai_module.condense_call_options(long_transcript, think=False)
        with_thinking = ai_module.condense_call_options(long_transcript, think=True)
        assert with_thinking["num_ctx"] > without_thinking["num_ctx"]
    finally:
        ai_module.set_ollama_generation_overrides({})


# ── RAG world_context wiring (condense_recap/summarize_transcript) ─────────

def test_with_world_context_prepends_labeled_block_ahead_of_system():
    result = ai_module._with_world_context("SYSTEM PROMPT", "- [npc] Gareth: a blacksmith")
    assert result.index("Gareth") < result.index("SYSTEM PROMPT")
    assert "for accuracy only" in result


def test_with_world_context_passthrough_when_blank():
    assert ai_module._with_world_context("SYSTEM PROMPT", "") == "SYSTEM PROMPT"


def test_with_world_context_instructs_against_inventing_a_translation():
    """Real report: without an explicit call-out, the model translated a
    Russian character name into a plausible but wrong English rendering
    instead of the one actually established in the World's entities — a
    bare "for accuracy" framing wasn't enough to stop it. The instruction
    must explicitly tell the model to use the reference list's name even
    when the input spells/transliterates/translates it differently."""
    result = ai_module._with_world_context("SYSTEM PROMPT", "- [character] Crimson Doll: a masked performer")
    assert "translat" in result.lower()
    assert "transliterat" in result.lower()
    assert "Crimson Doll" in result


@pytest.mark.asyncio
async def test_condense_recap_world_context_reaches_the_system_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", world_context="- [npc] Gareth: a blacksmith")
    system = calls[0]["messages"][0]["content"]
    assert "Gareth" in system


@pytest.mark.asyncio
async def test_condense_recap_no_world_context_block_when_blank(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap")
    system = calls[0]["messages"][0]["content"]
    assert "Relevant world lore" not in system


@pytest.mark.asyncio
async def test_summarize_transcript_world_context_reaches_unchunked_system(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.summarize_transcript("a short transcript", world_context="- [place] The Rusty Anchor: a tavern")
    system = calls[0]["messages"][0]["content"]
    assert "Rusty Anchor" in system


@pytest.mark.asyncio
async def test_summarize_transcript_world_context_reaches_chunked_part_system(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda transcript, system, think=True: 50)
    long_transcript = "word " * 500
    await ai_module.summarize_transcript(long_transcript, world_context="- [place] The Rusty Anchor: a tavern")
    assert len(calls) > 1  # actually chunked, given the tiny forced budget
    for call in calls:
        assert "Rusty Anchor" in call["messages"][0]["content"]


class _FakeRespFull:
    """Same shape as _FakeResp but also carries done_reason/eval_count and
    message.thinking, like a real ollama.ChatResponse — needed to exercise
    generate_chat's empty-content diagnostic message (see app.ai.generate_chat)."""

    def __init__(self, content, done_reason=None, eval_count=None, thinking=None):
        self.message = types.SimpleNamespace(content=content, thinking=thinking)
        self.done_reason = done_reason
        self.eval_count = eval_count


class _FakeFixedRespClient:
    def __init__(self, resp):
        self._resp = resp

    async def chat(self, **kwargs):
        return self._resp


@pytest.mark.asyncio
async def test_generate_chat_empty_content_reports_done_reason(monkeypatch):
    resp = _FakeRespFull("", done_reason="length", eval_count=512)
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "done_reason=length" in result


@pytest.mark.asyncio
async def test_generate_chat_empty_content_without_done_reason(monkeypatch):
    resp = _FakeRespFull("", done_reason=None, eval_count=None)
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "no done_reason reported" in result


@pytest.mark.asyncio
async def test_generate_chat_empty_content_with_thinking_reports_it_instead(monkeypatch):
    """A model that doesn't honor think=False can still burn its whole output
    budget on hidden reasoning — content ends up empty, but message.thinking
    (only present on a real ollama>=0.5 client) has text. That case should be
    reported specifically rather than falling through to the generic
    done_reason message."""
    resp = _FakeRespFull("", done_reason="length", eval_count=512, thinking="pondering deeply...")
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeFixedRespClient(resp))
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}])
    assert "empty response" in result
    assert "hidden" in result and "thinking" in result
    assert "done_reason=length" not in result


@pytest.mark.asyncio
async def test_stream_chat_passes_options(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"top_p": 0.9})
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}])]
    assert tokens == ["hi"]
    assert calls[0]["options"] == {"top_p": 0.9}
    assert "keep_alive" not in calls[0]
    assert calls[0]["think"] is False  # default — see stream_chat's own docstring


@pytest.mark.asyncio
async def test_stream_chat_think_true_reaches_the_client(monkeypatch):
    """The entity detail page's Ask AI panel is the one caller that ever
    passes think=True (via its Thinking checkbox) — confirm it actually
    reaches the ollama client call, not just gets accepted and dropped."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}], think=True)]
    assert tokens == ["hi"]
    assert calls[0]["think"] is True


# ── _chat_kwargs: downgrade think=True for a non-thinking-capable model ────
#
# Regression coverage for "Ollama 400: ... does not support thinking" —
# reported live against a model pulled straight from Hugging Face (this
# app's own hf.co search/upload feature), which doesn't reliably carry the
# "thinking" capability tag Ollama's /api/chat requires before it'll accept
# think=True at all. See _model_supports_thinking's own docstring.

@pytest.mark.asyncio
async def test_chat_kwargs_downgrades_think_true_for_non_thinking_model(monkeypatch):
    calls = []
    fake = _FakeChatClient(calls, show_capabilities=[])  # no "thinking" tag
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    tokens = [tok async for tok in ai_module.stream_chat(
        [{"role": "user", "content": "hi"}], think=True, model="hf.co/unsloth/gemma-4-26B-A4B-it-GGUF",
    )]
    assert tokens == ["hi"]
    assert calls[0]["think"] is False


@pytest.mark.asyncio
async def test_chat_kwargs_keeps_think_true_for_thinking_capable_model(monkeypatch):
    calls = []
    fake = _FakeChatClient(calls, show_capabilities=["completion", "thinking"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    kwargs = await ai_module._chat_kwargs(think=True, model="deepseek-r1")
    assert kwargs["think"] is True


@pytest.mark.asyncio
async def test_model_supports_thinking_result_is_cached(monkeypatch):
    fake = _FakeChatClient([], show_capabilities=["thinking"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    assert await ai_module._model_supports_thinking("some-model") is True
    assert await ai_module._model_supports_thinking("some-model") is True
    assert fake.show_calls == 1  # second call hit the cache, not .show() again


@pytest.mark.asyncio
async def test_model_supports_thinking_fails_closed_on_show_error(monkeypatch):
    class _BrokenShowClient:
        async def show(self, model):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(ai_module, "_client", lambda: _BrokenShowClient())
    assert await ai_module._model_supports_thinking("unreachable-model") is False


# ── KNOWN_MODELS thinking fallback ───────────────────────────────────────
#
# A model pulled as a raw GGUF via hf.co/{user}/{repo}:{filename} never
# gets Ollama's own "thinking" capability tag in /api/show the way an
# official ollama.com library model's Modelfile does — including the
# Unsloth IQ4_NL quantisation registered below — so relying on /api/show
# alone silently downgrades think=True to False for a model that's fully
# capable of it. _known_model_thinks/KNOWN_MODELS' own "thinking": True
# flag is the fallback for exactly that case.

_KNOWN_THINKING_GGUF_ID = "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:gemma-4-26B-A4B-it-UD-IQ4_NL.gguf"


def test_gemma_iq4nl_gguf_is_registered_as_a_known_thinking_model():
    assert ai_module._known_model_thinks(_KNOWN_THINKING_GGUF_ID) is True


def test_known_model_thinks_false_for_unlisted_model():
    assert ai_module._known_model_thinks("some-random-model:latest") is False


@pytest.mark.asyncio
async def test_model_supports_thinking_falls_back_to_known_models_when_show_lacks_tag(monkeypatch):
    """Ollama's own /api/show succeeds but doesn't report "thinking" for
    this GGUF (the normal case) — KNOWN_MODELS' flag should still win."""
    fake = _FakeChatClient([], show_capabilities=["completion"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    assert await ai_module._model_supports_thinking(_KNOWN_THINKING_GGUF_ID) is True


@pytest.mark.asyncio
async def test_model_supports_thinking_falls_back_to_known_models_when_show_fails(monkeypatch):
    class _BrokenShowClient:
        async def show(self, model):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(ai_module, "_client", lambda: _BrokenShowClient())
    assert await ai_module._model_supports_thinking(_KNOWN_THINKING_GGUF_ID) is True


@pytest.mark.asyncio
async def test_chat_kwargs_keeps_think_true_for_the_registered_gemma_gguf(monkeypatch):
    """End-to-end regression test for the reported bug: chatting with the
    Unsloth IQ4_NL GGUF and Thinking enabled must not get silently
    downgraded, even though Ollama itself never tags this model."""
    calls = []
    fake = _FakeChatClient(calls, show_capabilities=[])  # Ollama doesn't tag it
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    tokens = [tok async for tok in ai_module.stream_chat(
        [{"role": "user", "content": "hi"}], think=True, model=_KNOWN_THINKING_GGUF_ID,
    )]
    assert tokens == ["hi"]
    assert calls[0]["think"] is True


@pytest.mark.asyncio
async def test_chat_kwargs_never_calls_show_when_think_is_false(monkeypatch):
    """The common case (think=False, the vast majority of calls) must not
    pay for an extra /api/show round trip at all."""
    fake = _FakeChatClient([], show_capabilities=[])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    await ai_module._chat_kwargs(think=False, model="whatever")
    assert fake.show_calls == 0


# ── stream_chat: empty-stream diagnostic ────────────────────────────────────
#
# No surface using stream_chat exposes a Thinking toggle (see
# is_thinking_starved_sentinel's own docstring for the full survey — every
# caller runs think=False via _chat_kwargs' own default), but a model that
# ignores that and burns its whole budget on hidden reasoning used to come
# back as a completely silent reply on this path — nothing for the caller
# to show at all, unlike generate_chat's own explanatory sentinel.

class _FakeStreamChunk:
    def __init__(self, content="", thinking=None, done_reason=None):
        self.message = types.SimpleNamespace(content=content, thinking=thinking)
        self.done_reason = done_reason


class _FakeStreamClient:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chat(self, **kwargs):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


@pytest.mark.asyncio
async def test_stream_chat_empty_stream_with_thinking_yields_the_thinking_sentinel(monkeypatch):
    chunks = [
        _FakeStreamChunk(content="", thinking="pondering "),
        _FakeStreamChunk(content="", thinking="deeply...", done_reason="length"),
    ]
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeStreamClient(chunks))
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}])]
    assert len(tokens) == 1
    assert "hidden" in tokens[0] and "thinking" in tokens[0]
    assert str(len("pondering deeply...")) in tokens[0]  # accumulated across chunks
    assert "done_reason=length" not in tokens[0]  # same convention generate_chat's own sentinel uses


@pytest.mark.asyncio
async def test_stream_chat_empty_stream_without_thinking_yields_the_done_reason_sentinel(monkeypatch):
    chunks = [_FakeStreamChunk(content="", done_reason="length")]
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeStreamClient(chunks))
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}])]
    assert len(tokens) == 1
    assert "empty response" in tokens[0]
    assert "done_reason=length" in tokens[0]


@pytest.mark.asyncio
async def test_stream_chat_normal_stream_yields_no_sentinel(monkeypatch):
    """A stream that DOES produce content must behave exactly as before —
    no trailing empty-response sentinel appended just because the stream
    also carried some thinking text alongside real tokens."""
    chunks = [
        _FakeStreamChunk(content="Hello", thinking="a little reasoning"),
        _FakeStreamChunk(content=" there", done_reason="stop"),
    ]
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeStreamClient(chunks))
    tokens = [tok async for tok in ai_module.stream_chat([{"role": "user", "content": "hi"}])]
    assert tokens == ["Hello", " there"]


@pytest.mark.asyncio
async def test_parse_facts_from_recap_passes_options_and_keep_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"seed": 42}, "1h")
    facts = await ai_module.parse_facts_from_recap("some recap text")
    assert facts == []
    # The GM's options ride along, PLUS the chunk-window pin: every parse
    # call now carries an explicit num_ctx = reserves + the chunk's
    # estimated input (floored at _FACTS_PARSE_MIN_WINDOW_TOKENS), so chunk
    # + reserves + JSON response always fit the ENFORCED window. Computed
    # here with the module's own helpers so the assertion is against the
    # real numbers, not the test's guess — this tiny paste is one chunk
    # whose reserves+estimate land under the 2048 floor.
    reserve = ai_module._chunk_reserve_tokens(
        ai_module._RECAP_SYSTEM, False, ai_module._FACTS_PARSE_RESPONSE_RESERVE_TOKENS)
    chunk_tokens = -(-len("some recap text") // ai_module._chars_per_token_estimate("some recap text"))
    expected_ctx = min(ai_module.MAX_AUTO_NUM_CTX, max(ai_module._FACTS_PARSE_MIN_WINDOW_TOKENS, reserve + chunk_tokens))
    assert calls[0]["options"] == {"num_ctx": expected_ctx, "seed": 42}
    assert calls[0]["keep_alive"] == "1h"
    # format= (the JSON-schema constraint) must still be sent alongside.
    assert calls[0]["format"]


@pytest.mark.asyncio
async def test_facts_chunk_window_pinned_to_fit_reserves(monkeypatch):
    """The pin rule (see _facts_parse_chunk_plan): every chunk call's
    num_ctx is exactly reserve_tokens + THAT chunk's estimated input
    tokens (floored at _FACTS_PARSE_MIN_WINDOW_TOKENS, ceiled at
    MAX_AUTO_NUM_CTX). Constructed here with a stubbed chunk plan
    (400-char chunks, 5000 tokens of reserves) so the expected pin is
    checkable per call against the chunk text each call actually carried.
    The GM's configured num_ctx deliberately appears nowhere in the math:
    it used to CAP this parse (the exact bug that floored think=True chunks
    to 500 input tokens and split an ~11k-token recap into 26 parts), while
    the per-call pin overrides the configured value anyway."""
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (400, 5000))
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides({"num_ctx": 8192})
    text = "The party met Elyra at the tavern. " * 30  # ~1050 chars → several 400-char chunks
    await ai_module.parse_facts_from_recap(text)
    assert len(calls) >= 2  # actually chunked under the tiny forced plan
    for k in calls:
        user = [m for m in k["messages"] if m["role"] == "user"][0]["content"]
        est = -(-len(user) // ai_module._chars_per_token_estimate(user))
        assert k["options"]["num_ctx"] == max(ai_module._FACTS_PARSE_MIN_WINDOW_TOKENS, 5000 + est)


@pytest.mark.asyncio
async def test_facts_chunk_window_pin_floor_and_ceiling(monkeypatch):
    """The pin's two clamps: a tiny one-chunk parse whose reserves + input
    estimate sit under the floor still pins 2048 (never a window too small
    for its own reserves), and reserves so large the pin would exceed
    MAX_AUTO_NUM_CTX clamp to the ceiling instead — Ollama then truncates
    rather than 400ing, the accepted degradation for that pathological
    reserve size (see _facts_parse_chunk_plan)."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (10_000_000, 100))
    await ai_module.parse_facts_from_recap("met Elyra")  # one tiny chunk
    assert len(calls) == 1
    assert calls[0]["options"]["num_ctx"] == ai_module._FACTS_PARSE_MIN_WINDOW_TOKENS

    calls.clear()
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (200, ai_module.MAX_AUTO_NUM_CTX - 10))
    await ai_module.parse_facts_from_recap("The party met Elyra at the tavern. " * 30)
    assert len(calls) >= 2
    assert all(k["options"]["num_ctx"] == ai_module.MAX_AUTO_NUM_CTX for k in calls)


@pytest.mark.asyncio
async def test_parse_facts_default_budget_splits_an_11k_token_paste_into_several_chunks(monkeypatch):
    """The reported production failure, replayed against the real budget
    math: an ~11k-token recap pasted with Thinking on and ~700 tokens of
    RAG lore. Under the old window-first sizing the thinking reserve alone
    ate the whole assumed 4096 window, every chunk floored to 500 input
    tokens, and the paste split into 26 parts (40+ minutes of calls while
    the job was healthily progressing). Under the input-target sizing it
    must land in a HANDFUL of chunks, and every call's pinned window must
    cover its reserves plus that chunk's estimated input without ever
    exceeding MAX_AUTO_NUM_CTX — the window grows with the reserves instead
    of the chunks shrinking under them."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    text = "The party traveled north and fought the bandits at the old bridge. " * 680  # ~46k chars ≈ 11k tokens
    world_context = "- [npc] Elyra: " + "an enchanter of some renown. " * 93  # ~2.7k chars ≈ ~700 tokens of RAG lore
    facts = await ai_module.parse_facts_from_recap(text, think=True, world_context=world_context)
    assert facts == []
    assert 3 <= len(calls) <= 6  # ~4 parts at the 3072-token think=True input target — not the old 26
    # The reserve the parse just pinned against, replayed so the per-call
    # bound is asserted against the real numbers, not the test's own guess.
    reserve = ai_module._chunk_reserve_tokens(
        ai_module._RECAP_SYSTEM, True,
        len(world_context) // ai_module._chars_per_token_estimate(world_context)
        + ai_module._FACTS_PARSE_RESPONSE_RESERVE_TOKENS,
    )
    framing_len = len(ai_module._with_world_context("", world_context))
    for k in calls:
        user = [m for m in k["messages"] if m["role"] == "user"][0]["content"]
        chunk = user[framing_len:]  # strip the lore wrapper — the rest is this call's chunk
        est = -(-len(chunk) // ai_module._chars_per_token_estimate(chunk))
        assert k["options"]["num_ctx"] >= reserve + est  # the pin covers reserves + this chunk
        assert k["options"]["num_ctx"] <= ai_module.MAX_AUTO_NUM_CTX  # never past the ceiling
    # And the pinned window genuinely grew past the old assumed-window cap —
    # that growth IS the fix (with think+RAG the reserves alone exceed what
    # the old model could ever budget chunks into).
    assert calls[0]["options"]["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


@pytest.mark.asyncio
async def test_parse_facts_one_failed_chunk_does_not_fail_the_parse(monkeypatch):
    """A model/connection failure on ONE chunk must not throw away the
    facts every other chunk already extracted (a 6-part parse dying on part
    1 used to be the whole job) — the failed chunk is skipped and the rest
    merge normally."""
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (200, 5000))
    facts_json = '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}'

    class _FailFirstChunkClient:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("connection reset")
            return _FakeResp(facts_json)

        async def show(self, model):
            return types.SimpleNamespace(capabilities=["thinking"])

    fake = _FailFirstChunkClient()
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    facts = await ai_module.parse_facts_from_recap("The party met Elyra at the tavern. " * 6)
    assert len(fake.calls) == 2  # chunk 1 failed, chunk 2 was still asked
    assert facts == [{"content": "The party met Elyra at the tavern.", "visible_to_players": True}]


@pytest.mark.asyncio
async def test_parse_facts_every_chunk_failing_still_raises_valueerror(monkeypatch):
    """The tolerance above has a floor: when EVERY chunk failed there are no
    facts to salvage, so the parse raises the same ValueError contract the
    single-call version always had (the job runner maps it to job.error,
    the sync route to HTTP 502) instead of silently returning []."""
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (200, 5000))

    class _AlwaysFailsClient:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("connection reset")

        async def show(self, model):
            return types.SimpleNamespace(capabilities=[])

    fake = _AlwaysFailsClient()
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    with pytest.raises(ValueError, match="AI unavailable"):
        await ai_module.parse_facts_from_recap("The party met Elyra at the tavern. " * 6)
    assert len(fake.calls) >= 2  # every chunk really was attempted


# ── Settings > System save/validation round-trip ────────────────────────────

def test_generation_settings_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.5",
    })
    assert r.status_code == 403


def test_generation_settings_roundtrip(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.7",
        "ollama_top_p": "0.95",
        "ollama_top_k": "40",
        "ollama_repeat_penalty": "1.1",
        "ollama_num_predict": "512",
        "ollama_num_ctx": "8192",
        "ollama_seed": "1234",
        "ollama_mirostat": "2",
        "ollama_mirostat_tau": "5.0",
        "ollama_mirostat_eta": "0.1",
        "ollama_num_gpu": "20",
        "ollama_keep_alive": "10m",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_temperature == 0.7
        assert settings.ollama_top_p == 0.95
        assert settings.ollama_top_k == 40
        assert settings.ollama_repeat_penalty == 1.1
        assert settings.ollama_num_predict == 512
        assert settings.ollama_num_ctx == 8192
        assert settings.ollama_seed == 1234
        assert settings.ollama_mirostat == 2
        assert settings.ollama_mirostat_tau == 5.0
        assert settings.ollama_mirostat_eta == 0.1
        assert settings.ollama_num_gpu == 20
        assert settings.ollama_keep_alive == "10m"
    finally:
        db.close()

    # Pushed live into app.ai without a restart, same as ollama_url/model.
    # mirostat/mirostat_tau/mirostat_eta are saved (asserted above) but
    # deliberately NOT sent to Ollama — see AppSettings.ollama_mirostat's
    # docstring: current Ollama has no such Options fields at all.
    assert ai_module.effective_ollama_options() == {
        "temperature": 0.7, "top_p": 0.95, "top_k": 40, "repeat_penalty": 1.1,
        "num_predict": 512, "num_ctx": 8192, "seed": 1234, "num_gpu": 20,
    }
    assert ai_module.effective_ollama_keep_alive() == "10m"

    page = client.get("/settings?tab=system")
    assert 'value="0.7"' in page.text
    assert 'value="10m"' in page.text


def test_new_bucket_c_fields_roundtrip(client, seed):
    """The 10 fields added for expanded Ollama tuning — every one a real
    field of Ollama's current api.Options/api.Runner (verified against its
    api/types.go), so each is saved AND sent live, same as the original
    temperature/top_p/num_ctx fields."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_min_p": "0.05",
        "ollama_typical_p": "0.9",
        "ollama_repeat_last_n": "256",
        "ollama_presence_penalty": "0.5",
        "ollama_frequency_penalty": "0.3",
        "ollama_num_keep": "24",
        "ollama_num_batch": "512",
        "ollama_num_thread": "8",
        "ollama_main_gpu": "1",
        "ollama_use_mmap": "1",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_min_p == 0.05
        assert settings.ollama_typical_p == 0.9
        assert settings.ollama_repeat_last_n == 256
        assert settings.ollama_presence_penalty == 0.5
        assert settings.ollama_frequency_penalty == 0.3
        assert settings.ollama_num_keep == 24
        assert settings.ollama_num_batch == 512
        assert settings.ollama_num_thread == 8
        assert settings.ollama_main_gpu == 1
        assert settings.ollama_use_mmap == "1"
    finally:
        db.close()

    assert ai_module.effective_ollama_options() == {
        "min_p": 0.05, "typical_p": 0.9, "repeat_last_n": 256,
        "presence_penalty": 0.5, "frequency_penalty": 0.3, "num_keep": 24,
        "num_batch": 512, "num_thread": 8, "main_gpu": 1, "use_mmap": True,
    }


def test_mirostat_saved_but_not_sent_to_ollama(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_mirostat": "2", "ollama_mirostat_tau": "5.0", "ollama_mirostat_eta": "0.1",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_mirostat == 2
        assert settings.ollama_mirostat_tau == 5.0
        assert settings.ollama_mirostat_eta == 0.1
    finally:
        db.close()

    options = ai_module.effective_ollama_options()
    assert "mirostat" not in options
    assert "mirostat_tau" not in options
    assert "mirostat_eta" not in options


def test_use_mmap_tristate(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)

    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_use_mmap": "1",
    }, follow_redirects=False)
    assert ai_module.effective_ollama_options()["use_mmap"] is True

    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_use_mmap": "0",
    }, follow_redirects=False)
    assert ai_module.effective_ollama_options()["use_mmap"] is False

    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_use_mmap": "",
    }, follow_redirects=False)
    assert "use_mmap" not in ai_module.effective_ollama_options()


def test_gpu_preset_saves_and_reflects_on_page(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "v100_16gb",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_gpu_preset == "v100_16gb"
    finally:
        db.close()

    page = client.get("/settings?tab=system")
    assert 'value="v100_16gb" selected' in page.text


def test_gpu_preset_unknown_key_falls_back_to_none(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "not-a-real-preset",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_gpu_preset == ""
    finally:
        db.close()


def test_gpu_preset_blank_clears_it(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "v100_16gb",
    }, follow_redirects=False)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_gpu_preset == ""
    finally:
        db.close()


def test_main_gpu_zero_is_sent(client, seed):
    """Guards the is-not-None-vs-truthiness bug: main_gpu=0 is a real,
    meaningful value (the first GPU), not "unset" — it must survive the
    `if v is not None` filter in _refresh_settings_overrides, not a bare
    `if v` check that would drop it."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "", "ollama_main_gpu": "0",
    }, follow_redirects=False)
    assert ai_module.effective_ollama_options()["main_gpu"] == 0


def test_generation_settings_blank_means_unset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_temperature": "0.7", "ollama_num_ctx": "8192",
    }, follow_redirects=False)
    # Re-save with everything blank — should clear back to None/unset, not
    # silently keep the old values.
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.ollama_temperature is None
        assert settings.ollama_num_ctx is None
        assert settings.ollama_keep_alive == ""
    finally:
        db.close()
    assert ai_module.effective_ollama_options() == {}
    assert ai_module.effective_ollama_keep_alive() == ""


@pytest.mark.parametrize("field,value", [
    ("ollama_temperature", "not-a-number"),
    ("ollama_temperature", "5.0"),      # above the 0-2 bound
    ("ollama_top_p", "1.5"),            # above the 0-1 bound
    ("ollama_top_k", "-1"),             # below 0
    ("ollama_num_ctx", "0"),            # below 1
    ("ollama_mirostat", "3"),           # not in {0,1,2}
    ("ollama_min_p", "1.5"),            # above the 0-1 bound
    ("ollama_num_batch", "0"),          # below 1
    ("ollama_num_thread", "-1"),        # below 0
])
def test_generation_settings_out_of_range_rejected(client, seed, field, value):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        field: value,
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or getattr(settings, field) is None
    finally:
        db.close()


# ── Server-level ("Bucket A") Ollama tuning — POST /settings/system ────────

def test_server_env_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_srv_flash_attention": "1",
    })
    assert r.status_code == 403


def test_server_env_saves_to_db_and_writes_file(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_srv_flash_attention": "1",
        "ollama_srv_kv_cache_type": "q8_0",
        "ollama_srv_num_parallel": "2",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        saved = json.loads(settings.ollama_server_env_json)
        assert saved == {
            "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NUM_PARALLEL": "2",
        }
    finally:
        db.close()

    on_disk = tuning_module.read_server_env_file()
    assert on_disk == {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NUM_PARALLEL": "2"}


def test_server_env_blank_clears_a_previously_saved_key(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_srv_flash_attention": "1",
    }, follow_redirects=False)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_srv_flash_attention": "",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert json.loads(settings.ollama_server_env_json) == {}
    finally:
        db.close()
    assert tuning_module.read_server_env_file() == {}


def test_server_env_invalid_value_returns_400_and_does_not_write(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_srv_kv_cache_type": "not-a-real-type",
    })
    assert r.status_code == 400

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert not settings or json.loads(settings.ollama_server_env_json or "{}") == {}
    finally:
        db.close()
    assert tuning_module.read_server_env_file() is None


def test_server_env_unwritable_dir_saves_db_but_reports_a_warning(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    unwritable = tmp_path / "readonly"
    unwritable.mkdir()
    unwritable.chmod(0o555)
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", unwritable)

    login(client, seed.gm.email, GM_PASSWORD)
    try:
        r = client.post("/settings/system", data={
            "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
            "ollama_srv_flash_attention": "1",
        })
        if os.geteuid() == 0:
            pytest.skip("root can write through a chmod 0o555 directory")
        assert r.status_code == 200
        assert "couldn" in r.text.lower() or "write" in r.text.lower()

        db = SessionLocal()
        try:
            settings = db.query(AppSettings).first()
            assert json.loads(settings.ollama_server_env_json) == {"OLLAMA_FLASH_ATTENTION": "1"}
        finally:
            db.close()
    finally:
        unwritable.chmod(0o755)


# ── GET /api/ai/hardware ────────────────────────────────────────────────────

def test_hardware_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/ai/hardware")
    assert r.status_code == 403


def test_hardware_route_shape(client, seed, monkeypatch):
    import app.ollama_tuning as tuning_module

    async def fake_detect(vram_override_mb=None, gpu_preset=""):
        return {"cpu_model": "Test CPU", "cpu_cores": 8, "cpu_affinity": 8, "ram_total_mb": 16384,
                "ram_available_mb": 8192, "gpus": [], "vram_total_mb": None, "vram_source": "none",
                "vram_is_lower_bound": False, "notes": ["no gpu"]}
    # app.routers.ai's `_tuning` is the same module object as app.ollama_tuning
    # (imported via `from .. import ollama_tuning as _tuning`), so patching
    # the attribute here affects what the route resolves at call time.
    monkeypatch.setattr(tuning_module, "detect_hardware", fake_detect)

    async def fake_installed():
        return [{"model": "llama3.1:8b", "size_bytes": int(4.9e9), "parameter_size": "8.0B", "quantization_level": "Q4_K_M"}]
    monkeypatch.setattr(ai_module, "installed_models_detail", fake_installed)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/hardware")
    assert r.status_code == 200
    body = r.json()
    assert body["hardware"]["cpu_cores"] == 8
    assert len(body["models"]) == 1
    assert body["models"][0]["model"] == "llama3.1:8b"
    assert "recommendation" in body["models"][0]
    assert body["models"][0]["recommendation"]["fit"] in ("full_gpu", "partial_gpu", "cpu_only", "unknown")


def test_hardware_route_passes_saved_gpu_preset(client, seed, tmp_path, monkeypatch):
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    captured = {}

    async def fake_detect(vram_override_mb=None, gpu_preset=""):
        captured["gpu_preset"] = gpu_preset
        return {"cpu_model": "", "cpu_cores": None, "cpu_affinity": None, "ram_total_mb": None,
                "ram_available_mb": None, "gpus": [], "vram_total_mb": None, "vram_source": "none",
                "vram_is_lower_bound": False, "notes": []}
    monkeypatch.setattr(tuning_module, "detect_hardware", fake_detect)

    async def fake_installed():
        return []
    monkeypatch.setattr(ai_module, "installed_models_detail", fake_installed)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "v100_16gb",
    }, follow_redirects=False)

    r = client.get("/api/ai/hardware")
    assert r.status_code == 200
    assert captured["gpu_preset"] == "v100_16gb"


def test_hardware_route_preset_query_param_overrides_saved_value(client, seed, tmp_path, monkeypatch):
    """The `preset` query param lets the settings page live-preview a preset
    the GM has picked in the dropdown but not yet saved — it must win over
    whatever's actually persisted in AppSettings.ollama_gpu_preset."""
    import app.ollama_tuning as tuning_module
    monkeypatch.setattr(tuning_module, "OLLAMA_CONFIG_DIR", tmp_path)
    captured = {}

    async def fake_detect(vram_override_mb=None, gpu_preset=""):
        captured["gpu_preset"] = gpu_preset
        return {"cpu_model": "", "cpu_cores": None, "cpu_affinity": None, "ram_total_mb": None,
                "ram_available_mb": None, "gpus": [], "vram_total_mb": None, "vram_source": "none",
                "vram_is_lower_bound": False, "notes": []}
    monkeypatch.setattr(tuning_module, "detect_hardware", fake_detect)

    async def fake_installed():
        return []
    monkeypatch.setattr(ai_module, "installed_models_detail", fake_installed)

    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system", data={
        "ollama_model": "", "ollama_url": "", "swarmui_external_url": "",
        "ollama_gpu_preset": "",
    }, follow_redirects=False)

    r = client.get("/api/ai/hardware", params={"preset": "v100_16gb"})
    assert r.status_code == 200
    assert captured["gpu_preset"] == "v100_16gb"

    # Omitting the param falls back to the saved (still blank) value.
    r = client.get("/api/ai/hardware")
    assert r.status_code == 200
    assert captured["gpu_preset"] == ""


def test_hardware_route_survives_unreachable_ollama(client, seed, monkeypatch):
    async def fake_installed():
        return []
    monkeypatch.setattr(ai_module, "installed_models_detail", fake_installed)

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/ai/hardware")
    assert r.status_code == 200
    assert r.json()["models"] == []


def test_recommendation_keys_are_all_real():
    """The other half of tests/test_ollama_recommend.py's
    test_recommendation_server_keys_are_all_real — checked here instead of
    there since it needs routers.ai's own _OPTION_ALLOWLIST, and this file
    already owns that import. The recommendation engine must never suggest
    a per-request setting the Settings page can't actually accept."""
    from app.ollama_tuning import recommend_settings
    from app.routers.ai import _OPTION_ALLOWLIST

    scenarios = [
        {"vram_total_mb": 24000, "ram_total_mb": 65536, "cpu_cores": 16},   # full_gpu
        {"vram_total_mb": 8000, "ram_total_mb": 65536, "cpu_cores": 16},    # partial_gpu
        {"vram_total_mb": 0, "ram_total_mb": 16384, "cpu_cores": 8},        # cpu_only
        {"vram_total_mb": None, "ram_total_mb": 16384, "cpu_cores": 4},     # unknown
    ]
    for hardware in scenarios:
        rec = recommend_settings(model="qwen2.5:32b", hardware=hardware,
                                  parameter_size="32.8B", size_bytes=int(19e9))
        for key in rec["per_request"]:
            assert key in _OPTION_ALLOWLIST, f"{key} (fit={rec['fit']}) not in routers.ai._OPTION_ALLOWLIST"


# ── Per-model overrides ──────────────────────────────────────────────────────
# A GM-editable {model: {"options": {...}, "keep_alive": "..."}} map layered
# on top of the instance-wide fields above — see
# AppSettings.ollama_model_overrides_json's own docstring for the field
# scope (only the core Generation tuning panel, not Advanced sampling).

def test_effective_options_no_model_overrides_untouched():
    ai_module.set_ollama_generation_overrides({"temperature": 0.5}, "5m")
    assert ai_module.effective_ollama_options() == {"temperature": 0.5}
    assert ai_module.effective_ollama_options("some-other-model") == {"temperature": 0.5}
    assert ai_module.effective_ollama_keep_alive("some-other-model") == "5m"


def test_effective_options_per_model_layers_over_global():
    ai_module.set_ollama_generation_overrides(
        {"temperature": 0.5, "num_ctx": 4096}, "5m",
        model_overrides={"gemma4:26b": {"options": {"num_ctx": 16000}, "keep_alive": "1h"}},
    )
    # The unset field (temperature) still falls back to the global value;
    # num_ctx is the model's own override, winning over the global 4096.
    assert ai_module.effective_ollama_options("gemma4:26b") == {"temperature": 0.5, "num_ctx": 16000}
    assert ai_module.effective_ollama_keep_alive("gemma4:26b") == "1h"
    # A different model is untouched by gemma4:26b's own override.
    assert ai_module.effective_ollama_options("some-other-model") == {"temperature": 0.5, "num_ctx": 4096}
    assert ai_module.effective_ollama_keep_alive("some-other-model") == "5m"


def test_effective_options_per_model_keep_alive_falls_back_when_blank():
    ai_module.set_ollama_generation_overrides(
        {}, "5m", model_overrides={"gemma4:26b": {"options": {"temperature": 0.9}, "keep_alive": ""}},
    )
    # No per-model keep_alive set — falls back to the global one, not "".
    assert ai_module.effective_ollama_keep_alive("gemma4:26b") == "5m"


@pytest.mark.asyncio
async def test_chat_kwargs_passes_per_model_options_via_generate_chat(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    ai_module.set_ollama_generation_overrides(
        {"temperature": 0.5}, "5m",
        model_overrides={"gemma4:26b": {"options": {"temperature": 0.1}, "keep_alive": "30m"}},
    )
    await ai_module.generate_chat([{"role": "user", "content": "hi"}], model="gemma4:26b")
    assert calls[0]["options"] == {"temperature": 0.1}
    assert calls[0]["keep_alive"] == "30m"
    calls.clear()
    await ai_module.generate_chat([{"role": "user", "content": "hi"}], model="some-other-model")
    assert calls[0]["options"] == {"temperature": 0.5}
    assert calls[0]["keep_alive"] == "5m"


def test_model_override_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/settings/system/model-override", data={"model": "gemma4:26b", "temperature": "0.5"})
    assert r.status_code == 403


def test_model_override_save_and_apply_live(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system/model-override", data={
        "model": "gemma4:26b", "temperature": "0.2", "num_ctx": "16000", "keep_alive": "1h",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        overrides = json.loads(settings.ollama_model_overrides_json)
        assert overrides["gemma4:26b"] == {
            "options": {"temperature": 0.2, "num_ctx": 16000}, "keep_alive": "1h",
        }
    finally:
        db.close()

    # Pushed live without a restart, same as the global fields.
    assert ai_module.effective_ollama_options("gemma4:26b") == {"temperature": 0.2, "num_ctx": 16000}
    assert ai_module.effective_ollama_keep_alive("gemma4:26b") == "1h"
    # A model with no override is unaffected.
    assert ai_module.effective_ollama_options("other-model") == {}

    page = client.get("/settings?tab=system")
    assert "gemma4:26b" in page.text


def test_model_override_save_requires_a_model(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system/model-override", data={"model": "  ", "temperature": "0.5"})
    assert r.status_code == 400


def test_model_override_save_validates_range(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system/model-override", data={"model": "gemma4:26b", "temperature": "9"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert json.loads(settings.ollama_model_overrides_json or "{}") == {}
    finally:
        db.close()


def test_model_override_save_all_blank_removes_entry(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "gemma4:26b", "temperature": "0.2"})
    # Re-saving the same model with every field blank clears it back out —
    # no phantom empty entry left behind.
    client.post("/settings/system/model-override", data={"model": "gemma4:26b"})
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert json.loads(settings.ollama_model_overrides_json or "{}") == {}
    finally:
        db.close()
    assert ai_module.effective_ollama_options("gemma4:26b") == {}


def test_model_override_delete_route(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "gemma4:26b", "temperature": "0.2"})
    r = client.post("/settings/system/model-override/delete", data={"model": "gemma4:26b"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert json.loads(settings.ollama_model_overrides_json or "{}") == {}
    finally:
        db.close()
    assert ai_module.effective_ollama_options("gemma4:26b") == {}


def test_model_override_delete_route_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/settings/system/model-override/delete", data={"model": "gemma4:26b"})
    assert r.status_code == 403


def test_settings_page_ships_per_model_override_form(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "gemma4:26b", "temperature": "0.2"})
    page = client.get("/settings?tab=system").text
    assert 'action="/settings/system/model-override"' in page
    assert 'name="model"' in page
    assert 'action="/settings/system/model-override/delete"' in page
    assert "gemma4:26b" in page
    assert "temperature" in page


def test_settings_page_model_override_field_is_a_dropdown_not_free_text():
    """The "Model" field must be a <select> populated from installed
    models, not a free-text <input> a GM could typo against a model that
    isn't even downloaded — see populatePmoModelSelect in settings.html."""
    page = open("app/templates/settings.html").read()
    assert '<select id="pmo-model" name="model" required>' in page
    assert 'id="pmo-model" name="model"' in page
    # Populated from the same installed-models fetch the hardware panel
    # already makes — no free-text <input id="pmo-model" left over.
    assert '<input id="pmo-model"' not in page
    assert "function populatePmoModelSelect(models)" in page
    assert "populatePmoModelSelect(data.models || [])" in page


# ── Per-model override: "this model supports thinking" checkbox ────────────
#
# The general-purpose fix for "I need thinking to work for a model that
# isn't in KNOWN_MODELS" — a GM ticks a box for any model (one pulled via
# the Hugging Face search feature, one uploaded straight from their PC,
# anything) instead of needing a code change + redeploy for every new
# thinking-capable model that shows up. See app.ai._model_override_thinks.

def test_model_override_save_with_thinking_checkbox(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings/system/model-override", data={
        "model": "hf.co/some-org/some-model-GGUF:Q4_K_M.gguf", "thinking": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        overrides = json.loads(settings.ollama_model_overrides_json)
        assert overrides["hf.co/some-org/some-model-GGUF:Q4_K_M.gguf"] == {
            "options": {}, "keep_alive": "", "thinking": True,
        }
    finally:
        db.close()


def test_model_override_thinking_checkbox_alone_is_not_treated_as_blank(client, seed):
    """Ticking only the thinking box (no options, no keep_alive) must still
    create/keep the override entry — it isn't "nothing set"."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "some-model", "thinking": "1"})
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert json.loads(settings.ollama_model_overrides_json)["some-model"]["thinking"] is True
    finally:
        db.close()


def test_model_override_unticking_thinking_removes_it(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "some-model", "thinking": "1", "temperature": "0.5"})
    # Re-save with thinking left unticked (the box just isn't sent at all,
    # matching how an unchecked HTML checkbox submits) but temperature kept.
    client.post("/settings/system/model-override", data={"model": "some-model", "temperature": "0.5"})
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        entry = json.loads(settings.ollama_model_overrides_json)["some-model"]
        assert "thinking" not in entry
        assert entry["options"] == {"temperature": 0.5}
    finally:
        db.close()


def test_settings_page_ships_thinking_checkbox_in_model_override_form():
    page = open("app/templates/settings.html").read()
    assert 'id="pmo-thinking" name="thinking"' in page
    assert "This model supports thinking mode" in page


def test_settings_page_shows_thinking_indicator_for_flagged_models(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "gemma4:26b", "thinking": "1"})
    page = client.get("/settings?tab=system").text
    assert "thinking-capable" in page


def test_model_override_thinks_true_when_flagged():
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        assert ai_module._model_override_thinks("my-model") is True
        assert ai_module._model_override_thinks("other-model") is False
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_model_supports_thinking_falls_back_to_model_override_when_show_lacks_tag(monkeypatch):
    fake = _FakeChatClient([], show_capabilities=["completion"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-uploaded-model": {"thinking": True}})
    try:
        assert await ai_module._model_supports_thinking("my-uploaded-model") is True
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_model_supports_thinking_falls_back_to_model_override_when_show_fails(monkeypatch):
    class _BrokenShowClient:
        async def show(self, model):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(ai_module, "_client", lambda: _BrokenShowClient())
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-uploaded-model": {"thinking": True}})
    try:
        assert await ai_module._model_supports_thinking("my-uploaded-model") is True
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_chat_kwargs_keeps_think_true_for_a_gm_flagged_model(monkeypatch):
    """End-to-end: a GM-uploaded model with no KNOWN_MODELS entry at all,
    flagged thinking-capable purely via the Settings UI."""
    calls = []
    fake = _FakeChatClient(calls, show_capabilities=[])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-custom-upload": {"thinking": True}})
    try:
        tokens = [tok async for tok in ai_module.stream_chat(
            [{"role": "user", "content": "hi"}], think=True, model="my-custom-upload",
        )]
        assert tokens == ["hi"]
        assert calls[0]["think"] is True
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_set_ollama_generation_overrides_clears_capabilities_cache():
    """A GM ticking the thinking box must take effect on the very next
    request, not require a restart — same promise every other setting on
    this page makes."""
    ai_module._model_capabilities_cache["some-model"] = []  # cached as "not thinking"
    ai_module.set_ollama_generation_overrides({})
    assert "some-model" not in ai_module._model_capabilities_cache


# ── _model_thinking_failures: warn when a "supports thinking" model ────────
# ── actually fails at Ollama's own runtime check ────────────────────────────
#
# The override checkbox (or KNOWN_MODELS) is a GM's/this codebase's claim,
# not a guarantee — Ollama itself is still the final word once a real
# think=true request goes out. If the GM was wrong (or the model was
# re-pulled and Ollama's real behavior changed), the actual chat call gets
# the exact "<model> does not support thinking" 400 this whole feature
# exists to avoid — this tracks that so Settings > System can show a
# warning next to the model instead of it only ever showing up as a raw
# chat error the GM has to go dig up.

class _RejectsThinkingClient:
    """Raises Ollama's real "does not support thinking" ResponseError
    whenever think=true is actually sent — simulates a GM's override (or
    KNOWN_MODELS entry) being wrong about a model's real capability.
    Records every .chat() call's kwargs so tests can assert generate_chat/
    stream_chat's internal think=False retry actually happened (and that a
    poisoned capability cache skips straight to think=False on the NEXT
    call, without a second failing round-trip). `non_thinking_content` lets
    the parse_facts_from_recap tests answer non-thinking calls with JSON
    that survives the schema-driven parsing (the default "hi" is fine for
    the chat callers, which return content verbatim)."""

    def __init__(self, model_name="my-model", non_thinking_content="hi"):
        self._model_name = model_name
        self._non_thinking_content = non_thinking_content
        self.calls: list[dict] = []

    async def show(self, model):
        return types.SimpleNamespace(capabilities=[])  # Ollama itself never tagged it

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("think"):
            raise ollama.ResponseError(
                f'"{self._model_name}" does not support thinking', 400,
            )
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeResp("hi")
            return _gen()
        return _FakeResp(self._non_thinking_content)


@pytest.mark.asyncio
async def test_generate_chat_records_thinking_failure_on_rejection(monkeypatch):
    """The rejection is recovered from internally now (see Wave 1's
    think=False retry in generate_chat) — the caller gets real output, not
    the old sentinel string. The Settings warning badge still fires
    (_model_thinking_failures), and exactly two Ollama calls happen: the
    failing think=True probe, then a think=False retry. Because this test's
    GM override vouches for the model, that retry carries the <|think|>
    prompt token (ollama#16936 fallback) rather than dropping to plain
    instruct mode — see the dedicated section below."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        result = await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert result == "hi"
        assert "my-model" in ai_module._model_thinking_failures
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not fake.calls[1]["think"]
        # The vouched-model retry re-enables reasoning via the token — this
        # call passes no system=, so the helper inserts a fresh system
        # message at index 0 to carry it.
        assert fake.calls[1]["messages"][0]["role"] == "system"
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_stream_chat_records_thinking_failure_on_rejection(monkeypatch):
    """Same recovery as generate_chat, on the streaming path."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        tokens = [tok async for tok in ai_module.stream_chat(
            [{"role": "user", "content": "hi"}], think=True, model="my-model",
        )]
        assert tokens == ["hi"]
        assert "my-model" in ai_module._model_thinking_failures
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not fake.calls[1]["think"]
        # Same vouched-model <|think|> token retry as generate_chat above.
        assert fake.calls[1]["messages"][0]["role"] == "system"
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_generate_chat_skips_straight_to_think_false_after_poisoned(monkeypatch):
    """Once a model's capability cache is poisoned by a confirmed
    rejection, a LATER think=True request must not repeat the failing
    round-trip — _model_supports_thinking's cache short-circuits before
    ever consulting the (still-True) override, so only one clean
    think=False call happens (carrying the <|think|> token now that the
    fallback is armed for this vouched model — see the dedicated section
    below)."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert len(fake.calls) == 2  # the failing probe + the recovery retry

        result = await ai_module.generate_chat([{"role": "user", "content": "hi again"}], think=True, model="my-model")
        assert result == "hi"
        assert len(fake.calls) == 3  # no second 400 — went straight to think=False
        assert not fake.calls[2]["think"]
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_settings_save_re_arms_thinking_probe_after_rejection(monkeypatch):
    """A Settings save already clears _model_capabilities_cache (see
    set_ollama_generation_overrides) — confirm that un-poisons a
    previously-rejected model too, so a GM who fixes the model (or just
    wants to re-probe it) gets a fresh real think=true attempt."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert len(fake.calls) == 2

        ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
        await ai_module.generate_chat([{"role": "user", "content": "hi again"}], think=True, model="my-model")
        assert len(fake.calls) == 4  # a fresh think=True probe (fails again) + retry
        assert fake.calls[2]["think"] is True
        assert not fake.calls[3]["think"]
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_poisoned_model_not_wrongly_cleared_by_downgraded_call(monkeypatch):
    """Regression guard: once a model is in _model_thinking_failures, a
    caller that still passes think=True gets silently downgraded to
    think=False by _chat_kwargs (the poisoned cache) — that successful
    call must NOT clear the failure set, since think=true was never
    actually sent. Only a genuinely successful think=true call may clear
    it (see test_thinking_failure_cleared_by_a_later_successful_think_call
    below, which uses a real capability so the downgrade never happens)."""
    ai_module._model_thinking_failures.add("my-model")
    ai_module._model_capabilities_cache["my-model"] = []  # poisoned: no "thinking" tag
    fake = _FakeChatClient([], show_capabilities=[])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
    assert result == "hi"
    assert "my-model" in ai_module._model_thinking_failures


@pytest.mark.asyncio
async def test_thinking_failure_not_recorded_when_think_was_false(monkeypatch):
    """A model rejecting thinking is only meaningful if we actually asked
    for it — an unrelated error on a plain think=false call proves
    nothing about the model's thinking capability."""
    class _AlwaysFailsClient:
        async def chat(self, **kwargs):
            raise ollama.ResponseError("some unrelated error", 500)
    monkeypatch.setattr(ai_module, "_client", lambda: _AlwaysFailsClient())
    await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=False, model="my-model")
    assert "my-model" not in ai_module._model_thinking_failures


@pytest.mark.asyncio
async def test_thinking_failure_cleared_by_a_later_successful_think_call(monkeypatch):
    """Once a GM actually fixes the model (or it was transient), a real
    successful think=true call clears the earlier warning."""
    ai_module._model_thinking_failures.add("my-model")
    fake = _FakeChatClient([], show_capabilities=["thinking"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    result = await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
    assert result == "hi"
    assert "my-model" not in ai_module._model_thinking_failures


# ── <|think|> prompt-token fallback for vouched models (ollama#16936) ───────
#
# The rejection-recovery above falls back to plain think=False — silently
# dropping the reasoning a GM explicitly asked for. But when nd-world ITSELF
# vouches for the model's thinking (KNOWN_MODELS, or the per-model override
# checkbox those tests above use), the rejection is almost certainly not "the
# GM was wrong" but Ollama's missing capability tag on hf.co-imported GGUFs
# (ollama#16936: the import path never reports "thinking", so /api/chat 400s
# an explicit think=true). Gemma 4 still reasons perfectly when the template's
# own <|think|> token is supplied as literal system-message text, so for
# exactly those vouched models the retry (and every later think=True request,
# until a Settings save re-arms the real probe) sends think=False WITH the
# token prepended to the system message — reasoning still reaches the UI
# instead of being quietly lost. Unvouched models keep the plain think=False
# fallback: nobody claims they can think, so there's no token to send.


@pytest.mark.asyncio
async def test_generate_chat_think_rejection_uses_prompt_token_for_vouched_model(monkeypatch):
    """The override vouches, Ollama rejects the real think=true probe — the
    retry must re-enable reasoning via the <|think|> token, not silently
    drop to instruct mode. The advisory failure badge still fires, and a
    system= IS passed here so the token gets PREPENDED to it (the
    insert-a-fresh-system-message branch is covered by the helper unit test
    and the two extended rejection tests above, which pass no system=)."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        result = await ai_module.generate_chat(
            [{"role": "user", "content": "hi"}], system="You are a scribe.", think=True, model="my-model",
        )
        assert result == "hi"
        assert "my-model" in ai_module._model_thinking_failures  # still advisory-recorded
        assert "my-model" in ai_module._prompt_token_thinking_models  # fallback armed for next time
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not any("<|think|>" in (msg.get("content") or "") for msg in fake.calls[0]["messages"])
        assert not fake.calls[1]["think"]
        assert fake.calls[1]["messages"][0]["role"] == "system"
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>You are a scribe.")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_stream_chat_think_rejection_uses_prompt_token_for_vouched_model(monkeypatch):
    """Same prompt-token retry on the streaming path."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        tokens = [tok async for tok in ai_module.stream_chat(
            [{"role": "user", "content": "hi"}], system="You are a scribe.", think=True, model="my-model",
        )]
        assert tokens == ["hi"]
        assert "my-model" in ai_module._model_thinking_failures
        assert "my-model" in ai_module._prompt_token_thinking_models
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not any("<|think|>" in (msg.get("content") or "") for msg in fake.calls[0]["messages"])
        assert not fake.calls[1]["think"]
        assert fake.calls[1]["messages"][0]["role"] == "system"
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>You are a scribe.")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_poisoned_vouched_model_skips_straight_to_prompt_token(monkeypatch):
    """The fallback is sticky: once armed, a SECOND think=True request makes
    no repeated failing think=true round-trip — the poisoned capability
    cache downgrades the flag AND the pre-call injection adds the token, so
    exactly one (non-failing) call goes out."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert len(fake.calls) == 2  # the failing probe + the token retry

        result = await ai_module.generate_chat([{"role": "user", "content": "hi again"}], think=True, model="my-model")
        assert result == "hi"
        assert len(fake.calls) == 3  # no second 400 — one think=False call, token included
        assert not fake.calls[2]["think"]
        assert fake.calls[2]["messages"][0]["role"] == "system"
        assert fake.calls[2]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_unvouched_model_rejection_still_falls_back_to_plain_think_false(monkeypatch):
    """Nobody vouches for this model (no override, no KNOWN_MODELS entry) —
    the token fallback must NOT engage; the retry stays a plain think=False
    call. The capability cache is pre-seeded as thinking-capable (same
    direct-seeding convention as the poisoned-model test above) so the
    first call actually attempts the real think=true probe despite .show()
    reporting no tag."""
    fake = _RejectsThinkingClient("stray-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module._model_capabilities_cache["stray-model"] = ["thinking"]
    try:
        result = await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="stray-model")
        assert result == "hi"
        assert "stray-model" in ai_module._model_thinking_failures
        assert "stray-model" not in ai_module._prompt_token_thinking_models
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not fake.calls[1]["think"]
        assert not any("<|think|>" in (msg.get("content") or "") for msg in fake.calls[1]["messages"])
    finally:
        ai_module._model_capabilities_cache.clear()


@pytest.mark.asyncio
async def test_settings_save_re_arms_real_think_probe_after_prompt_token_fallback(monkeypatch):
    """The fallback is armed, but a Settings save clears it along with the
    capability cache — the next think=True request must attempt a REAL
    think=true again (which Ollama rejects here, exercising the full
    rejection → token-retry cycle a second time)."""
    fake = _RejectsThinkingClient("my-model")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert len(fake.calls) == 2
        assert "my-model" in ai_module._prompt_token_thinking_models

        ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
        assert "my-model" not in ai_module._prompt_token_thinking_models

        result = await ai_module.generate_chat([{"role": "user", "content": "hi again"}], think=True, model="my-model")
        assert result == "hi"
        assert len(fake.calls) == 4  # a fresh think=True probe (rejected again) + token retry
        assert fake.calls[2]["think"] is True
        assert not fake.calls[3]["think"]
        assert fake.calls[3]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_successful_think_true_retires_prompt_token_fallback(monkeypatch):
    """If Ollama later DOES accept think=true (an update tagged the model,
    or the GM re-registered it properly), the successful probe retires the
    token fallback — the API flag is the cleaner mechanism, so no token is
    injected on that call or any later one."""
    calls = []
    fake = _FakeChatClient(calls, show_capabilities=["thinking"])
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module._prompt_token_thinking_models.add("my-model")
    try:
        result = await ai_module.generate_chat([{"role": "user", "content": "hi"}], think=True, model="my-model")
        assert result == "hi"
        # think=true was actually sent (no downgrade → no injection either)
        assert calls[0]["think"] is True
        assert not any("<|think|>" in (msg.get("content") or "") for msg in calls[0]["messages"])
        assert "my-model" not in ai_module._prompt_token_thinking_models
    finally:
        ai_module._prompt_token_thinking_models.discard("my-model")


# parse_facts_from_recap gets the same two-layer recovery as the chat
# callers above: it used to turn the same "does not support thinking" 400
# into a hard ValueError, failing the Facts page's parse job outright —
# these tests mirror the generate_chat/stream_chat ones above, plus the
# JSON-schema wrinkle unique to this caller (the format= constraint and the
# response parsing both have to survive the retry).


@pytest.mark.asyncio
async def test_parse_facts_think_rejection_uses_prompt_token_for_vouched_model(monkeypatch):
    """The Facts page's Thinking checkbox hits the ollama#16936 rejection on
    a GM-vouched model — the parse must recover via the <|think|> token
    retry instead of raising ValueError, with the schema constraint still on
    the retry and the advisory failure badge still recorded. The fake's
    non-thinking answer is real facts JSON, since parse_facts_from_recap
    json.loads resp.message.content against _RECAP_FACTS_SCHEMA (it doesn't
    return content verbatim the way the chat callers do)."""
    facts_json = '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}'
    fake = _RejectsThinkingClient("my-model", non_thinking_content=facts_json)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        facts = await ai_module.parse_facts_from_recap("met Elyra at the tavern", model="my-model", think=True)
        assert facts == [{"content": "The party met Elyra at the tavern.", "visible_to_players": True}]
        assert "my-model" in ai_module._model_thinking_failures  # still advisory-recorded
        assert "my-model" in ai_module._prompt_token_thinking_models  # fallback armed for next time
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert fake.calls[0]["format"]  # the schema constraint rode the failing probe too
        assert not any("<|think|>" in (msg.get("content") or "") for msg in fake.calls[0]["messages"])
        assert not fake.calls[1]["think"]
        # The retry keeps the JSON constraint AND re-enables reasoning via
        # the token prepended to the _RECAP_SYSTEM message already at
        # messages[0].
        assert fake.calls[1]["format"]
        assert fake.calls[1]["messages"][0]["role"] == "system"
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_parse_facts_subsequent_calls_reuse_prompt_token_without_failing_round_trip(monkeypatch):
    """Same stickiness as generate_chat's poisoned-cache test above, on the
    parse path: once the token fallback is armed, a SECOND think=True parse
    makes exactly ONE new .chat() call — the poisoned capability cache
    downgrades the flag and the pre-call injection adds the token, so no
    failing think=true round-trip is repeated (every parse job paying a 400
    before working would double the model's latency for nothing)."""
    facts_json = '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}'
    fake = _RejectsThinkingClient("my-model", non_thinking_content=facts_json)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        await ai_module.parse_facts_from_recap("met Elyra at the tavern", model="my-model", think=True)
        assert len(fake.calls) == 2  # the failing probe + the token retry

        facts = await ai_module.parse_facts_from_recap("met Elyra again", model="my-model", think=True)
        assert facts == [{"content": "The party met Elyra at the tavern.", "visible_to_players": True}]
        assert len(fake.calls) == 3  # no second 400 — one think=False call, token included
        assert not fake.calls[2]["think"]
        assert fake.calls[2]["messages"][0]["role"] == "system"
        assert fake.calls[2]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})


@pytest.mark.asyncio
async def test_parse_facts_unvouched_rejection_falls_back_to_plain_think_false(monkeypatch):
    """Nobody vouches for this model (no override, no KNOWN_MODELS entry) —
    the parse still recovers from the rejection (it used to be a hard
    ValueError), but as a plain think=False call with NO token: nobody
    claims this model can think, so injecting <|think|> would just pollute
    the system prompt. Same direct cache-seeding convention as
    test_unvouched_model_rejection_still_falls_back_to_plain_think_false
    above, so the first call actually attempts the real think=true probe."""
    facts_json = '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}'
    fake = _RejectsThinkingClient("stray-model", non_thinking_content=facts_json)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module._model_capabilities_cache["stray-model"] = ["thinking"]
    try:
        facts = await ai_module.parse_facts_from_recap("met Elyra at the tavern", model="stray-model", think=True)
        assert facts == [{"content": "The party met Elyra at the tavern.", "visible_to_players": True}]
        assert "stray-model" in ai_module._model_thinking_failures
        assert "stray-model" not in ai_module._prompt_token_thinking_models
        assert len(fake.calls) == 2
        assert fake.calls[0]["think"] is True
        assert not fake.calls[1]["think"]
        assert not any("<|think|>" in (msg.get("content") or "") for msg in fake.calls[1]["messages"])
    finally:
        ai_module._model_capabilities_cache.clear()


@pytest.mark.asyncio
async def test_parse_facts_think_rejection_recovers_per_chunk(monkeypatch):
    """The <|think|> recovery is per CHUNK now, not per parse: chunk 1's
    failing think=true probe is retried with the token (the GM override
    vouches for the model), and chunk 2 onward go straight to think=False
    + token via the poisoned capability cache and the pre-call injection —
    no further failing 400 round-trips mid-parse. All chunks' facts still
    merge (deduplicated here, since the fake answers every non-thinking
    call with the same JSON)."""
    monkeypatch.setattr(ai_module, "_facts_parse_chunk_plan", lambda *a, **k: (200, 5000))
    facts_json = '{"facts": [{"content": "The party met Elyra at the tavern.", "visible_to_players": true}]}'
    fake = _RejectsThinkingClient("my-model", non_thinking_content=facts_json)
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"my-model": {"thinking": True}})
    try:
        text = "The party met Elyra at the tavern. " * 6  # two chunks under the forced budget
        facts = await ai_module.parse_facts_from_recap(text, model="my-model", think=True)
        assert facts == [{"content": "The party met Elyra at the tavern.", "visible_to_players": True}]
        # Chunk 1: the failing think=true probe, then the token retry.
        assert fake.calls[0]["think"] is True
        assert fake.calls[1]["think"] is False
        assert fake.calls[1]["format"]  # the schema constraint survived onto the retry
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>")
        # Chunk 2: downgraded pre-call, token injected WITHOUT another probe.
        assert len(fake.calls) == 3
        assert fake.calls[2]["think"] is False
        assert fake.calls[2]["messages"][0]["content"].startswith("<|think|>")
        assert fake.calls[2]["format"]
    finally:
        ai_module.set_ollama_generation_overrides({})


def test_messages_with_prompt_think_token_prepends_and_inserts():
    """The helper is pure: it returns a NEW list (and a new dict for the
    mutated system message) without touching the caller's originals —
    generate_chat/stream_chat reuse their `full` list in later branches
    (e.g. the rejection retry), so an in-place mutation would compound."""
    src = [{"role": "system", "content": "You are..."}, {"role": "user", "content": "hi"}]
    out = ai_module._messages_with_prompt_think_token(src)
    assert out[0]["content"] == "<|think|>You are..."
    assert out[0] is not src[0]
    assert src == [{"role": "system", "content": "You are..."}, {"role": "user", "content": "hi"}]

    src_no_system = [{"role": "user", "content": "hi"}]
    out_no_system = ai_module._messages_with_prompt_think_token(src_no_system)
    assert out_no_system[0] == {"role": "system", "content": "<|think|>"}
    assert out_no_system[1] == {"role": "user", "content": "hi"}
    assert src_no_system == [{"role": "user", "content": "hi"}]


def test_settings_page_shows_thinking_failure_warning(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "my-model", "thinking": "1"})
    ai_module._model_thinking_failures.add("my-model")
    try:
        page = client.get("/settings?tab=system").text
        assert "thinking failed last time" in page
    finally:
        ai_module._model_thinking_failures.discard("my-model")


def test_settings_page_hides_thinking_failure_warning_for_unaffected_models(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings/system/model-override", data={"model": "my-model", "thinking": "1"})
    page = client.get("/settings?tab=system").text
    assert "thinking failed last time" not in page
