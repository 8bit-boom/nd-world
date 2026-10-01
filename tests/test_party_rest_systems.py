"""Party Rest applies each member's OWN system rules.

It used to be N&D-only (+half PP/MP, all Shock) and silently skipped everyone on a
custom system. Now: Asterion has a Short Rest (Spark Shield full, +2 Ichor) and a Long
Rest (all Flesh and Ichor), Hunt in the Moonlight restores all Stamina and clears
Strain (never Health), N&D also resets the stim counter - and Undo puts every one of
them back.
"""
import json

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

STATS = [{"id": k, "value": v} for k, v in
         (("str", 3), ("dex", 3), ("bod", 3), ("per", 3), ("wil", 2), ("int", 2), ("cha", 2), ("itu", 2))]


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug, SheetTemplate.is_builtin == True).first().id  # noqa: E712
    finally:
        db.close()


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _cf(pc):
    db = SessionLocal()
    try:
        return json.loads(db.get(PlayerCharacter, pc).custom_fields_json or "{}")
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _party(seed, *pcs):
    return _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps(list(pcs))))


def _asterion(seed, **cf):
    return _add(PlayerCharacter(world_id=seed.world_a.id, name="Zeus", sheet_template_id=_tpl("asterion"),
                                custom_fields_json=json.dumps(cf)))


HURT = {"sparkShield_current": 0, "sparkShield_max": 3, "flesh_current": 2, "flesh_max": 5,
        "ichor_current": 1, "ichor_max": 5}


def test_asterion_short_rest(client, seed):
    god = _asterion(seed, **HURT)
    party = _party(seed, god)
    _gm(client, seed)
    r = client.post(f"/api/parties/{party}/rest", json={"kind": "short"})
    assert r.status_code == 200
    cf = _cf(god)
    assert (cf["sparkShield_current"], cf["flesh_current"], cf["ichor_current"]) == (3, 2, 3), \
        "Spark Shield full, +2 Ichor, Flesh untouched"
    assert [a["id"] for a in r.json()["applied"]] == [god]


def test_asterion_long_rest_and_the_ichor_cap(client, seed):
    god = _asterion(seed, **{**HURT, "ichor_current": 4})
    party = _party(seed, god)
    _gm(client, seed)
    client.post(f"/api/parties/{party}/rest", json={"kind": "short"})
    assert _cf(god)["ichor_current"] == 5, "+2 Ichor never exceeds the max"
    _gm(client, seed)
    client.post(f"/api/parties/{party}/rest", json={"kind": "long"})
    cf = _cf(god)
    assert (cf["flesh_current"], cf["ichor_current"], cf["sparkShield_current"]) == (5, 5, 3)


def test_default_kind_is_a_full_rest(client, seed):
    god = _asterion(seed, **HURT)
    party = _party(seed, god)
    _gm(client, seed)
    assert client.post(f"/api/parties/{party}/rest").status_code == 200
    assert _cf(god)["flesh_current"] == 5, "no body = the existing behaviour: a full Rest"


def test_hitm_rest_restores_stamina_clears_strain_never_health(client, seed):
    hunter = _add(PlayerCharacter(world_id=seed.world_a.id, name="Vex", sheet_template_id=_tpl("hunt-in-the-moonlight"),
                                  custom_fields_json=json.dumps({"health_current": 2, "health_max": 5, "stamina_current": 1,
                                                                 "stamina_max": 5, "strain": "2", "staminaSpent": 7,
                                                                 "hunger_current": 6, "hunger_max": 10})))
    party = _party(seed, hunter)
    _gm(client, seed)
    assert client.post(f"/api/parties/{party}/rest", json={"kind": "short"}).status_code == 200
    cf = _cf(hunter)
    assert cf["stamina_current"] == 5 and cf["strain"] == "0" and cf["staminaSpent"] == 0
    assert cf["health_current"] == 2, "Health comes back from treatment, not from resting"
    assert cf["hunger_current"] == 6, "Hunger only falls through play"


def test_nd_member_resets_stims_and_still_gets_pp_mp_shock(client, seed):
    nd = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps(STATS),
                              sheet_template_id=_tpl("nd-default"), pp_current=0, mp_current=0, shock_current=0,
                              custom_fields_json=json.dumps({"stims_current": 3, "stims_max": 3})))
    party = _party(seed, nd)
    _gm(client, seed)
    r = client.post(f"/api/parties/{party}/rest")
    assert r.status_code == 200
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, nd)
        assert (pc.pp_current, pc.mp_current, pc.shock_current) == (6, 4, 8)
    finally:
        db.close()
    assert _cf(nd)["stims_current"] == 0


def test_mixed_party_and_a_system_without_rules(client, seed):
    god = _asterion(seed, **HURT)
    nd = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps(STATS)))
    homebrew = _add(SheetTemplate(world_id=seed.world_a.id, name="Homebrew", slug="homebrew", sheet_mode="custom",
                                  fields_json=json.dumps([{"id": "grit", "label": "Grit", "type": "resource", "default_value": "3/3"}])))
    brew = _add(PlayerCharacter(world_id=seed.world_a.id, name="Brewer", sheet_template_id=homebrew,
                                custom_fields_json=json.dumps({"grit_current": 1})))
    party = _party(seed, god, nd, brew)
    _gm(client, seed)
    r = client.post(f"/api/parties/{party}/rest", json={"kind": "long"})
    applied = {a["id"] for a in r.json()["applied"]}
    assert god in applied and nd in applied and brew not in applied, "a custom system with no Rest rules is left alone"
    assert _cf(brew) == {"grit_current": 1}


def test_undo_restores_every_system(client, seed):
    god = _asterion(seed, **HURT)
    nd = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps(STATS), pp_current=1,
                              sheet_template_id=_tpl("nd-default"), custom_fields_json=json.dumps({"stims_current": 2, "stims_max": 3})))
    party = _party(seed, god, nd)
    _gm(client, seed)
    before_god, before_nd = _cf(god), _cf(nd)
    snap = client.post(f"/api/parties/{party}/rest", json={"kind": "long"}).json()["snapshot"]
    assert _cf(god) != before_god
    r = client.post(f"/api/parties/{party}/rest/undo", json={"snapshot": snap})
    assert r.status_code == 200 and r.json()["restored"] == 2
    assert _cf(god) == before_god and _cf(nd) == before_nd
    db = SessionLocal()
    try:
        assert db.get(PlayerCharacter, nd).pp_current == 1
    finally:
        db.close()


def test_undo_refuses_junk_snapshots(client, seed):
    god = _asterion(seed, **HURT)
    other = _asterion(seed, **HURT)
    party = _party(seed, god)
    _gm(client, seed)
    for bad in ("not json", "[1,2]", "null", '"text"'):
        r = client.post(f"/api/parties/{party}/rest/undo", json={"snapshot": [{"id": god, "custom_fields_json": bad}]})
        assert r.status_code == 200
    assert _cf(god) == HURT, "a malformed snapshot entry must not corrupt the sheet"
    # a well-formed one may only put back what a Rest can change - not rewrite the rest of the sheet
    sneaky = json.dumps({"flesh_current": 1, "appearance": "pwned", "x": 1, "ichor_current": {"nested": 1}})
    client.post(f"/api/parties/{party}/rest/undo", json={"snapshot": [{"id": god, "custom_fields_json": sneaky}]})
    cf = _cf(god)
    assert "appearance" not in cf and "x" not in cf, "undo must not write fields a Rest never touches"
    assert cf["flesh_current"] == 1 and cf["ichor_current"] == HURT["ichor_current"], "numbers restore; non-scalar values are ignored"
    _restore = {k: v for k, v in HURT.items()}
    client.post(f"/api/parties/{party}/rest/undo", json={"snapshot": [{"id": other, "custom_fields_json": "{}"}]})
    assert _cf(other) == HURT, "only members of THIS party are ever touched"


def test_bad_kind_is_a_400(client, seed):
    party = _party(seed, _asterion(seed, **HURT))
    _gm(client, seed)
    assert client.post(f"/api/parties/{party}/rest", json={"kind": "nap"}).status_code == 400
    assert client.post(f"/api/parties/{party}/rest", content=b"junk").status_code in (200, 400)


def test_party_page_offers_a_short_rest_only_when_a_member_has_one(client, seed):
    nd_only = _party(seed, _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps(STATS))))
    both = _add(Party(world_id=seed.world_a.id, name="Pantheon", member_pc_ids_json=json.dumps([_asterion(seed, **HURT)])))
    _gm(client, seed)
    assert 'id="short-rest-btn"' not in client.get(f"/parties/{nd_only}").text
    html = client.get(f"/parties/{both}").text
    assert 'id="short-rest-btn"' in html and "partyRest('short')" in html
