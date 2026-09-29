"""Tests for app/video_jobs.py and its /api/video-jobs routes — the
durable background engine for AI video generation via Studio's
/v1/videos. The Studio HTTP surface is mocked at the unsloth_extras
boundary: these tests pin nd-world's own state machine (start → poll →
fetch → VideoClip, restart re-attach, cancel, delete) rather than
Studio's unverified response shapes — those are covered defensively in
test_unsloth_extras.

Engine tests call video_jobs.create_job DIRECTLY on the test's own event
loop so the background task can be awaited deterministically; a task the
TestClient route created lives on the portal's loop and can't be awaited
from here (cross-loop), so route tests only pin what the routes own —
row creation, gating, list/delete.
"""
import pytest

from app import video_jobs
from app.database import SessionLocal
from app.models import VideoClip, VideoJob


class _FakeStudio:
    """Replaces video_jobs._studio with scripted responses. Exposes
    StudioError because the poll loop raises _studio.StudioError."""
    from app.unsloth_extras import StudioError

    def __init__(self, *, gen_resp=None, entry=None, content=b"MP4"):
        self.calls = {"generate": 0, "list": 0, "content": 0}
        # When an entry is scripted, the generate response must return THAT
        # entry's id by default — the poll loop keys on it, and a mismatch
        # silently waits out the (hours-long) deadline instead of finishing.
        self._gen_resp = gen_resp or ({"id": entry["id"]} if entry else {"id": "vid-42"})
        self._entry = entry
        self._content = content

    async def video_generate(self, body):
        self.calls["generate"] += 1
        self.last_body = body
        return self._gen_resp

    async def video_list(self):
        self.calls["list"] += 1
        if self._entry is None:
            return {"object": "list", "data": []}
        return {"object": "list", "data": [self._entry]}

    async def video_content(self, video_id):
        self.calls["content"] += 1
        return self._content, "video/mp4"


def _fast_poll(monkeypatch):
    """Seconds-scale polling + deadline, so a fixture/scripting mistake in
    these tests fails fast instead of hanging for the 4 h default."""
    monkeypatch.setattr(video_jobs, "_MAX_WAIT_SECONDS", 5.0)


async def _drive(job_id):
    """Await the job's background task to completion (valid only for tasks
    created on THIS loop — see the module docstring)."""
    task = video_jobs._running_tasks.get(job_id)
    if task:
        await task


@pytest.mark.asyncio
async def test_job_runs_to_done_and_creates_a_gm_only_clip(seed, monkeypatch):
    _fast_poll(monkeypatch)
    _fast_poll(monkeypatch)
    _fast_poll(monkeypatch)
    _fast_poll(monkeypatch)
    _fast_poll(monkeypatch)
    fake = _FakeStudio(entry={"id": "vid-42", "status": "succeeded"})
    monkeypatch.setattr(video_jobs, "_studio", fake)

    job_id = video_jobs.create_job(seed.world_a.id, "neon alley, camera push",
                                    {"prompt": "neon alley, camera push", "model": "m",
                                     "seconds": 5, "name": "Alley"})
    await _drive(job_id)

    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        assert job.status == "done"
        assert job.studio_video_id == "vid-42"
        assert fake.calls["generate"] == 1
        clip = db.get(VideoClip, job.clip_id)
        assert clip is not None
        assert clip.world_id == seed.world_a.id
        assert clip.visible_to_players is False
        assert clip.name == "Alley"
        assert clip.file_url.startswith("/uploads/video/ai-video-")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_reattaches_without_paying_for_generation_again(seed, monkeypatch):
    fake = _FakeStudio(entry={"id": "vid-7", "status": "completed"})
    monkeypatch.setattr(video_jobs, "_studio", fake)

    job_id = video_jobs.create_job(seed.world_a.id, "re push", {"prompt": "re push"})
    await _drive(job_id)

    # Simulate a restart: row back to interrupted with the Studio id kept,
    # then the boot resume path — which must NOT POST /v1/videos again.
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        job.status = "interrupted"
        db.commit()
    finally:
        db.close()
    fake.calls["generate"] = 0
    assert video_jobs.resume_interrupted_jobs() == 1
    await _drive(job_id)
    assert fake.calls["generate"] == 0
    db = SessionLocal()
    try:
        assert db.get(VideoJob, job_id).status == "done"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_generation_marks_job_error(seed, monkeypatch):
    fake = _FakeStudio(entry={"id": "vid-42", "status": "failed", "error": "oom"})
    monkeypatch.setattr(video_jobs, "_studio", fake)

    job_id = video_jobs.create_job(seed.world_a.id, "x", {"prompt": "x"})
    await _drive(job_id)
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        assert job.status == "error"
        assert "oom" in job.error
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unrecognized_generate_response_is_a_clean_error(seed, monkeypatch):
    fake = _FakeStudio(gen_resp={"nothing": "recognizable"}, entry=None)
    monkeypatch.setattr(video_jobs, "_studio", fake)

    job_id = video_jobs.create_job(seed.world_a.id, "x", {"prompt": "x"})
    await _drive(job_id)
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        assert job.status == "error"
        assert "no generation id" in job.error
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_job_keeps_the_clip(client, seed, monkeypatch):
    from .conftest import GM_PASSWORD, login
    fake = _FakeStudio(entry={"id": "vid-42", "status": "succeeded"})
    monkeypatch.setattr(video_jobs, "_studio", fake)

    job_id = video_jobs.create_job(seed.world_a.id, "x", {"prompt": "x"})
    await _drive(job_id)
    db = SessionLocal()
    clip_id = db.get(VideoJob, job_id).clip_id
    db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.delete(f"/api/video-jobs/{job_id}")
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(VideoJob, job_id) is None
        # The clip is a library asset now — deleting the job must not eat it
        assert db.get(VideoClip, clip_id) is not None
    finally:
        db.close()


@pytest.fixture
def studio_key_on(monkeypatch):
    """The start route refuses to run without a Studio key; the route tests
    don't touch Studio itself, so just satisfy the gate."""
    from app.routers import video as _video_router
    monkeypatch.setattr(_video_router._ai_module, "effective_llm_api_key",
                        lambda: "sk-test")


def test_start_route_creates_a_pending_row(client, seed, studio_key_on, monkeypatch):
    """Route-level contract only (GM gate, world scoping, params plumbing) —
    create_job is stubbed so no background task reaches a nonexistent
    Studio; the engine itself is covered by the tests above."""
    import json as _json

    def _stub_create(world_id, prompt, params, created_by_user_id=None):
        db = SessionLocal()
        try:
            job = VideoJob(world_id=world_id, prompt=prompt,
                           params_json=_json.dumps(params), status="pending")
            db.add(job)
            db.commit()
            db.refresh(job)
            return job.id
        finally:
            db.close()

    monkeypatch.setattr(video_jobs, "create_job", _stub_create)
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/video-jobs/start", json={
        "prompt": "route smoke", "model": "m", "seconds": 5, "name": "R"})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    db = SessionLocal()
    try:
        job = db.get(VideoJob, job_id)
        assert job.world_id == seed.world_a.id
        assert job.prompt == "route smoke"
        assert job.status == "pending"
        assert _json.loads(job.params_json)["model"] == "m"
    finally:
        db.close()


def test_routes_are_gm_only_and_prompt_required(client, seed):
    from .conftest import PLAYER_PASSWORD, login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/video-jobs").status_code == 403
    assert client.post("/api/video-jobs/start", json={"prompt": "x"}).status_code == 403

    from .conftest import GM_PASSWORD
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/video-jobs/start", json={"prompt": "  "}).status_code == 400


def test_list_route_returns_jobs(client, seed):
    from .conftest import GM_PASSWORD, login
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        db.add(VideoJob(world_id=seed.world_a.id, prompt="p", status="done"))
        db.commit()
    finally:
        db.close()
    r = client.get("/api/video-jobs")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert any(j["prompt"] == "p" for j in jobs)
