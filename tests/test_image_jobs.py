"""Tests for background image generation jobs — app/image_jobs.py (the
job engine, mirroring app/audio_jobs.py's shape) and the
POST/GET /api/ai/imagegen/jobs* routes (app/routers/ai.py), an opt-in
"process in background" alternative to the direct /imagegen/generate route
for a slow generation (large batch, hires-fix, big upscale).
"""
import asyncio
import json
import time

import pytest

from app import ai as ai_module
from app import image_jobs
from app import job_shutdown
from app.database import SessionLocal
from app.models import ImageJob

from .conftest import GM_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    """app.ai persists per-surface defaults to a JSON file next to the DB,
    not the DB itself — point it at a throwaway path per test so tests
    can't see each other's saved defaults."""
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(ImageJob, job_id)
            if job.status in ("done", "error", "cancelled", "interrupted"):
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


# ── app/image_jobs.py engine, exercised directly ────────────────────────────

@pytest.mark.asyncio
async def test_create_job_runs_to_completion(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return ["/uploads/ai-images/x.png", "/uploads/ai-images/y.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    job_id = image_jobs.create_job(
        world_id=seed.world_a.id, prompt="a neon dragon",
        params={"prompt": "a neon dragon", "uploads_dir": "/tmp"},
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert json.loads(job.result_urls_json) == ["/uploads/ai-images/x.png", "/uploads/ai-images/y.png"]


@pytest.mark.asyncio
async def test_create_job_records_failure(client, seed, monkeypatch):
    async def failing_generate(**kwargs):
        raise RuntimeError("Cannot reach SwarmUI")
    monkeypatch.setattr(ai_module, "imagegen_generate", failing_generate)

    job_id = image_jobs.create_job(
        world_id=seed.world_a.id, prompt="x", params={"prompt": "x", "uploads_dir": "/tmp"},
    )
    job = await _await_terminal(job_id)
    assert job.status == "error"
    assert "SwarmUI" in job.error


@pytest.mark.asyncio
async def test_cancel_stops_an_in_progress_job(client, seed, monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", hang)

    job_id = image_jobs.create_job(
        world_id=seed.world_a.id, prompt="x", params={"prompt": "x", "uploads_dir": "/tmp"},
    )
    await asyncio.sleep(0.05)
    assert image_jobs.cancel_job(job_id) is True
    job = await _await_terminal(job_id)
    assert job.status == "cancelled"


def test_cancel_returns_false_for_unknown_job():
    assert image_jobs.cancel_job(999999) is False


def test_delete_removes_a_finished_job(client, seed):
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="x", status="done", result_urls_json="[]")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert image_jobs.delete_job(job_id) is True
    db = SessionLocal()
    try:
        assert db.get(ImageJob, job_id) is None
    finally:
        db.close()


def test_delete_refuses_an_in_progress_job(client, seed):
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="x", status="generating")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert image_jobs.delete_job(job_id) is False
    db = SessionLocal()
    try:
        assert db.get(ImageJob, job_id) is not None
    finally:
        db.close()


def test_delete_returns_false_for_unknown_job():
    assert image_jobs.delete_job(999999) is False


def test_sweep_interrupted_jobs_marks_in_progress_as_interrupted(client, seed):
    """A job still mid-flight at boot means the process died UNCLEANLY (a
    crash/OOM/SIGKILL — job_shutdown's own drain()/mark_stragglers_interrupted
    already handle a clean shutdown). It's marked "interrupted", not
    "error", so resume_interrupted_jobs (called right after this in the
    same startup hook) auto-restarts it from its saved params, same as any
    other interruption."""
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="x", status="generating")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    image_jobs.sweep_interrupted_jobs()

    db = SessionLocal()
    try:
        job = db.get(ImageJob, job_id)
        assert job.status == "interrupted"
        assert "restart" in job.error.lower()
    finally:
        db.close()


# ── Job survival: checkpointing, resume, shutdown guard (app/job_shutdown.py) ─
#
# See that module's own docstring for the "stop fast" design, and
# audio_jobs.py's own equivalent tests for the fuller pattern this mirrors.
# Image generation has no intermediate state to checkpoint (one opaque
# SwarmUI/ComfyUI call) — an interrupted job restarts from its saved
# params on the next boot rather than truly resuming.

@pytest.fixture(autouse=True)
def _reset_job_shutdown_flag():
    job_shutdown.clear_stop()
    yield
    job_shutdown.clear_stop()


@pytest.mark.asyncio
async def test_shutdown_cancel_marks_interrupted_not_cancelled(client, seed, monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", hang)

    job_id = image_jobs.create_job(
        world_id=seed.world_a.id, prompt="x", params={"prompt": "x", "uploads_dir": "/tmp"},
    )
    await asyncio.sleep(0.05)
    job_shutdown.request_stop()
    assert image_jobs.cancel_job(job_id)
    job = await _await_terminal(job_id)
    assert job.status == "interrupted"
    assert "restart" in job.error.lower()


@pytest.mark.asyncio
async def test_gm_cancel_still_marks_cancelled(client, seed, monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", hang)

    job_id = image_jobs.create_job(
        world_id=seed.world_a.id, prompt="x", params={"prompt": "x", "uploads_dir": "/tmp"},
    )
    await asyncio.sleep(0.05)
    assert not job_shutdown.stopping()
    assert image_jobs.cancel_job(job_id)
    job = await _await_terminal(job_id)
    assert job.status == "cancelled"
    assert job.error == "Cancelled by GM."


@pytest.mark.asyncio
async def test_resume_interrupted_jobs_restarts_from_the_persisted_request(client, seed, monkeypatch):
    calls = []

    async def fake_generate(**kwargs):
        calls.append(kwargs)
        return ["/uploads/ai-images/resumed.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    params = {"prompt": "a neon dragon", "uploads_dir": "/tmp"}
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="a neon dragon", status="interrupted",
                        params_json=json.dumps(params))
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    resumed = image_jobs.resume_interrupted_jobs()
    assert resumed == 1

    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert job.resumed_count == 1
    assert calls and calls[0]["prompt"] == "a neon dragon"


def test_resume_gives_up_after_max_auto_resumes(client, seed):
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="x", status="interrupted",
                        params_json=json.dumps({"prompt": "x", "uploads_dir": "/tmp"}),
                        resumed_count=job_shutdown.MAX_AUTO_RESUMES)
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    resumed = image_jobs.resume_interrupted_jobs()
    assert resumed == 0

    db = SessionLocal()
    try:
        j = db.get(ImageJob, job_id)
        assert j.status == "error"
        assert "restart" in j.error.lower()
        assert str(job_shutdown.MAX_AUTO_RESUMES) in j.error
    finally:
        db.close()


class _FakeTask:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled

    def cancelled(self):
        return self._cancelled


def test_forget_task_does_not_evict_a_newer_task_for_the_same_job_id():
    old_task = _FakeTask()
    new_task = _FakeTask()
    image_jobs._running_tasks[999999] = new_task
    try:
        image_jobs._forget_task(999999, old_task)
        assert image_jobs._running_tasks.get(999999) is new_task
    finally:
        image_jobs._running_tasks.pop(999999, None)


def test_forget_task_reconciles_a_job_cancelled_before_its_body_ever_ran(client, seed):
    db = SessionLocal()
    try:
        job = ImageJob(world_id=seed.world_a.id, prompt="x", status="pending")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    task = _FakeTask(cancelled=True)
    image_jobs._running_tasks[job_id] = task
    image_jobs._forget_task(job_id, task)

    assert job_id not in image_jobs._running_tasks
    db = SessionLocal()
    try:
        updated = db.get(ImageJob, job_id)
        assert updated.status == "cancelled"
        assert updated.error == "Cancelled by GM."
    finally:
        db.close()


# ── POST/GET /api/ai/imagegen/jobs* routes ──────────────────────────────────

def _body(**overrides):
    body = {"prompt": "a neon dragon"}
    body.update(overrides)
    return body


def test_job_create_and_poll(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return ["/uploads/ai-images/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/jobs", json=_body())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    data = _poll_until_terminal(client, f"/api/ai/imagegen/jobs/{job_id}")
    assert data["status"] == "done"
    assert data["urls"] == ["/uploads/ai-images/x.png"]
    assert data["prompt"] == "a neon dragon"


def test_job_list_scoped_to_active_world(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/imagegen/jobs", json=_body())

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/imagegen/jobs")
    assert r.json() == []


def test_job_status_404s_across_worlds(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get(f"/api/ai/imagegen/jobs/{job_id}")
    assert r.status_code == 404


def test_job_cancel(client, seed, monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", hang)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]

    r = client.post(f"/api/ai/imagegen/jobs/{job_id}/cancel")
    assert r.status_code == 200


def test_job_delete(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return ["/uploads/ai-images/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/jobs/{job_id}")

    r = client.delete(f"/api/ai/imagegen/jobs/{job_id}")
    assert r.status_code == 200, r.text
    r2 = client.get(f"/api/ai/imagegen/jobs/{job_id}")
    assert r2.status_code == 404


def test_job_delete_rejects_in_progress_job(client, seed, monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", hang)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]

    r = client.delete(f"/api/ai/imagegen/jobs/{job_id}")
    assert r.status_code == 400

    client.post(f"/api/ai/imagegen/jobs/{job_id}/cancel")


def test_job_delete_404s_across_worlds(client, seed, monkeypatch):
    async def fake_generate(**kwargs):
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/jobs/{job_id}")

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.delete(f"/api/ai/imagegen/jobs/{job_id}")
    assert r.status_code == 404


def test_job_create_blank_model_falls_back_to_configured_default(client, seed, monkeypatch):
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return []
    monkeypatch.setattr(ai_module, "imagegen_generate", fake_generate)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/defaults", json={"surface": "image", "model_id": "configured-image-model"})
    job_id = client.post("/api/ai/imagegen/jobs", json=_body()).json()["job_id"]

    deadline = time.time() + 5
    while time.time() < deadline and "model" not in captured:
        time.sleep(0.02)
    assert captured.get("model") == "configured-image-model"
