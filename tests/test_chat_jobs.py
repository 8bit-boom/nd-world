"""Tests for background chat-completion jobs — app/chat_jobs.py (the job
engine, mirroring app/audio_jobs.py's/app/image_jobs.py's shape) and the
POST/GET/DELETE /api/ai/chat/jobs* routes (app/routers/ai.py), an opt-in
"process in background" alternative to the live-streamed /api/ai/stream
route for a generation slow enough that keeping the tab open isn't
practical.
"""
import asyncio
import time

import pytest

from app import ai as ai_module
from app import chat_jobs
from app.database import SessionLocal
from app.models import ChatJob

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(ChatJob, job_id)
            if job.status in ("done", "error", "cancelled"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen status={job.status!r}")
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


# ── app/chat_jobs.py engine, exercised directly ─────────────────────────────

@pytest.mark.asyncio
async def test_create_job_runs_to_completion(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "a reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options={},
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.result == "a reply"
    assert job.prompt == "hi"


@pytest.mark.asyncio
async def test_create_job_treats_ai_error_sentinel_as_a_failure(client, seed, monkeypatch):
    """generate_chat() never raises — a failure comes back as a wrapped
    "[AI error: ...]" string instead. A background job with no exception to
    catch would otherwise show "done" with that string as its "result"."""
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "[AI error: Ollama 404: model 'x' not found]"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options={},
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "404" in job.error


@pytest.mark.asyncio
async def test_create_job_treats_empty_response_sentinel_as_a_failure(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "[empty response from x (done_reason=length) — try a different model]"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options={},
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"


@pytest.mark.asyncio
async def test_create_job_records_raised_exception_as_failure(client, seed, monkeypatch):
    async def failing_generate_chat(messages, system="", model="", options=None):
        raise RuntimeError("Cannot reach Ollama")
    monkeypatch.setattr(ai_module, "generate_chat", failing_generate_chat)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options={},
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "Cannot reach Ollama" in job.error


@pytest.mark.asyncio
async def test_create_job_prompt_uses_last_user_message(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id,
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "got it"},
            {"role": "user", "content": "second"},
        ],
        system="", model="", options={},
    )
    job = await _await_terminal(job_id)
    assert job.prompt == "second"


@pytest.mark.asyncio
async def test_cancel_stops_an_in_progress_job(client, seed, monkeypatch):
    async def hang(messages, system="", model="", options=None):
        await asyncio.sleep(30)
        return "unused"
    monkeypatch.setattr(ai_module, "generate_chat", hang)

    job_id = chat_jobs.create_job(
        world_id=seed.world_a.id, messages=[{"role": "user", "content": "hi"}],
        system="", model="", options={},
    )
    await asyncio.sleep(0.05)
    assert chat_jobs.cancel_job(job_id) is True
    job = await _await_terminal(job_id)
    assert job.status == "cancelled"


def test_cancel_returns_false_for_unknown_job():
    assert chat_jobs.cancel_job(999999) is False


def test_delete_removes_a_finished_job(client, seed):
    db = SessionLocal()
    try:
        job = ChatJob(world_id=seed.world_a.id, prompt="hi", status="done", result="reply")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert chat_jobs.delete_job(job_id) is True
    db = SessionLocal()
    try:
        assert db.get(ChatJob, job_id) is None
    finally:
        db.close()


def test_delete_refuses_an_in_progress_job(client, seed):
    db = SessionLocal()
    try:
        job = ChatJob(world_id=seed.world_a.id, prompt="hi", status="generating")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert chat_jobs.delete_job(job_id) is False
    db = SessionLocal()
    try:
        assert db.get(ChatJob, job_id) is not None
    finally:
        db.close()


def test_delete_returns_false_for_unknown_job():
    assert chat_jobs.delete_job(999999) is False


def test_sweep_interrupted_jobs_marks_in_progress_as_error(client, seed):
    db = SessionLocal()
    try:
        job = ChatJob(world_id=seed.world_a.id, prompt="hi", status="generating")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    chat_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        job = db.get(ChatJob, job_id)
        assert job.status == "error"
        assert "restart" in job.error.lower()
    finally:
        db.close()


# ── POST/GET/DELETE /api/ai/chat/jobs* routes ───────────────────────────────

def _body(**overrides):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


def test_job_create_and_poll(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "a reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/jobs", json=_body())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/ai/chat/jobs/{job_id}")
    assert data["status"] == "done"
    assert data["result"] == "a reply"
    assert data["prompt"] == "hi"


def test_job_create_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/chat/jobs", json=_body())
    assert r.status_code == 403


def test_job_list_scoped_to_active_world(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/chat/jobs", json=_body())

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/chat/jobs")
    assert r.json() == []


def test_job_status_404s_across_worlds(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/chat/jobs", json=_body()).json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/ai/chat/jobs/{job_id}")
    assert r.status_code == 404


def test_job_cancel(client, seed, monkeypatch):
    async def hang(messages, system="", model="", options=None):
        await asyncio.sleep(30)
        return "unused"
    monkeypatch.setattr(ai_module, "generate_chat", hang)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/chat/jobs", json=_body()).json()["job_id"]

    r = client.post(f"/api/ai/chat/jobs/{job_id}/cancel")
    assert r.status_code == 200


def test_job_delete(client, seed, monkeypatch):
    async def fake_generate_chat(messages, system="", model="", options=None):
        return "reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/chat/jobs", json=_body()).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/chat/jobs/{job_id}")

    r = client.delete(f"/api/ai/chat/jobs/{job_id}")
    assert r.status_code == 200, r.text
    r2 = client.get(f"/api/ai/chat/jobs/{job_id}")
    assert r2.status_code == 404


def test_job_delete_rejects_in_progress_job(client, seed, monkeypatch):
    async def hang(messages, system="", model="", options=None):
        await asyncio.sleep(30)
        return "unused"
    monkeypatch.setattr(ai_module, "generate_chat", hang)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/chat/jobs", json=_body()).json()["job_id"]

    r = client.delete(f"/api/ai/chat/jobs/{job_id}")
    assert r.status_code == 400

    client.post(f"/api/ai/chat/jobs/{job_id}/cancel")


def test_job_delete_requires_gm(client, seed):
    db = SessionLocal()
    try:
        job = ChatJob(world_id=seed.world_a.id, prompt="hi", status="done", result="reply")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/ai/chat/jobs/{job_id}")
    assert r.status_code == 403


def test_job_delete_404s_across_worlds(client, seed):
    db = SessionLocal()
    try:
        job = ChatJob(world_id=seed.world_b.id, prompt="hi", status="done", result="reply")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/ai/chat/jobs/{job_id}")
    assert r.status_code == 404


def test_job_create_passes_messages_system_and_model_through(client, seed, monkeypatch):
    captured = {}

    async def fake_generate_chat(messages, system="", model="", options=None):
        captured["messages"] = messages
        captured["system"] = system
        captured["model"] = model
        return "reply"
    monkeypatch.setattr(ai_module, "generate_chat", fake_generate_chat)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/chat/jobs", json=_body(system="Be terse.", model="llama3.1")).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/chat/jobs/{job_id}")

    assert captured["system"] == "Be terse."
    assert captured["model"] == "llama3.1"
    assert captured["messages"][-1]["content"] == "hi"
