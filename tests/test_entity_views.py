"""Entity layouts: the registry's entities section, the generic group keys the engine gained for it, the split view's keyboard step, and the
entity list page's wiring (static/js/entity-views.js, templates/entities/list.html + _card.html)."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _run(file, script):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{file}');" + script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_entities_section_offers_cards_compact_wall_and_split():
    sp = view_prefs.spec("entities")
    assert [m["id"] for m in sp["modes"]] == ["grid", "compact", "wall", "split"]
    assert sp["size_modes"] == ["grid", "wall"] and [g["id"] for g in sp["groups"]] == ["subtype", "tag"]
    assert view_prefs.clean("entities", {"mode": "wall", "group": "tag", "sort": "-date"}) == {"mode": "wall", "size": 2, "sort": "-date", "group": "tag", "filters": []}
    assert view_prefs.clean("entities", {"mode": "pads"})["mode"] == "grid"


@needs_node
def test_engine_groups_by_any_data_g_key_with_none_last():
    spec = json.dumps(view_prefs.spec("entities"))
    metas = json.dumps([{"name": "a", "g": {"subtype": "Spy"}}, {"name": "b", "g": {"subtype": ""}}, {"name": "c", "g": {"subtype": "Guard"}}, {"name": "d", "g": {"subtype": "Spy"}}])
    plan = _run("nd-views.js", f"console.log(JSON.stringify(c.plan({metas}, c.cleanState({{group: 'subtype'}}, {spec}))))")
    assert [e.get("head") or e["item"] for e in plan["entries"]] == ["Guard", 2, "Spy", 0, 3, "None", 1]


@needs_node
def test_split_view_keyboard_steps_stay_inside_the_list():
    step = lambda *a: _run("entity-views.js", f"console.log(JSON.stringify(c.stepKey(...{json.dumps(list(a))})))")
    assert step("j", 0, 3) == 1 and step("ArrowDown", 2, 3) == 2 and step("k", 2, 3) == 1 and step("ArrowUp", 0, 3) == 0
    assert step("x", 1, 3) == -1 and step("j", 0, 0) == -1


def _login(client, seed, player=False):
    client.cookies.clear()
    login(client, (seed.player_a if player else seed.gm).email, PLAYER_PASSWORD if player else GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_entity_list_cards_carry_layout_data_and_players_get_the_switcher(client, seed):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Mara Vell", subtype="Spy", tags="cult, hunter", folder="Cult"))
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Tobias", subtype="Guard", visible_to_players=True))
        db.commit()
    finally:
        db.close()
    for player in (False, True):
        _login(client, seed, player)
        page = client.get("/kind/character").text
        # the root listing may be folder cards; a folder's own listing carries the bar
        if 'data-for="entity-cards"' not in page:
            page = client.get("/kind/character?view=grid&folder=Cult").text
        assert 'data-for="entity-cards"' in page, "player" if player else "gm"
        assert 'class="entity-card-wrap nd-item"' in page and 'data-g-subtype="Spy"' in page and 'data-g-tag="cult"' in page
        assert 'id="ev-preview"' in page and "/static/js/entity-views.js" in page and "/static/js/nd-views.js" in page


def test_the_split_preview_never_writes_empty_parts_into_the_pane():
    js = open("static/js/entity-views.js", encoding="utf-8").read()
    assert ".filter(Boolean))" in js           # replaceChildren(null) would print the word "null"
