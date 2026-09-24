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


# ── delete_job's own file cleanup ────────────────────────────────────────────
#
# A real bug: delete_job used to only remove the ImageJob ROW — the actual
# generated file(s) under <uploads_dir>/ai-images/ were never touched, so a
# player could cycle {generate, delete, generate, delete, ...} to accumulate
# unbounded disk usage despite nominally staying under app.routers.ai's own
# _MAX_IMAGE_JOBS_PER_PLAYER/_MAX_IMAGE_JOBS_PER_WORLD cap the whole time —
# that cap counts ROWS, and disk usage is what it was actually meant to
# bound.

def _make_image_job(world_id, uploads_dir, urls, **overrides):
    db = SessionLocal()
    try:
        job = ImageJob(
            world_id=world_id, prompt="x", status="done",
            result_urls_json=json.dumps(urls),
            params_json=json.dumps({"uploads_dir": str(uploads_dir)}),
            **overrides,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_delete_removes_the_generated_image_file(client, seed, tmp_path):
    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    img = ai_img_dir / "abc123.png"
    img.write_bytes(b"fake png")
    job_id = _make_image_job(seed.world_a.id, tmp_path, ["/uploads/ai-images/abc123.png"])

    assert image_jobs.delete_job(job_id) is True
    assert not img.exists()


def test_delete_removes_the_thumbnail_alongside_the_image(client, seed, tmp_path):
    from app.imaging import thumbnail_path_for

    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    img = ai_img_dir / "abc123.png"
    img.write_bytes(b"fake png")
    thumb = thumbnail_path_for(img)
    thumb.write_bytes(b"fake thumb")
    job_id = _make_image_job(seed.world_a.id, tmp_path, ["/uploads/ai-images/abc123.png"])

    assert image_jobs.delete_job(job_id) is True
    assert not img.exists()
    assert not thumb.exists()


def test_delete_removes_every_image_in_a_batch(client, seed, tmp_path):
    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    imgs = [ai_img_dir / f"img{i}.png" for i in range(3)]
    for img in imgs:
        img.write_bytes(b"x")
    job_id = _make_image_job(
        seed.world_a.id, tmp_path, [f"/uploads/ai-images/{p.name}" for p in imgs],
    )

    assert image_jobs.delete_job(job_id) is True
    assert not any(img.exists() for img in imgs)


def test_delete_never_removes_a_still_starred_image(client, seed, tmp_path):
    """Starring copies nothing — a StarredImage row just keeps referencing
    the same /uploads/ai-images/... URL a job produced, so deleting the
    originating job must never take the file (and therefore the star)
    down with it."""
    from app.models import StarredImage

    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    img = ai_img_dir / "starred.png"
    img.write_bytes(b"fake png")
    url = "/uploads/ai-images/starred.png"
    job_id = _make_image_job(seed.world_a.id, tmp_path, [url])

    db = SessionLocal()
    try:
        db.add(StarredImage(url=url))
        db.commit()
    finally:
        db.close()

    assert image_jobs.delete_job(job_id) is True
    assert img.exists()

    db = SessionLocal()
    try:
        assert db.query(StarredImage).filter(StarredImage.url == url).first() is not None
    finally:
        db.close()


def test_delete_only_skips_the_specific_starred_image_in_a_batch(client, seed, tmp_path):
    """A batch job's images can be starred individually — the unstarred
    sibling(s) must still be cleaned up even when one is protected."""
    from app.models import StarredImage

    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    starred_img = ai_img_dir / "keep.png"
    other_img = ai_img_dir / "gone.png"
    starred_img.write_bytes(b"x")
    other_img.write_bytes(b"x")
    starred_url = "/uploads/ai-images/keep.png"
    other_url = "/uploads/ai-images/gone.png"
    job_id = _make_image_job(seed.world_a.id, tmp_path, [starred_url, other_url])

    db = SessionLocal()
    try:
        db.add(StarredImage(url=starred_url))
        db.commit()
    finally:
        db.close()

    assert image_jobs.delete_job(job_id) is True
    assert starred_img.exists()
    assert not other_img.exists()


def test_delete_tolerates_missing_uploads_dir_in_params(client, seed):
    """An old/degraded job row with no params_json at all (or no
    uploads_dir in it) must not crash the delete — there's simply nothing
    safe to clean up."""
    db = SessionLocal()
    try:
        job = ImageJob(
            world_id=seed.world_a.id, prompt="x", status="done",
            result_urls_json=json.dumps(["/uploads/ai-images/orphan.png"]),
            params_json="{}",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    assert image_jobs.delete_job(job_id) is True


def test_delete_rejects_a_path_traversal_filename(client, seed, tmp_path):
    """A corrupted/malicious result_urls_json entry must never be used to
    delete a file outside ai-images — the filename portion is rejected
    outright if it carries a path separator, same hardening as
    app.ai._swarmui_model_path."""
    ai_img_dir = tmp_path / "ai-images"
    ai_img_dir.mkdir()
    outside_file = tmp_path / "important.txt"
    outside_file.write_bytes(b"do not delete me")
    job_id = _make_image_job(seed.world_a.id, tmp_path, ["/uploads/ai-images/../important.txt"])

    assert image_jobs.delete_job(job_id) is True
    assert outside_file.exists()


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
