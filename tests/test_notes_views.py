"""Notes layouts: the registry's notes section (timeline, cards, outline, reader), the reader's keyboard step (static/js/notes-views.js under Node)
and the private-notes page's wiring for the GM and for the player who owns the notes."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import PrivateNote

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")


def test_the_notes_section_and_its_reader_label():
    sp = view_prefs.spec("notes")
    assert [m["id"] for m in sp["modes"]] == ["timeline", "cards", "outline", "split"] and sp["default"] == "timeline"
    assert {m["id"]: m["label"] for m in sp["modes"]}["split"] == "Reader"           # the same mode id is "Split" on the entity list
    assert {m["id"]: m["label"] for m in view_prefs.spec("entities")["modes"]}["split"] == "Split"
    assert sp["default_sort"] == "-date" and sp["size_modes"] == ["cards"] and [g["id"] for g in sp["groups"]] == ["month"]
    assert view_prefs.clean("notes", {})["sort"] == "-date" and view_prefs.clean("notes", {"sort": "name"})["sort"] == "name"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_reader_keys_and_month_grouping():
    step = lambda *a: json.loads(subprocess.run([NODE, "-e", f"const c = require('./static/js/notes-views.js'); console.log(JSON.stringify(c.stepKey(...{json.dumps(list(a))})))"],
                                                capture_output=True, text=True, check=True).stdout)
    assert step("j", 0, 3) == 1 and step("k", 0, 3) == 0 and step("ArrowDown", 2, 3) == 2 and step("x", 1, 3) == -1
    spec = json.dumps(view_prefs.spec("notes"))
    metas = json.dumps([{"name": "a", "date": 3, "g": {"month": "2026-09"}}, {"name": "b", "date": 9, "g": {"month": "2026-10"}}, {"name": "c", "date": 5, "g": {"month": "2026-10"}}])
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/nd-views.js'); console.log(JSON.stringify(c.plan({metas}, c.cleanState({{group: 'month'}}, {spec}))))"],
                         capture_output=True, text=True, check=True).stdout
    entries = json.loads(out)["entries"]
    assert [e.get("head") or e["item"] for e in entries] == ["2026-10", 1, 2, "2026-09", 0]       # newest first inside a month, months by name


def test_private_notes_page_has_the_switcher_for_gm_and_owner(client, seed):
    db = SessionLocal()
    try:
        db.add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_a.id, author_id=seed.gm.id, title="Riddle", content="The door opens at dawn."))
        db.add(PrivateNote(world_id=seed.world_a.id, player_user_id=seed.player_a.id, author_id=seed.gm.id, title="", content="Second note with no title."))
        db.commit()
    finally:
        db.close()
    for email, password in ((seed.gm.email, GM_PASSWORD), (seed.player_a.email, PLAYER_PASSWORD)):
        client.cookies.clear()
        login(client, email, password)
        client.cookies.set("active_world", seed.world_a.slug)
        page = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}").text
        assert 'data-for="pn-notes"' in page and page.count('class="pn-note nd-item"') == 2
        assert 'data-g-month="' in page and 'id="nv-reader"' in page
        assert "/static/js/notes-views.js" in page and "/static/js/nd-views.js" in page
