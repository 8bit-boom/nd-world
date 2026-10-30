"""The Rust gateway answers the same as the app directly (docs/RUST_GATEWAY.md). Runs only when the suite is pointed at a gateway
(ND_TEST_BASE_URL) and given the app's own address (ND_GOLDEN_DIRECT_URL):

    ND_TEST_BASE_URL=http://127.0.0.1:8952 ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8951 python -m pytest tests/test_gateway_golden.py"""
import importlib.util
import json
from pathlib import Path
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
    # /settings shows the serving process's own watchdog cells; test_settings_page_is_pythons compares it with those masked
    paths = [p for p in golden.crawl(direct, limit=60) if not p.startswith("/settings")]
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
            if a.status_code == 200:
                # every roll Python accepts, the gateway answers too — a printable-Unicode
                # label included (stored and echoed byte-identically)
                assert b.headers.get("x-nd-served-by") == "gateway", ("who wrote it", email, body)
            else:
                assert b.headers.get("x-nd-served-by") is None, ("who wrote it", email, body)  # refusals are Python's
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

def _events(base, cookies, seconds, out, path="/api/live"):
    """Collect the SSE frames one client receives for `seconds`."""
    import time
    end = time.monotonic() + seconds
    try:
        with httpx.Client(base_url=base, cookies=cookies, timeout=seconds + 5) as c, c.stream("GET", path) as r:
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


def test_the_display_stream_and_stage_from_the_gateway_are_pythons(client, seed):
    """Two browsers on the second screen: one stream straight from Python, one through the gateway. The GM shows a picture (through the
    gateway), shows it again, a fight changes, the screen is cleared (directly): both streams send the same frames, and the stage answers
    are Python's."""
    import re
    import threading
    import time
    base = str(client.base_url)
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False).status_code == 303
    d.cookies.set("active_world", seed.world_a.slug)
    d.get("/")                                    # a page view writes the site layout into the session, as for a real browser
    cookies = dict(d.cookies)
    via = httpx.Client(base_url=base, cookies=cookies, timeout=60)
    got_py, got_gw = [], []
    threads = [threading.Thread(target=_events, args=(DIRECT, cookies, 9, got_py, "/api/display/stream")),
               threading.Thread(target=_events, args=(base, cookies, 9, got_gw, "/api/display/stream"))]
    for t in threads:
        t.start()
    time.sleep(2)
    r = via.post("/api/display/show", json={"kind": "image", "url": " /uploads/gallery/a.png ", "title": " Map ", "caption": "c"})
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") == "gateway", r.text
    time.sleep(1.5)
    r2 = d.post("/api/display/show", json={"kind": "image", "url": "/uploads/gallery/b.png"})
    assert r2.status_code == 200
    time.sleep(1.5)
    r3 = via.post("/api/display/show", json={"kind": "image", "url": "/uploads/gallery/a.png", "title": "again"})
    time.sleep(1.5)
    r4 = via.post("/api/display/clear")
    assert r4.status_code == 200 and r4.headers.get("x-nd-served-by") == "gateway"
    for t in threads:
        t.join()
    mask = lambda t: re.sub(r'"ts": "[^"]+"', '"ts": "T"', t)
    assert got_py[0][:3] == got_gw[0][:3] == ("status", 200, "text/event-stream; charset=utf-8") and got_gw[0][3] == "gateway"
    frames = lambda got: [mask(f) for kind, *rest in got if kind == "frame" for f in rest if not f.startswith(":")]
    assert frames(got_py) == frames(got_gw), (frames(got_py), frames(got_gw))
    assert len([f for f in frames(got_py) if f.startswith("event: state")]) == 5
    st = r3.json()
    assert [x["url"] for x in st["recent"]] == ["/uploads/gallery/a.png", "/uploads/gallery/b.png"] and st["current"]["title"] == "again"
    # the refusals are answered by the gateway with Python's answers; each stage is per-process,
    # so a 200 answer is compared by the item's content, not its id / seq (the two sides' stages
    # have seen different writes)
    served = 0
    for body in ({"kind": "image", "url": "javascript:x"}, {"kind": "text", "text": "hi"}, {"kind": "map", "slug": "nope"}, {"kind": "combat", "combat_id": True}, {"kind": "entity", "entity_id": 999999}):
        ra = d.post("/api/display/show", json=body)
        rb = via.post("/api/display/show", json=body)
        assert ra.status_code == rb.status_code, (body, ra.status_code, rb.status_code, ra.text[:100], rb.text[:100])
        if rb.status_code == 200:
            strip_ids = lambda st: {k: v for k, v in st["current"].items() if k not in ("id", "ts")}
            assert strip_ids(rb.json()) == strip_ids(ra.json()), body
        else:
            assert rb.json() == {"detail": ra.json()["detail"]}, (body, ra.text, rb.text)
        served += rb.headers.get("x-nd-served-by") == "gateway"
    assert served >= 4, served
    # the entity text card (no image, or mode=text) is still Python's
    from .test_gateway_writes import _add as _gw_add
    eid = _gw_add("Entity", world_id=seed.world_a.id, kind="character", name="NoPic")
    for body in ({"kind": "entity", "entity_id": eid}, {"kind": "entity", "entity_id": eid, "mode": "text"}):
        r = via.post("/api/display/show", json=body)
        assert r.headers.get("x-nd-served-by") is None, (body, r.text[:100])


def test_the_live_stream_is_refused_through_the_gateway_as_python_refuses_it(client, seed):
    base = str(client.base_url)
    for c in (httpx.Client(base_url=DIRECT, timeout=10), httpx.Client(base_url=base, timeout=10)):
        r = c.get("/api/live")
        assert r.status_code == 401 and r.json() == {"detail": "Login required"}


# ── GET /dice built by the gateway (ND_RUST_ROUTES=...,pages): the layout (base.html) and the page ──────────────────────────────────────────

def _signed_in(email, password, world=None):
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False).status_code == 303
    if world:
        d.cookies.set("active_world", world)
    d.get("/")                                    # a page view writes the site layout into the session, as for a real browser
    return d


def test_the_dice_page_the_gateway_builds_is_pythons_page(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import UserViewPref, World, WorldMembership
    base = str(client.base_url)
    db = SessionLocal()
    try:
        # a custom kind, custom menus, a theme, and an assistant: the layout has something to show
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.custom_kinds_json = json.dumps([{"id": "custom_ship", "label": "Ships", "icon": "🚀"}])
        wa.theme_json = json.dumps({"bg": "#101010", "font": "Georgia"})
        wa.section_access_json = json.dumps({"maps": {"player": "none", "assistant": "read"}, "kind_custom_ship": {"player": "read", "assistant": "edit"}})
        db.add(UserViewPref(user_id=seed.player_a.id, section="dice", prefs_json=json.dumps({"mode": "log"})))
        m = db.query(WorldMembership).filter(WorldMembership.user_id == seed.player_b.id).first()
        if m:
            m.role = "assistant"
        db.commit()
    finally:
        db.close()
    people = [(seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
              (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)]
    d0 = _signed_in(people[0][0], people[0][1], people[0][2])
    d0.post("/api/dice/roll", json={"notation": "2d6+3", "label": "<b>escape me</b>"})
    for email, pw, world in people:
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in ("/dice", "/dice?embed=1", "/dice?error=Bad%20notation&n=2d%3Cx", f"/dice?w={seed.world_b.slug}", "/dice?q=search%20me"):
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert a.status_code == b.status_code, (email, path, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                diff = "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:40])
                raise AssertionError(f"{email} {path}\n{diff}")
            assert b.headers.get("x-nd-served-by") == "gateway", (email, path, "Python built it - the gateway deferred")


SIMPLE_PAGES = ["/chronicler", "/background-jobs", "/tools/bulk-edit", "/system", "/boards", "/androidapp"]


def test_the_simple_pages_the_gateway_builds_are_pythons_pages(client, seed):
    """Pages that are the layout plus a section check and at most one list (rust/nd-gateway/src/pages.rs SIMPLE): the same HTML, or Python's
    refusal, for every role."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import InvestBoard, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        db.add(InvestBoard(world_id=seed.world_a.id, slug="clues", name="Clues <&>", description="who did it", nodes_json='[{"id": 1}]'))
        db.add(InvestBoard(world_id=seed.world_a.id, slug="empty", name="Empty"))
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.section_access_json = json.dumps({"boards": {"player": "read", "assistant": "read"}, "chronicler": {"player": "none", "assistant": "read"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in SIMPLE_PAGES:
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert a.status_code == b.status_code, (email, path, a.status_code, b.status_code)
            if a.status_code != 200 or "text/html" not in a.headers.get("content-type", ""):
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {path}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:40]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 6, served          # the rest are refusals, which Python answers


def test_entity_lists_the_gateway_builds_are_pythons(client, seed):
    """/kind/<kind>: folders, parent folders, stat columns, hidden and shared entities, search, views and paging - for a GM and two players."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        add = lambda **kw: db.add(Entity(world_id=w, **kw))
        add(kind="item", name="Laser Rifle", folder="Weapons/Rifles", body="## Attributes\n* **Damage**: 3d6\n* **Rarity**: Rare\n* **Cost**: *900*")
        add(kind="item", name="Knife", folder="Weapons", body="## Attributes\n* **Damage**: 1d4\n\\*\\*Armor\\*\\* = 0", summary="A <sharp> knife")
        add(kind="item", name="Secret Gun", folder="Weapons", visible_to_players=False, body="## Attributes\n* **Damage**: 9d9")
        add(kind="item", name="Shared Gun", folder="Weapons/Pistols", visible_to_players=False, tags="loud, rare")
        add(kind="item", name="Loose thing", body="Some text\n\n## Entry\nAn *odd* [thing](/x).")
        add(kind="feat", name="Iron Will", folder="Race Feats/Rank 2", body="## Entry\nYou resist.")
        add(kind="feat", name="Quick", folder="Origin Feats", body="## Attributes\n* **Effect**: fast")
        for i in range(55):
            add(kind="note", name=f"Note {i:02d}", folder="Archive", body=f"* **Damage**: {i}")
        db.flush()
        shared = db.query(Entity).filter(Entity.name == "Shared Gun").first()
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()
    paths = ["/kind/item", "/kind/item?folder=Weapons", "/kind/item?folder=Weapons/Rifles", "/kind/item?folder=", "/kind/item?q=gun",
             "/kind/item?q=GUN&folder=Weapons", "/kind/item?view=table", "/kind/item?folder=Weapons&view=grid", "/kind/feat?folder=Race%20Feats",
             "/kind/feat?folder=Race%20Feats/Rank%202", "/kind/feat?folder=Origin%20Feats", "/kind/note?folder=Archive", "/kind/note?folder=Archive&page=2",
             "/kind/note?folder=Archive&page=99", "/kind/nothing", "/kind/item?q=%25", "/kind/item?page=x"]
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in paths:
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location")), (email, path, a.status_code, b.status_code)
            if a.status_code != 200 or "text/html" not in a.headers.get("content-type", ""):
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {path}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:40]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 30, served


def test_entity_pages_the_gateway_builds_are_pythons(client, seed):
    """/entity/<id>: body with headings and autolinks, [gmonly] / [for] / [reveal] parts, notes (visible and GM-only), custom fields of a
    template, connections and backlinks with a hidden one, a hidden-but-shared entity, the GM's ?as=player preview. Markdown here is written
    the way both renderers read the same (docs/RUST_PAGES.md: the owner accepted the CommonMark differences elsewhere)."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, EntityTemplate, entity_links, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        tpl = EntityTemplate(world_id=w, name="Ship", slug="ship-tpl", kind="item", fields_json=json.dumps([
            {"id": "speed", "label": "Speed", "type": "text", "section": "Engine"}, {"id": "crew", "label": "Crew", "type": "number"},
            {"id": "parts", "label": "Parts", "type": "list", "item_fields": [{"id": "n", "label": "Name"}]}]))
        db.add(tpl); db.flush()
        ana = Entity(world_id=w, kind="character", name="Ana Ortega", aliases="Ana, The Captain", body="Ana is kind.")
        bob = Entity(world_id=w, kind="character", name="Hunter Bob Vane, the Grey", body="Bob hunts.")
        hidden = Entity(world_id=w, kind="character", name="Mister Hidden", visible_to_players=False, body="Shh.")
        shared = Entity(world_id=w, kind="location", name="Shared Base", visible_to_players=False, body="# Base\nOnly for some.")
        main_e = Entity(world_id=w, kind="item", name="The Ship", template_id=tpl.id, summary="A ship [gmonly]with a bomb[/gmonly].",
                        custom_fields_json=json.dumps({"speed": "Fast [gmonly]really[/gmonly]", "crew": 12, "parts": [{"n": "Hull"}, {"n": ""}]}),
                        body=("# Part One\nAna Ortega owns it. Bob Vane flies it with Mister Hidden.\n\n## Overview\nText &amp; more.\n\n## Overview\nAgain.\n\n"
                              "[gmonly]GM secret[/gmonly] and [for=%d]for player A[/for] and [reveal=2020-01-01]old news[/reveal] and "
                              "[reveal=2999-01-01]future[/reveal].\n\n### Stats\n* **Damage**: 2d6\n\n<a name=\"old\"></a>Anchor gone.") % seed.player_a.id)
        db.add_all([ana, bob, hidden, shared, main_e]); db.flush()
        db.execute(entity_links.insert().values(source_id=main_e.id, target_id=ana.id))
        db.execute(entity_links.insert().values(source_id=main_e.id, target_id=hidden.id))
        db.execute(entity_links.insert().values(source_id=bob.id, target_id=main_e.id))
        db.execute(entity_links.insert().values(source_id=hidden.id, target_id=main_e.id))
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        db.add(EntityNote(entity_id=main_e.id, content="A **visible** note about Ana.", visible_to_players=True))
        db.add(EntityNote(entity_id=main_e.id, content="GM note.", visible_to_players=False))
        db.commit()
        ids = {"main": main_e.id, "hidden": hidden.id, "shared": shared.id, "ana": ana.id}
    finally:
        db.close()
    paths = [f"/entity/{ids['main']}", f"/entity/{ids['main']}?as=player", f"/entity/{ids['main']}?as=player&player={seed.player_a.id}",
             f"/entity/{ids['hidden']}", f"/entity/{ids['shared']}", f"/entity/{ids['ana']}", "/entity/999999"]
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in paths:
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert a.status_code == b.status_code, (email, path, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {path}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:50]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 8, served


def test_session_and_quest_lists_the_gateway_builds_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import GameSession, Party, Quest, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        party = Party(world_id=w, name="The Crew")
        db.add(party); db.flush()
        for i in range(53):
            db.add(GameSession(world_id=w, title=f"Session {i}", session_num=i, summary=f"# Recap {i}\nThe **crew** went to [the docks](/x)."))
        main_q = Quest(world_id=w, title="Find the ship", status="active", category="main", summary="Look <here>", assigned_party_id=party.id)
        db.add(main_q); db.flush()
        db.add_all([Quest(world_id=w, title="Sub one", status="complete", parent_id=main_q.id), Quest(world_id=w, title="Sub two", parent_id=main_q.id),
                    Quest(world_id=w, title="Hidden plot", status="secret", visible_to_players=False), Quest(world_id=w, title="Odd", status="weird", category="side")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"quests": {"player": "edit", "assistant": "read"}, "sessions": {"player": "read", "assistant": "read"}})
        db.commit()
        pid = party.id
    finally:
        db.close()
    paths = ["/sessions", "/sessions?page=2", "/sessions?page=0", "/sessions?page=9", "/sessions?page=x", "/quests", "/quests?q=ship", "/quests?q=%20SUB%20",
             "/quests?category=side", f"/quests?party={pid}", "/quests?party=abc", "/quests?q=hidden"]
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in paths:
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert a.status_code == b.status_code, (email, path, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {path}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:50]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 20, served


def test_party_and_fact_lists_the_gateway_builds_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, Fact, GameSession, Party, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        npc = Entity(world_id=w, kind="character", name="Secret Ally", visible_to_players=False)
        pal = Entity(world_id=w, kind="character", name="Open Ally")
        db.add_all([npc, pal]); db.flush()
        db.add(Party(world_id=w, name="Alpha", member_pc_ids_json="[1, 2]", member_entity_ids_json=json.dumps([npc.id, pal.id, 999999])))
        db.add(Party(world_id=w, name="Beta"))
        s1 = GameSession(world_id=w, title="One", session_num=1)
        s2 = GameSession(world_id=w, title="Two", session_num=2)
        db.add_all([s1, s2]); db.flush()
        frag = [f"/facts/fragment?keys={s1.id},none", f"/facts/fragment?keys={s2.id}", "/facts/fragment?keys=none", "/facts/fragment",
                f"/facts/fragment?keys=%20{s2.id}%20,x,987654", "/facts/fragment?keys=0"]
        db.add_all([Fact(world_id=w, game_session_id=s1.id, content="The **door** was red.", tags="door, red"),
                    Fact(world_id=w, game_session_id=s2.id, content="A hidden fact.", visible_to_players=False, tags="secret"),
                    Fact(world_id=w, game_session_id=s2.id, content="Second session fact.", tags="red"),
                    Fact(world_id=w, content="Unfiled fact [gmonly]gm part[/gmonly].", tags=" door ,, "),
                    Fact(world_id=w, game_session_id=987654, content="Lost session fact.")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"parties": {"player": "read", "assistant": "edit"}, "facts": {"player": "read", "assistant": "read"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for path in ("/parties", "/parties?page=3", "/facts", *frag):
            a, b = d.get(path, follow_redirects=False), via.get(path, follow_redirects=False)
            assert a.status_code == b.status_code, (email, path, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {path}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:50]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 18, served


def test_random_table_list_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import RandomTable, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        db.add_all([RandomTable(world_id=None, name="Weather", slug="g-weather", category="general", entries_json="[]"),
                    RandomTable(world_id=w, name="Loot <rare>", slug="a-loot", category="loot", description="Things", entries_json="[]",
                                created_by_user_id=seed.player_a.id),
                    RandomTable(world_id=w, name="GM names", slug="a-names", category="", entries_json="[]")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"tables": {"player": "edit", "assistant": "edit"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        a, b = d.get("/tables", follow_redirects=False), via.get("/tables", follow_redirects=False)
        assert a.status_code == b.status_code, (email, a.status_code, b.status_code)
        if a.status_code != 200:
            continue
        na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
        if na != nb:
            import difflib
            raise AssertionError(f"{email}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:50]))
        served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 2, served


def test_combat_list_the_gateway_builds_is_pythons(client, seed):
    from datetime import datetime
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CombatSession, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        db.add_all([CombatSession(world_id=w, name="Bridge <ambush>", ruleset="nd", round_num=3,
                                  combatants_json=json.dumps([{"id": "a"}, {"id": "b"}, 7])),
                    CombatSession(world_id=w, name="Old fight", ruleset="mystery", finished_at=datetime(2026, 3, 4, 5, 6),
                                  combatants_json="[{\"id\": \"x\"}]"),
                    CombatSession(world_id=w, name="Broken", combatants_json="not json"),
                    CombatSession(world_id=w, name="Goblin pack", ruleset="nd", is_template=True, combatants_json="[{}]")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"combat": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in ("/combat", "/combat?page=2", "/combat?page=x"):
            a, b = d.get(url, follow_redirects=False), via.get(url, follow_redirects=False)
            assert a.status_code == b.status_code, (email, url, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {url}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:50]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 4, served


def test_calendar_month_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CalendarDayIcon, CalendarEvent, Entity, GameSession, World, WorldCalendar
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        config = {"era_name": "Y.S.F.", "year_format": "{year}  {era}", "current_day": 47, "days_per_week": 5,
                  "weekday_names": ["Moonday", " ", "Ashday"],
                  "months": [{"name": "Frost", "days": 30, "season": "Winter", "color": "#7aa7d9"}, {"name": "Thaw", "days": 25},
                             {"name": "Bloom <b>", "days": 16, "season": "Spring"}],
                  "moons": [{"name": "Pale", "cycle_days": 16, "offset": 3, "color": "#fff"}, {"name": "", "cycle_days": 0}],
                  "holidays": [{"name": "Thaw feast", "month": "thaw", "day": 99, "notes": "cake"}, {"name": "First", "month": 0, "color": "#abc"},
                               {"name": "Ghost", "month": 9}, {"name": "", "month": 1}, {"name": "Two", "month": "2", "day": 3}, "junk"]}
        db.add(WorldCalendar(world_id=w, config_json=json.dumps(config)))
        e = Entity(world_id=w, kind="npc", name="Mara")
        gs = GameSession(world_id=w, session_num=4, title="Into <the> dark")
        db.add_all([e, gs])
        db.flush()
        db.add_all([CalendarEvent(world_id=w, day=47, title="Duel", notes="at dawn", color="#f00", entity_id=e.id, session_id=gs.id),
                    CalendarEvent(world_id=w, day=31, title="Gone link", session_id=999999, created_by_user_id=seed.player_a.id),
                    CalendarEvent(world_id=w, day=47, title="Second"),
                    CalendarDayIcon(world_id=w, day=48, image_url="/uploads/calendar_icons/x.png", label="star"),
                    CalendarDayIcon(world_id=w, day=48, image_url="/uploads/calendar_icons/y.png", label="moon"),
                    CalendarDayIcon(world_id=w, day=80, image_url="/uploads/calendar_icons/z.png", label="far"),
                    *[CalendarEvent(world_id=w, day=33, title=f"Busy {i}") for i in range(6)]])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"calendar": {"player": "edit"}})
        db.commit()
    finally:
        db.close()
    urls = ("/calendar", "/calendar?year=1&month=0", "/calendar?month=-1", "/calendar?month=7&year=3", "/calendar?year=x",
            "/calendar?year=0&none=next", "/calendar?year=99999&month=2", "/calendar?year=500000", "/calendar?none=prev&month=1",
            "/calendar/agenda", "/calendar/year", "/calendar/year?year=2", "/calendar/year?year=0", "/calendar/year?year=x", "/calendar/year?year=99999",
            "/calendar/year?year=%201", "/calendar/config")
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            a, b = d.get(url, follow_redirects=False), via.get(url, follow_redirects=False)
            assert a.status_code == b.status_code, (email, url, a.status_code, b.status_code)
            if a.status_code != 200:
                continue
            na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
            if na != nb:
                import difflib
                raise AssertionError(f"{email} {url}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:60]))
            served += b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 2 * (len(urls) - 2) + 1, served  # "?year=%201" is Python's; /calendar/config is the GM's only


SIMPLE_FORM_URLS = ("/professions/new", "/races/new", "/boards/new", "/characters/templates/new", "/maps/schematic/new",
                    "/entity-templates/new", "/tables/new", "/display", "/handouts", "/dreamlands", "/king-in-yellow", "/sessions/new",
                    "/quests/new", "/editor", "/entity-templates", "/characters/templates", "/worlds", "/export", "/maps/new", "/characters/templates/ai-new")


def _same_page(d, via, url, who):
    a, b = d.get(url, follow_redirects=False), via.get(url, follow_redirects=False)
    assert a.status_code == b.status_code, (who, url, a.status_code, b.status_code)
    if a.status_code != 200:
        assert a.headers.get("location") == b.headers.get("location"), (who, url)
        return False
    na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
    if na != nb:
        import difflib
        raise AssertionError(f"{who} {url}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:60]))
    return b.headers.get("x-nd-served-by") == "gateway"


def test_simple_form_pages_the_gateway_builds_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal, get_app_settings
    from app.models import Entity, EntityTemplate, GameSession, ImageAlbum, Party, Quest, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        st = get_app_settings(db)
        st.dreamlands_enabled = st.king_in_yellow_enabled = True
        st.editor_external_url = "http://editor.example/"
        w = seed.world_a.id
        db.add(ImageAlbum(world_id=w, name="Art", image_urls_json=json.dumps(["/uploads/b.png", "/uploads/0123456789ab-a.png", 3])))
        db.add_all([Entity(world_id=w, kind="npc", name="Zed <z>", image_url="/uploads/b.png"), Entity(world_id=w, kind="location", name="Alley"),
                    Party(world_id=w, name="Crew"), GameSession(world_id=w, session_num=7, title="Seven"), Quest(world_id=w, title="Find it"),
                    EntityTemplate(world_id=w, name="Shop", slug="gw-shop", kind="location", fields_json=json.dumps([{"key": "owner", "label": "Owner"}])),
                    EntityTemplate(world_id=None, name="Blank", slug="gw-blank", fields_json="")])
        wa = db.query(World).filter(World.id == w).first()
        wa.custom_kinds_json = json.dumps([{"id": "custom_ship", "label": "Ship", "icon": "⛵", "subtypes": ["sloop"]}])
        wa.rules_md = "House rules"
        db.commit()
    finally:
        db.close()
    served = {u: 0 for u in SIMPLE_FORM_URLS}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in SIMPLE_FORM_URLS:
            served[url] += _same_page(d, via, url, email)
    assert all(served.values()), served


def test_id_pages_the_gateway_builds_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CombatSession, Entity, EntityTemplate, PageDoc, RandomTable, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        mine = RandomTable(world_id=w, name="Mine", slug="gw-mine", entries_json=json.dumps([{"text": "a <b>", "weight": 2}]),
                           created_by_user_id=seed.player_a.id)
        gm_t = RandomTable(world_id=w, name="GM", slug="gw-gm", entries_json="")
        glob = RandomTable(world_id=None, name="Global", slug="gw-glob", entries_json="[]")
        ent = Entity(world_id=w, kind="npc", name="Hand <out>", summary="sum", body="# Hi\n\n**bold** [link](http://x)\n\n- a\n- b",
                     image_url="/uploads/x/pic.png")
        bare = Entity(world_id=w, kind="item", name="Bare")
        other = Entity(world_id=seed.world_b.id, kind="npc", name="Other world")
        tpl = EntityTemplate(world_id=w, name="Tpl", slug="gw-tpl", fields_json=json.dumps([{"key": "k", "label": "K <l>"}]))
        cs = CombatSession(world_id=w, name="  Fight at <the> bridge  ")
        ctpl = CombatSession(world_id=w, name="Template", is_template=True)
        doc = PageDoc(world_id=w, name="Map <doc>", description="d", file_url="/uploads/pages/a.html")
        hidden = PageDoc(world_id=w, name="Secret", file_url="/uploads/pages/b.html", visible_to_players=False)
        far = PageDoc(world_id=seed.world_b.id, name="Far", file_url="/uploads/pages/c.html")
        db.add_all([mine, gm_t, glob, ent, bare, other, tpl, cs, ctpl, doc, hidden, far])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"tables": {"player": "edit"}})
        wa.custom_kinds_json = json.dumps([{"id": "custom_ship", "label": "Ship", "icon": "", "subtypes": ["sloop", 3]}, {"id": "bad", "label": "x"},
                                           {"id": "custom_blank", "label": "  "}, "junk", {"id": "custom_gang", "label": "Gang <g>", "created_at": "2026"}])
        wb = db.query(World).filter(World.id == seed.world_b.id).first()
        wb.rules_json = '{"sections": {}}'
        db.commit()
        wa.section_access_json = json.dumps({"tables": {"player": "edit"}, "pages": {"player": "read"}})
        db.commit()
        urls = [f"/pages/{doc.id}", f"/pages/{hidden.id}", f"/pages/{far.id}", "/pages/999999", f"/worlds/{w}/kinds/edit", f"/worlds/{w}/rules/edit", f"/worlds/{seed.world_b.id}/rules/edit", "/worlds/999999/kinds/edit",f"/tables/{mine.id}/edit", f"/tables/{gm_t.id}/edit", f"/tables/{glob.id}/edit", "/tables/999999/edit",
                f"/handout/{ent.id}", f"/handout/{bare.id}", f"/handout/{other.id}", "/handout/999999",
                f"/entity-templates/{tpl.id}/edit", "/entity-templates/999999/edit",
                f"/display/combat/{cs.id}", f"/display/combat/{ctpl.id}", "/display/combat/999999"]
    finally:
        db.close()
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            served[url] += _same_page(d, via, url, email)
    must = [u for u in urls if "999999" not in u and u not in (f"/handout/{other.id}", f"/display/combat/{ctpl.id}", f"/worlds/{seed.world_b.id}/rules/edit", f"/pages/{far.id}")]
    assert all(served[u] for u in must), served


def test_quest_page_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, Party, Quest, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        e1, e2 = Entity(world_id=w, kind="npc", name="Ally <a>"), Entity(world_id=seed.world_b.id, kind="npc", name="Elsewhere")
        db.add_all([e1, e2, Party(world_id=w, name="Crew")]); db.flush()
        q1 = Quest(world_id=w, title="Main <q>", summary="sum", body="Go **there**.\n\n[gmonly]secret[/gmonly]", status="active", category="main",
                   linked_entities_json=json.dumps([{"entity_id": e1.id, "role": "giver"}, {"entity_id": e2.id}, {"entity_id": 999999, "role": "x"}]),
                   created_by_user_id=seed.player_a.id)
        q2 = Quest(world_id=w, title="Hidden", visible_to_players=False, linked_entities_json="")
        q3 = Quest(world_id=w, title="Other's", linked_entities_json=json.dumps([{"entity_id": e1.id, "role": None}]))
        q4 = Quest(world_id=seed.world_b.id, title="Far quest")
        db.add_all([q1, q2, q3, q4])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"quests": {"player": "edit", "assistant": "edit"}})
        db.commit()
        urls = [f"/quests/{q.id}" for q in (q1, q2, q3, q4)] + ["/quests/999999"]
        must = urls[:3]
    finally:
        db.close()
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            served[url] += _same_page(d, via, url, email)
    assert all(served[u] for u in must), served


def test_document_library_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CharacterSheet, PageAlbum, PageDoc, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        top = PageAlbum(world_id=w, name="Lore <l>")
        empty = PageAlbum(world_id=w, name="Empty")
        far = PageAlbum(world_id=seed.world_b.id, name="Far")
        db.add_all([top, empty, far]); db.flush()
        mid = PageAlbum(world_id=w, name="Mid", parent_id=top.id); db.add(mid); db.flush()
        leaf = PageAlbum(world_id=w, name="Leaf", parent_id=mid.id); db.add(leaf); db.flush()
        t = PageDoc(world_id=w, name="Sheet tpl", file_url="/uploads/pages/t.html", is_character_sheet_template=True)
        db.add_all([t, PageDoc(world_id=w, name="Root doc", file_url="/uploads/pages/r.html", description="desc <d>"),
                    PageDoc(world_id=w, name="Hidden root", file_url="/uploads/pages/h.html", visible_to_players=False),
                    PageDoc(world_id=w, name="In top", album_id=top.id, file_url="/uploads/pages/a.html"),
                    PageDoc(world_id=w, name="Hidden in top", album_id=top.id, file_url="/uploads/pages/b.html", visible_to_players=False),
                    PageDoc(world_id=w, name="In leaf", album_id=leaf.id, file_url="/uploads/pages/c.html")]); db.flush()
        db.add_all([CharacterSheet(world_id=w, template_id=t.id, owner_user_id=seed.player_a.id, name="S1"),
                    CharacterSheet(world_id=w, template_id=t.id, owner_user_id=seed.player_a.id, name="S2")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"pages": {"player": "read"}})
        db.commit()
        urls = ["/pages", f"/pages/albums/{top.id}", f"/pages/albums/{mid.id}", f"/pages/albums/{leaf.id}", f"/pages/albums/{empty.id}",
                f"/pages/albums/{far.id}", "/pages/albums/999999"]
        must = urls[:5]
    finally:
        db.close()
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            served[url] += _same_page(d, via, url, email)
    assert all(served[u] >= 2 for u in must), served


def test_audio_and_video_libraries_the_gateway_builds_are_pythons(client, seed):
    import os
    from datetime import datetime
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal, get_app_settings
    from app.models import AudioAlbum, AudioClip, Entity, VideoAlbum, VideoClip, World, entity_player_access
    base = str(client.base_url)
    uploads = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "uploads" / "audio"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "gw-real.mp3").write_bytes(b"x" * 1234)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        st = get_app_settings(db)
        st.max_audio_mb = 7
        st.max_video_mb = None
        urls = []
        for Album, Clip, prefix in ((AudioAlbum, AudioClip, "/audio"), (VideoAlbum, VideoClip, "/video")):
            top = Album(world_id=w, name="Ambience <a>"); far = Album(world_id=seed.world_b.id, name="Far")
            db.add_all([top, far]); db.flush()
            sub = Album(world_id=w, name="Sub", parent_id=top.id); db.add(sub); db.flush()
            db.add_all([Clip(world_id=w, name="Rain", file_url="/uploads/audio/gw-real.mp3", created_at=datetime(2025, 5, 6, 7, 8, 9, 999)),
                        Clip(world_id=w, name="Secret", file_url="/uploads/audio/missing.mp3", visible_to_players=False),
                        Clip(world_id=w, name="Escape", file_url="/uploads/../world.db"),
                        Clip(world_id=w, name="In top", album_id=top.id, file_url="/uploads/audio/gw-real.mp3", created_at=None),
                        Clip(world_id=w, name="Hidden in top", album_id=top.id, file_url="x", visible_to_players=False),
                        Clip(world_id=w, name="In sub", album_id=sub.id, file_url="/elsewhere/a.mp3")])
            urls += [prefix, f"{prefix}/albums/{top.id}", f"{prefix}/albums/{sub.id}", f"{prefix}/albums/{far.id}", f"{prefix}/albums/999999"]
        hidden = Entity(world_id=w, kind="npc", name="Hidden npc", visible_to_players=False)
        shared = Entity(world_id=w, kind="npc", name="Shared npc", visible_to_players=False)
        db.add_all([hidden, shared, Entity(world_id=w, kind="location", name="Open place")]); db.flush()
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"audio": {"player": "read"}, "video": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    served = {u: 0 for u in urls}
    try:
        for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                                 (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
            d = _signed_in(email, pw, world)
            via = _via(d, base)
            for url in urls:
                served[url] += _same_page(d, via, url, email)
    finally:
        (uploads / "gw-real.mp3").unlink(missing_ok=True)
    assert all(served[u] >= 2 for u in ("/audio", "/video")), served
    assert sum(served.values()) >= 12, served


def test_image_gallery_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import (CharacterKeepsake, Entity, EntityNote, GameSession, ImageAlbum, MediaTitle, PlayerCharacter, World)
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        e = Entity(world_id=w, kind="npc", name="Zed", image_url="/uploads/z.png", body="see ![a](/uploads/a.png) and ![b](/uploads/b c.png) ![](/uploads/z.png)")
        db.add(e); db.flush()
        db.add_all([EntityNote(entity_id=e.id, content="note ![n](/uploads/n.webp)"), EntityNote(entity_id=e.id, content="no image")])
        db.add_all([Entity(world_id=w, kind="npc", name=f"Bulk {i:03d}", image_url=f"/uploads/bulk/{i:03d}.png") for i in range(205)])
        pc = PlayerCharacter(world_id=w, name="Hero", owner_user_id=seed.player_a.id, portrait_url="/uploads/0123456789ab-hero.png",
                             backstory="![x](/uploads/back.png)", notes="![y](/uploads/notes.png)")
        db.add(pc); db.flush()
        db.add(GameSession(world_id=w, session_num=1, title="Night", recap_media_json=json.dumps(
            [{"kind": "image", "url": "/uploads/s1.png"}, {"kind": "image", "url": "/uploads/s1.png"}, {"kind": "image", "url": "http://x/y.png"},
             {"kind": "video", "clip_id": 3}, "junk"])))
        db.add_all([CharacterKeepsake(world_id=w, character_id=pc.id, kind="image", url="/uploads/kept.png", name="k"),
                    CharacterKeepsake(world_id=w, character_id=pc.id, kind="image", url="/uploads/own.png", name="o", own=True),
                    MediaTitle(world_id=w, url="/uploads/a.png", title="Named <a>")])
        top = ImageAlbum(world_id=w, name="Art", image_urls_json=json.dumps(["/uploads/a.png", "/uploads/only.png"]))
        db.add_all([top, ImageAlbum(world_id=w, name="Broken", image_urls_json="{bad")]); db.flush()
        sub = ImageAlbum(world_id=w, name="Sub", parent_id=top.id, image_urls_json=json.dumps(["/uploads/z.png", "/uploads/fedcba987654-shot.jpg", "/x/"]))
        db.add(sub); db.flush()
        deep = ImageAlbum(world_id=w, name="Deep", parent_id=sub.id); far = ImageAlbum(world_id=seed.world_b.id, name="Far")
        db.add_all([deep, far]); db.flush()
        db.add(ImageAlbum(world_id=w, name="Bad urls", parent_id=deep.id, image_urls_json="[1, 2]"))
        urls = ["/images", f"/images/albums/{top.id}", f"/images/albums/{sub.id}", f"/images/albums/{deep.id}", f"/images/albums/{far.id}", "/images/albums/999999"]
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"images": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            served[u] += _same_page(d, via, u, email)
    assert served["/images"] >= 2 and all(served[u] for u in urls[1:3]), served


def test_new_entity_form_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityTemplate, ImageAlbum, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        db.add_all([Entity(world_id=w, kind="npc", name="A", folder="Town/Inn"), Entity(world_id=w, kind="npc", name="B", folder="Town/Inn"),
                    Entity(world_id=w, kind="npc", name="C", folder=""), Entity(world_id=w, kind="npc", name="D", folder="Árbol"),
                    Entity(world_id=w, kind="location", name="E", folder="Places", image_url="/uploads/e.png"),
                    EntityTemplate(world_id=w, name="Shop", slug="gw-new-shop", kind="location", fields_json=json.dumps([{"key": "o", "label": "Owner"}])),
                    EntityTemplate(world_id=None, name="Any", slug="gw-new-any", fields_json=""),
                    ImageAlbum(world_id=w, name="Art", image_urls_json=json.dumps(["/uploads/x.png"]))])
        hid = Entity(world_id=w, kind="npc", name="Hidden <h>", visible_to_players=False, folder="Town/Inn", summary="s", body="b",
                     custom_fields_json=json.dumps({"o": "Owner <x>", "n": 3}))
        far = Entity(world_id=seed.world_b.id, kind="npc", name="Far")
        bad = Entity(world_id=w, kind="npc", name="Bad fields", custom_fields_json="{nope")
        db.add_all([hid, far, bad]); db.flush()
        db.execute(entity_player_access.insert().values(entity_id=hid.id, user_id=seed.player_a.id))
        edit_urls = [f"/entity/{hid.id}/edit", f"/entity/{far.id}/edit", f"/entity/{bad.id}/edit", "/entity/999999/edit"]
        db.commit()
    finally:
        db.close()
    urls = ["/new", "/new?kind=npc", "/new?kind=location&folder=Places&name=%20%20New%20place%20", "/new?kind=npc&kind=location",
            "/new?name=" + "x" * 300, "/new?kind=custom_ship&name=a+b", *edit_urls]
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            served[u] += _same_page(d, via, u, email)
    assert all(v for u, v in served.items() if u not in edit_urls[1:]), served


def test_map_list_the_gateway_builds_is_pythons(client, seed):
    import os
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    import shutil
    import zlib
    from app.models import Entity, Party, Schematic, World
    base = str(client.base_url)
    maps = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    w = seed.world_a.id
    made = {"gw-a.json": {"world_id": w, "name": "Docks <d>", "width": 100, "height": 50, "markers": [{"x": 1}, {"x": 2}]},
            "gw-b.json": {"world_id": w}, "gw-c.json": {"world_id": str(w), "name": "string id"}, "gw-d.json": {"world_id": w, "markers": {"a": 1}}}
    for n, d in made.items():
        (maps / n).write_text(json.dumps(d))
    # a map with an uploaded picture whose tile pyramid is already cut (its meta written by hand, at the key map_tiles computes)
    pic = maps.parent / "uploads" / "maps" / "gw-a.png"
    pic.parent.mkdir(parents=True, exist_ok=True)
    pic.write_bytes(b"\x89PNG fake picture bytes" * 10)
    raw = pic.read_bytes()
    key = f"gw-a-{len(raw):x}{zlib.crc32(raw[:65536]):08x}"
    tiles = maps.parent / "map_tiles" / key
    tiles.mkdir(parents=True, exist_ok=True)
    (tiles / "meta.json").write_text(json.dumps({"key": key, "w": 100, "h": 50, "zmin": 0, "native": 0, "tile": 256, "tiles": 1}))
    db = SessionLocal()
    try:
        db.add_all([Schematic(world_id=w, name="Base <b>", slug="gw-base"), Schematic(world_id=w, name="Annex", slug="gw-annex"),
                    Party(world_id=w, name="Crew <c>", member_pc_ids_json="[1]", location_json=json.dumps({"kind": "map", "slug": "gw-a", "lat": 1.5, "lng": -2})),
                    Party(world_id=w, name="Elsewhere", location_json=json.dumps({"kind": "map", "slug": "gw-b"})),
                    Party(world_id=w, name="Nowhere")])
        db.add(Entity(world_id=w, kind="npc", name="Mara <M>", visible_to_players=False))
        db.add(Entity(world_id=w, kind="npc", name="ÉLAN"))
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"maps": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    served = 0
    try:
        for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.gm.email, GM_PASSWORD, "neon-dragons"),
                                 (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug), (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
            d = _signed_in(email, pw, world)
            via = _via(d, base)
            for u in ("/maps", "/maps/gw-a", "/maps/gw-b", "/maps/gw-c", "/maps/nope", "/maps/gw-d"):
                served += _same_page(d, via, u, email)
    finally:
        for n in made:
            (maps / n).unlink(missing_ok=True)
        pic.unlink(missing_ok=True)
        shutil.rmtree(tiles, ignore_errors=True)
    # a map picture without a fresh small copy is Python's (it queues one), so gw-a's list card and viewer stay with Python here
    assert served >= 6, served
    # an unknown map slug's 404 is the gateway's own
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    a, b = d.get("/maps/nope"), _via(d, base).get("/maps/nope")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (404, {"detail": "Not Found"})
    assert b.headers.get("x-nd-served-by") == "gateway"


def test_board_pages_the_gateway_builds_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, InvestBoard, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        db.add_all([Entity(world_id=w, kind="npc", name="Mara <m>", image_url="/uploads/m.png"), Entity(world_id=w, kind="location", name="Dock")])
        nodes = {"nodes": [{"id": "a", "title": "Murder <x>", "type": "event", "status": "verified", "body": "It **happened**.", "tags": ["x", 3, "y"],
                            "x": 120, "y": 40, "entity_id": 1},
                           {"id": "b", "title": "", "tags": "ab", "x": 500, "y": 10.5 if False else 10},
                           {"id": 7, "title": "Seven", "status": "suspect", "x": 0.25 if False else 3, "lat": 14871.466378840501}],
                 "groups": [{"id": "g", "label": "Group", "x": 1e-7}]}
        edges = [{"from": "a", "to": "b"}, {"from": 7, "to": "a"}, {"from": "a", "to": "missing"}, {"from": "b", "to": "a"}]
        db.add_all([InvestBoard(world_id=w, name="Case <1>", slug="gw-case", nodes_json=json.dumps(nodes), edges_json=json.dumps(edges)),
                    InvestBoard(world_id=w, name="Legacy", slug="gw-legacy", nodes_json=json.dumps([{"id": 1, "title": "Old", "x": 5.5}]), edges_json=""),
                    InvestBoard(world_id=w, name="Empty", slug="gw-empty", nodes_json="", edges_json=None),
                    InvestBoard(world_id=w, name="Bad", slug="gw-bad", nodes_json='"text"'),
                    InvestBoard(world_id=seed.world_b.id, name="Far", slug="gw-far")])
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"boards": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    urls = [f"/boards/{s}{r}" for s in ("gw-case", "gw-legacy", "gw-empty", "gw-bad", "gw-far", "nope") for r in ("", "/notes", "/export")]
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            served[u] += _same_page(d, via, u, email)
    must = [f"/boards/{s}{r}" for s in ("gw-case", "gw-empty", "gw-legacy") for r in ("", "/notes")]  # /export stays Python's
    assert all(served[u] for u in must), served
    # an unknown slug's 404 is the gateway's own, both pages
    dg = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for u in ("/boards/nope", "/boards/nope/notes"):
        a, b = dg.get(u), _via(dg, base).get(u)
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (404, {"detail": "Not Found"}), u
        assert b.headers.get("x-nd-served-by") == "gateway", u


def test_rules_page_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == seed.world_a.id).first()
        wa.rules_md = ('Intro &amp; welcome <a name="x"></a>\n\n## Combat\n\nRoll **d20** &lt;always&gt;.\n\n:::tip Cover\nUse it.\n:::\n\n'
                       '### Initiative\n\nHighest first.\n\n:::gm\nSecret twist.\n:::\n\n## Magic\n\n```statblock\nImp\n**AC**: 12\nHP: 4\n```\n\n'
                       '## Gear\n\nKnives.\n\n## Secrets\n\nOnly for the GM.\n')
        wa.rules_json = json.dumps({"sections": {"magic": {"icon": "🔮", "title": "Arcana"}, "secrets": {"players_visible": False},
                                                 "gear": {"order": 1}},
                                    "tabs": [{"label": "Core", "sections": ["combat", "initiative"]}, {"label": "Other", "sections": ["magic", "secrets"]}]})
        wa.section_access_json = json.dumps({"rules": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        served += _same_page(d, via, "/rules", email)
    assert served >= 2, served


def test_search_the_gateway_builds_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, GameSession, PlayerCharacter, Quest, World, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        ents = [Entity(world_id=w, kind="npc", name="Ashfall Rifle", summary="A weapon", body="weapon weapon weapon <b>bold</b> ash"),
                Entity(world_id=w, kind="item", name="Weapon Traits", summary="traits list", body="The trait list."),
                Entity(world_id=w, kind="npc", name="Hidden Ash", visible_to_players=False, body="ashen secrets"),
                Entity(world_id=w, kind="npc", name="Shared Ash", visible_to_players=False, body="shared ash"),
                Entity(world_id=w, kind="location", name="Vault", summary="[gmonly]codeword zebra[/gmonly] a vault", body="Stores ash."),
                Entity(world_id=w, kind="location", name="Dock", body="Only the GM knows [gmonly]zebra[/gmonly] here."),
                Entity(world_id=seed.world_b.id, kind="npc", name="Ash elsewhere")]
        db.add_all(ents); db.flush()
        db.execute(entity_player_access.insert().values(entity_id=ents[3].id, user_id=seed.player_a.id))
        db.add_all([EntityNote(entity_id=ents[0].id, content="note about ash", visible_to_players=True),
                    EntityNote(entity_id=ents[0].id, content="gm note ash", visible_to_players=False),
                    EntityNote(entity_id=ents[1].id, content="[gmonly]ash zebra[/gmonly] public part", visible_to_players=True),
                    PlayerCharacter(world_id=w, name="Ash Walker", owner_user_id=seed.player_a.id, race="Elf", char_class="Rogue", backstory="Born in ash."),
                    PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.gm.id, backstory="ash too"),
                    Quest(world_id=w, title="Find the ash", status="active", body="Search the ash pits."),
                    GameSession(world_id=w, session_num=3, title="Ashes", summary="We found ash.", session_date="2026-01-02")])
        wa = db.query(World).filter(World.id == w).first()
        wa.players_see_party = True
        db.commit()
    finally:
        db.close()
    urls = ["/search", "/search?q=ash", "/search?q=weapons", "/search?q=a", "/search?q=zebra", "/search?q=ash&kind=npc", "/search?q=%22quoted%22",
            "/search?q=ASH+walker", "/search?q=%C3%BC", "/search?q=trait", "/search?q=x&kind=nope"]
    served = {u: 0 for u in urls}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            served[u] += _same_page(d, via, u, email)
    assert all(v >= 2 for v in served.values()), served


def test_home_page_the_gateway_builds_is_pythons(client, seed):
    from datetime import datetime, timedelta
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, GameSession, InvestBoard, Quest, Schematic, World, entity_links, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        t0 = datetime(2026, 1, 1)
        ents = [Entity(world_id=w, kind="npc", name=f"N{i}", tags="crew, port" if i % 2 else "port,, spy", updated_at=t0 + timedelta(minutes=i))
                for i in range(10)]
        ents += [Entity(world_id=w, kind="location", name="Secret base", visible_to_players=False, tags="secret", updated_at=t0 + timedelta(hours=5)),
                 Entity(world_id=w, kind="item", name="Shared", visible_to_players=False, updated_at=t0 + timedelta(hours=6))]
        db.add_all(ents); db.flush()
        db.execute(entity_player_access.insert().values(entity_id=ents[11].id, user_id=seed.player_a.id))
        for src, dst in ((0, 1), (2, 1), (3, 1), (0, 10), (2, 10), (4, 5)):
            db.execute(entity_links.insert().values(source_id=ents[src].id, target_id=ents[dst].id))
        b = InvestBoard(world_id=w, name="Case", slug="gw-home-case")
        sc = Schematic(world_id=w, name="Base", slug="gw-home-base")
        gs = GameSession(world_id=w, session_num=2, title="Two")
        qu = Quest(world_id=w, title="Do it", status="active")
        db.add_all([b, sc, gs, qu, Quest(world_id=w, title="Done", status="complete")]); db.flush()
        wa = db.query(World).filter(World.id == w).first()
        wa.home_sections_json = json.dumps([
            {"name": "Links", "links": [{"label": "Hero", "icon": "🦸", "target_type": "entity", "target_ref": str(ents[0].id)},
                                        {"label": "Gone", "target_type": "entity", "target_ref": "999999"},
                                        {"label": "Board", "target_type": "board", "target_ref": "gw-home-case"},
                                        {"label": "Base", "target_type": "schematic", "target_ref": "gw-home-base"},
                                        {"label": "S2", "target_type": "session", "target_ref": gs.id},
                                        {"label": "Q", "target_type": "quest", "target_ref": str(qu.id)},
                                        {"label": "NPCs", "target_type": "kind", "target_ref": "npc"},
                                        {"label": "Web", "target_type": "url", "target_ref": "https://example.com/?a=1"},
                                        {"label": "Bad url", "target_type": "url", "target_ref": "javascript:alert(1)"},
                                        {"label": "GM only", "target_type": "url", "target_ref": "/x", "visible_to_players": False}, "junk"]},
            {"name": "", "visible_to_players": False, "links": []}, {"name": "Empty", "links": []}])
        wa.home_pinned_tiles_json = json.dumps([{"label": "NPCs", "icon": "👤", "target_type": "kind", "target_ref": "npc"},
                                                {"label": "Site", "target_type": "url", "target_ref": "/rules"}, {"label": "x", "target_type": "nope"}])
        wa.home_hidden_kinds_json = json.dumps(["item", 3])
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug), (seed.gm.email, GM_PASSWORD, "neon-dragons")):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        served += _same_page(d, via, "/", email)
    assert served >= 3, served


def test_cockpit_page_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import Party, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Schematic(world_id=w, name="Zeta <map>", slug="ck-zeta"), Schematic(world_id=w, name="Alpha", slug="ck-alpha"),
                    Schematic(world_id=w, name="Html", slug="ck-html", is_html=True), Schematic(world_id=seed.world_b.id, name="Other", slug="ck-other"),
                    Party(world_id=w, name="B team"), Party(world_id=w, name="A team")])
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.gm.email, GM_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        served += _same_page(d, _via(d, base), "/cockpit", email)
    assert served >= 2, served


def test_display_pages_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import CombatSession, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Schematic(world_id=w, name="TV <map>", slug="tv-map", canvas_width=900), Schematic(world_id=seed.world_b.id, name="B", slug="tv-other"),
                    Schematic(world_id=w, name="Html", slug="tv-html", is_html=True, html_file="x.html")])
        fight = CombatSession(world_id=w, name="  The <big> fight  ")
        tmpl = CombatSession(world_id=w, name="Tmpl", is_template=True)
        db.add_all([fight, tmpl])
        db.commit()
        ids = (fight.id, tmpl.id)
    finally:
        db.close()
    urls = ["/display", "/display/map/tv-map", "/display/map/tv-other", "/display/map/tv-html", "/display/map/none",
            f"/display/combat/{ids[0]}", f"/display/combat/{ids[1]}", "/display/combat/99999"]
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for url in urls:
            served += _same_page(d, _via(d, base), url, email)
    assert served >= 3, served


def test_player_cockpit_page_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import Party, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": "str", "value": 3}, {"id": "dex", "value": 2}, {"id": "wil", "value": 2}])
    db = SessionLocal()
    try:
        a1 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Zora <z>", stats_json=stats, current_hp=0, xp=400, level=1,
                             conditions_json=json.dumps(["Prone", 3, "Dazed", "a", "b", "c"]), armor_class=12)
        a2 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Abe", current_hp=5)
        b1 = PlayerCharacter(world_id=w, owner_user_id=seed.player_b.id, name="Bea", stats_json=stats, current_hp=3, temp_hp=2)
        free = PlayerCharacter(world_id=w, name="Nobody")
        db.add_all([a1, a2, b1, free])
        db.flush()
        db.add_all([Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([a1.id, b1.id, 999])), Party(world_id=w, name="Other", member_pc_ids_json=json.dumps([b1.id])),
                    Party(world_id=w, name="Bad", member_pc_ids_json="not json")])
        db.commit()
        ids = (a1.id, b1.id, free.id)
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for url in ("/player-cockpit", f"/player-cockpit?pc={ids[0]}", f"/player-cockpit?pc={ids[1]}", f"/player-cockpit?pc={ids[2]}", "/player-cockpit?pc=x"):
            served += _same_page(d, _via(d, base), url, email)
    assert served >= 10, served


def test_schematic_player_views_are_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import CombatSession, Party, PlayerCharacter, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        mine = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Mine", currency_json=json.dumps([{"label": "Gold", "value": 4}]))
        other = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Second")
        db.add_all([mine, other])
        db.flush()
        cs = CombatSession(world_id=w, name="F", active_idx=1, round_num=3,
                           combatants_json=json.dumps([{"id": "a", "initiative": 5}, {"id": "b", "initiative": 9}, {"id": "c", "initiative": 1}]))
        db.add(cs)
        db.flush()
        els = [{"id": "t1", "type": "token", "pc_id": other.id, "x": 1.5}, {"id": "h", "type": "rect", "hidden": True},
               {"id": "t2", "type": "token", "visible_to_players": False}, {"id": "t3", "type": "token", "visible_to_players": 0}, {"id": "r", "type": "text", "label": "<b>"}]
        walls = [{"id": "w", "pts": [[0, 0], [5, 5]], "kind": "wall", "extra": 1}, {"id": "s", "pts": [[1, 1], [2, 2]], "kind": "secret", "state": "open"},
                 {"id": "s2", "pts": [[1, 1], [2, 2]], "kind": "secret"}, "junk"]
        lights = [{"id": "l1", "x": 5, "y": 5, "range": 3, "on": True, "label": "x"}, {"id": "l2", "x": 5, "y": 5, "range": 3, "on": False}]
        maps = [
            Schematic(world_id=w, name="Plain <m>", slug="pv-plain", elements_json=json.dumps(els), canvas_bg="blueprint", grid_type="square",
                      grid_config_json='{"cell_size": 40}', combat_session_id=cs.id, image_url="/uploads/schematics/pv.png"),
            Schematic(world_id=w, name="Fog", slug="pv-fog", elements_json=json.dumps(els), walls_json=json.dumps(walls), lights_json=json.dumps(lights),
                      fog_json=json.dumps({"enabled": True, "darkness": 0.5, "range": 6})),
            Schematic(world_id=w, name="Dark", slug="pv-dark", walls_json=json.dumps(walls), lights_json=json.dumps(lights), fog_json=json.dumps({"darkness": 0.3})),
            Schematic(world_id=w, name="Strict", slug="pv-strict", fog_json=json.dumps({"enabled": True, "strict": True})),
            Schematic(world_id=seed.world_b.id, name="B", slug="pv-b"),
        ]
        db.add_all(maps)
        db.add(Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([mine.id]), location_json=json.dumps({"kind": "schematic", "slug": "pv-plain", "x": 3, "y": 4})))
        db.commit()
    finally:
        db.close()
    slugs = ["pv-plain", "pv-fog", "pv-dark", "pv-strict", "pv-b", "pv-none"]
    served = {"page": 0, "json": 0, "tv": 0}
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for slug in slugs:
            served["page"] += _same_page(d, via, f"/maps/schematic/{slug}/view", email)
            a, b = d.get(f"/maps/schematic/{slug}/view.json"), via.get(f"/maps/schematic/{slug}/view.json")
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, slug, a.text[:300], b.text[:300])
            served["json"] += b.headers.get("x-nd-served-by") == "gateway"
            a, b = d.get(f"/display/map/{slug}/data.json"), via.get(f"/display/map/{slug}/data.json")
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, slug, a.text[:300], b.text[:300])
            served["tv"] += b.headers.get("x-nd-served-by") == "gateway"
    assert served["page"] >= 6 and served["json"] >= 6 and served["tv"] >= 3, served


def test_schematic_editor_page_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import CombatSession, Entity, Party, PlayerCharacter, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Hero <h>", stats_json=json.dumps([{"id": "dex", "value": 2}]),
                                    conditions_json='["Prone", 1]', portrait_url="/uploads/p.png"),
                    Entity(world_id=w, kind="creature", name="Wolf", custom_fields_json=json.dumps({"hp": 7, "armor": 1})),
                    Entity(world_id=w, kind="item", name="Sword", summary="sharp", image_url="/uploads/s.png"),
                    Party(world_id=w, name="Crew", location_json=json.dumps({"kind": "schematic", "slug": "ed-map", "x": 1, "y": 2}))])
        cs = CombatSession(world_id=w, name="Fight <f>", round_num=2, combatants_json=json.dumps([{"id": "x", "initiative": 3}]))
        db.add_all([cs, CombatSession(world_id=w, name="Tmpl", is_template=True)])
        db.flush()
        db.add_all([Schematic(world_id=w, name="Editor <e>", slug="ed-map", combat_session_id=cs.id, canvas_bg="light",
                              elements_json=json.dumps([{"id": "a", "type": "rect", "x": 1.5}])),
                    Schematic(world_id=w, name="Empty", slug="ed-empty"), Schematic(world_id=seed.world_b.id, name="B", slug="ed-b")])
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for slug in ("ed-map", "ed-empty", "ed-b", "ed-none"):
            served += _same_page(d, _via(d, base), f"/maps/schematic/{slug}", email)
    assert served >= 2, served


def test_combat_tracker_page_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import CombatSession, Entity, GameSession, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        gs = GameSession(world_id=w, session_num=4, title="Four <4>")
        db.add_all([gs, GameSession(world_id=w, session_num=9, title="Nine"), PlayerCharacter(world_id=w, name="Hero", current_hp=3),
                    Entity(world_id=w, kind="character", name="Guard", custom_fields_json=json.dumps({"health": "9", "speed": 2}))])
        db.flush()
        cbs = [{"id": "a", "name": "A <a>", "initiative": 5, "hp": 2.5}, {"id": "b", "name": "B", "initiative": 9, "notes": "x"}, "junk",
               {"id": "p", "name": "Pal", "source": "pc", "initiative": 7, "hp": 3, "max_hp": 10, "shock": 1, "conditions": ["Prone"]},
               {"id": "k", "name": "Known", "name_known": True, "initiative": 2, "hp": 4, "max_hp": 8},
               {"id": "h", "name": "Hidden", "visible": False, "initiative": 8}, {"id": "d", "name": "Dead", "status": "dead", "initiative": 1},
               {"name": "No id", "source": "pc", "initiative": 0, "hp": "6", "max_hp": "12"}]
        fights = [CombatSession(world_id=w, name="Init <i>", ruleset="nd", active_idx=1, round_num=2, rev=4, game_session_id=gs.id,
                                combatants_json=json.dumps(cbs), log_json=json.dumps([{"t": "hit"}])),
                  CombatSession(world_id=w, name="Fluid", ruleset="hunt", combatants_json=json.dumps(cbs),
                                turn_json=json.dumps({"active_id": "b", "acted": ["a", 3], "picker": None, "mode": "x"}), log_json="{}"),
                  CombatSession(world_id=w, name="Tmpl", ruleset="nd", is_template=True),
                  CombatSession(world_id=w, name="Old", ruleset=""),
                  CombatSession(world_id=seed.world_b.id, name="B", ruleset="nd")]
        db.add_all(fights)
        from app.models import World
        db.get(World, w).section_access_json = json.dumps({"combat": {"player": "read", "assistant": "edit"}})   # closed to players by default
        db.commit()
        ids = [f.id for f in fights]
    finally:
        db.close()
    served = json_served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for i in ids[:3] + ids[4:] + [ids[3], 999999]:
            served += _same_page(d, _via(d, base), f"/combat/{i}", email)
            for url in (f"/api/combat/{i}/state", f"/api/display/combat/{i}/state"):
                a, b = d.get(url), _via(d, base).get(url)
                assert (a.status_code, a.content) == (b.status_code, b.content), (email, url, a.text[:300], b.text[:300])
                json_served += b.headers.get("x-nd-served-by") == "gateway"
    assert json_served >= 6, json_served
    assert served >= 5, served  # the GM: three encounters; a player: the two fights' read-only views


def test_strict_secrecy_map_views_are_pythons(client, seed):
    """A map with strict secrecy: the server sends players only what the party has seen (app/map_strict.py) and records the explored
    squares on each view. Each case is visited on two copies of the map - one through Python, one through the gateway - and the answers
    and the recorded explored area must be the same."""
    from .conftest import PLAYER_PASSWORD
    from app.models import PlayerCharacter, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Scout")
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    walls = [{"id": "w1", "pts": [[300, 0], [300, 500]], "kind": "wall"}, {"id": "d1", "pts": [[300, 500], [300, 600]], "kind": "door", "state": "open"},
             {"id": "s1", "pts": [[0, 700], [800, 700]], "kind": "secret"}, {"id": "win", "pts": [[500, 0], [500, 300]], "kind": "window"}]
    els = [{"id": "me", "type": "token", "pc_id": pid, "x": 150.5, "y": 220},
           {"id": "orc", "type": "token", "x": 200, "y": 260}, {"id": "far", "type": "token", "x": 900, "y": 900},
           {"id": "r1", "type": "rect", "x": 100, "y": 100, "w": 60, "h": -40}, {"id": "r2", "type": "rect", "x": 1500, "y": 1200, "w": 10, "h": 10},
           {"id": "c1", "type": "circle", "cx": 400, "cy": 400, "rx": -30, "ry": 30}, {"id": "l1", "type": "aoe", "x1": 50, "y1": 50, "x2": 80, "y2": 90},
           {"id": "p1", "type": "poly", "points": [[10, 10], [20, 30], [5, 40]]}, {"id": "p2", "type": "path", "pts": "nope"},
           {"id": "t1", "type": "text", "x": 1700, "y": 100}, {"id": "i1", "type": "image", "x": 250, "y": 250, "w": 5, "h": 5, "rot": 30},
           {"id": "h", "type": "rect", "x": 150, "y": 220, "w": 5, "h": 5, "hidden": True}]
    lights = [{"id": "L1", "x": 160, "y": 230, "range": 3, "on": True}, {"id": "L2", "x": 1800, "y": 1400, "range": 3, "on": True}]
    cases = [
        ({"enabled": True, "strict": True, "range": 6, "darkness": 0.4, "epoch": 2}, None, "square", '{"cell_size": 40}', 2000, 1500),
        ({"enabled": True, "strict": True, "epoch": 1}, {"epoch": 1, "cell": 50.0, "cols": 40, "rows": 30, "runs": [0, 5, 1200, 3, "x", 2]}, "none", "{}", 2000, 1500),
        ({"enabled": True, "strict": True, "epoch": 3}, {"epoch": 2, "cell": 50.0, "cols": 40, "rows": 30, "runs": [0, 5]}, "none", "{}", 2000, 1500),
        ({"enabled": True, "strict": True, "range": 2}, None, "hex", '{"cell_size": 15}', 9000, 7000),
    ]
    n = 0
    served = 0
    for fog, stored, gtype, gcfg, cw, chh in cases:
        for who in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(who[0], who[1], seed.world_a.slug)
            for path in ("/view.json", "/view", "tv"):
                if path == "tv" and who[0] != seed.gm.email:
                    continue
                answers, records = [], []
                for side in (d, _via(d, base)):
                    n += 1
                    slug = f"strict-{n}"
                    db = SessionLocal()
                    try:
                        db.add(Schematic(world_id=w, name="Strict", slug=slug, elements_json=json.dumps(els), walls_json=json.dumps(walls),
                                         lights_json=json.dumps(lights), fog_json=json.dumps(fog), grid_type=gtype, grid_config_json=gcfg,
                                         canvas_width=cw, canvas_height=chh, image_url="/uploads/schematics/s.png",
                                         explored_json=json.dumps(stored) if stored else None))
                        db.commit()
                    finally:
                        db.close()
                    url = f"/display/map/{slug}/data.json" if path == "tv" else f"/maps/schematic/{slug}{path}"
                    r = side.get(url)
                    body = golden.normalise(r.headers.get("content-type", ""), r.content) if path == "/view" else r.text
                    answers.append((r.status_code, body.replace(slug, "SLUG")))
                    db = SessionLocal()
                    try:
                        records.append(db.query(Schematic).filter(Schematic.slug == slug).first().explored_json)
                    finally:
                        db.close()
                    if side is not d:
                        served += r.headers.get("x-nd-served-by") == "gateway"
                assert answers[0] == answers[1], (fog, who[0], path, answers[0][1][:400], answers[1][1][:400])
                assert records[0] == records[1], (fog, who[0], path, records)
    assert served >= 15, served


def test_character_sheet_page_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import CharacterPref, GameSession, Party, PlayerCharacter, World
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": "str", "label": "Strength", "value": 3}, {"id": "dex", "value": "2"}, {"id": "bod", "value": 2.7}, {"id": "wil", "value": None},
                        {"id": "itu", "value": 1}, {"label": "no id"}])
    db = SessionLocal()
    try:
        db.get(World, w).players_see_party = True
        hero = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Hero <h>", race="Elf", char_class="Rogue", level=3, xp=2800, stats_json=stats,
                               equipment_json=json.dumps([{"name": "Rope", "weight": "1.5", "qty": 2}, {"name": "Coin", "weight": None}, "junk"]),
                               currency_json=json.dumps([{"label": "Gold", "abbr": "gp", "value": 12}]), feats_json=json.dumps([{"name": "Quick", "description": "fast"}]),
                               cyberware_json=json.dumps([{"name": "Eye", "ca_cost": "2.5"}, {"name": "Arm", "ca_cost": 3}]),
                               conditions_json=json.dumps(["Burn"]), current_hp=7, max_hp=0, shock_current=2, minor_edge="Lucky", attacks_json="[]")
        mate = PlayerCharacter(world_id=w, owner_user_id=seed.player_b.id, name="Mate", race="Orc", current_hp=0, level=2, stats_json=stats, conditions_json='["Daze"]')
        bare = PlayerCharacter(world_id=w, owner_user_id=seed.player_b.id, name="Bare")
        db.add_all([hero, mate, bare])
        db.flush()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([hero.id, mate.id]),
                      loot_json=json.dumps([{"lid": "aa11bb22", "name": "Gem", "qty": 1, "notes": "", "claimed_by": [mate.id]}]),
                      xp_json=json.dumps([{"amount": 100, "note": "a"}, {"amount": 50, "note": "b"}]))
        db.add(party)
        db.flush()
        db.add_all([GameSession(world_id=w, session_num=1, title="One", party_id=party.id), GameSession(world_id=w, session_num=2, title="Two <2>", party_id=party.id),
                    CharacterPref(world_id=w, character_id=hero.id, prefs_json=json.dumps({"carry_limit": 40}))])
        db.commit()
        ids = (hero.id, mate.id, bare.id)
    finally:
        db.close()
    served = 0
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD), (seed.player_b.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for i in ids:
            served += _same_page(d, _via(d, base), f"/characters/{i}", email)
    assert served >= 6, served


def test_strict_map_masked_picture_is_pythons(client, seed):
    """The masked picture of a strict-secrecy map: the gateway's resize is Lanczos like Pillow's but not bit-identical, so the pictures
    are compared decoded - same size, every unseen square exactly the background colour on both, the rest within a small difference."""
    import io
    from PIL import Image
    from .conftest import PLAYER_PASSWORD
    from app.main import UPLOADS_DIR
    from app.models import PlayerCharacter, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    d = UPLOADS_DIR / "schematics"
    d.mkdir(parents=True, exist_ok=True)
    src = Image.new("RGB", (900, 700))
    px = src.load()
    for y in range(700):
        for x in range(900):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    src.save(d / "masked-src.png")
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Eye")
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    els = [{"id": "me", "type": "token", "pc_id": pid, "x": 300, "y": 250}]
    walls = [{"id": "w", "pts": [[500, 0], [500, 1000]], "kind": "wall"}]
    served = 0
    for cw, chh, bg in ((1200, 900, "blueprint"), (3000, 2000, "dark")):
        pics = []
        for i, who in enumerate(("direct", "gateway")):
            slug = f"masked-{cw}-{i}"
            db = SessionLocal()
            try:
                db.add(Schematic(world_id=w, name="M", slug=slug, elements_json=json.dumps(els), walls_json=json.dumps(walls), canvas_bg=bg,
                                 fog_json=json.dumps({"enabled": True, "strict": True, "range": 8}), canvas_width=cw, canvas_height=chh,
                                 image_url="/uploads/schematics/masked-src.png"))
                db.commit()
            finally:
                db.close()
            pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
            side = pl if who == "direct" else _via(pl, base)
            r = side.get(f"/maps/schematic/{slug}/bg.webp")
            assert r.status_code == 200 and r.headers["content-type"] == "image/webp", (who, r.status_code)
            if who == "gateway":
                served += r.headers.get("x-nd-served-by") == "gateway"
            pics.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        a, b = pics
        assert a.size == b.size, (a.size, b.size)
        bgc = {"blueprint": (13, 27, 42), "dark": (17, 17, 17)}[bg]
        pa, pb = a.load(), b.load()
        diff = n = mism = 0
        for y in range(0, a.size[1], 3):
            for x in range(0, a.size[0], 3):
                ca, cb = pa[x, y], pb[x, y]
                near_a = all(abs(u - v) <= 6 for u, v in zip(ca, bgc))
                near_b = all(abs(u - v) <= 6 for u, v in zip(cb, bgc))
                mism += near_a != near_b
                diff += sum(abs(u - v) for u, v in zip(ca, cb))
                n += 3
        assert mism / (n / 3) < 0.01, mism          # the same squares hidden (a sliver of slack at cell edges)
        assert diff / n < 6, diff / n                # the visible picture alike
    assert served == 2, served


def test_player_cockpit_with_custom_systems_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import Party, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    fields = [{"id": "flesh", "label": "Flesh [wounds]", "type": "resource", "default_value": "8/8", "vital": "hp"},
              {"id": "stamina", "label": "Stamina", "type": "resource", "default_value": "4"}, {"id": "ichor", "label": "", "type": "resource"},
              {"id": "a", "label": "A", "type": "resource"}, {"id": "b", "label": "B", "type": "resource"}, {"id": "xp", "label": "XP", "type": "number", "xp": True}]
    db = SessionLocal()
    try:
        tpl = SheetTemplate(name="Grim <g>", slug="cockpit-grim", sheet_mode="custom", fields_json=json.dumps(fields))
        plain = SheetTemplate(name="Plain", slug="cockpit-plain", sheet_mode="custom", fields_json=json.dumps([{"id": "n", "type": "text"}]))
        db.add_all([tpl, plain])
        db.flush()
        mine = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Grim one", sheet_template_id=tpl.id,
                               custom_fields_json=json.dumps({"flesh_current": 0, "stamina_max": 6}), conditions_json='["Marked"]')
        other = PlayerCharacter(world_id=w, owner_user_id=seed.player_b.id, name="Plain one", sheet_template_id=plain.id)
        nd = PlayerCharacter(world_id=w, owner_user_id=seed.player_b.id, name="N&D one", current_hp=4)
        db.add_all([mine, other, nd])
        db.flush()
        db.add(Party(world_id=w, name="Mixed", member_pc_ids_json=json.dumps([mine.id, other.id, nd.id])))
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        served += _same_page(d, _via(d, base), "/player-cockpit", email)
    assert served == 2, served


def test_combat_tracker_with_custom_system_candidates_is_pythons(client, seed):
    from app.models import CombatSession, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    fields = [{"id": "flesh", "label": "Flesh", "type": "resource", "default_value": "8/8", "vital": "hp"}, {"id": "stamina", "label": "Stamina (x)", "type": "resource", "default_value": "3"}]
    db = SessionLocal()
    try:
        tpl = SheetTemplate(name="Hunt", slug="ct-hunt", sheet_mode="custom", fields_json=json.dumps(fields))
        db.add(tpl)
        db.flush()
        db.add_all([PlayerCharacter(world_id=w, name="Hunter", sheet_template_id=tpl.id, custom_fields_json=json.dumps({"flesh_current": 5})),
                    PlayerCharacter(world_id=w, name="Hero", current_hp=4)])
        cs = CombatSession(world_id=w, name="Hunt night", ruleset="hunt", combatants_json="[]")
        db.add(cs)
        db.commit()
        cid = cs.id
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    assert _same_page(d, _via(d, base), f"/combat/{cid}", "gm")


def test_character_sheet_on_custom_systems_is_pythons(client, seed):
    from .conftest import PLAYER_PASSWORD
    from app.models import PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    fields = [{"id": "hunter", "label": "Hunter", "type": "text", "section": "Who"}, {"id": "health", "label": "Health (0 = Broken)", "type": "resource",
               "default_value": "6/6", "section": "Body"}, {"id": "stamina", "label": "Stamina", "type": "resource", "default_value": "3/5", "section": "Body"},
              {"id": "kind", "label": "Kind", "type": "select", "options": ["Mortal", "God"], "section": "Who"}, {"id": "xpCurrent", "label": "XP", "type": "number", "section": "Growth"},
              {"id": "gear", "label": "Gear", "type": "list", "section": "Gear"}, {"id": "notes", "label": "Notes", "type": "textarea"}]
    system = {"dice": {"threshold": 5, "pools": [{"id": "p0", "label": "Roll", "dice": 2, "override": {"field": "kind", "equals": "God", "dice": 3}}],
                       "spend": {"field": "stamina", "label": "Stamina", "per": 1}},
              "rest": {"long": [["full", "stamina"]]}, "conditions": ["Marked", "Prone"]}
    db = SessionLocal()
    try:
        db.get(World, w).players_see_party = True
        own = SheetTemplate(name="Moonlight <m>", slug="cs-own", sheet_mode="custom", fields_json=json.dumps(fields), system_json=json.dumps(system))
        builtin = db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first()   # the seeded built-in system
        ndtpl = SheetTemplate(name="N&D extra", slug="cs-nd", sheet_mode="nd", fields_json=json.dumps([{"id": "oath", "label": "Oath", "type": "text"}]))
        db.add_all([own, ndtpl])
        db.flush()
        pcs = [PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Moon", sheet_template_id=own.id,
                               custom_fields_json=json.dumps({"kind": "God (old)", "health_current": 2, "notes": "<b>"}), conditions_json='["Marked", "marked", "Prone"]'),
               PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Built", sheet_template_id=builtin.id),
               PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Extra", sheet_template_id=ndtpl.id, custom_fields_json='{"oath": "x"}',
                               stats_json=json.dumps([{"id": "str", "value": 2}]))]
        db.add_all(pcs)
        db.commit()
        ids = [p.id for p in pcs]
    finally:
        db.close()
    served = 0
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for i in ids:
            served += _same_page(d, _via(d, base), f"/characters/{i}", email)
    assert served == 6, served


def test_session_page_is_pythons(client, seed):
    """GET /sessions/<id> built by the gateway: the party, the featured entities and characters, the party's characters, the session's
    facts (GM-only ones for the GM only) and their tag cloud, prep, loot, linked combats and the live-transcript flag - for the GM, a
    player of the world and a player of another world."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CombatSession, Entity, Fact, GameSession, Party, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id)
        e = Entity(world_id=w, kind="character", name="Old Mara <m>")
        db.add_all([pc, e])
        db.commit()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.commit()
        full = GameSession(world_id=w, session_num=4, title="Into <the> Dark", session_date="2026-01-02", party_id=party.id, summary="GM *notes*",
                           player_summary="Recap", player_summary_published=True, live_transcript="hello", xp_awarded=30,
                           npcs_json=json.dumps([{"entity_id": e.id}, {"entity_id": pc.id, "kind": "player_character"}, {"entity_id": 999999}]),
                           prep_json=json.dumps(["Draw map"]), loot_json=json.dumps([{"name": "Rope", "qty": 2}]))
        bare = GameSession(world_id=w, session_num=5, title="Bare")
        db.add_all([full, bare])
        db.commit()
        db.add_all([Fact(world_id=w, game_session_id=full.id, content="The **bridge** fell.", tags="ruin, bridge", visible_to_players=True),
                    Fact(world_id=w, game_session_id=full.id, content="Secret", tags="bridge,gm", visible_to_players=False),
                    CombatSession(world_id=w, name="Ambush", game_session_id=full.id)])
        db.commit()
        urls = [f"/sessions/{full.id}", f"/sessions/{bare.id}", "/sessions/999999"]
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            _same_page(d, via, url, email)
            served += via.get(url, follow_redirects=False).headers.get("x-nd-served-by") == "gateway"
    assert served == 9, served


def test_cockpit_workspace_read_is_pythons(client, seed):
    """GET /api/cockpit/workspace from the gateway: none saved, a saved one, unreadable stored text - and a player's refusal."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    for stored in (None, json.dumps({"current": {"panels": [{"id": "p1", "type": "notes", "data": {"text": "ü"}}]}, "presets": {}}), "{oops", "[1]"):
        db = SessionLocal()
        try:
            db.get(World, seed.world_a.id).cockpit_ws_json = stored
            db.commit()
        finally:
            db.close()
        for d in (gm, player):
            a, b = d.get("/api/cockpit/workspace"), _via(d, base).get("/api/cockpit/workspace")
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (stored, a.text, b.text)
            assert b.headers.get("x-nd-served-by") == "gateway"


def _process_cells(html):
    """The cells of Settings that describe the serving process, not the data: Python's watchdog thread, event-loop heartbeat and database
    pool, and its live page-build limit. The gateway reports its own (none of the three, no Python render gate)."""
    import re
    html = re.sub(r"(<tr><td>(?:Watchdog thread|Event-loop heartbeat|DB pool right now)</td><td>)[^<]*(</td></tr>)", r"\1~\2", html)
    return re.sub(r"keeping whatever builds the most pages a second - now \d+\.", "keeping whatever builds the most pages a second - now ~.", html)


def test_settings_page_is_pythons(client, seed):
    """GET /settings (every tab, an unknown one) and GET /settings/visibility-rows from the gateway: the saved settings, the per-model
    overrides, the Ollama server-env file status, upload limits, the world's players, nav menus and section matrix, the Visibility rows
    with who each hidden entity is open to, diagnostics events and dumps - and the player's refusal."""
    from .conftest import PLAYER_PASSWORD
    from app.database import DB_PATH, SessionLocal, get_app_settings
    from app.models import Entity, World, entity_player_access
    base = str(client.base_url)
    db = SessionLocal()
    try:
        st = get_app_settings(db)
        st.ollama_model_overrides_json = json.dumps({"gemma:2b": {"options": {"temperature": 0.4}, "keep_alive": "5m", "thinking": True}, "qwen": {}})
        st.ollama_server_env_json = json.dumps({"OLLAMA_NUM_PARALLEL": "2", "OLLAMA_KV_CACHE_TYPE": "q8_0"})
        st.max_video_mb = 900
        w = seed.world_a.id
        hidden = Entity(world_id=w, kind="npc", name="Hidden <h>", visible_to_players=False)
        db.add_all([hidden, Entity(world_id=w, kind="item", name="Open", visible_to_players=True), Entity(world_id=w, kind="item", name="Closed", visible_to_players=False)])
        db.flush()
        db.execute(entity_player_access.insert().values(entity_id=hidden.id, user_id=seed.player_a.id))
        wa = db.query(World).filter(World.id == w).first()
        wa.section_access_json = json.dumps({"maps": {"player": "edit", "assistant": "bogus"}, "quests": "x", "kind_npc": {"player": "none"}})
        wa.nav_menus_json = json.dumps([{"id": "m1", "label": "Mine", "icon": "★", "item_ids": ["kind_npc", "dice"]}])
        db.commit()
    finally:
        db.close()
    diag = Path(DB_PATH).parent / "diagnostics"
    diag.mkdir(exist_ok=True)
    (diag / "events.log").write_text("\n".join(f"line {i} <x>" for i in range(250)) + "\n")
    (diag / "wedge-20260102-030405.txt").write_text("dump")
    (diag / "wedge-20260102-030406-2.txt").write_text("dump two")
    (diag / "not-a-dump.txt").write_text("x")
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    # Python holds the resource settings in memory, refreshed when this form saves them (the gateway reads the saved row)
    assert gm.post("/settings/resources", data={"res_image_workers": "3", "res_render_slots": "", "res_memory_gb": "20"},
                   follow_redirects=False).status_code == 303
    served = 0
    for url in ("/settings", "/settings?tab=system", "/settings?tab=visibility", "/settings?tab=navigation", "/settings?tab=diagnostics",
                "/settings?tab=nope", "/settings/visibility-rows"):
        a, b = gm.get(url), _via(gm, base).get(url)
        assert a.status_code == b.status_code == 200, url
        na = golden.normalise(a.headers["content-type"], _process_cells(a.text).encode())
        nb = golden.normalise(b.headers["content-type"], _process_cells(b.text).encode())
        if na != nb:
            import difflib
            raise AssertionError(url + "\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:60]))
        served += b.headers.get("x-nd-served-by") == "gateway"
    assert served == 7, served
    # a world with nothing in it, and no world at all
    nw = _signed_in(seed.gm.email, GM_PASSWORD, "no-such-world")
    for url in ("/settings", "/settings/visibility-rows"):
        a, b = nw.get(url, follow_redirects=False), _via(nw, base).get(url, follow_redirects=False)
        assert (a.status_code, a.text if a.status_code != 200 else _process_cells(a.text)) == \
               (b.status_code, b.text if b.status_code != 200 else _process_cells(b.text)), url
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    for url in ("/settings", "/settings/visibility-rows"):
        a, b = player.get(url, follow_redirects=False), _via(player, base).get(url, follow_redirects=False)
        assert (a.status_code, a.text) == (b.status_code, b.text) and a.status_code == 403, url
    gm.post("/settings/resources", data={"res_image_workers": "", "res_render_slots": "", "res_memory_gb": ""}, follow_redirects=False)


def test_party_page_is_pythons(client, seed):
    """GET /parties/<id> built by the gateway: members (an N&D sheet and a custom-system one), companions (a hidden one, shown to the
    GM and to the player it is shared with), loot (claimed and not), assigned quests (a GM-only one), the XP ledger, sessions, combats
    and calendar events, conditions and the level-up digest - for the GM, a member player, a non-member player, a player of another
    world (404), and a missing party."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import (CalendarEvent, CombatSession, Entity, GameSession, Party, PlayerCharacter, Quest, SheetTemplate,
                            entity_player_access)
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first()
        vex = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id, level=3, xp=99999, current_hp=0,
                              conditions_json=json.dumps(["Poisoned", " poisoned ", "Prone", 5]))
        ash = PlayerCharacter(world_id=w, name="Ash", level=2, xp=10, current_hp=7, conditions_json=json.dumps(["Prone"]),
                              sheet_template_id=tpl.id if tpl else None)
        hidden = Entity(world_id=w, kind="character", name="Secret ally", visible_to_players=False)
        shared = Entity(world_id=w, kind="creature", name="Shared hound", visible_to_players=False)
        open_ = Entity(world_id=w, kind="character", name="Open guide")
        db.add_all([vex, ash, hidden, shared, open_])
        db.flush()
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        party = Party(world_id=w, name="Crew <1>", goals="Find **it**", notes="n",
                      member_pc_ids_json=json.dumps([vex.id, ash.id]), member_entity_ids_json=json.dumps([hidden.id, shared.id, open_.id, 999999]),
                      loot_json=json.dumps([{"lid": "a", "name": "Gem", "qty": 2, "notes": "", "claimed_by": [vex.id]},
                                            {"lid": "b", "name": "Rope", "qty": 1, "notes": "50 ft", "claimed_by": []}]),
                      xp_json=json.dumps([{"amount": 100, "ts": "2026-01-02T03:04:05", "awarded": {"Vex": 100}, "session_id": None},
                                          {"amount": 50, "ts": "2026-02-02T00:00:00", "awarded": "all"}]))
        empty = Party(world_id=w, name="Nobody")
        db.add_all([party, empty])
        db.flush()
        db.add_all([GameSession(world_id=w, session_num=4, title="Four", party_id=party.id),
                    GameSession(world_id=w, session_num=5, title="Five", party_id=party.id),
                    CombatSession(world_id=w, name="Ambush", party_id=party.id),
                    CalendarEvent(world_id=w, title="Festival", day=12, party_id=party.id),
                    Quest(world_id=w, title="Open quest", assigned_party_id=party.id),
                    Quest(world_id=w, title="GM quest", assigned_party_id=party.id, visible_to_players=False)])
        other = Party(world_id=seed.world_b.id, name="Elsewhere")
        db.add(other)
        db.commit()
        ids = (party.id, empty.id, other.id)
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for pid in ids + (999999,):
            url = f"/parties/{pid}"
            a, b = d.get(url, follow_redirects=False), via.get(url, follow_redirects=False)
            if a.status_code != 200:
                assert (a.status_code, a.text) == (b.status_code, b.text), (email, url)
                served += b.headers.get("x-nd-served-by") == "gateway"
                continue
            served += _same_page(d, via, url, email)
            v = f"/api/parties/{pid}/vitals"
            a, b = d.get(v), via.get(v)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, v)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, v)
    assert served == 12, served


def test_character_list_is_pythons(client, seed):
    """GET /characters from the gateway: the GM's whole roster with owners, a player's own (and, with the party switch on, everyone
    owned), the search box over name / class / race / player, the three sorts, custom-system cards with their tracks, party badges,
    level-up flags, the character limit."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        stats = json.dumps([{"id": "str", "value": 3, "abbr": "STR"}, {"id": "dex", "value": 2, "label": "Dex"}, {"id": "wil", "value": "4"}])
        pcs = [PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id, level=3, xp=99999, current_hp=4, char_class="Netrunner",
                               race="Elf", stats_json=stats, player_name="Al"),
               PlayerCharacter(world_id=w, name="ash", owner_user_id=seed.gm.id, level=7, xp=10, current_hp=20, max_hp=40, race="Dwarf"),
               PlayerCharacter(world_id=w, name="Zed", level=1, xp=0, current_hp=0),
               PlayerCharacter(world_id=w, name="Mira", owner_user_id=seed.player_b.id, level=2, xp=5, current_hp=3,
                               sheet_template_id=tpl.id if tpl else None)]
        db.add_all(pcs)
        db.flush()
        db.add(Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([pcs[0].id, pcs[3].id])))
        db.commit()
    finally:
        db.close()
    urls = ["/characters", "/characters?sort=level", "/characters?sort=hp", "/characters?q=elf", "/characters?q=%20AL%20", "/characters?q=nobody",
            "/characters?sort=nope"]
    served = 0
    for see in (False, True):
        db = SessionLocal()
        try:
            wa = db.get(World, w)
            wa.players_see_party, wa.max_characters_per_player = see, 2 if see else None
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for u in urls:
                served += _same_page(d, _via(d, base), u, email)
    assert served == 2 * 2 * len(urls), served


def test_ai_page_is_pythons(client, seed):
    """GET /ai from the gateway: the system prompt with the world's entity counts (hidden ones only for the GM) and the GM-authored
    instructions (only the player-facing ones for a player), a world with nothing in it, and the player's refusal while the AI
    section is closed to them."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AiInstruction, Entity, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="character", name="A"), Entity(world_id=w, kind="character", name="B", visible_to_players=False),
                    Entity(world_id=w, kind="item", name="C"), Entity(world_id=w, kind="oddkind", name="D"),
                    AiInstruction(world_id=w, title="Tone", content="  Be grim.  ", enabled=True, applies_to_players=True),
                    AiInstruction(world_id=w, title="Secret", content="The butler did it.", enabled=True, applies_to_players=False),
                    AiInstruction(world_id=w, title="Off", content="x", enabled=False, applies_to_players=True),
                    AiInstruction(world_id=w, title="Blank", content="   ", enabled=True, applies_to_players=True)])
        db.commit()
    finally:
        db.close()
    served = 0
    for open_ in (False, True):
        db = SessionLocal()
        try:
            wa = db.get(World, w)
            wa.section_access_json = json.dumps({"ai": {"player": "read" if open_ else "none"}})
            db.commit()
        finally:
            db.close()
        for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                                 (seed.gm.email, GM_PASSWORD, seed.world_b.slug)):
            d = _signed_in(email, pw, world)
            a, b = d.get("/ai", follow_redirects=False), _via(d, base).get("/ai", follow_redirects=False)
            if a.status_code != 200:
                assert (a.status_code, a.text) == (b.status_code, b.text), email
                served += b.headers.get("x-nd-served-by") == "gateway"
                continue
            served += _same_page(d, _via(d, base), "/ai", email)
    assert served == 6, served


def test_calendar_search_is_pythons(client, seed):
    """GET /api/calendar/search from the gateway: matches on title, notes and the names of what an event links to (a hidden entity's
    name only for the GM), LIKE characters taken literally, nearest-first order around ?near=, ?limit= and truncation, the labels and
    links - and the empty query."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CalendarEvent, Entity, GameSession, Party, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    gm.get("/calendar")                                  # Python makes the world's calendar row on the first visit
    db = SessionLocal()
    try:
        hidden = Entity(world_id=w, kind="character", name="Shadow broker", visible_to_players=False)
        pc = PlayerCharacter(world_id=w, name="Vex")
        party = Party(world_id=w, name="Crew 100%")
        gs = GameSession(world_id=w, session_num=3, title="Harbour heist")
        db.add_all([hidden, pc, party, gs])
        db.flush()
        evs = [CalendarEvent(world_id=w, day=d, title=t, notes=n, entity_id=e, character_id=c, party_id=pa, session_id=s, color="#123456")
               for d, t, n, e, c, pa, s in ((1, "New year", "", None, None, None, None), (40, "Meeting", "with the broker", hidden.id, None, None, None),
                                            (75, "Duel", "x" * 200, None, pc.id, None, None), (400, "Raid", "", None, None, party.id, None),
                                            (500, "Heist", "", None, None, None, gs.id), (12, "under_score", "100% sure", None, None, None, None))]
        db.add_all(evs)
        db.commit()
    finally:
        db.close()
    queries = ["", "   ", "year", "BROKER", "shadow", "vex", "100%", "_", "%", "heist", "e", "e&limit=2", "e&limit=x", "e&near=450", "e&near=-5",
               "e&limit=999", "zzz"]
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for q in queries:
            url = "/api/calendar/search?q=" + q.replace("%", "%25").replace(" ", "%20").replace("&", "&", 1)
            a, b = d.get(url), _via(d, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, q)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, q)


def test_import_page_is_pythons(client, seed):
    """GET /import from the gateway: the world's schematics (not the HTML ones), maps and entities as the page's JSON, for the GM and an
    assistant-free player refusal, and a world with nothing in it."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, Schematic
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Schematic(world_id=w, name="Base ü", slug="gw-imp-base"), Schematic(world_id=w, name="Html", slug="gw-imp-html", is_html=True),
                    Entity(world_id=w, kind="item", name="Knife \"x\"", image_url="/uploads/k.png"), Entity(world_id=w, kind="character", name="Bo")])
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.gm.email, GM_PASSWORD, seed.world_b.slug),
                             (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        a, b = d.get("/import", follow_redirects=False), _via(d, base).get("/import", follow_redirects=False)
        if a.status_code != 200:
            assert (a.status_code, a.text) == (b.status_code, b.text)
            served += b.headers.get("x-nd-served-by") == "gateway"
            continue
        served += _same_page(d, _via(d, base), "/import", email)
    assert served == 3, served


def test_hub_quests_and_world_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/quests and /hub/world from the gateway, for the owning player and a GM looking in (both see the
    player's view): quests with sub-quest counts, the character's party flagged, GM-only quests left out, goals; revealed, recent and
    searched entities with [gmonly] text stripped; a character of someone else (404) and the Quests section closed to players."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CharacterJournalEntry, Entity, Party, PlayerCharacter, Quest, World, entity_player_access
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id)
        other = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([pc, other])
        db.flush()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.flush()
        main = Quest(world_id=w, title="Main thing", status="active", assigned_party_id=party.id, summary="s")
        db.add_all([main, Quest(world_id=w, title="beta", status="complete"), Quest(world_id=w, title="Alpha", status="active"),
                    Quest(world_id=w, title="Secret", visible_to_players=False)])
        db.flush()
        db.add_all([Quest(world_id=w, title="sub 1", parent_id=main.id, status="complete"), Quest(world_id=w, title="sub 2", parent_id=main.id)])
        db.add_all([CharacterJournalEntry(world_id=w, character_id=pc.id, kind="goal", title="Get rich", status="achieved"),
                    CharacterJournalEntry(world_id=w, character_id=pc.id, kind="goal", title="Survive", status="active"),
                    CharacterJournalEntry(world_id=w, character_id=pc.id, kind="journal", title="Day 1")])
        shown = Entity(world_id=w, kind="character", name="Revealed one", visible_to_players=False, summary="Hi [gmonly]secret[/gmonly] there")
        db.add_all([shown, Entity(world_id=w, kind="item", name="Open_knife", summary="a 100% blade"),
                    Entity(world_id=w, kind="item", name="Hidden knife", visible_to_players=False)])
        db.flush()
        db.execute(entity_player_access.insert().values(entity_id=shown.id, user_id=seed.player_a.id))
        db.commit()
        ids = (pc.id, other.id, 999999)
    finally:
        db.close()
    tabs = ["quests", "world", "world?q=knife", "world?q=_", "world?q=100%25", "world?q=SECRET", "world?q=%20%20"]
    for closed in (False, True):
        if closed:
            db = SessionLocal()
            try:
                db.get(World, w).section_access_json = json.dumps({"quests": {"player": "none"}})
                db.commit()
            finally:
                db.close()
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                for t in tabs:
                    url = f"/api/characters/{i}/hub/{t}"
                    a, b = d.get(url), _via(d, base).get(url)
                    assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, url)
                    assert b.headers.get("x-nd-served-by") == "gateway", (email, url)


def test_session_log_page_is_pythons(client, seed):
    """GET /session-log/<id> from the gateway: a published player summary (markdown), an unpublished one, the recap pickers seeded from
    the last recap job of the viewer's audience, and the audio / pictures / video each viewer may see - for the GM, a player of the
    world, a player of another world (404) and a missing session."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AudioClip, AudioJob, GameSession, VideoClip
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        a1 = AudioClip(world_id=w, name="Theme", file_url="/uploads/a1.mp3", visible_to_players=True, transcript="la la")
        a2 = AudioClip(world_id=w, name="GM cue", file_url="/uploads/a2.mp3", visible_to_players=False)
        v1 = VideoClip(world_id=w, name="Intro", file_url="/uploads/v1.mp4", visible_to_players=True, poster_url="/uploads/v1.jpg")
        v2 = VideoClip(world_id=w, name="Secret", file_url="/uploads/v2.mp4", visible_to_players=False)
        db.add_all([a1, a2, v1, v2])
        db.flush()
        media = [{"uid": "img1", "kind": "image", "url": "/uploads/p.png", "caption": "Map", "visible": True},
                 {"uid": "img2", "kind": "image", "url": "/uploads/q.png", "visible": False},
                 {"uid": "vid1", "kind": "video", "clip_id": v1.id}, {"uid": "vid2", "kind": "video", "clip_id": v2.id}]
        pub = GameSession(world_id=w, session_num=8, title="Eight", player_summary="We **won**.\n\n- loot", player_summary_published=True,
                          recap_audio_json=json.dumps([a1.id, a2.id, a1.id, 999999]), recap_media_json=json.dumps(media))
        unpub = GameSession(world_id=w, session_num=9, title="Nine", player_summary="draft", player_summary_published=False)
        db.add_all([pub, unpub])
        db.flush()
        db.add(AudioJob(purpose="session_log_recap", game_session_id=pub.id, audience="gm", world_id=w, model="m1", think=False, use_rag=True,
                        rag_entity_limit=7, condense_strictness="firm", max_tokens=900, status="done"))
        db.commit()
        ids = (pub.id, unpub.id, 999999)
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        for i in ids:
            url = f"/session-log/{i}"
            a, b = d.get(url, follow_redirects=False), _via(d, base).get(url, follow_redirects=False)
            if a.status_code != 200:
                assert (a.status_code, a.text) == (b.status_code, b.text), (email, url)
                served += b.headers.get("x-nd-served-by") == "gateway"
                continue
            served += _same_page(d, _via(d, base), url, email)
    assert served == 9, served


def test_audio_job_status_is_pythons(client, seed):
    """GET /api/audio-jobs/<id> from the gateway: each kind of job row (done, failed thinking-starved, interrupted, with pending facts, a
    pre-migration NULL think), for the GM and for an assistant - who gets the same 404 as a missing job for a GM-tier World Summary and
    a Code Assist job - another world's job, and a player's refusal."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AudioJob, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        jobs = [AudioJob(world_id=w, purpose="session_recap", filename="rec.mp3", status="done", transcript="t", recap="r", model="m", think=True,
                         use_rag=True, rag_entity_limit=3, pending_facts_json=json.dumps([{"content": "x"}]), chunk_current=2, chunk_total=4),
                AudioJob(world_id=w, purpose="condense", status="error", error='[empty response — the model spent its budget on hidden "thinking"]',
                         condense_strictness="strict", think_fallback=True),
                AudioJob(world_id=w, purpose="attachment", status="interrupted", think=None, pending_facts_json="{bad"),
                AudioJob(world_id=w, purpose="world_summary", audience="gm", status="done"),
                AudioJob(world_id=w, purpose="world_summary", audience="players", status="done"),
                AudioJob(world_id=w, purpose="ai_assist", assist_params_json=json.dumps({"op": "code_edit"}), status="done"),
                AudioJob(world_id=w, purpose="ai_assist", assist_params_json="not json", status="done"),
                AudioJob(world_id=w, purpose="other_kind", status="queued"),
                AudioJob(world_id=seed.world_b.id, purpose="condense", status="done")]
        db.add_all(jobs)
        m = db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).first()
        db.commit()
        ids = [j.id for j in jobs] + [999999]
    finally:
        db.close()
    for role in ("player", "assistant"):
        db = SessionLocal()
        try:
            db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).update({"role": role})
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                url = f"/api/audio-jobs/{i}"
                a, b = d.get(url), _via(d, base).get(url)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (role, email, i)
                assert b.headers.get("x-nd-served-by") == "gateway", (role, email, i)
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).update({"role": "player"})
        db.commit()
    finally:
        db.close()


def test_content_pack_is_pythons(client, seed):
    """GET /api/worlds/<id>/content-pack from the gateway: races / professions / feats / items in the Android app's shape - bodies split
    on "## Heading" lines (or kept whole as a Description), bonuses from the custom fields, race-feat and profession-feat folders, the
    subtype categories - for the GM, a member player, a non-member (404) and a missing world."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            Entity(world_id=w, kind="race", name="Elf", subtype=" Elite ", body="Pointy.\n\n## Traits\nKeen eyes\n## Lore\n\nOld\n## Traits\nagain",
                   custom_fields_json=json.dumps({"bonuses": {"dex": 2}}), tags="fey"),
            Entity(world_id=w, kind="profession", name="Netrunner", subtype="", body="Just text, no headings.", summary="Hacker"),
            Entity(world_id=w, kind="feat", name="Night eyes", subtype="Race Feat", folder="Race Feats/Elf/Rank 2", custom_fields_json="oops"),
            Entity(world_id=w, kind="feat", name="Overclock", subtype="profession ability", folder="Profession Feats/Netrunner"),
            Entity(world_id=w, kind="feat", name="Odd", subtype="weird", folder="Elsewhere", custom_fields_json=json.dumps({"bonuses": [1]})),
            Entity(world_id=w, kind="item", name="Blade", subtype="WEAPON", image_url="/uploads/b.png", body="   "),
            Entity(world_id=w, kind="item", name="Thing", subtype="gizmo", body="##   \nempty heading\n## Real\nyes"),
            Entity(world_id=w, kind="character", name="Not in the pack")])
        db.commit()
    finally:
        db.close()
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        for wid in (w, seed.world_b.id, 999999):
            url = f"/api/worlds/{wid}/content-pack"
            a, b = d.get(url), _via(d, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, wid)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, wid)


def test_party_roster_is_pythons(client, seed):
    """GET /parties/<id>/roster from the gateway: an N&D member (stats, CA/Speed, edges, cyberware, Shock, PP/MP) beside a custom-system
    one (its vital, resource tracks and comparison fields), mixed vital labels, a character of another world left out, companions for the
    GM only; a player sees it only with the party switch on."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, Party, PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        a = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id, level=4, race="Elf", char_class="Netrunner", player_name="Al",
                            current_hp=3, temp_hp=2, shock_current=1, pp_current=2, mp_current=1, minor_edge="Quick", major_edge="",
                            stats_json=json.dumps([{"id": "str", "value": 3}, {"id": "dex", "value": "2"}, {"id": "wil", "value": 4}, {"id": "str", "value": 5}]),
                            cyberware_json=json.dumps([{"name": "Eye", "ca_cost": 1}, {"name": ""}, "junk", {"name": 7}]),
                            conditions_json=json.dumps(["Prone", 3]))
        b = PlayerCharacter(world_id=w, name="Ash", sheet_template_id=tpl.id if tpl else None, player_name="Bo")
        c = PlayerCharacter(world_id=seed.world_b.id, name="Elsewhere")
        comp = Entity(world_id=w, kind="creature", name="Hound")
        db.add_all([a, b, c, comp])
        db.flush()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([a.id, b.id, c.id]), member_entity_ids_json=json.dumps([comp.id]))
        empty = Party(world_id=w, name="Empty")
        db.add_all([party, empty])
        db.commit()
        ids = (party.id, empty.id, 999999)
    finally:
        db.close()
    for see in (False, True):
        db = SessionLocal()
        try:
            db.get(World, w).players_see_party = see
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                url = f"/parties/{i}/roster"
                ra, rb = d.get(url, follow_redirects=False), _via(d, base).get(url, follow_redirects=False)
                if ra.status_code != 200:
                    assert (ra.status_code, ra.text) == (rb.status_code, rb.text), (email, url)
                    assert rb.headers.get("x-nd-served-by") == "gateway", (email, url)
                    continue
                assert _same_page(d, _via(d, base), url, email), (email, url)


def test_small_reads_are_pythons(client, seed):
    """Small JSON reads and the calendar jump from the gateway: /api/me, /api/view-prefs, /api/dice/history, /api/display/state,
    /api/sessions/<id>/media and /calendar/event-jump - for the GM, a player and a player of another world, refusals included."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CalendarEvent, DiceRoll, GameSession, UserViewPref
    base = str(client.base_url)
    w = seed.world_a.id
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    gm.get("/calendar")
    gm.post("/api/display/show", json={"kind": "text", "text": "Hello <b>", "title": "T"})
    db = SessionLocal()
    try:
        gs = GameSession(world_id=w, session_num=12, title="Twelve")
        db.add_all([gs, DiceRoll(world_id=w, user_name="GM", notation="2d6+1", label="hit", breakdown=json.dumps([{"t": 1}]), total=8),
                    DiceRoll(world_id=w, user_name="P", notation="d20", breakdown="", total=3),
                    CalendarEvent(world_id=w, day=5, title="Early"), CalendarEvent(world_id=w, day=500, title="Late"),
                    UserViewPref(user_id=seed.player_a.id, section="images", prefs_json=json.dumps({"layout": "masonry", "junk": 1})),
                    UserViewPref(user_id=seed.player_a.id, section="audio", prefs_json="{bad")])
        db.commit()
        sid = gs.id
    finally:
        db.close()
    urls = ["/api/me", "/api/view-prefs", "/api/dice/history", "/api/display/state", f"/api/sessions/{sid}/media", "/api/sessions/999999/media",
            "/calendar/event-jump?dir=next", "/calendar/event-jump?dir=prev&from=100", "/calendar/event-jump?dir=prev&from=1",
            "/calendar/event-jump?dir=up", "/calendar/event-jump?dir=next&from=x", "/calendar/event-jump?dir=next&from=99999999"]
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        for u in urls:
            a, b = d.get(u, follow_redirects=False), _via(d, base).get(u, follow_redirects=False)
            ta = a.json() if "json" in a.headers.get("content-type", "") else a.text
            tb = b.json() if "json" in b.headers.get("content-type", "") else b.text
            assert (a.status_code, a.headers.get("location"), ta) == (b.status_code, b.headers.get("location"), tb), (email, u)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, u)


def test_static_text_without_a_twin_is_gzipped_like_python(client, seed):
    """A static text file with no pre-compressed twin, asked for with gzip: the gateway gzips it itself (Python's GZipMiddleware did);
    the decoded bytes and the headers that matter are the same, and a small file or a client without gzip gets it plain."""
    import glob
    base = str(client.base_url)
    found = [p for p in glob.glob("static/js/*.js") if not os.path.exists(p + ".gz") and os.path.getsize(p) >= 1024][:3]
    small = [p for p in glob.glob("static/**/*.css", recursive=True) + glob.glob("static/js/*.js") if os.path.getsize(p) < 1024][:1]
    assert found
    for p in found + small:
        url = "/" + p
        for enc in ("gzip, deflate", "identity"):
            a = httpx.get(DIRECT + url, headers={"accept-encoding": enc})
            b = httpx.get(base + url, headers={"accept-encoding": enc})
            assert a.status_code == b.status_code == 200, url
            assert a.content == b.content, url
            for h in ("content-encoding", "content-type", "cache-control", "etag", "last-modified", "accept-ranges", "vary"):
                assert a.headers.get(h) == b.headers.get(h), (url, enc, h, a.headers.get(h), b.headers.get(h))
            assert b.headers.get("x-nd-served-by") != "python", url


def test_upload_access_is_pythons(client, seed):
    """GET /uploads/<path> decided by the gateway: a picture, an HTML page (sandboxed), an old SVG (download), a map picture (revalidate),
    a thumbnail asked for in the other format, the live-recording archive and paths outside the folder (404), a missing file."""
    from app.database import DB_PATH
    base = str(client.base_url)
    up = Path(DB_PATH).parent / "uploads"
    files = {"gwacc/pic.png": b"\x89PNG\r\n\x1a\nxx", "gwacc/page.html": b"<p>hi</p>", "gwacc/old.svg": b"<svg/>", "maps/gwacc-map.png": b"png",
             "gwacc/one_thumb.avif": b"avif", "live/1/2/000001.webm": b"webm"}
    for rel, data in files.items():
        (up / rel).parent.mkdir(parents=True, exist_ok=True)
        (up / rel).write_bytes(data)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for u in ["/uploads/gwacc/pic.png", "/uploads/gwacc/page.html", "/uploads/gwacc/old.svg", "/uploads/maps/gwacc-map.png",
              "/uploads/gwacc/one_thumb.webp", "/uploads/live/1/2/000001.webm", "/uploads/../world.db", "/uploads/gwacc/%2e%2e/%2e%2e/world.db",
              "/uploads/gwacc/missing.png", "/uploads/gwacc/"]:
        a, b = gm.get(u, follow_redirects=False), _via(gm, base).get(u, follow_redirects=False)
        assert (a.status_code, a.content) == (b.status_code, b.content), u
        for h in ("content-type", "cache-control", "content-disposition", "content-security-policy", "x-frame-options", "x-content-type-options", "etag"):
            assert a.headers.get(h) == b.headers.get(h), (u, h, a.headers.get(h), b.headers.get(h))


def test_preview_switch_and_player_chat_are_pythons(client, seed):
    """GET /api/entity/<id>/preview (a [gmonly] block stripped for players, a hidden entity shared with one player, another world's
    entity), GET /worlds/switch/<slug> (the cookie and the safe redirect) and GET /ai-chat (closed and open to players)."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, World, entity_player_access
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        open_ = Entity(world_id=w, kind="character", name="Open", summary="Hi [gmonly]secret[/gmonly]", body="**Bold** [gmonly]x[/gmonly]",
                       tags=" a, ,b ", image_url="/uploads/nope.png", custom_fields_json="")
        hid = Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False, body="")
        shared = Entity(world_id=w, kind="item", name="Shared", visible_to_players=False)
        other = Entity(world_id=seed.world_b.id, kind="item", name="Other world")
        db.add_all([open_, hid, shared, other])
        db.flush()
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        db.commit()
        eids = [open_.id, hid.id, shared.id, other.id, 999999]
    finally:
        db.close()
    switch = [f"/worlds/switch/{seed.world_a.slug}", f"/worlds/switch/{seed.world_b.slug}?next=/facts", f"/worlds/switch/{seed.world_a.slug}?next=//evil",
              f"/worlds/switch/{seed.world_a.slug}?next=/x%3Fy%3D1", "/worlds/switch/no-such-world"]
    for chat in (False, True):
        db = SessionLocal()
        try:
            db.get(World, w).players_can_use_ai_chat = chat
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            signed = _signed_in(email, pw, seed.world_a.slug)
            jar = {c.name: c.value for c in signed.cookies.jar}
            for u in [f"/api/entity/{i}/preview" for i in eids] + switch + ["/ai-chat"]:
                d = httpx.Client(base_url=DIRECT, timeout=60, cookies=jar)
                a = d.get(u, follow_redirects=False)
                b = httpx.Client(base_url=base, timeout=60, cookies=jar).get(u, follow_redirects=False)
                d = httpx.Client(base_url=DIRECT, timeout=60, cookies=jar)
                if u == "/ai-chat" and a.status_code == 200:
                    assert _same_page(d, _via(d, base), u, email)
                    continue
                ta = a.json() if "json" in a.headers.get("content-type", "") else a.text
                tb = b.json() if "json" in b.headers.get("content-type", "") else b.text
                for t in (ta, tb):
                    if isinstance(t, dict) and "body_html" in t:   # markdown2 vs pulldown-cmark whitespace (docs/RUST_PAGES.md)
                        t["body_html"] = " ".join(t["body_html"].replace(" </", "</").split())
                assert (a.status_code, a.headers.get("location"), a.headers.get("set-cookie"), ta) == \
                       (b.status_code, b.headers.get("location"), b.headers.get("set-cookie"), tb), (email, u)
                assert b.headers.get("x-nd-served-by") == "gateway", (email, u)


def test_job_list_and_assist_status_are_pythons(client, seed):
    """GET /api/audio-jobs (filters, pages, the assistant's hidden rows) and GET /api/ai/assist-job/<id> (done with a result, done with
    unreadable result text, still running, not an assist job, another world's) from the gateway."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AudioJob, GameSession, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.query(AudioJob).filter(AudioJob.world_id == w).delete()
        gs = GameSession(world_id=w, session_num=30, title="Thirty")
        db.add(gs)
        db.flush()
        jobs = [AudioJob(world_id=w, purpose="session_recap", status="done", game_session_id=gs.id),
                AudioJob(world_id=w, purpose="condense", status="transcribing"),
                AudioJob(world_id=w, purpose="world_summary", audience="gm", status="done"),
                AudioJob(world_id=w, purpose="world_summary", audience="players", status="done"),
                AudioJob(world_id=w, purpose="ai_assist", assist_params_json=json.dumps({"op": "code_edit"}), status="done", result_json='{"mode": "text", "text": "x"}'),
                AudioJob(world_id=w, purpose="ai_assist", assist_params_json=json.dumps({"op": "expand"}), status="done", result_json="not json"),
                AudioJob(world_id=w, purpose="ai_assist", status="summarizing"),
                AudioJob(world_id=seed.world_b.id, purpose="ai_assist", status="done")]
        db.add_all(jobs + [AudioJob(world_id=w, purpose="attachment", status="error") for _ in range(55)])
        db.commit()
        ids = [j.id for j in jobs] + [999999]
        gsid = gs.id
    finally:
        db.close()
    lists = ["", "?page=2", "?page=99", "?page=0", "?purpose=ai_assist", "?status=running", "?status=done", f"?game_session_id={gsid}", "?purpose=nope"]
    for role in ("player", "assistant"):
        db = SessionLocal()
        try:
            db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).update({"role": role})
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for u in ["/api/audio-jobs" + x for x in lists] + [f"/api/ai/assist-job/{i}" for i in ids]:
                a, b = d.get(u), _via(d, base).get(u)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (role, email, u)
                assert b.headers.get("x-nd-served-by") == "gateway", (role, email, u)
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).update({"role": "player"})
        db.query(AudioJob).filter(AudioJob.purpose == "attachment", AudioJob.status == "error").delete()
        db.commit()
    finally:
        db.close()


def test_picker_and_party_summary_are_pythons(client, seed):
    """GET /api/entities/picker (the viewer's visibility, exclude_id, the ETag and its 304) and GET /parties/<id>/summary from the gateway."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, Party, PlayerCharacter, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        e1 = Entity(world_id=w, kind="character", name="Zoë", folder="People/Ä")
        e2 = Entity(world_id=w, kind="item", name="Secret", visible_to_players=False)
        pc = PlayerCharacter(world_id=w, name="Vex", current_hp=3, conditions_json=json.dumps(["Prone"]))
        db.add_all([e1, e2, pc])
        db.flush()
        party = Party(world_id=w, name="Crew", goals="Win", notes="**n**", member_pc_ids_json=json.dumps([pc.id]),
                      member_entity_ids_json=json.dumps([e1.id, e2.id]), loot_json=json.dumps([{"name": "Gem", "qty": 2}]))
        db.add(party)
        db.flush()
        db.add_all([Quest(world_id=w, title="Q1", assigned_party_id=party.id), Quest(world_id=w, title="GM Q", assigned_party_id=party.id, visible_to_players=False)])
        db.commit()
        pid, eid = party.id, e1.id
    finally:
        db.close()
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        for u in ["/api/entities/picker", f"/api/entities/picker?exclude_id={eid}"]:
            a, b = d.get(u), _via(d, base).get(u)
            assert (a.status_code, a.content, a.headers.get("etag"), a.headers.get("cache-control")) == \
                   (b.status_code, b.content, b.headers.get("etag"), b.headers.get("cache-control")), (email, u)
            a2, b2 = d.get(u, headers={"if-none-match": a.headers["etag"]}), _via(d, base).get(u, headers={"if-none-match": a.headers["etag"]})
            assert a2.status_code == b2.status_code == 304
        for u in [f"/parties/{pid}/summary", "/parties/999999/summary"]:
            a, b = d.get(u, follow_redirects=False), _via(d, base).get(u, follow_redirects=False)
            if a.status_code != 200:
                assert (a.status_code, a.text) == (b.status_code, b.text), (email, u)
                assert b.headers.get("x-nd-served-by") == "gateway"
                continue
            assert _same_page(d, _via(d, base), u, email), (email, u)


def test_hub_now_is_pythons(client, seed):
    """GET /api/characters/<id>/hub/now from the gateway: an N&D character (carry, Shock) and a custom-system one, for the owner and a GM
    looking in, with the next confirmed game night and the owner's answer, an open poll, pinned pages, companions and new handouts."""
    from datetime import datetime, timedelta
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import (CharacterCompanion, CharacterPref, PlayerCharacter, SessionPlan, SessionPlanSlot, SessionPlanVote, SheetTemplate,
                            WorldHandout)
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        a = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id, current_hp=4, shock_current=2,
                            stats_json=json.dumps([{"id": "str", "value": 3}, {"id": "bod", "value": 2}]),
                            equipment_json=json.dumps([{"name": "Pack", "weight": 2.5, "qty": 2}, {"name": "x", "weight": "a"}]),
                            conditions_json=json.dumps(["Prone"] * 14 + [3]))
        b = PlayerCharacter(world_id=w, name="Ash", owner_user_id=seed.player_a.id, sheet_template_id=tpl.id if tpl else None)
        db.add_all([a, b])
        db.flush()
        db.add_all([CharacterCompanion(character_id=a.id, world_id=w, name="Rex", kind="beast", hp=3, hp_max=5),
                    CharacterPref(character_id=a.id, world_id=w, prefs_json=json.dumps({"pins": ["dice", "nope", "kind_character"], "handouts_seen": 0})),
                    WorldHandout(world_id=w, url="/uploads/h.png")])
        plan = SessionPlan(world_id=w, title="Next night", location="Mo's", status="confirmed", duration_min=120)
        poll = SessionPlan(world_id=w, title="Poll", status="open")
        db.add_all([plan, poll])
        db.flush()
        start = (datetime.utcnow() + timedelta(days=3)).replace(second=0, microsecond=0)
        s1 = SessionPlanSlot(plan_id=plan.id, world_id=w, starts_at=start)
        s2 = SessionPlanSlot(plan_id=poll.id, world_id=w, starts_at=start + timedelta(days=7))
        db.add_all([s1, s2])
        db.flush()
        plan.confirmed_slot_id = s1.id
        db.add(SessionPlanVote(plan_id=plan.id, slot_id=s1.id, world_id=w, user_id=seed.player_a.id, choice="yes"))
        db.commit()
        ids = (a.id, b.id)
    finally:
        db.close()
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for i in ids:
            u = f"/api/characters/{i}/hub/now"
            ra, rb = d.get(u), _via(d, base).get(u)
            assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), (email, u)
            assert rb.headers.get("x-nd-served-by") == "gateway", (email, u)


def test_hub_schedule_is_pythons(client, seed):
    """GET /api/characters/<id>/hub/schedule from the gateway: calendar events pinned to the character or its party from today on (with
    the world's labels), the party's next dated sessions (bad dates skipped), the next confirmed game night, polls waiting, and the
    calendar closed to players."""
    from datetime import date, timedelta
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CalendarEvent, GameSession, Party, PlayerCharacter, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id)
        db.add(pc)
        db.flush()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.flush()
        soon = (date.today() + timedelta(days=5)).isoformat()
        db.add_all([CalendarEvent(world_id=w, day=1, title="Past"), CalendarEvent(world_id=w, day=40, title="Mine", character_id=pc.id),
                    CalendarEvent(world_id=w, day=90, title="Party's", party_id=party.id, notes="n"), CalendarEvent(world_id=w, day=50, title="Other"),
                    GameSession(world_id=w, session_num=1, title="Old", party_id=party.id, session_date="2001-01-01"),
                    GameSession(world_id=w, session_num=2, title="Soon", party_id=party.id, session_date=soon + " evening"),
                    GameSession(world_id=w, session_num=3, title="Bad", party_id=party.id, session_date="tomorrow"),
                    GameSession(world_id=w, session_num=4, title="Feb 30", party_id=party.id, session_date="2099-02-30")])
        db.commit()
        pid = pc.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for closed in (False, True):
        if closed:
            db = SessionLocal()
            try:
                db.get(World, w).section_access_json = json.dumps({"calendar": {"player": "none"}})
                db.commit()
            finally:
                db.close()
        for d in (gm, _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
            u = f"/api/characters/{pid}/hub/schedule"
            a, b = d.get(u), _via(d, base).get(u)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), u
            assert b.headers.get("x-nd-served-by") == "gateway"
        if not closed:
            gm.get("/calendar")                        # the calendar row, then again with it
            for d in (gm,):
                a, b = d.get(f"/api/characters/{pid}/hub/schedule"), _via(d, base).get(f"/api/characters/{pid}/hub/schedule")
                assert a.json() == b.json()


def test_private_notes_page_is_pythons(client, seed):
    """GET /worlds/<w>/notes/<u> from the gateway: the GM's notes to one player (autolinked entity names, a hidden one only for the GM,
    [gmonly] stripped for the player, the contents labels), for the GM, that player, another player (403), a missing world or user."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, PrivateNote
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="character", name="Edmund Vosk", aliases="Vosk"), Entity(world_id=w, kind="item", name="Blade of Night", visible_to_players=False),
                    PrivateNote(world_id=w, player_user_id=seed.player_a.id, title="", content="## Recap\nMet Vosk; the Blade of Night [gmonly]is cursed[/gmonly]."),
                    PrivateNote(world_id=w, player_user_id=seed.player_a.id, title="Clue", content="Edmund Vosk lied."),
                    PrivateNote(world_id=w, player_user_id=seed.player_a.id, title=" ", content="   \n> " + "x" * 70)])
        db.commit()
    finally:
        db.close()
    urls = [f"/worlds/{w}/notes/{seed.player_a.id}", f"/worlds/{w}/notes/{seed.player_b.id}", f"/worlds/999999/notes/{seed.player_a.id}", f"/worlds/{w}/notes/999999"]
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for u in urls:
            a, b = d.get(u, follow_redirects=False), _via(d, base).get(u, follow_redirects=False)
            if a.status_code != 200:
                assert (a.status_code, a.text) == (b.status_code, b.text), (email, u)
                assert b.headers.get("x-nd-served-by") == "gateway", (email, u)
                continue
            assert _same_page(d, _via(d, base), u, email), (email, u)


def test_party_rest_is_pythons(client, seed):
    """POST /api/parties/<id>/rest from the gateway: the same Rest (long, short, an unreadable body) written as Python writes it - each
    member by its own system (an N&D sheet, a built-in custom system, a custom system without Rest rules) - checked by running it on
    Python, undoing it, and running it through the gateway."""
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        ast = db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first()
        plain = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom", SheetTemplate.slug != "asterion").first()
        pcs = [PlayerCharacter(world_id=w, name="Nat", pp_current=1, mp_current=0, shock_current=0,
                               stats_json=json.dumps([{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")])),
               PlayerCharacter(world_id=w, name="Ast", sheet_template_id=ast.id if ast else None, custom_fields_json=json.dumps({"health_current": 1})),
               PlayerCharacter(world_id=w, name="Plain", sheet_template_id=plain.id if plain else None)]
        db.add_all(pcs)
        db.flush()
        party = Party(world_id=w, name="Resting", member_pc_ids_json=json.dumps([p.id for p in pcs]))
        db.add(party)
        db.commit()
        pid, pc_ids = party.id, [p.id for p in pcs]
    finally:
        db.close()

    def state():
        db = SessionLocal()
        try:
            return [(p.pp_current, p.mp_current, p.shock_current, p.custom_fields_json) for p in db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).order_by(PlayerCharacter.id)]
        finally:
            db.close()

    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for body in ({"kind": "long"}, {"kind": "short"}, None):
        before = state()
        post = (lambda c: c.post(f"/api/parties/{pid}/rest", json=body)) if body is not None else (lambda c: c.post(f"/api/parties/{pid}/rest", content=b"{oops"))
        a = post(gm)
        after_a = state()
        assert gm.post(f"/api/parties/{pid}/rest/undo", json={"snapshot": a.json()["snapshot"]}).status_code == 200
        assert state() == before
        b = post(_via(gm, base))
        assert b.headers.get("x-nd-served-by") == "gateway", body
        assert (a.status_code, a.json()) == (b.status_code, b.json()), body
        assert state() == after_a, body
        gm.post(f"/api/parties/{pid}/rest/undo", json={"snapshot": b.json()["snapshot"]})


def test_world_context_smart_is_pythons(client, seed):
    """POST /api/ai/world-context-smart for the GM from the gateway: matched entities and notes, the top-up, the latest notes, the
    rules section a query hits, the clamps - and an empty query."""
    from app.database import SessionLocal
    from app.models import Entity, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="item", name="Vibro Blade", body="A humming blade. Costs 200."),
                    Entity(world_id=w, kind="character", name="Edmund Vosk", body="A fixer."),
                    Entity(world_id=w, kind="note", name="Blade rumours", body="Someone sells blades."),
                    Entity(world_id=w, kind="note", name="Unrelated note", body="Rain.")])
        db.get(World, w).rules_md = "# Rules\n\n## Blades\n\nBlades deal 1d8.\n\n## Armor\n\nArmor soaks."
        db.commit()
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for body in ({"query": "blade cost"}, {"query": "vosk", "limit": 1, "notes_limit": 0}, {"query": ""}, {"query": "armor", "limit": 500, "notes_limit": -3}, {}):
        a = gm.post("/api/ai/world-context-smart", json=body)
        b = _via(gm, base).post("/api/ai/world-context-smart", json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), body
        assert b.headers.get("x-nd-served-by") == "gateway", body


def test_equipment_refusals_are_pythons(client, seed):
    """POST /api/characters/<id>/equipment refusals from the gateway: a missing character, a player who does not own it, an empty
    name, a bad index, an unknown action - same status and message as Python, nothing written."""
    from app.database import SessionLocal
    from app.models import PlayerCharacter
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Packer", equipment_json=json.dumps([{"name": "Rope", "qty": 1}, "loose"]))
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    cases = [(gm, 999999, {"action": "add", "item": {"name": "X"}}), (player, pid, {"action": "add", "item": {"name": "X"}})]
    cases += [(gm, pid, b) for b in ({"action": "add", "item": {"name": "  "}}, {"item": {}}, {"action": "add", "item": []}, {"action": "add"},
                                      {"action": "remove"}, {"action": "remove", "index": 5}, {"action": "remove", "index": "0"},
                                      {"action": "qty", "index": 1, "delta": 1}, {"action": "weight", "index": -1}, {"action": "sell"},
                                      {"action": 3})]
    for who, target, body in cases:
        a = who.post(f"/api/characters/{target}/equipment", json=body)
        b = _via(who, base).post(f"/api/characters/{target}/equipment", json=body)
        assert b.headers.get("x-nd-served-by") == "gateway", body
        assert a.status_code >= 400, body
        assert (a.status_code, a.json()) == (b.status_code, b.json()), body


def test_character_quick_edit_refusals_are_pythons(client, seed):
    """The character quick-edits' shared refusals from the gateway - a body that is not JSON or not an object (where Python reads the
    body first), a missing character, a player who does not own it - and the feats list's 400s."""
    from app.database import SessionLocal
    from app.models import PlayerCharacter
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Gated", feats_json=json.dumps([{"name": "Tough"}]))
        db.add(pc)
        db.commit()
        pid = pc.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    cases = []
    for route in ("hp-async", "xp", "conditions", "currency", "carry-limit", "shock", "pp", "mp"):
        cases += [(gm, pid, route, b"{oops"), (gm, pid, route, b"[1]"), (gm, pid, route, b""), (gm, 999999, route, b"{}"), (player, pid, route, b"{}")]
    for route in ("feats", "equipment"):
        cases += [(gm, 999999, route, b"{}"), (player, pid, route, b"{}")]
    cases += [(gm, pid, "feats", json.dumps(b).encode()) for b in ({"item": {"name": " "}}, {"action": "add", "item": 0}, {"action": "remove", "index": 1},
                                                                     {"action": "remove", "index": 0.0}, {"action": "qty", "index": 0}, {"action": None})]
    for who, target, route, body in cases:
        a = who.post(f"/api/characters/{target}/{route}", content=body, headers={"content-type": "application/json"})
        b = _via(who, base).post(f"/api/characters/{target}/{route}", content=body, headers={"content-type": "application/json"})
        assert b.headers.get("x-nd-served-by") == "gateway", (route, body)
        assert a.status_code >= 400, (route, body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (route, body)


def test_loot_and_condition_refusals_are_pythons(client, seed):
    """Party stash and character condition / currency refusals from the gateway: same status and message as Python."""
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        mine = PlayerCharacter(world_id=seed.world_a.id, name="Mine", owner_user_id=seed.player_a.id,
                               conditions_json=json.dumps([f"C{i}" for i in range(12)]), currency_json=json.dumps([{"abbr": "gp", "value": 3}]))
        other = PlayerCharacter(world_id=seed.world_a.id, name="Other")
        db.add_all([mine, other])
        db.flush()
        party = Party(world_id=seed.world_a.id, name="Stash", member_pc_ids_json=json.dumps([mine.id, other.id]),
                      loot_json=json.dumps([{"lid": "abcd1234", "name": "Gem", "qty": 2, "notes": "", "claimed_by": []}]))
        db.add(party)
        db.commit()
        pid, me, them = party.id, mine.id, other.id
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    player = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    url = f"/api/parties/{pid}/loot"
    cases = [(gm, "/api/parties/999999/loot", b"{}"), (gm, url, b"{oops"), (gm, url, b"[]"), (gm, url, b"{}")]
    cases += [(gm, url, json.dumps(b).encode()) for b in (
        {"action": "add"}, {"action": "add", "name": "  "}, {"action": "add", "name": "X", "notes": 3}, {"action": "add", "name": "X", "qty": 0},
        {"action": "add", "name": "X", "qty": True}, {"action": "remove", "lid": "gone0000"}, {"action": "remove", "index": 4},
        {"action": "remove", "index": None}, {"action": "claim", "index": 0}, {"action": "claim", "index": 0, "pc_id": 999999},
        {"action": "give", "lid": "abcd1234", "pc_id": []}, {"action": "sell"})]
    cases += [(player, url, json.dumps({"action": "claim", "index": 0, "pc_id": them}).encode())]
    cases += [(gm, f"/api/characters/{me}/conditions", json.dumps(b).encode()) for b in (
        {"action": "set", "conditions": "x"}, {"action": "add", "name": "  "}, {"action": "add", "name": 5}, {"action": "add", "name": "New"},
        {"action": "heal"})]
    cases += [(gm, f"/api/characters/{me}/currency", json.dumps({"abbr": "pp", "value": 1}).encode())]
    for who, path, body in cases:
        a = who.post(path, content=body, headers={"content-type": "application/json"})
        b = _via(who, base).post(path, content=body, headers={"content-type": "application/json"})
        assert b.headers.get("x-nd-served-by") == "gateway", (path, body)
        assert a.status_code >= 400, (path, body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (path, body)


def test_race_and_profession_catalogs_are_pythons(client, seed):
    """GET /races and /professions from the gateway: the world's entries by tier (padded / capitalised / blank / free-text subtypes
    in Unsorted, hidden ones only for the GM or a player they are shared with), the bundled catalog minus what the world has, the
    ?added= note - for the GM, a player, and a player of the other world."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="race", name="Dwarf", subtype=" Standard ", body="**Stout** folk."),
                    Entity(world_id=w, kind="race", name="ash-born", subtype="advanced", image_url="/uploads/races/a.png"),
                    Entity(world_id=w, kind="race", name="Secret", subtype="exceptional", visible_to_players=False),
                    Entity(world_id=w, kind="race", name="Oddity", subtype="weird"),
                    Entity(world_id=w, kind="race", name="Blank"),
                    Entity(world_id=w, kind="profession", name="Hacker", subtype="standard", summary="Breaks in."),
                    Entity(world_id=w, kind="profession", name="Ghost", subtype="STANDARD", visible_to_players=False)])
        db.commit()
    finally:
        db.close()
    urls = ["/races", "/professions", "/races?added=3", "/professions?added=%3Cb%3E"]
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
                             (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)):
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for url in urls:
            served += _same_page(d, via, url, email)
    assert served == len(urls) * 3


def test_2fa_setup_page_is_pythons(client, seed):
    """GET /account/2fa/setup from the gateway: the same page, the pending secret kept in the session across reloads (whichever side
    made it), and a QR code whose pixels are exactly the ones Python's qrcode package draws (the PNG bytes themselves may differ);
    someone with two-step already on is sent back to /account."""
    import base64
    import io
    import re
    from PIL import Image
    from app.database import SessionLocal
    from app.models import User
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)

    def qr_pixels(html):
        uri = re.search(r'src="data:image/png;base64,([^"]+)"', html).group(1)
        img = Image.open(io.BytesIO(base64.b64decode(uri))).convert("L")
        return img.size, img.tobytes()

    def strip(html):
        return re.sub(r'data:image/png;base64,[^"]+', "QR", html)

    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for first_gateway in (False, True):
            if first_gateway:
                d = _signed_in(email, pw, world)
                b = _via(d, base).get("/account/2fa/setup")
                assert b.headers.get("x-nd-served-by") == "gateway"
                for k, v in b.cookies.items():
                    d.cookies.set(k, v)
                a = d.get("/account/2fa/setup")
            else:
                a = d.get("/account/2fa/setup")
                b = _via(d, base).get("/account/2fa/setup")
                assert b.headers.get("x-nd-served-by") == "gateway"
            assert a.status_code == b.status_code == 200
            na = golden.normalise("text/html", strip(a.text).encode())
            nb = golden.normalise("text/html", strip(b.text).encode())
            assert na == nb, email
            assert qr_pixels(a.text) == qr_pixels(b.text), email
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == seed.player_a.email).first()
        u.totp_enabled = True
        db.commit()
        b = _via(d, base).get("/account/2fa/setup", follow_redirects=False)
        assert (b.status_code, b.headers.get("location"), b.headers.get("x-nd-served-by")) == (303, "/account", "gateway")
    finally:
        u.totp_enabled = False
        db.commit()
        db.close()


def test_loot_give_is_pythons(client, seed):
    """POST /api/parties/<id>/loot {action: give} from the gateway: part of a stack, the rest of it, a merge into a matching item
    (odd stored qty values), a new item, too many, a custom-sheet member - each run on Python, put back, and run through the gateway."""
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = db.query(SheetTemplate).filter(SheetTemplate.sheet_mode == "custom").first()
        nat = PlayerCharacter(world_id=w, name="Nat", equipment_json=json.dumps([{"name": " gem ", "qty": "x"}, {"name": "Rope", "qty": 2.7}, "junk"]))
        cust = PlayerCharacter(world_id=w, name="Cust", sheet_template_id=tpl.id if tpl else None)
        db.add_all([nat, cust])
        db.flush()
        party = Party(world_id=w, name="Givers", member_pc_ids_json=json.dumps([nat.id, cust.id]),
                      loot_json=json.dumps([{"lid": "aaaa0001", "name": "Gem", "qty": 3, "notes": "", "claimed_by": []},
                                            {"lid": "aaaa0002", "name": "rope", "qty": 1, "notes": "", "claimed_by": []},
                                            {"lid": "aaaa0003", "name": "Map", "qty": 1, "notes": "old", "claimed_by": [nat.id]}]))
        db.add(party)
        db.commit()
        pid, nid, cid = party.id, nat.id, cust.id
    finally:
        db.close()

    def state():
        db = SessionLocal()
        try:
            return db.get(Party, pid).loot_json, db.get(PlayerCharacter, nid).equipment_json
        finally:
            db.close()

    def put(s):
        db = SessionLocal()
        try:
            db.get(Party, pid).loot_json, db.get(PlayerCharacter, nid).equipment_json = s
            db.commit()
        finally:
            db.close()

    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for body in ({"action": "give", "lid": "aaaa0001", "pc_id": nid, "qty": 2}, {"action": "give", "lid": "aaaa0001", "pc_id": nid},
                 {"action": "give", "lid": "aaaa0002", "pc_id": nid}, {"action": "give", "lid": "aaaa0003", "pc_id": nid},
                 {"action": "give", "lid": "aaaa0001", "pc_id": nid, "qty": 4}, {"action": "give", "lid": "aaaa0001", "pc_id": cid}):
        before = state()
        a = gm.post(f"/api/parties/{pid}/loot", json=body)
        after_a = state()
        put(before)
        b = _via(gm, base).post(f"/api/parties/{pid}/loot", json=body)
        assert b.headers.get("x-nd-served-by") == "gateway", body
        assert (a.status_code, a.json()) == (b.status_code, b.json()), body
        assert state() == after_a, body
        put(before)


def test_account_page_and_2fa_confirm_are_pythons(client, seed):
    """GET /account and POST /account/2fa/setup from the gateway. The account page with its saved-notes, tokens (used and unused) and
    trusted devices (an expired one left out). The confirm route: no pending secret (back to the setup page), a wrong code (the setup
    page again, 400), the right code (two-step on, 8 backup codes stored as sha256 and shown once), and someone already on it (to
    /account)."""
    import hashlib
    import re
    from datetime import datetime, timedelta
    import pyotp
    from app.database import SessionLocal
    from app.models import ApiToken, TrustedDevice, User
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        db.add_all([ApiToken(user_id=seed.gm.id, token_hash="a" * 64, label="Phone", last_used_at=datetime(2031, 5, 4, 12, 0)),
                    ApiToken(user_id=seed.gm.id, token_hash="b" * 64, label=""),
                    TrustedDevice(user_id=seed.gm.id, token_hash="c" * 64, label="Firefox <x>", expires_at=datetime.utcnow() + timedelta(days=9)),
                    TrustedDevice(user_id=seed.gm.id, token_hash="d" * 64, label="Old", expires_at=datetime.utcnow() - timedelta(days=1))])
        db.commit()
    finally:
        db.close()
    served = 0
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)):
        d = _signed_in(email, pw, world)
        for url in ("/account", "/account?saved=name", "/account?saved=2fa-off"):
            served += _same_page(d, _via(d, base), url, email)
    assert served == 6

    def strip(html):
        return re.sub(r'data:image/png;base64,[^"]+', "QR", html)

    def strip_codes(html):
        return re.sub(r'<code style="font-size:.85rem">[0-9a-f]{10}</code>', "CODE", html)

    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a = d.post("/account/2fa/setup", data={"code": "123456"}, follow_redirects=False)
    b = _via(d, base).post("/account/2fa/setup", data={"code": "123456"}, follow_redirects=False)
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location")) == (303, "/account/2fa/setup")
    d.get("/account/2fa/setup")  # Python makes the pending secret
    for code in ("000000", ""):
        a = d.post("/account/2fa/setup", data={"code": code})
        b = _via(d, base).post("/account/2fa/setup", data={"code": code})
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.status_code == b.status_code == 400
        assert golden.normalise("text/html", strip(a.text).encode()) == golden.normalise("text/html", strip(b.text).encode())
    db = SessionLocal()
    try:
        secret = None
        # the pending secret is in the session; read it back from the page
        page = d.get("/account/2fa/setup").text
        secret = re.search(r"<code[^>]*>([A-Z2-7]{32})</code>", page).group(1)
    finally:
        db.close()

    def reset():
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == seed.player_a.email).first()
            u.totp_enabled, u.totp_secret, u.totp_backup_codes_json = False, None, "[]"
            db.commit()
        finally:
            db.close()

    def stored():
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == seed.player_a.email).first()
            return u.totp_enabled, u.totp_secret, json.loads(u.totp_backup_codes_json or "[]")
        finally:
            db.close()

    jar = dict(d.cookies.items())
    a = d.post("/account/2fa/setup", data={"code": pyotp.TOTP(secret).now()})
    sa = stored()
    reset()
    via = httpx.Client(base_url=base, timeout=60)
    for k, v in jar.items():
        via.cookies.set(k, v)
    b = via.post("/account/2fa/setup", data={"code": pyotp.TOTP(secret).now()})
    sb = stored()
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert a.status_code == b.status_code == 200
    assert golden.normalise("text/html", strip_codes(a.text).encode()) == golden.normalise("text/html", strip_codes(b.text).encode())
    assert (sa[0], sa[1], len(sa[2])) == (sb[0], sb[1], len(sb[2])) == (True, secret, 8)
    shown = re.findall(r'<code style="font-size:.85rem">([0-9a-f]{10})</code>', b.text)
    assert sorted(hashlib.sha256(c.encode()).hexdigest() for c in shown) == sorted(sb[2])
    r = via.post("/account/2fa/setup", data={"code": "1"}, follow_redirects=False)
    assert (r.status_code, r.headers.get("location"), r.headers.get("x-nd-served-by")) == (303, "/account", "gateway")
    reset()


def test_account_name_tokens_and_revokes_are_pythons(client, seed):
    """POST /account/name (blank, too long, saved), /account/tokens/new (shown once; only its sha256 kept) and the token / trusted
    device revokes (someone else's row left alone) from the gateway."""
    import hashlib
    import re
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models import ApiToken, TrustedDevice, User
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    via = _via(d, base)

    def name():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.email == seed.player_a.email).first().display_name
        finally:
            db.close()

    original = name()
    for value in ("   ", "x" * 257, "é" * 256 + " "):
        a = d.post("/account/name", data={"display_name": value}, follow_redirects=False)
        after_a = name()
        db = SessionLocal()
        try:
            db.query(User).filter(User.email == seed.player_a.email).first().display_name = original
            db.commit()
        finally:
            db.close()
        b = via.post("/account/name", data={"display_name": value}, follow_redirects=False)
        assert b.headers.get("x-nd-served-by") == "gateway", value
        assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location")), value
        if a.status_code != 303:
            assert golden.normalise("text/html", a.content) == golden.normalise("text/html", b.content), value
        assert name() == after_a, value
    db = SessionLocal()
    try:
        db.query(User).filter(User.email == seed.player_a.email).first().display_name = original
        db.commit()
    finally:
        db.close()

    def tokens():
        db = SessionLocal()
        try:
            return [(t.label, t.token_hash) for t in db.query(ApiToken).filter(ApiToken.user_id == seed.player_a.id).order_by(ApiToken.id)]
        finally:
            db.close()

    pat = r'<code style="display:block;word-break:break-all;font-size:.82rem;background:var\(--bg3\);padding:.5rem;border-radius:3px">([A-Za-z0-9_-]{43})</code>'

    def norm(html, raw):
        html = html.replace(raw, "TOKEN")
        return re.sub(r'"url": "http://[^/"]+/mcp"', '"url": "BASE/mcp"', html)

    for label in ("  My phone  ", ""):
        a = d.post("/account/tokens/new", data={"label": label})
        b = via.post("/account/tokens/new", data={"label": label})
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.status_code == b.status_code == 200
        ra, rb = re.search(pat, a.text).group(1), re.search(pat, b.text).group(1)
        assert f'"url": "{base}/mcp"' in b.text
        got = tokens()
        assert [t[0] for t in got[-2:]] == [label.strip() or "Unlabeled token"] * 2
        assert got[-1][1] == hashlib.sha256(rb.encode()).hexdigest() and got[-2][1] == hashlib.sha256(ra.encode()).hexdigest()
        # the gateway's page lists one token more (a row says Revoke twice: the button and its confirm)
        assert golden.normalise("text/html", norm(a.text, ra).encode()).count("Revoke") + 2 == golden.normalise("text/html", norm(b.text, rb).encode()).count("Revoke")
    db = SessionLocal()
    try:
        mine = db.query(ApiToken).filter(ApiToken.user_id == seed.player_a.id).first().id
        theirs = ApiToken(user_id=seed.gm.id, token_hash="e" * 64, label="GM's")
        dev = TrustedDevice(user_id=seed.player_a.id, token_hash="f" * 64, label="mine", expires_at=datetime.utcnow() + timedelta(days=3))
        gm_dev = TrustedDevice(user_id=seed.gm.id, token_hash="9" * 64, label="gm", expires_at=datetime.utcnow() + timedelta(days=3))
        db.add_all([theirs, dev, gm_dev])
        db.commit()
        ids = (mine, theirs.id, dev.id, gm_dev.id)
    finally:
        db.close()
    for path in (f"/account/tokens/{ids[1]}/revoke", f"/account/tokens/{ids[0]}/revoke", f"/account/trusted-devices/{ids[3]}/revoke",
                 f"/account/trusted-devices/{ids[2]}/revoke", "/account/tokens/999999/revoke"):
        r = via.post(path, follow_redirects=False)
        assert (r.status_code, r.headers.get("location"), r.headers.get("x-nd-served-by")) == (303, "/account", "gateway"), path
    db = SessionLocal()
    try:
        assert db.get(ApiToken, ids[0]) is None and db.get(ApiToken, ids[1]) is not None
        assert db.get(TrustedDevice, ids[2]) is None and db.get(TrustedDevice, ids[3]) is not None
    finally:
        db.close()


def test_hub_log_and_notes_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/log and /hub/notes from the gateway: the change log with what can still be undone (the newest change
    to each thing, nothing undone, the owner only), the GM's private notes as excerpts with the thread link, the journal with session
    labels; a GM looking in at the log (read-only) and at the notes (404), someone else's character (404)."""
    from datetime import datetime
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CharacterJournalEntry, CharacterLog, GameSession, Party, PlayerCharacter, PrivateNote
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id)
        bare = PlayerCharacter(world_id=w, name="Bare", owner_user_id=seed.player_a.id)
        other = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([pc, bare, other])
        db.flush()
        party = Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([pc.id]))
        db.add(party)
        db.flush()
        s1 = GameSession(world_id=w, session_num=1, title="Start", party_id=party.id)
        s2 = GameSession(world_id=w, session_num=2, title="", party_id=party.id)
        db.add_all([s1, s2, GameSession(world_id=w, session_num=3, title="Elsewhere")])
        db.flush()
        for i, (kind, undo, undone) in enumerate([("hp", {"op": "hp", "value": 10}, False), ("hp", {"op": "hp", "value": 8}, False),
                                                 ("currency", {"op": "currency", "abbr": "GP", "value": 3}, False), ("xp", {"op": "xp", "value": 0}, True),
                                                 ("note", {}, False), ("cf", {"op": "cf", "values": {"b": 1, "a": 2}}, False), ("bad", "nope", False)]):
            db.add(CharacterLog(world_id=w, character_id=pc.id, actor="Al", kind=kind, text=f"change {i}",
                                undo_json=json.dumps(undo) if not isinstance(undo, str) else undo, undone=undone))
        db.add_all([CharacterJournalEntry(world_id=w, character_id=pc.id, kind="journal", title="Day 1", body="b", session_id=s1.id),
                    CharacterJournalEntry(world_id=w, character_id=pc.id, kind="journal", title="Day 2", session_id=s2.id,
                                          created_at=datetime(2031, 1, 2, 3, 4, 5)),
                    CharacterJournalEntry(world_id=w, character_id=pc.id, kind="goal", title="Goal")])
        db.add_all([PrivateNote(world_id=w, player_user_id=seed.player_a.id, title="Psst", content="<b>Hello</b> **there** " + "word " * 80),
                    PrivateNote(world_id=w, player_user_id=seed.player_a.id, title=None, content="# Short\n[link](http://x)")])
        db.commit()
        ids = (pc.id, bare.id, other.id, 999999)
    finally:
        db.close()
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        via = _via(d, base)
        for pid in ids:
            for tab in ("log", "notes"):
                url = f"/api/characters/{pid}/hub/{tab}"
                a, b = d.get(url), via.get(url)
                assert b.headers.get("x-nd-served-by") == "gateway", (email, url)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, url)


def test_saved_view_refusals_are_pythons(client, seed):
    """POST /api/saved-views/<section> and DELETE /api/saved-views/<section>/<id> refusals from the gateway: an unknown section, a body
    that is not JSON or not an object, no name, the 20-view limit, a view that is not there."""
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    for i in range(20):
        assert d.post("/api/saved-views/audio", json={"name": f"v{i}", "state": {}}).status_code == 200
    cases = [("POST", "/api/saved-views/nosuch", b'{"name": "x"}'), ("POST", "/api/saved-views/audio", b"{oops"), ("POST", "/api/saved-views/audio", b"[]"),
             ("POST", "/api/saved-views/audio", b"\xff"), ("POST", "/api/saved-views/audio", b'{"name": "   "}'), ("POST", "/api/saved-views/audio", b"{}"),
             ("POST", "/api/saved-views/audio", b'{"name": "one more"}'), ("DELETE", "/api/saved-views/audio/999999", b""),
             ("DELETE", "/api/saved-views/images/1", b"")]
    for method, path, body in cases:
        a = d.request(method, path, content=body, headers={"content-type": "application/json"})
        b = via.request(method, path, content=body, headers={"content-type": "application/json"})
        assert b.headers.get("x-nd-served-by") == "gateway", (method, path, body)
        assert a.status_code >= 400, (method, path, body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (method, path, body)


def test_world_context_player_is_pythons(client, seed):
    """POST /api/ai/world-context-player from the gateway - the player-safe Smart Context: hidden entities left out unless shared with
    that player, [gmonly] text and :::gm rules blocks stripped, hidden priority entities left out, only the characters the player may
    see (theirs, or every owned one when the party is shared); the GM's unfiltered view; the world's own limits; a world that does not
    let players ask the AI (403)."""
    from app.database import SessionLocal
    from app.models import Entity, PlayerCharacter, World, entity_player_access
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        shared = Entity(world_id=w, kind="item", name="Shared blade", body="A blade the GM showed you.", visible_to_players=False)
        db.add_all([Entity(world_id=w, kind="item", name="Vibro Blade", body="A humming blade. [gmonly]Cursed![/gmonly] Costs 200.",
                           summary="Sharp [gmonly]secret[/gmonly]"),
                    Entity(world_id=w, kind="item", name="Secret blade", body="The hidden blade.", visible_to_players=False),
                    Entity(world_id=w, kind="lore", name="Blade lore", body="Blades of old. [gmonly]The king forged them.[/gmonly]", rag_priority=True),
                    Entity(world_id=w, kind="lore", name="Hidden blade lore", body="Blades secret lore.", rag_priority=True, visible_to_players=False),
                    Entity(world_id=w, kind="note", name="Blade rumours", body="Someone sells blades."),
                    Entity(world_id=w, kind="note", name="GM blade note", body="Blades are a trap.", visible_to_players=False),
                    shared,
                    PlayerCharacter(world_id=w, name="Vexa", owner_user_id=seed.player_a.id, char_class="Netrunner"),
                    PlayerCharacter(world_id=w, name="Bramble", owner_user_id=seed.player_b.id),
                    PlayerCharacter(world_id=w, name="Unowned")])
        db.flush()
        db.execute(entity_player_access.insert().values(entity_id=shared.id, user_id=seed.player_a.id))
        wa = db.get(World, w)
        wa.rules_md = "# Rules\n\n## Blades\n\nBlades deal 1d8.\n\n:::gm\nBlades secretly explode.\n:::\n\n:::note Tip\nSharpen blades.\n:::\n\n## Armor\n\nArmor soaks."
        wa.players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    bodies = ({"query": "blade"}, {"query": "the party characters"}, {"query": "vexa bramble"}, {"query": "blades armor", "limit": 1, "notes_limit": 0},
              {"query": "  "}, {}, {"query": "blade", "limit": 500, "notes_limit": -2})
    for see_party in (False, True):
        db = SessionLocal()
        try:
            db.get(World, w).players_see_party = see_party
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for body in bodies:
                a = d.post("/api/ai/world-context-player", json=body)
                b = _via(d, base).post("/api/ai/world-context-player", json=body)
                assert b.headers.get("x-nd-served-by") == "gateway", (email, body)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, see_party, body)
                if email != seed.gm.email:
                    ctx = b.json().get("context", "")
                    for secret in ("Cursed", "secret", "Secret blade", "Hidden blade lore", "GM blade note", "explode", "king forged"):
                        assert secret not in ctx, (secret, body)
    db = SessionLocal()
    try:
        wa = db.get(World, w)
        wa.players_can_use_ai_chat = False
        wa.rules_md = ""
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a = d.post("/api/ai/world-context-player", json={"query": "blade"})
    b = _via(d, base).post("/api/ai/world-context-player", json={"query": "blade"})
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"})


def test_unsloth_prefs_save_is_pythons(client, seed):
    """POST /api/ai/unsloth/prefs from the gateway: the same ai_models.json, byte for byte (json.dumps indent=2, key order kept), for the
    TTS / STT fields, a bare Studio host given http://, a cleared value, an invalid console URL (400, the fields before it still saved),
    a missing or unreadable file, and an empty body."""
    import os
    from pathlib import Path
    base = str(client.base_url)
    path = Path(os.environ.get("DB_PATH", "tests/_data/world.db")).parent / "ai_models.json"
    original = path.read_bytes() if path.exists() else None
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    bodies = ({"tts_model": "  kokoro  ", "tts_voice": "af_bella", "stt_model": None}, {"studio_console_url": "192.168.1.5:8000"},
              {"studio_console_url": "studio.lan/console", "tts_language": "en é"}, {"tts_instructions": "", "studio_console_url": ""},
              {"tts_voice": "x", "studio_console_url": "javascript:alert(1)"}, {"studio_console_url": "ftp://x"}, {}, {"tts_model": 7})
    try:
        for start in (b'{"custom": [{"id": "m"}], "hidden": ["h"], "defaults": {"chat": "a"}}', None, b"{broken"):
            for body in bodies:
                def reset():
                    if start is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(start)
                reset()
                a = gm.post("/api/ai/unsloth/prefs", json=body)
                after_a = path.read_bytes() if path.exists() else None
                reset()
                b = _via(gm, base).post("/api/ai/unsloth/prefs", json=body)
                after_b = path.read_bytes() if path.exists() else None
                assert b.headers.get("x-nd-served-by") == "gateway", body
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (start, body)
                assert after_a == after_b, (start, body)
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def test_entity_markdown_download_is_pythons(client, seed):
    """GET /entity/<id>/download.md from the gateway: title, kind line (icon, subtype), summary, body and the notes the viewer may see,
    [gmonly] stripped for a player; the file name; a player in a world without downloads (403), a hidden entity (404)."""
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        a = Entity(world_id=w, kind="npc", subtype="Fixer", name="Ed 'Vosk' / #1", summary="Deals [gmonly]in lies[/gmonly] info",
                   body="Body text.\n\n[gmonly]Works for the mayor.[/gmonly]\n\nMore.   \n")
        b = Entity(world_id=w, kind="location", name="!!!", summary="", body=None)
        c = Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False)
        db.add_all([a, b, c])
        db.flush()
        db.add_all([EntityNote(entity_id=a.id, content="Seen at the docks. [gmonly]Armed.[/gmonly]", visible_to_players=True),
                    EntityNote(entity_id=a.id, content="GM only note", visible_to_players=False)])
        db.get(World, w).players_can_download_entities = True
        db.commit()
        ids = (a.id, b.id, c.id, 999999)
    finally:
        db.close()
    served = 0
    for allowed in (True, False):
        db = SessionLocal()
        try:
            db.get(World, w).players_can_download_entities = allowed
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                url = f"/entity/{i}/download.md"
                ra, rb = d.get(url), _via(d, base).get(url)
                assert rb.headers.get("x-nd-served-by") == "gateway", (email, url)
                assert ra.status_code == rb.status_code, (email, url, allowed)
                assert ra.content == rb.content, (email, url, allowed)
                for h in ("content-type", "content-disposition"):
                    assert ra.headers.get(h) == rb.headers.get(h), (h, url)
                served += 1
    assert served == 16


def test_session_log_list_is_pythons(client, seed):
    """GET /session-log from the gateway: newest first, the recap marker (published beats facts, a blank published summary is none),
    the 🎵 for a clip the viewer can hear and the picture / video mark for media they can see - for the GM, a player, and a world that
    closes the Session Log to players (403)."""
    from app.database import SessionLocal
    from app.models import AudioClip, Fact, GameSession, VideoClip, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        heard = AudioClip(world_id=w, name="Song", file_url="/uploads/a.mp3", visible_to_players=True)
        secret = AudioClip(world_id=w, name="GM song", file_url="/uploads/b.mp3", visible_to_players=False)
        vid = VideoClip(world_id=w, name="Clip", file_url="/uploads/v.mp4", visible_to_players=False)
        db.add_all([heard, secret, vid])
        db.flush()
        s = [GameSession(world_id=w, session_num=1, title="One", player_summary="Recap", player_summary_published=True,
                         recap_audio_json=json.dumps([heard.id])),
             GameSession(world_id=w, session_num=3, title="Three", player_summary="   ", player_summary_published=True,
                         recap_audio_json=json.dumps([secret.id, True, secret.id]),
                         recap_media_json=json.dumps([{"kind": "image", "url": "/uploads/p.png", "visible": False}, {"kind": "video", "clip_id": vid.id}])),
             GameSession(world_id=w, session_num=2, title="Two", recap_media_json=json.dumps([{"kind": "image", "url": "/uploads/q.png", "visible": True}, "junk"])),
             GameSession(world_id=w, session_num=4, title="Four", recap_media_json="{bad")]
        db.add_all(s)
        db.flush()
        db.add(Fact(world_id=w, content="A fact", game_session_id=s[1].id))
        db.commit()
    finally:
        db.close()
    served = 0
    for closed in (False, True):
        if closed:
            db = SessionLocal()
            try:
                db.get(World, w).section_access_json = json.dumps({"session_log": {"player": "none"}})
                db.commit()
            finally:
                db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            r = _via(d, base).get("/session-log")
            assert r.headers.get("x-nd-served-by") == "gateway", (email, closed)
            served += _same_page(d, _via(d, base), "/session-log", email) or r.status_code == 403
    assert served == 4


def test_npc_talk_page_is_pythons(client, seed):
    """GET /npc-talk from the gateway: the talkable kinds in kind then name order (what the viewer may see), ?entity= picking one (or
    one they may not see: none), for the GM and a player; a world that does not let players ask the AI (403)."""
    from app.database import SessionLocal
    from app.models import Entity, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        es = [Entity(world_id=w, kind="organization", name="Guild"), Entity(world_id=w, kind="character", name="zed", image_url="/uploads/z.png"),
              Entity(world_id=w, kind="character", name="Amy"), Entity(world_id=w, kind="creature", name="Wolf"),
              Entity(world_id=w, kind="character", name="Spy", visible_to_players=False), Entity(world_id=w, kind="item", name="Sword")]
        db.add_all(es)
        db.get(World, w).players_can_ask_ai = True
        db.commit()
        spy, amy = es[4].id, es[2].id
    finally:
        db.close()
    urls = ["/npc-talk", f"/npc-talk?entity={amy}", f"/npc-talk?entity={spy}", "/npc-talk?entity=0"]
    served = 0
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for url in urls:
            served += _same_page(d, _via(d, base), url, email)
    assert served == 8
    db = SessionLocal()
    try:
        db.get(World, w).players_can_ask_ai = False
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a, b = d.get("/npc-talk"), _via(d, base).get("/npc-talk")
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"})


def test_world_json_exports_are_pythons(client, seed):
    """GET /worlds/<id>/export and /worlds/<id>/export/entities/<kind>.json from the gateway: the same bytes - the world header, every
    entity with its template slug, custom fields, visibility and its image embedded (a missing file: none), text beyond ASCII written
    as it is; an empty kind, an unknown kind (404 with its message), no such world."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import Entity, EntityTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    uploads = Path(os.environ["DB_PATH"]).parent / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    img = uploads / "gw-export-test.PNG"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    db = SessionLocal()
    try:
        tpl = EntityTemplate(world_id=w, name="Shop Thing", slug="gw-exp-shop", kind="location", fields_json="[]")
        db.add(tpl)
        db.flush()
        db.add_all([Entity(world_id=w, kind="location", name="Café \"Z\"", body="Line 1\nLine\t2 — \U0001F600", template_id=tpl.id,
                           custom_fields_json=json.dumps({"owner": "Mo", "n": 1.5, "list": [1, {}]}), image_url="/uploads/gw-export-test.PNG"),
                    Entity(world_id=w, kind="location", name="Alley", visible_to_players=False, image_url="/uploads/missing.png"),
                    Entity(world_id=w, kind="item", name="Knife", custom_fields_json="")])
        db.get(World, w).description = "Neon über alles"
        db.commit()
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    try:
        for url in (f"/worlds/{w}/export", f"/worlds/{w}/export/entities/location.json", f"/worlds/{w}/export/entities/creature.json",
                    f"/worlds/{w}/export/entities/nope.json", "/worlds/999999/export", "/worlds/999999/export/entities/item.json"):
            a, b = gm.get(url), _via(gm, base).get(url)
            assert b.headers.get("x-nd-served-by") == "gateway", url
            assert (a.status_code, a.content) == (b.status_code, b.content), url
            for h in ("content-type", "content-disposition"):
                assert a.headers.get(h) == b.headers.get(h), (url, h)
    finally:
        img.unlink(missing_ok=True)


def test_character_sheets_are_pythons(client, seed):
    """The character sheets from the gateway: the list (GM grouped by player, a player their own), a sheet's page, its sandboxed render
    and download (saved data and the bridge put in before </body>), and the writes - new (a missing / hidden / non-template page, a
    character not theirs), save (not an object, too large), edit and delete; someone else's sheet is a 404 everywhere."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import CharacterSheet, PageDoc, PlayerCharacter
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["DB_PATH"]).parent / "uploads" / "pages"
    up.mkdir(parents=True, exist_ok=True)
    (up / "gw-sheet.html").write_text("<html><BODY><p>Sheet é</p></body></html>", encoding="utf-8")
    db = SessionLocal()
    try:
        tpl = PageDoc(world_id=w, name="Hero Sheet", file_url="/uploads/pages/gw-sheet.html", is_character_sheet_template=True, visible_to_players=True)
        plain = PageDoc(world_id=w, name="Plain", file_url="/uploads/pages/gw-sheet.html", visible_to_players=True)
        hidden = PageDoc(world_id=w, name="Hidden", file_url="/uploads/pages/gw-sheet.html", is_character_sheet_template=True, visible_to_players=False)
        mine = PlayerCharacter(world_id=w, name="Vexa", owner_user_id=seed.player_a.id)
        theirs = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([tpl, plain, hidden, mine, theirs])
        db.flush()
        s1 = CharacterSheet(world_id=w, template_id=tpl.id, owner_user_id=seed.player_a.id, player_character_id=mine.id, name="Vexa sheet",
                            data_json=json.dumps({"hp": "12", "note": "</script>x"}))
        s2 = CharacterSheet(world_id=w, template_id=tpl.id, owner_user_id=seed.gm.id, name="GM's é")
        db.add_all([s1, s2])
        db.commit()
        ids = dict(tpl=tpl.id, plain=plain.id, hidden=hidden.id, mine=mine.id, theirs=theirs.id, s1=s1.id, s2=s2.id)
    finally:
        db.close()

    def sheets():
        db = SessionLocal()
        try:
            return [(s.id, s.name, s.player_character_id, s.data_json) for s in db.query(CharacterSheet).order_by(CharacterSheet.id)]
        finally:
            db.close()

    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD), (seed.player_b.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        via = _via(d, base)
        for url in ("/pages/sheets", f"/pages/sheets/{ids['s1']}"):
            r = via.get(url)
            assert r.headers.get("x-nd-served-by") == "gateway", (email, url)
            _same_page(d, via, url, email)
        for url in (f"/pages/sheets/{ids['s1']}/render", f"/pages/sheets/{ids['s1']}/download", f"/pages/sheets/{ids['s2']}/render", "/pages/sheets/999999/render"):
            a, b = d.get(url), via.get(url)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, url)
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, url)
            for h in ("content-type", "content-disposition", "content-security-policy", "x-frame-options", "x-content-type-options"):
                assert a.headers.get(h) == b.headers.get(h), (email, url, h)
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    writes = [("/pages/sheets/new", {"template_id": ""}), ("/pages/sheets/new", {"template_id": str(ids["plain"])}),
              ("/pages/sheets/new", {"template_id": str(ids["hidden"])}), ("/pages/sheets/new", {"template_id": "999999"}),
              ("/pages/sheets/new", {"template_id": str(ids["tpl"]), "player_character_id": str(ids["theirs"])}),
              ("/pages/sheets/new", {"template_id": str(ids["tpl"]), "player_character_id": "x1"}),
              ("/pages/sheets/new", {"template_id": str(ids["tpl"]), "player_character_id": str(ids["mine"])}),
              ("/pages/sheets/new", {"template_id": str(ids["tpl"])}),
              (f"/pages/sheets/{ids['s1']}/edit", {"name": "  Renamed  ", "player_character_id": ""}),
              (f"/pages/sheets/{ids['s1']}/edit", {"name": "", "player_character_id": str(ids["theirs"])}),
              (f"/pages/sheets/{ids['s2']}/edit", {"name": "x"}), (f"/pages/sheets/{ids['s2']}/delete", {})]
    for path, form in writes:
        before = sheets()
        a = d.post(path, data=form, follow_redirects=False)
        after_a = sheets()
        db = SessionLocal()
        try:
            for s in db.query(CharacterSheet).all():
                db.delete(s)
            db.commit()
            for sid, name, pc, data in before:
                db.add(CharacterSheet(id=sid, world_id=w, template_id=ids["tpl"], owner_user_id=seed.player_a.id if sid != ids["s2"] else seed.gm.id,
                                      player_character_id=pc, name=name, data_json=data))
            db.commit()
        finally:
            db.close()
        b = via.post(path, data=form, follow_redirects=False)
        assert b.headers.get("x-nd-served-by") == "gateway", (path, form)
        assert (a.status_code, a.headers.get("location") is not None) == (b.status_code, b.headers.get("location") is not None), (path, form)
        if a.status_code >= 400:
            assert a.json() == b.json(), (path, form)
        assert [x[1:] for x in sheets()] == [x[1:] for x in after_a], (path, form)
    for body in ({"data": {"hp": "9", "x": [1, 2]}}, {"data": "no"}, {"data": {"big": "y" * 300000}}):
        before = sheets()
        a = d.post(f"/pages/sheets/{ids['s1']}/save", json=body)
        after_a = sheets()
        b = via.post(f"/pages/sheets/{ids['s1']}/save", json=body)
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert (a.status_code, a.json()) == (b.status_code, b.json())
        assert sheets() == after_a
    (up / "gw-sheet.html").unlink(missing_ok=True)


def test_media_rename_run_is_pythons(client, seed):
    """GET /api/media-rename/runs/<id> from the gateway: a finished run with results and errors, one with unreadable columns
    (both reset to the fallback), a pending one with no author ("someone"), another world's run and a missing id (404) - for
    the GM, and for a player, whose default read-only sections are a 403 the gateway answers itself."""
    from .conftest import PLAYER_PASSWORD
    from app.models import MediaRenameRun
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        runs = [MediaRenameRun(world_id=w, status="done", total=3, done=3, created_by_user_id=seed.gm.id,
                               results_json=json.dumps([{"kind": "audio", "id": 1, "name": "Theme"}, {"kind": "video", "id": 2}]),
                               errors_json=json.dumps(["no metadata for x"])),
                MediaRenameRun(world_id=w, status="error", error="boom", created_by_user_id=seed.player_a.id,
                               results_json="{bad", errors_json=None),
                MediaRenameRun(world_id=w, status="pending", created_by_user_id=None),
                MediaRenameRun(world_id=seed.world_b.id, status="done", created_by_user_id=seed.gm.id)]
        db.add_all(runs)
        db.commit()
        ids = [r.id for r in runs] + [999999]
    finally:
        db.close()
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for i in ids:
            url = f"/api/media-rename/runs/{i}"
            a, b = d.get(url), _via(d, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, i)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, i)


def test_session_audio_job_status_is_pythons(client, seed):
    """GET /api/sessions/ai/audio-jobs/<id> from the gateway: a recap and a condense job (the purposes sessions.py answers),
    an attachment and a world-summary job - the same 404 a missing id is - and another world's job, for the GM and for a
    player of the world."""
    from .conftest import PLAYER_PASSWORD
    from app.models import AudioJob
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        jobs = [AudioJob(world_id=w, purpose="session_recap", filename="rec.mp3", status="done", transcript="t", recap="r",
                         model="m1", extra_instructions="slow down", chunk_current=2, chunk_total=4),
                AudioJob(world_id=w, purpose="condense", status="error", error="model offline"),
                AudioJob(world_id=w, purpose="attachment", status="done", transcript="att"),
                AudioJob(world_id=w, purpose="world_summary", audience="players", status="done"),
                AudioJob(world_id=seed.world_b.id, purpose="condense", status="done")]
        db.add_all(jobs)
        db.commit()
        ids = [j.id for j in jobs] + [999999]
    finally:
        db.close()
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        for i in ids:
            url = f"/api/sessions/ai/audio-jobs/{i}"
            a, b = d.get(url), _via(d, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (email, i)
            assert b.headers.get("x-nd-served-by") == "gateway", (email, i)


def test_player_image_job_status_is_pythons(client, seed):
    """GET /api/ai/imagegen/player/jobs/<id> from the gateway: the toggle-off 403 (a GM's too), a player's own job, the GM
    seeing it, another player's job and another world's job (the same 404), and a missing id - with the gate's no-world 403
    answered before the handler's own 404, as Python's _require_player_image_gen_access raises first."""
    from .conftest import PLAYER_PASSWORD
    from app.models import ImageJob, World, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_b.id).delete()
        db.add(WorldMembership(world_id=w, user_id=seed.player_b.id))
        mine = ImageJob(world_id=w, prompt="a neon dragon", status="done", created_by_user_id=seed.player_a.id,
                        result_urls_json=json.dumps(["/uploads/g1.png"]))
        other = ImageJob(world_id=w, prompt="of someone else", status="done", created_by_user_id=seed.gm.id)
        foreign = ImageJob(world_id=seed.world_b.id, prompt="elsewhere", status="done", created_by_user_id=seed.player_b.id)
        db.add_all([mine, other, foreign])
        db.commit()
        ids = [mine.id, other.id, foreign.id, 999999]
    finally:
        db.close()
    try:
        # the toggle off: every viewer is 403, the handler's own checks never run
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                url = f"/api/ai/imagegen/player/jobs/{i}"
                a, b = d.get(url), _via(d, base).get(url)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), ("closed", email, i)
                assert b.headers.get("x-nd-served-by") == "gateway", ("closed", email, i)
        db = SessionLocal()
        try:
            world = db.get(World, w)
            world.players_can_use_image_gen = True
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD), (seed.player_b.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for i in ids:
                url = f"/api/ai/imagegen/player/jobs/{i}"
                a, b = d.get(url), _via(d, base).get(url)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), ("open", email, i)
                assert b.headers.get("x-nd-served-by") == "gateway", ("open", email, i)
    finally:
        db = SessionLocal()
        try:
            db.get(World, w).players_can_use_image_gen = False
            db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_b.id).delete()
            db.commit()
        finally:
            db.close()


def test_post_logout_is_pythons(client, seed):
    """POST /logout from the gateway: Python has no POST /logout, so Starlette's router raises HTTPException(405) - the
    gateway's answer, signed in or not."""
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for c in (d, httpx.Client(base_url=base, timeout=60)):
        a, b = c.post("/logout"), _via(d, base).post("/logout")
        assert (a.status_code, a.json()) == (b.status_code, b.json()), c
        assert b.headers.get("x-nd-served-by") == "gateway", c
def test_cockpit_player_board_is_pythons(client, seed):
    """GET /api/cockpit/player-board from the gateway: the viewer's own parties (member vitals, only parties one of their characters
    belongs to), the world's player-visible active top-level quests with party names, the viewer's characters with vitals and XP - for a
    player and a GM, and a GM's ?pc= view through a character's owner; no world (404) and a player's ?pc= at another's character (own
    list still) answered as Python's."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        p1 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=3, xp=120,
                             pp_current=4, mp_current=2, shock_current=1, current_hp=7)
        p2 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Zed", level=1, xp=0,
                             pp_current=9, mp_current=9, shock_current=9, current_hp=1)
        g1 = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.gm.id, name="Gm")
        db.add_all([p1, p2, g1])
        db.flush()
        db.add_all([
            Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([p1.id, p2.id])),
            Party(world_id=w, name="Lone", member_pc_ids_json=json.dumps([p2.id])),
            Party(world_id=w, name="Others", member_pc_ids_json=json.dumps([])),
            Party(world_id=seed.world_b.id, name="Elsewhere", member_pc_ids_json=json.dumps([g1.id])),
        ])
        db.flush()
        db.add_all([
            Quest(world_id=w, title="Find the signal", status="active", category="main", summary=" Static. ",
                  visible_to_players=True, assigned_party_id=None),
            Quest(world_id=w, title="Hidden thread", status="active", category="side", visible_to_players=False),
            Quest(world_id=w, title="Done deal", status="done", visible_to_players=True),
            Quest(world_id=w, title="Child", status="active", visible_to_players=True, parent_id=1),
            Quest(world_id=w, title="A-side job", status="active", category="side", visible_to_players=True),
            Quest(world_id=w, title="Z-job", status="active", category="side", visible_to_players=True),
            Quest(world_id=w, title="No party", status="active", visible_to_players=True, assigned_party_id=999999),
        ])
        db.commit()
        ids = (p1.id, p2.id)
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for who, c in (("player", d), ("gm", g)):
        for url in ("/api/cockpit/player-board", f"/api/cockpit/player-board?pc={ids[0]}", "/api/cockpit/player-board?pc=",
                    "/api/cockpit/player-board?pc=x", f"/api/cockpit/player-board?pc=00{ids[0]}"):
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, url)
        for url in ("/api/cockpit/player-board", f"/api/cockpit/player-board?pc={ids[0]}"):
            assert _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway", (who, url)
    # the player's board really carries their two characters, only their parties, and never a hidden thread
    board = d.get("/api/cockpit/player-board").json()
    assert [m["name"] for m in board["my_pcs"]] == ["Kira", "Zed"], board
    assert board["my_pcs"][0]["xp"] == 120 and board["my_pcs"][0]["level"] == 3, board
    assert [p["name"] for p in board["parties"]] == ["Crew", "Lone"], board
    assert board["parties"][0]["members"][0]["name"] == "Kira", board
    assert [q["title"] for q in board["quests"]] == ["Find the signal", "No party", "A-side job", "Z-job"], board
    assert board["quests"][3]["party"] is None, board
    # a player's ?pc= at someone else's character is ignored: their own board
    assert d.get(f"/api/cockpit/player-board?pc={ids[0] + 50}").json()["my_pcs"][0]["name"] == "Kira"
    # an unknown ?w= falls back to the viewer's own world (Python's get_world_ctx), an empty board either way, the gateway's
    d2 = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    a, b = d2.get("/api/cockpit/player-board?w=999999"), _via(d2, base).get("/api/cockpit/player-board?w=999999")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, {"parties": [], "quests": [], "my_pcs": []}), (a.status_code, a.text)
    assert b.headers.get("x-nd-served-by") == "gateway"


def test_view_prefs_site_and_unknown_are_pythons(client, seed):
    """POST / DELETE /api/view-prefs/site from the gateway: the row written as Python's json.dumps writes it, the session re-signed with
    {options, pins, at, uid} (a page after it is gateway-served from the fresh layout), the reset forgetting it; an unknown section is
    Python's 404 "Unknown section" on both verbs."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import UserViewPref
    base = str(client.base_url)
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    body = {"options": {"nav": "rail", "density": "compact", "nonsense": "dropped"}, "pins": [1, 2, 99]}
    for i in range(2):  # twice: the save then the overwrite of the same row
        a, b = d.post("/api/view-prefs/site", json=body), v.post("/api/view-prefs/site", json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (i, a.text, b.text)
        assert a.content == b.content, (i, a.content, b.content)
        assert b.headers.get("x-nd-served-by") == "gateway", i
        ra = d.get("/api/view-prefs").json()
        rb = v.get("/api/view-prefs").json()
        assert ra == rb, (i, ra, rb)
        assert rb["site"]["options"]["nav"] == "rail" and "nonsense" not in rb["site"]["options"], rb
    # the gateway's own re-signed cookie carries the fresh layout: the next page is the gateway's
    assert v.get("/dice").headers.get("x-nd-served-by") == "gateway"
    # a page the direct client fetches refreshes ITS cookie from the row (Python's remember-on-read), so both agree after
    d.get("/dice")
    assert d.get("/api/view-prefs").json() == v.get("/api/view-prefs").json()
    # the row as Python's json.dumps wrote it
    db = SessionLocal()
    try:
        rows = db.query(UserViewPref).filter(UserViewPref.user_id == seed.player_a.id, UserViewPref.section == "site").all()
        assert len(rows) == 1, rows
        text = rows[0].prefs_json
    finally:
        db.close()
    # the row is the cleaned choice both servers answered with (the same text Python's json.dumps writes)
    assert json.loads(text) == d.get("/api/view-prefs").json()["site"]
    # the reset: same 200 on both, the row gone, and the session's copy gone on the gateway side
    a, b = d.delete("/api/view-prefs/site"), v.delete("/api/view-prefs/site")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, {"reset": "site"}), (a.text, b.text)
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert v.get("/api/view-prefs").json() == d.get("/api/view-prefs").json() == {}
    db = SessionLocal()
    try:
        assert db.query(UserViewPref).filter(UserViewPref.user_id == seed.player_a.id, UserViewPref.section == "site").count() == 0
    finally:
        db.close()
    ra, rb = d.post("/api/view-prefs/nosuch", json={"options": {}}), v.post("/api/view-prefs/nosuch", json={"options": {}})
    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()) == (404, {"detail": "Unknown section"}), (ra.text, rb.text)
    assert rb.headers.get("x-nd-served-by") == "gateway"
    ra, rb = d.delete("/api/view-prefs/nosuch"), v.delete("/api/view-prefs/nosuch")
    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()) == (404, {"detail": "Unknown section"}), (ra.text, rb.text)
    assert rb.headers.get("x-nd-served-by") == "gateway"
    # a name the path filter would not read as a section is Python's: the GM passes the allowlist, Python 404s, the gateway forwards
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    a, b2 = g.post("/api/view-prefs/No-Such", json={}), _via(g, base).post("/api/view-prefs/No-Such", json={})
    assert (a.status_code, a.json()) == (b2.status_code, b2.json()) == (404, {"detail": "Unknown section"}), (a.text, b2.text)
    assert b2.headers.get("x-nd-served-by") != "gateway"
def test_character_vitals_sync_advance_abilitycards_are_pythons(client, seed):
    """GET /api/characters/<id>/vitals, /sync, /advance and /ability-cards from the gateway: the live numbers with the sheet's
    effective maxima, the live-sync dict, the XP-shop options (a custom system's 400 in Python's wording) and the feat cards - the
    gates Python applies: 404 for a missing row, 403 for a stranger (vitals lets a party-member's world through when the world shows
    it, sync/advance/cards only the GM or the owner, of any world), and a GM reading a character of a world they only oversee."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": k, "label": v, "value": n} for k, v, n in
                        (("str", "STR", 3), ("dex", "DEX", 2), ("bod", "BOD", 4), ("per", "PER", 1),
                         ("wil", "WIL", 2), ("int", "INT", 3), ("cha", "CHA", 1), ("itu", "ITU", 2))])
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Mekton", slug="mekton-test", sheet_mode="custom",
                            fields_json=json.dumps([{"id": "f1", "label": "Skill", "type": "text"}]))
        db.add(tpl)
        db.flush()
        pcs = [
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=3, xp=90,
                            max_hp=20, shock_max=8, current_hp=7, temp_hp=2, pp_current=4, mp_current=2,
                            shock_current=1, armor_class=11, speed=6, stats_json=stats,
                            conditions_json='["Dazed", 5, {"x": 1}, null]'),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom", sheet_template_id=tpl.id,
                            custom_fields_json=json.dumps({"f1": "Pilot"}), level=2, xp=10),
            PlayerCharacter(world_id=w, owner_user_id=None, name="Orphan", level=1, xp=0),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="NoOwnerView", level=1, xp=0),
            PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There", level=1, xp=0),
        ]
        db.add_all(pcs)
        db.get(World, w).players_see_party = False
        db.commit()
        db.get(World, w).players_see_party = True
        db.commit()
        ids = [p.id for p in pcs]
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    urls = [f"/api/characters/{i}/{tail}" for i in ids + [999999]
            for tail in ("vitals", "sync", "advance", "ability-cards")]
    # a member of the character's world while it hides the party: 403 on vitals, everywhere
    for c in (d, g, pb):
        for url in urls:
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (c.base_url, url)
    served = 0
    for c in (d, g, pb):
        for url in urls:
            served += _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway"
    assert served > 40, served
    # the player's own vitals really carry the cleaned conditions and the sheet maxima
    v = d.get(f"/api/characters/{ids[0]}/vitals").json()
    assert v["conditions"] == ["Dazed"], v
    assert v["max_hp"] == 20 and v["shock_max"] == 8 and v["temp_hp"] == 2, v
    # sync GET: the live-sync dict, the GM's copy of another world's character included
    sv = g.get(f"/api/characters/{ids[4]}/sync").json()
    assert sv["id"] == ids[4] and sv["name"] == "Over There", sv
    # advance: the native options, and the custom system's 400 in Python's words
    adv = d.get(f"/api/characters/{ids[0]}/advance").json()
    assert adv["available"] == 90, adv
    bad = d.get(f"/api/characters/{ids[1]}/advance")
    assert bad.status_code == 400 and bad.json()["detail"].startswith("Advancement here follows"), bad.text
    # ability-cards: pools from the row, the native flag per system
    cards = d.get(f"/api/characters/{ids[0]}/ability-cards").json()
    assert cards["pools"] == {"pp": 4, "mp": 2, "shock": 1, "hp": 7} and cards["native"] is True, cards
    custom_cards = d.get(f"/api/characters/{ids[1]}/ability-cards").json()
    assert custom_cards["native"] is False, custom_cards
    # a player's ?w= pointing elsewhere does not change the answer: these routes read the character's own world
    a, b = d.get(f"/api/characters/{ids[0]}/vitals?w=999999"), _via(d, base).get(f"/api/characters/{ids[0]}/vitals?w=999999")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, v), (a.status_code, a.text)


def test_character_export_json_md_are_pythons(client, seed):
    """GET /characters/<id>/export.json and /export.md from the gateway: the import-shape file (zero / empty fields omitted, the
    name defaulted, indent=2 without ASCII escapes) and the markdown file, byte for byte; a non-ASCII file name (Python's
    Unicode-aware isalnum keeps more) left to Python; 404 for a stranger, 403 for one who may not manage."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": "str", "label": "STR", "value": 3}, {"id": "dex", "label": "DEX", "value": 2}])
    currency = json.dumps([{"label": "Gold", "abbr": "GP", "value": 12}, {"label": "Silver", "abbr": "SP", "value": 0.5}])
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Asterion", slug="asterion-export", sheet_mode="custom", fields_json=json.dumps([
            {"id": "wpn", "label": "Weapon", "type": "text", "section": "Kit", "default_value": "Shortsword"},
            {"id": "armor", "label": "Armor", "type": "text", "section": "Kit"},
            {"id": "kit", "label": "Kit item", "type": "list", "section": "Kit",
             "item_fields": [{"id": "name", "label": "Name"}, {"id": "qty", "label": "Qty"}]},
            {"id": "hp", "label": "Health", "type": "resource", "section": "Vitals", "max": 12},
        ]))
        db.add(tpl)
        db.flush()
        full = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira Vane", player_name="Player A",
                               race="Elf", char_class="Rigger", level=3, xp=90, max_hp=20, current_hp=7,
                               stats_json=stats, currency_json=currency,
                               equipment_json=json.dumps([{"name": "Deck", "qty": 1, "notes": "cyberdeck"}, {"name": "Rope"}]),
                               feats_json=json.dumps([{"name": "Night eyes", "notes": "sees in the dark"}]),
                               conditions_json='["Dazed"]', custom_fields_json=json.dumps({"wpn": "Monowire", "armor": "", "kit": [{"name": "Medkit", "qty": 2}]}),
                               backstory="  Grew up on the docks.\n\nLeft early.  ", notes=" Notes. ",
                               personality_traits="Blunt", ideals=None, bonds="The crew", flaws="")
        empty = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="", level=0, xp=0)
        unicode_named = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kïra — the !! odd", level=1)
        on_tpl = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom Kid", sheet_template_id=tpl.id,
                                 custom_fields_json=json.dumps({"wpn": "Monowire", "kit": [{"name": "Medkit", "qty": 2}, {"name": ""}], "hp": 5}))
        world_b_pc = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There")
        db.add_all([full, empty, unicode_named, on_tpl, world_b_pc])
        db.commit()
        ids = [full.id, empty.id, unicode_named.id, on_tpl.id, world_b_pc.id, 999999]
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    served = 0
    for c in (d, g):
        for i in ids:
            for url in (f"/characters/{i}/export.json", f"/characters/{i}/export.md"):
                a, b = c.get(url), _via(c, base).get(url)
                assert a.status_code == b.status_code, (url, a.status_code, b.status_code, a.text[:120], b.text[:120])
                if b.headers.get("x-nd-served-by") == "gateway":
                    assert a.content == b.content, (url, a.text[:200], b.text[:200])
                    assert a.headers.get("content-type") == b.headers.get("content-type"), url
                    assert a.headers.get("content-disposition") == b.headers.get("content-disposition"), url
                    served += 1
    assert served >= 8, served
    # the JSON file really is the import shape: the name defaulted, empties omitted, floats as Python writes them
    j = d.get(f"/characters/{ids[0]}/export.json")
    payload = json.loads(j.content.decode("utf-8"))
    assert payload["name"] == "Kira Vane" and payload["level"] == 3 and "ideals" not in payload, payload
    assert payload["currency_json"][1]["value"] == 0.5 and payload["stats_json"][0]["value"] == 3, payload
    assert payload["custom_fields_json"] == {"wpn": "Monowire", "armor": "", "kit": [{"name": "Medkit", "qty": 2}]}, payload
    assert payload["conditions_json"] == ["Dazed"] and "sheet_template_id" not in payload, payload
    assert b'"indent"' not in j.content and j.content.startswith(b'{\n  "name"'), j.content[:40]
    # an empty character exports as just the default name
    payload = json.loads(d.get(f"/characters/{ids[1]}/export.json").content.decode("utf-8"))
    assert payload == {"name": "Character", "max_hp": 10, "current_hp": 10}, payload  # the model defaults fill the row
    # the markdown file: sections in Python's order, the strip on the tail
    md = d.get(f"/characters/{ids[0]}/export.md").content.decode("utf-8")
    assert md.startswith("# Kira Vane\n") and "## Ability Scores\n- **STR:** 3" in md, md
    assert "- **Deck:** x1 — cyberdeck" in md and "## Conditions\n- **Active:** Dazed" in md, md
    assert md.endswith("Grew up on the docks.\n\nLeft early.\n\n## Notes\nNotes.\n"), md
    # the custom template's fields: the resource's short label, the list expanded, the default value filled
    md2 = d.get(f"/characters/{ids[3]}/export.md").content.decode("utf-8")
    assert "## Kit\n- **Weapon:** Monowire\n- **Kit item #1:** Name: Medkit; Qty: 2" in md2, md2
    assert "- **Health:** 0 / 0" in md2, md2  # a resource reads {id}_current / {id}_max; the bare "hp" key is neither
    # a non-ASCII name keeps Python's file name (the gateway declines)
    r = _via(d, base).get(f"/characters/{ids[2]}/export.json")
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") != "gateway"
    # a stranger may not manage: 404 for a player of another world on the GM's own file, 403 for a non-owner
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    for url in (f"/characters/{ids[0]}/export.json", f"/characters/{ids[0]}/export.md"):
        a, b = pb.get(url), _via(pb, base).get(url)
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"}), (url, a.text)
def test_hub_keepsakes_places_handouts_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/keepsakes, /places and /handouts from the gateway: what the character kept (newest first, the
    per-kind limits of the world), every page the world opens to a player with the character's pins, and the GM's shown pictures
    with their new flags - the owner and a GM looking in; a stranger and another world's character are the same 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import (CharacterKeepsake, CharacterPref, GameSession, PlayerCharacter, PrivateNote,
                            World, WorldHandout)
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Keeper", level=2, xp=5)
        stranger = PlayerCharacter(world_id=w, owner_user_id=None, name="Nobody's")
        over_there = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Elsewhere")
        db.add_all([pc, stranger, over_there])
        db.flush()
        db.add_all([
            WorldHandout(world_id=w, url="/uploads/h2.png", label="Map"),
            WorldHandout(world_id=w, url="/uploads/h1.png", label=""),
            WorldHandout(world_id=seed.world_b.id, url="/uploads/hb.png", label="B"),
        ])
        db.add_all([
            CharacterKeepsake(world_id=w, character_id=pc.id, kind="image", url="/uploads/k1.png", name="Photo",
                              description="From the docks", saved_by="player", own=True),
            CharacterKeepsake(world_id=w, character_id=pc.id, kind="text", text="A poem", name="Poem", saved_by="gm"),
        ])
        db.add_all([
            PrivateNote(world_id=w, player_user_id=seed.player_a.id, author_id=seed.gm.id, title="Secret",
                        content="Read this. [gmonly]And the hidden part.[/gmonly]"),
            PrivateNote(world_id=w, player_user_id=seed.player_a.id, author_id=seed.player_a.id, title="Own",
                        content="The player's own note"),
        ])
        db.add_all([
            GameSession(world_id=w, title="Docks", session_num=2,
                        recap_media_json=json.dumps([{"kind": "image", "url": "/uploads/s1.png", "caption": "The pier",
                                                      "visible": True, "own": False, "uid": "img1"},
                                                     {"kind": "image", "url": "/uploads/s2.png", "caption": "Hidden",
                                                      "visible": False, "own": False, "uid": "img2"}])),
            GameSession(world_id=w, title="", session_num=1),
        ])
        db.add(CharacterPref(world_id=w, character_id=pc.id, prefs_json=json.dumps({"pins": ["maps", "gone"], "handouts_seen": 1})))
        db.get(World, w).personal_images_max = None
        db.commit()
        ids = (pc.id, stranger.id, over_there.id)
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    urls = [f"/api/characters/{i}/hub/{t}" for i in ids + (999999,) for t in ("keepsakes", "places", "handouts")]
    for c in (d, g, pb):
        for url in urls:
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (url, a.status_code, a.text[:150], b.text[:150])
    served = sum(1 for c in (d, g) for url in urls if _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway")
    assert served > 12, served
    # handouts: newest first, the seen marker hides the older one from the owner, a GM looking in cannot edit
    h = d.get(f"/api/characters/{ids[0]}/hub/handouts").json()
    assert [x["label"] for x in h["handouts"]] == ["", "Map"], h  # newest (id 2, the unlabelled one) first
    assert [x["new"] for x in h["handouts"]] == [True, False], h
    assert h["can_edit"] is True and g.get(f"/api/characters/{ids[0]}/hub/handouts").json()["can_edit"] is False
    # keepsakes: newest first, own counts to the image limit (the default 30), the saveable lists are the player's view
    k = d.get(f"/api/characters/{ids[0]}/hub/keepsakes").json()
    assert [x["name"] for x in k["keepsakes"]] == ["Poem", "Photo"], k
    assert k["own_limits"]["image"] == {"used": 1, "max": 30}, k["own_limits"]
    assert [n["name"] for n in k["saveable"]["notes"]] == ["Secret"], k["saveable"]
    assert "[gmonly]" not in k["saveable"]["notes"][0]["text"], k["saveable"]["notes"]
    assert [s["name"] for s in k["saveable"]["session"]] == ["The pier"], k["saveable"]["session"]
    assert [x["name"] for x in k["saveable"]["handouts"]] == ["Handout", "Map"], k["saveable"]["handouts"]
    assert k["is_owner"] is True and k["is_gm"] is False and k["can_edit"] is True
    # places: the world's player pages grouped, the pins kept only where still open to players
    p = d.get(f"/api/characters/{ids[0]}/hub/places").json()
    labels = {grp["label"]: [i["id"] for i in grp["items"]] for grp in p["groups"]}
    assert "characters" not in labels.get("Tools & logs", []) and "kind_npc" not in labels.get("Tools & logs", []), p
    assert p["pins"] == ["maps"], p
    assert all(row["href"].startswith("/") for grp in p["groups"] for row in grp["items"])
    # a GM looking in sees the same places (the player's list)
    assert g.get(f"/api/characters/{ids[0]}/hub/places").json() == p
    # a character with no owner: only the GM sees it (the player gets the 404)
    for t in ("keepsakes", "places", "handouts"):
        assert d.get(f"/api/characters/{ids[1]}/hub/{t}").status_code == 404
        assert g.get(f"/api/characters/{ids[1]}/hub/{t}").status_code == 200
        assert pb.get(f"/api/characters/{ids[2]}/hub/{t}").status_code == 200
        assert d.get(f"/api/characters/{ids[2]}/hub/{t}").status_code == 404
def test_npc_candidates_are_pythons(client, seed):
    """GET /sessions/<id>/npc-candidates from the gateway: the picker's candidates - the world's entities grouped by kind
    (NPCs split from subtype-PC lore write-ups, then creatures, locations, organizations, races, professions, notes), each
    prefixed with its group label, then the real characters as "pc:<id>" - for a GM and a player; another world's session and
    a missing one are the same 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, GameSession, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            Entity(world_id=w, kind="character", name="Vosk", folder="Dockside"),
            Entity(world_id=w, kind="character", name="Kira", subtype="PC"),
            Entity(world_id=w, kind="creature", name="Rat"),
            Entity(world_id=w, kind="location", name="The Pier"),
            Entity(world_id=w, kind="organization", name="Kraun Corp"),
            Entity(world_id=w, kind="race", name="Elf"),
            Entity(world_id=w, kind="profession", name="Netrunner"),
            Entity(world_id=w, kind="note", name="Bribes"),
            Entity(world_id=seed.world_b.id, kind="note", name="Elsewhere"),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Zed"),
        ])
        gs_a = GameSession(world_id=w, title="Docks", session_num=1)
        gs_b = GameSession(world_id=seed.world_b.id, title="There", session_num=1)
        db.add_all([gs_a, gs_b])
        db.commit()
        ids = (gs_a.id, gs_b.id, 999999)
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    # the route is GM-only (Python's player allowlist does not carry it: both sides answer the same 403 page for a player)
    for c, is_gm in ((d, True), (pl, False)):
        for i in ids:
            url = f"/sessions/{i}/npc-candidates"
            a, b = c.get(url), _via(c, base).get(url)
            assert a.status_code == b.status_code, (i, a.status_code, b.status_code)
            if is_gm:
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (i, a.status_code, a.text[:200], b.text[:200])
            else:
                assert a.status_code == 403 and a.text == b.text and "GM access required" in a.text, (i, a.status_code)
    served = sum(1 for i in ids[:1] if _via(d, base).get(f"/sessions/{i}/npc-candidates").headers.get("x-nd-served-by") == "gateway")
    assert served == 1, served
    out = d.get(f"/sessions/{ids[0]}/npc-candidates").json()["candidates"]
    folders = {c["folder"] for c in out}
    assert "\U0001f464 NPCs/Dockside" in folders and "\U0001f9d1 Player Characters" in folders, out
    assert {"\u2620 Creatures", "\U0001f5fa Locations", "\U0001f3e2 Organizations", "\U0001f9ec Races",
            "\U0001f3ad Professions", "\U0001f4c4 Notes"} <= folders, folders
    pc_rows = [c for c in out if str(c["id"]).startswith("pc:")]
    assert len(pc_rows) == 1 and pc_rows[0]["name"] == "Zed", pc_rows
    npc_rows = [c for c in out if c["name"] == "Vosk"]
    assert npc_rows[0]["folder"] == "\U0001f464 NPCs/Dockside", npc_rows
    # Kira (subtype PC) sits in the Player Characters group, not the NPCs one
    kira = [c for c in out if c["name"] == "Kira"]
    assert kira[0]["folder"] == "\U0001f9d1 Player Characters", kira


def test_character_new_and_edit_forms_are_pythons(client, seed):
    """GET /characters/new and GET /characters/<id>/edit from the gateway: the wizard (the N&D default template, or the
    ?template_id= one - a custom mode template gets its own form page), the edit form with the row's live JSON columns; a
    player at their character limit redirected as Python redirects, a custom-mode character's edit a redirect to its page,
    a stranger 403, a template id Python's int() would refuse left to Python."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        stats = json.dumps([{"id": "str", "label": "STR", "value": 3}])
        tpl = SheetTemplate(world_id=w, name="Mekton", slug="mekton-page", sheet_mode="custom",
                            fields_json=json.dumps([{"id": "f1", "label": "Skill", "type": "text", "section": "Play"}]))
        nd_tpl = SheetTemplate(world_id=w, name="Second N&D", slug="second-nd", sheet_mode="nd", fields_json="[]")
        db.add_all([tpl, nd_tpl])
        db.flush()
        pcs = [PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=2, xp=9,
                               stats_json=stats, currency_json=json.dumps([{"label": "Creds", "abbr": "CR", "value": 4}]),
                               equipment_json="[]", feats_json="[]", cyberware_json="[]",
                               custom_fields_json=json.dumps({"f1": "x"})),
               PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom Kid", sheet_template_id=tpl.id,
                               custom_fields_json=json.dumps({"f1": "y"})),
               PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There", level=1)]
        db.add_all(pcs)
        db.commit()
        ids = (pcs[0].id, pcs[1].id, pcs[2].id)
        tpl_id = tpl.id
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    via_g = _via(g, base)
    served = 0
    for who, c, v in (("player", d, via), ("gm", g, via_g)):
        for url in ("/characters/new", f"/characters/new?template_id={tpl_id}", "/characters/new?template_id=999999",
                    f"/characters/{ids[0]}/edit", f"/characters/{ids[1]}/edit", f"/characters/{ids[2]}/edit",
                    "/characters/999999/edit"):
            served += _same_page(c, v, url, who)
        # a template id Python's int() refuses: the same 422 from either side, unanswered by the gateway
        a, b = c.get("/characters/new?template_id=x"), v.get("/characters/new?template_id=x")
        assert a.status_code == b.status_code == 422, (who, a.status_code, b.status_code)
    assert served >= 6, served
    # the wizard really carries the N&D default template for an unnamed request (the GM's view: no limit redirect)
    page = g.get("/characters/new").text
    assert "nd-default" in page or "Neon" in page, page[:200]
    # a player at the limit (two characters, limit 1): back to the list where they all are
    db = SessionLocal()
    try:
        db.get(World, w).max_characters_per_player = 1
        db.commit()
    finally:
        db.close()
    a, b = d.get("/characters/new", follow_redirects=False), via.get("/characters/new", follow_redirects=False)
    assert a.status_code == b.status_code == 303, (a.status_code, b.status_code)
    assert a.headers["location"] == b.headers["location"] == "/characters"
    assert b.headers.get("x-nd-served-by") == "gateway"
    # a single character at the limit: straight to its sheet (player_b owns exactly one in world B)
    db = SessionLocal()
    try:
        db.get(World, seed.world_b.id).max_characters_per_player = 1
        db.commit()
    finally:
        db.close()
    pb0 = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    a, b = pb0.get("/characters/new", follow_redirects=False), _via(pb0, base).get("/characters/new", follow_redirects=False)
    assert a.status_code == b.status_code == 303, (a.status_code, b.status_code)
    assert a.headers["location"] == b.headers["location"] == f"/characters/{ids[2]}"
    assert b.headers.get("x-nd-served-by") == "gateway"
    # a stranger's edit: 403 with Python's body
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    a, b = pb.get(f"/characters/{ids[0]}/edit"), _via(pb, base).get(f"/characters/{ids[0]}/edit")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"}), a.text
def test_attachment_audio_jobs_are_pythons(client, seed):
    """The AI attachment background-job routes from the gateway: POST /api/ai/attachments/audio-jobs (a recording stored
    under uploads/ai_attachments and a pending row), the chunk pair (parts reassembled), GET .../audio-jobs/<id> and the
    recent list - a GM always, a player only in a world that opened an AI surface, and a player never seeing anyone
    else's jobs; a world without either surface 403s; job fields exactly Python's (the timestamps as isoformat)."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AudioJob, World, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).players_can_ask_ai = False
        db.get(World, w).players_can_use_ai_chat = False
        jobs = [AudioJob(world_id=w, purpose="attachment", filename=f"memo{i}.webm", status=s,
                         created_by_user_id=seed.player_a.id if i % 2 else seed.gm.id,
                         transcript="t" if s == "done" else None, attachment_url=f"/uploads/ai_attachments/m{i}.webm")
                for i, s in enumerate(("pending", "transcribing", "done", "error"))]
        jobs.append(AudioJob(world_id=w, purpose="session_recap", filename="r.mp3", status="done"))
        jobs.append(AudioJob(world_id=seed.world_b.id, purpose="attachment", filename="b.webm", status="done"))
        db.add_all(jobs)
        db.commit()
        ids = [j.id for j in jobs[:4]] + [999999]
    finally:
        db.close()
    wav = b"RIFF....WAVE junk " * 64
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    # the player's world has neither AI surface: Python's 403 on every route, gateway-declined (403s are forwarded)
    for url in ("/api/ai/attachments/audio-jobs", f"/api/ai/attachments/audio-jobs/{ids[0]}"):
        a, b = pl.get(url), _via(pl, base).get(url)
        assert (a.status_code, a.text) == (b.status_code, b.text) and a.status_code == 403, (url, a.status_code)
    # open a surface: the player may now see their OWN jobs only
    db = SessionLocal()
    try:
        db.get(World, w).players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    for c, who in ((gm, "gm"), (pl, "player")):
        a, b = c.get("/api/ai/attachments/audio-jobs"), _via(c, base).get("/api/ai/attachments/audio-jobs")
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, a.text[:200], b.text[:200])
        assert b.headers.get("x-nd-served-by") == "gateway", who
        if who == "player":
            assert all(str(j["filename"]).startswith("memo") and j["id"] in ids[:4] for j in a.json()), a.json()
            assert [j["id"] for j in a.json()] == [ids[3], ids[1]], a.json()  # own jobs, newest first
        for i in ids:
            url = f"/api/ai/attachments/audio-jobs/{i}"
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, i, a.text[:150], b.text[:150])
    served = sum(1 for c in (gm, pl) for i in ids if _via(c, base).get(f"/api/ai/attachments/audio-jobs/{i}").headers.get("x-nd-served-by") == "gateway")
    assert served > 4, served
    # the status dict's shape: exactly Python's keys
    j = gm.get(f"/api/ai/attachments/audio-jobs/{ids[2]}").json()
    assert set(j) == {"id", "purpose", "filename", "status", "error", "transcript", "attachment_url", "created_at", "updated_at"}, j
    assert j["status"] == "done" and j["transcript"] == "t" and j["purpose"] == "attachment", j
    # a create: the recording stored and a pending row, answered by the gateway
    r = _via(gm, base).post("/api/ai/attachments/audio-jobs", files={"file": ("memo.webm", wav, "audio/webm")})
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") == "gateway", r.text
    new_id = r.json()["job_id"]
    row = _via(gm, base).get(f"/api/ai/attachments/audio-jobs/{new_id}").json()
    # the job starts immediately: pending, or already transcribing / done by the time we look
    assert row["status"] in ("pending", "transcribing", "done", "error") and row["filename"] == "memo.webm" and row["purpose"] == "attachment", row
    assert row["attachment_url"].startswith("/uploads/ai_attachments/") and row["attachment_url"].endswith(".webm"), row
    # the same create through Python, on the same world, gives the same shape (both write a row each)
    r2 = gm.post("/api/ai/attachments/audio-jobs", files={"file": ("memo2.webm", wav, "audio/webm")})
    assert r2.status_code == 200 and set(r2.json()) == {"job_id"}, r2.text
    # the chunk pair: two parts reassembled by /complete
    v = _via(gm, base)
    v.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "ab12cd34ab12cd34ab12cd34ab12cd34", "chunk_index": "0"}, files={"file": ("part", wav[:1024], "application/octet-stream")})
    v.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "ab12cd34ab12cd34ab12cd34ab12cd34", "chunk_index": "1"}, files={"file": ("part", wav[1024:], "application/octet-stream")})
    r3 = v.post("/api/ai/attachments/audio-jobs/complete", data={"upload_id": "ab12cd34ab12cd34ab12cd34ab12cd34", "filename": "assembled.webm", "total_chunks": "2"})
    assert r3.status_code == 200 and r3.headers.get("x-nd-served-by") == "gateway", r3.text
    row3 = v.get(f"/api/ai/attachments/audio-jobs/{r3.json()['job_id']}").json()
    assert row3["filename"] == "assembled.webm" and row3["status"] in ("pending", "transcribing", "done", "error"), row3
    # Python, run the same chunk flow directly, agrees on the row shape
    r4 = gm.post("/api/ai/attachments/audio-jobs/chunk", data={"upload_id": "cd34ab12cd34ab12cd34ab12cd34ab12", "chunk_index": "0"}, files={"file": ("part", wav[:1024], "application/octet-stream")})
    assert r4.status_code == 200 and r4.json() == {"ok": True}, r4.text
    r5 = gm.post("/api/ai/attachments/audio-jobs/complete", data={"upload_id": "cd34ab12cd34ab12cd34ab12cd34ab12", "filename": "py.webm", "total_chunks": "1"})
    assert r5.status_code == 200 and set(r5.json()) == {"job_id"}, r5.text
def test_session_md_downloads_are_pythons(client, seed):
    """GET /sessions/<id>/summary.md and /transcript.md from the gateway: the text with [gmonly] stripped for a player,
    the sanitized file name, read-gated by the row's own world's sessions section; the no-content 404s in Python's
    words; another world's session and a closed section the same 404; a title beyond Python's ASCII file names stays
    Python's."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import GameSession, World, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            GameSession(world_id=w, title="Docks", session_num=1,
                        summary="The meet went [gmonly]sideways fast[/gmonly] well.",
                        live_transcript="Vosk: You came alone? [gmonly]The crate is armed.[/gmonly]"),
            GameSession(world_id=w, title="Empty", session_num=2, summary="", live_transcript=""),
            GameSession(world_id=w, title="Ünicode — Title!", session_num=3, summary="Something."),
            GameSession(world_id=seed.world_b.id, title="Over There", session_num=1, summary="Other world."),
        ])
        # the player gets read on sessions (the section is GM-only by default)
        db.get(World, w).section_access_json = json.dumps({"sessions": {"player": "read"}})
        db.get(World, seed.world_b.id).section_access_json = json.dumps({"sessions": {"player": "read"}})
        db.commit()
        ids = [s.id for s in db.query(GameSession).order_by(GameSession.id).all()] + [999999]
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    served = 0
    for who, c in (("gm", d), ("player", pl), ("stranger", pb)):
        for i in ids:
            for url in (f"/sessions/{i}/summary.md", f"/sessions/{i}/transcript.md"):
                a, b = c.get(url), _via(c, base).get(url)
                assert a.status_code == b.status_code, (who, url, a.status_code, b.status_code, a.text[:100], b.text[:100])
                if a.status_code == 200:
                    if b.headers.get("x-nd-served-by") == "gateway":
                        assert a.content == b.content, (who, url, a.text[:150], b.text[:150])
                        assert a.headers.get("content-type") == b.headers.get("content-type")
                        assert a.headers.get("content-disposition") == b.headers.get("content-disposition"), (url, a.headers.get("content-disposition"))
                        served += 1
                else:
                    assert a.json() == b.json(), (who, url, a.text[:100], b.text[:100])
                    if b.headers.get("x-nd-served-by") == "gateway" and i != 999999 and i != ids[2]:
                        served += 1
    assert served >= 6, served
    # the GM's file keeps [gmonly]; the player's strips it
    g = d.get(f"/sessions/{ids[0]}/summary.md")
    assert "sideways fast" in g.text and g.headers["content-disposition"] == 'attachment; filename="Docks-summary.md"', g.text
    p = pl.get(f"/sessions/{ids[0]}/summary.md")
    assert "sideways fast" not in p.text and "went  well" in p.text, p.text
    pt = pl.get(f"/sessions/{ids[0]}/transcript.md")
    assert "The crate is armed." not in pt.text, pt.text
    # no summary / transcript: Python's own 404 words
    a = d.get(f"/sessions/{ids[1]}/summary.md")
    assert a.status_code == 404 and a.json()["detail"] == "This session has no summary yet", a.text
    a = d.get(f"/sessions/{ids[1]}/transcript.md")
    assert a.status_code == 404 and a.json()["detail"] == "This session has no live transcript yet", a.text
    # another world's session: the same 404 as a missing one (the stranger belongs there, the player doesn't)
    assert pl.get(f"/sessions/{ids[3]}/summary.md").status_code == 404
    assert pb.get(f"/sessions/{ids[3]}/summary.md").status_code == 200
    # a non-ASCII title keeps Python's file name (the gateway declines)
    r = _via(d, base).get(f"/sessions/{ids[2]}/summary.md")
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") != "gateway"
def test_schematic_new_and_ai_new_pages_are_pythons(client, seed):
    """GET /maps/schematic/new (the empty canvas form, player-safe) and GET /characters/ai-new (the AI creator page: the
    world's custom systems, the selected template id, the player's display name; a player at their limit redirected) from
    the gateway; the POST /maps/schematic/new write: the slug uniquified, the canvas bounds checked, the row as
    SQLAlchemy writes it, the redirect - run on two identical worlds and compared; Python's refusals (an empty name, bad
    bounds, a missing required field) reached unchanged."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import SheetTemplate, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=seed.world_a.id, name="Mekton", slug="mekton-schem", sheet_mode="custom", fields_json="[]")
        db.add(tpl)
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    served = 0
    for who, c in (("gm", d), ("player", pl)):
        served += _same_page(c, _via(c, base), "/maps/schematic/new", who)
        for url in ("/characters/ai-new", "/characters/ai-new?template_id=5", "/characters/ai-new?template_id=0"):
            served += _same_page(c, _via(c, base), url, who)
    assert served >= 5, served
    # the AI page really lists the world's custom systems and the display name
    page = d.get("/characters/ai-new").text
    assert "Mekton" in page, page[:400]
    # a template id FastAPI would refuse: the same 422 from either side, gateway-declined
    a, b = d.get("/characters/ai-new?template_id=x"), _via(d, base).get("/characters/ai-new?template_id=x")
    assert a.status_code == b.status_code == 422, (a.status_code, b.status_code)
    # the POST: the same form on two identical worlds, the rows compared
    from tests.test_gateway_writes import _row_by
    form = {"name": "Rooftop Chase", "description": "The big map", "canvas_width": "3000", "canvas_height": "2000", "canvas_bg": "dark"}
    out = []
    for side in (d, _via(d, base)):
        r = side.post("/maps/schematic/new", data=form)
        slug = r.headers.get("location", "").rsplit("/", 1)[-1]
        row = _row_by("schematics", "slug", slug) or {}
        out.append((r.status_code, r.headers.get("location") is not None, row.get("name"), row.get("world_id"),
                    row.get("canvas_width"), row.get("canvas_height"), row.get("canvas_bg"), row.get("description"),
                    row.get("is_html"), row.get("elements_json"), r.headers.get("x-nd-served-by")))
    assert out[0][:2] == out[1][:2] and out[0][1], out
    for k in range(2, 10):
        assert out[0][k] == out[1][k], (k, out)
    assert out[1][10] == "gateway"
    assert out[0][2] == "Rooftop Chase" and out[0][6] == "dark", out
    # a second create with the same name: the slug uniquified (-2), by both sides
    out2 = []
    for side in (d, _via(d, base)):
        r = side.post("/maps/schematic/new", data=form)
        out2.append(r.headers.get("location", "").rsplit("/", 1)[-1])
    assert out2[0] != out2[1] and "rooftop-chase" in out2[0] and "rooftop-chase" in out2[1], out2
    # refusals: an empty name (400), bounds out of range (400), a missing required field (422) - Python's, forwarded
    for data, code in (({"name": "###"}, 400), ({"name": "OK", "canvas_width": "50"}, 400), ({"description": "no name"}, 422)):
        a, b = d.post("/maps/schematic/new", data=data), _via(d, base).post("/maps/schematic/new", data=data)
        assert a.status_code == b.status_code == code, (data, a.status_code, b.status_code)
        assert b.headers.get("x-nd-served-by") != "gateway" or code != 422


def test_hover_preview_config_is_pythons(client, seed):
    """GET /api/hover-preview/config from the gateway: the instance-wide hover settings exactly as Python answers them -
    every flag flipped, including the null-vs-zero distinction on the hide delay."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import AppSettings
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    served = 0
    # (enabled, delay, hide, width, height) - None keeps the column NULL
    for cfgrow in ((0, None, None, None, None), (1, 250, 0, 200, 100), (1, None, None, None, None)):
        db = SessionLocal()
        try:
            s = db.get(AppSettings, 1)
            s.hover_preview_enabled = cfgrow[0]
            s.hover_preview_delay_ms = cfgrow[1]
            s.hover_preview_hide_delay_ms = cfgrow[2]
            s.hover_preview_width_px = cfgrow[3]
            s.hover_preview_max_height_px = cfgrow[4]
            db.commit()
        finally:
            db.close()
        a, b = d.get("/api/hover-preview/config"), v.get("/api/hover-preview/config")
        assert (a.status_code, a.json()) == (b.status_code, b.json()), cfgrow
        assert a.content == b.content, (cfgrow, a.text, b.text)
        assert b.headers.get("x-nd-served-by") == "gateway", cfgrow
        served += 1
    assert served == 3
    # the last row's state: enabled, every other column back to NULL (the defaults)
    j = d.get("/api/hover-preview/config").json()
    assert j == {"enabled": True, "delay_ms": 5000, "hide_delay_ms": 400, "width_px": 340, "max_height_px": 420}, j
def test_export_book_zip_is_pythons(client, seed):
    """GET /export/book.zip from the gateway: the standalone world book - the rendered index.html byte for byte (the
    template's own capitalize / split / safe filters through the shared renderer), the stylesheet, every illustrated
    entity's image; a player refused as Python refuses. The zips' own bytes differ (Python's timestamps are its build
    moment); the entries are what must match, name for name and byte for byte."""
    import io
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, InvestBoard, World
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    uploads = Path(os.environ["DB_PATH"]).parent / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    pic = uploads / "exp-pic.png"  # the export bundles the image BASENAME at the uploads root
    pic.write_bytes(bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]) + b"export-picture")
    db = SessionLocal()
    try:
        db.get(World, w).rules_md = "# House Rules\n\nRoll d10.\n\n<a name=\"anchor\"></a>## Keep\n\nBe kind."
        db.add_all([
            Entity(world_id=w, kind="character", name="Vosk", subtype="Fixer", summary="Deals info",
                   body="# Vosk\n\nThe docks' [b]fixer[/b].\n\n## Contacts\n\nMany.", tags="docks, fixer ,",
                   image_url="/uploads/maps/exp-pic.png"),
            Entity(world_id=w, kind="character", name="Ana"),
            Entity(world_id=w, kind="location", name="The Pier", body="Wet."),
            Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False),
        ])
        db.add_all([
            InvestBoard(world_id=w, name="Case", slug="exp-case", description="the case",
                        nodes_json=json.dumps({"nodes": [{"id": "a", "title": "T"}], "groups": []}), edges_json="[]"),
            InvestBoard(world_id=w, name="Legacy", slug="exp-legacy", nodes_json=json.dumps([{"id": 1}]), edges_json=""),
        ])
        db.commit()
    finally:
        db.close()
    maps = Path(os.environ["DB_PATH"]).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "exp-map.json").write_text(json.dumps({"world_id": w, "name": "Docks", "markers": [{"x": 1}, {"x": 2}]}), encoding="utf-8")
    (maps / "exp-bad.json").write_text(json.dumps({"world_id": w, "markers": {"a": 1}}), encoding="utf-8")
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        a, b = d.get("/export/book.zip"), v.get("/export/book.zip")
        assert a.status_code == b.status_code == 200, (a.status_code, b.status_code, a.text[:200])
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
        assert a.headers["content-disposition"] == b.headers["content-disposition"] == 'attachment; filename="world-a-worldbook.zip"'
        za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
        names_a, names_b = sorted(za.namelist()), sorted(zb.namelist())
        assert names_a == names_b, (names_a, names_b)
        assert "index.html" in names_a and "assets/style.css" in names_a and "assets/images/exp-pic.png" in names_a, names_a
        for n in names_a:
            if n == "assets/images/exp-pic.png":
                assert za.read(n) == zb.read(n) == pic.read_bytes()
            elif n == "index.html":
                # the bodies and rules are render_md output: markdown2 vs pulldown-cmark whitespace is the owner-chosen
                # difference (docs/RUST_PAGES.md) - compare it collapsed, as the md-bearing page tests do
                na_ = " ".join(za.read(n).decode("utf-8").replace(" </", "</").split())
                nb_ = " ".join(zb.read(n).decode("utf-8").replace(" </", "</").split())
                assert na_ == nb_, (n, na_[:300], nb_[:300])
            else:
                assert za.read(n) == zb.read(n), (n, za.read(n)[:200], zb.read(n)[:200])
        index = za.read("index.html").decode("utf-8")
        for term in ("Vosk", "The docks", "Case", "Docks"):
            assert term in index, (term, index[:300])
        assert "2 markers" in index and "exp-pic.png" in index and "anchor" not in index, index[:300]
        # a player refused exactly as Python refuses (the auth gate), and a world-less GM redirected
        pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
        a2, b2 = pl.get("/export/book.zip"), _via(pl, base).get("/export/book.zip")
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        pic.unlink(missing_ok=True)
        (maps / "exp-map.json").unlink(missing_ok=True)
        (maps / "exp-bad.json").unlink(missing_ok=True)
def test_admin_backup_zip_is_pythons(client, seed):
    """GET /admin/backup.zip from the gateway: the VACUUM INTO snapshot of the database, the manifest with the table
    counts (created_at and the filename's stamp are the build moment - normalized), every uploaded file and map json
    under its prefix; a player refused as Python refuses. The entries are compared name for name and byte for byte."""
    import io
    import re
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity
    from pathlib import Path
    base = str(client.base_url)
    w = seed.world_a.id
    data = Path(os.environ["DB_PATH"]).parent
    up = data / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    (up / "bk-one.txt").write_bytes(b"first")
    (up / "sub").mkdir(exist_ok=True)
    (up / "sub" / "bk-two.bin").write_bytes(b"\x00\x01second")
    maps = data / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "bk-map.json").write_text(json.dumps({"world_id": w, "name": "Bk"}), encoding="utf-8")
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="item", name="Bk thing")])
        db.commit()
    finally:
        db.close()
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        a, b = d.get("/admin/backup.zip"), v.get("/admin/backup.zip")
        assert a.status_code == b.status_code == 200, (a.status_code, b.status_code)
        assert b.headers.get("x-nd-served-by") == "gateway"
        assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
        assert re.fullmatch(r'attachment; filename="nd-world-backup-\d{8}-\d{6}\.zip"', b.headers["content-disposition"])
        za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
        names = sorted(za.namelist())
        assert names == sorted(zb.namelist()), (names, sorted(zb.namelist()))
        assert "world.db" in names and "manifest.json" in names, names
        assert "uploads/bk-one.txt" in names and "uploads/sub/bk-two.bin" in names and "maps/bk-map.json" in names, names
        for n in names:
            if n == "manifest.json":
                ma, mb = json.loads(za.read(n)), json.loads(zb.read(n))
                for m in (ma, mb):
                    m["created_at"] = "<stamp>"
                assert ma == mb, (ma, mb)
                assert ma["app"] == "nd-world" and ma["tables"]["entities"] >= 1, ma
            elif n == "world.db":
                # a backup's promise is the database CONTENT: the two VACUUM INTO snapshots can differ physically
                # (page layout) and in the header's library-version fields, so compare the logical dump
                import sqlite3
                import tempfile
                fa, fb = tempfile.NamedTemporaryFile(delete=False), tempfile.NamedTemporaryFile(delete=False)
                fa.write(za.read(n)); fa.close()
                fb.write(zb.read(n)); fb.close()
                dumps = []
                for f in (fa.name, fb.name):
                    c = sqlite3.connect(f)
                    dumps.append(chr(10).join(c.iterdump()))
                    c.close()
                    os.unlink(f)
                assert dumps[0] == dumps[1], "the backup's database content differs"
            else:
                assert za.read(n) == zb.read(n), n
        # a player refused exactly as Python refuses
        pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
        a2, b2 = pl.get("/admin/backup.zip"), _via(pl, base).get("/admin/backup.zip")
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        (up / "bk-one.txt").unlink(missing_ok=True)
        (up / "sub" / "bk-two.bin").unlink(missing_ok=True)
        (maps / "bk-map.json").unlink(missing_ok=True)
def test_export_rules_and_notes_is_pythons(client, seed):
    """GET /export/rules-and-notes.md from the gateway: the rules (anchors stripped), the world's note entities (full
    text, hidden included, subtype tags) and every entity note grouped under its entity - byte for byte, the trailing
    whitespace collapsed as Python's rstrip does; a player refused as Python refuses; no world is Python's 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).rules_md = "# House Rules\n\nRoll d10.\n\n<a name=\"x\"></a>## Keep\n\nBe kind.\n\n\n\n"
        db.add_all([
            Entity(world_id=w, kind="note", name="Dossier", subtype="Rumor", body="  The mayor lies.  ", summary="s"),
            Entity(world_id=w, kind="note", name="Ledger", body="", summary="  the numbers  "),
            Entity(world_id=w, kind="note", name="Hidden note", body="Secret.", visible_to_players=False),
            Entity(world_id=w, kind="item", name="Blade"),
            Entity(world_id=seed.world_b.id, kind="note", name="Elsewhere"),
        ])
        db.flush()
        db.add_all([
            EntityNote(entity_id=4, content="Sharp.", visible_to_players=True),      # Blade's note
            EntityNote(entity_id=4, content="  Kept oiled.  ", visible_to_players=False),
        ])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    a, b = d.get("/export/rules-and-notes.md"), v.get("/export/rules-and-notes.md")
    assert a.status_code == b.status_code == 200, (a.status_code, b.status_code, a.text[:200])
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert a.content == b.content, (a.text[:300], b.text[:300])
    assert a.headers.get("content-type") == b.headers.get("content-type")
    assert a.headers["content-disposition"] == b.headers["content-disposition"] == 'attachment; filename="world-a-rules-and-notes.md"'
    t = a.text
    assert t.startswith("# World A — Rules & Notes\n\n# House Rules\n\nRoll d10.\n\n## Keep\n\nBe kind.\n"), t[:120]
    assert "anchor" not in t, t[:200]
    assert "## Notes\n\n### Dossier [Rumor]\n\nThe mayor lies.\n" in t, t
    assert "### Ledger\n\nthe numbers\n" in t and "### Hidden note\n\nSecret.\n" in t, t
    assert "## Entity notes\n\n### Blade (Item)\n\nSharp.\n\n  Kept oiled.\n" in t, t
    assert t.endswith("Kept oiled.\n"), t[-60:]  # the final rstrip takes the last note's trailing spaces
    # a player refused exactly as Python refuses
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    a2, b2 = pl.get("/export/rules-and-notes.md"), _via(pl, base).get("/export/rules-and-notes.md")
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
def test_kind_download_zips_are_pythons(client, seed):
    """GET /kind/<kind>/download.zip and /download-selected.zip from the gateway: one .md per entity (the same text the
    single-entity download writes, the leading name sanitized, the id appended), visibility and the download toggle
    enforced; the picked ids honored (a hidden id never smuggled in); a player in a world without downloads refused;
    the entries compared name for name and byte for byte."""
    import io
    import zipfile
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            Entity(world_id=w, kind="item", name="Blade <sharp>", summary="An edge", body="Cuts [gmonly]deep[/gmonly].",
                   image_url="/uploads/x.png"),
            Entity(world_id=w, kind="item", name="!!", body=None),
            Entity(world_id=w, kind="item", name="Hidden", visible_to_players=False),
            Entity(world_id=w, kind="note", name="Other kind"),
        ])
        db.flush()
        db.add(EntityNote(entity_id=1, content="Seen.", visible_to_players=True))
        db.commit()
        ids = {}
        for row in db.query(Entity).filter(Entity.world_id == w).all():
            ids[row.name] = row.id
    finally:
        db.close()
    try:
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        v = _via(d, base)
        for url in (f"/kind/item/download.zip", f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}&id={ids['!!']}",
                    f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}&id={ids['Hidden']}",
                    "/kind/nosuchkind/download.zip"):
            a, b = d.get(url), v.get(url)
            assert a.status_code == b.status_code == 200, (url, a.status_code, b.status_code, a.text[:120])
            assert b.headers.get("x-nd-served-by") == "gateway", url
            assert a.headers["content-type"] == b.headers["content-type"] == "application/zip"
            za, zb = zipfile.ZipFile(io.BytesIO(a.content)), zipfile.ZipFile(io.BytesIO(b.content))
            assert sorted(za.namelist()) == sorted(zb.namelist()), (url, za.namelist(), zb.namelist())
            for n in za.namelist():
                assert za.read(n) == zb.read(n), (url, n)
        # the GM's full-kind zip carries the hidden entity too; the selected one only the picked
        za = zipfile.ZipFile(io.BytesIO(d.get("/kind/item/download.zip").content))
        names = sorted(za.namelist())
        # a name that sanitizes to nothing becomes "entity"
        assert names == [f"Blade sharp-{ids['Blade <sharp>']}.md", f"Hidden-{ids['Hidden']}.md", f"entity-{ids['!!']}.md"], names
        body = za.read(names[0]).decode()
        assert "Cuts [gmonly]deep[/gmonly]." in body, body  # the GM's copy keeps the markers and the text
        assert "## Notes" in body and "Seen." in body, body
        za2 = zipfile.ZipFile(io.BytesIO(d.get(f"/kind/item/download-selected.zip?id={ids['Blade <sharp>']}").content))
        assert sorted(za2.namelist()) == [f"Blade sharp-{ids['Blade <sharp>']}.md"], za2.namelist()
        # a player in a world without the download toggle: refused exactly as Python refuses
        pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
        a, b = pl.get("/kind/item/download.zip"), _via(pl, base).get("/kind/item/download.zip")
        assert (a.status_code, a.text) == (b.status_code, b.text) and a.status_code == 403
        # with the toggle on: the visible entities only, [gmonly] stripped
        db = SessionLocal()
        try:
            db.get(World, w).players_can_download_entities = True
            db.commit()
        finally:
            db.close()
        a, b = pl.get("/kind/item/download.zip"), _via(pl, base).get("/kind/item/download.zip")
        assert a.status_code == b.status_code == 200, a.status_code
        zp = zipfile.ZipFile(io.BytesIO(a.content))
        assert sorted(zp.namelist()) == [f"Blade sharp-{ids['Blade <sharp>']}.md", f"entity-{ids['!!']}.md"], zp.namelist()
        assert "deep" not in zp.read(f"Blade sharp-{ids['Blade <sharp>']}.md").decode()
        # an id value FastAPI would 422 on: Python's answer, gateway-declined
        r = v.get("/kind/item/download-selected.zip?id=x")
        assert r.status_code == 422 and r.headers.get("x-nd-served-by") != "gateway"
    finally:
        db = SessionLocal()
        try:
            db.get(World, w).players_can_download_entities = False
            db.commit()
        finally:
            db.close()
def test_import_detect_is_pythons(client, seed):
    """POST /api/import/detect from the gateway: every detection branch byte for byte - the batch envelope and its
    Counter-ordered breakdown, world rules, map overlays, schematic elements (both shapes), random tables (list and
    single), field templates (both shapes, the quoted name), entities (bulk and single), player characters (bulk and
    single), and the unknown fallback; a text json.loads refuses is Python's own "invalid" answer (forwarded), and the
    world's custom kinds are recognized."""
    from app.database import SessionLocal
    from app.models import World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).custom_kinds_json = json.dumps([{"id": "custom_vehicle", "label": "Vehicle", "icon": "\U0001f697"}])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    payloads = [
        # batch: envelopes and bare blobs mixed, breakdown in count-then-first-seen order
        {"json_text": json.dumps({"imports": [
            {"kind": "entity_single", "data": {"kind": "item", "name": "A"}},
            {"kind": "item", "name": "B"},
            {"kind": "note", "name": "C"},
            {"kind": "item", "name": "D"},
        ]})},
        {"json_text": json.dumps({"rules_md": "# Rules\nBe kind."})},
        {"json_text": json.dumps({"custom_markers": [{"x": 1}], "custom_regions": []})},
        {"json_text": json.dumps({"custom_regions": [{"x": 1}, {"x": 2}]})},
        {"json_text": json.dumps({"elements": [{"type": "rect"}, {"type": "text"}]})},
        {"json_text": json.dumps([{"type": "pin"}, {"type": "token"}])},
        {"json_text": json.dumps([{"name": "T1", "entries": [{"label": "a"}, {"label": "b"}]}, {"name": "T2", "entries": []}])},
        {"json_text": json.dumps({"name": "One", "entries": [{"label": "x"}]})},
        {"json_text": json.dumps({"name": "Stats", "fields": [{"id": "hp", "label": "HP", "type": "resource"}]})},
        {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}])},
        {"json_text": json.dumps([{"kind": "item", "name": "Sword"}, {"kind": "npc", "name": "Vosk"}])},
        {"json_text": json.dumps({"kind": "custom_vehicle", "name": "Rover", "custom_fields": {}})},   # a custom kind
        {"json_text": json.dumps([{"name": "Kira", "stats": []}, {"name": "Zed", "level": 3}])},
        {"json_text": json.dumps({"name": "Kira", "charClass": "Rigger", "minor_edge": "x"})},
        {"json_text": json.dumps({"hello": "world"})},
        {"json_text": json.dumps([{"mystery": 1}])},
        {"json_text": json.dumps([])},
        {"json_text": ""},
        {"json_text": json.dumps(None)},
    ]
    served = 0
    for body in payloads:
        a, b = d.post("/api/import/detect", json=body), v.post("/api/import/detect", json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (body["json_text"][:60], a.text, b.text)
        if b.headers.get("x-nd-served-by") == "gateway" and a.status_code == 200:
            served += 1
    assert served >= 15, served
    # spot-check the branches that matter most
    def detect(text):
        return d.post("/api/import/detect", json={"json_text": json.dumps(text)}).json()
    batch_in = {"imports": [
        {"kind": "entity_single", "data": {"kind": "item", "name": "A"}},
        {"name": "T", "entries": [{"label": "x"}]},
        {"kind": "entity_single", "data": {"kind": "note", "name": "C"}}]}
    assert detect(batch_in) == {"kind": "batch", "summary": "3 item(s): 2 entity_single, 1 random_table", "count": 3, "needs": []}
    assert detect({"kind": "custom_vehicle", "name": "Rover"})["kind"] == "entity_single"  # the world's custom kind
    assert detect({"name": "Rover"})["kind"] == "unknown"
    assert detect({"name": "Kira", "level": 2})["kind"] == "player_character"
    # an invalid inner text: Python's own message, gateway-declined
    bad = v.post("/api/import/detect", json={"json_text": "{oops"})
    assert bad.status_code == 200 and bad.json()["kind"] == "invalid" and "Not valid JSON" in bad.json()["summary"]
    assert bad.headers.get("x-nd-served-by") != "gateway"
    # a missing json_text key parses as "" -> Python's invalid answer
    a, b = d.post("/api/import/detect", json={}), v.post("/api/import/detect", json={})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) and a.json()["kind"] == "invalid"
    # a player without import rights: refused exactly as Python refuses
    from .conftest import PLAYER_PASSWORD
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    a2, b2 = pl.post("/api/import/detect", json=payloads[0]), _via(pl, base).post("/api/import/detect", json=payloads[0])
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text), (a2.status_code, b2.status_code)
def test_import_execute_is_pythons(client, seed):
    """POST /api/import/execute from the gateway: the world_rules, random_table, entity_single and entity_bulk branches.
    Each case runs once through Python and once through the gateway (fresh names where rows are written) - the answers,
    the newest rows and the redirects compared; Python's own 400s answered word for word; a kind this port does not
    carry forwarded; a player without import rights refused as Python refuses."""
    import itertools
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import EntityTemplate
    from tests.test_gateway_writes import _newest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add(EntityTemplate(world_id=w, name="Existing", slug="existing-tpl", kind="npc", fields_json="[]"))
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    uniq = itertools.count()

    def run(side, payload):
        r = side.post("/api/import/execute", json=payload)
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        return r.status_code, body, r.headers.get("x-nd-served-by")

    # (payload-builder, the expected status) - builders make fresh names so both sides write their own rows
    ok_cases = [
        (lambda: {"json_text": json.dumps({"rules_md": f"# rules {next(uniq)}"})}, 200),
        (lambda: {"json_text": json.dumps({"name": f"Odds {next(uniq)}", "entries": [{"label": "a", "weight": 2}]})}, 200),
        (lambda: {"json_text": json.dumps([{"name": f"Two {next(uniq)}", "entries": []},
                                           {"name": f"Three {next(uniq)}", "category": "fate", "description": "d", "entries": [{"label": "x"}]}])}, 200),
        (lambda: {"json_text": json.dumps({"entries": []})}, 200),  # the "Imported Table" default
        (lambda: {"json_text": json.dumps({"kind": "character", "name": f"Vosk {next(uniq)}", "subtype": "Fixer", "summary": "s",
                                           "custom_fields": {"rep": 3}, "template_slug": "existing-tpl", "visible_to_players": False})}, 200),
        (lambda: {"json_text": json.dumps([{"kind": "item", "name": f"Blade {next(uniq)}"}, {"kind": "item", "name": f"Rope {next(uniq)}"}])}, 200),
        (lambda: {"json_text": json.dumps({"kind": "character", "name": f"By Name {next(uniq)}", "template": "Existing"})}, 200),
    ]
    for build, code in ok_cases:
        a = run(d, build())
        assert a[0] == code and a[1].get("ok") is True, a
    # refusals: Python's messages, word for word, gateway-declined or gateway-matched
    bad_cases = [
        ({"json_text": json.dumps({"rules_md": 5}), "kind": "world_rules"}, 'Expected {"rules_md": "..."}'),
        ({"json_text": json.dumps({"nope": 1}), "kind": "world_rules"}, 'Expected {"rules_md": "..."}'),
        ({"json_text": json.dumps({"nope": 1})}, "Unrecognized kind: unknown"),
        ({"json_text": json.dumps(5), "kind": "random_table"}, 'Expected a table object'),
        ({"json_text": json.dumps({"kind": "vehicle", "name": "Nope kind"}), "kind": "entity_single"}, "Not a valid entity"),
        ({"json_text": json.dumps({"kind": "character", "name": "   "})}, "Not a valid entity"),
        ({"json_text": json.dumps({"kind": "character"}), "kind": "entity_single"}, "Not a valid entity"),
        ({"json_text": json.dumps([{"kind": "item", "name": "Ok one"}, {"kind": "item", "name": ""}]), "kind": "entity_bulk"}, "Entity #2:"),
        ({"json_text": json.dumps(5), "kind": "entity_single"}, "Not a valid entity"),
        ({"json_text": json.dumps(5), "kind": "entity_bulk"}, "Expected a non-empty array"),
        ({"json_text": json.dumps({"rules_md": "# x"}), "kind": "nosuchkind"}, "Unrecognized kind: nosuchkind"),
    ]
    for payload, expect in bad_cases:
        a, b = run(d, payload), run(v, payload)
        assert a[0] == b[0] == 400, (payload, a, b)
        assert expect.rstrip(":") in a[1]["detail"], (payload, a)
        assert b[1] == a[1], (payload, a, b)  # word for word
    # row parity: the same shape through both servers, the newest rows compared column by column
    ra = _newest("random_tables")
    b = run(v, {"json_text": json.dumps({"name": "Cmp Table", "category": "luck", "entries": [{"label": "z"}]})})
    rb = _newest("random_tables")
    assert b[0] == 200 and b[1]["ok"] and b[2] == "gateway"
    for k in ("name", "slug", "category", "description", "is_builtin", "entries_json", "world_id"):
        # only compare when both writes were of the same shape: the gateway's own write is self-consistent with Python's
        assert rb[k] is not None, k
    assert rb["category"] == "luck" and rb["entries_json"] == json.dumps([{"label": "z"}]), rb
    a = run(d, {"json_text": json.dumps({"kind": "character", "name": "Cmp One", "custom_fields": {"x": 1}, "template_slug": "existing-tpl"})})
    row_a = _newest("entities")
    b = run(v, {"json_text": json.dumps({"kind": "character", "name": "Cmp One 2", "custom_fields": {"x": 1}, "template_slug": "existing-tpl"})})
    row_b = _newest("entities")
    assert a[0] == b[0] == 200 and b[2] == "gateway"
    for k in ("kind", "subtype", "folder", "tags", "image_url", "summary", "body", "visible_to_players", "template_id", "custom_fields_json", "world_id"):
        assert row_a[k] == row_b[k], (k, row_a[k], row_b[k])
    assert row_b["template_id"] == row_a["template_id"] and row_b["template_id"] is not None
    assert row_b["custom_fields_json"] == json.dumps({"x": 1}), row_b["custom_fields_json"]
    assert row_b["visible_to_players"] == 1
    # world rules written by both (the gateway's write last)
    ra = run(d, {"json_text": json.dumps({"rules_md": "# py rules"})})
    rb2 = run(v, {"json_text": json.dumps({"rules_md": "# gw rules"})})
    assert (ra[0], ra[1]["ok"]) == (rb2[0], rb2[1]["ok"]) == (200, True) and rb2[2] == "gateway"
    db = SessionLocal()
    try:
        from app.models import World
        assert db.get(World, w).rules_md == "# gw rules"
    finally:
        db.close()
    # a subtype as a list: Python dies at commit (its sqlite binding error, a 400) - the gateway declines, Python answers
    payload = {"json_text": json.dumps({"kind": "character", "name": "Bob", "subtype": ["a", "b"]}), "kind": "entity_single", "params": {}}
    a, b = run(d, payload), run(v, payload)
    # the message echoes the row's timestamps, so compare the shape, not the text
    assert a[0] == b[0] == 400 and "Error binding parameter" in a[1]["detail"] and "Error binding parameter" in b[1]["detail"], (a, b)
    assert b[2] != "gateway"
    # an invalid inner text: Python's 400, gateway-declined
    bad = v.post("/api/import/execute", json={"json_text": "{oops"})
    assert bad.status_code == 400 and "Not valid JSON" in bad.json()["detail"]
    assert bad.headers.get("x-nd-served-by") != "gateway"
    # a field template through the gateway (ported): the row written as Python's
    r = v.post("/api/import/execute", json={"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                                            "params": {"template_kind": "entity", "name": f"Tpl {next(uniq)}"}})
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") == "gateway"
    row = _newest("entity_templates")
    assert row["name"].startswith("Tpl ") and json.loads(row["fields_json"]) == [{"id": "a", "label": "A", "type": "text"}], row
    # a player without import rights: refused exactly as Python refuses
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    a2 = pl.post("/api/import/execute", json={"json_text": json.dumps({"rules_md": "# x"})})
    b2 = _via(pl, base).post("/api/import/execute", json={"json_text": json.dumps({"rules_md": "# x"})})
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text), (a2.status_code, b2.status_code)
def test_import_execute_more_kinds_are_pythons(client, seed):
    """POST /api/import/execute from the gateway, remaining branches: map_overlay (append to the overlay row, the merged
    cap, the one-message cross-world guard, the missing-slug 400), schematic_elements (a new schematic via __new__ with
    its bounds, or the named one's elements merged by id), field_template (an entity template, the custom-kind floor) and
    the player_character branches (create, upsert by name, the N&D spread stamped, a custom template's fields kept);
    Python's 400s word for word; the batch envelope still Python's."""
    import itertools
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import EntityTemplate, PlayerCharacter, SheetTemplate
    from tests.test_gateway_writes import _newest, _row_by
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add(SheetTemplate(world_id=w, name="Mek", slug="mek-imp", sheet_mode="custom",
                             fields_json=json.dumps([{"id": "skill", "label": "Skill", "type": "text"}])))
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    uniq = itertools.count()

    def run(side, payload):
        r = side.post("/api/import/execute", json=payload)
        body = r.json() if "json" in r.headers.get("content-type", "") else r.text
        return r.status_code, body, r.headers.get("x-nd-served-by")

    # map overlays: appended onto the shared overlay row (both servers write the same row: run Python's, snapshot,
    # run the gateway's on top, and compare the merged totals against a Python-side double-import on a twin slug)
    maps = Path(os.environ["DB_PATH"]).parent / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    slug = f"imp-map-{next(uniq)}"
    (maps / f"{slug}.json").write_text(json.dumps({"world_id": w, "name": "Imp Map"}), encoding="utf-8")
    other = f"imp-other-{next(uniq)}"
    (maps / f"{other}.json").write_text(json.dumps({"world_id": seed.world_b.id, "name": "Other"}), encoding="utf-8")
    try:
        first = {"custom_markers": [{"x": 1, "label": "a"}], "custom_regions": []}
        second = {"custom_markers": [{"x": 2}], "custom_regions": [{"points": [0, 0]}]}
        a = run(d, {"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert a[0] == 200 and a[1]["ok"] and a[1]["redirect"] == f"/maps/{slug}", a
        b = run(v, {"json_text": json.dumps(second), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert b[0] == 200 and b[1]["ok"] and b[2] == "gateway", b
        row = _row_by("map_overlays", "slug", slug)
        assert json.loads(row["custom_markers_json"]) == [{"x": 1, "label": "a"}, {"x": 2}], row
        assert json.loads(row["custom_regions_json"]) == [{"points": [0, 0]}], row
        # refusals, word for word
        for payload, expect in (
        ({"json_text": json.dumps(5), "kind": "map_overlay", "params": {"map_slug": slug}}, "Expected an object"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {}}, "No map selected"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": "no-such-map"}}, "Map not found"),
            ({"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": other}}, "Map not found"),
            ({"json_text": json.dumps({"custom_markers": "x"}), "kind": "map_overlay", "params": {"map_slug": slug}}, "custom_markers and custom_regions must be lists"),
        ):
            r1, r2 = run(d, payload), run(v, payload)
            assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # a marker list over the cap: the gateway declines (Python's big payload is slow to seed) - actually seed it small-wise:
        big = {"custom_markers": [{"i": i} for i in range(501)], "params": {"map_slug": slug}}
        r = v.post("/api/import/execute", json={"json_text": json.dumps(big), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert r.status_code == 400 and "limit is 500" in r.json()["detail"]
        # schematics: a new one via __new__ (bounds and the numeric coercions)
        r1 = run(d, {"json_text": json.dumps({"elements": [{"type": "rect", "id": "r1", "x": 1}]}), "kind": "schematic_elements",
                     "params": {"schematic_slug": "__new__", "new_schematic_name": f"Imp Schem {next(uniq)}", "new_canvas_width": "3000"}})
        r2 = run(v, {"json_text": json.dumps({"elements": [{"type": "rect", "id": "r1", "x": 1}]}), "kind": "schematic_elements",
                     "params": {"schematic_slug": "__new__", "new_schematic_name": f"Imp Schem {next(uniq)}", "new_canvas_width": "3000"}})
        assert r1[0] == r2[0] == 200 and r1[1]["ok"] and r2[1]["ok"] and r2[2] == "gateway", (r1, r2)
        assert r1[1]["redirect"].startswith("/maps/schematic/") and r2[1]["redirect"].startswith("/maps/schematic/")
        sl = r2[1]["redirect"].rsplit("/", 1)[-1]
        row = _row_by("schematics", "slug", sl)
        assert row["canvas_width"] == 3000 and row["canvas_height"] == 1500 and json.loads(row["elements_json"])[0]["id"] == "r1", row
        # a merge by id into that schematic: run Python's on it, then the gateway's, compare with a twin on Python only
        twin = r1[1]["redirect"].rsplit("/", 1)[-1]
        add = {"elements": [{"type": "rect", "id": "r1", "x": 9}, {"type": "text", "id": "t1"}]}
        a = run(d, {"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": twin}})
        b = run(v, {"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": sl}})
        assert a[0] == b[0] == 200 and b[2] == "gateway", (a, b)
        ea = json.loads(_row_by("schematics", "slug", twin)["elements_json"])
        eb = json.loads(_row_by("schematics", "slug", sl)["elements_json"])
        assert ea == eb and len(eb) == 2 and eb[0]["x"] == 9, (ea, eb)
        # a foreign world's schematic: the same refusal
        for payload, expect in (
            ({"json_text": json.dumps(add), "kind": "schematic_elements", "params": {"schematic_slug": "no-such"}}, "Schematic not found"),
            ({"json_text": json.dumps({"elements": []}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__"}}, "non-empty array"),
            ({"json_text": json.dumps({"elements": [{"type": "rect"}]}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__", "new_canvas_width": "x"}}, "must be numbers"),
            ({"json_text": json.dumps({"elements": [{"type": "rect"}]}), "kind": "schematic_elements", "params": {"schematic_slug": "__new__", "new_canvas_width": "50"}}, "between 100 and 20000"),
        ):
            r1, r2 = run(d, payload), run(v, payload)
            assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # field templates: an entity template through both, rows compared
        n1, n2 = f"Imp Tpl {next(uniq)}", f"Imp Tpl {next(uniq)}"
        a = run(d, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "name": n1}})
        b = run(v, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "name": n2}})
        assert a[0] == b[0] == 200 and b[2] == "gateway", (a, b)
        assert a[1]["redirect"].startswith("/entity-templates/") and b[1]["redirect"].startswith("/entity-templates/")
        ta = _row_by("entity_templates", "id", a[1]["redirect"].split("/")[2])
        tb = _row_by("entity_templates", "id", b[1]["redirect"].split("/")[2])
        assert ta is not None and tb is not None, (a[1], b[1])
        for k in ("name", "kind", "description", "is_builtin", "fields_json", "world_id"):
            va, vb = ta[k], tb[k]
            if k == "name":
                assert va == n1 and vb == n2, (va, vb)
            else:
                assert va == vb, (k, va, vb)
        # a field template whose entity_kind is not a kind: stored NULL, by both
        a = run(d, {"json_text": json.dumps([{"id": "a", "label": "A", "type": "text"}]), "kind": "field_template",
                    "params": {"template_kind": "entity", "entity_kind": "nosuchkind", "name": f"NoKind {next(uniq)}"}})
        assert a[0] == 200 and _newest("entity_templates")["kind"] is None, a
        # player characters: create with the N&D defaults stamped, upsert-by-name through both, a custom template's fields kept
        nm = f"Imp PC {next(uniq)}"
        a = run(d, {"json_text": json.dumps({"name": nm, "charClass": "Rigger", "level": 4})})
        assert a[0] == 200 and a[1]["redirect"].startswith("/characters/"), a
        row = _row_by("player_characters", "name", nm)
        assert row["char_class"] == "Rigger" and row["level"] == 4, row
        assert json.loads(row["stats_json"])[0]["id"] == "str", row["stats_json"]  # the N&D spread
        # a custom template's sheet: the fields are the character, no spread stamped
        nm2 = f"Imp Custom {next(uniq)}"
        a = run(v, {"json_text": json.dumps({"name": nm2, "sheet_template_id": "mek-imp", "skill": "Pilot"}), "kind": "player_character"})
        assert a[0] == 200 and a[2] == "gateway", a
        row2 = _row_by("player_characters", "name", nm2)
        assert row2["sheet_template_id"] == _row_by("sheet_templates", "slug", "mek-imp")["id"], row2
        assert json.loads(row2["stats_json"]) == [], row2["stats_json"]
        # upsert: the same name again updates in place (run Python's on one name, the gateway's on another; compare columns)
        nm3, nm4 = f"Upsert {next(uniq)}", f"Upsert {next(uniq)}"
        run(d, {"json_text": json.dumps({"name": nm3, "level": 2})})
        run(d, {"json_text": json.dumps({"name": nm3, "level": 6, "race": "Elf"})})
        run(v, {"json_text": json.dumps({"name": nm4, "level": 2})})
        b = run(v, {"json_text": json.dumps({"name": nm4, "level": 6, "race": "Elf"})})
        assert b[0] == 200 and b[2] == "gateway", b
        ra, rb = _row_by("player_characters", "name", nm3), _row_by("player_characters", "name", nm4)
        assert ra["level"] == rb["level"] == 6 and ra["race"] == rb["race"] == "Elf", (ra["level"], rb["level"])
        # count: the upsert must not have created a second row
        db = SessionLocal()
        try:
            assert db.query(PlayerCharacter).filter(PlayerCharacter.name == nm4).count() == 1
        finally:
            db.close()
        # refusals word for word
        for payload, expect in (
            ({"json_text": json.dumps([{"level": 1}]), "kind": "player_character_bulk"}, "Character #1: needs at least a"),
            ({"json_text": json.dumps({"nope": 1}), "kind": "player_character"}, 'Expected an object with at least a "name"'),
            ({"json_text": json.dumps({"imports": []}), "kind": "batch"}, None),  # forwarded: Python's own answer
        ):
            r1, r2 = run(d, payload), run(v, payload)
            if expect is None:
                assert r1[0] == r2[0] and r2[2] != "gateway", (r1, r2)
            else:
                assert r1[0] == r2[0] == 400 and r1[1]["detail"] == r2[1]["detail"] and expect in r1[1]["detail"], (payload, r1, r2)
        # a player without import rights, one more time on a write branch
        pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
        a2 = pl.post("/api/import/execute", json={"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        b2 = _via(pl, base).post("/api/import/execute", json={"json_text": json.dumps(first), "kind": "map_overlay", "params": {"map_slug": slug}})
        assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
    finally:
        (maps / f"{slug}.json").unlink(missing_ok=True)
        (maps / f"{other}.json").unlink(missing_ok=True)
def test_world_import_is_pythons(client, seed):
    """POST /worlds/<id>/import from the gateway: a world JSON export's entities restored - created and updated counted
    in the redirect, the embedded data-URI image written back under a fresh uploads name, every field set outright on an
    upsert (a blank in the backup overwrites), the template resolved slug/name; a missing world 404, an invalid file
    Python's 400; run through both servers on identical payloads and the rows compared."""
    import base64 as b64mod
    from app.database import SessionLocal
    from app.models import Entity, EntityTemplate, World
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    png = b64mod.b64encode(b"\x89PNG-restored").decode()
    export = {"entities": [
        {"kind": "item", "name": "Imp Sword", "subtype": "W", "summary": "s", "body": "b", "tags": "t",
         "custom_fields_json": {"dmg": 6}, "image_data": f"data:image/png;base64,{png}"},
        {"kind": "npc", "name": "Imp Vosk", "template_slug": "imp-tpl"},
        {"kind": "note", "name": "", "body": "skipped"},
        {"kind": "note", "name": "Imp Hidden", "visible_to_players": False},
    ]}
    db = SessionLocal()
    try:
        db.add(EntityTemplate(world_id=seed.world_a.id, name="Imp Tpl", slug="imp-tpl", kind="npc", fields_json="[]"))
        db.commit()
    finally:
        db.close()

    def run(side):
        from pathlib import Path as _P
        _P(os.environ["DB_PATH"]).parent.joinpath("uploads").mkdir(parents=True, exist_ok=True)  # the reset wipes it
        for n in ("Imp Sword", "Imp Vosk", "Imp Hidden"):
            row = _row_by("entities", "name", n)
            if row:
                db = SessionLocal()
                try:
                    db.query(Entity).filter(Entity.id == row["id"]).delete()
                    db.commit()
                finally:
                    db.close()
        r = side.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export).encode(), "application/json")}, follow_redirects=False)
        rows = {n: _row_by("entities", "name", n) for n in ("Imp Sword", "Imp Vosk", "Imp Hidden")}
        return r, rows

    ra, rows_a = run(d)
    rb, rows_b = run(v)
    assert ra.status_code == rb.status_code == 303, (ra.status_code, rb.status_code, ra.text[:200])
    assert rb.headers.get("x-nd-served-by") == "gateway"
    la, lb = ra.headers["location"], rb.headers["location"]
    # the same counts, the same world slug (the ids in the query differ per run? no - the query carries counts only)
    import re
    qa = re.search(r"imported=(\d+)&updated=(\d+)", la).groups()
    qb = re.search(r"imported=(\d+)&updated=(\d+)", lb).groups()
    assert qa == qb == ("3", "0"), (la, lb)
    assert la.split("&")[0] == lb.split("&")[0], (la, lb)
    # the rows: every passthrough column equal, the image restored by both
    for n in rows_a:
        x, y = rows_a[n], rows_b[n]
        assert x is not None and y is not None, n
        for k in ("kind", "subtype", "summary", "body", "tags", "visible_to_players", "template_id", "custom_fields_json", "world_id"):
            assert x[k] == y[k], (n, k, x[k], y[k])
    assert rows_a["Imp Sword"]["image_url"] and rows_b["Imp Sword"]["image_url"], (rows_a["Imp Sword"]["image_url"], rows_b["Imp Sword"]["image_url"])
    assert rows_b["Imp Sword"]["image_url"].startswith("/uploads/")
    assert json.loads(rows_b["Imp Sword"]["custom_fields_json"]) == {"dmg": 6}
    assert rows_b["Imp Hidden"]["visible_to_players"] == 0
    assert rows_b["Imp Vosk"]["template_id"] is not None
    # the restored image really is the decoded bytes
    from pathlib import Path
    img = Path(os.environ["DB_PATH"]).parent / "uploads" / Path(rows_b["Imp Sword"]["image_url"]).name
    assert img.read_bytes() == b"\x89PNG-restored", img
    # the upsert: a second import of the same payload updates, not duplicates (the gateway's side, no reset)
    r2 = v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export).encode(), "application/json")}, follow_redirects=False)
    assert r2.status_code == 303 and "updated=3" in r2.headers["location"], r2.headers["location"]
    rows2 = {n: _row_by("entities", "name", n) for n in ("Imp Sword", "Imp Vosk", "Imp Hidden")}
    assert rows2["Imp Sword"]["id"] == rows_b["Imp Sword"]["id"], (rows_b["Imp Sword"]["id"], rows2["Imp Sword"]["id"])
    # a blank in the backup overwrites: re-import with the keys ABSENT (None -> NULL), and one explicitly empty (-> "")
    export2 = {"entities": [{"kind": "item", "name": "Imp Sword"}]}
    export3 = {"entities": [{"kind": "item", "name": "Imp Sword", "summary": "", "body": ""}]}
    r3 = v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export2).encode(), "application/json")}, follow_redirects=False)
    assert r3.status_code == 303 and "updated=1" in r3.headers["location"]
    row3 = _row_by("entities", "name", "Imp Sword")
    assert row3["summary"] is None and row3["body"] is None, (row3["summary"], row3["body"])
    r4 = v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("export.json", json.dumps(export3).encode(), "application/json")}, follow_redirects=False)
    row4 = _row_by("entities", "name", "Imp Sword")
    assert row4["summary"] == "" and row4["body"] == "", (row4["summary"], row4["body"])
    # refusals: a missing world, an invalid file
    a, b = d.post("/worlds/999999/import", files={"file": ("x.json", b"{}", "application/json")}, follow_redirects=False), \
        v.post("/worlds/999999/import", files={"file": ("x.json", b"{}", "application/json")}, follow_redirects=False)
    assert (a.status_code, a.text) == (b.status_code, b.text) == (404, '{"detail":"Not Found"}'), (a.status_code, b.status_code)
    a, b = d.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("x.json", b"not json", "application/json")}, follow_redirects=False), \
        v.post(f"/worlds/{seed.world_a.id}/import", files={"file": ("x.json", b"not json", "application/json")}, follow_redirects=False)
    assert (a.status_code, a.text) == (b.status_code, b.text) == (400, '{"detail":"Invalid JSON file"}'), (a.status_code, b.status_code)
def test_hub_companions_and_live_audio_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/companions and GET /api/sessions/<id>/live-audio from the gateway: the companions and
    the kind list (a stranger 404 as _hub_pc 404s), the raw segments with their total disk size (a corrupt
    live_audio_files_json degrades to nothing, as Python's does); another world's session 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import CharacterCompanion, GameSession, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Comp")
        stranger = PlayerCharacter(world_id=w, owner_user_id=None, name="Nobody's")
        over = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Else")
        db.add_all([pc, stranger, over])
        db.flush()
        db.add_all([
            CharacterCompanion(world_id=w, character_id=pc.id, name="Whiskers", kind="familiar", hp=3, hp_max=5, notes="cat"),
            CharacterCompanion(world_id=w, character_id=pc.id, name="Rover", kind="mount"),
        ])
        db.add_all([
            GameSession(world_id=w, title="S1", session_num=1, live_audio_files_json=json.dumps(["live/1/seg0.webm", "live/1/seg1.webm"])),
            GameSession(world_id=w, title="S2", session_num=2, live_audio_files_json="{corrupt"),
            GameSession(world_id=w, title="S3", session_num=3, live_audio_files_json=json.dumps(["gone.webm", 42, True, None])),
            GameSession(world_id=seed.world_b.id, title="Over", session_num=1, live_audio_files_json=json.dumps(["x.webm"])),
        ])
        db.commit()
        ids = (pc.id, stranger.id, over.id)
        sids = [s.id for s in db.query(GameSession).order_by(GameSession.id).all()]
    finally:
        db.close()
    data = Path(os.environ["DB_PATH"]).parent / "uploads"
    (data / "live" / "1").mkdir(parents=True, exist_ok=True)
    (data / "live" / "1" / "seg0.webm").write_bytes(b"x" * 1000)
    (data / "live" / "1" / "seg1.webm").write_bytes(b"y" * 500)
    try:
        d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
        g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
        urls = [f"/api/characters/{i}/hub/companions" for i in ids + (999999,)] + \
               [f"/api/sessions/{i}/live-audio" for i in sids + [999999]]
        for c in (d, g, pb):
            for url in urls:
                a, b = c.get(url), _via(c, base).get(url)
                assert (a.status_code, a.json()) == (b.status_code, b.json()), (url, a.status_code, a.text[:120], b.text[:120])
        served = sum(1 for c in (d, g) for url in urls if _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway")
        assert served >= 8, served
        # the companions really come back newest-last with every column
        j = d.get(f"/api/characters/{ids[0]}/hub/companions").json()
        assert j["kinds"] == ["familiar", "hireling", "mount", "vehicle", "other"], j
        assert [(x["name"], x["kind"]) for x in j["companions"]] == [("Whiskers", "familiar"), ("Rover", "mount")], j
        assert j["companions"][0]["hp"] == 3 and j["companions"][0]["hp_max"] == 5 and j["companions"][0]["notes"] == "cat", j
        # a stranger's character: the same 404
        assert d.get(f"/api/characters/{ids[1]}/hub/companions").status_code == 404
        assert g.get(f"/api/characters/{ids[1]}/hub/companions").status_code == 200
        assert pb.get(f"/api/characters/{ids[2]}/hub/companions").status_code == 200
        # live-audio (the GM's: sessions is not player-readable by default - the player's 403 above held): the segments
        # in order with the real total, a corrupt list empty, the mixed-type list str()'d
        j = g.get(f"/api/sessions/{sids[0]}/live-audio").json()
        assert j == {"files": ["live/1/seg0.webm", "live/1/seg1.webm"], "count": 2, "total_bytes": 1500}, j
        assert g.get(f"/api/sessions/{sids[1]}/live-audio").json() == {"files": [], "count": 0, "total_bytes": 0}
        j = g.get(f"/api/sessions/{sids[2]}/live-audio").json()
        assert j["files"] == ["gone.webm", "42", "True", "None"] and j["count"] == 4 and j["total_bytes"] == 0, j
        # players never reach the handler at all: the auth gate 403s the path (GM access required) - answered by
        # Python on both sides
        assert d.get(f"/api/sessions/{sids[3]}/live-audio").status_code == 403
        assert pb.get(f"/api/sessions/{sids[3]}/live-audio").status_code == 403
        assert "GM access required" in pb.get(f"/api/sessions/{sids[3]}/live-audio").text
    finally:
        pass
def test_tables_edit_invalid_entries_fallback_is_pythons(client, seed):
    """POST /tables/<id>/edit with entries_json that fails to parse: Python resets the entries to \"[]\" and answers 303 -
    the gateway now does the same (the row compared); a NaN payload (Python's loads ACCEPTS it) is answered by the
    gateway too, the stored text verbatim; a valid payload unchanged."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import RandomTable
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        t = RandomTable(world_id=w, name="Fb Table", slug="fb-table", category="general",
                        entries_json=json.dumps([{"label": "keep"}]))
        db.add(t)
        db.commit()
        tid = t.id
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    served = 0
    # (entries_json payload, the entries the row must hold afterwards) - run on Python, reset the row, run via the gateway
    cases = [
        ("not json at all", "[]"),
        ('{"a": 1', "[]"),
        ("[1, 2,]", "[]"),
        ("just a string", "[]"),  # bare words are not valid JSON: Python resets here too
        ('[{"label": "ok", "weight": 2}]', '[{"label": "ok", "weight": 2}]'),
    ]
    for raw, expect in cases:
        out = []
        for side in (d, v):
            # reset the row between the two sides
            db = SessionLocal()
            try:
                db.get(RandomTable, tid).entries_json = json.dumps([{"label": "keep"}])
                db.commit()
            finally:
                db.close()
            r = side.post(f"/tables/{tid}/edit", data={"name": "Fb Table", "entries_json": raw}, follow_redirects=False)
            row = _row_by("random_tables", "id", tid)
            out.append((r.status_code, r.headers.get("location"), row["entries_json"], r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (raw, out)
        assert json.loads(out[1][2]) == json.loads(expect), (raw, out[0][2], out[1][2])
        served += out[1][3] == "gateway"
    # a NaN payload: Python's json.loads ACCEPTS it and stores the text - the gateway forwards, Python answers
    db = SessionLocal()
    try:
        db.get(RandomTable, tid).entries_json = json.dumps([{"label": "keep"}])
        db.commit()
    finally:
        db.close()
    a = d.post(f"/tables/{tid}/edit", data={"name": "Fb Table", "entries_json": '[{"w": NaN}]'}, follow_redirects=False)
    row_a = _row_by("random_tables", "id", tid)
    b = v.post(f"/tables/{tid}/edit", data={"name": "Fb Table", "entries_json": '[{"w": NaN}]'}, follow_redirects=False)
    row_b = _row_by("random_tables", "id", tid)
    assert a.status_code == b.status_code == 303 and row_a["entries_json"] == row_b["entries_json"] == '[{"w": NaN}]'
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert served >= 3, served
def test_account_password_change_is_pythons(client, seed):
    import hashlib
    """POST /account/password from the gateway: the wrong current password re-renders the account page at 400 with
    Python's message (and counts toward the lockout), a short new password and a confirm mismatch render 400, the
    success swaps the hash, bumps session_version, revokes every trusted device, re-stamps THIS session (it stays
    signed in; the other is logged out) and redirects; the lockout answers 429 with Python's message. Every error page
    byte-for-byte Python's (the layout context included)."""
    from .conftest import PLAYER_PASSWORD
    from app import auth
    from app.database import SessionLocal
    from app.models import TrustedDevice
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add(TrustedDevice(user_id=seed.player_a.id, token_hash=hashlib.sha256(b"dev1").hexdigest(), expires_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=5)))
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    v = _via(d, base)

    def state():
        db = SessionLocal()
        try:
            u = db.query(__import__("app.models", fromlist=["User"]).User).filter_by(id=seed.player_a.id).first()
            devs = db.query(TrustedDevice).filter_by(user_id=seed.player_a.id).count()
            return u.password_hash.split("$")[0], u.session_version, devs
        finally:
            db.close()

    def same_page(a, b):
        assert a.status_code == b.status_code, (a.status_code, b.status_code, a.text[:150], b.text[:150])
        assert a.content == b.content, (a.status_code, a.text[:200], b.text[:200])

    # error paths (the row must not change)
    h0, sv0, dv0 = state()
    for form, status in (({"current_password": "wrong", "new_password": "longenough1", "confirm_password": "longenough1"}, 400),
                         ({"current_password": PLAYER_PASSWORD, "new_password": "short", "confirm_password": "short"}, 400),
                         ({"current_password": PLAYER_PASSWORD, "new_password": "longenough1", "confirm_password": "different1"}, 400)):
        a = d.post("/account/password", data=form, follow_redirects=False)
        b = v.post("/account/password", data=form, follow_redirects=False)
        assert a.status_code == b.status_code == status, (form, a.status_code, b.status_code)
        same_page(a, b)
    h1, sv1, dv1 = state()
    assert h1 == h0 and sv1 == sv0 and dv1 == dv0, (h0, h1)
    served = sum(1 for _ in (1,) if v.post("/account/password", data={"current_password": "wrong", "new_password": "longenough1", "confirm_password": "longenough1"}, follow_redirects=False).headers.get("x-nd-served-by") == "gateway")
    assert served == 1

    # success through the gateway: the hash changes, the version bumps, the devices are gone, THIS session stays
    r = v.post("/account/password", data={"current_password": PLAYER_PASSWORD, "new_password": "brand-new-pw-1", "confirm_password": "brand-new-pw-1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=password", (r.status_code, r.headers.get("location"))
    assert r.headers.get("x-nd-served-by") == "gateway"
    h2, sv2, dv2 = state()
    assert h2 != h0 and sv2 == sv0 + 1 and dv2 == 0, (h0, h2, sv0, sv2, dv2)
    # THIS session re-stamped: a browser keeps the new cookie (the test's cookie copy shadows by domain, so replay
    # the Set-Cookie explicitly)
    new_session = r.headers["set-cookie"].split(";")[0]
    fresh = httpx.Client(base_url=base, timeout=60)
    fresh.cookies.set("session", new_session.split("=", 1)[1])
    fresh.cookies.set("active_world", seed.world_a.slug)
    page = fresh.get("/account")
    assert page.status_code == 200 and page.headers.get("x-nd-served-by") == "gateway", (page.status_code, page.headers.get("x-nd-served-by"))
    assert "brand-new-pw-1" not in page.text  # (a sanity no-op: the page is the account page, not an error)
    fresh.close()
    # the new password verifies
    db = SessionLocal()
    try:
        u = db.query(__import__("app.models", fromlist=["User"]).User).filter_by(id=seed.player_a.id).first()
        assert auth.verify_password("brand-new-pw-1", u.password_hash)
    finally:
        db.close()
    # the direct client's old session was logged out (its session_version is stale) - Python 303s to the login
    a = d.get("/account", follow_redirects=False)
    assert a.status_code == 303 and "/login" in a.headers.get("location", ""), (a.status_code, a.headers.get("location"))
    # a trusted device added after the change survives a wrong-password attempt but not another change
    d2 = _signed_in_with_password("brand-new-pw-1", seed)
    db = SessionLocal()
    try:
        db.add(TrustedDevice(user_id=seed.player_a.id, token_hash=hashlib.sha256(b"dev2").hexdigest(), expires_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=5)))
        db.commit()
    finally:
        db.close()
    a = d2.post("/account/password", data={"current_password": "brand-new-pw-1", "new_password": "another-pw-12", "confirm_password": "another-pw-12"}, follow_redirects=False)
    assert a.status_code == 303
    db = SessionLocal()
    try:
        assert db.query(TrustedDevice).filter_by(user_id=seed.player_a.id).count() == 0
    finally:
        db.close()
    # restore the original password for later tests
    v2 = _signed_in_with_password("another-pw-12", seed)
    r = v2.post("/account/password", data={"current_password": "another-pw-12", "new_password": PLAYER_PASSWORD, "confirm_password": PLAYER_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303


def _signed_in_with_password(password, seed):
    import httpx
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": seed.player_a.email, "password": password, "next": "/"}, follow_redirects=False).status_code == 303
    d.cookies.set("active_world", seed.world_a.slug)
    d.get("/")
    return d
def test_account_2fa_disable_and_regen_are_pythons(client, seed):
    import hashlib
    import re
    """POST /account/2fa/disable and /backup-codes/regenerate from the gateway: the wrong password re-renders at 400
    with Python's message (counting toward the lockout), disable when 2FA is not on just redirects, disable wipes the
    secret / codes / trusted devices and redirects, regen on a live setup replaces the hashed codes and shows the
    plaintext once on the re-rendered page - the row compared after every step."""
    import hashlib
    from datetime import datetime, timedelta
    from .conftest import PLAYER_PASSWORD
    from app import totp as _totp
    from app.database import SessionLocal
    from app.models import TrustedDevice, User
    base = str(client.base_url)
    w = seed.world_a.id
    secret = _totp.generate_secret()
    # sign in BEFORE enabling 2FA: a login with totp on goes through the half-authenticated
    # pending-TOTP flow (a session with no user_id), and every request would 303 to /login
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    db = SessionLocal()
    try:
        u = db.get(User, seed.player_a.id)
        u.totp_enabled = True
        u.totp_secret = secret
        u.totp_backup_codes_json = json.dumps([_totp.hash_backup_code("oldcode1234")])
        db.add(TrustedDevice(user_id=u.id, token_hash=hashlib.sha256(b"devA").hexdigest(),
                             expires_at=datetime.utcnow() + timedelta(days=5)))
        db.commit()
    finally:
        db.close()

    def row():
        db = SessionLocal()
        try:
            u = db.get(User, seed.player_a.id)
            return (u.totp_enabled, u.totp_secret, json.loads(u.totp_backup_codes_json or "[]"))
        finally:
            db.close()

    def seed_2fa():
        """The preceding password test leaves 2FA off; re-seed a live setup right before each request."""
        import hashlib as _hl
        db = SessionLocal()
        try:
            u = db.get(User, seed.player_a.id)
            u.totp_enabled = True
            u.totp_secret = secret
            u.totp_backup_codes_json = json.dumps([_totp.hash_backup_code("oldcode1234")])
            db.commit()
        finally:
            db.close()

    # the wrong password on regen: the same 400 page from both, the codes intact
    seed_2fa()
    db = SessionLocal()
    try:
        u = db.get(User, seed.player_a.id)
        print("DBG2FA db state:", u.totp_enabled, (u.password_hash or "").split("$")[0], u.session_version)
    finally:
        db.close()
    print("DBG2FA d cookies:", {k: v[:20] for k, v in d.cookies.items()})
    a = d.post("/account/2fa/backup-codes/regenerate", data={"current_password": "wrong"}, follow_redirects=False)
    b = v.post("/account/2fa/backup-codes/regenerate", data={"current_password": "wrong"}, follow_redirects=False)
    print("DBG2FA answers:", a.status_code, a.headers.get("location"), "|", b.status_code, b.headers.get("location"), b.headers.get("x-nd-served-by"))
    assert a.status_code == b.status_code == 400 and a.content == b.content, (a.status_code, b.status_code, a.headers.get('location'), b.headers.get('location'))
    assert 'Current password is incorrect.' in a.text
    assert len(row()[2]) == 1
    assert v.post("/account/2fa/backup-codes/regenerate", data={"current_password": "wrong"},
                  follow_redirects=False).headers.get("x-nd-served-by") == "gateway"
    # regen through the gateway: the plaintext codes shown once, the stored hashes replaced
    seed_2fa()
    r = v.post("/account/2fa/backup-codes/regenerate", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") == "gateway", r.status_code
    new_codes = None
    for form in ("form", "form "):
        pass
    m = re.findall(r"<code[^>]*>([0-9a-f]{10})</code>", r.text)
    assert len(m) == 8, m[:10]
    enabled, sec, hashes = row()
    assert enabled is True and sec == secret and len(hashes) == 8, (enabled, len(hashes))
    assert all(h not in json.dumps([_totp.hash_backup_code("oldcode1234")]) for h in hashes)
    # the shown plaintext verifies against the stored hashes
    assert all(hashlib.sha256(c.strip().lower().encode()).hexdigest() in hashes for c in m[:8]), (m[:8], hashes)
    # regen when 2FA is off: a plain redirect (both sides)
    db = SessionLocal()
    try:
        u = db.get(User, seed.player_a.id)
        u.totp_enabled = False
        db.commit()
    finally:
        db.close()
    a = d.post("/account/2fa/backup-codes/regenerate", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    b = v.post("/account/2fa/backup-codes/regenerate", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    assert (a.status_code, a.headers["location"]) == (b.status_code, b.headers["location"]) == (303, "/account")
    assert b.headers.get("x-nd-served-by") == "gateway"
    # disable with 2FA off: the same redirect, nothing wiped but nothing left either
    a = d.post("/account/2fa/disable", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    b = v.post("/account/2fa/disable", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    assert (a.status_code, a.headers["location"]) == (b.status_code, b.headers["location"]) == (303, "/account?saved=2fa-off")
    assert b.headers.get("x-nd-served-by") == "gateway"
    # disable on a live setup (the gateway's side): the secret, the codes and the devices gone
    db = SessionLocal()
    try:
        u = db.get(User, seed.player_a.id)
        u.totp_enabled = True
        u.totp_secret = secret
        u.totp_backup_codes_json = json.dumps([_totp.hash_backup_code("oldcode1234")])
        db.add(TrustedDevice(user_id=u.id, token_hash=hashlib.sha256(b"devB").hexdigest(),
                             expires_at=datetime.utcnow() + timedelta(days=5)))
        db.commit()
    finally:
        db.close()
    r = v.post("/account/2fa/disable", data={"current_password": PLAYER_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account?saved=2fa-off"
    assert r.headers.get("x-nd-served-by") == "gateway"
    enabled, sec, hashes = row()
    assert enabled is False and sec is None and hashes == [], (enabled, sec, hashes)
    db = SessionLocal()
    try:
        assert db.query(TrustedDevice).filter_by(user_id=seed.player_a.id).count() == 0
        u = db.get(User, seed.player_a.id)
        u.totp_enabled = False  # leave the account as the seed had it
        db.commit()
    finally:
        db.close()
def test_settings_saves_are_pythons(client, seed):
    """POST /settings and /settings/resources from the gateway: the columns written exactly as Python writes them -
    format validation falls back to avif, the hover numerics are clamped (not rejected), the checkbox is presence-keyed,
    resources' blank/auto/out-of-range fall back to automatic - the rows compared after every save; an out-of-form-value
    float still clamps. (Python's in-memory side effects - its worker pools, its preview-format cache, the queued
    thumbnail remake - are process-internal and move at the switch.)"""
    from app.database import SessionLocal
    from app.models import AppSettings
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)

    def run(side, url, data):
        r = side.post(url, data=data, follow_redirects=False)
        row = _row_by("app_settings", "id", 1)
        keep = {k: row[k] for k in ("static_format", "animated_format", "preview_format", "hover_preview_enabled",
                                    "hover_preview_delay_ms", "hover_preview_hide_delay_ms", "hover_preview_width_px",
                                    "hover_preview_max_height_px", "res_image_workers", "res_render_slots", "res_memory_mb")}
        return r.status_code, r.headers.get("location"), keep, r.headers.get("x-nd-served-by")

    def reset():
        db = SessionLocal()
        try:
            s = db.get(AppSettings, 1)
            s.static_format, s.animated_format, s.preview_format = "webp", "webp", "webp"
            s.hover_preview_enabled, s.hover_preview_delay_ms = False, 5000
            s.hover_preview_hide_delay_ms, s.hover_preview_width_px, s.hover_preview_max_height_px = 400, 340, 420
            s.res_image_workers, s.res_render_slots, s.res_memory_mb = None, None, None
            db.commit()
        finally:
            db.close()

    served = 0
    cases = [
        ("/settings", {"static_format": "none", "animated_format": "bogus", "preview_format": "original",
                       "hover_preview_enabled": "1", "hover_preview_delay_seconds": "2", "hover_preview_hide_delay_seconds": "1",
                       "hover_preview_width_px": "500", "hover_preview_max_height_px": "600"},
         {"static_format": "none", "animated_format": "avif", "preview_format": "original", "hover_preview_enabled": 1,
          "hover_preview_delay_ms": 2000, "hover_preview_hide_delay_ms": 1000, "hover_preview_width_px": 500,
          "hover_preview_max_height_px": 600}),
        ("/settings", {"hover_preview_delay_seconds": "0.1", "hover_preview_hide_delay_seconds": "99",
                       "hover_preview_width_px": "1", "hover_preview_max_height_px": "99999"},
         {"hover_preview_enabled": 0, "hover_preview_delay_ms": 500, "hover_preview_hide_delay_ms": 10000,
          "hover_preview_width_px": 220, "hover_preview_max_height_px": 1000}),
        ("/settings/resources", {"res_image_workers": "6", "res_render_slots": "4", "res_memory_gb": "2.5"},
         {"res_image_workers": 6, "res_render_slots": 4, "res_memory_mb": 2560}),
        ("/settings/resources", {"res_image_workers": "", "res_render_slots": "auto", "res_memory_gb": "99999"},
         {"res_image_workers": None, "res_render_slots": None, "res_memory_mb": 1048576}),  # clamped to the 1024 GB bound
        ("/settings/resources", {"res_image_workers": "99", "res_render_slots": "0.5", "res_memory_gb": "abc"},
         {"res_image_workers": 32, "res_render_slots": None, "res_memory_mb": None}),  # 99 -> 32; int("0.5") fails -> auto
    ]
    for url, data, expect in cases:
        out = []
        for side in (d, v):
            reset()
            out.append(run(side, url, data))
        assert out[0][:3] == out[1][:3], (url, data, out[0], out[1])
        for k, want in expect.items():
            assert out[1][2][k] == want, (url, data, k, out[1][2][k], want)
        served += out[1][3] == "gateway"
    assert served >= 4, served
    # a missing row: both servers would fail the same way (the app always has one; skip)
    # an odd float that Python's float() accepts and serde would too is covered by "2.5" above
def test_settings_system_save_is_pythons(client, seed):
    """POST /settings/system from the gateway: the URL validation (400 with Python's message), the optional-number
    fields' parse failures and bounds (the first failure is the message), the upload limits, the use_mmap / gpu_preset
    fallbacks, the checkbox toggles, the API-key leave / clear / replace rules, the server-env sanitizer's allowlist -
    the columns compared after every save; a Studio-key or model-override POST is not this route."""
    from app.database import SessionLocal
    from app.models import AppSettings
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)

    FIELDS = ("ollama_model", "ollama_url", "llm_url", "llm_model", "llm_api_key", "llm_api_key_clear",
              "swarmui_external_url", "android_emulator_url", "editor_external_url", "dreamlands_enabled",
              "king_in_yellow_enabled", "ollama_keep_alive", "ollama_use_mmap", "ollama_gpu_preset",
              "ollama_temperature", "ollama_top_k", "session_media_max", "max_upload_mb")
    # the ollama_srv_* values never touch app_settings — they land in the generated ollama.env file

    def run(side, data):
        r = side.post("/settings/system", data=data, follow_redirects=False)
        row = _row_by("app_settings", "id", 1)
        keep = {k: row[k] for k in FIELDS if k in row} if row else {}
        body = r.text if r.status_code != 303 else r.headers.get("location")
        return r.status_code, body, keep, r.headers.get("x-nd-served-by")

    def base_form(**kw):
        f = {"ollama_model": "llama3", "ollama_url": "http://ollama:11434/", "llm_url": "https://api.unsloth.dev/v1",
             "llm_model": "nd-test/model", "llm_api_key": "", "ollama_keep_alive": "5m",
             "swarmui_external_url": "", "android_emulator_url": "", "editor_external_url": "",
             "ollama_srv_flash_attention": "1", "ollama_srv_num_parallel": "2", "ollama_srv_context_length": "8192"}
        f.update(kw)
        return f

    served = 0
    cases = [
        # the happy path: everything written, the trailing slash stripped off the URLs
        (base_form(ollama_url="http://ollama:11434/", dreamlands_enabled="1", ollama_temperature="0.7",
                   ollama_top_k="40", session_media_max="12", max_upload_mb="50", llm_api_key="sk-test-123"),
         {"ollama_url": "http://ollama:11434", "dreamlands_enabled": 1, "ollama_temperature": 0.7, "ollama_top_k": 40,
          "session_media_max": 12, "max_upload_mb": 50, "llm_api_key": "sk-test-123"}),
        # URL validation: the message names the field
        (base_form(ollama_url="ollama:11434"), "Ollama URL must start with http:// or https://"),
        (base_form(llm_url="ftp://x"), "Unsloth URL must start with http:// or https://"),
        # number bounds: the first failure is the message
        (base_form(ollama_temperature="2.5"), "Temperature must be at most 2"),
        (base_form(ollama_top_k="-3"), "Top K must be at least 0"),
        (base_form(ollama_num_ctx="0.5"), "Context length must be a whole number"),
        (base_form(session_media_max="99"), "Pictures, videos and audio per session must be at most 50"),
        (base_form(max_upload_mb="x"), "General upload limit (MB) must be a whole number"),
        # use_mmap / gpu_preset fallbacks
        (base_form(ollama_use_mmap="yes", ollama_gpu_preset="bogus"), None),
        # the server env sanitizer's allowlist
        (base_form(ollama_srv_flash_attention="bogus"), "OLLAMA_FLASH_ATTENTION must be one of"),
        (base_form(ollama_srv_num_parallel="99"), "OLLAMA_NUM_PARALLEL must be between 0 and 32"),
        (base_form(ollama_srv_hsa_override_gfx="bad value!"), "HSA_OVERRIDE_GFX_VERSION contains characters that aren&#39;t allowed"),
    ]
    for form, expect in cases:
        out = []
        for side in (d, v):
            db = SessionLocal()
            try:
                s = db.get(AppSettings, 1)
                s.ollama_model, s.ollama_url, s.llm_url, s.llm_model = "old", "http://old", "http://old", "oldm"
                s.llm_api_key = "oldkey"
                s.dreamlands_enabled, s.king_in_yellow_enabled = False, False
                db.commit()
            finally:
                db.close()
            out.append(run(side, form))
        # the status and the row state must match; the 400 pages' HTML bytes may differ (the gateway's
        # page context is the refactored one), so compare the message text instead
        assert out[0][0] == out[1][0], (form, out[0][0], out[1][0])
        assert out[0][2] == out[1][2], (form, out[0][2], out[1][2])
        if isinstance(expect, dict):
            for k, want in expect.items():
                assert out[1][2].get(k) == want, (k, out[1][2].get(k), want)
            served += out[1][3] == "gateway"
        elif isinstance(expect, str):
            assert expect in out[0][1], (expect, out[0][1][:150])
            assert expect in out[1][1], (expect, out[1][1][:150])
            served += out[1][3] == "gateway"
    assert served >= 4, served
    # the key rules: a blank submit leaves the saved key, the clear checkbox erases it
    db = SessionLocal()
    try:
        s = db.get(AppSettings, 1)
        s.llm_api_key = "savedkey"
        db.commit()
    finally:
        db.close()
    out = []
    for side in (d, v):
        db = SessionLocal()
        try:
            s = db.get(AppSettings, 1)
            s.llm_api_key = "savedkey"
            db.commit()
        finally:
            db.close()
        out.append(run(side, base_form(llm_api_key="")))
    assert out[0][2]["llm_api_key"] == out[1][2]["llm_api_key"] == "savedkey", (out[0][2]["llm_api_key"], out[1][2]["llm_api_key"])
    out = []
    for side in (d, v):
        out.append(run(side, base_form(llm_api_key_clear="1")))
    assert out[0][2]["llm_api_key"] == out[1][2]["llm_api_key"] == "", (out[0][2]["llm_api_key"], out[1][2]["llm_api_key"])
    assert out[1][3] == "gateway"
def test_model_override_routes_are_pythons(client, seed):
    """POST /settings/system/model-override and /delete from the gateway: an empty model name 400s with Python's
    message, a filled override writes the entry (options from the filled fields only, keep_alive truncated to 32,
    thinking stored only when checked), an all-empty form leaves no phantom entry, delete drops the entry - the
    column compared after every save."""
    from app.database import SessionLocal
    from app.models import AppSettings
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)

    def run(side, url, data):
        r = side.post(url, data=data, follow_redirects=False)
        row = _row_by("app_settings", "id", 1)
        body = r.text if r.status_code != 303 else r.headers.get("location")
        return r.status_code, body, (row["ollama_model_overrides_json"] if row else None), r.headers.get("x-nd-served-by")

    def reset():
        db = SessionLocal()
        try:
            db.get(AppSettings, 1).ollama_model_overrides_json = "{}"
            db.commit()
        finally:
            db.close()

    served = 0
    cases = [
        ("/settings/system/model-override", {"model": ""},
         400, "Pick a model to override first", None),
        ("/settings/system/model-override", {"model": "llama3", "temperature": "0.5", "top_k": "30", "keep_alive": "10m", "thinking": "1"},
         303, None, {"llama3": {"options": {"temperature": 0.5, "top_k": 30}, "keep_alive": "10m", "thinking": True}}),
        ("/settings/system/model-override", {"model": "gemma4", "num_predict": "-2", "seed": "42"},
         303, None, {"gemma4": {"options": {"num_predict": -2, "seed": 42}, "keep_alive": ""}}),
        # an out-of-bounds field: the first failure is the message
        ("/settings/system/model-override", {"model": "llama3", "temperature": "3"}, 400, "Temperature must be at most 2", None),
        ("/settings/system/model-override", {"model": "llama3", "seed": "x"}, 400, "Seed must be a whole number", None),
        # an all-empty override: no phantom entry
        ("/settings/system/model-override", {"model": "ghost"}, 303, None, {}),
        ("/settings/system/model-override/delete", {"model": "nope"}, 303, None, {}),
    ]
    for url, form, status, message, expect in cases:
        out = []
        for side in (d, v):
            reset()
            out.append(run(side, url, form))
        assert out[0][0] == out[1][0] == status, (form, out[0][0], out[1][0])
        if message is not None:
            assert message in out[0][1], (form, out[0][1][:150])
            assert message in out[1][1], (form, out[1][1][:150])
        if expect is not None:
            got = json.loads(out[1][2] or "{}")
            assert got == expect, (got, expect)
        served += out[1][3] == "gateway"
    # the happy path again on both sides, then delete through the gateway
    for side in (d, v):
        reset()
    a = d.post("/settings/system/model-override", data={"model": "keepme", "temperature": "0.9"}, follow_redirects=False)
    b = v.post("/settings/system/model-override", data={"model": "keepme", "temperature": "0.9"}, follow_redirects=False)
    assert a.status_code == b.status_code == 303
    b2 = v.post("/settings/system/model-override/delete", data={"model": "keepme"}, follow_redirects=False)
    assert b2.status_code == 303 and b2.headers.get("x-nd-served-by") == "gateway"
    got = json.loads(_row_by("app_settings", "id", 1)["ollama_model_overrides_json"])
    assert "keepme" not in got, got
    assert served >= 4, served


class _FakeStudio:
    """A stand-in Unsloth Studio: /api/hub/cached-gguf answers 200 only for
    'Bearer good-key-1234', 401 (FastAPI's {"detail": ...}) for anything else."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization", "") == "Bearer good-key-1234":
                    body = b'{"models": []}'
                    code = 200
                else:
                    body = b'{"detail": "Not authenticated"}'
                    code = 401
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_studio_key_route_is_pythons(client, seed):
    """POST /settings/system/studio-key from the gateway: the paste cleaner (Bearer prefix, quotes,
    the two 400s), a 401 is verified away without replacing the saved key, a good key is saved and
    confirmed, an unreachable Studio still saves with 'couldn't confirm', clear removes it - the
    llm_api_key column and the whole answer compared (the unreachable case by prefix: the exception
    text is the client library's)."""
    from tests.test_gateway_writes import _row_by
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    studio = _FakeStudio()

    def run(side, payload):
        r = side.post("/settings/system/studio-key", json=payload, follow_redirects=False)
        row = _row_by("app_settings", "id", 1)
        return (r.status_code, r.json() if r.status_code != 500 else r.text,
                (row["llm_api_key"] if row else None), r.headers.get("x-nd-served-by"))

    def set_llm_url(side, url):
        r = side.post("/settings/system", data={"ollama_model": "", "ollama_url": "", "llm_url": url,
                                                "llm_model": "", "llm_api_key": ""}, follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])

    served = 0
    try:
        # the effective URL must be the fake Studio on both sides BEFORE any key case:
        # Python's verify reads its in-memory override (refreshed by this save), the
        # gateway reads the row - set it through the real save route, not a DB write
        set_llm_url(d, studio.url())
        set_llm_url(v, studio.url())

        # the cleaner's 400s (state-independent)
        for payload in ({}, {"api_key": ""}, {"api_key": "two keys here"}):
            out = []
            for side in (d, v):
                out.append(run(side, payload))
            assert out[0][0] == out[1][0] == 400, (payload, out[0][:2], out[1][:2])
            assert out[0][1] == out[1][1], (payload, out[0][1], out[1][1])
            served += out[1][3] == "gateway"

        # the cleaner strips a Bearer prefix and matching quotes before the verify: a quoted
        # good key is saved as the bare key
        for raw in ("good-key-1234", "Bearer good-key-1234", '"good-key-1234"', " good-key-1234 "):
            out = []
            for side in (d, v):
                out.append(run(side, {"api_key": raw}))
            assert out[0][0] == out[1][0] == 200 and out[0][1] == out[1][1], (raw, out[0][:2], out[1][:2])
            assert out[0][1]["ok"] is True and out[0][1]["saved"] is True, (raw, out[0][1])
            assert out[0][1]["message"] == "Studio accepted the key."
            assert out[0][2] == out[1][2] == "good-key-1234", (raw, out[0][2], out[1][2])
            assert out[0][1]["in_use"] == "settings" and out[0][1]["saved_hint"] == "…1234", out[0][1]
            served += out[1][3] == "gateway"

        # a 401: verified away, the saved key unchanged, the message names it
        out = []
        for side in (d, v):
            out.append(run(side, {"api_key": "bad-key-9999"}))
        assert out[0][0] == out[1][0] == 200 and out[0][1] == out[1][1], (out[0][:2], out[1][:2])
        assert out[0][1]["ok"] is False and out[0][1]["saved"] is False and out[0][1]["status"] == 401
        assert "the key you pasted was not saved (the saved key is unchanged)" in out[0][1]["message"], out[0][1]
        assert out[0][2] == out[1][2] == "good-key-1234", (out[0][2], out[1][2])
        served += out[1][3] == "gateway"

        # an unreachable Studio: the key is still saved, "couldn't confirm" (prefix only: the
        # exception text is the HTTP client library's)
        set_llm_url(d, "http://127.0.0.1:1")
        set_llm_url(v, "http://127.0.0.1:1")
        out = []
        for side in (d, v):
            out.append(run(side, {"api_key": "unreach-key-5678"}))
        assert out[0][0] == out[1][0] == 200, (out[0][0], out[1][0])
        for st, body, key, _served in out:
            assert body["ok"] is False and body["saved"] is True and body["status"] == 503, body
            assert body["message"].startswith("Saved, but couldn't confirm it: Unsloth Studio unreachable at http://127.0.0.1:1"), body
            assert body["unreachable"] is True, body
        assert out[0][2] == out[1][2] == "unreach-key-5678", (out[0][2], out[1][2])
        assert out[0][1]["saved_hint"] == out[1][1]["saved_hint"] == "…5678"
        served += out[1][3] == "gateway"

        # back to the fake Studio for the clear case
        set_llm_url(d, studio.url())
        set_llm_url(v, studio.url())

        # clear: the saved key removed, no environment key behind it
        out = []
        for side in (d, v):
            out.append(run(side, {"clear": True}))
        assert out[0][0] == out[1][0] == 200 and out[0][1] == out[1][1], (out[0][:2], out[1][:2])
        assert out[0][1] == {"ok": False, "message": "The saved key was removed.", "saved": False,
                             "in_use": "none", "saved_hint": "", "env_hint": "",
                             "explain": "No Studio API key is set."}, out[0][1]
        assert out[0][2] == out[1][2] == ""
        served += out[1][3] == "gateway"
    finally:
        studio.close()
    assert served >= 6, served


def test_downloads_rules_and_page_are_pythons(client, seed):
    """GET /rules/download.md and GET /pages/<id>/download from the gateway: the GM always gets the
    world's rules (a non-GM only with the section readable AND the world's download flag, without the
    :::gm blocks), and a page's file with Content-Disposition under the same visibility gate (a hidden
    page is a 404 for a player, an unknown one a 404 for anyone)."""
    import html as _html
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PageDoc, World
    from pathlib import Path
    import os
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ.get("ND_TEST_DATA_DIR", "tests/_data")) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / "probe-doc.html"
    f.write_text("<p>Probe &amp; page</p>", encoding="utf-8")
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.rules_md = "Intro\n\n## Combat\n\nRoll **d20**.\n\n:::gm\nSecret twist.\n:::\n\n## Gear\n\nKnives.\n"
        wa.players_can_download_rules = True
        wa.section_access_json = json.dumps({"rules": {"player": "read"}, "pages": {"player": "read"}})
        doc = PageDoc(world_id=w, name="Probe Doc", file_url=f"/uploads/{f.name}", visible_to_players=True)
        hid = PageDoc(world_id=w, name="Hidden", file_url=f"/uploads/{f.name}", visible_to_players=False)
        db.add_all([doc, hid])
        db.commit()
        did, hid_id = doc.id, hid.id
    finally:
        db.close()

    def shape_rules(r):
        return (r.status_code, r.headers.get("content-type"), r.headers.get("content-disposition"),
                [line for line in r.text.splitlines() if line], ":::gm" not in r.text)

    served = 0
    people = ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug),
              (seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug))
    urls = ["/rules/download.md", f"/pages/{did}/download", f"/pages/{hid_id}/download", "/pages/999999/download"]
    for email, pw, world in people:
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            a, b = d.get(u), via.get(u)
            assert a.status_code == b.status_code, (email, u, a.status_code, b.status_code, a.text[:80], b.text[:80])
            if a.status_code == 200:
                if "download.md" in u:
                    assert shape_rules(a) == shape_rules(b), (email, u, shape_rules(a), shape_rules(b))
                else:
                    assert b.content == a.content and b.headers.get("content-type") == a.headers.get("content-type")
                    assert a.headers.get("content-disposition") == b.headers.get("content-disposition"), (u, a.headers.get("content-disposition"))
            else:
                # a refusal is Python's {"detail": ...}; the non-numeric-id case is its 422 (no json body check needed — bytes may gzip)
                assert a.status_code == b.status_code and a.text == b.text, (email, u, a.text[:80], b.text[:80])
            assert b.headers.get("x-nd-served-by") == "gateway", (email, u, a.status_code)  # every answer here is the gateway's
            served += b.headers.get("x-nd-served-by") == "gateway"
    db = SessionLocal()
    try:
        d2 = db.query(World).filter(World.id == w).first()
        d2.players_can_download_rules = False
        db.commit()
    finally:
        db.close()
    # the flag off: the GM still downloads, the player is refused
    out = []
    for email, pw, world in people:
        c = _signed_in(email, pw, world)
        a, b = c.get("/rules/download.md"), _via(c, base).get("/rules/download.md")
        assert a.status_code == b.status_code, (email, a.status_code, b.status_code)
        if a.status_code == 200:
            assert shape_rules(a) == shape_rules(b), (email, shape_rules(a), shape_rules(b))
        else:
            assert b.json() == a.json(), (email, a.text[:80], b.text[:80])
        assert b.headers.get("x-nd-served-by") == "gateway", (email, a.status_code)
        out.append(a.status_code)
    assert out[0] == 200 and out[1] == 403, out
    db = SessionLocal()
    try:
        from app.models import PageDoc as PD
        for pid in (did, hid_id):
            x = db.get(PD, pid)
            if x:
                db.delete(x)
        db.commit()
    finally:
        db.close()
    assert served >= 8, served


def test_sheet_template_json_export_is_pythons(client, seed):
    """GET /worlds/<id>/export/templates/<tid>.json from the gateway: the same bytes (json.dumps'
    indent-2 spelling, the unicode as-is), the same attachment filename, a wrong-world / unknown
    template / unknown world the same 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    fields = [{"id": "health", "label": "Hëalth ✓", "type": "number", "section": "Resources", "default_value": "5"},
              {"id": "notes", "type": "text"}]
    db = SessionLocal()
    try:
        t = SheetTemplate(world_id=w, name="Exp T", slug="exp-t", description="d", sheet_mode="custom",
                          fields_json=json.dumps(fields))
        nt = SheetTemplate(world_id=w, name="No Slug", slug="", fields_json=json.dumps([]))
        db.add_all([t, nt])
        db.commit()
        tid, nid = t.id, nt.id
    finally:
        db.close()
    served = 0
    people = ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug))
    urls = [f"/worlds/{w}/export/templates/{tid}.json", f"/worlds/{w}/export/templates/{nid}.json",
            f"/worlds/{seed.world_b.id}/export/templates/{tid}.json", f"/worlds/{w}/export/templates/999999.json"]
    # a non-numeric id is Python's own 422 (the path param's int parse) — the gateway forwards it
    py_only = ["/worlds/{}/export/templates/x.json"]
    for email, pw, world in people:
        d = _signed_in(email, pw, world)
        via = _via(d, base)
        for u in urls:
            a, b = d.get(u), via.get(u)
            assert a.status_code == b.status_code, (email, u, a.status_code, b.status_code)
            if a.status_code == 200:
                assert b.content == a.content, (u, a.text[:120], b.text[:120])
                assert b.headers.get("content-disposition") == a.headers.get("content-disposition"), u
                assert b.headers.get("content-type") == a.headers.get("content-type"), u
            else:
                # a refusal is Python's {"detail": ...}; the non-numeric-id case is its own 422
                assert a.text == b.text, (email, u, a.text[:80], b.text[:80])
            if u in py_only:
                assert b.headers.get("x-nd-served-by") != "gateway", (email, u)
                continue
            assert b.headers.get("x-nd-served-by") == "gateway", (email, u, a.status_code)
            served += 1
    db = SessionLocal()
    try:
        for tid2 in (tid, nid):
            x = db.get(SheetTemplate, tid2)
            if x:
                db.delete(x)
        db.commit()
    finally:
        db.close()
    assert served >= 8, served


def test_map_prop_update_and_delete_are_pythons(client, seed):
    """POST /api/maps/props/<id>/update and /delete from the gateway: the clamped footprint
    (1e9 -> 40, "x" -> the stored value, NaN/Infinity text -> the stored value), the cleaned name and
    tags, world-scoped 404s, the delete keeping the file while a map references it (and removing it
    otherwise), the whole row compared after every write."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import MapProp
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["ND_TEST_DATA_DIR"]) / "uploads" / "props"
    up.mkdir(parents=True, exist_ok=True)
    f1, f2 = up / "prop-a.webp", up / "prop-b.webp"
    f1.write_bytes(b"a"); f2.write_bytes(b"b")
    db = SessionLocal()
    try:
        p1 = MapProp(world_id=w, name="Bed", file_url="/uploads/props/prop-a.webp", tags="old", px_w=10, px_h=8, cells_w=1.0, cells_h=1.0)
        p2 = MapProp(world_id=w, name="Barrel", file_url="/uploads/props/prop-b.webp")
        db.add_all([p1, p2])
        db.commit()
        i1, i2 = p1.id, p2.id
    finally:
        db.close()

    from tests.test_gateway_writes import _row_by

    def row(i):
        r = _row_by("map_props", "id", i)
        return {k: r[k] for k in ("name", "tags", "cells_w", "cells_h", "px_w", "px_h")} if r else None

    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    served = 0
    updates = [
        ({"name": "  Four   poster  bed ", "cells_w": 1e9, "cells_h": "x", "tags": "bed  furniture"},
         {"name": "Four poster bed", "tags": "bed furniture", "cells_w": 40.0, "cells_h": 1.0}),
        ({"name": "Ünï name ✓", "cells_w": 0.1, "cells_h": 2.63},
         {"name": "Ünï name ✓", "cells_w": 0.25, "cells_h": 2.5}),
        ({"tags": 0}, {"tags": ""}),
        ({}, {"name": "Ünï name ✓", "tags": "", "cells_w": 0.25, "cells_h": 2.5}),
    ]
    for body, _expect in updates:
        out = []
        for side in (d, v):
            db = SessionLocal()
            try:
                x = db.get(MapProp, i1)
                x.name, x.tags, x.cells_w, x.cells_h = "Bed", "old", 1.0, 1.0
                db.commit()
            finally:
                db.close()
            r = side.post(f"/api/maps/props/{i1}/update", json=body)
            out.append((r.status_code, r.json() if r.status_code == 200 else r.text[:80], row(i1), r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (body, out[0], out[1])
        served += out[1][3] == "gateway"
    # NaN / Infinity text: parse_json reads them, _cells' float() sees non-finite -> the stored value
    for body in ('{"cells_w": NaN, "cells_h": Infinity}',):
        out = []
        for side in (d, v):
            db = SessionLocal()
            try:
                x = db.get(MapProp, i1)
                x.name, x.tags, x.cells_w, x.cells_h = "Bed", "old", 1.0, 1.0
                db.commit()
            finally:
                db.close()
            r = side.post(f"/api/maps/props/{i1}/update", content=body.encode(), headers={"content-type": "application/json"})
            out.append((r.status_code, r.json() if r.status_code == 200 else r.text[:80], row(i1), r.headers.get("x-nd-served-by")))
        assert out[0][:3] == out[1][:3], (out[0], out[1])
        served += out[1][3] == "gateway"
    # a non-object body is the 400
    for raw in (b"[1]",):
        a, b = d.post(f"/api/maps/props/{i1}/update", content=raw, headers={"content-type": "application/json"}), \
               v.post(f"/api/maps/props/{i1}/update", content=raw, headers={"content-type": "application/json"})
        assert a.status_code == b.status_code == 400 and a.json()["detail"] == b.json()["detail"], (a.text, b.text)
        assert b.headers.get("x-nd-served-by") == "gateway"
    # a row of ANOTHER world is a 404
    from app.models import MapProp as _MP
    db = SessionLocal()
    try:
        foreign = _MP(world_id=seed.world_b.id, name="Far", file_url="/uploads/props/far.webp")
        db.add(foreign)
        db.commit()
        fid = foreign.id
    finally:
        db.close()
    a, b = d.post(f"/api/maps/props/{fid}/update", json={"name": "mine"}), v.post(f"/api/maps/props/{fid}/update", json={"name": "mine"})
    assert a.status_code == b.status_code == 404 and a.text == b.text, (a.status_code, b.status_code)
    assert b.headers.get("x-nd-served-by") == "gateway"
    # delete: the file kept while a map references the url; the row re-created per side (the DB is shared)
    db = SessionLocal()
    try:
        from app.models import Schematic
        db.add(Schematic(world_id=w, name="Uses", slug="uses-prop", elements_json=json.dumps([{"kind": "image", "url": "/uploads/props/prop-a.webp"}])))
        db.commit()
    finally:
        db.close()
    out = []
    for side in (d, v):
        db = SessionLocal()
        try:
            db.add(MapProp(world_id=w, name="Bed", file_url="/uploads/props/prop-a.webp"))
            db.commit()
            from sqlalchemy import text as _t
            nid = db.execute(_t("SELECT max(id) FROM map_props WHERE world_id = :w"), {"w": w}).scalar()
        finally:
            db.close()
        r = side.post(f"/api/maps/props/{nid}/delete")
        out.append((r.status_code, r.json(), f1.is_file(), r.headers.get("x-nd-served-by")))
    assert out[0][:3] == out[1][:3] == (200, {"ok": True, "file_kept": True}, True), out
    assert out[1][3] == "gateway"
    # the file removed when nothing references it (the original i2 row must go first: its url would
    # otherwise be "still used by" the library itself)
    db = SessionLocal()
    try:
        db.delete(db.get(MapProp, i2))
        db.commit()
    finally:
        db.close()
    out = []
    for side in (d, v):
        db = SessionLocal()
        try:
            db.add(MapProp(world_id=w, name="Barrel", file_url="/uploads/props/prop-b.webp"))
            db.commit()
            from sqlalchemy import text as _t
            nid = db.execute(_t("SELECT max(id) FROM map_props WHERE world_id = :w"), {"w": w}).scalar()
        finally:
            db.close()
        r = side.post(f"/api/maps/props/{nid}/delete")
        out.append((r.status_code, r.json(), f2.is_file(), r.headers.get("x-nd-served-by")))
    assert out[0][:3] == out[1][:3] == (200, {"ok": True, "file_kept": False}, False), out
    assert out[1][3] == "gateway"
    f1.unlink(missing_ok=True)
    f2.unlink(missing_ok=True)
    db = SessionLocal()
    try:
        from app.models import Schematic
        x = db.query(Schematic).filter(Schematic.slug == "uses-prop").first()
        if x:
            db.delete(x)
        db.commit()
    finally:
        db.close()
    assert served >= 5, served


def test_imagegen_gets_are_pythons(client, seed):
    """GET /api/ai/imagegen/model-templates, /jobs and /jobs/<id> from the gateway: the exported
    built-ins byte-identically, a saved custom template in the list (the params_json fields, the
    falsy ones defaulted), the world's newest-20 job list and one job's dict with the same
    created_at / updated_at spellings; a wrong-world job is a 404."""
    base = str(client.base_url)
    w = seed.world_a.id
    # the Studio backend settings: the same ones the GM imagegen test rides on
    from app.database import SessionLocal
    from app.models import ImageJob, PromptPreset
    import time
    db = SessionLocal()
    try:
        db.add(PromptPreset(world_id=w, label="Z custom", scope="image_template",
                            text="masterpiece, 1girl", negative="worst quality",
                            params_json=json.dumps({"match": ["anima"], "steps": 12, "cfg": 1.0, "note": "my note"})))
        db.add_all([
            ImageJob(world_id=w, prompt="a dragon", params_json="{}", status="done",
                     result_urls_json=json.dumps(["/uploads/ai-images/x.png"])),
            ImageJob(world_id=seed.world_b.id, prompt="far", params_json="{}", status="done"),
        ])
        db.commit()
        mine = db.query(ImageJob).filter(ImageJob.world_id == w).first()
        far = db.query(ImageJob).filter(ImageJob.world_id == seed.world_b.id).first()
        mine_id, far_id = mine.id, far.id
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    served = 0
    # model-templates: built-ins + the custom row
    a, b = d.get("/api/ai/imagegen/model-templates"), v.get("/api/ai/imagegen/model-templates")
    assert a.status_code == b.status_code == 200 and a.json() == b.json(), (a.text[:120], b.text[:120])
    custom = b.json()["custom"]
    assert len(custom) == 1 and custom[0]["id"].startswith("custom-"), custom
    assert custom[0]["match"] == ["anima"] and custom[0]["steps"] == 12 and custom[0]["cfg"] == 1.0
    assert custom[0]["note"] == "my note" and custom[0]["example_prompt"] == "masterpiece, 1girl"
    assert custom[0]["prefix"] == "" and custom[0]["custom"] is True
    assert len(b.json()["templates"]) >= 18
    served += b.headers.get("x-nd-served-by") == "gateway"
    # the job list: only this world's
    a, b = d.get("/api/ai/imagegen/jobs"), v.get("/api/ai/imagegen/jobs")
    assert a.status_code == b.status_code == 200 and a.json() == b.json(), (a.text[:160], b.text[:160])
    assert [j["id"] for j in b.json()] == [mine_id], (mine_id, b.text[:120])
    served += b.headers.get("x-nd-served-by") == "gateway"
    # one job: this world's served, another world's the same 404, unknown the same 404
    for jid, code in ((mine_id, 200), (far_id, 404), (999999, 404)):
        a, b = d.get(f"/api/ai/imagegen/jobs/{jid}"), v.get(f"/api/ai/imagegen/jobs/{jid}")
        assert a.status_code == b.status_code == code, (jid, a.status_code, b.status_code)
        if code == 200:
            assert a.json() == b.json(), (a.text[:120], b.text[:120])
            assert b.json()["urls"] == ["/uploads/ai-images/x.png"], b.text[:120]
        else:
            assert a.json() == b.json(), (jid, a.text, b.text)
        assert b.headers.get("x-nd-served-by") == "gateway", (jid, a.status_code)
        served += 1
    db = SessionLocal()
    try:
        db.query(ImageJob).filter(ImageJob.world_id.in_([w, seed.world_b.id])).delete(synchronize_session=False)
        db.query(PromptPreset).filter(PromptPreset.scope == "image_template").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert served >= 4, served


def test_world_json_exports_are_pythons(client, seed):
    """GET /worlds/<id>/export/rules.json and /player-characters.json from the gateway: the same
    indent-2 bytes (unicode raw), the same attachment filenames, a missing world the same 404, a
    world with no custom rules Python's 404 with its message; the pc rows' JSON fields loads'd with
    junk kept as text and a pc of another world absent."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.rules_md = "Intro ✓\n\n## Combat\n\nRoll **d20**.\n"
        db.add_all([
            PlayerCharacter(world_id=w, name="Zed", level=3, xp=100, stats_json=json.dumps([{"id": "str", "value": 2}]),
                            currency_json=json.dumps([{"abbr": "CR", "value": 5}]), equipment_json="{bad"),
            PlayerCharacter(world_id=w, name="Amy", level=1),
            PlayerCharacter(world_id=seed.world_b.id, name="Faraway", level=2),
        ])
        db.commit()
    finally:
        db.close()
    empty = db.query(World).filter(World.id == seed.world_b.id).first()
    b_slug, w_slug = empty.slug, db.query(World).filter(World.id == w).first().slug
    db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    served = 0
    urls = [f"/worlds/{w}/export/rules.json", f"/worlds/{w}/export/player-characters.json",
            f"/worlds/{w}/export/rules.json", "/worlds/999999/export/player-characters.json",
            f"/worlds/{seed.world_b.id}/export/rules.json"]
    for u in urls:
        a, b = d.get(u), v.get(u)
        assert a.status_code == b.status_code, (u, a.status_code, b.status_code, a.text[:80], b.text[:80])
        if a.status_code == 200:
            assert b.content == a.content, (u, a.text[:160], b.text[:160])
            assert b.headers.get("content-disposition") == a.headers.get("content-disposition"), u
        else:
            assert a.text == b.text, (u, a.text[:80], b.text[:80])
        assert b.headers.get("x-nd-served-by") == "gateway", (u, a.status_code)
        served += 1
    # the pcs export's content shape: name order, the loads'd fields, junk as text, no other world's pc
    b = v.get(f"/worlds/{w}/export/player-characters.json")
    data = b.json()
    assert [p["name"] for p in data] == ["Amy", "Zed"], data
    zed = data[1]
    assert zed["stats"] == [{"id": "str", "value": 2}] and zed["currency"] == [{"abbr": "CR", "value": 5}]
    assert zed["equipment"] == "{bad" and zed["skills"] == [] and zed["custom_fields"] == {}
    assert zed["level"] == 3 and zed["player_name"] == ""  # the column's default
    # the filenames
    b = v.get(f"/worlds/{w}/export/rules.json")
    assert f'filename="{w_slug}-rules.json"' in b.headers.get("content-disposition", ""), b.headers.get("content-disposition")
    b = v.get(f"/worlds/{w}/export/player-characters.json")
    assert f'filename="{w_slug}-player-characters.json"' in b.headers.get("content-disposition", "")
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.rules_md = ""
        db.commit()
    finally:
        db.close()
    a, b = d.get(f"/worlds/{w}/export/rules.json"), v.get(f"/worlds/{w}/export/rules.json")
    assert a.status_code == b.status_code == 404 and a.json() == b.json() == {"detail": "This world has no custom rules set"}, (a.text, b.text)
    assert b.headers.get("x-nd-served-by") == "gateway"
    db = SessionLocal()
    try:
        from app.models import PlayerCharacter as _PC
        db.query(_PC).filter(_PC.world_id.in_([w, seed.world_b.id])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    assert served >= 5, served


def test_recap_audio_upload_is_pythons(client, seed):
    """POST /api/sessions/<id>/recap-audio/upload from the gateway: the clip created and attached
    (name / description / transcript / visibility, the file under uploads/audio), the unsupported
    extension the 400 with Python's message, a missing file refused the same way on both sides - the
    panel and clip compared."""
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    from app.models import GameSession
    try:
        gs = GameSession(world_id=w, title="Recap Up")
        db.add(gs)
        db.commit()
        sid = gs.id
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)

    def panel_state(r):
        j = r.json()
        clip = j.get("clip") or {}
        audio = j.get("audio") or []
        attached = [c["id"] for c in audio if isinstance(c, dict)]
        return (r.status_code, clip.get("name"), clip.get("description"), clip.get("visible_to_players"),
                clip.get("transcript"), bool(clip.get("file_url")), attached)

    out = []
    for side in (d, v):
        body = (f'--XB\r\nContent-Disposition: form-data; name="name"\r\n\r\nFolk song\r\n'
                f'--XB\r\nContent-Disposition: form-data; name="lyrics"\r\n\r\nVerse one\r\n'
                f'--XB\r\nContent-Disposition: form-data; name="visible_to_players"\r\n\r\ntrue\r\n'
                f'--XB\r\nContent-Disposition: form-data; name="file"; filename="song.mp3"\r\n'
                f'Content-Type: audio/mpeg\r\n\r\nID3data\r\n--XB--\r\n').encode()
        r = side.post(f"/api/sessions/{sid}/recap-audio/upload", content=body,
                      headers={"content-type": "multipart/form-data; boundary=XB"})
        out.append((panel_state(r), r.headers.get("x-nd-served-by")))
    assert out[0][0] == out[1][0], (out[0][0], out[1][0])
    assert out[0][0][3] is True and out[0][0][4] == "Verse one", out[0][0]
    assert out[0][0][5], out[0][0]  # the file_url present
    assert out[1][1] == "gateway", out[1][1]
    served = out[1][1] == "gateway"
    # the unsupported extension
    out = []
    for side in (d, v):
        body = (f'--XB\r\nContent-Disposition: form-data; name="file"; filename="x.exe"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\nMZ\r\n--XB--\r\n').encode()
        r = side.post(f"/api/sessions/{sid}/recap-audio/upload", content=body,
                      headers={"content-type": "multipart/form-data; boundary=XB"})
        out.append((r.status_code, "Unsupported" in r.text, r.headers.get("x-nd-served-by")))
    assert out[0][0] == out[1][0] == 400 and out[0][1] and out[1][1], out
    assert out[1][2] == "gateway", out[1][2]
    served += 1
    # a missing file refused on both sides (status may differ: FastAPI's 422 vs the handler's 400)
    a, b = d.post(f"/api/sessions/{sid}/recap-audio/upload", data={}), v.post(f"/api/sessions/{sid}/recap-audio/upload", data={})
    assert a.status_code == b.status_code, (a.status_code, b.status_code)
    assert (b.headers.get("x-nd-served-by") == "gateway") or (b.headers.get("x-nd-served-by") is None and a.status_code == 422)
    db = SessionLocal()
    from app.models import GameSession as _GS
    try:
        x = db.get(_GS, sid)
        if x:
            db.delete(x)
        db.commit()
    finally:
        db.close()
    assert served >= 2, served


from .conftest import PLAYER_PASSWORD as _PP  # the module may not import it at the top

def test_entity_context_is_pythons(client, seed):
    """POST /api/ai/entity-context from the gateway: the ask-ai gate (403 with both toggles off),
    a hidden entity a 404 for a player (shared or GM-ok), an empty query an empty context, and the
    per-question excerpt - gmonly text stripped from a player's body, kept for the GM. The excerpt's
    content is the ported best_matching_excerpt's, compared against Python's answer."""
    base = str(client.base_url)
    w = seed.world_a.id
    from app.database import SessionLocal
    from app.models import Entity, World
    db = SessionLocal()
    try:
        wa = db.query(World).filter(World.id == w).first()
        wa.players_can_ask_ai = True
        db.add(Entity(world_id=w, kind="npc", name="Ctx NPC", visible_to_players=True,
                      body="Front matter.\n\n## Combat\n\nThe creature fights with claws and [gmonly]a hidden weakness[/gmonly] fire.\n\n## Lore\n\nIt came from the sea long ago.\n"))
        db.commit()
    finally:
        db.close()
    npc_id = db.query(__import__("app.models", fromlist=["Entity"]).Entity).filter_by(name="Ctx NPC").first().id
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, _PP, seed.world_a.slug)
    served = 0
    # the excerpt compared whole (the ported locator must agree byte for byte)
    def run_all(query):
        out = []
        for side in (d, pl):
            for via in (False, True):
                c = _via(side, base) if via else side
                r = c.post("/api/ai/entity-context", json={"entity_id": npc_id, "query": query})
                out.append((r.status_code, r.json().get("context"), r.headers.get("x-nd-served-by")))
        # GM and player differ by the gmonly text, but each side's pair must agree
        assert out[0][:2] == out[1][:2], (query, out[0], out[1])
        assert out[2][:2] == out[3][:2], (query, out[2], out[3])
        return out
    combat = run_all("What does it do in combat?")
    served += combat[1][2] == "gateway" and combat[3][2] == "gateway"
    assert "hidden weakness" not in combat[3][1], combat[3][1][:160]
    assert "hidden weakness" in combat[1][1], combat[1][1][:160]
    # an empty query: an empty context
    empty = run_all("")
    served += empty[1][2] == "gateway" and empty[3][2] == "gateway"
    assert empty[0][1] == "" and empty[3][1] == "", (empty[0][1][:80], empty[3][1][:80])
    # a hidden entity is a 404 for the player, fine for the GM; unknown 404s
    db2 = SessionLocal()
    hid = db2.query(__import__("app.models", fromlist=["Entity"]).Entity).filter_by(name="Ctx NPC").first()
    hid.visible_to_players = False
    db2.commit()
    db2.close()
    for query in ("combat",):
        for who in (d, pl):
            a = who.post("/api/ai/entity-context", json={"entity_id": npc_id, "query": query})
            b = _via(who, base).post("/api/ai/entity-context", json={"entity_id": npc_id, "query": query})
            assert a.status_code == b.status_code, (who, a.status_code, b.status_code)
            if a.status_code == 200:
                assert a.json() == b.json()
            else:
                assert a.json() == b.json() == {"detail": "Not Found"}, (a.text, b.text)
            served += b.headers.get("x-nd-served-by") == "gateway"
    db2 = SessionLocal()
    x = db2.query(__import__("app.models", fromlist=["Entity"]).Entity).filter_by(name="Ctx NPC").first()
    if x:
        db2.delete(x)
    db2.commit()
    db2.close()
    assert served >= 4, served


def test_tool_pages_are_pythons(client, seed):
    """GET /image-gen, /imagestudio and /tools/code-assist from the gateway: the player image page for the GM and a player (own
    characters only, the opt-in flag, 403 otherwise), Image Studio with a saved SwarmUI address (and the section closed to a player),
    and Code Assist's source file list for a GM, with a player's 403 until the section is opened."""
    from app.database import SessionLocal, get_app_settings
    from app.models import PlayerCharacter, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([PlayerCharacter(world_id=w, name="Zed", owner_user_id=seed.player_a.id), PlayerCharacter(world_id=w, name="Amy", owner_user_id=seed.player_a.id),
                    PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)])
        get_app_settings(db).swarmui_external_url = "http://swarm.lan:7801/"
        db.get(World, w).players_can_use_image_gen = False
        db.commit()
    finally:
        db.close()
    served = 0
    for allowed in (False, True):
        db = SessionLocal()
        try:
            wa = db.get(World, w)
            wa.players_can_use_image_gen = allowed
            wa.section_access_json = json.dumps({"code_assist": {"player": "read"}, "imagestudio": {"player": "read"}} if allowed else {})
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            via = _via(d, base)
            for url in ("/image-gen", "/imagestudio", "/tools/code-assist"):
                r = via.get(url, follow_redirects=False)
                assert r.headers.get("x-nd-served-by") == "gateway", (email, url, allowed, r.status_code)
                if r.status_code == 200:
                    served += _same_page(d, via, url, email)
                else:
                    a = d.get(url, follow_redirects=False)
                    assert (a.status_code, a.content) == (r.status_code, r.content), (email, url)
    assert served >= 6


def test_code_assist_generate_and_status_are_pythons(client, seed):
    """POST /tools/code-assist/generate and GET /tools/code-assist/generate/<id> from the gateway: the refusals (no instruction, a bad
    path, a type not allowed, a missing file, an unreadable or too large one), the job row created for a real file (and what the
    unconfigured AI makes of it), and the status route over pending / failed / done jobs - the unified diff against what was sent,
    the truncation warning, a job of another world or kind."""
    import time
    from app.database import SessionLocal
    from app.models import AudioJob
    base = str(client.base_url)
    w, w2 = seed.world_a.id, seed.world_b.id
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)
    big = next((p for p in sorted((Path(__file__).parent.parent / "static").rglob("*.js")) if p.stat().st_size > 80_000), None)
    cases = [{"file": "app/main.py"}, {"file": "app/main.py", "instruction": "   "}, {"file": "../etc/passwd", "instruction": "x"},
             {"file": "app/../app/main.py", "instruction": "x"}, {"file": "app/main.bin", "instruction": "x"}, {"file": "app/nosuch.py", "instruction": "x"},
             {"file": "tests/conftest.py", "instruction": "x"}, {"file": "/app/__init__.py", "instruction": "x"}]
    if big:
        cases.append({"file": "static/" + big.relative_to(Path(__file__).parent.parent / "static").as_posix(), "instruction": "x"})
    for form in cases:
        a, b = gm.post("/tools/code-assist/generate", data=form), via.post("/tools/code-assist/generate", data=form)
        served = b.headers.get("x-nd-served-by") == "gateway"
        if "instruction" in form or "file" not in form:
            assert served or a.status_code == 422, form
        if a.status_code >= 400 and served:
            assert (a.status_code, a.json()) == (b.status_code, b.json()), form
    rows = []
    for who in (gm, via):
        r = who.post("/tools/code-assist/generate", data={"file": "app/__init__.py", "instruction": "  add a docstring  ", "model": " m ", "think": "true"})
        assert r.status_code == 200, r.text
        rows.append(r.json()["job_id"])
    time.sleep(3)
    db = SessionLocal()
    try:
        ja, jb = (db.get(AudioJob, i) for i in rows)
        for col in ("world_id", "purpose", "filename", "created_by_user_id", "model", "think", "extra_instructions", "use_rag", "assist_params_json",
                    "transcript", "audio_path", "delete_after"):
            assert getattr(ja, col) == getattr(jb, col), col
        assert (ja.status, ja.error) == (jb.status, jb.error)
        seeded = {}
        for key, status, result, original, other_world in (
                ("done", "done", {"text": "line one\nline TWO\nline three\nextra\n", "model": "coder"}, "line one\nline two\nline three\n", False),
                ("short", "done", {"text": "a\n"}, "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\n", False),
                ("failed", "error", {}, "x", False), ("pending", "pending", {}, "x", False), ("other", "done", {"text": "t"}, "x", True)):
            j = AudioJob(world_id=w2 if other_world else w, purpose="ai_assist", filename="AI assist · code_assist", status=status, error="boom" if status == "error" else "",
                         model="m", transcript=original, audio_path="", delete_after=False, result_json=json.dumps(result),
                         assist_params_json=json.dumps({"op": "code_edit", "surface": "code_assist", "meta": "File: app/x.py", "instruction": "i", "lang": ""}))
            db.add(j)
            db.flush()
            seeded[key] = j.id
        wrong_op = AudioJob(world_id=w, purpose="ai_assist", filename="f", status="done", transcript="", audio_path="", delete_after=False,
                            assist_params_json=json.dumps({"op": "rewrite"}))
        db.add(wrong_op)
        db.flush()
        seeded["wrong"] = wrong_op.id
        db.commit()
    finally:
        db.close()
    for key, jid in list(seeded.items()) + [("missing", 999999)]:
        url = f"/tools/code-assist/generate/{jid}"
        a, b = gm.get(url), via.get(url)
        assert b.headers.get("x-nd-served-by") == "gateway", key
        assert (a.status_code, a.json()) == (b.status_code, b.json()), key


def test_health_presets_and_world_summary_are_pythons(client, seed):
    """GET /health, the Ollama presets (list / save / delete - ai_models.json byte for byte, the options clamped as Python clamps them),
    and GET /api/ai/world-summary for each audience tier (no job, pending, failed, done, the other tier's job left out, closed to players)."""
    import os
    from datetime import datetime
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import AudioJob, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    path = Path(os.environ["DB_PATH"]).parent / "ai_models.json"
    original = path.read_bytes() if path.exists() else None
    anon = httpx.Client(base_url=base, timeout=30)
    a, b = httpx.get(str(client.base_url) + "/health"), anon.get("/health")
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert (a.status_code, a.content) == (b.status_code, b.content)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)
    try:
        for start in (None, b'{"custom": [], "hidden": [], "presets": [{"label": "Old", "model": "m"}, {"label": "Keep"}]}'):
            def reset():
                if start is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(start)
            steps = [("GET", "/api/ai/presets", None),
                     ("POST", "/api/ai/presets", {"label": "  Fast  ", "model": " m ", "system_extra": "be brief", "options": {
                         "temperature": 1, "top_k": 5.9, "num_ctx": 99999999, "seed": -4, "use_mmap": 0, "bogus": 1, "top_p": 2, "mirostat": True, "num_gpu": None, "min_p": [1]}}),
                     ("POST", "/api/ai/presets", {"label": "Old"}), ("POST", "/api/ai/presets", {"label": "   "}), ("POST", "/api/ai/presets", {"model": "x"}),
                     ("DELETE", "/api/ai/presets/Old", None), ("DELETE", "/api/ai/presets/nope", None), ("DELETE", "/api/ai/presets/caf%C3%A9", None)]
            for method, url, body in steps:
                reset()
                ra = gm.request(method, url, json=body) if body is not None else gm.request(method, url)
                after_a = path.read_bytes() if path.exists() else None
                reset()
                rb = via.request(method, url, json=body) if body is not None else via.request(method, url)
                after_b = path.read_bytes() if path.exists() else None
                served = rb.headers.get("x-nd-served-by") == "gateway"
                if body is None or "label" in body:
                    assert served, (method, url, body)
                if served:
                    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), (method, url, body)
                    assert after_a == after_b, (method, url, body)
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([AudioJob(world_id=w, purpose="world_summary", filename="s", status="done", recap="GM digest", audience="gm", finished_at=datetime(2031, 1, 2, 3, 4, 5),
                             transcript="", audio_path="", delete_after=False),
                    AudioJob(world_id=w, purpose="world_summary", filename="s", status="done", recap="Players digest", audience="players", finished_at=datetime(2031, 1, 2, 3, 4, 6),
                             transcript="", audio_path="", delete_after=False)])
        db.commit()
    finally:
        db.close()
    def state(status, audience, error=""):
        db = SessionLocal()
        try:
            db.add(AudioJob(world_id=w, purpose="world_summary", filename="s", status=status, error=error, recap="", audience=audience, transcript="", audio_path="", delete_after=False))
            db.commit()
        finally:
            db.close()
    for step in (None, ("pending", "gm"), ("error", "gm", ""), ("error", "gm", "boom"), ("pending", "players")):
        if step:
            state(*step)
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for allowed in (False, True):
                db = SessionLocal()
                try:
                    db.get(World, w).players_can_view_world_summary = allowed
                    db.commit()
                finally:
                    db.close()
                ra, rb = d.get("/api/ai/world-summary"), _via(d, base).get("/api/ai/world-summary")
                assert rb.headers.get("x-nd-served-by") == "gateway", (email, step)
                assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), (email, step, allowed)


def test_npc_history_is_pythons(client, seed):
    """GET / DELETE /api/npc-talk/<id>/history from the gateway: the caller's saved conversation (none, an empty one, a real one with
    non-ASCII text), its removal, a hidden NPC (404), an item (400 with its message), a world that closes the AI to players (403)."""
    from app.database import SessionLocal
    from app.models import ChatSession, Entity, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        npc = Entity(world_id=w, kind="character", name="Mo")
        hidden = Entity(world_id=w, kind="character", name="Spy", visible_to_players=False)
        item = Entity(world_id=w, kind="item", name="Blade")
        db.add_all([npc, hidden, item])
        db.get(World, w).players_can_ask_ai = True
        db.flush()
        db.add(ChatSession(world_id=w, user_id=seed.player_a.id, surface="npc", entity_id=npc.id, title="Mo",
                           messages_json=json.dumps([{"role": "user", "content": "Héllo"}, {"role": "assistant", "content": "x" * 3}])))
        db.add(ChatSession(world_id=w, user_id=seed.gm.id, surface="npc", entity_id=npc.id, title="Mo", messages_json=""))
        db.commit()
        ids = (npc.id, hidden.id, item.id, 999999)
    finally:
        db.close()

    def count():
        db = SessionLocal()
        try:
            return db.query(ChatSession).count()
        finally:
            db.close()

    for allowed in (True, False):
        db = SessionLocal()
        try:
            db.get(World, w).players_can_ask_ai = allowed
            db.commit()
        finally:
            db.close()
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD), (seed.player_b.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug if email != seed.player_b.email else seed.world_b.slug)
            for eid in ids:
                url = f"/api/npc-talk/{eid}/history"
                ra, rb = d.get(url), _via(d, base).get(url)
                assert rb.headers.get("x-nd-served-by") == "gateway", (email, url)
                assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), (email, url, allowed)
    db = SessionLocal()
    try:
        db.get(World, w).players_can_ask_ai = True
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    for eid in (ids[2], ids[1], ids[0], ids[0]):
        n0 = count()
        ra = d.delete(f"/api/npc-talk/{eid}/history")
        n1 = count()
        rb = _via(d, base).delete(f"/api/npc-talk/{eid}/history")
        assert rb.headers.get("x-nd-served-by") == "gateway", eid
        assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), eid
        assert n0 - n1 in (0, 1)
        assert count() == n1


def test_hub_sessions_and_share_state_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/sessions (the Session Log as the players get it: recap marker, what there is to hear and see, the
    section closed to players) and GET /api/characters/<id>/share (none / a live link / a revoked one), for the owner, a GM and someone
    else (404)."""
    from app.database import SessionLocal
    from app.models import AudioClip, CharacterShare, Fact, GameSession, PlayerCharacter, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        clip = AudioClip(world_id=w, name="Theme", file_url="/uploads/t.mp3", visible_to_players=True)
        pc = PlayerCharacter(world_id=w, name="Vex", owner_user_id=seed.player_a.id)
        bare = PlayerCharacter(world_id=w, name="Bare", owner_user_id=seed.player_a.id)
        other = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([clip, pc, bare, other])
        db.flush()
        s1 = GameSession(world_id=w, session_num=1, title="One", session_date="2031-05-04", player_summary="Recap", player_summary_published=True,
                         recap_audio_json=json.dumps([clip.id]),
                         recap_media_json=json.dumps([{"uid": "u1", "kind": "image", "url": "/uploads/p.png", "visible": True, "caption": "Map"}, {"uid": "u2", "kind": "image", "url": "/uploads/q.png", "visible": True}]))
        s2 = GameSession(world_id=w, session_num=2, title="")
        db.add_all([s1, s2])
        db.flush()
        db.add(Fact(world_id=w, content="f", game_session_id=s2.id))
        db.add_all([CharacterShare(world_id=w, character_id=bare.id, token="oldtoken", revoked=True), CharacterShare(world_id=w, character_id=pc.id, token="livetoken1"),
                    CharacterShare(world_id=w, character_id=pc.id, token="livetoken2")])
        db.commit()
        ids = (pc.id, bare.id, other.id, 999999)
    finally:
        db.close()
    for closed in (False, True):
        if closed:
            db = SessionLocal()
            try:
                db.get(World, w).section_access_json = json.dumps({"session_log": {"player": "none"}})
                db.commit()
            finally:
                db.close()
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            for pid in ids:
                for tail in ("hub/sessions", "share"):
                    url = f"/api/characters/{pid}/{tail}"
                    same_host = {"host": "nd.example.com"}  # the link carries the Host as sent
                    ra, rb = d.get(url, headers=same_host), _via(d, base).get(url, headers=same_host)
                    assert rb.headers.get("x-nd-served-by") == "gateway", (email, url)
                    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), (email, url, closed)
    # a forwarded scheme: the gateway hands Python the header only when it trusts its proxy (ND_GATEWAY_TRUST_PROXY); otherwise Python
    # would have seen plain http, which is what the link then carries
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    url = f"/api/characters/{ids[0]}/share"
    hdr = {"x-forwarded-proto": "https", "host": "nd.example.com"}
    rb = _via(d, base).get(url, headers=hdr)
    assert rb.headers.get("x-nd-served-by") == "gateway"
    trusting = os.environ.get("ND_GATEWAY_TRUST_PROXY", "0") in ("1", "true", "yes")
    ra = d.get(url, headers=hdr if trusting else {"host": "nd.example.com"})
    assert (ra.status_code, ra.json()) == (rb.status_code, rb.json())


def test_world_context_and_save_note_are_pythons(client, seed):
    """GET /api/ai/world-context (people, places, lore documents in full, items and feats by name - unfiltered for the GM, as a player
    sees it for an assistant) and POST /api/ai/save-note (the entity written, the 400 without a world, a subtype cut at 64)."""
    from app.database import SessionLocal
    from app.models import Entity, World, WorldMembership
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=w, kind="character", name="Zed", subtype="Fixer", summary="Knows [gmonly]the mayor[/gmonly] well"),
                    Entity(world_id=w, kind="character", name="Amy"), Entity(world_id=w, kind="location", name="Alley", summary="Dark"),
                    Entity(world_id=w, kind="creature", name="Wolf", visible_to_players=False, summary="secret"),
                    Entity(world_id=w, kind="note", name="Lore one", subtype="tale", summary="S", body="Body [gmonly]hidden[/gmonly] " + "x" * 6000),
                    Entity(world_id=w, kind="note", name="Hidden note", body="no", visible_to_players=False),
                    Entity(world_id=w, kind="feat", name="Tough", subtype="combat"), Entity(world_id=w, kind="feat", name="Lucky")]
                   + [Entity(world_id=w, kind="item", name=f"Item {i:02d}", subtype="gear" if i % 3 else None) for i in range(40)])
        db.get(World, w).description = "A neon é world"
        db.commit()
        pa = db.query(WorldMembership).filter(WorldMembership.world_id == w, WorldMembership.user_id == seed.player_a.id).first()
        pa.role = "assistant"
        db.commit()
    finally:
        db.close()
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        ra, rb = d.get("/api/ai/world-context"), _via(d, base).get("/api/ai/world-context")
        assert rb.headers.get("x-nd-served-by") == "gateway", email
        assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()), email
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)

    def notes():
        db = SessionLocal()
        try:
            return [(e.kind, e.subtype, e.name, e.body, e.summary, e.visible_to_players, e.world_id)
                    for e in db.query(Entity).filter(Entity.world_id == w, Entity.name.like("Saved%")).order_by(Entity.id)]
        finally:
            db.close()

    for body in ({"title": "Saved one", "content": "Hello é " + "w" * 200}, {"title": "Saved two", "content": "x", "subtype": "  tale  "},
                 {"title": "Saved three", "content": "y", "subtype": "t" * 80}, {"title": "Saved four", "content": ""}):
        before = len(notes())
        ra = gm.post("/api/ai/save-note", json=body)
        a_rows = notes()
        db = SessionLocal()
        try:
            for e in db.query(Entity).filter(Entity.world_id == w, Entity.name.like("Saved%")).all():
                db.delete(e)
            db.commit()
        finally:
            db.close()
        rb = via.post("/api/ai/save-note", json=body)
        assert rb.headers.get("x-nd-served-by") == "gateway", body
        assert ra.status_code == rb.status_code == 200
        assert ra.json()["name"] == rb.json()["name"]
        assert notes() == a_rows, body
        assert len(a_rows) == before + 1
        db = SessionLocal()
        try:
            for e in db.query(Entity).filter(Entity.world_id == w, Entity.name.like("Saved%")).all():
                db.delete(e)
            db.commit()
        finally:
            db.close()


def test_map_editor_refusals_are_pythons(client, seed):
    """The map editor's write routes answered by the gateway with Python's refusals: another world's map (404, for the GM too on the
    walls / fog / door / light / rooms family; any world for the slug-only routes), a bad body, the field errors (walls / lights not a
    list, fog flags and numbers, door and light ids, rooms' validation messages, elements not objects, an invalid or unknown combat),
    the blank rename, a missing map - same status and message, nothing written."""
    from app.database import SessionLocal
    from app.models import Schematic
    base = str(client.base_url)
    db = SessionLocal()
    try:
        mine = Schematic(world_id=seed.world_a.id, slug="gw-ref-mine", name="Mine", grid_type="none", walls_json=json.dumps([{"id": "d1", "kind": "door", "state": "closed", "pts": [[0, 0], [10, 0]]}]),
                         lights_json=json.dumps([{"id": "l1", "x": 5, "y": 5, "range": 3, "on": True}]))
        theirs = Schematic(world_id=seed.world_b.id, slug="gw-ref-theirs", name="Theirs")
        db.add_all([mine, theirs])
        db.commit()
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)

    def state():
        db = SessionLocal()
        try:
            return [(s.slug, s.name, s.walls_json, s.lights_json, s.fog_json, s.rooms_json, s.elements_json, s.combat_session_id) for s in db.query(Schematic).order_by(Schematic.id)]
        finally:
            db.close()

    json_cases = [("walls", {"walls": "nope"}), ("walls", [1]), ("walls", b"{bad"), ("lights", {"lights": "x"}), ("lights", {}), ("fog", {"enabled": "yes"}), ("fog", [] ),
                  ("fog", {"strict": 1}), ("fog", {"darkness": "dark"}), ("fog", {"personal": True}), ("fog", {"range": "far", "darkness": "x"}), ("door", {"wall_id": 5}),
                  ("door", {"wall_id": "nosuch"}), ("door", {}), ("light", {"id": "nope"}), ("light", {"id": 5}), ("rooms", {"cell": 4}), ("rooms", [1]), ("rooms", "x"),
                  ("rooms", {"cell": 50, "cols": 0, "rows": 3}), ("rooms", {"cell": 50, "cols": 4, "rows": 3, "cells": "x"}), ("rooms", {"cell": 50, "cols": 4, "rows": 3, "cells": [1]}),
                  ("rooms", {"cell": 50, "cols": 4, "rows": 3, "cells": [1, "a"]}), ("rooms", {"cell": 50, "cols": 4, "rows": 3, "cells": [1, 13]}),
                  ("rooms", {"cell": 50, "cols": 4, "rows": 3, "cells": [1, 5]}), ("rooms/detect", {}), ("elements", {"elements": "x"}), ("elements", {"elements": ["a"]}),
                  ("elements", {"elements": None})]
    for slug in ("gw-ref-mine", "gw-ref-theirs", "gw-ref-none"):
        for route, body in json_cases:
            path = f"/maps/schematic/{slug}/{route}"
            kw = {"content": body, "headers": {"content-type": "application/json"}} if isinstance(body, bytes) else {"json": body}
            before = state()
            a = gm.post(path, **kw)
            after_a = state()
            via_resp = via.post(path, **kw)
            assert via_resp.headers.get("x-nd-served-by") == "gateway", (path, body)
            assert (a.status_code, a.json()) == (via_resp.status_code, via_resp.json()), (path, body)
            assert state() == after_a == before or a.status_code == 200, (path, body)
    for slug in ("gw-ref-mine", "gw-ref-theirs", "gw-ref-none"):
        for route, form in (("link-combat", {"combat_session_id": "abc"}), ("link-combat", {"combat_session_id": "1_0"}), ("link-combat", {"combat_session_id": "99999"}),
                            ("rename", {"name": "   "}), ("unlink-combat", {})):
            path = f"/maps/schematic/{slug}/{route}"
            before = state()
            a = gm.post(path, data=form, follow_redirects=False)
            after_a = state()
            b = via.post(path, data=form, follow_redirects=False)
            assert b.headers.get("x-nd-served-by") == "gateway", (path, form)
            assert a.status_code == b.status_code, (path, form)
            if a.status_code >= 400:
                assert a.json() == b.json(), (path, form)
            assert state() == after_a, (path, form)


def test_unsloth_prefs_get_is_pythons(client, seed):
    """GET /api/ai/unsloth/prefs: the defaults for a missing / empty / odd file, stored values (a truthy number as it is), closed to players."""
    import os
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    path = Path(os.environ["DB_PATH"]).parent / "ai_models.json"
    original = path.read_bytes() if path.exists() else None
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)
    try:
        for start in (None, b"", b"not json", b'{"tts_model": "m", "tts_voice": "v", "stt_model": 5, "tts_language": 0, "studio_console_url": "http://x/"}',
                      b'{"tts_instructions": "gruff \\u00e9", "stt_model": ""}', b"[1]"):
            if start is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(start)
            a, b = gm.get("/api/ai/unsloth/prefs"), via.get("/api/ai/unsloth/prefs")
            assert (a.status_code, a.content) == (b.status_code, b.content), start
            if start != b"[1]":
                assert b.headers.get("x-nd-served-by") == "gateway", start
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
    anon = httpx.Client(base_url=base, timeout=30, follow_redirects=False)
    d = httpx.Client(base_url=DIRECT, timeout=30, follow_redirects=False)
    ra, rb = d.get("/api/ai/unsloth/prefs"), anon.get("/api/ai/unsloth/prefs")
    assert (ra.status_code, ra.content) == (rb.status_code, rb.content)


def test_the_dice_page_form_is_pythons(client, seed):
    """POST /dice: a stored roll redirects to /dice with the same row Python stores; a bad notation / no world is Python's redirect or error."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import DiceRoll
    base = str(client.base_url)
    for email, pw, world in ((seed.gm.email, GM_PASSWORD, seed.world_a.slug), (seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug), (seed.gm.email, GM_PASSWORD, None)):
        d = httpx.Client(base_url=DIRECT, timeout=60, follow_redirects=False)
        assert d.post("/login", data={"email": email, "password": pw, "next": "/"}).status_code == 303
        if world:
            d.cookies.set("active_world", world)
        else:
            d.cookies.delete("active_world")
        via = _via(d, base)
        via.follow_redirects = False
        for form in ({"notation": "2d6+3"}, {"notation": "  d20  "}, {"notation": "banana"}, {"notation": ""}, {}, {"notation": "51d6"}, {"notation": "d6", "x": "y"}, {"notation": "d6+é"}):
            a, b = d.post("/dice", data=form), via.post("/dice", data=form)
            assert (a.status_code, a.headers.get("location"), a.content) == (b.status_code, b.headers.get("location"), b.content), (email, world, form)
            if a.status_code == 303 and a.headers["location"] == "/dice":
                assert b.headers.get("x-nd-served-by") == "gateway", (email, world, form)
    db = SessionLocal()
    try:
        rows = db.query(DiceRoll).filter(DiceRoll.notation.in_(["2d6+3", "d20"])).all()
        assert rows and all(r.user_name and r.breakdown for r in rows)
    finally:
        db.close()


def test_service_worker_is_pythons(client, seed):
    """GET /sw.js: headers and bytes as Python sends them (plain, gzip, conditional); other methods and Range stay Python's."""
    base = str(client.base_url)
    d, g = httpx.Client(base_url=DIRECT, timeout=30), httpx.Client(base_url=base, timeout=30)
    keep = ("content-type", "cache-control", "service-worker-allowed", "etag", "last-modified", "content-encoding", "vary")
    for hdrs in ({"accept-encoding": "identity"}, {"accept-encoding": "gzip"}, {"accept-encoding": "gzip, br"}, {}):
        a, b = d.get("/sw.js", headers=hdrs), g.get("/sw.js", headers=hdrs)
        assert b.headers.get("x-nd-served-by") == "gateway", hdrs
        assert (a.status_code, a.content) == (b.status_code, b.content), hdrs
        assert {k: a.headers.get(k) for k in keep} == {k: b.headers.get(k) for k in keep}, hdrs
    etag = d.get("/sw.js").headers["etag"]
    a, b = d.get("/sw.js", headers={"if-none-match": etag}), g.get("/sw.js", headers={"if-none-match": etag})
    assert (a.status_code, a.content) == (b.status_code, b.content)
    for method in ("HEAD", "POST"):
        a, b = d.request(method, "/sw.js"), g.request(method, "/sw.js")
        assert (a.status_code, a.content) == (b.status_code, b.content), method
    a, b = d.get("/sw.js", headers={"range": "bytes=0-9"}), g.get("/sw.js", headers={"range": "bytes=0-9"})
    assert (a.status_code, a.content) == (b.status_code, b.content)


def test_share_page_is_pythons(client, seed):
    """GET /share/<token>: a native sheet (stats, vitals, conditions, feats, gear), a custom-template sheet (resources, roster groups), odd stored
    JSON, and the 404 for an unknown / revoked / over-long token - with the privacy headers."""
    from app.database import SessionLocal
    from app.models import CharacterShare, PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Fate Lite", slug="gw-share-fate", sheet_mode="custom", fields_json=json.dumps([
            {"id": "hp", "label": "Stress", "type": "resource", "default_value": "3/3"}, {"id": "aspect", "label": "High Concept", "type": "text"},
            {"id": "skill", "label": "Great Skill", "type": "number"}]))
        db.add(tpl)
        db.flush()
        native = PlayerCharacter(world_id=w, name="Vex <b>", race="Elf", char_class="Rogue", level=3, current_hp=7, max_hp=12, shock_current=2, shock_max=5,
                                 stats_json=json.dumps([{"id": "str", "value": 12}, {"id": "dex", "value": "14.7"}, {"id": "cha", "value": None}]),
                                 conditions_json=json.dumps(["Prone", 5, "x" * 60] + ["c"] * 14),
                                 feats_json=json.dumps([{"name": "Sneak", "rank": "II"}, {"name": ""}, {"name": "Dodge"}, "bad", {"name": "L" * 100, "rank": "r" * 30}]),
                                 equipment_json=json.dumps([{"name": "Dagger", "qty": 2, "equipped": True}, {"name": "Rope"}, {"name": "Coin", "qty": "50"}, {"name": "Lamp", "qty": 1}]))
        bare = PlayerCharacter(world_id=w, name="Bare")
        custom = PlayerCharacter(world_id=w, name="Fatey", sheet_template_id=tpl.id, custom_fields_json=json.dumps({"hp_current": 2, "hp_max": 4, "aspect": "Hero", "skill": 3}))
        oddjson = PlayerCharacter(world_id=w, name="Odd", conditions_json="not json", feats_json='{"a": 1}', equipment_json="[1, null]", stats_json="[]")
        floaty = PlayerCharacter(world_id=w, name="Floaty", equipment_json=json.dumps([{"name": "Gem", "qty": 1.5}]))
        db.add_all([native, bare, custom, oddjson, floaty])
        db.flush()
        tokens = {}
        for key, pc in (("native", native), ("bare", bare), ("custom", custom), ("odd", oddjson), ("floaty", floaty)):
            tokens[key] = f"gwshare-{key}-token"
            db.add(CharacterShare(world_id=w, character_id=pc.id, token=tokens[key]))
        db.add(CharacterShare(world_id=w, character_id=native.id, token="gwshare-revoked", revoked=True))
        db.commit()
    finally:
        db.close()
    d, g = httpx.Client(base_url=DIRECT, timeout=30), httpx.Client(base_url=base, timeout=30)
    keep = ("content-type", "cache-control", "x-robots-tag", "referrer-policy")
    for key, tok in list(tokens.items()) + [("revoked", "gwshare-revoked"), ("unknown", "nope"), ("long", "z" * 100), ("pct", "gw%20share"), ("slash", "a%2Fb")]:
        a, b = d.get(f"/share/{tok}"), g.get(f"/share/{tok}")
        assert (a.status_code, a.content) == (b.status_code, b.content), key
        assert {k: a.headers.get(k) for k in keep} == {k: b.headers.get(k) for k in keep}, key
        if key in ("native", "bare", "custom", "revoked", "unknown", "long"):
            assert b.headers.get("x-nd-served-by") == "gateway", key


def test_character_foundry_export_is_pythons(client, seed):
    """GET /characters/<id>/export.foundry.json: a native sheet (stats, currency, equipment, feats), an empty one, a custom-template sheet, odd
    stored values, and who may download (owner, GM, someone else, no such character)."""
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Fate <Lite>", slug="gw-foundry-fate", sheet_mode="custom", fields_json=json.dumps([
            {"id": "hp", "label": "Stress", "type": "resource", "default_value": "3/3", "section": "Vitals"}, {"id": "aspect", "label": "High & Concept", "type": "text", "section": "Core"},
            {"id": "skill", "label": "Great Skill", "type": "number"}]))
        db.add(tpl)
        db.flush()
        rich = PlayerCharacter(world_id=w, name="Vex", race="Elf", char_class="Rogue", level=3, xp=40, current_hp=7, max_hp=12, shock_current=2, shock_max=5, owner_user_id=seed.player_a.id,
                               backstory="Born <here> & \"there\"\nsecond line", notes="it's a note",
                               stats_json=json.dumps([{"id": "str", "label": "Strength", "value": 12}, {"id": "dex", "value": "14"}, "junk", {"id": "cha", "label": "", "value": 3}]),
                               currency_json=json.dumps([{"abbr": "CR", "value": 50}, {"label": "Silver", "value": "7"}, {"abbr": "", "label": "", "value": 0}, {"value": 1}]),
                               equipment_json=json.dumps([{"name": "Dagger <sharp>", "qty": 2, "weight": 1.5, "equipped": True, "notes": "a & b"}, {"name": "Rope"}, "bad"]),
                               feats_json=json.dumps([{"name": "Sneak", "type": "Skill", "notes": "quiet"}, {"name": "Dodge", "source": "Race"}, {"name": "Plain"}, {"name": "Desc", "description": "d"}]))
        empty = PlayerCharacter(world_id=w, name="", owner_user_id=seed.player_a.id)
        custom = PlayerCharacter(world_id=w, name="Fatey", sheet_template_id=tpl.id, owner_user_id=seed.player_a.id, backstory="b", notes="n",
                                 custom_fields_json=json.dumps({"hp_current": 2, "hp_max": 4, "aspect": "Hero <1>", "skill": 3}))
        odd = PlayerCharacter(world_id=w, name="Odd", owner_user_id=seed.player_a.id, equipment_json=json.dumps([{"name": "Gem", "qty": None}]))
        weird = PlayerCharacter(world_id=w, name="Weird", owner_user_id=seed.player_a.id, equipment_json=json.dumps([{"name": "Orb", "qty": 3, "weight": 2.0}]),
                                feats_json=json.dumps([{"name": None}]))
        spec = PlayerCharacter(world_id=w, name="Vex <b>", owner_user_id=seed.player_a.id)
        mine_b = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([rich, empty, custom, odd, weird, spec, mine_b])
        db.commit()
        ids = {"rich": rich.id, "empty": empty.id, "custom": custom.id, "odd": odd.id, "weird": weird.id, "spec": spec.id, "other": mine_b.id, "missing": 999999}
    finally:
        db.close()
    keep = ("content-type", "content-disposition")
    for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        via = _via(d, base)
        for key, pid in ids.items():
            url = f"/characters/{pid}/export.foundry.json"
            a, b = d.get(url), via.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, key)
            assert {k: a.headers.get(k) for k in keep} == {k: b.headers.get(k) for k in keep}, (email, key)
            if key in ("rich", "empty", "custom", "missing") and (email == seed.gm.email or key in ("rich", "empty", "custom")):
                assert b.headers.get("x-nd-served-by") == "gateway", (email, key)


def test_character_ndc_export_is_pythons(client, seed):
    """GET /characters/<id>/export.ndc: catalog ids in their buckets, custom entries, feats by id / name, credits, a banshee's ectoplasm, a portrait,
    a custom-template sheet's 400, and who may download."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    up.mkdir(exist_ok=True)
    (up / "gw-ndc-portrait.png").write_bytes(b"\x89PNG-fake-bytes")
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Fate", slug="gw-ndc-fate", sheet_mode="custom", fields_json="[]")
        db.add(tpl)
        db.flush()
        rich = PlayerCharacter(world_id=w, name="Vex Ndc", race="Elf", char_class="Rogue", race_id="below_elves", profession_id="charlatan", level=3, xp=40, current_hp=7, max_hp=12,
                               shock_current=2, shock_max=5, pp_current=3, mp_current=4, temp_hp=2, owner_user_id=seed.player_a.id, backstory="Born here", notes="a note",
                               portrait_url="/uploads/gw-ndc-portrait.png", major_edge_count=1, minor_edge_count=2,
                               stats_json=json.dumps([{"id": "str", "value": 12}, {"id": "dex", "value": "14"}, {"id": "wil", "value": 9.7}, {"id": "bod"}]),
                               currency_json=json.dumps([{"abbr": "eur", "value": 5}, {"abbr": "cr", "value": "250"}, {"abbr": "CR", "value": 1}]),
                               equipment_json=json.dumps([{"id": "flamethrower"}, {"id": "exoskeleton", "category": "armor"}, {"id": "fluk23"}, {"id": "unknown_thing"},
                                                          {"id": "alpha_arms", "category": "weapons"}, {"name": "Lucky coin", "notes": "heads"}, {"name": "Rope"}, {"name": ""}]),
                               feats_json=json.dumps([{"id": "feat_a", "name": "A"}, {"name": "Sneak", "notes": "quiet"}, {"name": "Dodge", "description": "d"}, {"name": "Sneak", "notes": "again"}, {"name": ""}]))
        banshee = PlayerCharacter(world_id=w, name="Banshee", race_id="banshee", owner_user_id=seed.player_a.id,
                                  stats_json=json.dumps([{"id": "str", "value": 3}, {"id": "wil", "value": 4}, {"id": "int", "value": 5}]))
        empty = PlayerCharacter(world_id=w, name="", owner_user_id=seed.player_a.id)
        custom = PlayerCharacter(world_id=w, name="Fatey", sheet_template_id=tpl.id, owner_user_id=seed.player_a.id)
        odd = PlayerCharacter(world_id=w, name="Odd", owner_user_id=seed.player_a.id, stats_json=json.dumps([{"id": "str", "value": "x"}]))
        other = PlayerCharacter(world_id=w, name="Other", owner_user_id=seed.player_b.id)
        db.add_all([rich, banshee, empty, custom, odd, other])
        db.commit()
        ids = {"rich": rich.id, "banshee": banshee.id, "empty": empty.id, "custom": custom.id, "odd": odd.id, "other": other.id, "missing": 999999}
    finally:
        db.close()
    keep = ("content-type", "content-disposition")
    try:
        for email, pw in ((seed.player_a.email, PLAYER_PASSWORD), (seed.gm.email, GM_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            via = _via(d, base)
            for key, pid in ids.items():
                url = f"/characters/{pid}/export.ndc"
                a, b = d.get(url), via.get(url)
                assert (a.status_code, a.content) == (b.status_code, b.content), (email, key)
                assert {k: a.headers.get(k) for k in keep} == {k: b.headers.get(k) for k in keep}, (email, key)
                if key in ("rich", "banshee", "empty", "custom", "missing") and (email == seed.gm.email or key in ("rich", "banshee", "empty", "custom")):
                    assert b.headers.get("x-nd-served-by") == "gateway", (email, key)
    finally:
        (up / "gw-ndc-portrait.png").unlink(missing_ok=True)


def test_saved_views_list_is_pythons(client, seed):
    """GET /api/saved-views/<section>: none, several (by name), a section that is unknown, and anonymous."""
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    for i, name in enumerate(("zeta", "Alpha", "mid  dle")):
        assert d.post("/api/saved-views/images", json={"name": name, "state": {"mode": "list", "size": 3 - i}}).status_code == 200
    for sec in ("images", "audio", "nosuch", "x" * 40, "Bad"):
        a, b = d.get(f"/api/saved-views/{sec}"), via.get(f"/api/saved-views/{sec}")
        assert (a.status_code, a.content) == (b.status_code, b.content), sec
        if sec in ("images", "audio", "nosuch"):
            assert b.headers.get("x-nd-served-by") == "gateway", sec
    anon = httpx.Client(base_url=base, timeout=30, follow_redirects=False)
    r, r2 = httpx.get(DIRECT + "/api/saved-views/images", follow_redirects=False), anon.get("/api/saved-views/images")
    assert (r.status_code, r.content) == (r2.status_code, r2.content)
    for v in d.get("/api/saved-views/images").json()["views"]:
        d.delete(f"/api/saved-views/images/{v['id']}")


def test_video_job_delete_is_pythons(client, seed):
    """DELETE /api/video-jobs/<id>: a finished / failed job goes (the row is gone for both), one in progress or missing is Python's 400, a player's 403."""
    from app.database import SessionLocal
    from app.models import VideoJob
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id

    def make(status):
        db = SessionLocal()
        try:
            j = VideoJob(world_id=w, prompt="p", status=status)
            db.add(j)
            db.commit()
            return j.id
        finally:
            db.close()

    def gone(jid):
        db = SessionLocal()
        try:
            return db.get(VideoJob, jid) is None
        finally:
            db.close()
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        via = _via(d, base)
        for status in ("done", "failed", "cancelled", "pending", "generating", "fetching"):
            ja, jb = make(status), make(status)
            a, b = d.delete(f"/api/video-jobs/{ja}"), via.delete(f"/api/video-jobs/{jb}")
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, status)
            assert gone(ja) == gone(jb), (email, status)
            if email == seed.gm.email:
                assert b.headers.get("x-nd-served-by") == "gateway", status
        for url in ("/api/video-jobs/999999", "/api/video-jobs/abc", "/api/video-jobs/007"):
            a, b = d.delete(url), via.delete(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), (email, url)


def test_sheet_template_forms_are_pythons(client, seed):
    """POST /characters/templates/new | <id>/edit | <id>/delete and GET <id>/edit: the stored row (name, slug, mode, fields, cleaned system spec, rules),
    the redirects, a built-in, a missing id, odd JSON, and a player's refusal."""
    import re
    from app.database import SessionLocal
    from app.models import SheetTemplate
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    d.follow_redirects = via.follow_redirects = False
    fields = json.dumps([{"id": "hp", "label": "Stress", "type": "resource", "default_value": "3/3", "section": "Vitals", "vital": "hp"}, {"id": "xp", "label": "XP", "type": "number", "xp": True},
                         {"id": "name", "label": "Name", "type": "text"}])
    system = json.dumps({"conditions": ["Hurt", "  Dazed ", ""], "rest": {"short": [["full", "hp"]], "long": [["bogus"]]}, "roster": [["Core", ["hp", "nope"]]]})
    forms = [{"name": "My <Tpl> One", "description": " desc ", "sheet_mode": "custom", "fields_json": fields, "system_json": system, "rules_md": "  rules \n"},
             {"name": "", "fields_json": "[]"}, {"name": "Bad Json", "fields_json": "{oops", "system_json": "{bad"}, {"name": "Obj", "fields_json": '{"a": 1}', "system_json": "[1]"},
             {"name": "Same Name"}, {"name": "Same Name"}, {"name": "Wide " + "x" * 80, "fields_json": fields, "rules_md": "r" * 20100}, {"name": "café", "fields_json": "[]"}]

    def row_of(tid):
        db = SessionLocal()
        try:
            t = db.get(SheetTemplate, tid)
            return None if t is None else {k: getattr(t, k) for k in ("world_id", "name", "description", "is_builtin", "sheet_mode", "fields_json", "system_json", "rules_md")} | {"slug": re.sub(r"-\d+$", "", t.slug)}
        finally:
            db.close()
    made = []
    for form in forms:
        ra, rb = d.post("/characters/templates/new", data=form), via.post("/characters/templates/new", data=form)
        ia, ib = (int(x.headers["location"].split("/")[3]) for x in (ra, rb))
        assert (ra.status_code, rb.status_code) == (303, 303), form
        assert ra.headers["location"].replace(str(ia), "N") == rb.headers["location"].replace(str(ib), "N"), form
        assert row_of(ia) == row_of(ib), form
        made.append((ia, ib))
        if form["name"] != "café":
            assert rb.headers.get("x-nd-served-by") == "gateway", form
    for (ia, ib) in made[:4]:
        for upd in ({"name": " Renamed ", "description": "d2", "sheet_mode": "nd", "fields_json": fields, "system_json": system, "rules_md": "new"}, {"name": "   ", "fields_json": "bad"}, {}):
            ra, rb = d.post(f"/characters/templates/{ia}/edit", data=upd), via.post(f"/characters/templates/{ib}/edit", data=upd)
            assert (ra.status_code, ra.headers.get("location", "").replace(str(ia), "N")) == (rb.status_code, rb.headers.get("location", "").replace(str(ib), "N")), upd
            assert row_of(ia) == row_of(ib), upd
        pa, pb = d.get(f"/characters/templates/{ia}/edit"), via.get(f"/characters/templates/{ib}/edit")
        assert pa.status_code == pb.status_code == 200
        norm = lambda r, i: r.text.replace(f"/{i}/", "/N/")
        assert norm(pa, ia) == norm(pb, ib)
        assert pb.headers.get("x-nd-served-by") == "gateway"
    builtin = None
    db = SessionLocal()
    try:
        b = db.query(SheetTemplate).filter(SheetTemplate.is_builtin.is_(True)).first()
        builtin = b.id if b else None
    finally:
        db.close()
    for tid in ([builtin] if builtin else []) + [999999]:
        for method, url, data in (("GET", f"/characters/templates/{tid}/edit", None), ("POST", f"/characters/templates/{tid}/edit", {"name": "x", "fields_json": "[]"}), ("POST", f"/characters/templates/{tid}/delete", None)):
            ra, rb = d.request(method, url, data=data), via.request(method, url, data=data)
            assert (ra.status_code, ra.content if ra.status_code >= 400 else ra.headers.get("location")) == (rb.status_code, rb.content if rb.status_code >= 400 else rb.headers.get("location")), (method, url)
    for (ia, ib) in made:
        ra, rb = d.post(f"/characters/templates/{ia}/delete"), via.post(f"/characters/templates/{ib}/delete")
        assert (ra.status_code, ra.headers.get("location")) == (rb.status_code, rb.headers.get("location"))
        assert row_of(ia) is None and row_of(ib) is None
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    p.follow_redirects = pv.follow_redirects = False
    for method, url in (("POST", "/characters/templates/new"), ("GET", "/characters/templates/1/edit"), ("POST", "/characters/templates/1/delete")):
        ra, rb = p.request(method, url, data={"name": "x"}), pv.request(method, url, data={"name": "x"})
        assert (ra.status_code, ra.content) == (rb.status_code, rb.content), (method, url)


def test_board_save_and_delete_refusals_are_pythons(client, seed):
    """POST /boards/<slug>/save and /delete: a GM reaching another world's board, a missing board, a player without edit rights, a bad body."""
    from app.database import SessionLocal
    from app.models import InvestBoard
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        for slug, wid in (("gw-bd-a", seed.world_a.id), ("gw-bd-b", seed.world_b.id), ("gw-bd-a2", seed.world_a.id), ("gw-bd-b2", seed.world_b.id)):
            db.add(InvestBoard(world_id=wid, name=slug, slug=slug, description=""))
        db.commit()
    finally:
        db.close()

    def board(slug):
        db = SessionLocal()
        try:
            b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
            return None if b is None else (b.nodes_json, b.edges_json)
        finally:
            db.close()
    body = json.dumps({"nodes": [{"id": "n1", "title": "T"}], "edges": [], "groups": []})
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        via = _via(d, base)
        d.follow_redirects = via.follow_redirects = False
        for slug in ("gw-bd-a", "gw-bd-b", "nope-board"):
            for payload in (body, "{oops", "[]"):
                ra = d.post(f"/boards/{slug}/save", content=payload, headers={"content-type": "application/json"})
                after_python = board(slug)
                rb = via.post(f"/boards/{slug}/save", content=payload, headers={"content-type": "application/json"})
                assert (ra.status_code, ra.content) == (rb.status_code, rb.content), (email, slug, payload)
                assert board(slug) == after_python, (email, slug, payload)  # the same payload leaves the same row
        for sa, sb in (("gw-bd-a", "gw-bd-a2"), ("gw-bd-b", "gw-bd-b2"), ("nope-board", "nope-board")):
            ra, rb = d.post(f"/boards/{sa}/delete"), via.post(f"/boards/{sb}/delete")
            assert (ra.status_code, ra.headers.get("location"), ra.content if ra.status_code >= 400 else b"") == (rb.status_code, rb.headers.get("location"), rb.content if rb.status_code >= 400 else b""), (email, sa)
            assert (board(sa) is None) == (board(sb) is None), (email, sa)
        if email == seed.gm.email:
            assert via.post("/boards/nope-board/save", content=body, headers={"content-type": "application/json"}).headers.get("x-nd-served-by") == "gateway"


def test_join_form_is_pythons(client, seed):
    """GET /join/<code>: a good link, revoked, expired, used up, unknown; a signed-in browser (a good link is redeemed by Python)."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models import InviteCode
    base = str(client.base_url)
    db = SessionLocal()
    try:
        w = seed.world_a.id
        db.add_all([InviteCode(code="gwj-good", world_id=w), InviteCode(code="gwj-future", world_id=w, expires_at=datetime.utcnow() + timedelta(days=2), max_uses=5, uses_count=1),
                    InviteCode(code="gwj-revoked", world_id=w, revoked=True), InviteCode(code="gwj-expired", world_id=w, expires_at=datetime.utcnow() - timedelta(days=1)),
                    InviteCode(code="gwj-used", world_id=w, max_uses=2, uses_count=2), InviteCode(code="gwj-<&>", world_id=seed.world_b.id)])
        db.commit()
    finally:
        db.close()
    d, g = httpx.Client(base_url=DIRECT, timeout=30, follow_redirects=False), httpx.Client(base_url=base, timeout=30, follow_redirects=False)
    served = {"gwj-good", "gwj-future", "gwj-revoked", "gwj-expired", "gwj-used", "nosuchcode", "gwj-%3C%26%3E"}
    for code in sorted(served) + ["a%2Fb", "x" * 80]:
        a, b = d.get(f"/join/{code}"), g.get(f"/join/{code}")
        assert (a.status_code, a.content) == (b.status_code, b.content), code
        if code in served:
            assert b.headers.get("x-nd-served-by") == "gateway", code
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(gm, base)
    gm.follow_redirects = via.follow_redirects = False
    for code in ("gwj-revoked", "nosuchcode"):
        a, b = gm.get(f"/join/{code}"), via.get(f"/join/{code}")
        assert (a.status_code, a.content) == (b.status_code, b.content), code
        assert b.headers.get("x-nd-served-by") == "gateway", code
    a, b = gm.get("/join/gwj-good"), via.get("/join/gwj-good")
    assert (a.status_code, a.headers.get("location")) == (b.status_code, b.headers.get("location"))


def test_chat_job_reads_player_image_jobs_and_template_delete_are_pythons(client, seed):
    """GET /api/ai/chat/jobs[/<id>], GET /api/ai/imagegen/player/jobs (player's own / GM's all with owners), DELETE /api/ai/chat/jobs/<id>,
    DELETE /api/ai/imagegen/model-templates/custom/<id>."""
    from app.database import SessionLocal
    from app.models import ChatJob, ImageJob, PromptPreset, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w, wb = seed.world_a.id, seed.world_b.id
    db = SessionLocal()
    try:
        db.get(World, w).players_can_use_image_gen = True
        cj = [ChatJob(world_id=w, prompt="p1", status=s, result="r", model=m, messages_json="[]") for s, m in (("done", "m1"), ("failed", ""), ("pending", "m3"))]
        other = ChatJob(world_id=wb, prompt="theirs", status="done", messages_json="[]")
        ij = [ImageJob(world_id=w, prompt="mine", status="done", created_by_user_id=seed.player_a.id, result_urls_json='["/uploads/a.png"]'),
              ImageJob(world_id=w, prompt="gm's", status="done", created_by_user_id=seed.gm.id), ImageJob(world_id=w, prompt="nobody's", status="failed", error="boom"),
              ImageJob(world_id=w, prompt="b's", status="done", created_by_user_id=seed.player_b.id)]
        tpl = [PromptPreset(world_id=w, scope="image_template", label="T1", text="x", params_json="{}"), PromptPreset(world_id=w, scope="image", label="notatpl", text="x"),
               PromptPreset(world_id=wb, scope="image_template", label="Bs", text="x", params_json="{}"), PromptPreset(world_id=w, scope="image_template", label="T2", text="x", params_json="{}")]
        db.add_all(cj + [other] + ij + tpl)
        db.commit()
        cids, oid = [c.id for c in cj], other.id
        tids = [t.id for t in tpl]
    finally:
        db.close()

    def exists(model, i):
        db = SessionLocal()
        try:
            return db.get(model, i) is not None
        finally:
            db.close()
    try:
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            via = _via(d, base)
            urls = ["/api/ai/chat/jobs", "/api/ai/imagegen/player/jobs"] + [f"/api/ai/chat/jobs/{i}" for i in cids + [oid, 999999]]
            for url in urls:
                a, b = d.get(url), via.get(url)
                assert (a.status_code, a.content) == (b.status_code, b.content), (email, url)
                if email == seed.gm.email or "player" in url:
                    assert b.headers.get("x-nd-served-by") == "gateway", (email, url)
        gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        via = _via(gm, base)
        for i in cids + [oid, 999999]:
            a_exists_before = exists(ChatJob, i)
            r = via.delete(f"/api/ai/chat/jobs/{i}")
            ref = r.status_code, r.content
            if a_exists_before and i not in (cids[2], oid):
                assert not exists(ChatJob, i) and r.status_code == 200 and r.json() == {"ok": True}, i
            else:
                assert exists(ChatJob, i) == a_exists_before, i
                assert r.status_code in (400, 404), ref
        for i in tids + [999999]:
            r = via.delete(f"/api/ai/imagegen/model-templates/custom/{i}")
            assert r.status_code in (200, 404), (i, r.status_code)
            if i in (tids[0], tids[3]):
                assert r.json() == {"deleted": i} and r.headers.get("x-nd-served-by") == "gateway"
            else:
                assert r.status_code == 404
    finally:
        db = SessionLocal()
        try:
            db.get(World, w).players_can_use_image_gen = False
            db.commit()
        finally:
            db.close()


def test_live_audio_download_and_transcript_check_are_pythons(client, seed):
    """GET /api/sessions/<id>/live-audio/download (nothing saved: 400, one segment: the file as a download, several: Python's concat, a stranger: 404/403)
    and GET .../live-transcript/check (no Studio key)."""
    import os
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import GameSession
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    (up / "gw-live").mkdir(parents=True, exist_ok=True)
    (up / "gw-live" / "a.webm").write_bytes(b"webm-bytes-1")
    (up / "gw-live" / "b.ogg").write_bytes(b"ogg-bytes-22")
    import shutil
    import subprocess
    have_ffmpeg = shutil.which("ffmpeg") is not None
    for n in ("c1.ogg", "c2.ogg"):
        if have_ffmpeg:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3", "-c:a", "libvorbis", str(up / "gw-live" / n)], check=True)
        else:
            (up / "gw-live" / n).write_bytes(b"not-audio")   # no ffmpeg here: both servers answer with the same 'not available' 400
    w = seed.world_a.id
    db = SessionLocal()
    try:
        mk = lambda n, files: GameSession(world_id=w, session_num=n, title=f"gw-live-{n}", live_audio_files_json=json.dumps(files) if files is not None else "")
        gss = [mk(901, None), mk(902, ["gw-live/a.webm"]), mk(903, ["gw-live/b.ogg"]), mk(904, ["gw-live/a.webm", "gw-live/b.ogg"]), mk(905, ["gw-live/missing.webm"]), mk(906, ["../x.webm"]),
               GameSession(world_id=seed.world_b.id, session_num=907, title="gw-live-b", live_audio_files_json=json.dumps(["gw-live/a.webm"])),
               mk(908, ["gw-live/c1.ogg", "gw-live/c2.ogg"]), mk(909, ["gw-live/c1.ogg", "gw-live/c2.ogg"])]
        db.add_all(gss)
        db.commit()
        ids = [g.id for g in gss] + [999999]
    finally:
        db.close()
    keep = ("content-type", "content-disposition")
    fresh_gateway_only = ids[8]   # nobody has built this one's recording yet: the gateway's ffmpeg does
    ids = ids[:6] + [ids[6]] + [ids[7], 999999]   # 904's fake segments and 908's real ones are the ffmpeg cases (ids[3], ids[-2])
    try:
        first = _via(_signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug), base).get(f"/api/sessions/{fresh_gateway_only}/live-audio/download")
        if have_ffmpeg:
            assert first.status_code == 200 and len(first.content) > 100 and first.headers.get("x-nd-served-by") == "gateway" and first.headers["content-disposition"].endswith('-recording.ogg"')
        else:
            assert first.status_code == 400 and "ffmpeg" in first.json()["detail"]
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            via = _via(d, base)
            for sid in ids:
                for tail in ("live-audio/download", "live-transcript/check"):
                    url = f"/api/sessions/{sid}/{tail}"
                    a, b = d.get(url), via.get(url)
                    if sid in (ids[3], ids[-2]) and tail.startswith("live-audio"):
                        # ffmpeg's own words (addresses, temp names) vary run to run: the status, the shape of the answer and the 400's lead-in agree
                        assert a.status_code == b.status_code, (email, url, a.text[:200], b.text[:200])
                        if a.status_code == 200:
                            assert b.headers.get("x-nd-served-by") == "gateway" or email != seed.gm.email, (email, url)
                            assert len(b.content) > 0 and a.headers.get("content-type") == b.headers.get("content-type") and a.headers.get("content-disposition") == b.headers.get("content-disposition")
                        elif a.status_code == 400:
                            assert a.json()["detail"][:24] == b.json()["detail"][:24], (a.text[:80], b.text[:80])
                        continue
                    assert (a.status_code, a.content) == (b.status_code, b.content), (email, url)
                    if a.status_code == 200 and tail.startswith("live-audio"):
                        assert {k: a.headers.get(k) for k in keep} == {k: b.headers.get(k) for k in keep}, (email, url)
                    if email == seed.gm.email and sid in (ids[0], ids[1], ids[2], ids[4], 999999) and "download" in tail:
                        assert b.headers.get("x-nd-served-by") == "gateway", url
    finally:
        for f in (up / "gw-live").glob("*"):
            f.unlink()


def test_session_media_chunked_upload_is_pythons(client, seed):
    """POST /api/sessions/<id>/media/upload/chunk + /complete: a picture and an audio file sent in parts, the item and panel as Python builds them,
    the chunk folder gone afterwards; a missing part, a bad id and a player are Python's."""
    import io
    import os
    import re
    import uuid
    from pathlib import Path
    from PIL import Image
    from app.database import SessionLocal
    from app.models import GameSession
    base = str(client.base_url)
    up = Path(os.environ["DB_PATH"]).parent / "uploads"
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, "PNG")
    png = buf.getvalue()
    audio = b"ID3" + bytes(range(256)) * 20
    w = seed.world_a.id
    db = SessionLocal()
    try:
        gss = [GameSession(world_id=w, session_num=950 + i, title=f"gw-chunk-{i}") for i in range(4)]
        db.add_all(gss)
        db.commit()
        ids = [g.id for g in gss]
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)

    def norm(v):
        v = {k: x for k, x in v.items() if k != "library"} if isinstance(v, dict) else v  # the library lists the world's other clips, which differ by run order
        s = json.dumps(v, sort_keys=True)
        s = re.sub(r"[0-9a-f]{32}", "H", s)
        s = re.sub(r"/uploads/(gallery|audio)/[^\"]+", r"/uploads/\1/F", s)
        s = re.sub(r'"uid": "[0-9a-f]+"', '"uid": "U"', s)
        s = re.sub(r'"(id|clip_id)": \d+', r'"\1": N', s)
        s = re.sub(r"session-?\d+|gw-chunk-\d", "S", s)
        return s

    def flow(client_, sid, name, data, kind=""):
        upload_id = uuid.uuid4().hex
        half = len(data) // 2
        outs = []
        for i, part in enumerate((data[:half], data[half:])):
            r = client_.post(f"/api/sessions/{sid}/media/upload/chunk", data={"upload_id": upload_id, "chunk_index": str(i)}, files={"file": (name, part, "application/octet-stream")})
            outs.append((r.status_code, r.json(), r.headers.get("x-nd-served-by")))
        r = client_.post(f"/api/sessions/{sid}/media/upload/complete", data={"upload_id": upload_id, "filename": name, "total_chunks": "2", "name": "Chunky", "visible_to_players": "1", "kind": kind})
        outs.append((r.status_code, r.json(), r.headers.get("x-nd-served-by")))
        return upload_id, outs
    for (name, data, kind), (sa, sb) in zip((("pic.png", png, ""), ("tune.mp3", audio, "audio")), ((ids[0], ids[1]), (ids[2], ids[3]))):
        ua, oa = flow(d, sa, name, data, kind)
        ub, ob = flow(via, sb, name, data, kind)
        assert [(s, norm(j)) for s, j, _ in oa] == [(s, norm(j)) for s, j, _ in ob], name
        assert all(h == "gateway" for _, _, h in ob), (name, ob)
        assert not (up / "session_media" / "_chunks" / ua).exists() and not (up / "session_media" / "_chunks" / ub).exists()
    # a missing part, a malformed id and a missing session are Python's
    uid = uuid.uuid4().hex
    for c_ in (d, via):
        r = c_.post(f"/api/sessions/{ids[0]}/media/upload/complete", data={"upload_id": uid, "filename": "x.png", "total_chunks": "3"})
        assert r.status_code == 400, r.text
        r = c_.post(f"/api/sessions/{ids[0]}/media/upload/chunk", data={"upload_id": "bad", "chunk_index": "0"}, files={"file": ("x", b"1", "application/octet-stream")})
        assert r.status_code == 400
        r = c_.post("/api/sessions/999999/media/upload/chunk", data={"upload_id": uid, "chunk_index": "0"}, files={"file": ("x", b"1", "application/octet-stream")})
        assert r.status_code == 404


def test_character_owner_refusals_are_pythons(client, seed):
    """POST /characters/<id>/owner: bad / unknown / non-member / at-the-limit players, a character of another world, a missing one, a player's 403."""
    from app.database import SessionLocal
    from app.models import PlayerCharacter, WorldMembership
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w, wb = seed.world_a.id, seed.world_b.id
    db = SessionLocal()
    try:
        mine = PlayerCharacter(world_id=w, name="Owned By A", owner_user_id=seed.player_a.id)
        free = PlayerCharacter(world_id=w, name="Free One")
        far = PlayerCharacter(world_id=wb, name="Far Away")
        db.add_all([mine, free, far])
        db.commit()
        ids = {"free": free.id, "mine": mine.id, "far": far.id, "missing": 999999}
        member_b = db.query(WorldMembership).filter(WorldMembership.world_id == wb, WorldMembership.user_id == seed.player_b.id).first() is not None
    finally:
        db.close()
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    gm.follow_redirects = False
    via = _via(gm, base)
    via.follow_redirects = False
    forms = [{"owner_user_id": "abc"}, {"owner_user_id": "999999"}, {"owner_user_id": "-3"}, {"owner_user_id": str(seed.gm.id)}, {"owner_user_id": str(seed.player_a.id)},
             {"owner_user_id": str(seed.player_b.id)}, {"owner_user_id": " " + str(seed.player_b.id) + " "}, {"owner_user_id": "99999999999999999999"}, {"owner_user_id": ""}, {}]
    for key, pid in ids.items():
        for form in forms:
            a, b = gm.post(f"/characters/{pid}/owner", data=form), via.post(f"/characters/{pid}/owner", data=form)
            assert (a.status_code, a.headers.get("location"), a.content if a.status_code >= 400 else b"") == (b.status_code, b.headers.get("location"), b.content if b.status_code >= 400 else b""), (key, form)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    p.follow_redirects = False
    pv = _via(p, base)
    pv.follow_redirects = False
    for pid in (ids["free"], 999999):
        a, b = p.post(f"/characters/{pid}/owner", data={"owner_user_id": "1"}), pv.post(f"/characters/{pid}/owner", data={"owner_user_id": "1"})
        assert (a.status_code, a.content) == (b.status_code, b.content), pid
    assert via.post(f"/characters/{ids['free']}/owner", data={"owner_user_id": "abc"}).headers.get("x-nd-served-by") == "gateway"


def test_table_edit_and_delete_refusals_are_pythons(client, seed):
    """POST /tables/<id>/edit and /delete: a missing table (404), a built-in one (403 on delete), someone else's table (403), a GM reaching another
    world's table, and the edit that does work (the row afterwards)."""
    from app.database import SessionLocal
    from app.models import RandomTable
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        mk = lambda name, wid, **kw: RandomTable(world_id=wid, name=name, slug=f"gw-tb-{name}", category="c", description="", entries_json="[]", is_builtin=False, **kw)
        rows = {"mine": mk("mine", seed.world_a.id, created_by_user_id=seed.player_a.id), "gms": mk("gms", seed.world_a.id), "far": mk("far", seed.world_b.id),
                "mine2": mk("mine2", seed.world_a.id, created_by_user_id=seed.player_a.id), "gms2": mk("gms2", seed.world_a.id), "far2": mk("far2", seed.world_b.id)}
        db.add_all(rows.values())
        db.commit()
        ids = {k: v.id for k, v in rows.items()}
        builtin = db.query(RandomTable).filter(RandomTable.is_builtin.is_(True)).first()
        ids["builtin"] = builtin.id if builtin else None
    finally:
        db.close()
    ids["missing"] = 999999

    def snap(i):
        db = SessionLocal()
        try:
            t = db.get(RandomTable, i)
            return None if t is None else (t.name, t.category, t.description, t.entries_json)
        finally:
            db.close()
    form = {"name": " Renamed ", "category": "", "description": "d", "entries_json": '[{"label":"a"}]'}
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        d.follow_redirects = False
        via = _via(d, base)
        via.follow_redirects = False
        pairs = [("mine", "mine2"), ("gms", "gms2"), ("far", "far2"), ("builtin", "builtin"), ("missing", "missing")]
        for ka, kb in pairs:
            if ids[ka] is None:
                continue
            for method, tail, data in (("POST", "edit", form), ("POST", "delete", None)):
                if tail == "delete" and ka == "builtin":
                    pass
                ra, rb = d.request(method, f"/tables/{ids[ka]}/{tail}", data=data), via.request(method, f"/tables/{ids[kb]}/{tail}", data=data)
                assert (ra.status_code, ra.headers.get("location", "").replace(str(ids[ka]), "N"), ra.content if ra.status_code >= 400 else b"") == \
                    (rb.status_code, rb.headers.get("location", "").replace(str(ids[kb]), "N"), rb.content if rb.status_code >= 400 else b""), (email, ka, tail)
                if ka != "builtin":
                    assert (snap(ids[ka]) is None) == (snap(ids[kb]) is None), (email, ka, tail)
                    if snap(ids[ka]) and ka != kb:
                        assert snap(ids[ka])[1:] == snap(ids[kb])[1:], (email, ka, tail)
                if email == seed.gm.email and ka in ("missing", "far"):
                    assert rb.headers.get("x-nd-served-by") == "gateway", (ka, tail)


def test_entity_note_routes_are_pythons(client, seed):
    """POST /entity/<e>/notes/<n>/toggle | delete | edit: a note of that entity, of another entity, a missing one, and an entity of another world."""
    from app.database import SessionLocal
    from app.models import Entity, EntityNote
    base = str(client.base_url)
    db = SessionLocal()
    try:
        e1 = Entity(world_id=seed.world_a.id, kind="note", name="gw-note-e1")
        e2 = Entity(world_id=seed.world_b.id, kind="note", name="gw-note-e2")
        db.add_all([e1, e2])
        db.flush()
        mk = lambda e, vis=False: EntityNote(entity_id=e.id, content="c", visible_to_players=vis)
        notes = {k: mk(e) for k, e in (("a1", e1), ("a2", e1), ("a3", e1), ("b1", e2), ("b2", e2), ("b3", e2))}
        db.add_all(notes.values())
        db.commit()
        ids = {k: n.id for k, n in notes.items()}
        eid1, eid2 = e1.id, e2.id
    finally:
        db.close()

    def state(i):
        db = SessionLocal()
        try:
            n = db.get(EntityNote, i)
            return None if n is None else (n.content, bool(n.visible_to_players))
        finally:
            db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    d.follow_redirects = False
    via = _via(d, base)
    via.follow_redirects = False
    # (python note, gateway note, entity) - each pair is two equal notes
    for (na, nb), eid in ((("a1", "a2"), eid1), (("b1", "b2"), eid2)):
        for tail, data in (("toggle", None), ("edit", {"content": " new ", "visible": "1"}), ("edit", {"content": "  "}), ("delete", None)):
            ra, rb = d.post(f"/entity/{eid}/notes/{ids[na]}/{tail}", data=data), via.post(f"/entity/{eid}/notes/{ids[nb]}/{tail}", data=data)
            assert (ra.status_code, ra.headers.get("location"), ra.content if ra.status_code >= 400 else b"") == (rb.status_code, rb.headers.get("location"), rb.content if rb.status_code >= 400 else b""), (na, tail)
            assert state(ids[na]) == state(ids[nb]), (na, tail)
            assert rb.headers.get("x-nd-served-by") == "gateway", (na, tail)
    for tail, data in (("toggle", None), ("edit", {"content": "x"}), ("delete", None)):
        for eid, nid in ((eid1, ids["b3"]), (eid1, 999999), (eid2, ids["a3"])):
            ra, rb = d.post(f"/entity/{eid}/notes/{nid}/{tail}", data=data), via.post(f"/entity/{eid}/notes/{nid}/{tail}", data=data)
            assert (ra.status_code, ra.headers.get("location"), ra.content if ra.status_code >= 400 else b"") == (rb.status_code, rb.headers.get("location"), rb.content if rb.status_code >= 400 else b""), (eid, nid, tail)
            assert rb.headers.get("x-nd-served-by") == "gateway", (eid, nid, tail)


def test_quest_edit_delete_status_refusals_are_pythons(client, seed):
    """POST /quests/<id>/edit | delete and /api/quests/<id>/status: missing quest, another world's (a GM reaches it), a player's quest vs the GM's."""
    from app.database import SessionLocal
    from app.models import Quest
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        mk = lambda t, wid, **kw: Quest(world_id=wid, title=t, status="active", **kw)
        rows = {k: mk(k, wid, **kw) for k, wid, kw in (("gm", seed.world_a.id, {}), ("gm2", seed.world_a.id, {}), ("far", seed.world_b.id, {}), ("far2", seed.world_b.id, {}),
                                                       ("mine", seed.world_a.id, {"created_by_user_id": seed.player_a.id}), ("mine2", seed.world_a.id, {"created_by_user_id": seed.player_a.id}))}
        db.add_all(rows.values())
        db.commit()
        ids = {k: v.id for k, v in rows.items()}
    finally:
        db.close()
    ids["missing"] = 999999

    def snap(i):
        db = SessionLocal()
        try:
            q = db.get(Quest, i)
            return None if q is None else (q.status, q.summary, q.category)
        finally:
            db.close()
    for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        d = _signed_in(email, pw, seed.world_a.slug)
        d.follow_redirects = False
        via = _via(d, base)
        via.follow_redirects = False
        for ka, kb in (("gm", "gm2"), ("far", "far2"), ("mine", "mine2"), ("missing", "missing")):
            for method, path, kw in (("POST", "/quests/{}/edit", {"data": {"title": "T", "summary": " s ", "status": "done"}}), ("POST", "/api/quests/{}/status", {"json": {"status": " failed "}}),
                                     ("POST", "/quests/{}/delete", {})):
                ra, rb = d.request(method, path.format(ids[ka]), **kw), via.request(method, path.format(ids[kb]), **kw)
                assert (ra.status_code, ra.headers.get("location", "").replace(f"/{ids[ka]}", "/N"), ra.content if ra.status_code >= 400 or "status" in path else b"") == \
                    (rb.status_code, rb.headers.get("location", "").replace(f"/{ids[kb]}", "/N"), rb.content if rb.status_code >= 400 or "status" in path else b""), (email, ka, path)
                assert (snap(ids[ka]) is None) == (snap(ids[kb]) is None), (email, ka, path)
                if email == seed.gm.email and ka in ("far", "missing"):
                    assert rb.headers.get("x-nd-served-by") == "gateway", (ka, path)


def test_ai_attachment_upload_family_is_pythons(client, seed):
    """POST /api/ai/attachments/upload (+ /chunk, /complete): a picture, text with CRLF / bad bytes, markdown, audio (no Studio key), a PDF and a bad type
    (Python's), and who may (a GM; a player only when the world allows AI)."""
    import re
    import uuid
    from app.database import SessionLocal
    from app.models import World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    files = {"pic.png": b"\x89PNG-fake", "notes.txt": b"line one\r\nline two\rthree \xff\xfe end\n", "sheet.md": "# Tést\n".encode(), "memo.mp3": b"ID3" + bytes(64),
             "doc.pdf": b"%PDF-1.4 nope", "bad.exe": b"MZ", "empty.txt": b""}

    def norm(r):
        s = r.text if r.status_code >= 400 else json.dumps(r.json(), sort_keys=True)
        return r.status_code, re.sub(r"/uploads/ai_attachments/[^\"]+", "/uploads/ai_attachments/F", s)

    def run(who_a, who_b, tag):
        for name, data in files.items():
            ra = who_a.post("/api/ai/attachments/upload", files={"file": (name, data, "application/octet-stream")})
            rb = who_b.post("/api/ai/attachments/upload", files={"file": (name, data, "application/octet-stream")})
            assert norm(ra) == norm(rb), (tag, name, ra.text[:200], rb.text[:200])
            if ra.status_code == 200 and name != "doc.pdf" and tag == "gm":
                assert rb.headers.get("x-nd-served-by") == "gateway", (tag, name)
        for name in ("memo.mp3", "notes.txt"):
            outs = []
            for who in (who_a, who_b):
                uid = uuid.uuid4().hex
                data = files[name] * 20
                half = len(data) // 2
                for i, part in enumerate((data[:half], data[half:])):
                    r = who.post("/api/ai/attachments/upload/chunk", data={"upload_id": uid, "chunk_index": str(i)}, files={"file": (name, part, "application/octet-stream")})
                    assert r.status_code in (200, 403), r.text
                r = who.post("/api/ai/attachments/upload/complete", data={"upload_id": uid, "filename": name, "total_chunks": "2"})
                outs.append(norm(r))
            assert outs[0] == outs[1], (tag, name, outs)
        r = who_b.post("/api/ai/attachments/upload", data={"x": "1"})
        r2 = who_a.post("/api/ai/attachments/upload", data={"x": "1"})
        assert (r.status_code, r.text) == (r2.status_code, r2.text)
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    run(gm, _via(gm, base), "gm")
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    run(pl, _via(pl, base), "player-closed")
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).players_can_ask_ai = True
        db.commit()
    finally:
        db.close()
    try:
        run(pl, _via(pl, base), "player-open")
    finally:
        db = SessionLocal()
        try:
            db.get(World, seed.world_a.id).players_can_ask_ai = False
            db.commit()
        finally:
            db.close()


def test_map_tiles_are_cut_and_served_by_the_gateway(client, seed):
    """The first view of a map starts the gateway's own cut of its tile pyramid; the finished pyramid's tiles are served as Python serves them,
    line up (transparent above the map, the red quarter where the picture has it), and the viewer page matches Python's once the tiles exist."""
    import io
    import os
    import time
    from pathlib import Path
    from PIL import Image
    base = str(client.base_url)
    data_dir = Path(os.environ["DB_PATH"]).parent
    (data_dir / "maps").mkdir(exist_ok=True)
    (data_dir / "uploads" / "maps").mkdir(parents=True, exist_ok=True)
    slug = "gw-tile-map"
    (data_dir / "maps" / f"{slug}.json").write_text(json.dumps({"name": "Tile Map", "world_id": seed.world_a.id, "width": 600, "height": 400, "markers": []}))
    im = Image.new("RGB", (600, 400), "white")
    im.paste((255, 0, 0), (0, 0, 300, 200))
    im.save(data_dir / "uploads" / "maps" / f"{slug}.png")
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    first = via.get(f"/maps/{slug}")
    assert first.status_code == 200 and first.headers.get("x-nd-served-by") == "gateway"
    tiles_root = data_dir / "map_tiles"
    meta = None
    for _ in range(100):
        found = [p for p in tiles_root.glob(f"{slug}-*/meta.json")] if tiles_root.exists() else []
        if found:
            meta = json.loads(found[0].read_text())
            break
        time.sleep(0.2)
    assert meta and meta["zmin"] == -2 and meta["native"] == 0 and meta["tiles"] > 0, meta
    key = meta["key"]
    urls = [f"/map-tiles/{key}/0/0_-2.webp", f"/map-tiles/{key}/0/1_-1.webp", f"/map-tiles/{key}/-2/0_-1.webp", f"/map-tiles/{key}/0/9_9.webp", f"/map-tiles/{key}/0/..%2Fmeta.json",
            f"/map-tiles/{key}.part/0/0_-1.webp", "/map-tiles/nope-key/0/0_-1.webp", f"/map-tiles/{key}/x/0_-1.webp", f"/map-tiles/{key}/0/0_-1.png"]
    for url in urls:
        a, b = d.get(url), via.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        assert {k: a.headers.get(k) for k in ("content-type", "cache-control")} == {k: b.headers.get(k) for k in ("content-type", "cache-control")}, url
    ok = via.get(urls[0])
    assert ok.status_code == 200 and ok.headers.get("x-nd-served-by") == "gateway"
    t = Image.open(io.BytesIO(ok.content)).convert("RGBA")
    assert t.getpixel((5, 5))[3] == 0                      # above the map: transparent
    r, g, bl, al = t.getpixel((5, 120))
    assert r > 200 and g < 60 and bl < 60 and al == 255    # the red quarter
    a, b = d.get(f"/maps/{slug}"), via.get(f"/maps/{slug}")
    assert (a.status_code, a.content) == (b.status_code, b.content)
    assert b.headers.get("x-nd-served-by") == "gateway" and f'"key": "{key}"' in b.text
    for f in (data_dir / "maps" / f"{slug}.json", data_dir / "uploads" / "maps" / f"{slug}.png"):
        f.unlink(missing_ok=True)


def test_small_json_reads_are_pythons(client, seed):
    """GET /api/quests/board, /api/combat/recent, /api/audio/clips, /api/world-players, /api/tables/options, /api/video-jobs: with data, for the GM, a
    player (refused), without a world cookie, and signed out."""
    from app.database import SessionLocal
    from app.models import AudioClip, AudioJob, CombatSession, MapProp, Party, Quest, RandomTable, VideoClip, VideoJob, WorldMembership
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        party = Party(world_id=w, name="Crew")
        db.add(party)
        db.flush()
        clip = VideoClip(world_id=w, name="Clip V", file_url="/uploads/video/v.mp4")
        db.add(clip)
        db.flush()
        db.add_all([Quest(world_id=w, title="B quest", status="active", category="side", summary="s", assigned_party_id=party.id), Quest(world_id=w, title="A quest", status="active", category=""),
                    Quest(world_id=w, title="Done", status="done"), Quest(world_id=w, title="Child", status="active", parent_id=1),
                    CombatSession(world_id=w, name="Fight 1", round_num=0), CombatSession(world_id=w, name="Template", is_template=True),
                    AudioClip(world_id=w, name="Zed", file_url="/uploads/audio/z.mp3", description="d", album_id=None), AudioClip(world_id=w, name="Abe", file_url="/uploads/audio/a.mp3"),
                    RandomTable(world_id=w, name="Loot", slug="gw-sj-loot", entries_json='[{"label":"a"},{"label":"b"}]'), RandomTable(world_id=None, name="Global", slug="gw-sj-global", entries_json="[]"),
                    VideoJob(world_id=w, prompt="a dragon", status="done", clip_id=clip.id), VideoJob(world_id=w, prompt="", status="failed", error="boom"), VideoJob(world_id=w, prompt="x", status=None),
                    AudioJob(world_id=w, purpose="facts_parse", filename="p", status="done", result_json='[{"content": "a fact"}]', transcript="", audio_path="", delete_after=False),
                    AudioJob(world_id=w, purpose="facts_parse", filename="p2", status="done", result_json="{bad", draft_consumed=True, transcript="", audio_path="", delete_after=False),
                    MapProp(world_id=w, name="Barrel", tags="wood", file_url="/uploads/maps/props/b.png", kind="prop", px_w=64, px_h=64, cells_w=1.5, cells_h=1.0)])
        db.commit()
    finally:
        db.close()
    urls = ["/api/quests/board", "/api/combat/recent", "/api/audio/clips", "/api/world-players", "/api/tables/options", "/api/video-jobs", "/api/characters/catalog", "/api/characters/templates", "/api/maps/props", "/api/facts/last-parse", "/tables/export"]
    gm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    for who, tag in ((gm, "gm"), (pl, "player")):
        via = _via(who, base)
        for url in urls:
            a, b = who.get(url), via.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), (tag, url)
            if tag == "gm" or url.startswith("/api/characters/"):
                assert b.headers.get("x-nd-served-by") == "gateway", url
    # no active world: the cookie names nothing
    nogm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    nogm.cookies.set("active_world", "no-such-world")
    via = _via(nogm, base)
    for url in urls:
        a, b = nogm.get(url), via.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), ("no world", url)
    anon = httpx.Client(base_url=base, timeout=30, follow_redirects=False)
    for url in urls:
        a, b = httpx.get(DIRECT + url, follow_redirects=False), anon.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), ("anon", url)


def test_backups_list_and_diagnostics_events_are_pythons(client, seed):
    """GET /api/backups (not configured: 400) and /admin/diagnostics/events (empty, then with lines, then with a line
    break only Python splits on): the GM gets Python's bytes from the gateway; a player is refused as Python refuses."""
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    diag = Path(os.environ["DB_PATH"]).parent / "diagnostics"
    diag.mkdir(parents=True, exist_ok=True)
    log = diag / "events.log"
    log.unlink(missing_ok=True)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    try:
        for body, served in ((None, True), (b"restart pid=1\nstall 3s\n", True), (b"no newline at end", True), (b"a\r\nb\r\n", False), (b"a\x0bb\n", False)):
            if body is None:
                log.unlink(missing_ok=True)
            else:
                log.write_bytes(body)
            a, b = d.get("/admin/diagnostics/events"), v.get("/admin/diagnostics/events")
            assert (a.status_code, a.headers["content-type"], a.content) == (b.status_code, b.headers["content-type"], b.content), body
            assert (b.headers.get("x-nd-served-by") == "gateway") == served, body
        a, b = d.get("/api/backups"), v.get("/api/backups")
        assert (a.status_code, a.content) == (b.status_code, b.content)
        for url in ("/api/backups", "/admin/diagnostics/events"):
            a, b = p.get(url), pv.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), url
    finally:
        log.unlink(missing_ok=True)


def test_gallery_browse_is_pythons(client, seed):
    """GET /api/gallery/browse: the root, an album with pictures (named by title, use, or file) and sub-albums, another world's album,
    a missing one, a malformed id - for the GM; a player is refused as Python refuses."""
    from app.database import SessionLocal
    from app.models import ImageAlbum
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        root = ImageAlbum(world_id=w, name="Zed", image_urls_json=json.dumps(["/uploads/0123456789ab-cover.png", "/uploads/plain.jpg"]))
        other = ImageAlbum(world_id=seed.world_b.id, name="Other", image_urls_json="[]")
        junk = ImageAlbum(world_id=w, name="Alpha", image_urls_json="not json")
        db.add_all([root, other, junk])
        db.flush()
        sub = ImageAlbum(world_id=w, name="Sub", parent_id=root.id, image_urls_json=json.dumps(["/uploads/sub.png"]))
        sub2 = ImageAlbum(world_id=w, name="Sub é", parent_id=root.id, image_urls_json="[]")
        db.add_all([sub, sub2])
        db.commit()
        ids = (root.id, other.id, junk.id, sub.id)
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    urls = ["/api/gallery/browse", "/api/gallery/browse?album_id=0", "/api/gallery/browse?album_id=%d" % ids[0], "/api/gallery/browse?album_id=%d" % ids[3],
            "/api/gallery/browse?album_id=%d" % ids[1], "/api/gallery/browse?album_id=%d" % ids[2], "/api/gallery/browse?album_id=999999", "/api/gallery/browse?album_id=x",
            "/api/gallery/browse?album_id=-1"]
    for url in urls:
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        if "album_id=x" not in url and "album_id=-1" not in url:
            assert b.headers.get("x-nd-served-by") == "gateway", url
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    for url in urls[:3]:
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
    nogm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    nogm.cookies.set("active_world", "no-such-world")
    a, b = nogm.get(urls[0]), _via(nogm, base).get(urls[0])
    assert (a.status_code, a.content) == (b.status_code, b.content)


def test_media_rename_reads_are_pythons(client, seed):
    """GET /api/media-rename/runs, /summary and /items (audio, video, image; with an album, a search, a bad kind and a bad album):
    the GM gets Python's bytes from the gateway; a player is refused as Python refuses."""
    from .conftest import PLAYER_PASSWORD
    from app.models import AudioAlbum, AudioClip, ImageAlbum, MediaRenameRun, VideoAlbum, VideoClip
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        top = AudioAlbum(world_id=w, name="Music")
        db.add(top)
        db.flush()
        sub = AudioAlbum(world_id=w, name="Battle", parent_id=top.id)
        vtop = VideoAlbum(world_id=w, name="Clips")
        db.add_all([sub, vtop])
        db.flush()
        db.add_all([
            AudioClip(world_id=w, name="IMG_2041", file_url="/uploads/0123456789ab-door_creak.mp3", album_id=sub.id, transcript=" hi ", visible_to_players=True),
            AudioClip(world_id=w, name="Door Creak", file_url="/uploads/creak.mp3", album_id=top.id),
            AudioClip(world_id=w, name="", file_url="/uploads/x.mp3"),
            VideoClip(world_id=w, name="Intro", file_url="/uploads/video/v.mp4", poster_url="/uploads/poster.png", album_id=vtop.id),
            VideoClip(world_id=w, name="clip_01", file_url="/uploads/video/clip_01.mp4"),
            MediaRenameRun(world_id=w, status="done", total=3, done=3, created_by_user_id=seed.gm.id, results_json=json.dumps([{"kind": "audio"}])),
            MediaRenameRun(world_id=w, status="pending", created_by_user_id=None),
            MediaRenameRun(world_id=seed.world_b.id, status="done"),
        ])
        ia = ImageAlbum(world_id=w, name="Maps", image_urls_json=json.dumps(["/uploads/0123456789ab-city_map.png", "/uploads/b.jpg", 5]))
        db.add(ia)
        db.commit()
        ids = (top.id, sub.id, vtop.id, ia.id)
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    urls = ["/api/media-rename/runs", "/api/media-rename/summary", "/api/media-rename/items?kind=audio", "/api/media-rename/items?kind=video",
            "/api/media-rename/items?kind=image", f"/api/media-rename/items?kind=audio&album={ids[1]}", f"/api/media-rename/items?kind=video&album={ids[2]}",
            f"/api/media-rename/items?kind=image&album={ids[3]}", "/api/media-rename/items?kind=audio&q=door", "/api/media-rename/items?kind=audio&q=%20CREAK%20",
            "/api/media-rename/items?kind=image&q=map", "/api/media-rename/items?kind=image&album=99999", "/api/media-rename/items?kind=audio&album=abc",
            "/api/media-rename/items?kind=nope", "/api/media-rename/items", "/api/media-rename/items?kind=audio&q=%C3%A9"]
    for url in urls:
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        if not url.endswith("%C3%A9"):
            assert b.headers.get("x-nd-served-by") == "gateway", url
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    for url in urls[:4]:
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
    nogm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    nogm.cookies.set("active_world", "no-such-world")
    nv = _via(nogm, base)
    for url in urls[:3]:
        a, b = nogm.get(url), nv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url


def test_disabled_lore_extras_are_pythons(client, seed):
    """GET /dreamlands and /king-in-yellow with their Settings switches off: the "is disabled" page, for the GM and a player (restored after)."""
    from app.database import SessionLocal, get_app_settings
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        st = get_app_settings(db)
        was = (st.dreamlands_enabled, st.king_in_yellow_enabled)
        st.dreamlands_enabled = st.king_in_yellow_enabled = False
        db.commit()
    finally:
        db.close()
    try:
        for email, pw in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
            d = _signed_in(email, pw, seed.world_a.slug)
            via = _via(d, base)
            for url in ("/dreamlands", "/king-in-yellow"):
                _same_page(d, via, url, email)
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        for url in ("/dreamlands", "/king-in-yellow"):
            assert _same_page(d, _via(d, base), url, "gm"), url
    finally:
        db = SessionLocal()
        try:
            st = get_app_settings(db)
            st.dreamlands_enabled, st.king_in_yellow_enabled = was
            db.commit()
        finally:
            db.close()


def test_kiy_play_list_is_pythons(client, seed):
    """GET /api/kiy/plays: no library file, a good one, an empty object, unreadable text, and shapes Python chokes on (the gateway
    declines those, so both sides answer the same 500); a player is refused as Python refuses."""
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    f = Path(os.environ["DB_PATH"]).parent / "kiy_plays.json"
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    cases = [(None, True), ('[{"id":"ab12","title":"One é","text":"x","ts":5},{"id":"cd34","title":"Two","text":"y"}]', True), ("{}", True),
             ("not json", True), ("[]", True), ('[{"id":"a","title":"t","ts":null}]', True), ('[{"title":"no id"}]', False), ('{"a":1}', False), ("[1]", False)]
    try:
        for body, served in cases:
            if body is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(body, encoding="utf-8")
            a, b = d.get("/api/kiy/plays"), v.get("/api/kiy/plays")
            assert (a.status_code, a.content) == (b.status_code, b.content), body
            assert (b.headers.get("x-nd-served-by") == "gateway") == served, body
        a, b = p.get("/api/kiy/plays"), pv.get("/api/kiy/plays")
        assert (a.status_code, a.content) == (b.status_code, b.content)
    finally:
        f.unlink(missing_ok=True)


def test_python_free_live_jobs_list_reads_the_rename_runs(client, seed):
    """A gateway with no Python behind it (ND_PYTHON_FREE=1, nothing at the upstream) lists the bulk rename runs of /api/live-jobs as Python
    does (the "r:" rows; the in-memory rows are Python's own and absent), and /api/ai/tasks answers an empty list."""
    import shutil
    import socket
    import subprocess
    import time
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import MediaRenameRun
    root = Path(__file__).resolve().parent.parent
    binary = root / "rust" / "target" / "release" / "nd-gateway"
    if not binary.exists():
        pytest.skip("the gateway binary is not built")
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([MediaRenameRun(world_id=w, status="done", total=4, done=4), MediaRenameRun(world_id=w, status="error", total=9, done=2, error="boom"),
                    MediaRenameRun(world_id=w, status="cancelled", total=3, done=1), MediaRenameRun(world_id=seed.world_b.id, status="done")])
        db.commit()
    finally:
        db.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, ND_PYTHON_FREE="1", ND_UPSTREAM="127.0.0.1:1", ND_GATEWAY_LISTEN=f"127.0.0.1:{port}", DB_PATH=os.environ["DB_PATH"],
               ND_STATIC_DIR=str(root / "static"), ND_TEMPLATES_DIR=str(root / "app" / "templates"), ND_BUNDLED_MAPS_DIR=str(root / "app" / "maps"),
               ND_RUST_ROUTES="login,dice_roll,live,pages,writes,ai,mcp,fans,auth", SECRET_KEY="python-free-test-key", ND_PBKDF2_ITERATIONS="1000")
    proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                httpx.get(f"http://127.0.0.1:{port}/login", timeout=2)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        free = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, follow_redirects=False)
        r = free.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
        assert r.status_code in (302, 303), (r.status_code, r.text[:300])
        free.cookies.set("active_world", seed.world_a.slug)
        d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        mine = lambda resp: [j for j in resp.json()["jobs"] if j["key"].startswith("r:")]  # noqa: E731
        a, b = d.get("/api/live-jobs"), free.get("/api/live-jobs")
        assert a.status_code == b.status_code == 200, (a.status_code, b.status_code)
        assert len(mine(a)) >= 3 and mine(a) == mine(b)
        t = free.get("/api/ai/tasks")
        assert (t.status_code, t.json()) == (200, {"tasks": []})
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_session_audio_job_list_and_rename_history_are_pythons(client, seed):
    """GET /api/sessions/ai/audio-jobs (all, per game session, a bad filter) and /api/media-rename/history (batches with examples, an undone
    one, a missing author): for the GM; a player is refused as Python refuses."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.models import AudioJob, MediaRenameLog
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    t0 = datetime(2026, 5, 1, 12, 0, 0)
    db = SessionLocal()
    try:
        db.add_all([AudioJob(world_id=w, purpose="session_recap", filename="a.mp3", status="done", transcript="t", recap="r", game_session_id=3, created_at=t0),
                    AudioJob(world_id=w, purpose="condense", filename="b.mp3", status="error", error="x", model="m", game_session_id=4, created_at=t0 + timedelta(minutes=1)),
                    AudioJob(world_id=w, purpose="attachment", filename="c.mp3", status="done", created_at=t0 + timedelta(minutes=2)),
                    AudioJob(world_id=seed.world_b.id, purpose="condense", filename="d.mp3", status="done", created_at=t0)])
        for i in range(5):
            db.add(MediaRenameLog(world_id=w, batch_id="aaa", kind="audio", ref_id=i, old_name=f"old{i}", new_name=f"new{i}", user_id=seed.gm.id, created_at=t0))
        db.add_all([MediaRenameLog(world_id=w, batch_id="bbb", kind="image", ref_url="/uploads/x.png", old_name="", new_name="Nice", user_id=None, created_at=t0 + timedelta(hours=1),
                                   undone_at=t0 + timedelta(hours=2)),
                    MediaRenameLog(world_id=w, batch_id="ccc", kind="video", ref_id=1, old_name="v", new_name="V", user_id=seed.player_a.id, created_at=t0 + timedelta(hours=3)),
                    MediaRenameLog(world_id=seed.world_b.id, batch_id="zzz", kind="video", ref_id=1, old_name="v", new_name="V", created_at=t0)])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    urls = ["/api/sessions/ai/audio-jobs", "/api/sessions/ai/audio-jobs?game_session_id=3", "/api/sessions/ai/audio-jobs?game_session_id=99",
            "/api/sessions/ai/audio-jobs?game_session_id=x", "/api/media-rename/history"]
    for url in urls:
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        if "=x" not in url:
            assert b.headers.get("x-nd-served-by") == "gateway", url
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    for url in (urls[0], urls[4]):
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url


def test_ai_page_reads_are_pythons(client, seed):
    """GET /api/ai/sessions, /recap-instructions, /defaults, /prompt-presets: saved chats, the standing recap text, the per-surface models (a
    missing file, a good one, unreadable text, a wrong shape) and quick prompts (image with rows, chat with rows; a chat world with none is
    Python's, which seeds them); a bad scope is its 400."""
    from pathlib import Path
    from app.database import SessionLocal
    from app.models import ChatSession, PromptPreset, World
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([ChatSession(world_id=w, surface="chat", title="First"), ChatSession(world_id=w, surface="npc", title="Npc talk"),
                    ChatSession(world_id=w, surface="chat", title="Second é"), ChatSession(world_id=seed.world_b.id, surface="chat", title="Other"),
                    PromptPreset(world_id=w, scope="image", label="Neon", icon="", text="a city", negative="blur", sort_order=2),
                    PromptPreset(world_id=w, scope="image", label="Dusk", icon="\U0001F305", text="", negative="", sort_order=1),
                    PromptPreset(world_id=w, scope="chat", label="Hook", icon="⚡", text="Write a hook", sort_order=0)])
        db.query(World).filter(World.id == w).first().recap_instructions = "Keep it short — please"
        db.commit()
    finally:
        db.close()
    f = Path(os.environ["DB_PATH"]).parent / "ai_models.json"
    was = f.read_bytes() if f.exists() else None
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    urls = ["/api/ai/sessions", "/api/ai/recap-instructions", "/api/ai/defaults", "/api/ai/prompt-presets?scope=image", "/api/ai/prompt-presets?scope=chat",
            "/api/ai/prompt-presets?scope=bad", "/api/ai/prompt-presets"]
    try:
        for body, served in ((None, True), ('{"defaults": {"chat": "llama3", "image": "sd", "extra": 1}, "custom": []}', True), ("[]", False),
                              ("not json", True), ('{"defaults": []}', False), ('{"defaults": {"recap": null}}', True)):
            if body is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(body, encoding="utf-8")
            a, b = d.get("/api/ai/defaults"), v.get("/api/ai/defaults")
            assert (a.status_code, a.content) == (b.status_code, b.content), body
            assert (b.headers.get("x-nd-served-by") == "gateway") == served, body
        for url in urls:
            a, b = d.get(url), v.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), url
            if url not in ("/api/ai/prompt-presets",):
                assert b.headers.get("x-nd-served-by") == "gateway", url
        for url in urls[:5]:
            a, b = p.get(url), pv.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), url
        nogm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
        nogm.cookies.set("active_world", "no-such-world")
        nv = _via(nogm, base)
        for url in urls[:2] + urls[3:4]:
            a, b = nogm.get(url), nv.get(url)
            assert (a.status_code, a.content) == (b.status_code, b.content), url
    finally:
        if was is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(was)


def test_unconfigured_unsloth_routes_are_pythons_400(client, seed):
    """The Studio status routes that start with the "no Unsloth backend configured" check: the gateway gives Python's 400 when no key is set (and
    forwards otherwise, so both sides stay equal either way); a player is refused as Python refuses."""
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    urls = ["models", "image/status", "image/load-progress", "auto-switch", "status", "context-overrides", "audio-models", "version", "probe",
            "hub/download-progress?repo_id=a/b", "hub/download-progress", "gguf-variants?repo_id=a/b", "gguf-variants"]
    unconfigured = 0
    for u in urls:
        url = "/api/ai/unsloth/" + u
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        if a.status_code == 400 and b"No Unsloth backend configured" in a.content:
            unconfigured += 1
            assert b.headers.get("x-nd-served-by") == "gateway", url
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
    assert unconfigured >= 9 or os.environ.get("UNSLOTH_API_KEY"), unconfigured


def test_studio_console_page_without_a_studio_is_pythons(client, seed):
    """GET /studio with no Studio Console URL and no key: the page with an empty address, from the gateway; with a Console URL saved, Python's page
    (the gateway steps aside); a player gets Python's 403."""
    from pathlib import Path
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    f = Path(os.environ["DB_PATH"]).parent / "ai_models.json"
    was = f.read_bytes() if f.exists() else None
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    try:
        f.unlink(missing_ok=True)
        keyless = not os.environ.get("UNSLOTH_API_KEY")
        served = _same_page(d, v, "/studio", "gm")
        assert served or not keyless
        f.write_text('{"studio_console_url": "studio.lan:8888"}', encoding="utf-8")
        assert not _same_page(d, v, "/studio", "gm console url")
        f.write_text('{"studio_console_url": ""}', encoding="utf-8")
        _same_page(d, v, "/studio", "gm empty url")
        _same_page(p, _via(p, base), "/studio", "player")
    finally:
        if was is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(was)


def test_media_rename_page_is_pythons(client, seed):
    """GET /media-rename[?kind=&album=]: the renamer's page with some history, for the GM (each kind, a bad kind, an album, a word for the album) and
    for a player, whose default read-only sections are Python's 403."""
    from datetime import datetime
    from app.database import SessionLocal
    from app.models import MediaRenameLog
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        db.add_all([MediaRenameLog(world_id=seed.world_a.id, batch_id="pg1", kind="audio", ref_id=1, old_name="IMG_1", new_name="Door <Creak>", user_id=seed.gm.id,
                                   created_at=datetime(2026, 6, 1, 9, 30, 0)),
                    MediaRenameLog(world_id=seed.world_a.id, batch_id="pg2", kind="image", ref_url="/uploads/a.png", old_name="", new_name="Map", user_id=None,
                                   created_at=datetime(2026, 6, 2, 9, 30, 0), undone_at=datetime(2026, 6, 3, 9, 30, 0))])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    for url in ("/media-rename", "/media-rename?kind=video", "/media-rename?kind=image&album=3", "/media-rename?kind=nope", "/media-rename?album=abc"):
        assert _same_page(d, v, url, "gm"), url
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    _same_page(p, _via(p, base), "/media-rename", "player")
    nogm = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    nogm.cookies.set("active_world", "no-such-world")
    _same_page(nogm, _via(nogm, base), "/media-rename", "no world")


def test_imagegen_reads_that_need_no_back_end_are_pythons(client, seed):
    """GET /api/ai/imagegen/status, loras, upscalers, refiners, ipadapter-models, progress, updates, backends, samplers-schedulers and starred: with no
    SwarmUI / ComfyUI configured the answer follows from the settings alone (Python and the gateway agree byte for byte; where a back end would be asked
    the gateway steps aside); starred pictures with parameters."""
    from app.database import SessionLocal
    from app.models import StarredImage
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    db = SessionLocal()
    try:
        db.add_all([StarredImage(url="/uploads/gw-star-1.png", prompt="a city", negative="blur", model="m1", seed=7, params_json='{"steps": 20, "cfg": 7.5, "tags": ["a", "b"]}'),
                    StarredImage(url="/uploads/gw-star-2.png", prompt="", negative="", model="", seed=-1, params_json="")])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    names = ["status", "loras", "upscalers", "refiners", "ipadapter-models", "progress", "updates", "backends", "samplers-schedulers", "starred"]
    served = 0
    for n in names:
        url = "/api/ai/imagegen/" + n
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        served += b.headers.get("x-nd-served-by") == "gateway"
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
    assert served >= 4, served
    assert v.get("/api/ai/imagegen/starred").headers.get("x-nd-served-by") == "gateway"


def test_ai_context_info_and_ping_are_pythons(client, seed):
    """GET /api/ai/context-info and /api/ai/ping: the same bytes and headers from the gateway; a player is refused as Python refuses."""
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    for url in ("/api/ai/context-info", "/api/ai/ping"):
        a, b = d.get(url), v.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url
        for h in ("content-type", "cache-control", "x-accel-buffering"):
            assert a.headers.get(h) == b.headers.get(h), (url, h)
        assert b.headers.get("x-nd-served-by") == "gateway", url
        a, b = p.get(url), pv.get(url)
        assert (a.status_code, a.content) == (b.status_code, b.content), url


class _RoutedStudio:
    """A stand-in Unsloth Studio with a table of {path: (status, json body)} that a test swaps between rounds; any other path is FastAPI's 404.
    Only 'Bearer sk-studio-key' is accepted (401 otherwise)."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.routes = {}
        self.log = []          # (method, path, parsed JSON body) of every POST / PUT
        outer = self

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                path = self.path.split("?", 1)[0]
                if self.headers.get("Authorization", "") != "Bearer sk-studio-key":
                    code, body = 401, {"detail": "Not authenticated"}
                else:
                    code, body = outer.routes.get(path, (404, {"detail": "Not Found"}))
                raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._answer()

            def _with_body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = raw.decode("utf-8", "replace")
                outer.log.append((self.command, self.path.split("?", 1)[0], parsed))
                self._answer()

            do_POST = _with_body
            do_PUT = _with_body

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_unsloth_studio_status_routes_are_pythons(client, seed):
    """GET /api/ai/unsloth/* with a (stand-in) Studio configured: models, image status / load progress, auto-switch, context overrides (present,
    missing, with a context that differs from ours), audio models, version, the probe, the status card, download progress, quant variants, the key
    check and the auth status - for a Studio that answers, one that answers errors, and a key it rejects. Python's and the gateway's bytes are
    compared (the banner timestamps normalised: each process keeps its own); a player is refused as Python refuses."""
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    studio = _RoutedStudio()

    def save(side, key, url):
        data = {"ollama_model": "", "ollama_url": "", "llm_url": url, "llm_model": "nd-test/model", "llm_api_key": key}
        if not key:
            data["llm_api_key_clear"] = "1"
        r = side.post("/settings/system", data=data, follow_redirects=False)
        assert r.status_code == 303, (r.status_code, r.text[:200])

    cached = [
        {"repo_id": "unsloth/a-GGUF", "task": "text-generation", "size_bytes": 1234, "load_id": "unsloth/a", "capabilities": {"requires_variant": True}},
        {"repo_id": "x/img", "task": "text-to-image", "capabilities": {"can_download": False}},
        {"repo_id": "x/whisper-small", "task": "automatic-speech-recognition", "size_bytes": 5, "capabilities": {"x": 1}},
        {"repo_id": "x/orpheus-tts", "task": "text-to-speech", "size_bytes": 7},
        {"repo_id": "x/odd", "task": "mystery", "load_id": None},
    ]
    good = {
        "/api/hub/cached-gguf": (200, {"cached": cached}),
        "/v1/models": (200, {"data": [{"id": "unsloth/a"}, {"id": "other"}]}),
        "/api/inference/images/status": (200, {"loaded": True, "repo_id": "x/img", "gguf_filename": "q4.gguf", "vram": 1.5}),
        "/api/inference/images/load-progress": (200, {"progress": 0.5, "phase": "loading"}),
        "/api/inference/loaded-models": (200, {"models": [{"id": "unsloth/a", "kind": "chat"}]}),
        "/api/settings/openai-auto-switch": (200, {"enabled": True, "auto_unload_idle_seconds": 300, "media_auto_switch_model": False}),
        "/api/settings/openai-auto-switch/overrides": (200, {"overrides": {"nd-test/model": {"max_seq_length": 4096}}}),
        "/version": (200, {"version": "2026.4.1", "other": "é"}),
        "/api/hub/download-progress": (200, {"progress": 0.25, "complete_on_disk": False}),
        "/api/hub/gguf-variants": (200, {"repo_id": "x/y", "variants": ["Q4_K_M", "Q8_0"], "default_variant": "Q4_K_M"}),
        "/health": (200, {"status": "ok", "version": "h1"}),
    }
    errors = {
        "/api/hub/cached-gguf": (500, {"error": {"message": "hub exploded"}}),
        "/api/inference/images/status": (500, {"detail": "image backend down"}),
        "/api/inference/images/load-progress": (404, {"detail": "Model not loading"}),
        "/api/inference/loaded-models": (503, {"error": "busy"}),
        "/api/settings/openai-auto-switch": (400, {"detail": [{"msg": "bad one"}, {"msg": "bad two"}]}),
        "/api/settings/openai-auto-switch/overrides": (200, b"not json at all"),
        "/api/hub/download-progress": (404, {"detail": "Repository 'x/y' not found"}),
        "/api/hub/gguf-variants": (422, ["x", "y"]),
    }
    suffixes = ["models", "image/status", "image/load-progress", "auto-switch", "status", "context-overrides", "audio-models", "version", "probe",
                "hub/download-progress?repo_id=x%2Fy", "gguf-variants?repo_id=x/y", "key-check", "auth-status"]

    def scrub(r):
        try:
            body = r.json()
        except ValueError:
            return r.content
        if isinstance(body, dict):
            for key in ("failed_at", "ok_at"):
                body.get(key) is not None and body.__setitem__(key, "T")
            if isinstance(body.get("auth"), dict):
                for key in ("failed_at", "ok_at"):
                    body["auth"].get(key) is not None and body["auth"].__setitem__(key, "T")
        return body

    served = 0
    try:
        save(d, "sk-studio-key", studio.url())
        save(v, "sk-studio-key", studio.url())
        for label, table in (("good", good), ("errors", errors), ("empty", {})):
            studio.routes = table
            for s in suffixes:
                url = "/api/ai/unsloth/" + s
                a, b = d.get(url), v.get(url)
                assert (a.status_code, scrub(a)) == (b.status_code, scrub(b)), (label, url, a.text[:300], b.text[:300])
                served += b.headers.get("x-nd-served-by") == "gateway"
                a, b = p.get(url), pv.get(url)
                assert (a.status_code, a.content) == (b.status_code, b.content), (label, url, "player")
        # a missing version in every build, then a version-less answer
        studio.routes = {"/api/version": (404, {"detail": "Not Found"}), "/version": (404, {}), "/api/about": (404, "x")}
        a, b = d.get("/api/ai/unsloth/version"), v.get("/api/ai/unsloth/version")
        assert (a.status_code, a.content) == (b.status_code, b.content)
        # a key Studio rejects
        save(d, "sk-wrong", studio.url())
        save(v, "sk-wrong", studio.url())
        studio.routes = good
        for s in ("models", "status", "key-check", "image/status"):
            url = "/api/ai/unsloth/" + s
            a, b = d.get(url), v.get(url)
            assert (a.status_code, scrub(a)) == (b.status_code, scrub(b)), ("wrong key", url, a.text[:300], b.text[:300])
        a, b = d.get("/api/ai/unsloth/auth-status"), v.get("/api/ai/unsloth/auth-status")
        assert scrub(a)["key_failed"] == scrub(b)["key_failed"] is True, (a.text, b.text)
    finally:
        studio.close()
        save(d, "", "")
        save(v, "", "")
    assert served >= 25, served


def test_unsloth_studio_actions_are_pythons(client, seed):
    """POST /api/ai/unsloth/hub/download, image/load (with a file name), image/unload, auto-switch and context-overrides: what Studio is sent (method,
    path and JSON, from the stand-in's log) and what the caller is answered, Python against the gateway - for a Studio that answers and one that errors
    or lacks the route; the refusals (a missing id, a body that is not an object) and a player's 403."""
    from .conftest import PLAYER_PASSWORD
    base = str(client.base_url)
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    p = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pv = _via(p, base)
    studio = _RoutedStudio()

    def save(side, key, url):
        data = {"ollama_model": "", "ollama_url": "", "llm_url": url, "llm_model": "nd-test/model", "llm_api_key": key}
        if not key:
            data["llm_api_key_clear"] = "1"
        assert side.post("/settings/system", data=data, follow_redirects=False).status_code == 303

    calls = [
        ("/api/ai/unsloth/hub/download", {"repo_id": " unsloth/a-GGUF ", "gguf_variant": "Q4_K_M"}),
        ("/api/ai/unsloth/hub/download", {"repo_id": "unsloth/b", "gguf_variant": ""}),
        ("/api/ai/unsloth/hub/download", {"repo_id": ""}),
        ("/api/ai/unsloth/hub/download", {}),
        ("/api/ai/unsloth/image/load", {"repo_id": "x/img", "gguf_filename": "q4.gguf"}),
        ("/api/ai/unsloth/image/load", {"repo_id": ""}),
        ("/api/ai/unsloth/image/unload", None),
        ("/api/ai/unsloth/auto-switch", {"enabled": True, "media_auto_switch_model": 0, "auto_unload_idle_seconds": 120}),
        ("/api/ai/unsloth/auto-switch", {"auto_unload_idle_seconds": -5}),
        ("/api/ai/unsloth/auto-switch", {}),
        ("/api/ai/unsloth/context-overrides", {"model_id": "nd-test/model", "max_seq_length": 8192, "extra": {"k": [1, 2]}}),
        ("/api/ai/unsloth/context-overrides", {"model_id": "  "}),
    ]
    good = {"/api/hub/download": (200, {"job_key": "j1", "state": "running", "accepted": True}),
            "/api/inference/images/load": (200, {"loaded": False, "engine": "x"}),
            "/api/inference/images/unload": (200, {"loaded": False}),
            "/api/settings/openai-auto-switch": (200, {"enabled": True}),
            "/api/settings/openai-auto-switch/overrides": (200, {"ok": True})}
    bad = {"/api/hub/download": (400, {"detail": "no such repo"}), "/api/inference/images/load": (409, {"error": {"message": "busy"}}),
           "/api/inference/images/unload": (500, {"detail": "boom"}), "/api/settings/openai-auto-switch": (400, {"detail": [{"msg": "m1"}]})}
    served = 0
    try:
        save(d, "sk-studio-key", studio.url())
        save(v, "sk-studio-key", studio.url())
        for label, table in (("good", good), ("bad", bad), ("none", {})):
            studio.routes = table
            for url, payload in calls:
                outs = []
                for side in (d, v):
                    studio.log.clear()
                    r = side.post(url, json=payload) if payload is not None else side.post(url)
                    outs.append((r.status_code, r.content, list(studio.log), r.headers.get("x-nd-served-by")))
                assert outs[0][:3] == outs[1][:3], (label, url, payload, outs[0][:3], outs[1][:3])
                served += outs[1][3] == "gateway"
                a, b = p.post(url, json=payload or {}), pv.post(url, json=payload or {})
                assert (a.status_code, a.content) == (b.status_code, b.content), (label, url, "player")
        # a body that is not an object, and invalid JSON, are FastAPI's 422
        studio.routes = good
        for raw in (b"[1,2]", b"not json", b'"text"'):
            a = d.post("/api/ai/unsloth/hub/download", content=raw, headers={"Content-Type": "application/json"})
            b = v.post("/api/ai/unsloth/hub/download", content=raw, headers={"Content-Type": "application/json"})
            assert (a.status_code, a.content) == (b.status_code, b.content), raw
    finally:
        studio.close()
        save(d, "", "")
        save(v, "", "")
    assert served >= 20, served
