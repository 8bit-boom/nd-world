"""Phase 7: the small lists' layouts (races / professions catalog, handouts, worlds, templates), Settings as one page + search, layout presets
(one click sets several sections, keeping the person's theme / scale / pins) and a world's starting preset for players who join it."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import InviteCode, User, UserViewPref, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_small_list_sections_keep_todays_layout_as_the_default():
    want = {"catalog": ["grid", "table", "compact"], "handouts": ["grid", "list"], "worlds": ["grid", "list"], "templates": ["list", "grid"],
            "settings_page": ["tabs", "onepage"]}
    for sec, ids in want.items():
        sp = view_prefs.spec(sec)
        assert [m["id"] for m in sp["modes"]] == ids and sp["default"] == ids[0], sec


@pytest.mark.parametrize("tpl,sec", [("races.html", "catalog"), ("professions.html", "catalog"), ("handouts_gallery.html", "handouts"),
                                     ("worlds.html", "worlds"), ("entity_templates_list.html", "templates"), ("settings.html", "settings_page")])
def test_each_page_has_its_bar_and_layout_element(tpl, sec):
    src = open(f"app/templates/{tpl}").read()
    assert f'pl_bar("{sec}")' in src and f'pl_attrs("{sec}", request)' in src


@needs_node
def test_settings_search_matches_every_word():
    out = subprocess.run([NODE, "-e", "const c = require('./static/js/settings-onepage.js'); console.log(JSON.stringify(["
                          "c.matches('Image uploads: AVIF', 'avif image'), c.matches('Image uploads', 'video'), c.matches('x', ''), c.matches('', 'a')]))"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, True, False]


def test_presets_name_only_real_sections_and_modes():
    for pid, p in view_prefs.PRESETS.items():
        for sec, val in p["sets"].items():
            if sec == "site":
                for k, v in val.items():
                    assert v in view_prefs.SITE_OPTIONS[k], (pid, k, v)
            else:
                assert val in view_prefs.SECTIONS[sec]["modes"], (pid, sec, val)


def test_a_preset_merges_onto_what_the_person_had(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/api/view-prefs/site", json={"options": {"theme": "light", "nav": "top"}, "pins": ["home"]}).status_code == 200
    assert client.post("/api/view-prefs/entity_page", json={"mode": "classic", "sort": "name"}).status_code == 200
    r = client.post("/api/view-prefs/preset/player_phone")
    assert r.status_code == 200
    got = client.get("/api/view-prefs").json()
    assert got["site"]["options"]["nav"] == "bottom" and got["site"]["options"]["density"] == "touch"
    assert got["site"]["options"]["theme"] == "light" and got["site"]["pins"] == ["home"]          # kept
    assert got["hub"]["mode"] == "play" and got["entity_page"]["mode"] == "tabs" and got["recap"]["mode"] == "sections"
    assert 'data-nav="bottom"' in client.get("/").text                                             # the session copy was refreshed
    assert client.post("/api/view-prefs/preset/nope").status_code == 404


def test_presets_show_on_the_account_page(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    page = client.get("/account").text
    for pid in view_prefs.PRESETS:
        assert f'data-preset="{pid}"' in page


def test_a_world_starting_preset_applies_once_to_a_new_player(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    form = client.get(f"/worlds/{seed.world_a.id}/edit").text
    assert 'name="layout_preset"' in form
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).layout_preset = "reading"
        db.add(InviteCode(world_id=seed.world_a.id, code="presetinvite"))
        db.commit()
    finally:
        db.close()
    client.get("/logout")
    r = client.post("/join/presetinvite", data={"mode": "signup", "email": "fresh@test.local", "password": "fresh-password-1"}, follow_redirects=False)
    assert r.status_code == 303
    got = client.get("/api/view-prefs").json()
    assert got["site"]["options"]["width"] == "reading" and got["rules"]["mode"] == "book" and got["entity_page"]["mode"] == "reader"
    db = SessionLocal()
    try:
        uid = db.query(User).filter(User.email == "fresh@test.local").first().id
        assert db.query(UserViewPref).filter(UserViewPref.user_id == uid).count() == 3
    finally:
        db.close()


def test_a_world_preset_never_overwrites_a_players_own_layouts(client, seed):
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    assert client.post("/api/view-prefs/rules", json={"mode": "wide"}).status_code == 200
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).layout_preset = "reading"
        db.add(InviteCode(world_id=seed.world_a.id, code="presetinvite2"))
        db.commit()
    finally:
        db.close()
    client.get("/logout")
    r = client.post("/join/presetinvite2", data={"mode": "login", "email": seed.player_b.email, "password": PLAYER_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    got = client.get("/api/view-prefs").json()
    assert got["rules"]["mode"] == "wide" and "site" not in got


def test_the_world_edit_form_saves_and_clears_the_starting_preset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    base = {"name": "World A"}
    assert client.post(f"/worlds/{seed.world_a.id}/edit", data={**base, "layout_preset": "prep"}, follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        assert db.get(World, seed.world_a.id).layout_preset == "prep"
    finally:
        db.close()
    client.post(f"/worlds/{seed.world_a.id}/edit", data={**base, "layout_preset": "bogus"}, follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(World, seed.world_a.id).layout_preset is None
    finally:
        db.close()
