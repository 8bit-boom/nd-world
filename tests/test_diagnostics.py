"""app.diagnostics — the stall/pool watchers that capture evidence when the
single-worker event loop wedges. These tests drive the watchdog's state
machine directly (fabricated heartbeat/pool state + a tmp diagnostics dir)
rather than actually stalling a loop: the point is that a stall produces a
dump file containing the evidence a post-mortem needs (pool status, task
snapshot, thread stacks), that dumps are rate-limited, and that recovery
and restarts are journaled. The lifecycle roundtrip test covers the real
start()/stop() wiring main.py's lifespan uses."""
import asyncio
import time

import pytest

import app.diagnostics as diag

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _reset():
    """Pristine watcher state — module globals the white-box tests below
    fabricate. Never touches _thread (the daemon thread, once started,
    lives for the whole pytest process; _ready=False keeps it no-op)."""
    diag._heartbeat_task = None
    diag._ready = False
    diag._stall_since = None
    diag._last_dump = None
    diag._last_pool_warn = None
    diag._pool_saturated = False
    diag._task_snapshot = []
    diag._last_beat = time.monotonic()


def _events(tmp_path) -> str:
    p = tmp_path / "events.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_disabled_start_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DISABLED", "1")
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    thread_before = diag._thread  # earlier files' app boots may have started
    diag.start()                  # the daemon thread already — disabled must
                                  # not spawn a NEW one, not demand none exist
    assert diag._thread is thread_before
    assert diag._heartbeat_task is None
    assert not list(tmp_path.iterdir())


def test_stall_writes_dump_and_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    diag._task_snapshot = ["Task-1: fake_coro at app/example.py:42 in fake_fn"]
    now = time.monotonic()
    diag._ready = True
    diag._last_beat = now - 30.0  # 30s stale — past the 15s default threshold
    diag._evaluate(now)

    dumps = list(tmp_path.glob("wedge-*.txt"))
    assert len(dumps) == 1
    text = dumps[0].read_text(encoding="utf-8")
    assert "event-loop-stall" in text
    assert "event loop stalled for: 30.0s" in text
    assert "sqlalchemy pool:" in text
    assert "fake_coro" in text          # the loop-side task snapshot made it in
    assert "== all thread stacks ==" in text

    events = _events(tmp_path)
    assert "loop-stalled" in events
    assert "dump-written" in events
    assert "reason=event-loop-stall" in events


def test_stall_dump_is_cooldown_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    monkeypatch.setenv("ND_DIAG_DUMP_COOLDOWN_SECONDS", "0")  # every call dumps
    _reset()
    now = time.monotonic()
    diag._ready = True
    diag._last_beat = now - 20.0
    diag._evaluate(now)
    diag._evaluate(now + 0.1)
    # Cooldown 0 → two evaluations, two DISTINCT dumps (same-second name
    # collision disambiguated, not overwritten)
    assert len(list(tmp_path.glob("wedge-*.txt"))) == 2
    assert _events(tmp_path).count("loop-stalled") == 1  # once per episode


def test_stall_recovery_is_journaled(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    now = time.monotonic()
    diag._ready = True
    diag._last_beat = now - 20.0
    diag._evaluate(now)          # episode opens
    diag._last_beat = now + 1.0  # heart beats again
    diag._evaluate(now + 2.0)    # ...and the next check sees it
    assert "loop-recovered" in _events(tmp_path)
    # A second healthy check must not re-close a closed episode
    diag._evaluate(now + 4.0)
    assert _events(tmp_path).count("loop-recovered") == 1


def test_healthy_beat_never_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    diag._ready = True
    diag._evaluate(time.monotonic())  # _last_beat set fresh by _reset
    assert not list(tmp_path.glob("wedge-*.txt"))
    assert _events(tmp_path) == ""


def test_not_ready_never_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    diag._last_beat = time.monotonic() - 999.0  # ancient, but never beaten
    diag._evaluate(time.monotonic())
    assert not list(tmp_path.iterdir())


def test_first_dump_not_suppressed_on_fresh_boot(tmp_path, monkeypatch):
    """Regression: the cooldown sentinels used to be 0.0, and on a freshly
    booted CI runner (or a watchtower-recreated container) time.monotonic()
    stays under 300 for the first minutes of the process's life —
    `now - 0.0 < cooldown` then read as 'still cooling down' and the FIRST
    dump/warn of the whole episode was silently skipped. CI caught it; the
    same window exists in production after every container recreation."""
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    now = 50.0  # a plausible monotonic() reading seconds after machine boot
    diag._ready = True
    diag._last_beat = now - 30.0
    diag._evaluate(now)
    assert len(list(tmp_path.glob("wedge-*.txt"))) == 1
    assert "loop-stalled" in _events(tmp_path)
    monkeypatch.setattr(diag, "_pool_counts", lambda: (5, 5))
    diag._pool_check(now + 1.0)
    assert "pool-saturated" in _events(tmp_path)


def test_pool_saturation_warns_and_dumps(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    monkeypatch.setattr(diag, "_pool_counts", lambda: (5, 5))
    _reset()
    diag._ready = True
    diag._pool_check(time.monotonic())
    events = _events(tmp_path)
    assert "pool-saturated" in events
    assert "checked_out=5" in events
    dumps = list(tmp_path.glob("wedge-*.txt"))
    assert len(dumps) == 1
    assert "pool-saturated" in dumps[0].read_text(encoding="utf-8")
    # Within the warn cooldown a second check is silent (match the event
    # line's " pool-saturated checked_out=" — the plain word also appears
    # in the dump-written line's reason= field from the first check)
    diag._pool_check(time.monotonic() + 1.0)
    assert _events(tmp_path).count(" pool-saturated checked_out=") == 1


def test_pool_below_size_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    monkeypatch.setattr(diag, "_pool_counts", lambda: (2, 5))
    _reset()
    diag._ready = True
    diag._pool_check(time.monotonic())
    assert _events(tmp_path) == ""
    assert not list(tmp_path.glob("wedge-*.txt"))


def test_pool_counts_introspects_the_real_pool():
    """Regression: SQLAlchemy 2.x Pool.checkedout() returns an int, and
    len()-ing it raised TypeError — caught, turned into None, and silently
    disabled the pool watcher everywhere (the live Settings tab showed
    'DB pool right now: unavailable'). Must always introspect for real."""
    counts = diag._pool_counts()
    assert counts is not None
    checked_out, size = counts
    assert isinstance(checked_out, int) and isinstance(size, int)
    assert size >= 1
    assert 0 <= checked_out <= size + 10  # +10: the pool's overflow headroom


def test_pool_recovery_is_journaled_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    monkeypatch.setattr(diag, "_pool_counts", lambda: (5, 5))
    _reset()
    diag._ready = True
    diag._pool_check(time.monotonic())            # saturates: warns + dumps
    assert "pool-saturated" in _events(tmp_path)
    monkeypatch.setattr(diag, "_pool_counts", lambda: (2, 5))
    diag._pool_check(time.monotonic() + 1.0)      # pressure clears → recovery
    diag._pool_check(time.monotonic() + 2.0)      # stays healthy — one line
    assert _events(tmp_path).count("pool-recovered") == 1
    # And re-saturating re-warns (the flag flipped back, not latched shut)
    monkeypatch.setattr(diag, "_pool_counts", lambda: (5, 5))
    diag._last_pool_warn = None                  # outside the warn cooldown
    diag._pool_check(time.monotonic() + 3.0)
    assert _events(tmp_path).count(" pool-saturated checked_out=") == 2


def test_journal_line_format(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    diag._journal("startup", pid=123)
    line = _events(tmp_path).strip()
    assert line.endswith(" startup pid=123")
    # ISO timestamp prefix, UTC
    assert line[0].isdigit() and "T" in line.split()[0]


@pytest.mark.asyncio
async def test_start_stop_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    _reset()
    diag.start()
    try:
        assert diag._heartbeat_task is not None
        assert not diag._heartbeat_task.done()
        await asyncio.sleep(0.05)  # let the first beat land
        assert diag._ready
        diag._evaluate(time.monotonic())  # healthy → no dump from the check
        assert not list(tmp_path.glob("wedge-*.txt"))
    finally:
        await diag.stop()
    assert diag._heartbeat_task is None
    assert diag._ready is False
    events = _events(tmp_path)
    assert "startup" in events
    assert "shutdown" in events
    # stop() must have fully reaped the heartbeat task, not just cancelled
    # it — a pending-destroyed task would make every TestClient shutdown in
    # the suite emit a "Task was destroyed but it is pending" warning.
    await asyncio.sleep(0)


# ── Settings → Diagnostics tab + GM-only viewer routes ───────────────────────

def test_settings_diagnostics_tab_renders_journal_and_dumps(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    login(client, seed.gm.email, GM_PASSWORD)
    diag._journal("loop-stalled", seconds="20.0")
    (tmp_path / "wedge-20260925-000000.txt").write_text(
        "event-loop-stall\nevent loop stalled for: 20.0s", encoding="utf-8")
    r = client.get("/settings", params={"tab": "diagnostics"})
    assert r.status_code == 200
    assert "loop-stalled" in r.text                      # journal tail rendered
    assert "wedge-20260925-000000.txt" in r.text         # dump listed
    assert "/admin/diagnostics/dump/wedge-20260925-000000.txt" in r.text


def test_settings_diagnostics_tab_empty_state(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/settings", params={"tab": "diagnostics"})
    assert r.status_code == 200
    assert "No events recorded yet" in r.text
    assert "No stall or pool-saturation dumps" in r.text


def test_events_route_serves_full_journal(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    login(client, seed.gm.email, GM_PASSWORD)
    diag._journal("startup", pid=1)
    diag._journal("shutdown", pid=1)
    r = client.get("/admin/diagnostics/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "startup pid=1" in r.text
    assert "shutdown pid=1" in r.text


def test_dump_route_serves_only_valid_dump_files(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    login(client, seed.gm.email, GM_PASSWORD)
    (tmp_path / "wedge-20260925-000000.txt").write_text("stack evidence", encoding="utf-8")
    (tmp_path / "events.log").write_text("startup pid=1\n", encoding="utf-8")
    r = client.get("/admin/diagnostics/dump/wedge-20260925-000000.txt")
    assert r.status_code == 200
    assert "stack evidence" in r.text
    # A non-dump file in the same dir, an encoded traversal, and a
    # well-formed but nonexistent dump name all 404.
    assert client.get("/admin/diagnostics/dump/events.log").status_code == 404
    assert client.get("/admin/diagnostics/dump/..%2Fevents.log").status_code == 404
    assert client.get("/admin/diagnostics/dump/wedge-20990101-000000.txt").status_code == 404


def test_diagnostics_routes_are_gm_only(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_DIAG_DIR", str(tmp_path))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/admin/diagnostics/events").status_code == 403
    assert client.get("/admin/diagnostics/dump/wedge-20260925-000000.txt").status_code == 403
