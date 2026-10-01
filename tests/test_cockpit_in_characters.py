"""The Player Cockpit lives inside Player Characters: a Cockpit tab on the character hub (an embedded
cockpit focused on THAT character), a Cockpit button on the character list, and /player-cockpit?pc=
that opens on the player's own character (never someone else's)."""
import json
import re

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _pc(seed, owner, name):
    uid = {"a": seed.player_a.id, "b": seed.player_b.id, None: None}[owner]
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=uid, name=name)
        db.add(pc)
        db.commit()
        return pc.id
    finally:
        db.close()


def _player(client, seed, who="a"):
    p = seed.player_a if who == "a" else seed.player_b
    login(client, p.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _const(html, name):
    m = re.search(rf"const {name} = (.*?);\n", html)
    assert m, name
    return json.loads(m.group(1))


def test_the_cockpit_opens_focused_on_the_players_own_character(client, seed):
    mine, other = _pc(seed, "a", "Anders"), _pc(seed, "b", "Vex")
    _player(client, seed)
    html = client.get(f"/player-cockpit?pc={mine}").text
    assert _const(html, "CK_FOCUS_PC") == mine
    assert [m["name"] for m in _const(html, "CK_MY_PCS")] == ["Anders"], "only the viewer's own characters"
    for bad in (str(other), "999999", "abc", "-1", "0"):
        assert _const(client.get(f"/player-cockpit?pc={bad}").text, "CK_FOCUS_PC") is None, bad
    assert _const(client.get("/player-cockpit").text, "CK_FOCUS_PC") is None


def test_the_cockpit_header_says_player_cockpit_for_players(client, seed):
    _player(client, seed)
    html = client.get("/player-cockpit").text
    assert "Player Cockpit" in html.split('id="ck-head"')[1].split("</header>")[0]
    assert "GM Cockpit" not in html.split('id="ck-head"')[1].split("</header>")[0]


def test_the_hub_has_a_cockpit_tab_for_the_owner(client, seed):
    mine = _pc(seed, "a", "Anders")
    _player(client, seed)
    html = client.get(f"/characters/{mine}").text
    assert 'data-tab="cockpit"' in html and 'data-panel="cockpit"' in html
    frame = re.search(r'<iframe[^>]*id="pch-cockpit-frame"[^>]*>', html).group(0)
    assert f"/player-cockpit?pc={mine}" in frame and "embed=1" in frame
    assert " src=" not in frame.replace("data-src=", ""), "the cockpit loads only when the tab is opened"
    assert "'cockpit'" in html.split("const TABS")[1].split(";")[0]


def test_the_gm_looking_in_gets_the_cockpit_tab_too(client, seed):
    mine = _pc(seed, "a", "Anders")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/characters/{mine}").text
    assert 'data-tab="cockpit"' in html and 'id="pch-cockpit-frame"' in html
    frame = re.search(r'<iframe[^>]*id="pch-cockpit-frame"[^>]*>', html).group(0)
    assert f"/player-cockpit?pc={mine}" in frame and "embed=1" in frame
    assert "'cockpit'" in html.split("const TABS")[1].split(";")[0]


def _party(seed, *pc_ids, name="Crew"):
    from app.models import Party
    db = SessionLocal()
    try:
        p = Party(world_id=seed.world_a.id, name=name, member_pc_ids_json=json.dumps(list(pc_ids)))
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_the_gm_can_open_the_cockpit_as_a_character_sees_it(client, seed):
    anders, anders2 = _pc(seed, "a", "Anders"), _pc(seed, "a", "Anders II")
    vex = _pc(seed, "b", "Vex")
    crew, strangers = _party(seed, anders, name="Crew"), _party(seed, vex, name="Strangers")
    _gm(client, seed)
    html = client.get(f"/player-cockpit?pc={anders}").text
    assert _const(html, "CK_FOCUS_PC") == anders and _const(html, "CK_VIEW_AS") is True
    assert sorted(m["name"] for m in _const(html, "CK_MY_PCS")) == ["Anders", "Anders II"], "the owner's characters, not the GM's"
    assert [p["id"] for p in _const(html, "CK_PARTIES")] == [crew], "only that player's parties"
    # an unowned character stands alone
    npc = _pc(seed, None, "Loose Cannon")
    html = client.get(f"/player-cockpit?pc={npc}").text
    assert _const(html, "CK_FOCUS_PC") == npc and [m["name"] for m in _const(html, "CK_MY_PCS")] == ["Loose Cannon"]
    # a plain GM visit is the GM's own view
    html = client.get("/player-cockpit").text
    assert _const(html, "CK_FOCUS_PC") is None and _const(html, "CK_VIEW_AS") is False and _const(html, "CK_MY_PCS") == []
    assert strangers != crew


def test_a_player_never_gets_the_view_as_behaviour(client, seed):
    anders, vex = _pc(seed, "a", "Anders"), _pc(seed, "b", "Vex")
    _party(seed, vex, name="Strangers")
    _player(client, seed)
    html = client.get(f"/player-cockpit?pc={vex}").text
    assert _const(html, "CK_FOCUS_PC") is None and _const(html, "CK_VIEW_AS") is False
    assert [m["name"] for m in _const(html, "CK_MY_PCS")] == ["Anders"] and _const(html, "CK_PARTIES") == []
    board = client.get(f"/api/cockpit/player-board?pc={vex}").json()
    assert [m["name"] for m in board["my_pcs"]] == ["Anders"] and board["parties"] == [], "?pc= on the board is GM-only"


def test_the_board_follows_the_character_for_a_gm(client, seed):
    anders, vex = _pc(seed, "a", "Anders"), _pc(seed, "b", "Vex")
    crew = _party(seed, anders, name="Crew")
    _party(seed, vex, name="Strangers")
    _gm(client, seed)
    board = client.get(f"/api/cockpit/player-board?pc={anders}").json()
    assert [m["name"] for m in board["my_pcs"]] == ["Anders"] and [p["id"] for p in board["parties"]] == [crew]
    plain = client.get("/api/cockpit/player-board").json()
    assert plain["my_pcs"] == [] and plain["parties"] == [], "no ?pc= : the GM's own (empty) board"
    other_world = client.get("/api/cockpit/player-board?pc=999999").json()
    assert other_world["my_pcs"] == []


def test_a_gm_cannot_focus_a_character_of_another_world(client, seed):
    from app.models import PlayerCharacter as PC
    db = SessionLocal()
    try:
        owned = PC(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Elsewhere")
        loose = PC(world_id=seed.world_b.id, owner_user_id=None, name="Elsewhere Loose")
        db.add_all([owned, loose])
        db.commit()
        foreign = [owned.id, loose.id]
    finally:
        db.close()
    _gm(client, seed)
    for fid in foreign:   # owned and unowned alike
        html = client.get(f"/player-cockpit?pc={fid}").text
        assert _const(html, "CK_FOCUS_PC") is None and _const(html, "CK_VIEW_AS") is False and _const(html, "CK_MY_PCS") == [], fid
        assert client.get(f"/api/cockpit/player-board?pc={fid}").json()["my_pcs"] == [], fid


def test_the_character_list_links_to_the_cockpit(client, seed):
    mine = _pc(seed, "a", "Anders")
    _player(client, seed)
    html = client.get("/characters").text
    assert re.search(rf'<a href="/player-cockpit\?pc={mine}[^"]*"[^>]*>🎛 Cockpit</a>', html), "a labelled button to the cockpit"
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_html = client.get("/characters").text
    assert "🎛 GM Cockpit" in gm_html and "/player-cockpit?pc=" not in gm_html


def test_a_gm_viewing_as_a_player_keeps_a_separate_saved_layout():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static" / "js" / "cockpit.js").read_text()
    assert js.count("'nd_cockpit_ws_' + CK_WORLD") == 1, "the storage key is built in one place"
    assert "(CK_VIEW_AS ? '_as_player' : '')" in js and js.count("localStorage.getItem(LS_KEY)") == 2 and "localStorage.setItem(LS_KEY" in js
    assert js.count("fetch(BOARD_URL)") == 2 and "player-board' + (CK_VIEW_AS" in js


def test_embedded_cockpit_hides_its_exit_button():
    from pathlib import Path
    tpl = (Path(__file__).parent.parent / "app" / "templates" / "cockpit.html").read_text()
    assert 'id="ck-exit"' in tpl and "body.nd-embed #ck-exit" in tpl
