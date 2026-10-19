"""Board notes (/boards/<slug>/notes) and the Session Log's layouts: the registry sections and the pages' wiring for the GM and for a player."""
import json

from app import view_prefs
from app.database import SessionLocal
from app.models import GameSession, InvestBoard

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_registry_sections():
    bn = view_prefs.spec("board_notes")
    assert [m["id"] for m in bn["modes"]] == ["timeline", "cards", "outline", "split"]
    assert {s["id"]: s["label"] for s in bn["sorts"]} == {"date": "Board order", "name": "Name"} and bn["default_sort"] == "date"
    assert [g["id"] for g in bn["groups"]] == ["type", "status"]
    sl = view_prefs.spec("session_log")
    assert [m["id"] for m in sl["modes"]] == ["timeline", "grid", "compact"] and {m["id"]: m["label"] for m in sl["modes"]}["timeline"] == "List"
    assert sl["default_sort"] == "-num" and [g["id"] for g in sl["groups"]] == ["recap"] and sl["size_modes"] == ["grid"]


def _as(client, seed, gm):
    client.cookies.clear()
    login(client, *((seed.gm.email, GM_PASSWORD) if gm else (seed.player_a.email, PLAYER_PASSWORD)))
    client.cookies.set("active_world", seed.world_a.slug)


def test_board_notes_page(client, seed):
    nodes = [{"id": "a", "type": "person", "title": "The Baron", "body": "Owes <b>money</b>.", "tags": ["noble"], "status": "suspect", "x": 0, "y": 0},
             {"id": "b", "type": "clue", "title": "Torn ledger", "body": "", "tags": [], "status": "verified", "x": 5, "y": 5}]
    db = SessionLocal()
    try:
        db.add(InvestBoard(world_id=seed.world_a.id, name="Case File", slug="case-notes", nodes_json=json.dumps({"nodes": nodes, "groups": []}),
                           edges_json=json.dumps([{"from": "a", "to": "b"}])))
        db.commit()
    finally:
        db.close()
    _as(client, seed, True)
    page = client.get("/boards/case-notes/notes").text
    assert 'data-for="bn-notes"' in page and page.count('class="pn-note nd-item"') == 2 and 'id="nv-reader"' in page
    assert 'data-g-type="Person"' in page and 'data-g-status="Suspect"' in page and 'data-g-status="Verified"' in page
    assert "Connected to: Torn ledger" in page and "&lt;b&gt;money&lt;/b&gt;" in page and "<b>money</b>" not in page      # plain text, escaped
    assert "/boards/case-notes/notes" in client.get("/boards/case-notes").text                                           # the board links to it
    assert client.get("/boards/nope/notes").status_code == 404


def test_session_log_list_has_the_switcher(client, seed):
    db = SessionLocal()
    try:
        for n in (1, 2):
            db.add(GameSession(world_id=seed.world_a.id, title=f"Night {n}", session_num=n))
        db.commit()
    finally:
        db.close()
    _as(client, seed, False)
    r = client.get("/session-log")
    assert r.status_code == 200
    page = r.text
    assert 'data-for="sl-list"' in page and page.count('class="sl-card nd-item"') == 2 and 'data-num="2"' in page and 'data-g-recap="No recap yet"' in page
