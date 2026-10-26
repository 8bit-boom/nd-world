def test_hub_keepsakes_places_handouts_are_pythons(client, seed):
    """GET /api/characters/<id>/hub/keepsakes, /places and /handouts from the gateway: what the character kept (newest first, the
    per-kind limits of the world), every page the world opens to a player with the character's pins, and the GM's shown pictures
    with their new flags - the owner and a GM looking in; a stranger and another world's character are the same 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import (CharacterKeepsake, CharacterPref, GameSession, PlayerCharacter, PrivateNote,
                            World, WorldHandout)
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Keeper", level=2, xp=5)
        stranger = PlayerCharacter(world_id=w, owner_user_id=None, name="Nobody's")
        over_there = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.player_b.id, name="Elsewhere")
        db.add_all([pc, stranger, over_there])
        db.flush()
        db.add_all([
            WorldHandout(world_id=w, url="/uploads/h2.png", label="Map"),
            WorldHandout(world_id=w, url="/uploads/h1.png", label=""),
            WorldHandout(world_id=seed.world_b.id, url="/uploads/hb.png", label="B"),
        ])
        db.add_all([
            CharacterKeepsake(world_id=w, character_id=pc.id, kind="image", url="/uploads/k1.png", name="Photo",
                              description="From the docks", saved_by="player", own=True),
            CharacterKeepsake(world_id=w, character_id=pc.id, kind="text", text="A poem", name="Poem", saved_by="gm"),
        ])
        db.add_all([
            PrivateNote(world_id=w, player_user_id=seed.player_a.id, author_id=seed.gm.id, title="Secret",
                        content="Read this. [gmonly]And the hidden part.[/gmonly]"),
            PrivateNote(world_id=w, player_user_id=seed.player_a.id, author_id=seed.player_a.id, title="Own",
                        content="The player's own note"),
        ])
        db.add_all([
            GameSession(world_id=w, title="Docks", session_num=2,
                        recap_media_json=json.dumps([{"kind": "image", "url": "/uploads/s1.png", "caption": "The pier",
                                                      "visible": True, "own": False, "uid": "img1"},
                                                     {"kind": "image", "url": "/uploads/s2.png", "caption": "Hidden",
                                                      "visible": False, "own": False, "uid": "img2"}])),
            GameSession(world_id=w, title="", session_num=1),
        ])
        db.add(CharacterPref(world_id=w, character_id=pc.id, prefs_json=json.dumps({"pins": ["tables", "gone"], "handouts_seen": 1})))
        db.get(World, w).personal_images_max = None
        db.commit()
        ids = (pc.id, stranger.id, over_there.id)
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    urls = [f"/api/characters/{i}/{t}" for i in ids + (999999,) for t in ("keepsakes", "places", "handouts")]
    for c in (d, g, pb):
        for url in urls:
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (url, a.status_code, a.text[:150], b.text[:150])
    served = sum(1 for c in (d, g) for url in urls if _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway")
    assert served > 12, served
    # handouts: newest first, the seen marker hides the older one from the owner, a GM looking in cannot edit
    h = d.get(f"/api/characters/{ids[0]}/handouts").json()
    assert [x["label"] for x in h["handouts"]] == ["Map", ""], h
    assert [x["new"] for x in h["handouts"]] == [True, False], h
    assert h["can_edit"] is True and g.get(f"/api/characters/{ids[0]}/handouts").json()["can_edit"] is False
    # keepsakes: newest first, own counts to the image limit (the default 30), the saveable lists are the player's view
    k = d.get(f"/api/characters/{ids[0]}/keepsakes").json()
    assert [x["name"] for x in k["keepsakes"]] == ["Poem", "Photo"], k
    assert k["own_limits"]["image"] == {"used": 1, "max": 30}, k["own_limits"]
    assert [n["name"] for n in k["saveable"]["notes"]] == ["Secret"], k["saveable"]
    assert "[gmonly]" not in k["saveable"]["notes"][0]["text"], k["saveable"]["notes"]
    assert [s["name"] for s in k["saveable"]["session"]] == ["The pier"], k["saveable"]["session"]
    assert [x["name"] for x in k["saveable"]["handouts"]] == ["Handout", "Handout"], k["saveable"]["handouts"]
    assert k["is_owner"] is True and k["is_gm"] is False and k["can_edit"] is True
    # places: the world's player pages grouped, the pins kept only where still open to players
    p = d.get(f"/api/characters/{ids[0]}/places").json()
    labels = {grp["label"]: [i["id"] for i in grp["items"]] for grp in p["groups"]}
    assert "characters" not in labels.get("Tools & logs", []) and "kind_npc" not in labels.get("Tools & logs", []), p
    assert p["pins"] == ["tables"], p
    assert all(row["href"].startswith("/") for grp in p["groups"] for row in grp["items"])
    # a GM looking in sees the same places (the player's list)
    assert g.get(f"/api/characters/{ids[0]}/places").json() == p
    # a character with no owner: only the GM sees it (the player gets the 404)
    for t in ("keepsakes", "places", "handouts"):
        assert d.get(f"/api/characters/{ids[1]}/{t}").status_code == 404
        assert g.get(f"/api/characters/{ids[1]}/{t}").status_code == 200
        assert pb.get(f"/api/characters/{ids[2]}/{t}").status_code == 200
        assert d.get(f"/api/characters/{ids[2]}/{t}").status_code == 404
