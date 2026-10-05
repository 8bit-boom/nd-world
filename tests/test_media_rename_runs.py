"""Bulk rename: select everything that still needs a name across the whole library, and run it in the background
(app/media_rename_jobs.py, the /api/media-rename/{summary,select,runs} routes). A run keeps going when the page is closed,
saves its proposals chunk by chunk, can be cancelled, survives a server restart, and its results are reviewed and applied
like any other batch.
"""
import asyncio
import importlib
import json
import re
import time

import pytest

from app import ai as ai_module
from app import job_shutdown
from app import media_rename_jobs as jobs
from app.database import SessionLocal
from app.models import AudioAlbum, AudioClip, Entity, ImageAlbum, MediaRenameRun, MediaTitle, VideoClip, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────

def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _clips(seed, names, kind="audio", album_id=None, world=None):
    db = SessionLocal()
    try:
        cls = AudioClip if kind == "audio" else VideoClip
        rows = [cls(world_id=(world or seed.world_a).id, name=n, file_url=f"/uploads/{kind}/missing-{i}-{abs(hash(n)) % 9999}.bin", album_id=album_id)
                for i, n in enumerate(names)]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]
    finally:
        db.close()


def _refs(kind, ids):
    return [{"kind": kind, "id": i} for i in ids]


class _Chat:
    """A scripted model: names every item "New <n>", counts calls, optionally waits on a gate or fails."""

    def __init__(self, gate=None, fail_on_call=None, fail_text="[AI error: backend is down]"):
        self.calls, self.gate, self.fail_on_call, self.fail_text = [], gate, fail_on_call, fail_text

    async def __call__(self, messages, system="", model="", options=None, think=False, format=None):
        self.calls.append(messages[-1]["content"])
        if self.gate is not None and len(self.calls) > 1:
            await self.gate.wait()
        if self.fail_on_call == len(self.calls):
            return self.fail_text
        ids = re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.M)
        return json.dumps({"names": [{"id": i, "name": f"New {len(self.calls)}-{i}"} for i in ids]})


async def _await_status(run_id, statuses, timeout=8.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        while time.time() < deadline:
            db.expire_all()
            run = db.get(MediaRenameRun, run_id)
            if run.status in statuses:
                return run
            await asyncio.sleep(0.02)
        raise AssertionError(f"run stuck at {run.status!r} ({run.done}/{run.total})")
    finally:
        db.close()


def _run_row(run_id):
    db = SessionLocal()
    try:
        r = db.get(MediaRenameRun, run_id)
        return r.status, r.done, r.total, json.loads(r.results_json), json.loads(r.errors_json), r.resumed_count
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")


# ── the engine ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_run_goes_through_every_item_in_chunks_and_keeps_the_proposals(client, seed, monkeypatch):
    chat = _Chat()
    monkeypatch.setattr(ai_module, "generate_chat", chat)
    ids = _clips(seed, [f"track_{i}" for i in range(14)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    run = await _await_status(run_id, ("done", "error"))
    assert run.status == "done", run.error
    status, done, total, results, errors, _ = _run_row(run_id)
    assert (done, total, len(results)) == (14, 14, 14) and errors == []
    assert len(chat.calls) == 3, "6 + 6 + 2 items per model call"
    assert [r["old"] for r in results] == [f"track_{i}" for i in range(14)]
    assert all(r["changed"] and r["new"].startswith("New ") for r in results)


@pytest.mark.asyncio
async def test_pictures_are_processed_in_smaller_chunks(client, seed, monkeypatch):
    chat = _Chat()
    monkeypatch.setattr(ai_module, "generate_chat", chat)
    ids = _clips(seed, [f"VID_{i}" for i in range(7)], kind="video")
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("video", ids), {"pictures": True})
    await _await_status(run_id, ("done", "error"))
    assert _run_row(run_id)[1] == 7
    assert jobs.chunk_size({"pictures": True}) == 3 and jobs.chunk_size({}) == 6


@pytest.mark.asyncio
async def test_progress_is_saved_after_every_chunk(client, seed, monkeypatch):
    gate = asyncio.Event()
    monkeypatch.setattr(ai_module, "generate_chat", _Chat(gate=gate))
    ids = _clips(seed, [f"a_{i}" for i in range(13)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    deadline = time.time() + 5
    while _run_row(run_id)[1] < 6 and time.time() < deadline:
        await asyncio.sleep(0.02)
    status, done, total, results, _, _ = _run_row(run_id)
    assert status == "running" and done == 6 and len(results) == 6, "the first chunk is already saved while the second waits"
    gate.set()
    await _await_status(run_id, ("done",))
    assert len(_run_row(run_id)[3]) == 13


@pytest.mark.asyncio
async def test_cancelling_keeps_what_was_done_and_stops_asking(client, seed, monkeypatch):
    gate = asyncio.Event()
    chat = _Chat(gate=gate)
    monkeypatch.setattr(ai_module, "generate_chat", chat)
    ids = _clips(seed, [f"a_{i}" for i in range(13)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    while _run_row(run_id)[1] < 6:
        await asyncio.sleep(0.02)
    assert jobs.cancel_run(run_id) is True
    run = await _await_status(run_id, ("cancelled",))
    gate.set()
    await asyncio.sleep(0.1)
    status, done, total, results, _, _ = _run_row(run_id)
    assert status == "cancelled" and done == 6 and len(results) == 6
    assert len(chat.calls) == 2, "nothing was asked after the cancel"
    assert jobs.cancel_run(run_id) is False and jobs.cancel_run(424242) is False


@pytest.mark.asyncio
async def test_a_failed_chunk_is_reported_and_the_run_carries_on(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "generate_chat", _Chat(fail_on_call=2))
    ids = _clips(seed, [f"a_{i}" for i in range(12)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    await _await_status(run_id, ("done", "error"))
    status, done, total, results, errors, _ = _run_row(run_id)
    assert status == "done" and done == 12 and len(results) == 12
    assert errors and "backend is down" in errors[0]
    assert sum(1 for r in results if r["new"] == "") == 6, "the failed chunk's items just have no proposal"


@pytest.mark.asyncio
async def test_an_unexpected_crash_ends_the_run_with_the_reason(client, seed, monkeypatch):
    from app import media_rename

    async def boom(*a, **k):
        raise KeyError("surprise")
    monkeypatch.setattr(media_rename, "suggest", boom)
    ids = _clips(seed, ["a"])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    run = await _await_status(run_id, ("error",))
    assert "surprise" in run.error


@pytest.mark.asyncio
async def test_create_run_validates(client, seed):
    with pytest.raises(ValueError):
        jobs.create_run(seed.world_a.id, seed.gm.id, [], {})
    with pytest.raises(ValueError):
        jobs.create_run(seed.world_a.id, seed.gm.id, [{"kind": "audio", "id": 1}] * (jobs.MAX_RUN_ITEMS + 1), {})


@pytest.mark.asyncio
async def test_options_are_kept_and_unknown_ones_dropped(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "generate_chat", _Chat())
    ids = _clips(seed, ["a"])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids),
                             {"preset": "short", "style": "x" * 900, "pictures": 1, "listen": 0, "evil": "<script>"})
    db = SessionLocal()
    try:
        opts = json.loads(db.get(MediaRenameRun, run_id).options_json)
    finally:
        db.close()
    assert opts == {"preset": "short", "style": "x" * 400, "pictures": True, "listen": False}
    await _await_status(run_id, ("done",))


# ── restart, shutdown, deletion ───────────────────────────────────────────────────────────────────────────

def _seed_run(seed, n=14, done=6, status="running", resumed=0):
    ids = _clips(seed, [f"a_{i}" for i in range(n)])
    db = SessionLocal()
    try:
        results = [{"kind": "audio", "id": i, "url": None, "old": f"a_{k}", "new": f"Kept {k}", "changed": True, "basis": [], "generic": True, "note": ""}
                   for k, i in enumerate(ids[:done])]
        run = MediaRenameRun(world_id=seed.world_a.id, created_by_user_id=seed.gm.id, status=status, options_json="{}",
                             items_json=json.dumps(_refs("audio", ids)), results_json=json.dumps(results), errors_json="[]",
                             total=n, done=done, resumed_count=resumed)
        db.add(run)
        db.commit()
        return run.id, ids
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_restart_marks_the_run_interrupted_and_resume_continues_where_it_stopped(client, seed, monkeypatch):
    chat = _Chat()
    monkeypatch.setattr(ai_module, "generate_chat", chat)
    run_id, ids = _seed_run(seed, n=14, done=6, status="running")
    jobs.sweep_interrupted_runs()
    assert _run_row(run_id)[0] == "interrupted"
    assert jobs.resume_interrupted_runs() == 1
    await _await_status(run_id, ("done",))
    status, done, total, results, _, resumed = _run_row(run_id)
    assert (done, len(results), resumed) == (14, 14, 1)
    assert [r["new"] for r in results[:6]] == [f"Kept {k}" for k in range(6)], "finished chunks are not redone"
    assert len(chat.calls) == 2, "only the remaining 8 items were asked about (6 + 2)"


@pytest.mark.asyncio
async def test_a_run_that_keeps_getting_interrupted_gives_up(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "generate_chat", _Chat())
    run_id, _ = _seed_run(seed, status="interrupted", resumed=job_shutdown.MAX_AUTO_RESUMES)
    assert jobs.resume_interrupted_runs() == 0
    assert _run_row(run_id)[0] == "error"


@pytest.mark.asyncio
async def test_a_corrupt_stored_run_does_not_block_boot(client, seed):
    run_id, _ = _seed_run(seed, status="interrupted")
    db = SessionLocal()
    try:
        db.get(MediaRenameRun, run_id).items_json = "{not json"
        db.commit()
    finally:
        db.close()
    assert jobs.resume_interrupted_runs() == 0
    assert _run_row(run_id)[0] == "error"


@pytest.mark.asyncio
async def test_shutdown_helpers(client, seed, monkeypatch):
    gate = asyncio.Event()
    monkeypatch.setattr(ai_module, "generate_chat", _Chat(gate=gate))
    ids = _clips(seed, [f"a_{i}" for i in range(13)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    while _run_row(run_id)[1] < 6:
        await asyncio.sleep(0.02)
    assert len(jobs.live_tasks()) == 1
    jobs.mark_stragglers_interrupted()
    assert _run_row(run_id)[0] == "interrupted"
    job_shutdown.request_stop()
    try:
        jobs.cancel_run(run_id)
        await asyncio.sleep(0.1)
    finally:
        job_shutdown.clear_stop()
    gate.set()
    await asyncio.sleep(0.05)
    assert _run_row(run_id)[0] == "interrupted", "a shutdown cancel leaves the run resumable, not cancelled"


@pytest.mark.asyncio
async def test_delete_run_only_when_finished(client, seed, monkeypatch):
    gate = asyncio.Event()
    monkeypatch.setattr(ai_module, "generate_chat", _Chat(gate=gate))
    ids = _clips(seed, [f"a_{i}" for i in range(13)])
    run_id = jobs.create_run(seed.world_a.id, seed.gm.id, _refs("audio", ids), {})
    while _run_row(run_id)[1] < 6:
        await asyncio.sleep(0.02)
    assert jobs.delete_run(run_id) is False
    gate.set()
    await _await_status(run_id, ("done",))
    assert jobs.delete_run(run_id) is True and jobs.delete_run(run_id) is False


def test_the_chunk_runner_takes_its_turn_in_the_ai_queue():
    assert getattr(jobs._process_chunk, "_ai_serialized", False)


# ── summary and select ──────────────────────────────────────────────────────────────────────────────────────

def test_summary_counts_what_still_needs_a_name_per_kind(client, seed):
    _gm(client, seed)
    _clips(seed, ["track_0043", "Ember Waltz", "Recording 12"])
    _clips(seed, ["VID_0001"], kind="video")
    _clips(seed, ["Other World Clip"], world=seed.world_b)
    d = client.get("/api/media-rename/summary").json()
    assert d["audio"] == {"total": 3, "needs_work": 2} and d["video"] == {"total": 1, "needs_work": 1}
    assert d["image"] == {"total": 0, "needs_work": 0}


def test_select_returns_every_remaining_item_not_just_a_page(client, seed):
    _gm(client, seed)
    ids = _clips(seed, [f"track_{i:04d}" for i in range(40)] + ["Ember Waltz"])
    r = client.post("/api/media-rename/select", json={"kinds": ["audio"], "scope": "needs_work"})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 40 and len(d["items"]) == 40 and d["capped"] is False
    assert d["items"][0] == {"kind": "audio", "id": ids[0]} or d["items"][0]["kind"] == "audio"
    assert client.post("/api/media-rename/select", json={"kinds": ["audio"], "scope": "all"}).json()["count"] == 41


def test_select_filters_by_kind_album_and_scope(client, seed):
    _gm(client, seed)
    db = SessionLocal()
    try:
        album = AudioAlbum(world_id=seed.world_a.id, name="Tavern")
        db.add(album)
        db.commit()
        album_id = album.id
    finally:
        db.close()
    _clips(seed, ["track_1", "track_2"], album_id=album_id)
    _clips(seed, ["track_3"])
    _clips(seed, ["VID_1"], kind="video")
    sel = lambda **kw: client.post("/api/media-rename/select", json=kw).json()
    assert sel(kinds=["audio"], album=album_id)["count"] == 2
    assert sel(kinds=["audio", "video"])["count"] == 4
    assert sel(kinds=["video"])["items"][0]["kind"] == "video"
    assert sel(kinds=[], scope="needs_work")["count"] == 0
    assert client.post("/api/media-rename/select", json={"kinds": ["nonsense"]}).status_code == 403
    assert client.post("/api/media-rename/select", json={"kinds": "audio"}).status_code == 400


def test_select_caps_the_list_and_says_so(client, seed, monkeypatch):
    _gm(client, seed)
    monkeypatch.setattr(jobs, "MAX_RUN_ITEMS", 5)
    _clips(seed, [f"track_{i}" for i in range(9)])
    d = client.post("/api/media-rename/select", json={"kinds": ["audio"]}).json()
    assert len(d["items"]) == 5 and d["count"] == 9 and d["capped"] is True


def test_select_images_that_need_work_skips_used_and_titled_ones(client, seed):
    _gm(client, seed)
    from app.main import UPLOADS_DIR
    import io
    from PIL import Image

    def png(name):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "PNG")
        (UPLOADS_DIR / name).write_bytes(buf.getvalue())
        return "/uploads/" + name
    generic, used, titled = png("a1b2c3d4e5f6-img-2041.png"), png("a1b2c3d4e5f6-IMG_0001.png"), png("a1b2c3d4e5f6-dsc01234.png")
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Kaelen", image_url=used))
        db.add(ImageAlbum(world_id=seed.world_a.id, name="Dumps", image_urls_json=json.dumps([generic, titled])))
        db.add(MediaTitle(world_id=seed.world_a.id, url=titled, title="Harbour at Dusk"))
        db.commit()
    finally:
        db.close()
    d = client.post("/api/media-rename/select", json={"kinds": ["image"]}).json()
    assert d["items"] == [{"kind": "image", "url": generic}]
    assert client.get("/api/media-rename/summary").json()["image"] == {"total": 3, "needs_work": 1}


def test_select_and_summary_respect_permissions(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/media-rename/summary").status_code == 403
    assert client.post("/api/media-rename/select", json={"kinds": ["audio"]}).status_code == 403


# ── the run routes ────────────────────────────────────────────────────────────────────────────────────────

def _poll(client, run_id, want=("done", "error", "cancelled"), timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/media-rename/runs/{run_id}").json()
        if d["status"] in want:
            return d
        time.sleep(0.03)
    raise AssertionError(f"run stuck: {d}")


def test_start_a_run_poll_it_and_read_the_proposals(client, seed, monkeypatch):
    _gm(client, seed)
    monkeypatch.setattr(ai_module, "generate_chat", _Chat())
    ids = _clips(seed, [f"track_{i}" for i in range(8)])
    r = client.post("/api/media-rename/runs", json={"items": _refs("audio", ids), "preset": "short"})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    d = _poll(client, run_id)
    assert d["status"] == "done" and d["done"] == 8 and d["total"] == 8 and len(d["results"]) == 8
    assert d["results"][0]["old"] == "track_0" and d["results"][0]["new"].startswith("New ")
    listing = client.get("/api/media-rename/runs").json()["runs"]
    assert listing[0]["id"] == run_id and listing[0]["status"] == "done" and listing[0]["proposals"] == 8
    assert "results" not in listing[0], "the list stays light"


def test_run_requests_are_checked(client, seed, monkeypatch):
    _gm(client, seed)
    ids = _clips(seed, ["a"])
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "")
    assert client.post("/api/media-rename/runs", json={"items": _refs("audio", ids)}).status_code == 400
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")
    assert client.post("/api/media-rename/runs", json={"items": []}).status_code == 400
    assert client.post("/api/media-rename/runs", json={"items": "x"}).status_code == 400
    assert client.post("/api/media-rename/runs", json={"items": [{"kind": "nonsense", "id": 1}]}).status_code == 403
    assert client.post("/api/media-rename/runs", json={"items": ["junk"]}).status_code == 403
    assert client.post("/api/media-rename/runs", data="x", headers={"content-type": "application/json"}).status_code == 400
    too_many = [{"kind": "audio", "id": ids[0]}] * (jobs.MAX_RUN_ITEMS + 1)
    assert client.post("/api/media-rename/runs", json={"items": too_many}).status_code == 400


def test_runs_belong_to_their_world_and_need_permission(client, seed, monkeypatch):
    _gm(client, seed)
    monkeypatch.setattr(ai_module, "generate_chat", _Chat())
    ids = _clips(seed, ["a"])
    run_id = client.post("/api/media-rename/runs", json={"items": _refs("audio", ids)}).json()["id"]
    _poll(client, run_id)
    client.cookies.set("active_world", seed.world_b.slug)
    assert client.get(f"/api/media-rename/runs/{run_id}").status_code == 404
    assert client.get("/api/media-rename/runs").json()["runs"] == []
    assert client.post(f"/api/media-rename/runs/{run_id}/cancel").status_code == 404
    assert client.post(f"/api/media-rename/runs/{run_id}/delete").status_code == 404
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for method, path in (("get", "/api/media-rename/runs"), ("get", f"/api/media-rename/runs/{run_id}"),
                         ("post", "/api/media-rename/runs"), ("post", f"/api/media-rename/runs/{run_id}/delete")):
        assert getattr(client, method)(path, **({"json": {}} if method == "post" else {})).status_code == 403, path


def test_cancel_and_delete_over_http(client, seed, monkeypatch):
    _gm(client, seed)
    gate = asyncio.Event()
    monkeypatch.setattr(ai_module, "generate_chat", _Chat(gate=gate))
    ids = _clips(seed, [f"a_{i}" for i in range(13)])
    run_id = client.post("/api/media-rename/runs", json={"items": _refs("audio", ids)}).json()["id"]
    deadline = time.time() + 5
    while client.get(f"/api/media-rename/runs/{run_id}").json()["done"] < 6 and time.time() < deadline:
        time.sleep(0.03)
    assert client.post(f"/api/media-rename/runs/{run_id}/delete").status_code == 400, "still running"
    assert client.post(f"/api/media-rename/runs/{run_id}/cancel").status_code == 200
    d = _poll(client, run_id, want=("cancelled",))
    assert d["done"] == 6 and len(d["results"]) == 6
    assert client.post(f"/api/media-rename/runs/{run_id}/cancel").status_code == 400, "nothing left to cancel"
    assert client.post(f"/api/media-rename/runs/{run_id}/delete").status_code == 200
    assert client.get(f"/api/media-rename/runs/{run_id}").status_code == 404


def test_applying_a_runs_proposals_removes_them_from_the_run(client, seed, monkeypatch):
    _gm(client, seed)
    monkeypatch.setattr(ai_module, "generate_chat", _Chat())
    ids = _clips(seed, ["track_0", "track_1", "track_2"])
    run_id = client.post("/api/media-rename/runs", json={"items": _refs("audio", ids)}).json()["id"]
    d = _poll(client, run_id)
    first, second = d["results"][0], d["results"][1]
    r = client.post("/api/media-rename/apply", json={"run_id": run_id, "renames": [
        {"kind": "audio", "id": first["id"], "name": first["new"]}, {"kind": "audio", "id": second["id"], "name": "Edited By Hand"}]})
    assert r.status_code == 200 and r.json()["applied"] == 2
    left = client.get(f"/api/media-rename/runs/{run_id}").json()
    assert [x["id"] for x in left["results"]] == [d["results"][2]["id"]]
    db = SessionLocal()
    try:
        assert db.get(AudioClip, second["id"]).name == "Edited By Hand"
    finally:
        db.close()
    # applying the last one empties a finished run, and an empty finished run is removed
    third = left["results"][0]
    client.post("/api/media-rename/apply", json={"run_id": run_id, "renames": [{"kind": "audio", "id": third["id"], "name": "Third"}]})
    assert client.get(f"/api/media-rename/runs/{run_id}").status_code == 404


def test_a_bad_run_id_on_apply_is_harmless(client, seed):
    _gm(client, seed)
    ids = _clips(seed, ["track_0"])
    for run_id in (999999, "abc", None):
        r = client.post("/api/media-rename/apply", json={"run_id": run_id, "renames": [{"kind": "audio", "id": ids[0], "name": f"Name {run_id}"}]})
        assert r.status_code == 200


def test_world_delete_removes_runs(client, seed):
    _gm(client, seed)
    run_id, _ = _seed_run(seed, status="done", done=14)
    assert client.post(f"/worlds/{seed.world_a.id}/delete").status_code in (200, 303)
    db = SessionLocal()
    try:
        assert db.query(MediaRenameRun).count() == 0
    finally:
        db.close()


def test_the_runner_is_in_the_shutdown_and_startup_hooks():
    import inspect
    from app import main
    startup, shutdown = inspect.getsource(main._startup_tasks), inspect.getsource(main._shutdown_tasks)
    for needle in ("_media_rename_jobs.sweep_interrupted_runs", "_media_rename_jobs.resume_interrupted_runs"):
        assert needle in startup, needle
    for needle in ("_media_rename_jobs.live_tasks", "_media_rename_jobs.mark_stragglers_interrupted"):
        assert needle in shutdown, needle


def test_the_run_routes_are_not_an_ai_task_path():
    """A run is its own durable job (it saves progress and survives a restart) - it must not also be wrapped in the
    request-level background-task middleware."""
    from app import ai_background
    for path in ("/api/media-rename/runs", "/api/media-rename/select", "/api/media-rename/summary"):
        assert not ai_background.TASK_PATHS.match(path), path
