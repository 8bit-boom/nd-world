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


def test_the_gm_looking_in_gets_no_cockpit_tab(client, seed):
    mine = _pc(seed, "a", "Anders")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/characters/{mine}").text
    assert 'data-tab="cockpit"' not in html and 'id="pch-cockpit-frame"' not in html


def test_the_character_list_links_to_the_cockpit(client, seed):
    mine = _pc(seed, "a", "Anders")
    _player(client, seed)
    html = client.get("/characters").text
    assert re.search(rf'<a href="/player-cockpit\?pc={mine}[^"]*"[^>]*>🎛 Cockpit</a>', html), "a labelled button to the cockpit"
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_html = client.get("/characters").text
    assert "🎛 GM Cockpit" in gm_html and "/player-cockpit?pc=" not in gm_html


def test_embedded_cockpit_hides_its_exit_button():
    from pathlib import Path
    tpl = (Path(__file__).parent.parent / "app" / "templates" / "cockpit.html").read_text()
    assert 'id="ck-exit"' in tpl and "body.nd-embed #ck-exit" in tpl
