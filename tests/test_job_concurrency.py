"""Tests for the per-backend concurrency limits on background job work
(app.ai.whisper_job_semaphore / ollama_job_semaphore, WAVE 2 / plan item
AI 1.3). Without these, two session-recap jobs queued together interleave
Whisper chunks (or Ollama calls) against each other on the same backend,
roughly doubling wall time for both and thrashing whatever's resident in
VRAM. The semaphores are shared, process-wide, module-level state in
app/ai.py — audio_jobs.py acquires whisper_job_semaphore around its whole
transcribe_audio call and ollama_job_semaphore around its whole condense_
recap/summarize_transcript call; chat_jobs.py acquires the same ollama_job_
semaphore around its whole generate_chat call — so a queued chat job and a
queued recap job serialize against each other too, not just two jobs of
the same kind.

These tests default WHISPER_JOB_CONCURRENCY/OLLAMA_JOB_CONCURRENCY to 1
(same as production) and prove two jobs' calls never overlap in time; a
peak-concurrency counter (not just start/end ordering) makes this robust
against scheduling order."""
import asyncio
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app import chat_jobs
from app import image_jobs
from app.database import SessionLocal
from app.models import AudioJob, ChatJob, ImageJob

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _login_gm_in(client, seed, world):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", world.slug)


async def _await_audio_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError(f"audio job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


async def _await_chat_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(ChatJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError(f"chat job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


async def _await_image_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(ImageJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError(f"image job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


class _ConcurrencyTracker:
    """Records the peak number of simultaneously-in-flight calls."""
    def __init__(self):
        self.current = 0
        self.peak = 0

    async def enter(self, hold_seconds=0.05):
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(hold_seconds)

    def exit(self):
        self.current -= 1


def test_semaphores_default_to_one_permit():
    assert ai_module.WHISPER_JOB_CONCURRENCY == 1
    assert ai_module.OLLAMA_JOB_CONCURRENCY == 1


@pytest.mark.asyncio
async def test_two_audio_jobs_never_transcribe_concurrently(client, seed, tmp_path, monkeypatch):
    tracker = _ConcurrencyTracker()

    async def slow_transcribe(path, glossary="", **kwargs):
        await tracker.enter()
        tracker.exit()
        return "a transcript"
    async def fast_summarize(transcript, model="", extra_instructions="", **kwargs):
        return "a recap"
    monkeypatch.setattr(ai_module, "transcribe_audio", slow_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fast_summarize)

    audio1 = tmp_path / "a.mp3"
    audio1.write_bytes(b"a")
    audio2 = tmp_path / "b.mp3"
    audio2.write_bytes(b"b")
    job_id_1 = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap",
                                      filename="a.mp3", audio_path=audio1, delete_after=True)
    job_id_2 = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap",
                                      filename="b.mp3", audio_path=audio2, delete_after=True)

    job1 = await _await_audio_terminal(job_id_1)
    job2 = await _await_audio_terminal(job_id_2)
    assert job1.status == "done", job1.error
    assert job2.status == "done", job2.error
    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_two_audio_jobs_never_summarize_concurrently(client, seed, tmp_path, monkeypatch):
    tracker = _ConcurrencyTracker()

    async def fast_transcribe(path, glossary="", **kwargs):
        return "a transcript"
    async def slow_summarize(transcript, model="", extra_instructions="", **kwargs):
        await tracker.enter()
        tracker.exit()
        return "a recap"
    monkeypatch.setattr(ai_module, "transcribe_audio", fast_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", slow_summarize)

    audio1 = tmp_path / "a.mp3"
    audio1.write_bytes(b"a")
    audio2 = tmp_path / "b.mp3"
    audio2.write_bytes(b"b")
    job_id_1 = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap",
                                      filename="a.mp3", audio_path=audio1, delete_after=True)
    job_id_2 = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap",
                                      filename="b.mp3", audio_path=audio2, delete_after=True)

    job1 = await _await_audio_terminal(job_id_1)
    job2 = await _await_audio_terminal(job_id_2)
    assert job1.status == "done", job1.error
    assert job2.status == "done", job2.error
    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_two_chat_jobs_never_generate_concurrently(client, seed, monkeypatch):
    tracker = _ConcurrencyTracker()

    async def slow_generate(messages, system="", model="", options=None):
        await tracker.enter()
        tracker.exit()
        return "a reply"
    monkeypatch.setattr(ai_module, "generate_chat", slow_generate)

    msgs = [{"role": "user", "content": "hi"}]
    job_id_1 = chat_jobs.create_job(world_id=seed.world_a.id, messages=msgs, system="", model="", options=None)
    job_id_2 = chat_jobs.create_job(world_id=seed.world_a.id, messages=msgs, system="", model="", options=None)

    job1 = await _await_chat_terminal(job_id_1)
    job2 = await _await_chat_terminal(job_id_2)
    assert job1.status == "done", job1.error
    assert job2.status == "done", job2.error
    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_two_image_jobs_never_generate_concurrently(client, seed, monkeypatch):
    """app.ai.imagegen_job_semaphore — added alongside the SwarmUI/ComfyUI
    error-surfacing fixes so a queued image job can't race a concurrent
    direct-generate call (or another queued job) at the httpx-client/
    timeout layer, same reasoning as whisper_job_semaphore/
    ollama_job_semaphore above."""
    tracker = _ConcurrencyTracker()

    async def slow_imagegen_generate(**kwargs):
        await tracker.enter()
        tracker.exit()
        return ["/uploads/ai-images/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", slow_imagegen_generate)

    params = {"prompt": "a neon dragon", "uploads_dir": "/tmp"}
    job_id_1 = image_jobs.create_job(world_id=seed.world_a.id, prompt="a neon dragon", params=params)
    job_id_2 = image_jobs.create_job(world_id=seed.world_a.id, prompt="a neon dragon", params=params)

    job1 = await _await_image_terminal(job_id_1)
    job2 = await _await_image_terminal(job_id_2)
    assert job1.status == "done", job1.error
    assert job2.status == "done", job2.error
    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_chat_job_and_condense_job_share_the_ollama_semaphore(client, seed, tmp_path, monkeypatch):
    """The Ollama semaphore is shared across job TYPES, not one per module —
    a queued chat job and a queued condense job must serialize against each
    other too, since they'd otherwise hit the same Ollama backend at once."""
    tracker = _ConcurrencyTracker()

    async def slow_generate(messages, system="", model="", options=None):
        await tracker.enter()
        tracker.exit()
        return "a reply"
    async def slow_condense(recap, model="", options=None, think=True, **kwargs):
        await tracker.enter()
        tracker.exit()
        return "condensed"
    monkeypatch.setattr(ai_module, "generate_chat", slow_generate)
    monkeypatch.setattr(ai_module, "condense_recap", slow_condense)

    chat_job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options=None,
    )
    condense_job_id = audio_jobs.create_condense_job(world_id=seed.world_a.id, text="a long recap")

    chat_job = await _await_chat_terminal(chat_job_id)
    condense_job = await _await_audio_terminal(condense_job_id)
    assert chat_job.status == "done", chat_job.error
    assert condense_job.status == "done", condense_job.error
    assert tracker.peak == 1
