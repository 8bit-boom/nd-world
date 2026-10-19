"""Quest layouts: the registry's quests section, the board's pure helpers (static/js/nd-board.js under Node), and the quest page's wiring."""
import json
import shutil
import subprocess

import pytest

from app import view_prefs
from app.database import SessionLocal
from app.models import Quest

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

NODE = shutil.which("node")


def test_the_quests_section_has_a_board_a_list_and_cards():
    sp = view_prefs.spec("quests")
    assert [m["id"] for m in sp["modes"]] == ["board", "compact", "grid"] and sp["default"] == "board"
    assert {m["id"]: m["label"] for m in sp["modes"]} == {"board": "Board", "compact": "List", "grid": "Cards"}
    assert [g["id"] for g in sp["groups"]] == ["status", "category"] and sp["size_modes"] == ["grid"]


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_board_columns_come_from_suggestions_then_cards_and_the_busy_ones_lead():
    def run(script):
        out = subprocess.run([NODE, "-e", "const c = require('./static/js/nd-board.js');" + script], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    assert run("console.log(JSON.stringify(c.columnValues(['active','complete'], ['complete','failed','', 'active'])))") == ["active", "complete", "failed", ""]
    ordered = run("console.log(JSON.stringify(c.orderColumns([{value:'x',count:0},{value:'b',count:2},{value:'a',count:1},{value:'c',count:0}]).map(o => o.value)))")
    assert ordered == ["a", "b", "c", "x"]                                   # populated first (by name), then the empty ones


def _login(client, seed, player=False):
    client.cookies.clear()
    login(client, (seed.player_a if player else seed.gm).email, PLAYER_PASSWORD if player else GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_quest_page_carries_the_board_data_and_scripts_and_status_still_changes(client, seed):
    db = SessionLocal()
    try:
        q = Quest(world_id=seed.world_a.id, title="Find the ledger", status="active", category="main", visible_to_players=True, created_by_user_id=seed.gm.id)
        db.add(q)
        db.add(Quest(world_id=seed.world_a.id, title="Ancient debt", status="complete", category="side", visible_to_players=True))
        db.commit()
        qid = q.id
    finally:
        db.close()
    _login(client, seed)
    page = client.get("/quests").text
    assert 'data-for="quest-cards"' in page and page.count('class="quest-card nd-item"') == 2 and 'data-g-status="active"' in page
    assert "/static/js/nd-board.js" in page and "/static/js/quest-views.js" in page and 'data-can-edit="1"' in page
    assert client.post(f"/api/quests/{qid}/status", json={"status": "complete"}).json() == {"status": "complete"}
    from app.models import World
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).section_access_json = json.dumps({"quests": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    _login(client, seed, player=True)
    seen = client.get("/quests").text
    assert 'data-for="quest-cards"' in seen and 'data-can-edit=""' in seen                       # players see the layouts but cannot drag
    assert client.post(f"/api/quests/{qid}/status", json={"status": "failed"}).status_code == 403  # and the server holds the line
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).section_access_json = None             # leave the world as the other tests expect it
        db.commit()
    finally:
        db.close()


def test_the_sessions_section_and_page(client, seed):
    from app.models import GameSession, Party
    sp = view_prefs.spec("sessions")
    assert [m["id"] for m in sp["modes"]] == ["grid", "compact"] and sp["default_sort"] == "-num" and [g["id"] for g in sp["groups"]] == ["party"]
    assert view_prefs.clean("sessions", {"sort": "-num"})["sort"] == "-num" and view_prefs.clean("sessions", {"sort": "num"})["sort"] == "num"
    db = SessionLocal()
    try:
        party = Party(world_id=seed.world_a.id, name="Vanguard")
        db.add(party)
        db.commit()
        db.add(GameSession(world_id=seed.world_a.id, title="Night market", session_num=2, party_id=party.id))
        db.add(GameSession(world_id=seed.world_a.id, title="The heist", session_num=1))
        db.commit()
    finally:
        db.close()
    _login(client, seed)
    page = client.get("/sessions").text
    assert 'data-for="session-cards"' in page and page.count('class="entity-card session-card nd-item"') == 2
    assert 'data-num="2"' in page and 'data-g-party="Vanguard"' in page


def test_the_characters_section_and_page(client, seed):
    from app.models import PlayerCharacter
    sp = view_prefs.spec("characters")
    assert [m["id"] for m in sp["modes"]] == ["grid", "compact", "wall"] and {s["id"]: s["label"] for s in sp["sorts"]} == {"name": "Name", "num": "Level"}
    assert [g["id"] for g in sp["groups"]] == ["party", "owner", "system"]
    db = SessionLocal()
    try:
        db.add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Vex", level=3))
        db.add(PlayerCharacter(world_id=seed.world_a.id, name="Mira", level=5))
        db.commit()
    finally:
        db.close()
    for player in (False, True):
        _login(client, seed, player)
        page = client.get("/characters").text
        assert 'data-for="pc-roster"' in page and 'class="pc-card nd-item"' in page and 'data-num="3"' in page
