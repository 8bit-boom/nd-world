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
    options=/keep_alive= were (or weren't) included."""

    def __init__(self, calls):
        self._calls = calls

    async def chat(self, **kwargs):
        self._calls.append(kwargs)
        if kwargs.get("stream"):
            async def _gen():
                yield _FakeResp("hi")
            return _gen()
        if kwargs.get("format"):
            return _FakeResp('{"facts": []}')
        return _FakeResp("hi")


@pytest.fixture(autouse=True)
def _reset_ollama_overrides():
    ai_module.set_ollama_generation_overrides({})
    yield
    ai_module.set_ollama_generation_overrides({})


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
    assert calls[0]["options"] == ai_module.context_sized_options("a long recap")


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
    guidance only, same contract min_tokens already has — no num_predict
    cap gets set at all."""
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap", max_tokens=150, think=True)
    assert "num_predict" not in (calls[0].get("options") or {})
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
    assert "options" not in calls[0]  # no options dict at all — min_tokens sets no Ollama param
    system = calls[0]["messages"][0]["content"]
    assert "80" in system


@pytest.mark.asyncio
async def test_condense_recap_no_length_notes_when_neither_bound_given(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_module, "_client", lambda: _FakeChatClient(calls))
    await ai_module.condense_recap("a recap")
    system = calls[0]["messages"][0]["content"]
    assert "Length target" not in system


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
    assert calls[0]["options"] == {"seed": 42}
    assert calls[0]["keep_alive"] == "1h"
    # format= (the JSON-schema constraint) must still be sent alongside.
    assert calls[0]["format"]


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

    async def fake_detect(vram_override_mb=None):
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
