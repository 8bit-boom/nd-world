"""The Rust gateway answers the same as the app directly (docs/RUST_GATEWAY.md). Runs only when the suite is pointed at a gateway
(ND_TEST_BASE_URL) and given the app's own address (ND_GOLDEN_DIRECT_URL):

    ND_TEST_BASE_URL=http://127.0.0.1:8952 ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8951 python -m pytest tests/test_gateway_golden.py"""
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "")
pytestmark = pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")

_spec = importlib.util.spec_from_file_location("golden", Path(__file__).resolve().parent.parent / "scripts" / "golden.py")
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


def _login(c, seed):
    r = c.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    c.cookies.set("active_world", seed.world_a.slug)
    return c


def test_pages_are_the_same_through_the_gateway_and_directly(client, seed):
    db = SessionLocal()
    try:
        for n in ("Ana", "Bo"):
            db.add(Entity(world_id=seed.world_a.id, kind="character", name=n, body="Hello **there**"))
        db.commit()
    finally:
        db.close()
    via = _login(client, seed)
    direct = _login(httpx.Client(base_url=DIRECT, timeout=120), seed)
    paths = golden.crawl(direct, limit=60)
    assert len(paths) >= 5
    res = golden.compare(direct, via, paths)
    assert not res["differ"], {k: v[:4] for k, v in list(res["differ"].items())[:3]}
    assert len(res["same"]) == len(paths)


def test_uploads_and_static_files_are_the_same_through_the_gateway(client, seed):
    from app.main import UPLOADS_DIR
    (UPLOADS_DIR / "p").mkdir(parents=True, exist_ok=True)
    body = bytes(range(256)) * 40
    (UPLOADS_DIR / "p" / "same.png").write_bytes(body)
    via = _login(client, seed)
    direct = _login(httpx.Client(base_url=DIRECT, timeout=120), seed)
    for path, headers in (("/uploads/p/same.png", {}), ("/uploads/p/same.png", {"Range": "bytes=100-199"}), ("/uploads/p/same.png", {"Range": "bytes=-50"}),
                          ("/static/style.css", {"Accept-Encoding": "identity"}), ("/static/css/nope.css", {}), ("/uploads/p/missing.png", {})):
        a, b = direct.get(path, headers=headers), via.get(path, headers=headers)
        assert (a.status_code, a.content) == (b.status_code, b.content), (path, headers)
        for h in ("content-type", "cache-control", "content-range", "content-disposition"):
            assert a.headers.get(h) == b.headers.get(h), (path, h, a.headers.get(h), b.headers.get(h))
    # a conditional request is answered 304 by both
    etag = via.get("/uploads/p/same.png").headers["etag"]
    assert via.get("/uploads/p/same.png", headers={"If-None-Match": etag}).status_code == 304
    assert direct.get("/uploads/p/same.png").status_code == 200
    # a stranger gets nothing through the gateway either
    anon = httpx.Client(base_url=str(client.base_url), timeout=60)
    r = anon.get("/uploads/p/same.png", follow_redirects=False)
    assert r.content != body and "x-nd-accel-path" not in r.headers


def test_the_login_page_the_gateway_builds_is_the_one_python_builds(client, seed):
    """GET /login is built by the gateway (ND_RUST_ROUTES=login): same page, same redirects, for every kind of `next` and for a signed-in browser."""
    direct = httpx.Client(base_url=DIRECT, timeout=60)
    anon_paths = ["/login", "/login?next=/dice", "/login?next=%2Fentity%2F3%3Fa%3D1%26b%3D2", "/login?next=//evil.example", "/login?next=https://evil.example",
                  "/login?next=/%5Cevil", "/login?next=", "/login?next=%20/padded%20", "/login?next=/a&next=/b", "/login?next=/q%22%3E%3Cb%3Ex%3C/b%3E",
                  "/login?next=/caf%C3%A9", "/login?next=/a+b", "/login?other=1"]
    for path in anon_paths:
        a, b = direct.get(path, follow_redirects=False), client.get(path, follow_redirects=False)
        assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location")), path
        assert golden.normalise(a.headers["content-type"], a.content) == golden.normalise(b.headers["content-type"], b.content), path
    # signed in: both send the browser on, to the same place
    via = _login(client, seed)
    dir_in = _login(httpx.Client(base_url=DIRECT, timeout=60), seed)
    for path in ("/login", "/login?next=/dice", "/login?next=//evil.example", "/login?next=/caf%C3%A9%20x"):
        a, b = dir_in.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
        assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location")), path
    # a login that fails still shows Python's page (the POST is never the gateway's)
    r = client.post("/login", data={"email": "nobody@test.local", "password": "x", "next": "/"})
    assert r.status_code == 400 and "Incorrect email or password" in r.text


# ── POST /api/dice/roll written by the gateway (ND_RUST_ROUTES=...,dice_roll) ─────────────────────────────────────────────────────────────

def _roll(c, body, *, w=None, headers=None):
    return c.post("/api/dice/roll" + (f"?w={w}" if w else ""), json=body, headers=headers or {})


def _members(seed):
    """(client factory per person): a GM, a player of world A, a player of world B only, nobody."""
    from .conftest import PLAYER_PASSWORD

    def as_(email, password, world=None):
        c = httpx.Client(base_url=DIRECT, timeout=60)
        r = c.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False)
        assert r.status_code == 303, (email, r.status_code)
        if world:
            c.cookies.set("active_world", world)
        return c
    return as_


def _via(direct_client, base):
    """The same person's session, sent through the gateway (the cookie is the proof of who they are, so it is simply reused)."""
    c = httpx.Client(base_url=base, timeout=60)
    for k, v in direct_client.cookies.items():
        c.cookies.set(k, v)
    return c


def test_a_roll_the_gateway_writes_is_the_roll_python_would_have_written(client, seed):
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    direct = httpx.Client(base_url=DIRECT, timeout=60)
    people = []
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = httpx.Client(base_url=DIRECT, timeout=60)
        assert d.post("/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False).status_code == 303
        d.cookies.set("active_world", world)
        people.append((email, d, _via(d, base)))
    bodies = [{"notation": "2d6+3"}, {"notation": "d20", "label": "Perception"}, {"notation": "  4d8+2d6+1  ", "label": " padded "}, {"notation": "1d20-1"},
              {"notation": "banana"}, {"notation": ""}, {"notation": "51d6"}, {"notation": "d6", "label": None}, {"notation": 5}, {}, {"notation": "d6", "label": "café"},
              {"notation": "2 6"}, {"notation": "+5"}, {"notation": "d100", "label": "x" * 200}]
    for email, d, via in people:
        for body in bodies:
            a, b = _roll(d, body), _roll(via, body)
            assert a.status_code == b.status_code, (email, body, a.status_code, b.status_code, a.text, b.text)
            if a.status_code != 200:
                assert a.json() == b.json(), (email, body)           # an error is Python's error word for word
                continue
            ja, jb = a.json(), b.json()
            assert set(ja) == set(jb) == {"id", "user_name", "notation", "label", "breakdown", "total", "created_at"}
            for k in ("user_name", "notation", "label"):
                assert ja[k] == jb[k], (email, body, k)
            assert [t["term"] for t in ja["breakdown"]] == [t["term"] for t in jb["breakdown"]]
            assert [set(t) for t in ja["breakdown"]] == [set(t) for t in jb["breakdown"]]
            assert jb["total"] == sum(t["sum"] for t in jb["breakdown"])
            assert len(ja["created_at"]) in (20, 27) and len(jb["created_at"]) in (20, 27) and jb["created_at"].endswith("Z")
            ascii_only = all(all(" " <= ch <= "~" for ch in str(body.get(k) or "")) for k in ("notation", "label"))
            assert (b.headers.get("x-nd-served-by") == "gateway") == ascii_only, ("who wrote it", email, body)    # non-ASCII text is left to Python
            assert a.headers.get("content-type") == b.headers.get("content-type")
    # what was stored reads back the same through Python's own history route, whoever wrote it
    hist = people[0][1].get("/api/dice/history")
    if hist.status_code == 200:
        rows = hist.json()
        rows = rows if isinstance(rows, list) else rows.get("rolls", [])
        assert rows and all(set(r) >= {"id", "user_name", "notation", "breakdown", "total", "created_at"} for r in rows)


def test_the_gateway_never_writes_for_someone_python_would_refuse(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.section_access_json = '{"dice": {"player": "none", "assistant": "none"}}'
        db.commit()
    finally:
        db.close()
    try:
        d = httpx.Client(base_url=DIRECT, timeout=60)
        assert d.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"}, follow_redirects=False).status_code == 303
        d.cookies.set("active_world", seed.world_a.slug)
        via = _via(d, base)
        import time
        time.sleep(0.2)
        a, b = _roll(d, {"notation": "d6"}), _roll(via, {"notation": "d6"})
        assert (a.status_code, a.json()) == (b.status_code, b.json()) and a.status_code == 403
        # a stranger without any session: Python's 401 through the gateway
        anon = httpx.Client(base_url=base, timeout=60)
        r = _roll(anon, {"notation": "d6"})
        assert r.status_code == 401 and r.json() == {"detail": "Login required"}
        # a player's cookie for a changed password (session_version) is refused by both
        from app.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == seed.player_a.id).first()
            u.session_version = (u.session_version or 1) + 1
            db.commit()
        finally:
            db.close()
        a, b = _roll(d, {"notation": "d6"}), _roll(via, {"notation": "d6"})
        assert a.status_code == b.status_code == 401, (a.status_code, b.status_code)
    finally:
        pass


def test_rolls_written_by_both_servers_at_once_all_land_and_the_live_counter_is_exact(client, seed):
    """Python and the gateway write the same SQLite file and the same live counter at the same moment: nothing is lost."""
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text
    from app.database import SessionLocal
    base = str(client.base_url)
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False).status_code == 303
    d.cookies.set("active_world", seed.world_a.slug)
    cookies = dict(d.cookies)

    def counts():
        db = SessionLocal()
        try:
            rolls = db.execute(text("SELECT count(*) FROM dice_rolls WHERE world_id = :w"), {"w": seed.world_a.id}).scalar()
            n = db.execute(text("SELECT n FROM shared_state WHERE key = :k"), {"k": f"sig:{seed.world_a.id}:dice"}).scalar() or 0
            return rolls, n
        finally:
            db.close()

    before_rolls, before_n = counts()

    def one(i):
        c = httpx.Client(base_url=base if i % 2 else DIRECT, timeout=60, cookies=cookies)
        r = c.post("/api/dice/roll", json={"notation": "d20"})
        return r.status_code, r.headers.get("x-nd-served-by")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, range(40)))
    assert all(code == 200 for code, _ in results), results
    assert sum(1 for _, by in results if by == "gateway") == 20, "half were written by the gateway"
    from .conftest import wait_until
    assert wait_until(lambda: counts() == (before_rolls + 40, before_n + 40)), counts()           # Python's counter is written by its background thread


# ── GET /api/live streamed by the gateway (ND_RUST_ROUTES=...,live) ──────────────────────────────────────────────────────────────────────

def _events(base, cookies, seconds, out):
    """Collect the SSE frames one client receives for `seconds`."""
    import time
    end = time.monotonic() + seconds
    try:
        with httpx.Client(base_url=base, cookies=cookies, timeout=seconds + 5) as c, c.stream("GET", "/api/live") as r:
            out.append(("status", r.status_code, r.headers.get("content-type"), r.headers.get("x-nd-served-by")))
            buf = ""
            for chunk in r.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    out.append(("frame", frame))
                if time.monotonic() > end:
                    break
    except httpx.ReadTimeout:
        pass


def test_the_live_stream_from_the_gateway_says_what_pythons_says(client, seed):
    import threading
    import time
    from app import shared_state
    base = str(client.base_url)
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False).status_code == 303
    d.cookies.set("active_world", seed.world_a.slug)
    cookies = dict(d.cookies)
    got_py, got_gw = [], []
    threads = [threading.Thread(target=_events, args=(DIRECT, cookies, 6, got_py)), threading.Thread(target=_events, args=(base, cookies, 6, got_gw))]
    for t in threads:
        t.start()
    time.sleep(2)
    shared_state.bump(f"live:{seed.world_a.id}")                     # a change in world A, as any route's live.touch makes it
    shared_state.bump(f"sig:{seed.world_a.id}:spot")                  # and a named signal
    shared_state.bump(f"live:{seed.world_b.id}")                      # a change elsewhere: neither stream may mention it
    shared_state.flush()
    for t in threads:
        t.join()
    assert got_py[0][:3] == got_gw[0][:3] == ("status", 200, "text/event-stream; charset=utf-8")
    assert got_gw[0][3] == "gateway" and got_py[0][3] is None
    frames = lambda got: [f for kind, *rest in got if kind == "frame" for f in rest]
    fp, fg = frames(got_py), frames(got_gw)
    assert fp[0] == fg[0] == "retry: 3000"
    versions = lambda fs: [f for f in fs if f.startswith("event: version")]
    signals = lambda fs: sorted(f for f in fs if f.startswith("event: signal"))
    assert versions(fp) == versions(fg) and len(versions(fp)) == 2, (fp, fg)
    assert signals(fp) == signals(fg) == ["event: signal\ndata: spot"], (fp, fg)


def test_the_live_stream_is_refused_through_the_gateway_as_python_refuses_it(client, seed):
    base = str(client.base_url)
    for c in (httpx.Client(base_url=DIRECT, timeout=10), httpx.Client(base_url=base, timeout=10)):
        r = c.get("/api/live")
        assert r.status_code == 401 and r.json() == {"detail": "Login required"}
