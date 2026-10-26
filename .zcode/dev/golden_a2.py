def test_character_vitals_sync_advance_abilitycards_are_pythons(client, seed):
    """GET /api/characters/<id>/vitals, /sync, /advance and /ability-cards from the gateway: the live numbers with the sheet's
    effective maxima, the live-sync dict, the XP-shop options (a custom system's 400 in Python's wording) and the feat cards - the
    gates Python applies: 404 for a missing row, 403 for a stranger (vitals lets a party-member's world through when the world shows
    it, sync/advance/cards only the GM or the owner, of any world), and a GM reading a character of a world they only oversee."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate, World
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": k, "label": v, "value": n} for k, v, n in
                        (("str", "STR", 3), ("dex", "DEX", 2), ("bod", "BOD", 4), ("per", "PER", 1),
                         ("wil", "WIL", 2), ("int", "INT", 3), ("cha", "CHA", 1), ("itu", "ITU", 2))])
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Mekton", slug="mekton-test", sheet_mode="custom",
                            fields_json=json.dumps([{"id": "f1", "label": "Skill", "type": "text"}]))
        db.add(tpl)
        db.flush()
        pcs = [
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=3, xp=90,
                            current_hp=7, temp_hp=2, pp_current=4, mp_current=2, shock_current=1, armor_class=11,
                            speed=6, stats_json=stats, conditions_json='["Dazed", 5, {"x": 1}, null]'),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom", sheet_template_id=tpl.id,
                            custom_fields_json=json.dumps({"f1": "Pilot"}), level=2, xp=10),
            PlayerCharacter(world_id=w, owner_user_id=None, name="Orphan", level=1, xp=0),
            PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="NoOwnerView", level=1, xp=0),
            PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There", level=1, xp=0),
        ]
        db.add_all(pcs)
        db.get(World, w).players_see_party = False
        db.commit()
        db.get(World, w).players_see_party = True
        db.commit()
        ids = [p.id for p in pcs]
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    urls = [f"/api/characters/{i}/{tail}" for i in ids + [999999]
            for tail in ("vitals", "sync", "advance", "ability-cards")]
    # a member of the character's world while it hides the party: 403 on vitals, everywhere
    for c in (d, g, pb):
        for url in urls:
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (c.base_url, url)
    served = 0
    for c in (d, g, pb):
        for url in urls:
            served += _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway"
    assert served > 40, served
    # the player's own vitals really carry the cleaned conditions and the sheet maxima
    v = d.get(f"/api/characters/{ids[0]}/vitals").json()
    assert v["conditions"] == ["Dazed"], v
    assert v["max_hp"] == 20 and v["shock_max"] == 8 and v["temp_hp"] == 2, v
    # sync GET: the live-sync dict, the GM's copy of another world's character included
    sv = g.get(f"/api/characters/{ids[4]}/sync").json()
    assert sv["id"] == ids[4] and sv["name"] == "Over There", sv
    # advance: the native options, and the custom system's 400 in Python's words
    adv = d.get(f"/api/characters/{ids[0]}/advance").json()
    assert adv["available"] == 90, adv
    bad = d.get(f"/api/characters/{ids[1]}/advance")
    assert bad.status_code == 400 and bad.json()["detail"].startswith("Advancement here follows"), bad.text
    # ability-cards: pools from the row, the native flag per system
    cards = d.get(f"/api/characters/{ids[0]}/ability-cards").json()
    assert cards["pools"] == {"pp": 4, "mp": 2, "shock": 1, "hp": 7} and cards["native"] is True, cards
    custom_cards = d.get(f"/api/characters/{ids[1]}/ability-cards").json()
    assert custom_cards["native"] is False, custom_cards
    # a player's ?w= pointing elsewhere does not change the answer: these routes read the character's own world
    a, b = d.get(f"/api/characters/{ids[0]}/vitals?w=999999"), _via(d, base).get(f"/api/characters/{ids[0]}/vitals?w=999999")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, v), (a.status_code, a.text)


def test_character_export_json_md_are_pythons(client, seed):
    """GET /characters/<id>/export.json and /export.md from the gateway: the import-shape file (zero / empty fields omitted, the
    name defaulted, indent=2 without ASCII escapes) and the markdown file, byte for byte; a non-ASCII file name (Python's
    Unicode-aware isalnum keeps more) left to Python; 404 for a stranger, 403 for one who may not manage."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import PlayerCharacter, SheetTemplate
    base = str(client.base_url)
    w = seed.world_a.id
    stats = json.dumps([{"id": "str", "label": "STR", "value": 3}, {"id": "dex", "label": "DEX", "value": 2}])
    currency = json.dumps([{"label": "Gold", "abbr": "GP", "value": 12}, {"label": "Silver", "abbr": "SP", "value": 0.5}])
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=w, name="Asterion", slug="asterion-export", sheet_mode="custom", fields_json=json.dumps([
            {"id": "wpn", "label": "Weapon", "type": "text", "section": "Kit", "default_value": "Shortsword"},
            {"id": "armor", "label": "Armor", "type": "text", "section": "Kit"},
            {"id": "kit", "label": "Kit item", "type": "list", "section": "Kit",
             "item_fields": [{"id": "name", "label": "Name"}, {"id": "qty", "label": "Qty"}]},
            {"id": "hp", "label": "Health", "type": "resource", "section": "Vitals", "max": 12},
        ]))
        db.add(tpl)
        db.flush()
        full = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira Vane", player_name="Player A",
                               race="Elf", char_class="Rigger", level=3, xp=90, max_hp=20, current_hp=7,
                               stats_json=stats, currency_json=currency,
                               equipment_json=json.dumps([{"name": "Deck", "qty": 1, "notes": "cyberdeck"}, {"name": "Rope"}]),
                               feats_json=json.dumps([{"name": "Night eyes", "notes": "sees in the dark"}]),
                               conditions_json='["Dazed"]', custom_fields_json=json.dumps({"wpn": "Monowire", "armor": "", "kit": [{"name": "Medkit", "qty": 2}]}),
                               backstory="  Grew up on the docks.\n\nLeft early.  ", notes=" Notes. ",
                               personality_traits="Blunt", ideals=None, bonds="The crew", flaws="")
        empty = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="", level=0, xp=0)
        unicode_named = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kïra — the !! odd", level=1)
        on_tpl = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Custom Kid", sheet_template_id=tpl.id,
                                 custom_fields_json=json.dumps({"wpn": "Monowire", "kit": [{"name": "Medkit", "qty": 2}, {"name": ""}], "hp": 5}))
        world_b_pc = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Over There")
        db.add_all([full, empty, unicode_named, on_tpl, world_b_pc])
        db.commit()
        ids = [full.id, empty.id, unicode_named.id, on_tpl.id, world_b_pc.id, 999999]
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    served = 0
    for c in (d, g):
        for i in ids:
            for url in (f"/characters/{i}/export.json", f"/characters/{i}/export.md"):
                a, b = c.get(url), _via(c, base).get(url)
                assert a.status_code == b.status_code, (url, a.status_code, b.status_code, a.text[:120], b.text[:120])
                if b.headers.get("x-nd-served-by") == "gateway":
                    assert a.content == b.content, (url, a.text[:200], b.text[:200])
                    assert a.headers.get("content-type") == b.headers.get("content-type"), url
                    assert a.headers.get("content-disposition") == b.headers.get("content-disposition"), url
                    served += 1
    assert served >= 8, served
    # the JSON file really is the import shape: the name defaulted, empties omitted, floats as Python writes them
    j = d.get(f"/characters/{ids[0]}/export.json")
    payload = json.loads(j.content.decode("utf-8"))
    assert payload["name"] == "Kira Vane" and payload["level"] == 3 and "ideals" not in payload, payload
    assert payload["currency"][1]["value"] == 0.5 and payload["stats"][0]["value"] == 3, payload
    assert payload["custom_fields"] == {"wpn": "Monowire", "kit": [{"name": "Medkit", "qty": 2}]}, payload
    assert payload["conditions"] == ["Dazed"] and payload["sheet_template_id"] not in payload, payload
    assert b'"indent"' not in j.content and j.content.startswith(b'{\n  "name"'), j.content[:40]
    # an empty character exports as just the default name
    payload = json.loads(d.get(f"/characters/{ids[1]}/export.json").content.decode("utf-8"))
    assert payload == {"name": "Character"}, payload
    # the markdown file: sections in Python's order, the strip on the tail
    md = d.get(f"/characters/{ids[0]}/export.md").content.decode("utf-8")
    assert md.startswith("# Kira Vane\n") and "## Ability Scores\n- **STR:** 3" in md, md
    assert "- **Deck:** x1 — cyberdeck" in md and "## Conditions\n- **Active:** Dazed" in md, md
    assert md.endswith("Grew up on the docks.\n\nLeft early.\n\n## Notes\nNotes.\n"), md
    # the custom template's fields: the resource's short label, the list expanded, the default value filled
    md2 = d.get(f"/characters/{ids[3]}/export.md").content.decode("utf-8")
    assert "## Kit\n- **Weapon:** Monowire\n- **Kit item #1:** Name: Medkit; Qty: 2" in md2, md2
    assert "- **Health:** 5 / 12" in md2, md2
    # a non-ASCII name keeps Python's file name (the gateway declines)
    r = _via(d, base).get(f"/characters/{ids[2]}/export.json")
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") != "gateway"
    # a stranger may not manage: 404 for a player of another world on the GM's own file, 403 for a non-owner
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    for url in (f"/characters/{ids[4]}/export.json", f"/characters/{ids[4]}/export.md"):
        a, b = pb.get(url), _via(pb, base).get(url)
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, {"detail": "Forbidden"}), (url, a.text)
