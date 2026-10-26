def test_npc_candidates_are_pythons(client, seed):
    """GET /sessions/<id>/npc-candidates from the gateway: the picker's candidates - the world's entities grouped by kind
    (NPCs split from subtype-PC lore write-ups, then creatures, locations, organizations, races, professions, notes), each
    prefixed with its group label, then the real characters as "pc:<id>" - for a GM and a player; another world's session and
    a missing one are the same 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, GameSession, PlayerCharacter
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            Entity(world_id=w, kind="character", name="Vosk", folder="Dockside"),
            Entity(world_id=w, kind="character", name="Kira", subtype="PC"),
            Entity(world_id=w, kind="creature", name="Rat"),
            Entity(world_id=w, kind="location", name="The Pier"),
            Entity(world_id=w, kind="organization", name="Kraun Corp"),
            Entity(world_id=w, kind="race", name="Elf"),
            Entity(world_id=w, kind="profession", name="Netrunner"),
            Entity(world_id=w, kind="note", name="Bribes"),
            Entity(world_id=seed.world_b.id, kind="note", name="Elsewhere"),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Zed"),
        ])
        gs_a = GameSession(world_id=w, title="Docks", session_num=1)
        gs_b = GameSession(world_id=seed.world_b.id, title="There", session_num=1)
        db.add_all([gs_a, gs_b])
        db.commit()
        ids = (gs_a.id, gs_b.id, 999999)
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    for c in (d, pl):
        for i in ids:
            url = f"/sessions/{i}/npc-candidates"
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (i, a.status_code, a.text[:200], b.text[:200])
    served = sum(1 for c in (d, pl) for i in ids[:1] if _via(c, base).get(f"/sessions/{i}/npc-candidates").headers.get("x-nd-served-by") == "gateway")
    assert served == 2, served
    out = d.get(f"/sessions/{ids[0]}/npc-candidates").json()["candidates"]
    folders = {c["folder"] for c in out}
    assert "\U0001f464 NPCs/Dockside" in folders and "\U0001f9d1 Player Characters" in folders, out
    assert {"\u2620 Creatures", "\U0001f5fa Locations", "\U0001f3e2 Organizations", "\U0001f9ec Races",
            "\U0001f3ad Professions", "\U0001f4c4 Notes"} <= folders, folders
    pc_rows = [c for c in out if str(c["id"]).startswith("pc:")]
    assert len(pc_rows) == 1 and pc_rows[0]["name"] == "Zed", pc_rows
    npc_rows = [c for c in out if c["name"] == "Vosk"]
    assert npc_rows[0]["folder"] == "\U0001f464 NPCs/Dockside", npc_rows
    # Kira (subtype PC) sits in the Player Characters group, not the NPCs one
    kira = [c for c in out if c["name"] == "Kira"]
    assert kira[0]["folder"] == "\U0001f9d1 Player Characters", kira


def test_character_new_and_edit_forms_are_pythons(client, seed):
    """GET /characters/new and GET /characters/<id>/edit from the gateway: the wizard (the N&D default template, or the
    ?template_id= one - a custom mode template gets its own form page), the edit form with the row's live JSON columns; a
    player at their character limit redirected as Python redirects, a custom-mode character's edit a redirect to its page,
    a stranger 403, a template id Python's int() would refuse left to Python."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        stats = json.dumps([{"id": "str", "label": "STR", "value": 3}])
        tpl = SheetTemplate(world_id=w, name="Mekton", slug="mekton-page", sheet_mode="custom",
                            fields_json=json.dumps([{"id": "f1", "label": "Skill", "type": "text", "section": "Play"}]))
        nd_tpl = SheetTemplate(world_id=w, name="Second N&D", slug="second-nd", sheet_mode="nd", fields_json="[]")
        db.add_all([tpl, nd_tpl])
        db.flush()
        pcs = [PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=2, xp=9,
                               stats_json=stats, currency_json=json.dumps([{"label": "Creds", "abbr": "CR", "value": 4}]),
                               equipment_json="[]", feats_json="[]", cyberware_json="[]",
                               custom_fields_json=json.dumps({"f1": "x"})),
               PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom Kid", sheet_template_id=tpl.id,
                               custom_fields_json=json.dumps({"f1": "y"})),
               PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There", level=1)]
        db.add_all(pcs)
        db.commit()
        ids = (pcs[0].id, pcs[1].id, pcs[2].id)
        tpl_id = tpl.id
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    via = _via(d, base)
    via_g = _via(g, base)
    served = 0
    for who, c, v in (("player", d, via), ("gm", g, via_g)):
        for url in ("/characters/new", f"/characters/new?template_id={tpl_id}", "/characters/new?template_id=999999",
                    f"/characters/{ids[0]}/edit", f"/characters/{ids[1]}/edit", f"/characters/{ids[2]}/edit",
                    "/characters/999999/edit"):
            served += _same_page(c, v, url, who)
        # a template id Python's int() refuses: the same 422 from either side, unanswered by the gateway
        a, b = c.get("/characters/new?template_id=x"), v.get("/characters/new?template_id=x")
        assert a.status_code == b.status_code == 422, (who, a.status_code, b.status_code)
    assert served >= 6, served
    # the wizard really carries the N&D default template for an unnamed request
    page = d.get("/characters/new").text
    assert "nd-default" in page or "Neon" in page, page[:200]
    # the player at the limit: one character, limit 1 -> straight to their sheet
    db = SessionLocal()
    try:
        db.get(World, w).max_characters_per_player = 1
        db.commit()
    finally:
        db.close()
    a, b = d.get("/characters/new", follow_redirects=False), via.get("/characters/new", follow_redirects=False)
    assert a.status_code == b.status_code == 303, (a.status_code, b.status_code)
    assert a.headers["location"] == b.headers["location"] == f"/characters/{ids[0]}"
    assert b.headers.get("x-nd-served-by") == "gateway"
    # two characters, limit 1: back to the list
    db = SessionLocal()
    try:
        db.add(PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Second", level=1))
        db.commit()
    finally:
        db.close()
    a, b = d.get("/characters/new", follow_redirects=False), via.get("/characters/new", follow_redirects=False)
    assert a.status_code == b.status_code == 303 and a.headers["location"] == b.headers["location"] == "/characters"
    # a stranger's edit: 403 with Python's body
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    a, b = pb.get(f"/characters/{ids[0]}/edit"), _via(pb, base).get(f"/characters/{ids[0]}/edit")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"}), a.text
