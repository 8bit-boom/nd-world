"""Connections web + entity-notes layouts: the registry sections, the web's pure placement (static/js/connections-web.js under Node) and the entity
page's wiring (templates/entities/detail.html) for the GM and for a player, who only gets what they may see."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import Entity, EntityNote

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")


def test_registry_sections():
    sp = view_prefs.spec("connections")
    assert [m["id"] for m in sp["modes"]] == ["grid", "web"] and {m["id"]: m["label"] for m in sp["modes"]}["grid"] == "Cards"
    assert [g["id"] for g in sp["groups"]] == ["dir", "kind"] and "web" not in sp["size_modes"] and "grid" not in sp["size_modes"]
    assert view_prefs.clean("connections", {"mode": "web", "group": "kind"})["mode"] == "web"
    en = view_prefs.spec("entity_notes")
    assert [m["id"] for m in en["modes"]] == ["timeline", "cards", "outline", "split"]
    assert {m["id"]: m["label"] for m in en["modes"]} == {"timeline": "List", "cards": "Cards", "outline": "Outline", "split": "Reader"}
    assert en["default_sort"] == "date" and [g["id"] for g in en["groups"]] == ["month", "visibility"]


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_ring_layout():
    run = lambda *a: json.loads(subprocess.run([NODE, "-e", f"const c = require('./static/js/connections-web.js'); console.log(JSON.stringify(c.ringLayout(...{json.dumps(list(a))})))"],
                                               capture_output=True, text=True, check=True).stdout)
    assert run(0, 760, 520) == []
    one = run(1, 760, 520)
    assert len(one) == 1 and one[0]["x"] == 380 and one[0]["y"] < 260                        # a single node sits straight above the centre
    many = run(25, 760, 520, 10)
    assert len(many) == 25 and {p["ring"] for p in many} == {0, 1, 2}
    assert all(0 < p["x"] < 760 and 0 < p["y"] < 520 for p in many)                            # nothing leaves the picture
    assert len({(p["x"], p["y"]) for p in many}) == 25                                         # and no two nodes share a spot


def _login(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_entity_page_wiring(client, seed):
    db = SessionLocal()
    try:
        hub = Entity(world_id=seed.world_a.id, name="Hub City", kind="location", visible_to_players=True)
        out, back = (Entity(world_id=seed.world_a.id, name=n, kind="npc", visible_to_players=True) for n in ("Mira", "Orrin"))
        db.add_all([hub, out, back]); db.commit()
        hub.related.append(out); back.related.append(hub)
        db.add(EntityNote(entity_id=hub.id, content="# Secret plan\nonly the GM", visible_to_players=False))
        db.add(EntityNote(entity_id=hub.id, content="Public rumour", visible_to_players=True))
        db.add(EntityNote(entity_id=hub.id, content="Another rumour", visible_to_players=True))
        db.commit()
        hid = hub.id
    finally:
        db.close()
    _login(client, seed, True)
    gm = client.get(f"/entity/{hid}").text
    assert 'data-for="conn-items"' in gm and 'data-dir="out"' in gm and 'data-dir="in"' in gm and "↩ links here" in gm
    assert 'data-g-dir="Links to"' in gm and 'data-g-dir="Referenced by"' in gm
    assert 'data-for="en-notes"' in gm and gm.count("en-note pn-note nd-item") == 3 and 'data-vis="hidden"' in gm and 'id="nv-reader"' in gm
    assert "/static/js/connections-web.js" in gm and "/static/js/notes-views.js" in gm
    assert "Secret plan" in gm
    _login(client, seed, False)
    pl = client.get(f"/entity/{hid}").text
    assert 'data-for="en-notes"' in pl and pl.count("en-note pn-note nd-item") == 2 and "Secret plan" not in pl
    assert 'data-for="conn-items"' in pl


def test_a_single_note_gets_no_switcher(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, name="Lonely", kind="npc", visible_to_players=True)
        db.add(e); db.commit()
        db.add(EntityNote(entity_id=e.id, content="Just one", visible_to_players=True)); db.commit()
        eid = e.id
    finally:
        db.close()
    _login(client, seed, True)
    page = client.get(f"/entity/{eid}").text
    assert 'data-for="en-notes"' not in page and 'data-for="conn-items"' not in page and "No connections yet." in page
