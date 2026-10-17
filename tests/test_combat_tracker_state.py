"""The combat tracker's server side: what is stored (combat_state), what a player may see of it, revisions that catch lost updates,
world scoping, and the rule-system plumbing (app/combat_rules.py) including fluid initiative through the player hub."""
import json

from app import combat_rules, combat_state
from app.database import SessionLocal
from app.models import CombatSession, Entity, PlayerCharacter, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _cb(i, name, source="manual", init=0, **kw):
    return dict({"id": i, "name": name, "source": source, "pc_id": None, "entity_id": None, "initiative": init, "max_hp": 10, "hp": 7,
                 "max_shock": 0, "shock": 0, "armor": 2, "conditions": ["Poisoned secretly"], "notes": "Is the cult leader"}, **kw)


def _make(world_id, combatants=None, **kw):
    db = SessionLocal()
    try:
        cs = CombatSession(world_id=world_id, name=kw.pop("name", "Ambush"), combatants_json=json.dumps(combatants or []), **kw)
        db.add(cs); db.commit(); db.refresh(cs)
        return cs.id
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _player(client, seed, section="read"):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.section_access_json = json.dumps({"combat": {"player": section, "assistant": "read"}})
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── what is stored ──────────────────────────────────────────────────────────

def test_normalize_state_cleans_and_bounds_what_the_browser_sent():
    s = combat_state.normalize_state({
        "combatants": [{"id": "a", "name": "  Ana   Quill ", "hp": 99, "max_hp": 5, "initiative": "12", "conditions": ["Burn", "burn", "Daze"],
                        "res": {"ichor": {"cur": 9, "max": 5}}, "saves": {"ok": 7}}, {"id": "a", "name": "Dup"}],
        "round_num": "abc", "active_idx": 99, "turn": {"active_id": "a", "acted": ["a", "zz", "a"]}, "ruleset": "hunt", "log": [{"t": 1, "text": "x"}, "junk"]})
    a, dup = s["combatants"]
    assert a["name"] == "Ana Quill" and a["hp"] == 5 and a["initiative"] == 12 and a["conditions"] == ["Burn", "burn", "Daze"]     # a stack, hp capped at max
    assert a["res"]["ichor"]["cur"] == 5 and a["saves"]["ok"] == 3
    assert dup["id"] != "a"                                                                # ids stay unique
    assert s["round_num"] == 1 and s["active_idx"] == 1 and s["turn"] == {"active_id": "a", "acted": ["a"], "picker": None, "mode": ""}
    assert s["ruleset"] == "hunt" and s["log"] == [{"t": 1, "round": 1, "text": "x"}]
    for bad in (None, [], {"combatants": "x"}, {"combatants": [1]}, {"combatants": [{}] * 61}):
        try:
            combat_state.normalize_state(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_save_validates_and_never_500s(client, seed):
    _gm(client, seed)
    cid = _make(seed.world_a.id)
    assert client.post(f"/combat/{cid}/state", json={"combatants": [], "round_num": "abc"}).status_code == 200
    assert client.post(f"/combat/{cid}/state", json={"combatants": "nope"}).status_code == 422
    assert client.post(f"/combat/{cid}/state", content="not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post(f"/combat/{cid}/state", json=[1, 2]).status_code == 422


def test_revisions_catch_a_lost_update_and_hand_back_the_current_state(client, seed):
    _gm(client, seed)
    cid = _make(seed.world_a.id, [_cb("c1", "Ana", init=3)])
    r = client.post(f"/combat/{cid}/state", json={"combatants": [_cb("c1", "Ana", init=3)], "rev": 0})
    assert r.status_code == 200 and r.json()["rev"] == 1
    # a second writer that still holds rev 0 is told so, and nothing is overwritten
    stale = client.post(f"/combat/{cid}/state", json={"combatants": [_cb("c1", "Overwrite", init=3)], "rev": 0})
    assert stale.status_code == 409 and stale.json()["state"]["rev"] == 1 and stale.json()["state"]["combatants"][0]["name"] == "Ana"
    assert client.get(f"/api/combat/{cid}/state").json()["combatants"][0]["name"] == "Ana"
    # no rev in the body: the old behaviour (the cockpit and scripts keep working)
    assert client.post(f"/combat/{cid}/state", json={"combatants": [_cb("c1", "Bo", init=3)]}).status_code == 200


def test_every_writer_bumps_the_revision(client, seed):
    from app import combat_turns
    cid = _make(seed.world_a.id, [_cb("a", "A", init=1), _cb("b", "B", init=2)])
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        combatants = combat_turns.load(cs)
        combat_turns.set_initiative(cs, combatants, 0, 9); assert cs.rev == 1
        combat_turns.advance(cs, combatants); assert cs.rev == 2
    finally:
        db.close()


def test_changing_routes_are_scoped_to_the_active_world(client, seed):
    _gm(client, seed)
    other = _make(seed.world_b.id, [_cb("x", "Elsewhere")], name="Other world")
    for path in (f"/combat/{other}/state", f"/combat/{other}/delete", f"/combat/{other}/unlink-session", f"/api/combat/{other}/sync-characters"):
        assert client.post(path, json={"combatants": []}).status_code == 404, path
    db = SessionLocal()
    try:
        assert db.get(CombatSession, other) is not None
    finally:
        db.close()


# ── what a player may see ───────────────────────────────────────────────────

def test_a_player_sees_enemies_as_letters_and_words_never_names_numbers_or_notes(client, seed):
    cid = _make(seed.world_a.id, [
        _cb("e1", "Hidden Traitor Edmund", "entity", init=12, entity_id=1),
        _cb("e2", "Goblin Archer", "entity", init=9, name_known=True, hp=2),
        _cb("e3", "Secret Ambusher", "entity", init=8, visible=False),
        _cb("p1", "Ana", "pc", init=5, pc_id=1, hp=4, max_hp=9, conditions=["Burn"]),
    ])
    _player(client, seed)
    page = client.get(f"/combat/{cid}")
    assert page.status_code == 200
    state = client.get(f"/api/combat/{cid}/state").json()
    blob = page.text + json.dumps(state)
    for secret in ("Hidden Traitor Edmund", "Secret Ambusher", "Is the cult leader", "Poisoned secretly", "armor"):
        assert secret not in blob, secret
    rows = {r["id"]: r for r in state["rows"]}
    assert "e3" not in rows                                                    # hidden by the GM: not even mentioned
    assert rows["e1"]["name"] == "Enemy A" and rows["e1"]["state"] == "Hurt" and "hp" not in rows["e1"]
    assert rows["e2"]["name"] == "Goblin Archer" and rows["e2"]["state"] == "Bloodied"      # the GM allowed its name
    assert rows["p1"]["hp"] == 4 and rows["p1"]["max_hp"] == 9 and rows["p1"]["conditions"] == ["Burn"]
    assert [r["id"] for r in state["rows"]] == ["e1", "e2", "p1"]               # in initiative order
    assert "Delete Encounter" not in page.text and "combat-core.js" not in page.text   # the GM page is not what they get


def test_a_player_cannot_change_an_encounter(client, seed):
    cid = _make(seed.world_a.id, [_cb("a", "A")])
    _player(client, seed)
    assert client.post(f"/combat/{cid}/state", json={"combatants": [], "round_num": 9}).status_code == 403
    assert client.post(f"/combat/{cid}/delete").status_code == 403


def test_hp_words():
    assert [combat_state.hp_word(dict(max_hp=10, hp=h)) for h in (10, 9, 5, 4, 0)] == ["Unhurt", "Hurt", "Bloodied", "Bloodied", "Down"]
    assert combat_state.hp_word({"max_hp": 10, "hp": 10, "status": "dead"}) == "Down"


# ── rule systems ────────────────────────────────────────────────────────────

def test_every_ruleset_has_what_the_tracker_needs():
    assert set(combat_rules.RULESET_IDS) >= {"generic", "nd", "worm", "hunt", "asterion", "nd_asterion", "worm_asterion"}
    for rid, r in combat_rules.RULESETS.items():
        assert r["turns"] in ("initiative", "fluid") and r["label"] and r["hp_label"] and r["conditions"], rid
        assert {"state"} <= set(r["down"]["hp"]), rid
        for cond in r["conditions"]:
            assert cond["name"] and "stack" in cond
    assert [combat_rules.is_fluid(x) for x in ("nd", "worm", "generic", "hunt", "asterion", "nd_asterion", "worm_asterion", "", None)] == \
        [False, False, False, True, True, True, True, False, False]
    # the rule books' own numbers
    assert combat_rules.get("nd")["eco"][0]["max"] == 2 and combat_rules.get("hunt")["eco"][2]["id"] == "T"
    assert combat_rules.get("asterion")["absorb"][0] == "sparkShield" and combat_rules.get("asterion")["down"]["hp"]["saves"] is False
    assert combat_rules.get("hunt")["armor_cap"] == 3
    burn = combat_rules.condition("nd", "burn")
    assert burn["tick"] == {"when": "round_end", "dice": "1d8", "hp": True} and burn["stack"]
    assert combat_rules.condition("asterion", "Bleeding")["tick"]["when"] == "turn_start"
    assert combat_rules.condition("nd_asterion", "Yellow")["tick"]["shock"] == 1


def test_a_new_encounter_takes_the_ruleset_chosen_or_the_partys(client, seed):
    _gm(client, seed)
    r = client.post("/combat/new", data={"name": "Pit fight", "ruleset": "asterion"}, follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        assert db.get(CombatSession, cid).ruleset == "asterion"
    finally:
        db.close()
    r = client.post("/combat/new", data={"name": "Default"}, follow_redirects=False)
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        assert db.get(CombatSession, cid).ruleset == "nd"          # nothing says otherwise: Neon & Dragons
    finally:
        db.close()
    assert "Game of Gods (Asterion)" in client.get("/combat").text


def test_an_old_encounter_settles_on_a_ruleset_when_the_gm_opens_it(client, seed):
    _gm(client, seed)
    cid = _make(seed.world_a.id, [_cb("a", "A")])
    assert client.get(f"/combat/{cid}").status_code == 200
    db = SessionLocal()
    try:
        assert db.get(CombatSession, cid).ruleset == "nd"
    finally:
        db.close()


def test_the_gm_page_carries_the_rule_tables_and_the_state(client, seed):
    _gm(client, seed)
    cid = _make(seed.world_a.id, [_cb("a", "A")], ruleset="hunt")
    html = client.get(f"/combat/{cid}").text
    assert "combat-core.js" in html and "const RULES = JSON.parse(" in html and "Hunt in the Moonlight" in html
    state = client.get(f"/api/combat/{cid}/state").json()
    assert state["ruleset"] == "hunt" and state["rev"] == 0 and state["turn"] == {"active_id": None, "acted": [], "picker": None, "mode": ""} and state["active_id"] is None


def test_fluid_turns_through_the_player_hub(client, seed):
    """Hunt / Asterion: the actor ends the turn and CHOOSES who goes next - an ally who has not acted, or the enemy side (the GM names it)."""
    _gm(client, seed)
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        pcs = []
        for n in ("Kaidros", "Selene"):
            pc = PlayerCharacter(world_id=w.id, name=n, owner_user_id=seed.player_a.id, current_hp=5, max_hp=5)
            db.add(pc); db.flush(); pcs.append(pc.id)
        cs = CombatSession(world_id=w.id, name="Pit", ruleset="asterion", combatants_json=json.dumps([
            _cb("k", "Kaidros", "pc", pc_id=pcs[0], notes=""), _cb("s", "Selene", "pc", pc_id=pcs[1], notes=""), _cb("g", "Guard", "entity", notes="")]),
            turn_json=json.dumps({"active_id": "k", "acted": [], "picker": None, "mode": ""}))
        db.add(cs); db.commit(); cid = cs.id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    k, s = pcs
    assert client.post(f"/api/characters/{k}/hub/initiative").status_code == 409              # no initiative roll in this system
    end = client.post(f"/api/characters/{k}/hub/end-turn")
    assert end.status_code == 200 and end.json()["combat"]["turn"] == "choose"
    assert [c["name"] for c in end.json()["combat"]["choose"]] == ["Selene"]                  # an ally who has not acted; never an enemy
    assert client.post(f"/api/characters/{s}/hub/pass-turn", json={"next": "k"}).status_code == 409   # not Selene's choice
    assert client.post(f"/api/characters/{k}/hub/pass-turn", json={"next": "g"}).status_code == 400   # enemies are picked by the GM
    assert client.post(f"/api/characters/{k}/hub/pass-turn", json={"next": "k"}).status_code == 409   # already acted
    ok = client.post(f"/api/characters/{k}/hub/pass-turn", json={"next": "s"})
    assert ok.status_code == 200
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        assert json.loads(cs.turn_json) == {"active_id": "s", "acted": ["k"], "picker": None, "mode": ""}
    finally:
        db.close()
    # Selene passes to "an enemy": the GM then names which one
    client.post(f"/api/characters/{s}/hub/end-turn")
    assert client.post(f"/api/characters/{s}/hub/pass-turn", json={"next": "gm"}).status_code == 200
    db = SessionLocal()
    try:
        assert json.loads(db.get(CombatSession, cid).turn_json) == {"active_id": None, "acted": ["k", "s"], "picker": None, "mode": ""}
    finally:
        db.close()


def test_sync_characters_writes_hp_conditions_and_pp_mp_back_to_a_native_sheet(client, seed):
    _gm(client, seed)
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Sable", max_hp=12, current_hp=12, pp_current=5, mp_current=5,
                             stats_json=json.dumps([{"id": s, "value": 1} for s in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]))
        db.add(pc); db.commit(); pid = pc.id
        cs = CombatSession(world_id=seed.world_a.id, name="Sync", ruleset="nd", combatants_json=json.dumps([
            _cb("s", "Sable", "pc", pc_id=pid, hp=4, max_hp=12, conditions=["Burn", "Burn", "Daze"], res={"pp": {"cur": 1, "max": 4, "label": "PP"}, "mp": {"cur": 3, "max": 4, "label": "MP"}})]))
        db.add(cs); db.commit(); cid = cs.id
    finally:
        db.close()
    assert client.post(f"/api/combat/{cid}/sync-characters").json()["synced"] == ["Sable"]
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pid)
        assert pc.current_hp == 4 and (pc.pp_current, pc.mp_current) == (1, 3)
        assert json.loads(pc.conditions_json) == ["Burn", "Daze"]            # the sheet lists each condition once
    finally:
        db.close()


def test_entity_health_is_read_from_whichever_field_the_creature_uses(client, seed):
    from app.routers.combat import entity_to_combatant
    for field, expect in (("health", 7), ("hp", 9), ("flesh", 4), (None, 5)):
        e = Entity(world_id=seed.world_a.id, kind="creature", name="C", custom_fields_json=json.dumps({field: str(expect)} if field else {}))
        assert entity_to_combatant(e)["max_hp"] == expect


def test_a_table_can_override_the_turn_order_of_its_ruleset(client, seed):
    """The Asterion-engine books allow a fixed initiative order; N&D could run fluid. turn_json["mode"] wins over the ruleset."""
    from app import combat_turns
    _gm(client, seed)
    cid = _make(seed.world_a.id, [_cb("a", "A", init=3), _cb("b", "B", init=9)], ruleset="hunt")
    r = client.post(f"/combat/{cid}/state", json={"combatants": [_cb("a", "A", init=3), _cb("b", "B", init=9)], "ruleset": "hunt",
                                                  "turn": {"mode": "initiative"}, "active_idx": 0})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        assert not combat_turns.is_fluid(cs) and combat_turns.active_combatant_id(cs, combat_turns.load(cs)) == "b"
    finally:
        db.close()
    assert client.get(f"/api/combat/{cid}/state").json()["turn"]["mode"] == "initiative"


def test_speed_becomes_the_initiative_bonus_for_pcs_and_entities(client, seed):
    from app.routers.combat import entity_to_combatant, pc_to_combatant
    e = Entity(world_id=seed.world_a.id, kind="creature", name="Quick", custom_fields_json=json.dumps({"speed": "4"}))
    assert entity_to_combatant(e)["init_bonus"] == 4
    pc = PlayerCharacter(world_id=seed.world_a.id, name="Sable", stats_json=json.dumps([{"id": "dex", "value": 3}, {"id": "itu", "value": 2}]))
    assert pc_to_combatant(pc)["init_bonus"] == 5


def test_a_fallen_ally_is_not_offered_when_the_player_chooses_who_goes_next(client, seed):
    from app import combat_turns
    cs = type("CS", (), {"ruleset": "asterion", "turn_json": "{}"})()
    assert combat_turns.is_out(cs, {"source": "pc", "hp": 0, "max_hp": 5})            # Shattered cannot act
    nd = type("CS", (), {"ruleset": "nd", "turn_json": "{}"})()
    assert not combat_turns.is_out(nd, {"source": "pc", "hp": 0, "max_hp": 5})        # a dying N&D character still takes its rolls
    assert combat_turns.is_out(nd, {"source": "entity", "hp": 0, "max_hp": 5})
