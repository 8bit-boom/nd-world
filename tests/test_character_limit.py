"""How many characters a player may have in a world.

It used to be a hard 1 ("You already have a character in this world."), so a player whose character died, or who wanted
an alt, could not make another themselves. Now the GM sets "Characters per player" on the world (default 1, so nothing
changes until raised) and every place that enforced the old rule honours it: the new-character form and POST, the
Editor/Android sync create, and the GM assigning a character to a player. Battle maps — which assumed one character per
player — let a player move the token of any of their characters."""
import json

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter, Schematic, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _player(client, seed, world=None):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


def _set_limit(client, seed, n, world=None):
    """Through the real world-settings form, as the GM."""
    w = world or seed.world_a
    _gm(client, seed, w)
    r = client.post(f"/worlds/{w.id}/edit", data={"name": w.name, "max_characters_per_player": str(n)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def _limit_in_db(world_id):
    db = SessionLocal()
    try:
        return db.get(World, world_id).max_characters_per_player
    finally:
        db.close()


def _create(client, name):
    return client.post("/characters/new", data={"name": name}, follow_redirects=False)


def _mine(seed):
    db = SessionLocal()
    try:
        return db.query(PlayerCharacter).filter(
            PlayerCharacter.world_id == seed.world_a.id, PlayerCharacter.owner_user_id == seed.player_a.id
        ).order_by(PlayerCharacter.id).all()
    finally:
        db.close()


# ── the default is still one ────────────────────────────────────────────────────────────────────

def test_default_is_one_character_and_the_message_is_unchanged(client, seed):
    assert _limit_in_db(seed.world_a.id) == 1
    _player(client, seed)
    assert _create(client, "First").status_code == 303
    r = _create(client, "Second")
    assert r.status_code == 400
    assert r.json()["detail"] == "You already have a character in this world."
    assert len(_mine(seed)) == 1


# ── the GM raises it ────────────────────────────────────────────────────────────────────────────

def test_a_raised_limit_lets_the_player_create_up_to_it_and_no_more(client, seed):
    _set_limit(client, seed, 3)
    assert _limit_in_db(seed.world_a.id) == 3
    _player(client, seed)
    for n in ("One", "Two", "Three"):
        assert _create(client, n).status_code == 303, n
    r = _create(client, "Four")
    assert r.status_code == 400
    assert "3" in r.json()["detail"] and "limit" in r.json()["detail"].lower()
    assert [c.name for c in _mine(seed)] == ["One", "Two", "Three"]


def test_lowering_the_limit_never_deletes_anything_but_blocks_new_ones(client, seed):
    _set_limit(client, seed, 3)
    _player(client, seed)
    for n in ("A", "B", "C"):
        _create(client, n)
    _set_limit(client, seed, 2)
    assert len(_mine(seed)) == 3
    _player(client, seed)
    assert _create(client, "D").status_code == 400


def test_a_player_can_make_room_by_deleting_a_character(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    _create(client, "Old")
    _create(client, "Newer")
    assert _create(client, "Blocked").status_code == 400
    victim = _mine(seed)[0]
    r = client.post(f"/characters/{victim.id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303), r.text
    assert _create(client, "Replacement").status_code == 303


def test_the_limit_is_per_world(client, seed):
    _set_limit(client, seed, 5)                                  # world A only
    assert _limit_in_db(seed.world_b.id) == 1


@pytest.mark.parametrize("sent, stored", [("0", 1), ("-4", 1), ("999", 20), ("abc", 1), ("", 1), ("7", 7), (" 4 ", 4)])
def test_the_setting_is_clamped_to_a_sane_range(client, seed, sent, stored):
    _set_limit(client, seed, sent)
    assert _limit_in_db(seed.world_a.id) == stored


def test_leaving_the_field_out_of_the_form_keeps_the_current_limit(client, seed):
    """Another client (or an older cached page) posting the settings form without the field must not reset it."""
    _set_limit(client, seed, 4)
    _gm(client, seed)
    client.post(f"/worlds/{seed.world_a.id}/edit", data={"name": "World A"}, follow_redirects=False)
    assert _limit_in_db(seed.world_a.id) == 4


def test_gm_characters_are_never_limited(client, seed):
    _gm(client, seed)
    for n in ("N1", "N2", "N3"):
        assert _create(client, n).status_code == 303


# ── the form page ───────────────────────────────────────────────────────────────────────────────

def test_new_form_opens_while_under_the_limit_and_redirects_at_it(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    assert client.get("/characters/new", follow_redirects=False).status_code == 200
    _create(client, "One")
    assert client.get("/characters/new", follow_redirects=False).status_code == 200      # 1 of 2: still room
    _create(client, "Two")
    r = client.get("/characters/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/characters")        # full: back to the list


def test_new_form_with_a_limit_of_one_still_goes_straight_to_the_sheet(client, seed):
    _player(client, seed)
    _create(client, "Solo")
    r = client.get("/characters/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/characters/{_mine(seed)[0].id}"


def test_list_page_offers_new_character_only_while_there_is_room(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    page = client.get("/characters").text
    assert "+ New Character" in page and "0 of 2" in page
    _create(client, "One")
    page = client.get("/characters").text
    assert "+ New Character" in page and "1 of 2" in page
    _create(client, "Two")
    page = client.get("/characters").text
    assert "+ New Character" not in page and "Create with AI" not in page
    assert "2 of 2" in page and "limit" in page.lower()
    _gm(client, seed)
    assert "+ New Character" in client.get("/characters").text                          # the GM always can


def test_every_one_of_my_characters_is_marked_on_the_list(client, seed):
    _set_limit(client, seed, 3)
    _player(client, seed)
    _create(client, "One")
    _create(client, "Two")
    page = client.get("/characters").text
    assert page.count("outline:2px solid var(--neon)") == 2


# ── the sync API (Editor / Android) ─────────────────────────────────────────────────────────────

def test_sync_create_honours_the_limit(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    url = f"/api/worlds/{seed.world_a.id}/characters/sync"
    assert client.post(url, json={"name": "S1"}).status_code == 200
    assert client.post(url, json={"name": "S2"}).status_code == 200
    r = client.post(url, json={"name": "S3"})
    assert r.status_code == 400 and "limit" in r.json()["detail"].lower()


def test_sync_create_default_message_is_unchanged(client, seed):
    _player(client, seed)
    url = f"/api/worlds/{seed.world_a.id}/characters/sync"
    client.post(url, json={"name": "S1"})
    r = client.post(url, json={"name": "S2"})
    assert r.status_code == 400 and "use PUT" in r.json()["detail"]


def test_api_me_lists_all_my_characters_and_the_limit(client, seed):
    _set_limit(client, seed, 3)
    _player(client, seed)
    _create(client, "One")
    _create(client, "Two")
    ids = [c.id for c in _mine(seed)]
    me = client.get("/api/me").json()
    w = next(x for x in me["worlds"] if x["id"] == seed.world_a.id)
    assert w["character_id"] == ids[0]                  # existing clients keep reading this one
    assert w["character_ids"] == ids
    assert w["max_characters"] == 3


# ── the GM assigning a character to a player ────────────────────────────────────────────────────

def _gm_made_pc(seed, name):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name=name)
        db.add(pc)
        db.commit()
        return pc.id
    finally:
        db.close()


def test_gm_can_assign_a_second_character_only_when_the_limit_allows(client, seed):
    a, b = _gm_made_pc(seed, "NPC-A"), _gm_made_pc(seed, "NPC-B")
    _gm(client, seed)
    assert client.post(f"/characters/{a}/owner", data={"owner_user_id": str(seed.player_a.id)}, follow_redirects=False).status_code == 303
    r = client.post(f"/characters/{b}/owner", data={"owner_user_id": str(seed.player_a.id)}, follow_redirects=False)
    assert r.status_code == 400 and "NPC-A" in r.json()["detail"]                      # the old one-character wording
    _set_limit(client, seed, 2)
    _gm(client, seed)
    assert client.post(f"/characters/{b}/owner", data={"owner_user_id": str(seed.player_a.id)}, follow_redirects=False).status_code == 303
    c = _gm_made_pc(seed, "NPC-C")
    r = client.post(f"/characters/{c}/owner", data={"owner_user_id": str(seed.player_a.id)}, follow_redirects=False)
    assert r.status_code == 400 and "limit" in r.json()["detail"].lower()


# ── battle maps: a player moves any of their characters' tokens ─────────────────────────────────

def _map_with_tokens(seed, pc_ids):
    db = SessionLocal()
    try:
        els = [{"id": f"t{pid}", "type": "token", "x": 10, "y": 10, "pc_id": pid, "visible_to_players": True}
               for pid in pc_ids]
        s = Schematic(world_id=seed.world_a.id, slug="battle", name="Battle", elements_json=json.dumps(els))
        db.add(s)
        db.commit()
        return s.slug
    finally:
        db.close()


def test_a_player_moves_the_token_of_any_of_their_characters_but_not_anyone_elses(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    _create(client, "One")
    _create(client, "Two")
    mine = [c.id for c in _mine(seed)]
    other = _gm_made_pc(seed, "Stranger")
    slug = _map_with_tokens(seed, mine + [other])
    for pid in mine:
        r = client.post(f"/api/maps/schematic/{slug}/move-token", json={"token_id": f"t{pid}", "x": 42, "y": 43})
        assert r.status_code == 200, (pid, r.text)
    r = client.post(f"/api/maps/schematic/{slug}/move-token", json={"token_id": f"t{other}", "x": 1, "y": 1})
    assert r.status_code == 403
    db = SessionLocal()
    try:
        els = {e["id"]: e for e in json.loads(db.query(Schematic).filter(Schematic.slug == slug).first().elements_json)}
    finally:
        db.close()
    assert (els[f"t{mine[0]}"]["x"], els[f"t{mine[1]}"]["y"]) == (42, 43)
    assert els[f"t{other}"]["x"] == 10


def test_map_page_tells_the_browser_every_character_it_may_drag(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    _create(client, "One")
    _create(client, "Two")
    mine = [c.id for c in _mine(seed)]
    slug = _map_with_tokens(seed, mine)
    html = client.get(f"/maps/schematic/{slug}/view").text
    assert f"const OWN_PC_IDS = {json.dumps(mine)}" in html.replace("\n", "")


# ── the AI character creator respects it too (no drafting a character that cannot be saved) ─────

def test_ai_creator_page_and_start_stop_at_the_limit(client, seed):
    _set_limit(client, seed, 2)
    _player(client, seed)
    assert client.get("/characters/ai-new", follow_redirects=False).status_code == 200
    _create(client, "One")
    assert client.get("/characters/ai-new", follow_redirects=False).status_code == 200
    _create(client, "Two")
    r = client.get("/characters/ai-new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/characters")
    r = client.post("/api/characters/ai/start", data={"prompt": "a grumpy dwarf"})
    assert r.status_code == 400 and "limit" in r.json()["detail"].lower()
