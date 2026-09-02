"""Tests for durable background audio-processing jobs (AudioJob in
app/models.py, the engine in app/audio_jobs.py, and the routes it backs in
app/routers/sessions.py and app/routers/ai.py) — an opt-in alternative to
the blocking upload routes for a recording long enough that waiting on one
HTTP request isn't practical. The actual work runs as a fire-and-forget
asyncio task in the server process, independent of any one connection, so
it survives the browser tab that started it closing. These tests poll the
status route in a short retry loop rather than sleeping a fixed amount —
each poll request pumps the same event loop the background task runs on,
so the task actually gets a chance to progress between polls.
"""
import asyncio
import io
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import ai as ai_module
from app import audio_jobs
from app import job_shutdown as _job_shutdown
from app.database import SessionLocal
from app.models import AudioJob, Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

# Captured before the autouse _fake_ai fixture below ever runs, so tests that
# need the REAL map-reduce chunking logic (not _fake_ai's flat fake) can
# restore it for just that one test.
_REAL_SUMMARIZE_TRANSCRIPT = ai_module.summarize_transcript
# Same idea, for the think-rejection-recovery tests below — those need the
# REAL condense_recap -> generate_chat call chain (so app.ai's own internal
# think=False retry actually runs), not _fake_ai's flat fake.
_REAL_CONDENSE_RECAP = ai_module.condense_recap


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _poll_until_terminal(client, url, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        r = client.get(url)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"job never reached a terminal status, last seen: {data}")


@pytest.fixture(autouse=True)
def _fake_ai(monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        assert path.is_file(), f"audio should still exist while transcribing: {path}"
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "The party met Elena at the bazaar."

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        return "Condensed: " + recap[:20]

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)


# ── app/audio_jobs.py engine, exercised directly ────────────────────────────
#
# create_job() calls asyncio.create_task(), which requires an already-
# running event loop — true inside a real request (TestClient runs one per
# call, as the HTTP-level tests below rely on) but not in a plain sync test
# function. These engine-level tests use @pytest.mark.asyncio + await
# asyncio.sleep(...) to poll instead, giving the fire-and-forget task a
# loop to actually run on.

async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_create_job_runs_to_completion_session_recap(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "the party met elena at the bazaar"
    assert job.recap == "The party met Elena at the bazaar."
    assert not audio.exists()  # delete_after=True


@pytest.mark.asyncio
async def test_create_job_marks_error_when_summarize_returns_failure_sentinel(client, seed, tmp_path, monkeypatch):
    # summarize_transcript never raises on an Ollama-side failure — it
    # returns a "[AI ...]" sentinel string instead. Without checking for
    # that, a failed summarize would land as status="done" with the error
    # text sitting in the recap field, same gap as resummarize_job had.
    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI error: Ollama unreachable]"

    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == "[AI error: Ollama unreachable]"


@pytest.mark.asyncio
async def test_create_job_attachment_purpose_has_no_recap_and_keeps_file(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=False, attachment_url="/uploads/ai_attachments/clip.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "the party met elena at the bazaar"
    assert job.recap == ""  # attachment purpose never summarizes
    assert job.attachment_url == "/uploads/ai_attachments/clip.mp3"
    assert audio.exists()  # delete_after=False


# ── Auto-retry with think=False after a thinking-starved first attempt ─────
#
# A real production Session Recap job failed: the model burned its whole
# output budget on hidden thinking and returned generate_chat's own
# "[empty response ... hidden thinking output ...]" sentinel. Since a
# failed job has already produced nothing, retrying once with Thinking off
# automatically (see _run_job's own docstring) is a one-sided win — these
# tests exercise that retry loop directly.

_THINKING_STARVED_SENTINEL = (
    '[empty response from gemma4:26b — it produced 7781 character(s) of hidden '
    '"thinking" output but no final answer (usually means it ran out of output '
    'budget mid-reasoning). Try a shorter prompt, a higher response-length limit, '
    'or a non-reasoning model.]'
)


@pytest.mark.asyncio
async def test_session_recap_job_climbs_to_expanded_before_flipping_think(client, seed, tmp_path, monkeypatch):
    """The ladder's middle rung (same think, expanded budget — see
    app.audio_jobs._run_job's attempt_plans and
    docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1) must be tried BEFORE
    flipping think off — a model that just needed more room, not a
    different mode, should get the recap it actually asked for."""
    calls = []

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        if think and not expanded_thinking:
            return _THINKING_STARVED_SENTINEL
        return "A recap written with the expanded budget."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, think=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "A recap written with the expanded budget."
    assert calls == [(True, False), (True, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is True  # never flipped — the expanded rung alone was enough
        assert fresh.think_fallback is False
        assert fresh.expanded_thinking is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_session_recap_job_climbs_all_the_way_to_think_false_when_expanded_also_starves(client, seed, tmp_path, monkeypatch):
    calls = []

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        return _THINKING_STARVED_SENTINEL if think else "A recap written without thinking."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, think=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "A recap written without thinking."
    assert calls == [(True, False), (True, True), (False, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is False  # flipped so Retry-summary's checkbox reflects reality
        assert fresh.think_fallback is True
        assert fresh.expanded_thinking is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_session_recap_job_with_think_already_false_climbs_to_expanded_but_never_flips_think(client, seed, tmp_path, monkeypatch):
    """A job the GM explicitly ran with Thinking off still gets one expanded
    retry (a model can starve on think=False alone — an unset num_predict
    is then bounded only by num_ctx, or a model can ignore think=False
    outright), but its think value never flips against the GM's explicit
    choice, and think_fallback (which specifically means "we flipped think
    off for you") stays False."""
    calls = []

    async def always_starved(transcript, model="", extra_instructions="", think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        return _THINKING_STARVED_SENTINEL
    monkeypatch.setattr(ai_module, "summarize_transcript", always_starved)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, think=False,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == _THINKING_STARVED_SENTINEL
    assert calls == [(False, False), (False, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is False
        assert fresh.think_fallback is False
        assert fresh.expanded_thinking is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_session_recap_ladder_resumes_the_checkpoint_when_think_stays_the_same(client, seed, tmp_path, monkeypatch):
    """Rung 1 (think=True, normal) starves after checkpointing partial
    progress — rung 2 (think=True, expanded) must resume from exactly that
    checkpoint (chunk_chars is unaffected by expanded_thinking, so it's
    still valid) instead of redoing the parts already summarized. Rung 3
    (think=False) flips think, so it must NOT receive that checkpoint —
    see _run_job's own resume_for_attempt rule."""
    seen_resumes = []

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, expanded_thinking=False,
                              resume=None, on_checkpoint=None, **kwargs):
        seen_resumes.append((think, expanded_thinking, resume))
        if think and not expanded_thinking:
            on_checkpoint({"phase": "summarize", "parts_done": 1, "chunk_total": 2,
                           "chunk_chars": 50, "text": "part summary 0"})
            return _THINKING_STARVED_SENTINEL
        if think and expanded_thinking:
            return _THINKING_STARVED_SENTINEL
        return "A recap written without thinking."
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, think=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "A recap written without thinking."

    assert seen_resumes[0] == (True, False, None)  # rung 1: nothing to resume yet
    assert seen_resumes[1][0:2] == (True, True)
    assert seen_resumes[1][2] == {"phase": "summarize", "parts_done": 1, "chunk_total": 2,
                                   "chunk_chars": 50, "text": "part summary 0"}  # rung 2 resumes rung 1's checkpoint
    assert seen_resumes[2] == (False, True, None)  # rung 3 flips think — starts fresh


@pytest.mark.asyncio
async def test_session_recap_job_non_starved_failure_does_not_retry(client, seed, tmp_path, monkeypatch):
    """Only the specific thinking-starved sentinel triggers a retry — any
    other failure (a connection error, here) must fail on the first
    attempt, exactly like before this feature existed."""
    calls = []

    async def connection_error(transcript, model="", extra_instructions="", think=True, **kwargs):
        calls.append(think)
        return "[AI unavailable: ConnectionError: refused]"
    monkeypatch.setattr(ai_module, "summarize_transcript", connection_error)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, think=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == "[AI unavailable: ConnectionError: refused]"
    assert calls == [True]


@pytest.mark.asyncio
async def test_condense_job_climbs_to_expanded_before_flipping_think(client, seed, monkeypatch):
    calls = []

    async def fake_condense(recap, model="", options=None, think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        if think and not expanded_thinking:
            return _THINKING_STARVED_SENTINEL
        return "Condensed with the expanded budget."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="A long existing recap.", think=True)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "Condensed with the expanded budget."
    assert calls == [(True, False), (True, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is True
        assert fresh.think_fallback is False
        assert fresh.expanded_thinking is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_condense_job_climbs_all_the_way_to_think_false_when_expanded_also_starves(client, seed, monkeypatch):
    calls = []

    async def fake_condense(recap, model="", options=None, think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        return _THINKING_STARVED_SENTINEL if think else "Condensed without thinking."
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="A long existing recap.", think=True)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "Condensed without thinking."
    assert calls == [(True, False), (True, True), (False, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is False
        assert fresh.think_fallback is True
        assert fresh.expanded_thinking is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_condense_job_with_think_already_false_climbs_to_expanded_but_never_flips_think(client, seed, monkeypatch):
    calls = []

    async def always_starved(recap, model="", options=None, think=True, expanded_thinking=False, **kwargs):
        calls.append((think, expanded_thinking))
        return _THINKING_STARVED_SENTINEL
    monkeypatch.setattr(ai_module, "condense_recap", always_starved)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="A long existing recap.", think=False)
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == _THINKING_STARVED_SENTINEL
    assert calls == [(False, False), (False, True)]

    db = SessionLocal()
    try:
        db.expire_all()
        fresh = db.get(AudioJob, job_id)
        assert fresh.think is False
        assert fresh.think_fallback is False
        assert fresh.expanded_thinking is True
    finally:
        db.close()


def test_job_status_route_reports_thinking_starved_and_think_fallback(client, seed):
    """_job_to_dict's server-side detection (see is_thinking_starved_sentinel)
    is what powers the Background Jobs page's one-click "Retry without
    Thinking" button and the pre-unchecked Thinking checkbox — the client
    never has to duplicate sentinel-text knowledge."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="error", error=_THINKING_STARVED_SENTINEL, think=False, think_fallback=True,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["thinking_starved"] is True
    assert data["think_fallback"] is True
    assert data["think"] is False


def test_job_status_route_reports_expanded_thinking(client, seed):
    """expanded_thinking is independent of think_fallback — a job can climb
    to the expanded budget and succeed WITHOUT ever needing to flip think
    off (see test_session_recap_job_climbs_to_expanded_before_flipping_
    think)."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="done", recap="a recap", think=True, expanded_thinking=True,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["expanded_thinking"] is True
    assert data["think_fallback"] is False


def test_job_status_route_reports_think_rejected(client, seed):
    """Distinct from think_fallback (budget starvation) — see
    AudioJob.think_rejected's own docstring."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="condense", filename="clip.mp3",
            status="done", recap="a recap", think=False, think_rejected=True,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["think_rejected"] is True
    assert data["think_fallback"] is False
    assert data["think"] is False
    assert data["think_token_fallback"] is False


def test_job_status_route_reports_think_token_fallback(client, seed):
    """The vouched-model flavor of a rejection: reasoning ran via the
    <|think|> prompt token, so think stays true and the flag is exposed
    for the Background Jobs page's informational (not corrective) note —
    see AudioJob.think_token_fallback's own docstring."""
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="condense", filename="clip.mp3",
            status="done", recap="a recap", think=True, think_rejected=True, think_token_fallback=True,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["think_rejected"] is True
    assert data["think_token_fallback"] is True
    assert data["think"] is True


class _RejectsThinkingClient:
    """Ollama's real "does not support thinking" 400 whenever think=true is
    actually sent — same fake shape as tests/test_ollama_options.py's own
    (not imported from there to keep the two test files independent)."""

    def __init__(self, reply="A tidy recap."):
        self._reply = reply
        self.calls = []

    async def show(self, model):
        import types
        return types.SimpleNamespace(capabilities=[])

    async def chat(self, **kwargs):
        import ollama
        self.calls.append(kwargs)
        if kwargs.get("think"):
            raise ollama.ResponseError('"model-x" does not support thinking', 400)
        import types
        return types.SimpleNamespace(message=types.SimpleNamespace(content=self._reply))


@pytest.mark.asyncio
async def test_condense_job_recovers_from_thinking_rejection(client, seed, monkeypatch):
    """End-to-end: a real condense_recap -> generate_chat call chain (not
    _fake_ai's flat fake) against a fake Ollama client that rejects
    think=true — the job must land "done" with the real recap (app.ai's own
    internal recovery recovers it), not "error" with the raw Ollama 400,
    and be labeled think_rejected so the GM knows why. model-x here is
    VOUCHED (a GM override ticked its thinking checkbox), so under the
    <|think|> prompt-token fallback the retry still carried reasoning:
    think stays True and think_token_fallback is set alongside
    think_rejected. The ladder above never sees this at all —
    is_thinking_starved_sentinel doesn't match a rejection, so this
    exercises a path the ladder tests above don't cover."""
    monkeypatch.setattr(ai_module, "condense_recap", _REAL_CONDENSE_RECAP)
    fake = _RejectsThinkingClient()
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module.set_ollama_generation_overrides({}, model_overrides={"model-x": {"thinking": True}})
    try:
        job_id = audio_jobs.create_condense_job(
            world_id=seed.world_a.id, text="A long existing recap.", model="model-x", think=True,
        )
        job = await _await_terminal(job_id)
        assert job.status == "done", job.error
        assert job.recap == "A tidy recap."
        assert job.think_rejected is True
        assert job.think_token_fallback is True
        assert job.think is True
        assert job.think_fallback is False
        # The recovery's retry carried the <|think|> token, not a plain
        # think=False downgrade — the vouch (the override) routed it there.
        assert fake.calls[0]["think"] is True
        assert fake.calls[1]["think"] is False
        assert fake.calls[1]["messages"][0]["content"].startswith("<|think|>")
    finally:
        ai_module.set_ollama_generation_overrides({})
        ai_module._model_thinking_failures.discard("model-x")
        ai_module._prompt_token_thinking_models.discard("model-x")


@pytest.mark.asyncio
async def test_condense_job_unvouched_rejection_flips_think_off(client, seed, monkeypatch):
    """The OTHER flavor: nobody vouched for this model (no override, not in
    KNOWN_MODELS — the capability cache is pre-seeded so think=true still
    reaches Ollama and gets rejected). There's no prompt-token workaround
    for a model nd-world doesn't trust to handle it, so the old labeling
    applies verbatim: think flips to False, think_rejected set,
    think_token_fallback NOT set."""
    monkeypatch.setattr(ai_module, "condense_recap", _REAL_CONDENSE_RECAP)
    fake = _RejectsThinkingClient()
    monkeypatch.setattr(ai_module, "_client", lambda: fake)
    ai_module._model_capabilities_cache["stray-model"] = ["thinking"]
    try:
        job_id = audio_jobs.create_condense_job(
            world_id=seed.world_a.id, text="A long existing recap.", model="stray-model", think=True,
        )
        job = await _await_terminal(job_id)
        assert job.status == "done", job.error
        assert job.recap == "A tidy recap."
        assert job.think_rejected is True
        assert job.think_token_fallback is False
        assert job.think is False
        assert not any(str(m.get("content", "")).startswith("<|think|>") for m in fake.calls[1]["messages"])
    finally:
        ai_module._model_capabilities_cache.clear()
        ai_module._model_thinking_failures.discard("stray-model")
        ai_module._prompt_token_thinking_models.discard("stray-model")


@pytest.mark.asyncio
async def test_session_recap_job_recovers_from_thinking_rejection(client, seed, tmp_path, monkeypatch):
    """Same recovery, on the session_recap (chunked summarize_transcript)
    path rather than condense's single-call path — vouched model, so the
    <|think|> token flavor: think stays True, think_token_fallback set."""
    monkeypatch.setattr(ai_module, "summarize_transcript", _REAL_SUMMARIZE_TRANSCRIPT)
    fake = _RejectsThinkingClient("The party explored the ruins.")
    monkeypatch.setattr(ai_module, "_client", lambda: fake)

    async def fake_transcribe(*args, **kwargs):
        return "A short transcript."
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    ai_module.set_ollama_generation_overrides({}, model_overrides={"model-x": {"thinking": True}})
    try:
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio bytes")
        job_id = audio_jobs.create_job(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            audio_path=audio, delete_after=True, model="model-x", think=True,
        )
        job = await _await_terminal(job_id)
        assert job.status == "done", job.error
        assert job.recap == "The party explored the ruins."
        assert job.think_rejected is True
        assert job.think_token_fallback is True
        assert job.think is True
    finally:
        ai_module.set_ollama_generation_overrides({})
        ai_module._model_thinking_failures.discard("model-x")
        ai_module._prompt_token_thinking_models.discard("model-x")


def test_job_status_route_thinking_starved_false_for_other_failures(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
            status="error", error="[AI unavailable: ConnectionError: refused]",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["thinking_starved"] is False
    assert data["think_fallback"] is False


def test_background_jobs_page_wires_the_retry_without_thinking_button(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page_html = client.get("/background-jobs").text
    assert "🧠✕ Retry without Thinking" in page_html
    assert "job.status === 'error' && job.thinking_starved" in page_html
    assert "job.thinking_starved ? false : job.think !== false" in page_html
    # Reuses bgResummarize rather than a new endpoint — with its own
    # resetLabel so a failed retry doesn't relabel this button back to
    # "🔁 Retry summary".
    assert "resetLabel = '🔁 Retry summary'" in page_html
    assert "noThinkBtn, '🧠✕ Retry without Thinking'" in page_html


def test_background_jobs_page_wires_the_ladder_notes(client, seed):
    """job.expanded_thinking and job.think_fallback are independent flags
    (see docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 1 item 1.3) — a
    done audio job's card must be able to render either note, and both if
    both are set. The think_rejected note now comes in two flavors, split
    by think_token_fallback: the <|think|> prompt-token workaround's
    informational note (reasoning still ran) must not carry the plain
    rejection's "untick the override" guidance."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page_html = client.get("/background-jobs").text
    assert "job.expanded_thinking" in page_html
    assert "automatically retried with an expanded limit" in page_html
    assert "even with an expanded limit" in page_html
    assert "this recap was written with Thinking off" in page_html
    assert "job.think_rejected && job.think_token_fallback" in page_html
    assert "reasoning still ran, via the <|think|> prompt token" in page_html


# ── purpose="condense" (create_condense_job) ────────────────────────────────

@pytest.mark.asyncio
async def test_create_condense_job_runs_to_completion(client, seed):
    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="A long existing recap.")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "A long existing recap."  # the input, unchanged
    assert job.recap == "Condensed: A long existing reca"
    assert job.audio_path == ""


@pytest.mark.asyncio
async def test_create_condense_job_never_transcribes(client, seed, monkeypatch):
    """purpose="condense" has no audio at all — if _run_job's dispatch ever
    regressed to calling transcribe_audio for it, this would fail loudly
    instead of silently succeeding on the wrong path."""
    async def fail_if_called(*a, **kw):
        raise AssertionError("condense jobs must never transcribe")
    monkeypatch.setattr(ai_module, "transcribe_audio", fail_if_called)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="text to condense")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error


@pytest.mark.asyncio
async def test_create_condense_job_passes_model_and_think(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["model"] = model
        captured["think"] = think
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="some recap", model="llama3.1", think=False,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["model"] == "llama3.1"
    assert captured["think"] is False
    assert job.think is False
    assert job.model == "llama3.1"


@pytest.mark.asyncio
async def test_create_condense_job_fit_context_sizes_num_ctx(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    text = "word " * 2000
    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text=text, fit_context=True)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["options"] == ai_module.context_sized_options(text)
    assert job.fit_context is True


@pytest.mark.asyncio
async def test_create_condense_job_fit_context_off_by_default(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="short recap")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["options"] is None


@pytest.mark.asyncio
async def test_create_condense_job_auto_widens_ctx_for_a_long_input_without_fit_context(client, seed, monkeypatch):
    """Plain (non-fit-context) Condense on an input long enough to risk
    silently overflowing the assumed default context must still protect
    itself — see app.ai.condense_call_options' own docstring for why (a
    long unchunked call that overflows num_ctx can come back as garbage,
    e.g. reserved-vocabulary tokens, instead of a clean error)."""
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    long_text = "word " * 20000
    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text=long_text)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["options"] is not None
    assert captured["options"]["num_ctx"] > ai_module._DEFAULT_ASSUMED_CTX_TOKENS


# ── purpose="condense": strictness setting ──────────────────────────────────
# create_condense_job persists condense_strictness ("guideline"|"firm"|
# "strict"); in "strict" mode _run_job additionally estimates the finished
# recap's tokens with the same estimator the status labels use and re-runs
# condense_recap ONCE when the result lands outside the requested range.

@pytest.mark.asyncio
async def test_create_condense_job_persists_strictness_round_trip(client, seed):
    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="A long existing recap.", strictness="strict",
    )
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.condense_strictness == "strict"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_create_condense_job_defaults_strictness_to_guideline(client, seed):
    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="A long existing recap.")
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.condense_strictness == "guideline"
    finally:
        db.close()


def test_create_condense_job_rejects_invalid_strictness(seed):
    with pytest.raises(ValueError):
        audio_jobs.create_condense_job(world_id=seed.world_a.id, text="text", strictness="bogus")


@pytest.mark.asyncio
async def test_condense_job_strict_mode_retries_a_too_short_recap_once(client, seed, monkeypatch):
    """strict + a min_tokens target: the first (too short) draft must be
    re-run exactly once with a violation note in the extra instructions,
    and the in-band second draft wins. The drafts' sizes are chosen against
    the same chars-per-token estimator _run_job checks with (ASCII ~4
    chars/token): ~3 tokens first (far below 50), then exactly ~50."""
    calls = []

    async def fake_condense(recap, model="", options=None, think=True, extra_instructions="",
                            min_tokens=None, max_tokens=None, strictness="guideline", **kwargs):
        calls.append({
            "extra_instructions": extra_instructions, "min_tokens": min_tokens,
            "max_tokens": max_tokens, "strictness": strictness,
        })
        if len(calls) == 1:
            return "Too short."
        return "x" * 200
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="A long existing recap.", min_tokens=50, strictness="strict",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "x" * 200
    assert len(calls) == 2  # one strict violation → exactly one retry, no more
    assert "previous draft" in calls[1]["extra_instructions"]
    assert calls[1]["min_tokens"] == 50
    assert calls[1]["strictness"] == "strict"
    assert job.condense_strictness == "strict"


@pytest.mark.asyncio
async def test_condense_job_strict_mode_in_band_first_attempt_makes_one_call(client, seed, monkeypatch):
    """The strict check must only fire on an actual violation — a first
    draft already inside the requested range costs exactly one AI call."""
    calls = []

    async def fake_condense(recap, **kwargs):
        calls.append(kwargs)
        return "y" * 400  # ~100 tokens against a 50-token minimum — in band
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="A long existing recap.", min_tokens=50, strictness="strict",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "y" * 400
    assert len(calls) == 1


@pytest.mark.parametrize("strictness", ["guideline", "firm"])
@pytest.mark.asyncio
async def test_condense_job_only_strict_retries_an_out_of_band_recap(client, seed, monkeypatch, strictness):
    """guideline/firm stop at prompt wording even when the result blatantly
    misses the target — the out-of-band retry is the "strict" tier's whole
    distinguishing feature, so it must never leak into the other two."""
    calls = []

    async def fake_condense(recap, **kwargs):
        calls.append(kwargs)
        return "Too short."  # ~3 tokens against a 50-token minimum
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="A long existing recap.", min_tokens=50, strictness=strictness,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "Too short."
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_condense_job_strict_retry_failure_keeps_the_first_recap(client, seed, monkeypatch):
    """A retry that itself lands on a failure sentinel is discarded — the
    first draft was a usable answer, and reporting the failure text (or the
    starved sentinel) as a "done" recap would be strictly worse."""
    calls = []

    async def fake_condense(recap, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return "Too short."
        return "[AI error: connection refused]"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="A long existing recap.", min_tokens=50, strictness="strict",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "Too short."
    assert len(calls) == 2


# ── purpose="session_recap" seeded directly (create_text_recap_job) ────────
# docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2 item 3.2: a sibling of
# create_condense_job for the Live Recording panel's "Summarize in
# Background" button — same "text already in hand, no transcribe phase"
# shape, but seeded as purpose="session_recap" so _run_job's session_recap
# branch (map-reduce chunking, RAG, the Part 1 retry ladder) applies
# instead of condense_recap's single-call path.

@pytest.mark.asyncio
async def test_create_text_recap_job_runs_to_completion(client, seed):
    job_id = audio_jobs.create_text_recap_job(world_id=seed.world_a.id, text="A raw live transcript.")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.purpose == "session_recap"
    assert job.filename == "Live Transcript"
    assert job.transcript == "A raw live transcript."  # the input, unchanged
    assert job.recap == "The party met Elena at the bazaar."  # from the file's autouse _fake_ai fixture
    assert job.audio_path == ""


@pytest.mark.asyncio
async def test_create_text_recap_job_never_transcribes(client, seed, monkeypatch):
    async def should_not_be_called(*a, **k):
        raise AssertionError("transcribe_audio must not be called for a text-seeded recap job")
    monkeypatch.setattr(ai_module, "transcribe_audio", should_not_be_called)

    job_id = audio_jobs.create_text_recap_job(world_id=seed.world_a.id, text="Already-transcribed text.")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error


@pytest.mark.asyncio
async def test_create_text_recap_job_uses_the_full_session_recap_engine(client, seed, monkeypatch):
    """The whole point of seeding purpose="session_recap" instead of
    "condense": it gets summarize_transcript's chunking/RAG/retry-ladder
    machinery, not condense_recap's single-call path."""
    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, expanded_thinking=False, **kwargs):
        captured["called"] = True
        captured["extra_instructions"] = extra_instructions
        return "a woven recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    job_id = audio_jobs.create_text_recap_job(
        world_id=seed.world_a.id, text="Raw transcript text.", extra_instructions="focus on combat",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["called"] is True
    assert "focus on combat" in captured["extra_instructions"]
    assert job.recap == "a woven recap"


@pytest.mark.asyncio
async def test_create_text_recap_job_defaults_think_true(client, seed, monkeypatch):
    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", think=True, **kwargs):
        captured["think"] = think
        return "a recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    job_id = audio_jobs.create_text_recap_job(world_id=seed.world_a.id, text="text")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["think"] is True


# ── RAG (use_rag/rag_entity_limit/rag_notes_limit) ──────────────────────────

@pytest.mark.asyncio
async def test_create_condense_job_use_rag_off_by_default_no_world_context(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    def fail_if_called(*a, **kw):
        raise AssertionError("must not retrieve RAG context when use_rag is off")
    monkeypatch.setattr(audio_jobs, "_build_rag_context", fail_if_called)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="some recap")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["world_context"] == ""
    assert job.use_rag is False


@pytest.mark.asyncio
async def test_create_condense_job_use_rag_passes_retrieved_context_through(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    build_calls = []

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kwargs):
        build_calls.append((world_id, query, entity_limit, notes_limit))
        return "- [npc] Gareth: a blacksmith"
    monkeypatch.setattr(audio_jobs, "_build_rag_context", fake_build_rag_context)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="some recap", use_rag=True, rag_entity_limit=7, rag_notes_limit=2,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["world_context"] == "- [npc] Gareth: a blacksmith"
    assert build_calls == [(seed.world_a.id, "some recap", 7, 2)]
    assert job.use_rag is True
    assert job.rag_entity_limit == 7
    assert job.rag_notes_limit == 2


@pytest.mark.asyncio
async def test_create_condense_job_use_rag_blank_limits_use_module_defaults(client, seed, monkeypatch):
    build_calls = []

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kwargs):
        build_calls.append((entity_limit, notes_limit))
        return ""
    monkeypatch.setattr(audio_jobs, "_build_rag_context", fake_build_rag_context)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="some recap", use_rag=True)
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert build_calls == [(audio_jobs._DEFAULT_RAG_ENTITY_LIMIT, audio_jobs._DEFAULT_RAG_NOTES_LIMIT)]


@pytest.mark.asyncio
async def test_create_job_session_recap_use_rag_passes_retrieved_context_through(client, seed, tmp_path, monkeypatch):
    build_calls = []

    def fake_build_rag_context(world_id, query, entity_limit, notes_limit, **kwargs):
        build_calls.append((world_id, query, entity_limit, notes_limit))
        return "- [place] The Rusty Anchor: a tavern"
    monkeypatch.setattr(audio_jobs, "_build_rag_context", fake_build_rag_context)

    captured = {}

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return "recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, use_rag=True, rag_entity_limit=3, rag_notes_limit=1,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["world_context"] == "- [place] The Rusty Anchor: a tavern"
    # queried against the transcribed transcript (fake_transcribe's fixed
    # output), not the audio file itself — there's nothing else to search on.
    assert build_calls == [(seed.world_a.id, "the party met elena at the bazaar", 3, 1)]


def test_build_rag_context_combines_relevant_entities_and_guaranteed_notes(client, seed):
    npc_id = _make_entity(
        seed.world_a.id, name="Gareth", kind="character", summary="A blacksmith.",
        body="Gareth runs the forge near the eastern gate.",
    )
    other_note_id = _make_entity(seed.world_a.id, name="Unrelated Note", kind="note", body="Nothing to do with Gareth.")
    context = audio_jobs._build_rag_context(seed.world_a.id, "Gareth the blacksmith", entity_limit=10, notes_limit=10)
    assert "Gareth" in context
    assert "Unrelated Note" in context  # guaranteed via notes_limit, independent of relevance


def test_build_rag_context_zero_limits_retrieve_nothing():
    context = audio_jobs._build_rag_context(999999, "anything", entity_limit=0, notes_limit=0)
    assert context == ""


def test_build_rag_context_tops_up_entities_for_a_foreign_language_query(client, seed):
    """A Russian-language transcript against English-named entities has no
    literal keyword overlap for _find_relevant_entities' FTS/ILIKE search
    to find — real report: a GM running Russian-language sessions got RAG
    context with notes but no characters/places at all, since the keyword
    search came back empty without ever hitting _find_relevant_entities'
    own "no query words" fallback (a foreign-language query still splits
    into plenty of real "words," just ones that can't match anything).
    The top-up must still surface the character even though the query
    text shares no characters with its name."""
    npc_id = _make_entity(
        seed.world_a.id, name="Gareth Ashfall", kind="character", summary="A blacksmith.",
        body="Gareth Ashfall runs the forge near the eastern gate.",
    )
    russian_query = "Партия встретила Гарета возле восточных ворот"
    context = audio_jobs._build_rag_context(seed.world_a.id, russian_query, entity_limit=10, notes_limit=0)
    assert "Gareth Ashfall" in context


def test_build_rag_context_no_topup_leak_when_relevance_search_already_fills_the_limit(client, seed):
    """When the keyword search alone already returns entity_limit results,
    the top-up must not add anything beyond that budget — an unrelated
    entity that would otherwise get topped up must not appear once the
    limit is already spent on genuinely relevant matches."""
    _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character", body="A blacksmith.")
    _make_entity(seed.world_a.id, name="Completely Unrelated Entity", kind="location", body="Nothing to do with Gareth.")
    context = audio_jobs._build_rag_context(seed.world_a.id, "Gareth Ashfall the blacksmith", entity_limit=1, notes_limit=0)
    assert "Gareth Ashfall" in context
    assert "Completely Unrelated Entity" not in context


# ── pinned_entity_ids (GM-checked "Entities Featured" → guaranteed RAG) ─────

def test_build_rag_context_always_includes_pinned_entities(client, seed):
    """A pinned entity must show up even when the query text shares no
    words with it at all — exactly the case a GM checking it manually is
    meant to guarantee against keyword search missing it."""
    pinned_id = _make_entity(seed.world_a.id, name="Crimson Doll", kind="character", body="A masked performer.")
    context = audio_jobs._build_rag_context(
        seed.world_a.id, "completely unrelated query text", entity_limit=0, notes_limit=0,
        pinned_entity_ids=[pinned_id],
    )
    assert "Crimson Doll" in context


def test_build_rag_context_pinned_entities_dont_count_against_entity_limit(client, seed):
    """Pinned entities are guaranteed ON TOP of entity_limit, not carved
    out of it — a relevant match found by keyword search must still appear
    alongside a pinned entity, not get bumped out to make room."""
    pinned_id = _make_entity(seed.world_a.id, name="Crimson Doll", kind="character", body="A masked performer.")
    relevant_id = _make_entity(
        seed.world_a.id, name="Gareth Ashfall", kind="character",
        body="Gareth Ashfall runs the forge near the eastern gate.",
    )
    context = audio_jobs._build_rag_context(
        seed.world_a.id, "Gareth Ashfall the blacksmith", entity_limit=1, notes_limit=0,
        pinned_entity_ids=[pinned_id],
    )
    assert "Crimson Doll" in context
    assert "Gareth Ashfall" in context


def test_build_rag_context_pinned_entity_not_duplicated_when_also_relevant(client, seed):
    """A pinned entity that the keyword search would ALSO have found on
    its own must appear exactly once, not twice."""
    pinned_id = _make_entity(
        seed.world_a.id, name="Gareth Ashfall", kind="character",
        body="Gareth Ashfall runs the forge near the eastern gate.",
    )
    context = audio_jobs._build_rag_context(
        seed.world_a.id, "Gareth Ashfall the blacksmith", entity_limit=10, notes_limit=0,
        pinned_entity_ids=[pinned_id],
    )
    # Count the entity's own one-liner bullet, not every substring
    # occurrence — format_context_from_entities (see AI 1.1) also appends
    # a body excerpt under the first few entities, which for this entity
    # legitimately repeats its name as part of the body text itself.
    assert context.count("- [character] Gareth Ashfall") == 1


def test_session_featured_picks_reads_saved_npcs_json(client, seed):
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GameSession

    e1 = _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character")
    e2 = _make_entity(seed.world_a.id, name="The Bazaar", kind="location")
    db = _SL()
    try:
        gs = _GameSession(
            world_id=seed.world_a.id, title="Session 1", session_num=1,
            npcs_json=json.dumps([
                {"entity_id": e1, "name": "Gareth Ashfall", "kind": "entity"},
                {"entity_id": e2, "name": "The Bazaar", "kind": "entity"},
            ]),
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    entity_ids, pc_ids = audio_jobs._session_featured_picks(gs_id)
    assert set(entity_ids) == {e1, e2}
    assert pc_ids == []


def test_session_featured_picks_splits_player_character_kind(client, seed):
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GameSession, PlayerCharacter as _PlayerCharacter

    e1 = _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character")
    db = _SL()
    try:
        pc = _PlayerCharacter(world_id=seed.world_a.id, name="Boric Stonehand")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
        gs = _GameSession(
            world_id=seed.world_a.id, title="Session 1", session_num=1,
            npcs_json=json.dumps([
                {"entity_id": e1, "name": "Gareth Ashfall", "kind": "entity"},
                {"entity_id": pc_id, "name": "Boric Stonehand", "kind": "player_character"},
            ]),
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    entity_ids, pc_ids = audio_jobs._session_featured_picks(gs_id)
    assert entity_ids == [e1]
    assert pc_ids == [pc_id]


def test_session_featured_picks_legacy_rows_without_kind_default_to_entity(client, seed):
    """A pre-existing row saved before PlayerCharacter picks existed has no
    "kind" key at all — must default to "entity", not drop the pick."""
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GameSession

    e1 = _make_entity(seed.world_a.id, name="Gareth Ashfall", kind="character")
    db = _SL()
    try:
        gs = _GameSession(
            world_id=seed.world_a.id, title="Session 1", session_num=1,
            npcs_json=json.dumps([{"entity_id": e1, "name": "Gareth Ashfall"}]),
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    entity_ids, pc_ids = audio_jobs._session_featured_picks(gs_id)
    assert entity_ids == [e1]
    assert pc_ids == []


def test_session_featured_picks_empty_for_no_session_or_no_picks(client, seed):
    assert audio_jobs._session_featured_picks(999999) == ([], [])


def test_build_rag_context_always_includes_pinned_player_characters(client, seed):
    from app.database import SessionLocal as _SL
    from app.models import PlayerCharacter as _PlayerCharacter

    db = _SL()
    try:
        pc = _PlayerCharacter(world_id=seed.world_a.id, name="Boric Stonehand", race="Dwarf", char_class="Fighter", background="Blacksmith's apprentice")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
    finally:
        db.close()

    context = audio_jobs._build_rag_context(
        seed.world_a.id, "completely unrelated query text", entity_limit=0, notes_limit=0,
        pinned_pc_ids=[pc_id],
    )
    assert "Boric Stonehand" in context
    assert "Dwarf" in context
    assert "Fighter" in context


@pytest.mark.asyncio
async def test_create_condense_job_use_rag_pins_session_player_character(client, seed, monkeypatch):
    """End-to-end through the job engine: a session's saved PlayerCharacter
    pick reaches world_context even though the condensed text shares no
    words with the character's name at all."""
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GameSession, PlayerCharacter as _PlayerCharacter

    db = _SL()
    try:
        pc = _PlayerCharacter(world_id=seed.world_a.id, name="Boric Stonehand")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pc_id = pc.id
        gs = _GameSession(
            world_id=seed.world_a.id, title="Session 1", session_num=1,
            npcs_json=json.dumps([{"entity_id": pc_id, "name": "Boric Stonehand", "kind": "player_character"}]),
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="something entirely unrelated to any established name",
        use_rag=True, game_session_id=gs_id,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert "Boric Stonehand" in (captured["world_context"] or "")


@pytest.mark.asyncio
async def test_create_condense_job_use_rag_pins_session_featured_entities(client, seed, monkeypatch):
    """End-to-end through the job engine: a session's saved "Entities
    Featured" picks reach world_context even though the condensed text
    shares no words with the pinned entity's name at all."""
    from app.database import SessionLocal as _SL
    from app.models import GameSession as _GameSession

    pinned_id = _make_entity(seed.world_a.id, name="Crimson Doll", kind="character", body="A masked performer.")
    db = _SL()
    try:
        gs = _GameSession(
            world_id=seed.world_a.id, title="Session 1", session_num=1,
            npcs_json=json.dumps([{"entity_id": pinned_id, "name": "Crimson Doll"}]),
        )
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["world_context"] = kwargs.get("world_context")
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="something entirely unrelated to any established name",
        use_rag=True, game_session_id=gs_id,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert "Crimson Doll" in (captured["world_context"] or "")


@pytest.mark.asyncio
async def test_create_condense_job_passes_min_max_tokens_and_extra_instructions(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, extra_instructions="", min_tokens=None, max_tokens=None, **kwargs):
        captured["extra_instructions"] = extra_instructions
        captured["min_tokens"] = min_tokens
        captured["max_tokens"] = max_tokens
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text="some recap",
        extra_instructions="focus on combat", min_tokens=50, max_tokens=200,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured["extra_instructions"] == "focus on combat"
    assert captured["min_tokens"] == 50
    assert captured["max_tokens"] == 200
    assert job.min_tokens == 50
    assert job.max_tokens == 200
    assert job.extra_instructions == "focus on combat"


@pytest.mark.asyncio
async def test_create_condense_job_fit_context_widens_reserve_for_max_tokens(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["options"] = options
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    text = "word " * 2000
    job_id = audio_jobs.create_condense_job(
        world_id=seed.world_a.id, text=text, fit_context=True, max_tokens=4000,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    default_ctx = ai_module.context_sized_options(text)["num_ctx"]
    assert captured["options"]["num_ctx"] > default_ctx


@pytest.mark.asyncio
async def test_create_condense_job_marks_error_on_failure_sentinel(client, seed, monkeypatch):
    async def failing_condense(recap, model="", options=None, think=True, **kwargs):
        return "[AI error: Ollama unreachable]"
    monkeypatch.setattr(ai_module, "condense_recap", failing_condense)

    job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="text")
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == "[AI error: Ollama unreachable]"


@pytest.mark.asyncio
async def test_job_ends_in_error_on_empty_transcript(client, seed, tmp_path, monkeypatch):
    async def empty_transcribe(path, glossary="", **kwargs):
        return ""
    monkeypatch.setattr(ai_module, "transcribe_audio", empty_transcribe)

    audio = tmp_path / "silent.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="silent.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "whisper" in job.error.lower() or "transcribe" in job.error.lower()


@pytest.mark.asyncio
async def test_job_saves_partial_transcript_on_mid_chunk_whisper_failure(client, seed, tmp_path, monkeypatch):
    """A WhisperError carrying partial_transcript (transcribe_audio's
    chunked path, on a failure after at least one chunk succeeded) must be
    saved to the job row — not just the error message — so the GM can
    resummarize from what was actually transcribed instead of losing it
    along with the failure."""
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError(
            "Whisper failed on part 3 of 4: container restarted. The first 2 part(s) were transcribed.",
            partial_transcript="part 0\npart 1",
        )
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "part 3 of 4" in job.error
    assert job.transcript == "part 0\npart 1"


@pytest.mark.asyncio
async def test_job_error_has_no_transcript_when_whisper_error_has_no_partial(client, seed, tmp_path, monkeypatch):
    async def failing_transcribe(path, glossary="", **kwargs):
        raise ai_module.WhisperError("whisper unreachable")
    monkeypatch.setattr(ai_module, "transcribe_audio", failing_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert not job.transcript


@pytest.mark.asyncio
async def test_job_ends_in_error_on_exception(client, seed, tmp_path, monkeypatch):
    async def raising_transcribe(path, glossary="", **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(ai_module, "transcribe_audio", raising_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "boom" in job.error
    assert not audio.exists()  # still cleaned up even on failure


# ── Chunk progress (map-reduce summarization) — see test_transcript_chunking
# .py for the underlying app.ai.summarize_transcript(on_progress=...) unit
# tests; these confirm audio_jobs.py actually persists it to the DB row.

@pytest.mark.asyncio
async def test_chunk_progress_visible_mid_summarize_and_cleared_when_done(client, seed, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_module, "_transcript_chunk_char_budget", lambda *a, **k: 50)

    hang_on_second_chunk = asyncio.Event()
    release_second_chunk = asyncio.Event()
    call_count = {"n": 0}

    async def fake_transcribe(path, glossary="", **kwargs):
        return ("The party explored the ruins. " * 30).strip()

    async def fake_generate_chat(messages, system="", model="", options=None, think=True):
        call_count["n"] += 1
        if call_count["n"] == 2:
            hang_on_second_chunk.set()
            await release_second_chunk.wait()
        return "[part]"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)
    # Override the file's autouse _fake_ai fixture, which replaces
    # summarize_transcript wholesale — this test needs the REAL per-part
    # chunking logic (driving fake_generate_chat above) to actually run.
    monkeypatch.setattr(ai_module, "summarize_transcript", _REAL_SUMMARIZE_TRANSCRIPT)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )

    await asyncio.wait_for(hang_on_second_chunk.wait(), timeout=5)
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "summarizing"
        assert job.chunk_current == 2
        assert job.chunk_total is not None and job.chunk_total > 1
    finally:
        db.close()

    release_second_chunk.set()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.chunk_current is None
    assert job.chunk_total is None


def test_unified_job_status_route_exposes_chunk_progress(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="summarizing",
                        filename="x.mp3", chunk_current=2, chunk_total=5)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["chunk_current"] == 2
    assert r.json()["chunk_total"] == 5


def test_session_job_status_route_exposes_chunk_progress(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="summarizing",
                        filename="x.mp3", chunk_current=1, chunk_total=3)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/sessions/ai/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["chunk_current"] == 1
    assert r.json()["chunk_total"] == 3


# ── Job survival: checkpointing, resume, shutdown guard (app/job_shutdown.py) ─
#
# See that module's own docstring for the full "stop fast, trust the
# checkpoint" design. These tests cover audio_jobs.py's own half of the
# contract: persisting audio_path/delete_after so a resume can find the
# file, wiring app.ai's on_checkpoint/should_stop/resume through _run_job,
# distinguishing a shutdown-driven cancel from a GM-driven one, and the
# boot-time auto-resume pass.

@pytest.fixture(autouse=True)
def _reset_job_shutdown_flag():
    _job_shutdown.clear_stop()
    yield
    _job_shutdown.clear_stop()


async def _await_status_in(job_id, statuses, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in statuses:
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached one of {statuses}, last seen status={job.status!r}")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_create_job_persists_audio_path_and_delete_after(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=False,
    )
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.audio_path == str(audio)
        assert job.delete_after is False
    finally:
        db.close()
    await _await_terminal(job_id)


@pytest.mark.asyncio
async def test_checkpoint_written_to_the_row_after_each_transcribed_chunk(client, seed, tmp_path, monkeypatch):
    hang = asyncio.Event()
    release = asyncio.Event()

    async def fake_transcribe(path, glossary="", **kwargs):
        on_checkpoint = kwargs["on_checkpoint"]
        on_checkpoint({"phase": "transcribe", "chunks_done": 1, "chunk_total": 2,
                        "chunk_seconds": 600, "audio_size": path.stat().st_size, "text": "part 0"})
        hang.set()
        await release.wait()
        on_checkpoint({"phase": "transcribe", "chunks_done": 2, "chunk_total": 2,
                        "chunk_seconds": 600, "audio_size": path.stat().st_size, "text": "part 0\npart 1"})
        return "part 0\npart 1"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await asyncio.wait_for(hang.wait(), timeout=5)

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        cp = json.loads(job.checkpoint_json)
        assert cp["chunks_done"] == 1
        assert cp["chunk_total"] == 2
    finally:
        db.close()

    release.set()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error


@pytest.mark.asyncio
async def test_checkpoint_mirrors_the_partial_transcript_into_the_transcript_column(client, seed, tmp_path, monkeypatch):
    hang = asyncio.Event()
    release = asyncio.Event()

    async def fake_transcribe(path, glossary="", **kwargs):
        on_checkpoint = kwargs["on_checkpoint"]
        on_checkpoint({"phase": "transcribe", "chunks_done": 1, "chunk_total": 2,
                        "chunk_seconds": 600, "audio_size": path.stat().st_size, "text": "part 0 so far"})
        hang.set()
        await release.wait()
        return "part 0 so far\npart 1"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await asyncio.wait_for(hang.wait(), timeout=5)

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        # A GM watching the Background Jobs page mid-run sees real progress,
        # same as WhisperError's own partial_transcript salvage already did
        # on a hard failure — this is the checkpoint's live-progress version.
        assert job.transcript == "part 0 so far"
    finally:
        db.close()

    release.set()
    await _await_terminal(job_id)


@pytest.mark.asyncio
async def test_summarize_checkpoint_is_not_mirrored_into_recap(client, seed, tmp_path, monkeypatch):
    hang = asyncio.Event()
    release = asyncio.Event()

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        on_checkpoint = kwargs["on_checkpoint"]
        on_checkpoint({"phase": "summarize", "parts_done": 1, "chunk_total": 2,
                        "chunk_chars": 50, "text": "part summary 0"})
        hang.set()
        await release.wait()
        return "part summary 0\n\npart summary 1"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await asyncio.wait_for(hang.wait(), timeout=5)

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.recap == ""  # unlike the transcribe phase, deliberately not mirrored
        assert json.loads(job.checkpoint_json)["text"] == "part summary 0"
    finally:
        db.close()

    release.set()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.recap == "part summary 0\n\npart summary 1"


@pytest.mark.asyncio
async def test_checkpoint_cleared_once_the_job_reaches_a_terminal_status(client, seed, tmp_path, monkeypatch):
    async def fake_transcribe(path, glossary="", **kwargs):
        on_checkpoint = kwargs["on_checkpoint"]
        on_checkpoint({"phase": "transcribe", "chunks_done": 1, "chunk_total": 1,
                        "chunk_seconds": 600, "audio_size": path.stat().st_size, "text": "part 0"})
        return "part 0"

    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI error: boom]"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.checkpoint_json == ""


@pytest.mark.asyncio
async def test_shutdown_cancel_marks_interrupted_not_cancelled(client, seed, tmp_path, _hanging_transcribe):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await _await_status_in(job_id, {"transcribing"})
    _job_shutdown.request_stop()
    assert audio_jobs.cancel_job(job_id)
    job = await _await_status_in(job_id, {"interrupted", "cancelled", "done", "error"})
    assert job.status == "interrupted"
    assert "restart" in job.error.lower()


@pytest.mark.asyncio
async def test_gm_cancel_still_marks_cancelled_while_not_stopping(client, seed, tmp_path, _hanging_transcribe):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await _await_status_in(job_id, {"transcribing"})
    assert not _job_shutdown.stopping()
    assert audio_jobs.cancel_job(job_id)
    job = await _await_status_in(job_id, {"interrupted", "cancelled", "done", "error"})
    assert job.status == "cancelled"
    assert job.error == "Cancelled by GM."


@pytest.mark.asyncio
async def test_interrupted_job_keeps_its_audio_file_for_resume(client, seed, tmp_path, _hanging_transcribe):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await _await_status_in(job_id, {"transcribing"})
    _job_shutdown.request_stop()
    audio_jobs.cancel_job(job_id)
    await _await_status_in(job_id, {"interrupted", "cancelled", "done", "error"})
    assert audio.exists()  # kept despite delete_after=True — a resume needs it


@pytest.mark.asyncio
async def test_gm_cancelled_job_still_deletes_its_audio_file(client, seed, tmp_path, _hanging_transcribe):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    await _await_status_in(job_id, {"transcribing"})
    audio_jobs.cancel_job(job_id)
    await _await_status_in(job_id, {"interrupted", "cancelled", "done", "error"})
    assert not audio.exists()  # a genuine GM cancel still cleans up, same as before


def test_forget_task_marks_interrupted_when_stopping(client, seed, tmp_path):
    _job_shutdown.request_stop()
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="pending", filename="x.mp3",
                        audio_path=str(audio), delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=True)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task)

    assert audio.exists()  # kept for resume, unlike the GM-cancel path
    db = SessionLocal()
    try:
        updated = db.get(AudioJob, job_id)
        assert updated.status == "interrupted"
        assert "restart" in updated.error.lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resume_interrupted_jobs_restarts_from_the_checkpoint(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio-bytes")

    calls = []

    async def fake_transcribe(path, glossary="", **kwargs):
        calls.append(kwargs.get("resume"))
        return "part 0\npart 1"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    checkpoint = {"phase": "transcribe", "chunks_done": 1, "chunk_total": 2,
                  "chunk_seconds": 600, "audio_size": audio.stat().st_size, "text": "part 0"}
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True,
                       checkpoint_json=json.dumps(checkpoint), chunk_current=1, chunk_total=2,
                       error="Paused by a server restart at part 1 of 2 — the work so far is saved.")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    resumed_count = audio_jobs.resume_interrupted_jobs()
    assert resumed_count == 1

    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.resumed_count == 1
    assert calls and calls[0] == checkpoint


@pytest.mark.asyncio
async def test_resume_skips_already_transcribed_chunks(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio-bytes")

    async def fake_transcribe(path, glossary="", **kwargs):
        resume = kwargs.get("resume")
        assert resume and resume["chunks_done"] == 1
        return resume["text"] + "\npart 1"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    checkpoint = {"phase": "transcribe", "chunks_done": 1, "chunk_total": 2,
                  "chunk_seconds": 600, "audio_size": audio.stat().st_size, "text": "part 0"}
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True,
                       checkpoint_json=json.dumps(checkpoint))
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    audio_jobs.resume_interrupted_jobs()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.transcript == "part 0\npart 1"


@pytest.mark.asyncio
async def test_resume_does_not_skip_transcription_when_transcript_is_only_a_checkpoint_mirror(
    client, seed, tmp_path, monkeypatch,
):
    """Regression test: a transcribe-phase checkpoint's own on_checkpoint
    callback mirrors its partial text into job.transcript (see _checkpoint
    in _run_job) — the same field a FULLY finished transcription also
    populates. Naively treating "job.transcript is non-empty" as "resume
    straight into summarizing/done" would let a job interrupted partway
    through transcription skip the rest of the audio entirely — for
    purpose="attachment" (no summarize phase at all) that meant landing on
    "done" having only transcribed the first chunk. transcribe_audio must
    still be called (with the checkpoint as its resume=) whenever the
    checkpoint's own phase is still "transcribe", regardless of what
    job.transcript already holds."""
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio-bytes")

    calls = []

    async def fake_transcribe(path, glossary="", **kwargs):
        calls.append(kwargs.get("resume"))
        return "part 0\npart 1"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    checkpoint = {"phase": "transcribe", "chunks_done": 1, "chunk_total": 2,
                  "chunk_seconds": 600, "audio_size": audio.stat().st_size, "text": "part 0"}
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True,
                       checkpoint_json=json.dumps(checkpoint),
                       transcript="part 0")  # the checkpoint's own mirror, NOT a finished transcript
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    audio_jobs.resume_interrupted_jobs()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert calls and calls[0] == checkpoint  # transcribe_audio WAS called, not skipped
    assert job.transcript == "part 0\npart 1"  # the real (full) result, not the stale mirror


@pytest.mark.asyncio
async def test_resume_of_a_job_with_a_transcript_goes_straight_to_summarizing(client, seed, monkeypatch):
    transcribe_calls = []

    async def fake_transcribe(path, glossary="", **kwargs):
        transcribe_calls.append(1)
        return "should not be called"

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="interrupted",
                       filename="clip.mp3", audio_path="", delete_after=True,
                       transcript="the party explored the ruins")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    audio_jobs.resume_interrupted_jobs()
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert transcribe_calls == []  # never re-transcribed — the audio is gone anyway
    assert job.recap  # from the file's autouse _fake_ai fixture


def test_resume_gives_up_after_max_auto_resumes_and_keeps_the_transcript(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path="/nonexistent/path.mp3", delete_after=True,
                       transcript="salvaged so far", resumed_count=_job_shutdown.MAX_AUTO_RESUMES)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    resumed = audio_jobs.resume_interrupted_jobs()
    assert resumed == 0

    db = SessionLocal()
    try:
        j = db.get(AudioJob, job_id)
        assert j.status == "error"
        assert "restart" in j.error.lower()
        assert str(_job_shutdown.MAX_AUTO_RESUMES) in j.error
        assert j.transcript == "salvaged so far"  # kept, not discarded
    finally:
        db.close()


def test_resume_marks_error_when_the_audio_file_is_gone_and_no_transcript(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path="/nonexistent/path.mp3", delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    resumed = audio_jobs.resume_interrupted_jobs()
    assert resumed == 0

    db = SessionLocal()
    try:
        j = db.get(AudioJob, job_id)
        assert j.status == "error"
        assert "restart" in j.error.lower()
        assert "re-upload" in j.error.lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resume_sets_chunk_progress_synchronously_before_the_task_starts(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    checkpoint = {"phase": "transcribe", "chunks_done": 3, "chunk_total": 7,
                  "chunk_seconds": 600, "audio_size": audio.stat().st_size, "text": "abc"}
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True,
                       checkpoint_json=json.dumps(checkpoint))
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    job_snapshot = audio_jobs.start_resume_job(job_id)
    assert job_snapshot.status == "transcribing"
    assert job_snapshot.chunk_current == 3
    assert job_snapshot.chunk_total == 7
    assert job_snapshot.resumed_count == 1

    await _await_terminal(job_id)  # let the (fast, faked) background task finish cleanly


def test_resume_route_round_trip(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resume")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "transcribing"
    assert body["resumable"] is False  # no longer "interrupted"

    data = _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")
    assert data["status"] == "done"


def test_resume_route_requires_gm(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resume")
    assert r.status_code == 403


def test_resume_route_cross_world_isolation(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resume")
    assert r.status_code == 404


def test_resume_route_rejects_a_job_already_in_progress(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="transcribing", filename="clip.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resume")
    assert r.status_code == 400


def test_resume_route_resets_the_attempt_counter(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                       filename="clip.mp3", audio_path=str(audio), delete_after=True,
                       resumed_count=_job_shutdown.MAX_AUTO_RESUMES)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resume")
    assert r.status_code == 200, r.text
    assert r.json()["resumed_count"] == 0


def test_sweep_orphaned_job_audio_keeps_a_file_a_resumable_job_still_needs(client, seed, tmp_path):
    jobs_dir = _session_audio_jobs_dir_for_test()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    kept_file = jobs_dir / "still-needed.mp3"
    kept_file.write_bytes(b"x")
    old_cutoff_time = time.time() - audio_jobs._SESSION_AUDIO_JOBS_CUTOFF_SECONDS - 3600
    os.utime(kept_file, (old_cutoff_time, old_cutoff_time))  # old enough to normally be swept

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="interrupted",
                       filename="clip.mp3", audio_path=str(kept_file), delete_after=True)
        db.add(job)
        db.commit()
    finally:
        db.close()

    audio_jobs.sweep_orphaned_job_audio()

    assert kept_file.exists()
    kept_file.unlink()


def test_sweep_interrupted_jobs_marks_in_progress_as_interrupted(client, seed):
    """A job still mid-flight at boot means the process died UNCLEANLY (a
    crash/OOM/SIGKILL — job_shutdown's own drain()/mark_stragglers_interrupted
    already handle a clean shutdown). It's marked "interrupted", not
    "error" — the same status a clean shutdown leaves a paused job in — so
    resume_interrupted_jobs (called right after this in the same startup
    hook) picks it up and auto-resumes it, same as any other interruption."""
    db = SessionLocal()
    try:
        stuck = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        done = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="y.mp3")
        db.add_all([stuck, done])
        db.commit()
        db.refresh(stuck)
        db.refresh(done)
        stuck_id, done_id = stuck.id, done.id
    finally:
        db.close()

    audio_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        s = db.get(AudioJob, stuck_id)
        d = db.get(AudioJob, done_id)
        assert s.status == "interrupted"
        assert "restart" in s.error.lower()
        assert d.status == "done"
        assert d.error == ""
    finally:
        db.close()


def _session_audio_jobs_dir_for_test():
    return Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads" / "session_audio" / "_jobs"


def test_sweep_orphaned_job_audio_removes_old_files(tmp_path):
    """A file left behind because a crash/restart skipped _run_job's own
    cleanup finally (sweep_interrupted_jobs only fixes the DB row, not the
    file) must eventually be removed, or an orphan like this leaks disk
    forever."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    old_file = jobs_dir / "orphaned-old.mp3"
    old_file.write_bytes(b"x")
    old_cutoff_time = time.time() - audio_jobs._SESSION_AUDIO_JOBS_CUTOFF_SECONDS - 3600
    os.utime(old_file, (old_cutoff_time, old_cutoff_time))

    audio_jobs.sweep_orphaned_job_audio()

    assert not old_file.exists()


def test_sweep_orphaned_job_audio_keeps_recent_files(tmp_path):
    """A file that's recent enough to plausibly belong to a job still
    genuinely in flight (this sweep runs at startup, before any new job
    could have been created) must survive — sweep_interrupted_jobs already
    errored out anything that was actually running at shutdown, but the
    cutoff is deliberately generous rather than exact."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    recent_file = jobs_dir / "recent.mp3"
    recent_file.write_bytes(b"x")

    audio_jobs.sweep_orphaned_job_audio()

    assert recent_file.exists()
    recent_file.unlink()


def test_sweep_orphaned_job_audio_is_a_noop_when_dir_missing(tmp_path):
    jobs_dir = _session_audio_jobs_dir_for_test()
    shutil.rmtree(jobs_dir, ignore_errors=True)
    audio_jobs.sweep_orphaned_job_audio()  # must not raise


def test_sweep_orphaned_job_audio_leaves_ai_attachments_alone(tmp_path):
    """Attachment jobs run with delete_after=False — the file IS the
    attachment, not working storage — so this sweep must only ever touch
    uploads/session_audio/_jobs/, never uploads/ai_attachments/."""
    jobs_dir = _session_audio_jobs_dir_for_test()
    attachments_dir = jobs_dir.parent.parent / "ai_attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    old_attachment = attachments_dir / "old-attachment.mp3"
    old_attachment.write_bytes(b"x")
    old_cutoff_time = time.time() - audio_jobs._SESSION_AUDIO_JOBS_CUTOFF_SECONDS - 3600
    os.utime(old_attachment, (old_cutoff_time, old_cutoff_time))

    audio_jobs.sweep_orphaned_job_audio()

    assert old_attachment.exists()
    old_attachment.unlink()


# ── Sessions routes: /api/sessions/ai/audio-jobs* ───────────────────────────

def test_session_job_create_poll_and_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["purpose"] == "session_recap"
    assert data["transcript"] == "the party met elena at the bazaar"
    assert data["recap"] == "The party met Elena at the bazaar."

    r = client.get("/api/sessions/ai/audio-jobs")
    assert r.status_code == 200
    assert any(j["id"] == job_id for j in r.json())


def test_session_job_chunked_create(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "a" * 32
    part_a, part_b = b"first-half-", b"second-half"
    r0 = client.post("/api/sessions/ai/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(part_a), "application/octet-stream")})
    r1 = client.post("/api/sessions/ai/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files={"file": ("part", io.BytesIO(part_b), "application/octet-stream")})
    assert r0.status_code == 200 and r1.status_code == 200

    r = client.post("/api/sessions/ai/audio-jobs/complete",
                     data={"upload_id": upload_id, "filename": "big.mp3", "total_chunks": "2"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done"


def test_session_job_links_to_game_session_when_given(client, seed):
    from app.models import GameSession
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs", data={"game_session_id": str(gs_id)},
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["game_session_id"] == gs_id


def test_session_job_rejects_unsupported_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400


def test_session_job_oversized_file_rejected(client, seed, monkeypatch):
    monkeypatch.setattr("app.routers.sessions.MAX_SESSION_AUDIO_BYTES", 4)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"way too much data"), "audio/mpeg")})
    assert r.status_code == 413


def test_session_job_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 403
    r2 = client.get("/api/sessions/ai/audio-jobs")
    assert r2.status_code == 403


def test_session_job_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/sessions/ai/audio-jobs/{job_id}")
    assert r.status_code == 404


# ── Sessions routes: /api/sessions/ai/condense-job ──────────────────────────

def test_condense_job_create_poll_and_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/condense-job", json={"recap": "A long recap to condense."})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data
    assert data["purpose"] == "condense"
    assert data["transcript"] == "A long recap to condense."
    assert data["recap"] == "Condensed: A long recap to cond"

    r = client.get("/api/sessions/ai/audio-jobs")
    assert r.status_code == 200
    assert any(j["id"] == job_id for j in r.json())  # shows up alongside session_recap jobs


def test_condense_job_requires_recap(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "  "})
    assert r.status_code == 400


def test_condense_job_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "text"})
    assert r.status_code == 403


def test_condense_job_passes_model_think_and_fit_context(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        captured["model"] = model
        captured["think"] = think
        captured["options"] = options
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    recap = "word " * 2000
    r = client.post("/api/sessions/ai/condense-job", json={
        "recap": recap, "model": "llama3.1", "think": False, "fit_context": True,
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data
    assert captured["model"] == "llama3.1"
    assert captured["think"] is False
    assert captured["options"] == ai_module.context_sized_options(recap)


def test_condense_job_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "text"})
    job_id = r.json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/sessions/ai/audio-jobs/{job_id}")
    assert r.status_code == 404
    listed = client.get("/api/sessions/ai/audio-jobs").json()
    assert all(j["id"] != job_id for j in listed)


def test_condense_job_passes_min_max_tokens_and_extra_instructions(client, seed, monkeypatch):
    captured = {}

    async def fake_condense(recap, model="", options=None, think=True, extra_instructions="", min_tokens=None, max_tokens=None, **kwargs):
        captured["extra_instructions"] = extra_instructions
        captured["min_tokens"] = min_tokens
        captured["max_tokens"] = max_tokens
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={
        "recap": "text", "min_tokens": 20, "max_tokens": 100, "extra_instructions": "focus on combat",
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data
    assert captured["extra_instructions"] == "focus on combat"
    assert captured["min_tokens"] == 20
    assert captured["max_tokens"] == 100


def test_condense_job_route_persists_strictness(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "text", "strictness": "firm"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data
    # The unified Background Jobs status route exposes the strictness the
    # job was created with (the session-scoped poll shape above is
    # deliberately lean and doesn't carry per-condense settings).
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["strictness"] == "firm"

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.condense_strictness == "firm"
    finally:
        db.close()


def test_condense_job_route_defaults_strictness_to_guideline(client, seed):
    """No strictness field in the body (today's UI payloads before the
    setting existed) must read as "guideline" everywhere — the row AND the
    unified status route, which normalizes NULL for pre-migration rows."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "text"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["strictness"] == "guideline"

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.condense_strictness == "guideline"
    finally:
        db.close()


def test_condense_job_route_rejects_bogus_strictness(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "text", "strictness": "bogus"})
    assert r.status_code == 400
    assert "strictness" in r.json()["detail"]


def test_job_status_route_condense_strictness_null_reads_as_guideline(client, seed):
    """A pre-migration row (column NULL) must report "guideline" through the
    unified status route too — same NULL-reads-as-default convention
    _run_job itself applies."""
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="condense", filename="old.mp3",
                       status="done", condense_strictness=None)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["strictness"] == "guideline"


@pytest.mark.parametrize("body", [
    {"recap": "text", "min_tokens": "not a number"},
    {"recap": "text", "max_tokens": "not a number"},
    {"recap": "text", "min_tokens": 0},
    {"recap": "text", "max_tokens": -5},
    {"recap": "text", "min_tokens": 100, "max_tokens": 50},
])
def test_condense_job_rejects_invalid_token_bounds(client, seed, body):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json=body)
    assert r.status_code == 400


def test_condense_job_route_persists_rag_options(client, seed, monkeypatch):
    async def fake_condense(recap, model="", options=None, think=True, **kwargs):
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json={
        "recap": "text", "use_rag": True, "rag_entity_limit": 12, "rag_notes_limit": 3,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")
    assert data["status"] == "done", data

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.use_rag is True
        assert job.rag_entity_limit == 12
        assert job.rag_notes_limit == 3
    finally:
        db.close()


@pytest.mark.parametrize("body", [
    {"recap": "text", "use_rag": True, "rag_entity_limit": "not a number"},
    {"recap": "text", "use_rag": True, "rag_notes_limit": "not a number"},
    {"recap": "text", "use_rag": True, "rag_entity_limit": -1},
    {"recap": "text", "use_rag": True, "rag_notes_limit": -1},
])
def test_condense_job_rejects_invalid_rag_limits(client, seed, body):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-job", json=body)
    assert r.status_code == 400


def test_audio_job_create_route_persists_rag_options(client, seed, tmp_path):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/api/sessions/ai/audio-jobs",
        files={"file": ("clip.mp3", b"fake audio bytes", "audio/mpeg")},
        data={"use_rag": "true", "rag_entity_limit": "9", "rag_notes_limit": "4"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.use_rag is True
        assert job.rag_entity_limit == 9
        assert job.rag_notes_limit == 4
    finally:
        db.close()


def test_audio_job_create_route_rag_off_by_default(client, seed, tmp_path):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/api/sessions/ai/audio-jobs",
        files={"file": ("clip.mp3", b"fake audio bytes", "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.use_rag is False
        assert job.rag_entity_limit is None
        assert job.rag_notes_limit is None
    finally:
        db.close()


def test_condense_recap_route_passes_min_max_tokens_and_extra_instructions(client, seed, monkeypatch):
    captured = {}

    # strictness needs a default here (not **kwargs) so the test also pins
    # that a body WITHOUT the field forwards "guideline" to condense_recap,
    # not None/something else.
    async def fake_condense(recap, model="", options=None, think=True, extra_instructions="", min_tokens=None, max_tokens=None, strictness="guideline"):
        captured["extra_instructions"] = extra_instructions
        captured["min_tokens"] = min_tokens
        captured["max_tokens"] = max_tokens
        captured["strictness"] = strictness
        return "condensed"
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-recap", json={
        "recap": "text", "min_tokens": 20, "max_tokens": 100, "extra_instructions": "focus on combat",
    })
    assert r.status_code == 200, r.text
    assert captured["extra_instructions"] == "focus on combat"
    assert captured["min_tokens"] == 20
    assert captured["max_tokens"] == 100
    assert captured["strictness"] == "guideline"


def test_condense_recap_route_rejects_min_greater_than_max(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/condense-recap", json={"recap": "text", "min_tokens": 100, "max_tokens": 50})
    assert r.status_code == 400


def test_session_job_list_excludes_attachment_purpose_jobs(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="attachment", status="done", filename="voice.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    listed = client.get("/api/sessions/ai/audio-jobs").json()
    assert all(j["filename"] != "voice.mp3" for j in listed)


# ── AI attachments routes: /api/ai/attachments/audio-jobs* ─────────────────

def test_attachment_job_create_poll_and_list(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/ai/attachments/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["purpose"] == "attachment"
    assert data["transcript"] == "the party met elena at the bazaar"
    assert data["attachment_url"].startswith("/uploads/ai_attachments/")

    r = client.get("/api/ai/attachments/audio-jobs")
    assert any(j["id"] == job_id for j in r.json())


def test_attachment_job_chunked_create(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "b" * 32
    r0 = client.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files={"file": ("part", io.BytesIO(b"first-"), "application/octet-stream")})
    r1 = client.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files={"file": ("part", io.BytesIO(b"second"), "application/octet-stream")})
    assert r0.status_code == 200 and r1.status_code == 200
    r = client.post("/api/ai/attachments/audio-jobs/complete",
                     data={"upload_id": upload_id, "filename": "big.mp3", "total_chunks": "2"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/ai/attachments/audio-jobs/{job_id}")
    assert data["status"] == "done"


def test_attachment_job_rejects_non_audio_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400


def test_attachment_job_player_denied_by_default_then_allowed(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 403

    _set_world(seed.world_a.id, players_can_ask_ai=True)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200


def test_attachment_job_cross_world_isolation(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/ai/attachments/audio-jobs/{job_id}")
    assert r.status_code == 404


def test_attachment_job_list_excludes_session_recap_purpose_jobs(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="recap.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    listed = client.get("/api/ai/attachments/audio-jobs").json()
    assert all(j["filename"] != "recap.mp3" for j in listed)


# ── Unified Background Jobs page + routes: app/routers/audio_jobs.py ───────
# Spans both purposes (unlike the purpose-scoped list routes above) and adds
# cancel — the one thing the smaller inline panels don't offer.

@pytest.fixture
def _hanging_transcribe(monkeypatch):
    """A transcribe_audio that never resolves on its own — only cancellation
    can end it — so a test can reliably catch a job mid-"transcribing"
    instead of racing a fast mock that might finish before the cancel
    request lands."""
    release = asyncio.Event()

    async def hang(path, glossary="", **kwargs):
        await release.wait()
        return "unused"

    monkeypatch.setattr(ai_module, "transcribe_audio", hang)
    return release


def _wait_for_status(client, url, status, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        r = client.get(url)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] == status:
            return data
        time.sleep(0.02)
    raise AssertionError(f"never reached status={status!r}, last seen: {data}")


def test_background_jobs_page_renders_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 200
    assert "Background Jobs" in r.text


def test_background_jobs_page_offers_resume_for_an_interrupted_job(client, seed):
    """The page renders its job cards entirely client-side from
    GET /api/audio-jobs (no server-rendered job list to grep the static
    HTML for), so this checks both halves of the actual contract: the
    static page ships the Resume button/handler gated on job.resumable,
    and the API it fetches from reports resumable=true for precisely an
    "interrupted" job and false for everything else."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    page_html = client.get("/background-jobs").text
    assert "bgResumeJob" in page_html
    assert "job.resumable" in page_html
    assert "▶ Resume" in page_html

    db = SessionLocal()
    try:
        interrupted = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="interrupted",
                               filename="a.mp3", error="Paused by a server restart — the work so far is saved.")
        done = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="done", filename="b.mp3")
        db.add_all([interrupted, done])
        db.commit()
    finally:
        db.close()

    r = client.get("/api/audio-jobs")
    assert r.status_code == 200
    by_filename = {j["filename"]: j for j in r.json()["jobs"]}
    assert by_filename["a.mp3"]["resumable"] is True
    assert by_filename["b.mp3"]["resumable"] is False


def test_background_jobs_page_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/background-jobs")
    assert r.status_code == 403


def test_unified_list_spans_both_purposes(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="recap.mp3"),
            AudioJob(world_id=seed.world_a.id, purpose="attachment", status="done", filename="voice.mp3"),
        ])
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/audio-jobs")
    assert r.status_code == 200
    names = {j["filename"] for j in r.json()["jobs"]}
    assert {"recap.mp3", "voice.mp3"} <= names


def test_unified_status_requires_gm(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 403


def test_unified_list_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        db.add(AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="a-only.mp3"))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/audio-jobs")
    assert r.status_code == 200
    assert all(j["filename"] != "a-only.mp3" for j in r.json()["jobs"])


def test_cancel_stops_an_in_progress_job(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text

    data = None
    deadline = time.time() + 5
    while time.time() < deadline:
        r = client.get(f"/api/audio-jobs/{job_id}")
        data = r.json()
        if data["status"] != "transcribing":
            break
        time.sleep(0.02)
    assert data["status"] == "cancelled", data
    assert "cancel" in data["error"].lower()


def test_cancel_rejects_a_job_thats_not_running(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 400


def test_cancel_requires_gm(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]
    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/cancel")
    assert r.status_code == 403

    # Clean up: the hanging task would otherwise linger past this test.
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/api/audio-jobs/{job_id}/cancel")


# ── _forget_task — the done-callback's registry race + stuck-pending fix ───

class _FakeTask:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled

    def cancelled(self):
        return self._cancelled


def test_forget_task_does_not_evict_a_newer_task_for_the_same_job_id():
    """The race this fixes: a resummarize starts in the brief window
    between the prior task finishing (row already out of
    IN_PROGRESS_STATUSES) and its own done-callback actually running
    (asyncio schedules callbacks via call_soon, not synchronously) — the
    OLD task's callback must not evict the NEW task's registry entry, or
    the new task becomes only weakly-referenced and cancel_job can no
    longer find it."""
    old_task = _FakeTask()
    new_task = _FakeTask()
    audio_jobs._running_tasks[999999] = new_task
    try:
        audio_jobs._forget_task(999999, old_task)
        assert audio_jobs._running_tasks.get(999999) is new_task
    finally:
        audio_jobs._running_tasks.pop(999999, None)


def test_forget_task_removes_its_own_entry():
    task = _FakeTask()
    audio_jobs._running_tasks[999999] = task
    audio_jobs._forget_task(999999, task)
    assert 999999 not in audio_jobs._running_tasks


def test_forget_task_reconciles_a_job_cancelled_before_its_body_ever_ran(client, seed, tmp_path):
    """asyncio.Task.cancel() on a task whose coroutine body hasn't started
    yet skips the body entirely — neither _run_job's own `except
    CancelledError` nor its `finally: audio_path.unlink(...)` ever runs.
    Without this reconciliation the row stays "pending" forever (both
    cancel_job and delete_job refuse a row in IN_PROGRESS_STATUSES) and
    the uploaded audio leaks."""
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="pending", filename="x.mp3",
                        audio_path=str(audio), delete_after=True)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=True)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task)

    assert job_id not in audio_jobs._running_tasks
    assert not audio.exists()
    db = SessionLocal()
    try:
        updated = db.get(AudioJob, job_id)
        assert updated.status == "cancelled"
        assert updated.error == "Cancelled by GM."
        assert updated.finished_at is not None
    finally:
        db.close()


def test_forget_task_keeps_audio_when_delete_after_is_false(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="attachment", status="pending", filename="x.mp3",
                        audio_path=str(audio), delete_after=False)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=True)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task)
    assert audio.exists()


def test_forget_task_does_not_touch_a_job_that_finished_normally(client, seed):
    """A task that ran its body to completion (whether cancelled partway
    through and handled by _run_job's own CancelledError branch, or simply
    finished with done/error) has already moved the row out of
    IN_PROGRESS_STATUSES before this callback fires — the reconciliation
    must be a no-op then, not overwrite whatever real outcome was
    recorded."""
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3", recap="a real recap")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=False)
    audio_jobs._running_tasks[job_id] = task
    audio_jobs._forget_task(job_id, task)

    db = SessionLocal()
    try:
        updated = db.get(AudioJob, job_id)
        assert updated.status == "done"
        assert updated.recap == "a real recap"
    finally:
        db.close()


# ── Per-job model selection + resummarize (retry with a different model) ───
#
# A job's own summarize_transcript() call passes model= (see
# app.audio_jobs._run_job) — the `_fake_ai` fixture above accepts model="" by
# default and ignores it; tests here that need to assert *which* model was
# passed install their own capturing fake instead.

@pytest.mark.asyncio
async def test_create_job_stores_and_uses_chosen_model(client, seed, tmp_path, monkeypatch):
    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap text"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, model="llama3.1",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.model == "llama3.1"
    assert captured["model"] == "llama3.1"


@pytest.mark.asyncio
async def test_create_job_blank_model_stored_as_none(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.model is None


@pytest.mark.asyncio
async def test_resummarize_job_uses_saved_transcript_and_new_model(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done"
    original_transcript = job.transcript

    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        captured["transcript"] = transcript
        captured["model"] = model
        return "A different, better recap."

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    started = audio_jobs.start_resummarize_job(job_id, model="llama3.1")
    assert started.status == "summarizing"
    updated = await _await_terminal(job_id)
    assert updated.status == "done"
    assert updated.recap == "A different, better recap."
    assert updated.model == "llama3.1"
    assert captured["transcript"] == original_transcript
    assert captured["model"] == "llama3.1"


@pytest.mark.asyncio
async def test_resummarize_job_falls_back_to_the_jobs_own_model_when_blank(client, seed, tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, model="gemma4:26b",
    )
    await _await_terminal(job_id)

    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        captured["model"] = model
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)
    audio_jobs.start_resummarize_job(job_id, model="")
    await _await_terminal(job_id)
    assert captured["model"] == "gemma4:26b"


@pytest.mark.asyncio
async def test_resummarize_job_rejects_attachment_purpose(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=False, attachment_url="/uploads/ai_attachments/clip.mp3",
    )
    await _await_terminal(job_id)
    with pytest.raises(ValueError):
        audio_jobs.start_resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_missing_transcript(client, seed, tmp_path, monkeypatch):
    async def empty_transcribe(path, glossary="", **kwargs):
        return ""

    monkeypatch.setattr(ai_module, "transcribe_audio", empty_transcribe)
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    with pytest.raises(ValueError):
        audio_jobs.start_resummarize_job(job_id)


@pytest.mark.asyncio
async def test_resummarize_job_rejects_unknown_job(client, seed):
    with pytest.raises(ValueError):
        audio_jobs.start_resummarize_job(999999)


@pytest.mark.asyncio
async def test_resummarize_job_marks_error_when_summarize_returns_failure_sentinel(client, seed, tmp_path, monkeypatch):
    # summarize_transcript never raises on an Ollama-side failure — it
    # returns a "[AI ...]" sentinel string instead. Without checking for
    # that, a failed re-summarize would land as status="done" with the
    # error text sitting in the recap field.
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "done"

    async def failing_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "[AI error: Ollama 404: model 'gemma4:26b' not found]"

    monkeypatch.setattr(ai_module, "summarize_transcript", failing_summarize)

    audio_jobs.start_resummarize_job(job_id, model="gemma4:26b")
    updated = await _await_terminal(job_id)
    assert updated.status == "error"
    assert updated.error == "[AI error: Ollama 404: model 'gemma4:26b' not found]"


def test_session_job_create_accepts_a_model_field(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")},
                     data={"model": "llama3.1"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    _poll_until_terminal(client, f"/api/sessions/ai/audio-jobs/{job_id}")

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.model == "llama3.1"
    finally:
        db.close()


def test_resummarize_route_gm_only(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 403


def test_resummarize_route_round_trip(client, seed, monkeypatch):
    async def fake_summarize_capture(transcript, model="", extra_instructions="", **kwargs):
        return f"Recap via {model or 'default'}: {transcript}"

    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="error",
                        filename="x.mp3", transcript="hello there", error="[AI error: Ollama 404]")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": "llama3.1"})
    assert r.status_code == 200, r.text
    # The route only starts the job now — see start_resummarize_job's
    # docstring for why running the whole (possibly multi-minute)
    # map-reduce summarize inline used to risk tripping a reverse proxy's
    # own timeout. The caller polls for the actual result.
    assert r.json()["status"] == "summarizing"

    data = _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")
    assert data["status"] == "done"
    assert data["recap"] == "Recap via llama3.1: hello there"
    assert data["model"] == "llama3.1"
    assert data["error"] == ""


def test_resummarize_route_think_defaults_true_and_is_overridable(client, seed, monkeypatch):
    captured = {}

    async def fake_summarize_capture(transcript, model="", extra_instructions="", think=True, **kwargs):
        captured["think"] = think
        return "recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize_capture)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 200, r.text
    _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")
    assert captured["think"] is True

    r2 = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": "", "think": "false"})
    assert r2.status_code == 200, r2.text
    data = _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")
    assert captured["think"] is False
    assert data["think"] is False


def test_resummarize_route_does_not_block_on_a_slow_summarize(client, seed, monkeypatch):
    """Direct regression test for the reported bug: retrying a summary used
    to await the whole (possibly multi-minute) map-reduce summarize inline
    inside this one request, which a reverse proxy's own timeout could trip
    long before Ollama finished (surfaced to the GM as a raw "HTTP 524").
    A slow-but-bounded fake here proves the HTTP response comes back well
    before summarize_transcript finishes, not just eventually."""
    release = asyncio.Event()

    async def slow_summarize(transcript, model="", extra_instructions="", **kwargs):
        await release.wait()
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", slow_summarize)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    try:
        r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "summarizing"
    finally:
        release.set()  # let the background task finish so nothing lingers past this test
    _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")


def test_resummarize_route_rejects_a_job_already_in_progress(client, seed, monkeypatch):
    release = asyncio.Event()

    async def slow_summarize(transcript, model="", extra_instructions="", **kwargs):
        await release.wait()
        return "recap"

    monkeypatch.setattr(ai_module, "summarize_transcript", slow_summarize)

    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    try:
        r1 = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r1.status_code == 200, r1.text
        r2 = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
        assert r2.status_code == 400
    finally:
        release.set()
    _poll_until_terminal(client, f"/api/audio-jobs/{job_id}")


def test_resummarize_route_rejects_job_with_no_transcript_yet(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="pending", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 400


def test_delete_removes_a_finished_job(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert audio_jobs.delete_job(job_id) is True
    db = SessionLocal()
    try:
        assert db.get(AudioJob, job_id) is None
    finally:
        db.close()


def test_delete_refuses_an_in_progress_job(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert audio_jobs.delete_job(job_id) is False
    db = SessionLocal()
    try:
        assert db.get(AudioJob, job_id) is not None
    finally:
        db.close()


def test_delete_returns_false_for_unknown_job():
    assert audio_jobs.delete_job(999999) is False


def test_delete_route_round_trip(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200, r.text
    r2 = client.get(f"/api/audio-jobs/{job_id}")
    assert r2.status_code == 404


def test_delete_route_rejects_in_progress_job(client, seed, _hanging_transcribe):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/sessions/ai/audio-jobs",
                     files={"file": ("clip.mp3", io.BytesIO(b"fake"), "audio/mpeg")})
    job_id = r.json()["job_id"]
    _wait_for_status(client, f"/api/audio-jobs/{job_id}", "transcribing")

    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 400

    client.post(f"/api/audio-jobs/{job_id}/cancel")


def test_delete_route_requires_gm(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 403


def test_delete_route_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_b.id, purpose="session_recap", status="done", filename="x.mp3")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 404


# ── run_started_at / finished_at (Background Jobs' "took Xm Ys" display) ───
#
# run_started_at marks the start of the CURRENT run (reset on every
# resummarize, unlike created_at which never changes) so a job resummarized
# days after its first run doesn't report a multi-day "duration" for what
# was mostly idle time between runs. finished_at is set once that run
# reaches a terminal status.

@pytest.mark.asyncio
async def test_run_timing_set_on_successful_completion(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    before = datetime.utcnow()
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    after = datetime.utcnow()
    assert job.run_started_at is not None
    assert job.finished_at is not None
    assert before <= job.run_started_at <= job.finished_at <= after


@pytest.mark.asyncio
async def test_run_timing_set_on_error_paths(client, seed, tmp_path, monkeypatch):
    async def raising_transcribe(path, glossary="", **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(ai_module, "transcribe_audio", raising_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.run_started_at is not None
    assert job.finished_at is not None
    assert job.finished_at >= job.run_started_at


@pytest.mark.asyncio
async def test_resummarize_resets_run_started_at_and_keeps_created_at(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    first = await _await_terminal(job_id)
    original_created_at = first.created_at
    first_run_started_at = first.run_started_at
    first_finished_at = first.finished_at

    audio_jobs.start_resummarize_job(job_id)
    second = await _await_terminal(job_id)
    assert second.created_at == original_created_at  # original creation time never changes
    assert second.run_started_at is not None
    assert second.run_started_at >= first_finished_at  # a fresh run, not the original start
    assert second.finished_at is not None
    assert second.finished_at >= second.run_started_at


def test_sweep_interrupted_jobs_sets_finished_at(client, seed):
    db = SessionLocal()
    try:
        stuck = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribing", filename="x.mp3")
        db.add(stuck)
        db.commit()
        db.refresh(stuck)
        stuck_id = stuck.id
    finally:
        db.close()

    audio_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        s = db.get(AudioJob, stuck_id)
        assert s.status == "interrupted"
        assert s.finished_at is not None
    finally:
        db.close()


def test_unified_status_route_exposes_timing_and_instructions_fields(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="session_recap", status="done", filename="x.mp3",
            extra_instructions="Focus on combat",
            run_started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["extra_instructions"] == "Focus on combat"
    assert data["run_started_at"] is not None
    assert data["finished_at"] is not None


# ── Per-job extra_instructions on create_job ────────────────────────────────

@pytest.mark.asyncio
async def test_create_job_stores_extra_instructions(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True, extra_instructions="Focus on combat",
    )
    job = await _await_terminal(job_id)
    assert job.extra_instructions == "Focus on combat"


@pytest.mark.asyncio
async def test_create_job_blank_extra_instructions_stored_as_none(client, seed, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio bytes")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3",
        audio_path=audio, delete_after=True,
    )
    job = await _await_terminal(job_id)
    assert job.extra_instructions is None


def test_resummarize_route_cross_world_isolation(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_b.id, purpose="session_recap", status="done",
                        filename="x.mp3", transcript="hello there")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={"model": ""})
    assert r.status_code == 404


# ── Download transcript/recap as .md ────────────────────────────────────────

def _make_done_job(world_id, filename="session1.mp3", transcript="", recap=""):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=world_id, purpose="session_recap", status="done",
                        filename=filename, transcript=transcript, recap=recap)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_download_transcript_md(client, seed):
    job_id = _make_done_job(seed.world_a.id, transcript="Raw transcript text here.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}/transcript.md")
    assert r.status_code == 200
    assert r.text == "Raw transcript text here."
    assert r.headers["content-type"].startswith("text/markdown")
    assert 'filename="session1-transcript.md"' in r.headers["content-disposition"]


def test_download_recap_md(client, seed):
    job_id = _make_done_job(seed.world_a.id, recap="Polished recap text.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}/recap.md")
    assert r.status_code == 200
    assert r.text == "Polished recap text."
    assert 'filename="session1-recap.md"' in r.headers["content-disposition"]


def test_download_transcript_md_404_when_empty(client, seed):
    job_id = _make_done_job(seed.world_a.id, transcript="")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}/transcript.md")
    assert r.status_code == 404


def test_download_recap_md_404_when_empty(client, seed):
    job_id = _make_done_job(seed.world_a.id, recap="")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}/recap.md")
    assert r.status_code == 404


def test_download_md_player_forbidden(client, seed):
    job_id = _make_done_job(seed.world_a.id, transcript="secret", recap="secret")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/audio-jobs/{job_id}/transcript.md").status_code == 403
    assert client.get(f"/api/audio-jobs/{job_id}/recap.md").status_code == 403


def test_download_md_cross_world_isolation(client, seed):
    job_id = _make_done_job(seed.world_b.id, transcript="text", recap="text")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/api/audio-jobs/{job_id}/transcript.md").status_code == 404
    assert client.get(f"/api/audio-jobs/{job_id}/recap.md").status_code == 404


def test_download_md_sanitizes_filename_and_falls_back_without_original_name(client, seed):
    job_id = _make_done_job(seed.world_a.id, filename="../weird name! ??.mp3", transcript="hi")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/audio-jobs/{job_id}/transcript.md")
    assert r.status_code == 200
    disposition = r.headers["content-disposition"]
    assert ".." not in disposition and "!" not in disposition and "?" not in disposition

    no_name_job_id = _make_done_job(seed.world_a.id, filename="", transcript="hi")
    r2 = client.get(f"/api/audio-jobs/{no_name_job_id}/transcript.md")
    assert r2.status_code == 200
    assert f'filename="job-{no_name_job_id}-transcript.md"' in r2.headers["content-disposition"]


# ── purpose="facts_parse" (create_facts_parse_job) ──────────────────────────
# The Facts page's recap→facts parse as a durable background job (see
# create_facts_parse_job's docstring): the synchronous POST /api/facts/parse
# used to hold one HTTP request open for the whole model call, tripping
# Cloudflare Tunnel's ~100s timeout (HTTP 524) on long recaps and losing
# everything. The result lands in result_json (NOT recap — a facts draft is
# review-UI data ({content, visible_to_players} dicts), not displayable
# recap prose the jobs UI would render as such).

_FACTS_PARSE_DRAFT = [
    {"content": "The party visited the tavern.", "visible_to_players": True},
    {"content": "Elyra is secretly a cult agent.", "visible_to_players": False},
]


@pytest.mark.asyncio
async def test_create_facts_parse_job_runs_to_completion(client, seed, monkeypatch):
    async def fake_parse(raw_text, model=""):
        assert "tavern" in raw_text
        return [dict(f) for f in _FACTS_PARSE_DRAFT]
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    job_id = audio_jobs.create_facts_parse_job(world_id=seed.world_a.id, text="went to the tavern, met Elyra")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.purpose == "facts_parse"
    assert job.filename == "Facts"
    assert job.transcript == "went to the tavern, met Elyra"  # the input, kept so the draft stays attributable
    assert json.loads(job.result_json) == _FACTS_PARSE_DRAFT
    assert job.recap == ""  # a facts draft never masquerades as a recap


@pytest.mark.asyncio
async def test_create_facts_parse_job_empty_result_is_done_not_error(client, seed, monkeypatch):
    """Out-of-character chatter parses to zero facts — that's a SUCCESS the
    Facts page's UI explains, not a failure: the model did its job."""
    async def fake_parse(raw_text, model=""):
        return []
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    job_id = audio_jobs.create_facts_parse_job(world_id=seed.world_a.id, text="banter with no events in it")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.result_json == "[]"
    assert job.error == ""


@pytest.mark.asyncio
async def test_create_facts_parse_job_valueerror_marks_error(client, seed, monkeypatch):
    """parse_facts_from_recap raises ValueError for every failure (Ollama
    down, malformed JSON — see its docstring), already worded for a GM; the
    job must carry that message in job.error, the same text the synchronous
    /api/facts/parse maps to HTTP 502."""
    async def failing_parse(raw_text, model=""):
        raise ValueError("Could not parse facts from that recap — try rephrasing it.")
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", failing_parse)

    job_id = audio_jobs.create_facts_parse_job(world_id=seed.world_a.id, text="gibberish")
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert job.error == "Could not parse facts from that recap — try rephrasing it."


@pytest.mark.asyncio
async def test_create_facts_parse_job_never_transcribes(client, seed, monkeypatch):
    """There is no audio — if _run_job's dispatch ever fell through to the
    transcribe branch, this fails loudly instead of silently succeeding."""
    async def fail_if_called(*a, **kw):
        raise AssertionError("facts_parse jobs must never transcribe")
    monkeypatch.setattr(ai_module, "transcribe_audio", fail_if_called)
    async def fake_parse(raw_text, model=""):
        return []
    monkeypatch.setattr(ai_module, "parse_facts_from_recap", fake_parse)

    job_id = audio_jobs.create_facts_parse_job(world_id=seed.world_a.id, text="some recap")
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.audio_path == ""


def test_create_facts_parse_job_rejects_blank_text(seed):
    """Checked synchronously at creation — before create_task — so the route
    maps it to a clean 400 rather than a job erroring mid-run (same shape as
    test_create_condense_job_rejects_invalid_strictness)."""
    with pytest.raises(ValueError):
        audio_jobs.create_facts_parse_job(world_id=seed.world_a.id, text="   ")


def test_facts_parse_job_serializer_exposes_result_json_and_facts_label(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(
            world_id=seed.world_a.id, purpose="facts_parse", filename="Facts",
            status="done", transcript="the input", result_json=json.dumps(_FACTS_PARSE_DRAFT),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    data = client.get(f"/api/audio-jobs/{job_id}").json()
    assert json.loads(data["result_json"]) == _FACTS_PARSE_DRAFT
    assert data["purpose"] == "facts_parse"
    assert data["purpose_label"] == "Facts"  # chip, not the raw purpose string


# ── GET /api/audio-jobs filters (purpose / status / game_session_id) ────────
# The Background Jobs page's dropdowns and the session page's scoped panel
# both filter server-side — client-side filtering over page 1 of a paginated
# list would silently hide rows living on page 2.

def _login_gm_audio_jobs(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_unified_list_filters_by_purpose(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="recap.mp3"),
            AudioJob(world_id=seed.world_a.id, purpose="condense", status="done", filename="condense-1"),
            AudioJob(world_id=seed.world_a.id, purpose="facts_parse", status="done", filename="facts-1"),
            AudioJob(world_id=seed.world_a.id, purpose="facts_parse", status="error", filename="facts-2"),
        ])
        db.commit()
    finally:
        db.close()
    _login_gm_audio_jobs(client, seed)

    jobs = client.get("/api/audio-jobs?purpose=facts_parse").json()["jobs"]
    assert {j["filename"] for j in jobs} == {"facts-1", "facts-2"}
    assert all(j["purpose"] == "facts_parse" for j in jobs)

    # No filter → everything (the pre-existing behavior, unchanged).
    names = {j["filename"] for j in client.get("/api/audio-jobs").json()["jobs"]}
    assert {"recap.mp3", "condense-1", "facts-1", "facts-2"} <= names


def test_unified_list_filters_by_status(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done", filename="done-1"),
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="error", filename="err-1"),
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="summarizing", filename="run-1"),
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="interrupted", filename="int-1"),
        ])
        db.commit()
    finally:
        db.close()
    _login_gm_audio_jobs(client, seed)

    # "running" is the UI's word for every in-progress phase, not just one.
    running = {j["filename"] for j in client.get("/api/audio-jobs?status=running").json()["jobs"]}
    assert "run-1" in running
    assert "done-1" not in running and "err-1" not in running and "int-1" not in running

    done = {j["filename"] for j in client.get("/api/audio-jobs?status=done").json()["jobs"]}
    assert "done-1" in done and "run-1" not in done

    errs = {j["filename"] for j in client.get("/api/audio-jobs?status=error").json()["jobs"]}
    assert "err-1" in errs and "done-1" not in errs

    interrupted = {j["filename"] for j in client.get("/api/audio-jobs?status=interrupted").json()["jobs"]}
    assert "int-1" in interrupted and "run-1" not in interrupted


def test_unified_list_filters_by_game_session(client, seed):
    from app.models import GameSession as _GameSession

    db = SessionLocal()
    try:
        gs1 = _GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        gs2 = _GameSession(world_id=seed.world_a.id, title="Session 2", session_num=2)
        db.add_all([gs1, gs2])
        db.commit()
        db.refresh(gs1)
        db.refresh(gs2)
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                     filename="for-1", game_session_id=gs1.id),
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                     filename="for-2", game_session_id=gs2.id),
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                     filename="no-session", game_session_id=None),
        ])
        db.commit()
        s1 = gs1.id
    finally:
        db.close()
    _login_gm_audio_jobs(client, seed)

    jobs = client.get(f"/api/audio-jobs?game_session_id={s1}").json()["jobs"]
    assert {j["filename"] for j in jobs} == {"for-1"}


def test_unified_list_filters_combine(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="facts_parse", status="done", filename="facts-done"),
            AudioJob(world_id=seed.world_a.id, purpose="facts_parse", status="error", filename="facts-err"),
            AudioJob(world_id=seed.world_a.id, purpose="condense", status="done", filename="condense-done"),
        ])
        db.commit()
    finally:
        db.close()
    _login_gm_audio_jobs(client, seed)
    jobs = client.get("/api/audio-jobs?purpose=facts_parse&status=error").json()["jobs"]
    assert {j["filename"] for j in jobs} == {"facts-err"}


def test_session_job_list_filters_by_game_session(client, seed):
    """The Sessions page's inline panel scopes to its own session via this
    param — a GM on session #1 shouldn't see session #2's jobs there."""
    from app.models import GameSession as _GameSession

    db = SessionLocal()
    try:
        gs1 = _GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        gs2 = _GameSession(world_id=seed.world_a.id, title="Session 2", session_num=2)
        db.add_all([gs1, gs2])
        db.commit()
        db.refresh(gs1)
        db.refresh(gs2)
        db.add_all([
            AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="done",
                     filename="mine", game_session_id=gs1.id),
            AudioJob(world_id=seed.world_a.id, purpose="condense", status="done",
                     filename="theirs", game_session_id=gs2.id),
            AudioJob(world_id=seed.world_a.id, purpose="condense", status="done",
                     filename="unattributed", game_session_id=None),
        ])
        db.commit()
        s1 = gs1.id
    finally:
        db.close()
    _login_gm_audio_jobs(client, seed)
    listed = client.get(f"/api/sessions/ai/audio-jobs?game_session_id={s1}").json()
    assert {j["filename"] for j in listed} == {"mine"}
    # Unfiltered keeps the pre-existing whole-world behavior.
    all_names = {j["filename"] for j in client.get("/api/sessions/ai/audio-jobs").json()}
    assert {"mine", "theirs", "unattributed"} <= all_names


def test_session_page_condense_and_recap_jobs_carry_game_session_id(client, seed, monkeypatch):
    """"Made for this session" only works if the session page's own job-
    creating routes persist game_session_id — the condense route reads it
    from the JSON body, the live-transcript route from the URL. If either
    ever stops passing it, the panel's session filter would silently hide
    the very job the GM just started."""
    from app.models import GameSession as _GameSession

    async def fake_condense(*a, **kw):
        return "condensed"  # only the row's fields matter here, not the result
    monkeypatch.setattr(ai_module, "condense_recap", fake_condense)
    async def fake_summarize(transcript, model="", extra_instructions="", think=True, **kw):
        return "a recap"
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)

    db = SessionLocal()
    try:
        gs = _GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1,
                          live_transcript="hours of live transcript")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    _login_gm_audio_jobs(client, seed)
    r = client.post("/api/sessions/ai/condense-job", json={"recap": "a recap", "game_session_id": gs_id})
    assert r.status_code == 200, r.text
    condense_job_id = r.json()["job_id"]

    r2 = client.post(f"/api/sessions/{gs_id}/ai/summarize-live-transcript-job", json={})
    assert r2.status_code == 200, r2.text
    recap_job_id = r2.json()["job_id"]

    db = SessionLocal()
    try:
        assert db.get(AudioJob, condense_job_id).game_session_id == gs_id
        assert db.get(AudioJob, recap_job_id).game_session_id == gs_id
    finally:
        db.close()


def test_background_jobs_page_has_purpose_and_status_filters(client, seed):
    _login_gm_audio_jobs(client, seed)
    html = client.get("/background-jobs").text
    assert 'id="bg-filter-purpose"' in html
    assert 'id="bg-filter-status"' in html
    assert '<option value="facts_parse">Facts</option>' in html
    assert '<option value="session_recap">Session recap</option>' in html
    assert '<option value="condense">Condense</option>' in html
    assert '<option value="attachment">Attachment</option>' in html
    # Filters re-fetch server-side (keeping pagination honest), not client-side.
    assert "audioParams.join('&')" in html
    assert "encodeURIComponent(fPurpose)" in html
    # Rows link to the owning session when the job has one.
    assert "'/sessions/' + job.game_session_id" in html
    assert "\u2192 Session" in html


def test_session_page_scopes_jobs_panel_and_links_all_jobs(client, seed):
    """The session page's inline panel polls the session-scoped list (its
    listUrl carries game_session_id) instead of every job in the world, and
    the "All jobs →" link is the advertised path to the full list."""
    from app.models import GameSession as _GameSession

    db = SessionLocal()
    try:
        gs = _GameSession(world_id=seed.world_a.id, title="Session 1", session_num=1)
        db.add(gs)
        db.commit()
        db.refresh(gs)
        gs_id = gs.id
    finally:
        db.close()

    _login_gm_audio_jobs(client, seed)
    html = client.get(f"/sessions/{gs_id}").text
    assert f"game_session_id={gs_id}" in html  # the scoped listUrl
    assert "allJobsUrl: '/background-jobs'" in html
    # The shared panel source renders the link from that opt.
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "audio-jobs.js").read_text()
    assert "opts.allJobsUrl" in js
    assert '"All jobs →"' in js
