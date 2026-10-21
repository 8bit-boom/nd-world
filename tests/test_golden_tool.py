"""scripts/golden.py: normalising, crawling and comparing two servers' answers (docs/RUST_REWRITE_PLAN.md, phase 0). The app compared with itself
must show no difference - that is what proves the normaliser removes only what changes between two requests - and a changed answer must show."""
import importlib.util
import json
from pathlib import Path

import httpx

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD

_spec = importlib.util.spec_from_file_location("golden", Path(__file__).resolve().parent.parent / "scripts" / "golden.py")
golden = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(golden)


def test_html_is_compared_by_meaning_not_by_spelling():
    a = b'<div class="x"  id="a">\n  <p>Hello   <b>world</b></p>\n</div><!-- c -->'
    b = b'<div id="a" class="x"><p>Hello <b>world</b></p></div>'
    assert golden.normalise("text/html; charset=utf-8", a) == golden.normalise("text/html", b)
    assert golden.normalise("text/html", b"<p>one</p>") != golden.normalise("text/html", b"<p>two</p>")
    assert golden.normalise("text/html", b"<pre>a\n  b</pre>") != golden.normalise("text/html", b"<pre>a b</pre>"), "preformatted text keeps its spaces"


def test_json_key_order_and_clock_values_do_not_count():
    a = json.dumps({"b": 1, "a": {"ts": "2026-10-21T10:00:01Z", "n": 2}, "updated_at": 1700000001.5}).encode()
    b = json.dumps({"a": {"n": 2, "ts": "2026-10-21T10:00:09Z"}, "updated_at": 1700000099.1, "b": 1}).encode()
    assert golden.normalise("application/json", a) == golden.normalise("application/json", b)
    assert golden.normalise("application/json", b'{"a": 1}') != golden.normalise("application/json", b'{"a": 2}')


def test_links_stay_on_the_site():
    html = '<a href="/kind/npc">x</a><a href="entity/3#top">y</a><a href="https://example.com/z">z</a><a href="mailto:a@b">m</a><a href="/s?q=1">q</a>'
    assert golden.links(html, "http://h/a/b") == ["/kind/npc", "/a/entity/3", "/s?q=1"]
    assert golden.SKIP_PATH.search("/logout") and golden.SKIP_PATH.search("/entity/3/delete") and not golden.SKIP_PATH.search("/entity/3")


def _gm(client, seed):
    r = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.set("active_world", seed.world_a.slug)
    return client


def test_crawl_finds_the_pages_of_the_data_and_the_app_agrees_with_itself(client, seed):
    db = SessionLocal()
    try:
        for n in ("Ana", "Bo"):
            db.add(Entity(world_id=seed.world_a.id, kind="npc", name=n, body="Hello **there**"))
        db.commit()
    finally:
        db.close()
    gm = _gm(client, seed)
    paths = golden.crawl(gm, limit=60)
    assert "/" in paths and any(p.startswith("/kind/npc") for p in paths) and any(p.startswith("/entity/") for p in paths), paths
    assert not any("logout" in p or "delete" in p for p in paths)
    res = golden.compare(gm, gm, paths)
    assert not res["differ"], {k: v[:3] for k, v in list(res["differ"].items())[:3]}
    assert len(res["same"]) == len(paths) >= 5


def test_a_changed_answer_is_reported_and_can_be_accepted(client, seed):
    gm = _gm(client, seed)

    class Altered:
        """Wraps a client; answers /rules differently, as a port with a bug would."""
        def __init__(self, inner):
            self.inner, self.base_url = inner, inner.base_url

        def get(self, path, **kw):
            r = self.inner.get(path, **kw)
            if path == "/rules":
                return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>different</p>")
            return r
    res = golden.compare(gm, Altered(gm), ["/", "/rules"])
    assert list(res["differ"]) == ["/rules"] and res["same"] == ["/"]
    assert any("body differs" in line for line in res["differ"]["/rules"])
    res = golden.compare(gm, Altered(gm), ["/", "/rules"], allow=[r"^/rules$"])
    assert not res["differ"] and res["allowed"] == ["/rules"]
