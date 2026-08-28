"""Tests for app/job_shutdown.py — the process-wide "we're stopping"
coordination shared by the three background-job engines (audio_jobs.py,
image_jobs.py, chat_jobs.py) and the chunk loops in app/ai.py they drive.
Pure module-level tests: no DB, no HTTP, no job engine involved — those are
covered where each engine wires this module in (tests/test_audio_jobs.py,
tests/test_image_jobs.py, tests/test_chat_jobs.py) and where app.main's
lifespan wires the whole thing together (also in this file, see the bottom).
"""
import asyncio

import pytest

from app import job_shutdown


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """Every test starts and ends with stopping() false, regardless of
    whether the test itself calls request_stop() — mirrors clear_stop()'s
    own real-world job (see its docstring), just enforced per-test here
    instead of per-process-boot."""
    job_shutdown.clear_stop()
    yield
    job_shutdown.clear_stop()


# ── stopping / request_stop / clear_stop ────────────────────────────────────

def test_stopping_is_false_until_request_stop():
    assert job_shutdown.stopping() is False
    job_shutdown.request_stop()
    assert job_shutdown.stopping() is True


def test_clear_stop_resets_the_flag():
    job_shutdown.request_stop()
    assert job_shutdown.stopping() is True
    job_shutdown.clear_stop()
    assert job_shutdown.stopping() is False


# ── drain() ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drain_returns_immediately_with_no_tasks():
    cancelled = await job_shutdown.drain([])
    assert cancelled == 0


@pytest.mark.asyncio
async def test_drain_lets_a_task_finish_inside_the_grace_window():
    async def quick():
        await asyncio.sleep(0.01)
        return "done"

    task = asyncio.create_task(quick())
    cancelled = await job_shutdown.drain([task], grace=1.0)
    assert cancelled == 0
    assert task.done() and not task.cancelled()
    assert task.result() == "done"


@pytest.mark.asyncio
async def test_drain_cancels_a_task_that_outlives_the_grace_window():
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    cancelled = await job_shutdown.drain([task], grace=0.01)
    assert cancelled == 1
    assert task.cancelled()


@pytest.mark.asyncio
async def test_drain_waits_for_a_cancelled_tasks_own_cleanup_to_run():
    cleanup_ran = False

    async def slow_with_cleanup():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            nonlocal cleanup_ran
            await asyncio.sleep(0.05)  # simulates a bit of async cleanup work
            cleanup_ran = True
            raise

    task = asyncio.create_task(slow_with_cleanup())
    await job_shutdown.drain([task], grace=0.01)
    assert cleanup_ran, "drain() should wait for the cancelled task's own handler to finish, not just fire-and-forget cancel()"


@pytest.mark.asyncio
async def test_drain_does_not_hang_on_a_task_that_swallows_cancellation():
    async def stubborn():
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue  # deliberately never actually stops — a single more
                          # cancel() would be swallowed the same way, so this
                          # task is genuinely un-cancellable from the outside;
                          # the test only needs to prove drain() itself still
                          # returns promptly, not that the task ever exits

    task = asyncio.create_task(stubborn())
    cancelled = await job_shutdown.drain([task], grace=0.01)
    assert cancelled == 1  # drain() returned instead of hanging forever
    assert not task.done()  # confirms it's genuinely still stubborn, not a fluke


@pytest.mark.asyncio
async def test_drain_with_zero_grace_cancels_immediately():
    async def slow():
        await asyncio.sleep(10)

    task = asyncio.create_task(slow())
    cancelled = await job_shutdown.drain([task], grace=0.0)
    assert cancelled == 1
    assert task.cancelled()


# ── JobInterrupted ───────────────────────────────────────────────────────────

def test_job_interrupted_is_not_caught_by_except_cancelled_error():
    with pytest.raises(job_shutdown.JobInterrupted):
        try:
            raise job_shutdown.JobInterrupted("stopped at a chunk boundary")
        except asyncio.CancelledError:
            raise AssertionError("JobInterrupted must not be caught as CancelledError")


# ── app.main lifespan integration ───────────────────────────────────────────
#
# The unit tests above cover job_shutdown.py's own primitives; the ones in
# each engine's own test file (test_audio_jobs.py etc.) cover the
# stopping()/JobInterrupted wiring at the engine level by calling
# request_stop()/cancel_job() directly. These instead drive the real ASGI
# lifespan (app.main._lifespan, via a raw TestClient enter/exit) to prove
# the actual wiring — app.main._startup_tasks/_shutdown_tasks calling the
# right engine functions in the right order — is correct end to end.
import time

from starlette.testclient import TestClient

import app.main as main_module


def test_lifespan_startup_still_runs_every_boot_sweep(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module._audio_jobs, "sweep_interrupted_jobs", lambda: calls.append("audio_sweep"))
    monkeypatch.setattr(main_module._audio_jobs, "sweep_orphaned_job_audio", lambda: calls.append("audio_orphan"))
    monkeypatch.setattr(main_module._audio_jobs, "resume_interrupted_jobs", lambda: calls.append("audio_resume") or 0)
    monkeypatch.setattr(main_module._image_jobs, "sweep_interrupted_jobs", lambda: calls.append("image_sweep"))
    monkeypatch.setattr(main_module._image_jobs, "resume_interrupted_jobs", lambda: calls.append("image_resume") or 0)
    monkeypatch.setattr(main_module._chat_jobs, "sweep_interrupted_jobs", lambda: calls.append("chat_sweep"))
    monkeypatch.setattr(main_module._chat_jobs, "resume_interrupted_jobs", lambda: calls.append("chat_resume") or 0)

    with TestClient(main_module.app):
        pass

    assert calls == [
        "audio_sweep", "audio_orphan", "image_sweep", "chat_sweep",
        "audio_resume", "image_resume", "chat_resume",
    ]


def test_lifespan_shutdown_is_fast_when_nothing_is_running():
    start = time.time()
    with TestClient(main_module.app):
        pass
    elapsed = time.time() - start
    # Generous margin over the near-instant "empty task list" fast path in
    # job_shutdown.drain() — this is what keeps the ~1000-test suite's
    # wall-clock time flat despite every test entering/exiting this same
    # lifespan (see tests/conftest.py's ND_JOB_STOP_GRACE_SECONDS=0).
    assert elapsed < 2.0


def test_no_deprecation_warning_for_on_event_handlers():
    fastapi_app = main_module._fastapi_app
    assert fastapi_app.router.on_startup == []
    assert fastapi_app.router.on_shutdown == []
    assert fastapi_app.router.lifespan_context is not None


def test_lifespan_shutdown_marks_an_in_flight_job_interrupted(monkeypatch):
    from app import ai as ai_module
    from app.database import SessionLocal, engine
    from app.models import AudioJob, Base, User, World
    from app import auth as auth_module

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)  # so the seed insert below has tables to write to, before the TestClient's own init_db() would normally create them
    db = SessionLocal()
    try:
        gm = User(email="gm-lifespan-a@test.local", password_hash=auth_module.hash_password("pw"),
                  display_name="GM", is_gm=True)
        world = World(name="W", slug="w-lifespan-a")
        db.add_all([gm, world])
        db.commit()
    finally:
        db.close()

    async def hang(path, glossary="", **kwargs):
        await asyncio.sleep(3600)
        return "unused"
    monkeypatch.setattr(ai_module, "transcribe_audio", hang)

    with TestClient(main_module.app) as c:
        c.post("/login", data={"email": "gm-lifespan-a@test.local", "password": "pw", "next": "/"})
        c.cookies.set("active_world", "w-lifespan-a")
        r = c.post("/api/ai/attachments/audio-jobs", files={"file": ("clip.mp3", b"fake", "audio/mpeg")})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        deadline = time.time() + 5
        db = SessionLocal()
        try:
            job = None
            while time.time() < deadline:
                db.expire_all()
                job = db.get(AudioJob, job_id)
                if job.status == "transcribing":
                    break
                time.sleep(0.02)
            assert job.status == "transcribing", "job never reached transcribing before shutdown"
        finally:
            db.close()
    # TestClient.__exit__ just ran the real ASGI lifespan shutdown.

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "interrupted"
        assert "restart" in job.error.lower()
    finally:
        db.close()


def test_interrupted_job_from_shutdown_is_resumed_on_the_next_startup(monkeypatch):
    """The end-to-end proof this whole feature exists for: a job's own
    checkpoint survives a real TestClient shutdown/restart cycle (not just
    the engine-level simulation other tests use) and auto-resumes with
    resumed_count incremented on the next boot."""
    from app import ai as ai_module
    from app.database import SessionLocal, engine
    from app.models import AudioJob, Base, User, World
    from app import auth as auth_module

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)  # so the seed insert below has tables to write to, before the TestClient's own init_db() would normally create them
    db = SessionLocal()
    try:
        gm = User(email="gm-lifespan-b@test.local", password_hash=auth_module.hash_password("pw"),
                  display_name="GM", is_gm=True)
        world = World(name="W", slug="w-lifespan-b")
        db.add_all([gm, world])
        db.commit()
    finally:
        db.close()

    async def hang(path, glossary="", **kwargs):
        on_checkpoint = kwargs["on_checkpoint"]
        on_checkpoint({"phase": "transcribe", "chunks_done": 1, "chunk_total": 3,
                        "chunk_seconds": 600, "audio_size": path.stat().st_size, "text": "part 0"})
        await asyncio.sleep(3600)
        return "unused"
    monkeypatch.setattr(ai_module, "transcribe_audio", hang)

    with TestClient(main_module.app) as c:
        c.post("/login", data={"email": "gm-lifespan-b@test.local", "password": "pw", "next": "/"})
        c.cookies.set("active_world", "w-lifespan-b")
        r = c.post("/api/ai/attachments/audio-jobs", files={"file": ("clip.mp3", b"fake", "audio/mpeg")})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        deadline = time.time() + 5
        db = SessionLocal()
        try:
            job = None
            while time.time() < deadline:
                db.expire_all()
                job = db.get(AudioJob, job_id)
                if job.checkpoint_json:
                    break
                time.sleep(0.02)
            assert job.checkpoint_json, "checkpoint was never written before shutdown"
        finally:
            db.close()
    # First TestClient's __exit__ ran the real shutdown — job is "interrupted".

    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "interrupted"
    finally:
        db.close()

    with TestClient(main_module.app):
        # Entering runs the real startup, including resume_interrupted_jobs
        # — which sets the resumed row's fields synchronously before this
        # `with` block's body even starts (see start_resume_job's own
        # docstring on why that ordering matters).
        deadline = time.time() + 5
        db = SessionLocal()
        try:
            job = None
            while time.time() < deadline:
                db.expire_all()
                job = db.get(AudioJob, job_id)
                if job.status == "transcribing":
                    break
                time.sleep(0.02)
            assert job.status == "transcribing"
            assert job.resumed_count == 1
            assert job.checkpoint_json  # the prior checkpoint carried through
        finally:
            db.close()


# ── Deployment config: graceful shutdown timing ─────────────────────────────
#
# These guard against the two settings that make everything above actually
# run in production drifting out of sync with each other or being dropped
# on a future edit — see Dockerfile's/docker-compose.yml's own comments for
# the full time-budget rationale.
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent


def test_dockerfile_sets_a_bounded_graceful_shutdown_timeout():
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text()
    assert "--timeout-graceful-shutdown" in dockerfile


def test_compose_files_set_a_stop_grace_period_for_the_world_service():
    for name in ("docker-compose.yml", "truenas-compose.yml"):
        text = (_REPO_ROOT / name).read_text()
        assert "stop_grace_period" in text, f"{name} is missing stop_grace_period"
