"""An entity page's Media tab through the Rust gateway (docs/FORK_RUST_PORT_GUIDE.md): the page the gateway builds is the one Python builds,
the tab's routes answer the same, and - with no Python behind it at all - the page opens and the routes work.

    ND_TEST_BASE_URL=http://127.0.0.1:8980 ND_GOLDEN_DIRECT_URL=http://127.0.0.1:8960 python -m pytest tests/test_gateway_entity_media.py
"""
import importlib.util
import io
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app import entity_media as em
from app.database import SessionLocal
from app.main import UPLOADS_DIR
from app.models import AudioClip, Entity, EntityNote, ImageAlbum, VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")

_spec = importlib.util.spec_from_file_location("golden", Path(__file__).resolve().parent.parent / "scripts" / "golden.py")
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600


def _signed_in(email, password, world):
    d = httpx.Client(base_url=DIRECT, timeout=60)
    assert d.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False).status_code == 303
    d.cookies.set("active_world", world)
    return d


def _via(direct_client, base):
    """The same person's session, sent through the gateway."""
    c = httpx.Client(base_url=base, timeout=60)
    for k, v in direct_client.cookies.items():
        c.cookies.set(k, v)
    return c


def _clip(table, world, name, visible, entity_id=None):
    folder, ext, body = ("audio", ".mp3", b"ID3fake") if table == AudioClip else ("video", ".mp4", b"\x00\x00\x00\x18ftypmp42")
    (UPLOADS_DIR / folder).mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / folder / f"{name}.{ext[1:]}").write_bytes(body)
    return table(world_id=world.id, name=name, file_url=f"/uploads/{folder}/{name}{ext}", visible_to_players=visible, entity_id=entity_id)


def _world(seed):
    """An entity with every kind of thing the tab shows, a second one with nothing, and something in the galleries to pick."""
    tag = os.urandom(3).hex()
    (UPLOADS_DIR / "gallery").mkdir(parents=True, exist_ok=True)
    for n in ("portrait", "kept", "hidden", "shop", "plan", "vault", "loose"):
        (UPLOADS_DIR / "gallery" / f"{tag}-{n}.png").write_bytes(PNG)
    u = lambda n: f"/uploads/gallery/{tag}-{n}.png"  # noqa: E731
    db = SessionLocal()
    try:
        w = seed.world_a
        db.add(ImageAlbum(world_id=w.id, name=f"Album {tag}", image_urls_json=json.dumps([u(n) for n in ("portrait", "kept", "hidden", "shop", "plan", "vault", "loose")])))
        e = Entity(world_id=w.id, kind="character", name=f"Mira Voss {tag}", image_url=u("portrait"),
                   body=f"![Mira]({u('kept')})\n\n![Shop]({u('shop')})\n\n[gmonly]![Vault]({u('vault')})[/gmonly]")
        db.add(e)
        db.flush()
        em.store_pictures(e, [{"uid": "kept000001", "url": u("kept"), "caption": "Mira", "visible": True},
                              {"uid": "hidden0001", "url": u("hidden"), "caption": "", "visible": False}])
        db.add(EntityNote(entity_id=e.id, content=f"A note ![Plan]({u('plan')})", visible_to_players=True))
        db.add(_clip(AudioClip, w, f"open-song-{tag}", True, e.id))
        db.add(_clip(AudioClip, w, f"hidden-song-{tag}", False, e.id))
        db.add(_clip(VideoClip, w, f"film-{tag}", False, e.id))
        loose = _clip(AudioClip, w, f"loose-song-{tag}", False)
        db.add(loose)
        bare = Entity(world_id=w.id, kind="item", name=f"Bare {tag}")
        db.add(bare)
        db.commit()
        return {"id": e.id, "bare": bare.id, "loose": loose.id, "url": u, "tag": tag}
    finally:
        db.close()


def _media(html):
    i = html.index('<section class="entity-media"')
    return html[i:html.index("</section>", i)]


def _same(a, b, what):
    assert a.status_code == b.status_code, (what, a.status_code, b.status_code)
    if a.status_code != 200:
        return
    na, nb = golden.normalise(a.headers["content-type"], a.content), golden.normalise(b.headers["content-type"], b.content)
    if na != nb:
        import difflib
        raise AssertionError(f"{what}\n" + "\n".join(list(difflib.unified_diff(na.splitlines(), nb.splitlines(), "python", "gateway", n=1, lineterm=""))[:40]))


def test_the_media_tab_the_gateway_builds_is_the_one_python_builds(client, seed):
    base = str(client.base_url)
    d = _world(seed)
    paths = [f"/entity/{d['id']}", f"/entity/{d['id']}?as=player", f"/entity/{d['bare']}", "/entity/999999", "/images"]
    served = 0
    for email, pw, must_show in ((seed.gm.email, GM_PASSWORD, 6), (seed.player_a.email, PLAYER_PASSWORD, 4)):
        direct = _signed_in(email, pw, seed.world_a.slug)
        via = _via(direct, base)
        for path in paths:
            a, b = direct.get(path), via.get(path)
            _same(a, b, f"{email} {path}")
            served += b.headers.get("x-nd-served-by") == "gateway"
            if path == f"/entity/{d['id']}":                             # kept (1: the other is also in the body), hidden, portrait, shop, vault, plan
                assert _media(a.text).count('class="em-item em-pic') == must_show, (email, _media(a.text)[:400])
            if path == f"/entity/{d['bare']}":
                assert ('data-ep="media"' in a.text) == (email == seed.gm.email)
        direct.close()
        via.close()
    assert served >= 6, served


def test_the_tabs_routes_answer_the_same_through_the_gateway(client, seed):
    base = str(client.base_url)
    d = _world(seed)
    eid = d["id"]
    direct = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(direct, base)
    root = f"/api/entity/{eid}/media"
    served = []

    def both(method, path, **kw):
        """The request made through the gateway (which must answer it itself), then the same state read from both servers."""
        r = via.request(method, path, **kw)
        served.append(r.headers.get("x-nd-served-by") == "gateway")
        a, b = direct.get(root), via.get(root)
        assert a.status_code == b.status_code == 200, (path, r.status_code, r.text[:300])
        assert a.json() == b.json(), path
        return r, b.json()

    r, state = both("GET", root)
    assert r.json() == state and r.headers.get("x-nd-served-by") == "gateway"
    assert [p["uid"] for p in state["pictures"]] == ["kept000001", "hidden0001"] and state["max_pictures"] == 100
    assert [c["name"] for c in state["audio"]] == [f"hidden-song-{d['tag']}", f"open-song-{d['tag']}"]     # newest first
    assert [c["name"] for c in state["video"]] == [f"film-{d['tag']}"]
    assert d["url"]("loose") in [p["url"] for p in state["library"]["pictures"]] and d["url"]("kept") not in [p["url"] for p in state["library"]["pictures"]]
    assert [c["id"] for c in state["library"]["audio"]] == [d["loose"]] and [c["id"] for c in state["library"]["video"]] == []

    # keep a gallery picture, show it, name it, take it off
    r, state = both("POST", f"{root}/attach", json={"kind": "image", "url": d["url"]("loose")})
    assert r.status_code == 200 and len(state["pictures"]) == 3 and state["pictures"][-1]["visible_to_players"] is False
    assert state["pictures"][-1]["caption"].endswith("loose.png")            # the gallery's own name for an unfiled upload
    uid = state["pictures"][-1]["uid"]
    _, state = both("POST", f"{root}/image/{uid}/visibility", json={"visible_to_players": True})
    _, state = both("POST", f"{root}/image/{uid}/rename", json={"name": "The loose one"})
    assert state["pictures"][-1]["caption"] == "The loose one"
    both("POST", f"{root}/image/{uid}/remove")
    assert len(direct.get(root).json()["pictures"]) == 2

    # the loose clip belongs to the entity now: by id, by address, shown, named, let go
    _, state = both("POST", f"{root}/attach", json={"kind": "audio", "url": f"/uploads/audio/loose-song-{d['tag']}.mp3", "visible_to_players": True})
    assert [c["id"] for c in state["audio"] if c["id"] == d["loose"]] and next(c for c in state["audio"] if c["id"] == d["loose"])["visible_to_players"] is True
    _, state = both("POST", f"{root}/audio/{d['loose']}/rename", json={"name": "  Mira's   theme "})
    assert next(c for c in state["audio"] if c["id"] == d["loose"])["name"] == "Mira's theme"
    _, state = both("POST", f"{root}/audio/{d['loose']}/visibility", json={"visible_to_players": False})
    both("POST", f"{root}/audio/{d['loose']}/remove")
    assert all(c["id"] != d["loose"] for c in direct.get(root).json()["audio"])

    # what Python refuses, the gateway refuses the same way
    for method, path, kw in (("POST", f"{root}/attach", {"json": {"kind": "image", "url": "/uploads/gallery/nope.png"}}),
                             ("POST", f"{root}/attach", {"json": {"kind": "spell", "url": "x"}}),
                             ("POST", f"{root}/attach", {"json": {"kind": "audio", "id": True}}),
                             ("POST", f"{root}/attach", {"json": {"kind": "audio", "id": 999999}}),
                             ("POST", f"{root}/image/nope/rename", {"json": {"name": "x"}}),
                             ("POST", f"{root}/audio/{d['loose']}/remove", {}),
                             ("POST", f"{root}/image/kept000001/visibility", {"json": {"visible_to_players": "yes"}}),
                             ("POST", f"/api/entity/999999/media/attach", {"json": {"kind": "image", "url": "x"}}),
                             ("GET", "/api/entity/999999/media", {})):
        a, b = direct.request(method, path, **kw), via.request(method, path, **kw)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (path, a.status_code, a.text[:200], b.status_code, b.text[:200])
    assert all(served), f"{served.count(False)} of {len(served)} requests were not answered by the gateway"

    # a dropped picture: whoever turns it into a thumbnail, the tab holds the same afterwards
    for c in (via, direct):
        r = c.post(f"{root}/upload", files={"file": (f"drop-{d['tag']}.png", io.BytesIO(PNG), "application/octet-stream")}, data={"visible_to_players": "1"})
        assert r.status_code == 200, r.text[:300]
        assert r.json()["kind"] == "image" and r.json()["item"]["caption"] == f"drop-{d['tag']}"
        a, b = direct.get(root), via.get(root)
        assert a.json() == b.json()
        both("POST", f"{root}/image/{r.json()['item']['uid']}/remove")
    _same(direct.get(f"/entity/{eid}"), via.get(f"/entity/{eid}"), "the page after the changes")
    direct.close()
    via.close()


def _python_free(seed):
    import socket
    root = Path(__file__).resolve().parent.parent
    binary = root / "rust" / "target" / "release" / "nd-gateway"
    if not binary.exists():
        pytest.skip("the gateway binary is not built")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, ND_PYTHON_FREE="1", ND_UPSTREAM="127.0.0.1:1", ND_GATEWAY_LISTEN=f"127.0.0.1:{port}", DB_PATH=os.environ["DB_PATH"],
               ND_STATIC_DIR=str(root / "static"), ND_TEMPLATES_DIR=str(root / "app" / "templates"), ND_BUNDLED_MAPS_DIR=str(root / "app" / "maps"),
               ND_RUST_ROUTES="login,dice_roll,live,pages,writes,ai,mcp,fans,auth", SECRET_KEY="python-free-test-key", ND_PBKDF2_ITERATIONS="1000")
    try:
        proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pytest.skip("the gateway binary exists but cannot run on this platform (built for another OS)")
    return proc, f"http://127.0.0.1:{port}"


def test_without_python_the_media_tab_opens_and_its_routes_work(client, seed):
    d = _world(seed)
    proc, url = _python_free(seed)
    try:
        g = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
        for _ in range(50):
            try:
                httpx.get(f"{url}/login", timeout=2)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert g.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}).status_code in (302, 303)
        g.cookies.set("active_world", seed.world_a.slug)
        root = f"/api/entity/{d['id']}/media"
        r = g.get(f"/entity/{d['id']}")
        assert r.status_code == 200, (r.status_code, r.text[:300])
        tab = _media(r.text)
        assert tab.count('class="em-item em-pic') == 6 and "hidden-song" in tab and "film-" in tab and 'data-em-manage="1"' in tab
        assert g.get(f"/entity/{d['bare']}").status_code == 200
        r = g.get(root)
        assert r.status_code == 200, r.text[:300]
        state = r.json()
        assert [p["uid"] for p in state["pictures"]] == ["kept000001", "hidden0001"] and [c["id"] for c in state["library"]["audio"]] == [d["loose"]]
        r = g.post(f"{root}/attach", json={"kind": "image", "url": d["url"]("loose")})
        assert r.status_code == 200, r.text[:300]
        uid = r.json()["pictures"][-1]["uid"]
        g.post(f"{root}/image/{uid}/visibility", json={"visible_to_players": True})
        g.post(f"{root}/image/{uid}/rename", json={"name": "Loose"})
        r = g.post(f"{root}/attach", json={"kind": "audio", "id": d["loose"]})
        assert [c["id"] for c in r.json()["audio"] if c["id"] == d["loose"]] == [d["loose"]]
        r = g.post(f"{root}/audio/{d['loose']}/remove")
        assert all(c["id"] != d["loose"] for c in r.json()["audio"])
        tab = _media(g.get(f"/entity/{d['id']}").text)
        assert tab.count('class="em-item em-pic') == 7 and "Loose" in tab
        # what is refused says so in Python's words
        assert g.post(f"{root}/attach", json={"kind": "image", "url": "/uploads/gallery/nope.png"}).status_code == 404
        assert g.post(f"{root}/image/nope/remove").status_code == 404
        assert g.get("/api/entity/999999/media").status_code == 404
        # a player gets the players' tab and nothing else
        p = httpx.Client(base_url=url, timeout=60, follow_redirects=False)
        assert p.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"}).status_code in (302, 303)
        p.cookies.set("active_world", seed.world_a.slug)
        tab = _media(p.get(f"/entity/{d['id']}").text)
        assert tab.count('class="em-item em-pic') == 5 and "hidden" not in tab and "data-em-act" not in tab
        assert p.get(root).status_code == 403
        # deleting the entity sets its clips loose
        assert g.post(f"/entity/{d['id']}/delete").status_code == 303
        db = SessionLocal()
        try:
            assert all(c.entity_id is None for c in db.query(AudioClip).all()) and all(c.entity_id is None for c in db.query(VideoClip).all())
        finally:
            db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=20)


def test_an_assistant_reaches_the_tab_through_the_gateway(client, seed):
    """The route is in the assistant allowlist on both sides (app/main.py `_is_assistant_safe`, rust/nd-perm allow.rs)."""
    from app.models import WorldMembership
    d = _world(seed)
    db = SessionLocal()
    try:
        db.query(WorldMembership).filter(WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id).update({"role": "assistant"})
        db.commit()
    finally:
        db.close()
    direct = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    via = _via(direct, str(client.base_url))
    a, b = direct.get(f"/api/entity/{d['id']}/media"), via.get(f"/api/entity/{d['id']}/media")
    assert a.status_code == b.status_code == 200, (a.status_code, b.status_code)
    assert a.json() == b.json() and b.headers.get("x-nd-served-by") == "gateway"
    assert [p["uid"] for p in b.json()["pictures"]] == ["kept000001"]           # what players may see
    assert all(c["visible_to_players"] for c in b.json()["library"]["audio"]) and b.json()["library"]["audio"] == []
