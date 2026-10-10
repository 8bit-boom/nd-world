"""The Background Jobs page's list of non-durable jobs (app/live_jobs.py, app/routers/live_jobs.py): every in-memory
AI job is recorded as it runs, and can be cancelled (its pollers stop waiting) and restarted from the same arguments."""
import asyncio

import pytest

from app import ai_background, ai_queue, live_jobs

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean():
    ai_queue.queue.reset()
    ai_background._TASKS.clear()
    live_jobs._JOBS.clear()
    yield
    ai_queue.queue.reset()
    ai_background._TASKS.clear()
    live_jobs._JOBS.clear()


def _make_runner(store, gate, log):
    @ai_queue.serialized("test job", "job", store=store)
    async def run(job_id, word):
        log.append((job_id, word))
        await gate.wait()
        if word == "boom":
            store[job_id].update(status="error", error="it broke")
        else:
            store[job_id].update(status="done", result=word)
    return run


@pytest.mark.asyncio
async def test_run_is_recorded_then_marked_done():
    store, log, gate = {1: {"status": "running", "user_id": 7}}, [], asyncio.Event()
    run = _make_runner(store, gate, log)
    task = asyncio.create_task(run(1, "hello"))
    await asyncio.sleep(0.05)
    [rec] = live_jobs.all_jobs()
    assert rec.status == "running" and rec.label == "test job" and rec.job_id == 1
    assert rec.view()["can_cancel"] and not rec.view()["can_restart"]
    gate.set()
    await task
    assert rec.status == "done" and store[1]["result"] == "hello"
    assert rec.view()["can_restart"]


@pytest.mark.asyncio
async def test_a_failure_the_runner_recorded_shows_as_error():
    store, log, gate = {1: {"status": "running"}}, [], asyncio.Event()
    gate.set()
    await _make_runner(store, gate, log)(1, "boom")
    [rec] = live_jobs.all_jobs()
    assert rec.status == "error" and rec.error == "it broke"


@pytest.mark.asyncio
async def test_cancel_stops_the_run_and_releases_its_pollers():
    store, log, gate = {1: {"status": "running"}}, [], asyncio.Event()
    run = _make_runner(store, gate, log)
    task = asyncio.create_task(run(1, "x"))
    await asyncio.sleep(0.05)
    [rec] = live_jobs.all_jobs()
    assert live_jobs.cancel(rec) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.status == "cancelled"
    assert store[1]["status"] == "error" and store[1]["error"] == "Cancelled."     # a poller stops waiting
    assert not ai_queue.queue.busy()                                                # the queue slot was given back
    assert live_jobs.cancel(rec) is False


@pytest.mark.asyncio
async def test_a_job_waiting_for_its_turn_can_be_cancelled_too():
    store, log, gate = {1: {"status": "running"}, 2: {"status": "running"}}, [], asyncio.Event()
    run = _make_runner(store, gate, log)
    first = asyncio.create_task(run(1, "a"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(run(2, "b"))
    await asyncio.sleep(0.05)
    waiting = next(r for r in live_jobs.all_jobs() if r.job_id == 2)
    assert waiting.status == "queued"
    assert live_jobs.cancel(waiting)
    with pytest.raises(asyncio.CancelledError):
        await second
    assert waiting.status == "cancelled" and log == [(1, "a")]
    gate.set()
    await first


@pytest.mark.asyncio
async def test_restart_runs_it_again_under_the_same_job_id():
    store, log, gate = {5: {"status": "running", "user_id": 7, "draft": None}}, [], asyncio.Event()
    run = _make_runner(store, gate, log)
    task = asyncio.create_task(run(5, "again"))
    await asyncio.sleep(0.05)
    [rec] = live_jobs.all_jobs()
    assert live_jobs.restart(rec) is False                  # still running: nothing to restart
    live_jobs.cancel(rec)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert live_jobs.restart(rec) is True
    await asyncio.sleep(0.05)
    assert store[5]["status"] == "running" and store[5]["user_id"] == 7 and store[5]["draft"] is None
    assert log == [(5, "again"), (5, "again")]
    gate.set()
    await asyncio.sleep(0.05)
    assert store[5]["status"] == "done"
    [new] = live_jobs.all_jobs()
    assert new is not rec and new.status == "done"


def test_untracked_when_there_is_no_store_or_job_id():
    assert live_jobs.begin("x", None, None, (1,), {}) is None
    assert live_jobs.begin("x", {}, None, ("slug",), {}) is None


def test_every_in_memory_runner_is_registered():
    """A new job runner must pass its status dict (store=...) or it never shows on the Background Jobs page."""
    import re
    from pathlib import Path
    missing = []
    for path in Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'@_ai_queue\.serialized\(([^)]*)\)', text):
            args = m.group(1)
            if '"job"' in args and "store=" not in args and path.name not in ("audio_jobs.py", "video_jobs.py", "image_jobs.py",
                                                                              "chat_jobs.py", "media_rename_jobs.py"):
                missing.append(f"{path}: {args}")
    assert not missing, missing


# ── the route ───────────────────────────────────────────────────────────────────────────────────

def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_list_cancel_restart_and_remove_over_http(client, seed):
    _gm(client, seed)
    store = {9: {"status": "done", "result": "x"}}
    rec = live_jobs.LiveJob("Test draft", store, None, (9,), {})
    rec.status, rec.finished = "error", rec.started + 3
    rec.error = "boom"
    live_jobs._JOBS[rec.id] = rec
    rows = client.get("/api/live-jobs").json()["jobs"]
    mine = next(r for r in rows if r["key"] == "m:" + rec.id)
    assert mine["status"] == "error" and mine["error"] == "boom" and mine["can_restart"] and mine["can_remove"]
    assert client.post("/api/live-jobs/m:" + rec.id + "/cancel").status_code == 400     # not running
    assert client.delete("/api/live-jobs/m:" + rec.id).status_code == 200
    assert not any(r["key"] == "m:" + rec.id for r in client.get("/api/live-jobs").json()["jobs"])
    assert client.post("/api/live-jobs/zz:1/restart").status_code == 404
    assert client.post("/api/live-jobs/r:999999/cancel").status_code == 404


def test_a_player_cannot_use_it(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/live-jobs").status_code in (401, 403)
    assert client.post("/api/live-jobs/m:abc/cancel").status_code in (401, 403)


def test_ai_button_task_can_be_restarted(client, seed, monkeypatch):
    """A finished AI-button task is run again from its saved request, as a new task."""
    _gm(client, seed)
    hits = []

    async def fake_app(scope, receive, send):
        hits.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})

    from app.ai_background import Task, AiBackgroundMiddleware
    mw = AiBackgroundMiddleware(fake_app)
    scope = {"type": "http", "method": "POST", "path": "/api/ai/assist", "headers": [], "query_string": b""}

    async def drive():
        task = mw._launch(scope, b"{}", seed.gm.id, "Test")
        await task.task
        assert task.status == "done"
        again = ai_background.restart(task)
        assert again is not None and again.id != task.id and task.id not in ai_background._TASKS
        await again.task
        return again

    asyncio.run(drive())
    assert hits == ["/api/ai/assist", "/api/ai/assist"]
