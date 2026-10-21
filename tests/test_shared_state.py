"""Live state shared between processes (app/shared_state.py; docs/RUST_REWRITE_PLAN.md, phase 0): counters written by one background writer,
read from a small refreshed copy, the second screen's stage as a compare-and-swap JSON value, and a second OS process sharing all of it."""
import os
import subprocess
import sys
import threading
from pathlib import Path

from sqlalchemy import text

from app import live, shared_state
from app.database import engine
from app.routers import display

ROOT = Path(__file__).resolve().parent.parent


def _row(key):
    with engine.connect() as c:
        return c.execute(text("SELECT n FROM shared_state WHERE key = :k"), {"k": key}).scalar()


def test_a_bump_is_seen_at_once_and_written_soon(client):
    before = live.version(4001)
    for _ in range(5):
        live.touch(4001)
    assert live.version(4001) == before + 5, "this process sees its own bumps immediately"
    assert shared_state.flush(5)
    assert _row("live:4001") == before + 5, "and the table has them"
    assert live.version(4001) == before + 5, "without counting them twice"


def test_signals_are_per_world_and_global(client):
    live.signal(4002, "dice")
    live.signal(4002, "dice")
    live.signal_all("spot")
    sig = live.signals(4002)
    assert sig["dice"] == (0, 2) and sig["spot"][0] >= 1
    assert "dice" not in live.signals(4003)
    assert live.signals(4003)["spot"] == sig["spot"], "the every-world counter reaches every world"


def test_a_second_process_shares_the_counters(client):
    base = live.version(4004)
    live.touch(4004)
    assert shared_state.flush(5)
    code = ("from app import live, shared_state\n"
            "print(live.version(4004))\n"
            "live.touch(4004); live.touch(4004); live.signal(4004, 'combat')\n"
            "assert shared_state.flush(10)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=60)
    assert out.returncode == 0, out.stderr[-600:]
    assert int(out.stdout.split()[-1]) == base + 1, "the other process read what this one wrote"
    shared_state.refresh(force=True)
    assert live.version(4004) == base + 3, "and this process reads what the other one wrote"
    assert live.signals(4004)["combat"] == (0, 1)


def test_the_stream_sees_a_change_made_by_another_process(client):
    import asyncio

    async def go():
        gen = live.stream_world_events(4005)
        await asyncio.wait_for(gen.__anext__(), 2)                          # retry hint
        first = await asyncio.wait_for(gen.__anext__(), 2)
        base = int(first.split("data: ")[1])
        with engine.begin() as c:                                           # what a Rust server's bump looks like
            c.execute(text("INSERT INTO shared_state(key, n) VALUES ('live:4005', :n) ON CONFLICT(key) DO UPDATE SET n = :n"), {"n": base + 7})
        nxt = await asyncio.wait_for(gen.__anext__(), 5)
        assert nxt == f"event: version\ndata: {base + 7}\n\n"
        await gen.aclose()
    asyncio.run(go())


def test_a_failed_write_is_queued_again_not_lost(client, monkeypatch):
    real = shared_state.engine
    calls = {"n": 0}

    class Flaky:
        def begin(self):
            calls["n"] += 1
            if calls["n"] <= 6:
                from sqlalchemy.exc import OperationalError
                raise OperationalError("x", {}, Exception("database is locked"))
            return real.begin()
        connect = real.connect
    monkeypatch.setattr(shared_state, "engine", Flaky())
    before = live.version(4006)
    live.touch(4006)
    assert shared_state._idle.wait(0.3) is False, "not written yet"
    assert live.version(4006) == before + 1, "but not forgotten"
    assert shared_state.flush(15)
    monkeypatch.setattr(shared_state, "engine", real)
    assert _row("live:4006") == before + 1


def test_the_stage_is_one_value_shared_by_every_writer(client):
    display.reset_stage()
    errors = []

    def put(i):
        try:
            for j in range(5):
                display.set_stage(4007, {"kind": "image", "url": f"/uploads/{i}-{j}.png", "title": "t"})
        except Exception as e:                                              # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=put, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    st = display.get_state(4007)
    assert st["seq"] == 20 and len(st["recent"]) == display.RECENT_LIMIT and st["current"]["id"] == 20, "no two writers got the same sequence number"
    assert display.clear_stage(4007)["current"] is None and display.get_state(4007)["seq"] == 21
    display.reset_stage(4007)
    assert display.get_state(4007) == {"seq": 0, "current": None, "recent": []}


def test_the_stage_is_in_the_table_in_the_documented_shape(client):
    display.reset_stage()
    display.set_stage(4008, {"kind": "image", "url": "/uploads/a.png", "title": "A"})
    with engine.connect() as c:
        key, blob, n = c.execute(text("SELECT key, value, n FROM shared_state WHERE key = 'stage:4008'")).fetchone()
    import json
    assert key == "stage:4008" and n == 1 and json.loads(blob)["current"]["url"] == "/uploads/a.png"
    assert not any(k.startswith("stage:") for k in shared_state.snapshot("stage:")), "the stage is not a counter"
