def test_cockpit_player_board_is_pythons(client, seed):
    """GET /api/cockpit/player-board from the gateway: the viewer's own parties (member vitals, only parties one of their characters
    belongs to), the world's player-visible active top-level quests with party names, the viewer's characters with vitals and XP - for a
    player and a GM, and a GM's ?pc= view through a character's owner; no world (404) and a player's ?pc= at another's character (own
    list still) answered as Python's."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Party, PlayerCharacter, Quest
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        p1 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Kira", level=3, xp=120,
                             pp_current=4, mp_current=2, shock_current=1, current_hp=7)
        p2 = PlayerCharacter(world_id=w, owner_user_id=seed.player_a.id, name="Zed", level=1, xp=0,
                             pp_current=9, mp_current=9, shock_current=9, current_hp=1)
        g1 = PlayerCharacter(world_id=seed.world_b.id, owner_user_id=seed.gm.id, name="Gm")
        db.add_all([p1, p2, g1])
        db.flush()
        db.add_all([
            Party(world_id=w, name="Crew", member_pc_ids_json=json.dumps([p1.id, p2.id])),
            Party(world_id=w, name="Lone", member_pc_ids_json=json.dumps([p2.id])),
            Party(world_id=w, name="Others", member_pc_ids_json=json.dumps([])),
            Party(world_id=seed.world_b.id, name="Elsewhere", member_pc_ids_json=json.dumps([g1.id])),
        ])
        db.flush()
        db.add_all([
            Quest(world_id=w, title="Find the signal", status="active", category="main", summary=" Static. ",
                  visible_to_players=True, assigned_party_id=None),
            Quest(world_id=w, title="Hidden thread", status="active", category="side", visible_to_players=False),
            Quest(world_id=w, title="Done deal", status="done", visible_to_players=True),
            Quest(world_id=w, title="Child", status="active", visible_to_players=True, parent_id=1),
            Quest(world_id=w, title="A-side job", status="active", category="side", visible_to_players=True),
            Quest(world_id=w, title="Z-job", status="active", category="side", visible_to_players=True),
            Quest(world_id=w, title="No party", status="active", visible_to_players=True, assigned_party_id=999999),
        ])
        db.commit()
        ids = (p1.id, p2.id)
    finally:
        db.close()
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    g = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    for who, c in (("player", d), ("gm", g)):
        for url in ("/api/cockpit/player-board", f"/api/cockpit/player-board?pc={ids[0]}", "/api/cockpit/player-board?pc=",
                    "/api/cockpit/player-board?pc=x", f"/api/cockpit/player-board?pc=00{ids[0]}"):
            a, b = c.get(url), _via(c, base).get(url)
            assert (a.status_code, a.json()) == (b.status_code, b.json()), (who, url)
        for url in ("/api/cockpit/player-board", f"/api/cockpit/player-board?pc={ids[0]}"):
            assert _via(c, base).get(url).headers.get("x-nd-served-by") == "gateway", (who, url)
    # the player's board really carries their two characters, only their parties, and never a hidden thread
    board = d.get("/api/cockpit/player-board").json()
    assert [m["name"] for m in board["my_pcs"]] == ["Kira", "Zed"], board
    assert board["my_pcs"][0]["xp"] == 120 and board["my_pcs"][0]["level"] == 3, board
    assert [p["name"] for p in board["parties"]] == ["Crew", "Lone"], board
    assert board["parties"][0]["members"][0]["name"] == "Kira", board
    assert [q["title"] for q in board["quests"]] == ["Find the signal", "A-side job", "Z-job", "No party"], board
    assert board["quests"][3]["party"] is None, board
    # a player's ?pc= at someone else's character is ignored: their own board
    assert d.get(f"/api/cockpit/player-board?pc={ids[0] + 50}").json()["my_pcs"][0]["name"] == "Kira"
    # a missing world is the gateway's 404, as Python's is
    d2 = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    a, b = d2.get("/api/cockpit/player-board?w=999999"), _via(d2, base).get("/api/cockpit/player-board?w=999999")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (404, {"detail": "Not Found"}), (a.status_code, a.text)


def test_view_prefs_site_and_unknown_are_pythons(client, seed):
    """POST / DELETE /api/view-prefs/site from the gateway: the row written as Python's json.dumps writes it, the session re-signed with
    {options, pins, at, uid} (a page after it is gateway-served from the fresh layout), the reset forgetting it; an unknown section is
    Python's 404 "Unknown section" on both verbs."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import UserViewPref
    base = str(client.base_url)
    d = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    body = {"options": {"nav": "rail", "density": "compact", "nonsense": "dropped"}, "pins": [1, 2, 99]}
    for i in range(2):  # twice: the save then the overwrite of the same row
        a, b = d.post("/api/view-prefs/site", json=body), v.post("/api/view-prefs/site", json=body)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), (i, a.text, b.text)
        assert a.content == b.content, (i, a.content, b.content)
        assert b.headers.get("x-nd-served-by") == "gateway", i
        ra = d.get("/api/view-prefs").json()
        rb = v.get("/api/view-prefs").json()
        assert ra == rb, (i, ra, rb)
        assert rb["site"]["options"]["nav"] == "rail" and "nonsense" not in rb["site"]["options"], rb
    # the gateway's own re-signed cookie carries the fresh layout: the next page is the gateway's
    assert v.get("/dice").headers.get("x-nd-served-by") == "gateway"
    # a page the direct client fetches refreshes ITS cookie from the row (Python's remember-on-read), so both agree after
    d.get("/dice")
    assert d.get("/api/view-prefs").json() == v.get("/api/view-prefs").json()
    # the row as Python's json.dumps wrote it
    db = SessionLocal()
    try:
        rows = db.query(UserViewPref).filter(UserViewPref.user_id == seed.player_a.id, UserViewPref.section == "site").all()
        assert len(rows) == 1, rows
        text = rows[0].prefs_json
    finally:
        db.close()
    assert json.loads(text) == json.loads(json.dumps({"options": {"nav": "rail", "density": "compact"}, "pins": [1, 2, 99]}))
    # the reset: same 200 on both, the row gone, and the session's copy gone on the gateway side
    a, b = d.delete("/api/view-prefs/site"), v.delete("/api/view-prefs/site")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, {"reset": "site"}), (a.text, b.text)
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert v.get("/api/view-prefs").json() == d.get("/api/view-prefs").json() == {}
    db = SessionLocal()
    try:
        assert db.query(UserViewPref).filter(UserViewPref.user_id == seed.player_a.id, UserViewPref.section == "site").count() == 0
    finally:
        db.close()
    # unknown sections: Python's 404 on both verbs, gateway-answered
    for method in ("post", "delete"):
        ra, rb = getattr(d, method)("/api/view-prefs/nosuch", json={"options": {}} if method == "post" else None), \
            getattr(v, method)("/api/view-prefs/nosuch", json={"options": {}} if method == "post" else None)
        assert (ra.status_code, ra.json()) == (rb.status_code, rb.json()) == (404, {"detail": "Unknown section"}), (method, ra.text, rb.text)
        assert rb.headers.get("x-nd-served-by") == "gateway", method
    # a name the path filter would not read as a section is Python's (the same 404)
    b2 = v.post("/api/view-prefs/No-Such", json={})
    assert b2.status_code == 404 and b2.json() == {"detail": "Unknown section"}
    assert b2.headers.get("x-nd-served-by") != "gateway"
