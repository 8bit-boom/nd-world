"""A player's home page is a start page (rules, character creation, setting introduction, sheets ...); a GM and a GM-Assistant keep the dashboard.
A Page can be marked as the setting introduction."""
import json
import os

import httpx
import pytest

from app.database import SessionLocal
from app.models import CharacterSheet, PageDoc, PlayerCharacter, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "")


def _doc(world_id, name, **kw):
    db = SessionLocal()
    try:
        d = PageDoc(world_id=world_id, name=name, file_url="/uploads/pages/x.html", visible_to_players=kw.pop("visible", True), **kw)
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


def _as(client, seed, who):
    if who == "gm":
        login(client, seed.gm.email, GM_PASSWORD)
    else:
        login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    return client


def test_a_player_gets_the_start_page_and_a_gm_keeps_the_dashboard(client, seed):
    _doc(seed.world_a.id, "Welcome to Neon City", description="Start here", is_setting_intro=True)
    _doc(seed.world_a.id, "Hidden intro", is_setting_intro=True, visible=False)
    _doc(seed.world_a.id, "Blank sheet", is_character_sheet_template=True)
    h = _as(client, seed, "player").get("/").text
    assert "Start here" in h and "Read the setting introduction" in h and "Welcome to Neon City" in h and "Hidden intro" not in h
    assert "Learn the rules" in h and "Create your character" in h and "New sheet: Blank sheet" in h and "At the table" in h
    assert 'id="home-stack"' not in h and "Customize this page" not in h
    g = _as(client, seed, "gm").get("/").text
    assert 'id="home-stack"' in g and "Read the setting introduction" not in g
    p = client.get("/?as=player").text
    assert "This is the start page your players see" in p and "Read the setting introduction" in p and 'id="home-stack"' not in p
    # a player cannot ask for the GM's page
    assert 'id="home-stack"' not in _as(client, seed, "player").get("/?as=gm").text


def test_the_steps_follow_what_the_player_has_and_may_open(client, seed):
    c = _as(client, seed, "player")
    assert "✓ done" not in c.get("/").text and "Create a character" in c.get("/").text
    db = SessionLocal()
    try:
        db.add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Kira", race="Elf", char_class="Rogue", level=3))
        w = db.get(World, seed.world_a.id)
        w.section_access_json = json.dumps({"rules": {"player": "none"}, "dice": {"player": "none"}})
        db.commit()
    finally:
        db.close()
    h = c.get("/").text
    assert "✓ done" in h and "Kira" in h and "Elf · Rogue · level 3" in h and "Your characters" in h
    assert "Learn the rules" not in h and ">Dice<" not in h and "Create a character" not in h   # the default limit is one character


def test_marking_a_page_as_the_setting_introduction(client, seed):
    did = _doc(seed.world_a.id, "Intro", is_setting_intro=False)
    gm = _as(client, seed, "gm")
    r = gm.post(f"/pages/{did}/edit", data={"name": "Intro", "visible_to_players": "1", "is_setting_intro": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(PageDoc, did).is_setting_intro is True
    finally:
        db.close()
    assert "📖 setting introduction" in gm.get("/pages").text
    assert "Intro" in _as(client, seed, "player").get("/").text
    gm = _as(client, seed, "gm")
    gm.post(f"/pages/{did}/edit", data={"name": "Intro", "visible_to_players": "1"}, follow_redirects=False)
    assert "Read the setting introduction" not in _as(client, seed, "player").get("/").text


def test_my_sheets_are_listed(client, seed):
    tid = _doc(seed.world_a.id, "Blank sheet", is_character_sheet_template=True)
    db = SessionLocal()
    try:
        s = CharacterSheet(world_id=seed.world_a.id, template_id=tid, owner_user_id=seed.player_a.id, name="Kira's sheet")
        db.add(s)
        db.commit()
        sid = s.id
    finally:
        db.close()
    h = _as(client, seed, "player").get("/").text
    assert f"/pages/sheets/{sid}" in h and "Kira&#39;s sheet" in h and "✓ started" in h


@pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")
def test_the_start_page_is_the_same_through_the_gateway(client, seed):
    _doc(seed.world_a.id, "Welcome to Neon City", description="Start here", is_setting_intro=True)
    _doc(seed.world_a.id, "Blank sheet", is_character_sheet_template=True)
    db = SessionLocal()
    try:
        db.add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Kira", race="Elf", char_class="Rogue", level=3))
        db.commit()
    finally:
        db.close()
    for email, pw, path in ((seed.player_a.email, PLAYER_PASSWORD, "/"), (seed.gm.email, GM_PASSWORD, "/?as=player")):
        pair = []
        for base in (DIRECT, str(client.base_url)):
            c = httpx.Client(base_url=base, timeout=60)
            r = c.post("/login", data={"email": email, "password": pw, "next": "/"}, follow_redirects=False)
            assert r.status_code == 303
            c.cookies.set("active_world", seed.world_a.slug)
            pair.append(c.get(path))
        assert pair[0].status_code == pair[1].status_code == 200
        assert pair[0].text == pair[1].text, path
        assert pair[1].headers.get("x-nd-served-by") == "gateway"
