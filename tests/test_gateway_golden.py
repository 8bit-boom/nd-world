"""The Rust gateway answers the same as the app directly (docs/RUST_GATEWAY.md). Runs only when the suite is pointed at a gateway
(ND_TEST_BASE_URL) and given the app's own address (ND_GOLDEN_DIRECT_URL):

    ND_TEST_BASE_URL=http://127.0.0.1:8952 ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8951 python -m pytest tests/test_gateway_golden.py"""
import importlib.util
import json
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
    # refusals and text cards stay Python's
    for body in ({"kind": "image", "url": "javascript:x"}, {"kind": "text", "text": "hi"}, {"kind": "map", "slug": "nope"}, {"kind": "combat", "combat_id": True}):
        r = via.post("/api/display/show", json=body)
        assert r.headers.get("x-nd-served-by") is None, body


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
                    "/quests/new", "/editor", "/entity-templates", "/characters/templates", "/worlds", "/export", "/maps/new")


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
            pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
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
