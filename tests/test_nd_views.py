"""View layouts: the registry (app/view_prefs.py), the engine's pure decisions (static/js/nd-views.js, run under Node), the account-saved choice
(/api/view-prefs) and the pages that use them (All Images, an album)."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import Entity, ImageAlbum, UserViewPref

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")


# ── the registry ─────────────────────────────────────────────────────────────────────────────────────

def test_clean_falls_back_field_by_field():
    assert view_prefs.clean("images", {}) == {"mode": "grid", "size": 2, "sort": "name", "group": "", "filters": []}
    got = view_prefs.clean("images", {"mode": "masonry", "size": "3", "sort": "-size", "group": "usage", "filters": ["unused", "bogus"]})
    assert got == {"mode": "masonry", "size": 3, "sort": "-size", "group": "usage", "filters": ["unused"]}
    bad = view_prefs.clean("images", {"mode": "pie", "size": 9, "sort": "-nope", "group": "x", "filters": "unused"})
    assert bad == {"mode": "grid", "size": 2, "sort": "name", "group": "", "filters": []}
    assert view_prefs.clean("album", {"group": "usage"})["group"] == ""          # the album has no such grouping
    assert view_prefs.clean("nope", {"mode": "grid"}) == {} and view_prefs.spec("nope") == {}


def test_every_section_offers_only_known_labels():
    for name, s in view_prefs.SECTIONS.items():
        sp = view_prefs.spec(name)
        assert s["default"] in s["modes"] and s["default_sort"] in s["sorts"], name
        assert [m["id"] for m in sp["modes"]] == s["modes"]


# ── the engine's pure decisions (Node) ───────────────────────────────────────────────────────────────

def _node(script):
    out = subprocess.run([NODE, "-e", "const c = require('./static/js/nd-views.js');" + script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


SPEC = json.dumps(view_prefs.spec("images"))
METAS = json.dumps([
    {"name": "b castle", "uses": 2, "date": 30, "size": 500, "source": "Castle"},
    {"name": "a orphan", "uses": 0, "date": 10, "size": 9000, "source": ""},
    {"name": "c tavern", "uses": 1, "date": 20, "size": 100, "source": "Tavern"},
    {"name": "d orphan 2", "uses": 0, "date": 40, "size": 50, "source": ""},
])


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_engine_clean_state_matches_the_python_registry():
    for raw in ({}, {"mode": "filmstrip", "size": "1", "sort": "-date", "group": "source", "filters": ["unused"]},
                {"mode": "pie", "size": 7, "sort": "zzz", "group": "q", "filters": ["x"]}):
        assert _node(f"console.log(JSON.stringify(c.cleanState({json.dumps(raw)}, {SPEC})))") == view_prefs.clean("images", raw)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_engine_sorts_filters_and_groups():
    def run(state):
        return _node(f"console.log(JSON.stringify(c.plan({METAS}, {json.dumps(state)})))")
    base = {"mode": "grid", "size": 2, "sort": "name", "group": "", "filters": []}
    assert [e["item"] for e in run(base)["entries"]] == [1, 0, 2, 3]                       # by name
    assert [e["item"] for e in run({**base, "sort": "-size"})["entries"]] == [1, 0, 2, 3]  # 9000, 500, 100, 50
    assert [e["item"] for e in run({**base, "sort": "date"})["entries"]] == [1, 2, 0, 3]
    unused = run({**base, "filters": ["unused"]})
    assert sorted(unused["hidden"]) == [0, 2] and [e["item"] for e in unused["entries"]] == [1, 3]
    grouped = run({**base, "group": "usage", "sort": "-uses"})["entries"]
    assert [e.get("head") or e["item"] for e in grouped] == ["Used", 0, 2, "Unused", 1, 3]
    by_source = run({**base, "group": "source"})["entries"]
    assert [e.get("head") for e in by_source if "head" in e] == ["Castle", "Tavern", "Unused"]      # "Unused" last


# ── account-saved choices ────────────────────────────────────────────────────────────────────────────

def _login(client, seed, player=False):
    client.cookies.clear()
    login(client, (seed.player_a if player else seed.gm).email, PLAYER_PASSWORD if player else GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_a_signed_in_person_saves_and_reads_their_layout_and_only_their_own(client, seed):
    _login(client, seed)
    r = client.post("/api/view-prefs/images", json={"mode": "masonry", "size": 3, "sort": "-uses", "group": "usage", "filters": ["unused", "junk"]})
    assert r.status_code == 200 and r.json() == {"mode": "masonry", "size": 3, "sort": "-uses", "group": "usage", "filters": ["unused"]}
    assert client.get("/api/view-prefs").json()["images"]["mode"] == "masonry"
    assert client.post("/api/view-prefs/images", json={"mode": "list"}).json()["mode"] == "list"      # saving again replaces, never duplicates
    db = SessionLocal()
    try:
        assert db.query(UserViewPref).filter(UserViewPref.user_id == seed.gm.id, UserViewPref.section == "images").count() == 1
    finally:
        db.close()
    _login(client, seed, player=True)
    assert client.get("/api/view-prefs").json() == {}                              # a player never sees the GM's choice
    assert client.post("/api/view-prefs/images", json={"mode": "filmstrip"}).status_code == 200   # but may save their own
    assert client.get("/api/view-prefs").json()["images"]["mode"] == "filmstrip"


def test_view_prefs_need_a_sign_in_and_a_known_section(client, seed):
    client.cookies.clear()
    assert client.get("/api/view-prefs", follow_redirects=False).status_code in (401, 303, 307)
    _login(client, seed)
    assert client.post("/api/view-prefs/not_a_section", json={}).status_code == 404
    assert client.post("/api/view-prefs/images", content=b"not json", headers={"content-type": "application/json"}).status_code == 400


# ── the pages ────────────────────────────────────────────────────────────────────────────────────────

def test_all_images_page_has_the_switcher_the_items_and_the_account_choice(client, seed):
    _login(client, seed)
    client.post("/api/view-prefs/images", json={"mode": "list", "sort": "-date"})
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Holder", image_url="/uploads/a.png"))
        db.commit()
    finally:
        db.close()
    page = client.get("/images").text
    assert 'class="nd-viewbar"' in page and 'data-for="gallery-all"' in page and 'data-account="1"' in page
    assert '"mode": "list"' in page.replace('&#34;', '"') or '"mode":"list"' in page            # the saved choice reaches the engine
    assert 'class="gallery-img-grid nd-collection"' in page and "/static/js/nd-views.js" in page


def test_album_page_items_carry_the_sort_data_and_players_get_the_bar(client, seed):
    db = SessionLocal()
    try:
        album = ImageAlbum(world_id=seed.world_a.id, name="Pics", image_urls_json=json.dumps(["/uploads/x.png", "/uploads/y.png"]))
        db.add(album)
        db.commit()
        aid = album.id
    finally:
        db.close()
    _login(client, seed)
    page = client.get(f"/images/albums/{aid}").text
    assert page.count('class="gallery-album-img-cell nd-item"') == 2 and 'data-size="' in page and 'data-for="album-grid"' in page


def test_second_screen_is_a_tile_button_not_a_floating_label_on_gallery_tiles(client, seed):
    js = open("static/js/nd-stage.js", encoding="utf-8").read()
    assert ".nd-item, .nd-film" in js                       # the hover label skips layout tiles and the filmstrip preview
    db = SessionLocal()
    try:
        album = ImageAlbum(world_id=seed.world_a.id, name="Screens", image_urls_json=json.dumps(["/uploads/s.png"]))
        db.add(album)
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Holder", image_url="/uploads/s.png"))
        db.commit()
        aid = album.id
    finally:
        db.close()
    _login(client, seed)
    assert 'class="gallery-icon-btn gallery-screen-btn"' in client.get(f"/images/albums/{aid}").text
    assert "galleryShowOnScreen(this)" in client.get("/images").text
    engine = open("static/js/nd-views.js", encoding="utf-8").read()
    assert "nd-film-tools" in engine and "wheel" in engine and "nd-dragging" in engine      # draggable / wheel-scrollable strip
