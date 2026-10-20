"""The table-night tools' layouts: dice (3D tray / Quick buttons / History first), random tables (Cards / List / Roll board, opening grouped by
category) and the combat tracker (Standard / Compact / Cards / Turn view / Big screen) - the registry, the pure helpers under Node and the pages'
wiring. The choice is each person's own."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import CombatSession

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def test_the_three_sections():
    assert [m["id"] for m in view_prefs.spec("dice")["modes"]] == ["tray", "quick", "log"]
    cm = view_prefs.spec("combat")
    assert [m["id"] for m in cm["modes"]] == ["rows", "compact", "cards", "turn", "big"] and {m["id"]: m["label"] for m in cm["modes"]}["rows"] == "Standard"
    tb = view_prefs.spec("tables")
    assert [m["id"] for m in tb["modes"]] == ["grid", "compact", "roll"] and {m["id"]: m["label"] for m in tb["modes"]}["roll"] == "Roll board"
    assert tb["default_group"] == "category"
    for k in ("dice", "combat", "tables"):
        assert k in view_prefs.SECTION_TITLES


def test_a_section_can_open_grouped_but_a_person_can_still_choose_no_grouping():
    assert view_prefs.clean("tables", {})["group"] == "category"                        # nothing chosen yet: grouped, as the page always was
    assert view_prefs.clean("tables", {"group": ""})["group"] == ""                     # chosen: none
    assert view_prefs.clean("tables", {"group": "bogus"})["group"] == "category"
    assert view_prefs.clean("entities", {})["group"] == "" and view_prefs.clean("entities", {"group": "bogus"})["group"] == ""   # other sections: unchanged


def _node(file, expr):
    out = subprocess.run([NODE, "-e", f"const c = require('./static/js/{file}'); console.log(JSON.stringify({expr}))"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_the_engine_reads_default_group_like_the_server():
    spec = json.dumps(view_prefs.spec("tables"))
    assert _node("nd-views.js", f"c.cleanState({{}}, {spec}).group") == "category"
    assert _node("nd-views.js", f"c.cleanState({{group: ''}}, {spec}).group") == ""
    assert _node("nd-views.js", f"c.cleanState({{group: 'x'}}, {spec}).group") == "category"


@needs_node
def test_dice_notation_saved_formulas_and_the_combat_turn_window():
    assert _node("dice-layouts.js", "c.notation(20, 1, 0)") == "1d20" and _node("dice-layouts.js", "c.notation(6, 2, 3)") == "2d6+3"
    assert _node("dice-layouts.js", "c.notation(8, 3, -2)") == "3d8-2" and _node("dice-layouts.js", "c.notation(6, 99, 0)") == "50d6"
    assert _node("dice-layouts.js", "c.cleanFormula(' 2D6 + 3 ')") == "2d6+3" and _node("dice-layouts.js", "c.cleanFormula('rm -rf /')") == "" and _node("dice-layouts.js", "c.cleanFormula('')") == ""
    assert _node("dice-layouts.js", "c.addSaved(['1d20', '2d6'], '2d6')") == ["2d6", "1d20"]                                # newest first, no repeats
    assert len(_node("dice-layouts.js", "Array.from({length: 20}, (_, i) => i + 1).reduce((l, n) => c.addSaved(l, n + 'd6'), [])")) == 12
    assert _node("dice-layouts.js", "c.removeSaved(['a', 'b'], 'a')") == ["b"]
    assert _node("combat-layouts.js", "c.turnWindow(5, 1)") == [1, 2] and _node("combat-layouts.js", "c.turnWindow(5, 4)") == [4, 0]   # the last one acting: the first is next
    assert _node("combat-layouts.js", "c.turnWindow(1, 0)") == [0] and _node("combat-layouts.js", "c.turnWindow(0, 0)") == [] and _node("combat-layouts.js", "c.turnWindow(3, -1)") == [0, 1]
    assert _node("combat-layouts.js", "c.effectiveMode('turn')") == "turn" and _node("combat-layouts.js", "c.effectiveMode('x')") == "rows"


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_pages_carry_their_layout_controls(client, seed):
    _as(client, seed, False)
    dice = client.get("/dice").text
    assert 'id="dice-layoutbar"' in dice and 'id="dq"' in dice and 'data-ep-view=""' in dice and "/static/js/dice-layouts.js" in dice
    assert client.post("/api/view-prefs/dice", json={"mode": "quick"}).json()["mode"] == "quick"
    assert 'data-ep-view="quick"' in client.get("/dice").text
    _as(client, seed, True)
    assert 'data-ep-view=""' in client.get("/dice").text                                      # the GM is another person
    tables = client.get("/tables").text
    assert 'id="table-cards"' in tables and 'data-for="table-cards"' in tables and 'id="tbl-log"' in tables
    db = SessionLocal()
    try:
        cs = CombatSession(world_id=seed.world_a.id, name="Ambush", combatants_json=json.dumps([{"id": "c-1", "name": "Thug", "source": "manual", "pc_id": None, "entity_id": None,
                           "initiative": 10, "max_hp": 5, "hp": 5, "max_shock": 0, "shock": 0, "armor": 0, "conditions": [], "notes": ""}]))
        db.add(cs)
        db.commit()
        cid = cs.id
    finally:
        db.close()
    page = client.get(f"/combat/{cid}").text
    assert 'id="ct-layoutbar"' in page and 'data-layout="rows"' in page and "/static/js/combat-layouts.js" in page
    assert page.index("combat-layouts.js") > page.index("compact-toggle")                     # loaded after the tracker that owns the compact setting
