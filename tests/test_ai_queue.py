"""AI tasks run one at a time (app/ai_queue.py).

One Studio on one GPU: two model jobs at once fight over VRAM and each takes longer than both would back to back.
Every background AI task — the 202+poll tasks (app/ai_background.py), the durable job engines (audio, chat, image,
video) and the in-memory job runners (character, quests, map, cockpit find, …) — takes its turn in ONE first-come
queue, so they finish one by one. Interactive streams (live chat) are not queued."""
import asyncio
import importlib
import time

import pytest

from app import ai as ai_module
from app import ai_background, ai_queue

from .conftest import GM_PASSWORD, login

BG = {"X-ND-Background": "1"}


@pytest.fixture(autouse=True)
def _clean():
    ai_queue.queue.reset()
    ai_background._TASKS.clear()
    yield
    ai_queue.queue.reset()
    ai_background._TASKS.clear()


# ── the queue itself ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runs_one_at_a_time_in_arrival_order():
    q = ai_queue.AiQueue()
    order, running, peak = [], 0, 0

    async def job(n):
        nonlocal running, peak
        async with q.slot(f"job {n}"):
            running += 1
            peak = max(peak, running)
            order.append(n)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(job(n) for n in range(6)))
    assert peak == 1
    assert order == list(range(6))


@pytest.mark.asyncio
async def test_a_waiter_that_is_cancelled_does_not_block_the_rest():
    q = ai_queue.AiQueue()
    done = []
    hold = asyncio.Event()

    async def holder():
        async with q.slot("holder"):
            await hold.wait()

    async def waiter(name):
        async with q.slot(name):
            done.append(name)

    h = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)
    a, b, c = (asyncio.ensure_future(waiter(n)) for n in "abc")
    await asyncio.sleep(0.01)
    b.cancel()
    hold.set()
    await asyncio.gather(h, a, c)
    with pytest.raises(asyncio.CancelledError):
        await b
    assert done == ["a", "c"]


@pytest.mark.asyncio
async def test_a_failing_or_cancelled_holder_hands_the_turn_on():
    q = ai_queue.AiQueue()
    got = []

    async def boom():
        async with q.slot("boom"):
            raise RuntimeError("model fell over")

    async def hang():
        async with q.slot("hang"):
            await asyncio.sleep(30)

    async def after(n):
        async with q.slot(n):
            got.append(n)

    with pytest.raises(RuntimeError):
        await boom()
    h = asyncio.ensure_future(hang())
    await asyncio.sleep(0.01)
    nxt = asyncio.ensure_future(after("next"))
    await asyncio.sleep(0.01)
    assert got == []
    h.cancel()
    await asyncio.wait_for(nxt, 2)
    assert got == ["next"]


@pytest.mark.asyncio
async def test_position_and_snapshot_describe_the_line():
    q = ai_queue.AiQueue()
    hold = asyncio.Event()
    tickets = {}

    async def job(name):
        async with q.slot(name) as t:
            tickets[name] = t
            await hold.wait()

    first = asyncio.ensure_future(job("first"))
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(job("second"))
    third = asyncio.ensure_future(job("third"))
    await asyncio.sleep(0.01)
    snap = q.snapshot()
    assert snap["running"]["label"] == "first"
    assert [w["label"] for w in snap["waiting"]] == ["second", "third"]
    assert q.waiting_count() == 2
    hold.set()
    await asyncio.gather(first, second, third)
    assert q.snapshot() == {"running": None, "waiting": []}


@pytest.mark.asyncio
async def test_state_from_a_dead_event_loop_never_blocks_a_new_one():
    """Tests (and a reloaded server) get a fresh loop; a ticket stranded by the old loop must not hold the line."""
    q = ai_queue.AiQueue()
    stale_loop = asyncio.new_event_loop()
    q._loop = stale_loop                        # as if a previous loop had taken the slot and died with it
    q._running = ai_queue.Ticket("stranded", "")
    q._waiting.append(ai_queue.Ticket("also stranded", ""))
    async with q.slot("fresh"):
        pass
    stale_loop.close()


@pytest.mark.asyncio
async def test_a_stuck_holder_is_cancelled_so_the_line_moves(monkeypatch):
    monkeypatch.setattr(ai_queue, "MAX_HOLD_SECONDS", 0.05)
    monkeypatch.setattr(ai_queue, "_WATCH_INTERVAL", 0.02)
    q = ai_queue.AiQueue()
    reaped = []

    async def stuck():
        try:
            async with q.slot("stuck"):
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            reaped.append(True)
            raise

    s = asyncio.ensure_future(stuck())
    await asyncio.sleep(0.01)
    async with q.slot("waiting"):
        pass
    assert reaped == [True]
    with pytest.raises(asyncio.CancelledError):
        await s


@pytest.mark.asyncio
async def test_serialized_decorator_queues_and_is_detectable():
    q_before = ai_queue.queue
    seen, running, peak = [], 0, 0

    @ai_queue.serialized("unit")
    async def work(n):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        seen.append(n)
        return n * 2

    assert getattr(work, "_ai_serialized", False)
    assert await asyncio.gather(*(work(n) for n in range(4))) == [0, 2, 4, 6]
    assert peak == 1 and seen == [0, 1, 2, 3]
    assert ai_queue.queue is q_before


# ── the 202+poll tasks line up ──────────────────────────────────────────────────────────────────

def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _status(client, tid):
    return client.get(f"/api/ai/tasks/{tid}").json()


def _wait(client, tid, want, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        d = _status(client, tid)
        if d["status"] in want:
            return d
        time.sleep(0.03)
    raise AssertionError(f"task stayed {_status(client, tid)['status']!r}, wanted {want}")


@pytest.fixture()
def gated_model(monkeypatch):
    state = {"running": 0, "peak": 0, "started": [], "gates": {}}

    async def fake(messages, system="", model="", options=None, think=False, format=None):
        name = messages[-1]["content"]
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        state["started"].append(name)
        try:
            gate = state["gates"].get(name)
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(0.05)
        finally:
            state["running"] -= 1
        return f"answer to {name}"

    monkeypatch.setattr(ai_module, "generate_chat", fake)
    return state


def _post(client, name):
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": name}]}, headers=BG)
    assert r.status_code == 202, r.text
    return r.json()["task_id"]


def test_background_tasks_wait_their_turn_and_say_where_they_are(client, seed, gated_model):
    _gm(client, seed)
    gated_model["gates"] = {n: asyncio.Event() for n in ("one", "two", "three")}
    a = _post(client, "one")
    _wait(client, a, {"running"})
    b = _post(client, "two")
    c = _post(client, "three")
    db_, dc = _status(client, b), _status(client, c)
    assert (db_["status"], db_["position"]) == ("queued", 1)
    assert (dc["status"], dc["position"]) == ("queued", 2)
    assert gated_model["started"] == ["one"], "a second model call started while the first was still running"
    # release them one by one: each starts only after the previous finished
    for name, tid, nxt in (("one", a, b), ("two", b, c), ("three", c, None)):
        gated_model["gates"][name].set()
        assert _wait(client, tid, {"done"})["body"] == {"result": f"answer to {name}"}
        if nxt:
            _wait(client, nxt, {"running", "done"})
    assert gated_model["peak"] == 1
    assert gated_model["started"] == ["one", "two", "three"]


def test_cancelling_a_queued_task_just_removes_it_from_the_line(client, seed, gated_model):
    _gm(client, seed)
    gated_model["gates"] = {n: asyncio.Event() for n in ("one", "two", "three")}
    a = _post(client, "one")
    _wait(client, a, {"running"})
    b = _post(client, "two")
    c = _post(client, "three")
    assert client.delete(f"/api/ai/tasks/{b}").status_code == 200
    assert _wait(client, b, {"cancelled"})["status"] == "cancelled"
    assert _status(client, c)["position"] == 1
    gated_model["gates"]["one"].set()
    gated_model["gates"]["three"].set()
    _wait(client, c, {"done"})
    assert gated_model["started"] == ["one", "three"], "the cancelled task must never reach the model"


def test_queued_tasks_count_towards_the_per_user_limit(client, seed, gated_model, monkeypatch):
    monkeypatch.setattr(ai_background, "MAX_RUNNING_PER_USER", 3)
    _gm(client, seed)
    gated_model["gates"] = {n: asyncio.Event() for n in ("a", "b", "c", "d")}
    ids = [_post(client, n) for n in ("a", "b", "c")]
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "d"}]}, headers=BG)
    assert r.status_code == 429
    for t in ids:
        client.delete(f"/api/ai/tasks/{t}")


# ── every background AI runner takes its turn ───────────────────────────────────────────────────

@pytest.mark.parametrize("module, name", [
    ("app.audio_jobs", "_run_job"), ("app.chat_jobs", "_run_job"),
    ("app.image_jobs", "_run_job"), ("app.video_jobs", "_run_job"),
    ("app.main", "_map_ai_markers_task"), ("app.main", "_schematic_ai_build_task"),
    ("app.routers.ai", "_auto_tag_task"), ("app.routers.quests", "_quests_suggest_task"),
    ("app.routers.character_ai", "_pc_ai_task"), ("app.routers.character_ai", "_analysis_task"),
    ("app.routers.template_ai", "_draft_task"), ("app.routers.cockpit", "_cockpit_find_task"),
])
def test_every_background_ai_runner_goes_through_the_queue(module, name):
    fn = getattr(importlib.import_module(module), name)
    assert getattr(fn, "_ai_serialized", False), f"{module}.{name} runs outside the AI queue"


# ── the browser helper keeps waiting while queued ───────────────────────────────────────────────

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_helper_keeps_polling_while_queued_and_reports_it():
    from .test_ai_background import _HARNESS
    scenario = {
        "start": {"status": 202, "body": '{"task_id":"abc"}', "headers": {"X-ND-Task": "abc"}},
        "polls": [{"json": {"status": "queued", "position": 2, "elapsed": 1}},
                  {"json": {"status": "queued", "position": 1, "elapsed": 2}},
                  {"json": {"status": "running", "elapsed": 3}},
                  {"json": {"status": "done", "http_status": 200, "content_type": "application/json", "body": {"ok": 1}}}],
    }
    env = dict(os.environ, SCENARIO=json.dumps(scenario), HELPER=str(ROOT / "static" / "js" / "nd-ai-task.js"))
    res = subprocess.run(["node", "-e", _HARNESS], capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["status"] == 200 and json.loads(out["body"]) == {"ok": 1}
    assert out["progress"][:3] == ["queued", "queued", "running"]
