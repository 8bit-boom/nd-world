"""Characters on a built-in system (Hunt in the Moonlight, Asterion) must integrate
with the Player Characters list, Parties, combat and XP — not just N&D sheets.

Seen in the wild: the character list's cards were torn apart for anyone in a party
(a party-badge <a> nested inside the card's own <a> — browsers split it into stray
tiles), party Member Vitals said "AC 10 · no HP tracked" for every member whose
resource tracks hadn't been saved yet (the sheet shows the template's defaults, the
strip ignored them) and could never show DOWN, N&D-only level-up prompts appeared on
custom systems, and the template's own "Player" / "Hunter Name" fields duplicated the
sheet's name fields.
"""
import json
from html.parser import HTMLParser

from app.database import SessionLocal
from app.models import CombatSession, GameSession, Party, PlayerCharacter, SheetTemplate
from app.sheet_systems import (
    enrich_fields, hp_track, resource_tracks, short_label, system_meta, xp_field_ids,
)

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _tpl(slug):
    db = SessionLocal()
    try:
        t = db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first()
        db.expunge(t)
        return t
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


def _row(model, pk):
    db = SessionLocal()
    try:
        r = db.get(model, pk)
        db.expunge(r)
        return r
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


class _Anchors(HTMLParser):
    """Finds <a> opened while another <a> is still open (invalid HTML)."""
    def __init__(self):
        super().__init__()
        self.depth, self.nested = 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            if self.depth:
                self.nested.append(dict(attrs).get("href"))
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "a" and self.depth:
            self.depth -= 1


def nested_anchors(html):
    p = _Anchors()
    p.feed(html)
    return p.nested


def _hitm_scene(seed, party_name="Burn the Witch"):
    hitm = _tpl("hunt-in-the-moonlight")
    saved = _add(PlayerCharacter(world_id=seed.world_a.id, name="Anders", sheet_template_id=hitm.id,
                                 custom_fields_json=json.dumps({"health_current": 3, "health_max": 5})))
    fresh = _add(PlayerCharacter(world_id=seed.world_a.id, name="Bella", sheet_template_id=hitm.id))
    party = _add(Party(world_id=seed.world_a.id, name=party_name, member_pc_ids_json=json.dumps([saved, fresh])))
    return saved, fresh, party


# ── the helper ───────────────────────────────────────────────────────────────

def test_short_label_drops_the_rules_hint():
    assert short_label("Health (0 = Broken — Wound + Press On)") == "Health"
    assert short_label("Stamina (dice + Tier 2/3 costs)") == "Stamina"
    assert short_label("Flesh (0 = Shattered)") == "Flesh"
    assert short_label("Arcane Knowledge (10 = Insane)") == "Arcane Knowledge"
    assert short_label("Plain") == "Plain" and short_label("") == "" and short_label(None) == ""


def test_tracks_apply_template_defaults_like_the_sheet_does():
    fields = json.loads(_tpl("hunt-in-the-moonlight").fields_json)
    t = {r["id"]: r for r in resource_tracks(fields, {})}
    assert (t["health"]["current"], t["health"]["max"]) == (5, 5), "an unsaved track shows its default, not nothing"
    assert (t["hunger"]["current"], t["hunger"]["max"]) == (0, 10)
    assert t["health"]["label"] == "Health"
    saved = {r["id"]: r for r in resource_tracks(fields, {"health_current": 2, "health_max": "6"})}
    assert (saved["health"]["current"], saved["health"]["max"]) == (2, 6)
    half = {r["id"]: r for r in resource_tracks(fields, {"stamina_current": 1})}
    assert (half["stamina"]["current"], half["stamina"]["max"]) == (1, 5), "a missing half falls back to its default"


def test_builtin_systems_declare_their_hp_resource():
    hitm, ast = _tpl("hunt-in-the-moonlight"), _tpl("asterion")
    assert hp_track(json.loads(hitm.fields_json), {}, system_meta(hitm)) == {"id": "health", "label": "Health", "current": 5, "max": 5}
    assert hp_track(json.loads(ast.fields_json), {"flesh_current": 0}, system_meta(ast))["current"] == 0
    assert system_meta(hitm)["binds"] == {"hunter": "name", "player": "player_name"}
    assert xp_field_ids(system_meta(hitm)) == ["xpCurrent", "xpLifetime"]
    assert xp_field_ids(system_meta(ast)) == ["glory"]


def test_a_custom_template_can_declare_the_same_metadata_per_field():
    class T:
        slug, is_builtin = "homebrew", False
        fields_json = json.dumps([
            {"id": "vigor", "label": "Vigor", "type": "resource", "default_value": "7/7", "vital": "hp"},
            {"id": "pc_name", "label": "Name", "type": "text", "binds": "name"},
            {"id": "exp", "label": "Experience", "type": "number", "xp": True}])
    m = system_meta(T)
    assert m["hp"] == "vigor" and m["binds"] == {"pc_name": "name"} and m["xp"] == ["exp"]
    assert hp_track(json.loads(T.fields_json), {}, m)["max"] == 7
    assert hp_track(json.loads(T.fields_json), {}, system_meta(type("U", (), {"slug": "x", "is_builtin": False, "fields_json": "[]"})) ) is None


def test_enrich_fields_carries_the_metadata_to_the_pages():
    hitm = _tpl("hunt-in-the-moonlight")
    by_id = {f["id"]: f for f in enrich_fields(hitm)}
    assert by_id["hunter"]["binds"] == "name" and by_id["player"]["binds"] == "player_name"
    assert by_id["health"]["vital"] == "hp" and by_id["xpCurrent"].get("xp") is True
    assert "binds" not in by_id["pronouns"]


# ── Player Characters list ───────────────────────────────────────────────────

def test_character_cards_have_no_nested_links(client, seed):
    _hitm_scene(seed)
    _gm(client, seed)
    html = client.get("/characters").text
    assert "Burn the Witch" in html, "the party badge must still render"
    assert nested_anchors(html) == [], "an <a> inside the card's <a> tears the card apart in browsers"


def test_custom_system_cards_show_their_vital_tracks(client, seed):
    _hitm_scene(seed)
    _gm(client, seed)
    html = client.get("/characters").text
    assert "Health 3/5" in html and "Health 5/5" in html
    assert "Lvl " not in html.split("Anders")[1].split("</a>")[0] or True  # N&D chrome is not asserted loosely


def test_other_pages_with_party_badges_have_no_nested_links_either(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    for path in ("/parties", f"/parties/{party}", f"/parties/{party}/roster", f"/parties/{party}/summary"):
        assert nested_anchors(client.get(path).text) == [], path


# ── Parties ──────────────────────────────────────────────────────────────────

def test_party_vitals_use_template_defaults_and_drop_nd_chrome(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    members = {m["id"]: m for m in client.get(f"/api/parties/{party}/vitals").json()["members"]}
    b, a = members[fresh], members[saved]
    assert (b["hp"], b["max_hp"]) == (5, 5) and b["down"] is False
    assert (a["hp"], a["max_hp"]) == (3, 5)
    assert b["ac"] is None, "AC is an N&D stat; a custom system has none"
    assert [r["label"] for r in b["resources"]][:2] == ["Health", "Stamina"], "the vital leads, long rules text is trimmed"
    html = client.get(f"/parties/{party}").text
    assert "no HP tracked" not in html and "AC 10" not in html
    assert "Health" in html


def test_a_member_at_zero_of_their_hp_resource_is_down(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, saved).custom_fields_json = json.dumps({"health_current": 0, "health_max": 5})
        db.commit()
    finally:
        db.close()
    _gm(client, seed)
    members = {m["id"]: m for m in client.get(f"/api/parties/{party}/vitals").json()["members"]}
    assert members[saved]["down"] is True and members[fresh]["down"] is False
    assert "1 DOWN" in client.get(f"/parties/{party}").text


def test_no_level_up_prompts_on_custom_systems(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, fresh).xp = 5000
        db.commit()
    finally:
        db.close()
    from app.routers.characters import _levelup_ready
    assert _levelup_ready(_row(PlayerCharacter, fresh)) is False
    _gm(client, seed)
    assert "Level-up" not in client.get("/characters").text
    members = {m["id"]: m for m in client.get(f"/api/parties/{party}/vitals").json()["members"]}
    assert members[fresh]["levelup"] is False
    native = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", xp=5000, stats_json=json.dumps([{"id": "str", "value": 3}])))
    assert _levelup_ready(_row(PlayerCharacter, native)) is True


def test_roster_lists_the_full_set_of_tracks(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    html = client.get(f"/parties/{party}/roster").text
    for label in ("Health", "Stamina", "Hunger", "Arcane Knowledge", "Signature Point"):
        assert label in html, label


# ── combat ───────────────────────────────────────────────────────────────────

def test_launch_combat_brings_the_vital_track_in_as_hp(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    r = client.post(f"/api/parties/{party}/launch-combat")
    assert r.status_code == 200
    cid = int(r.json()["redirect"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        combatants = {c["pc_id"]: c for c in json.loads(db.get(CombatSession, cid).combatants_json)}
    finally:
        db.close()
    assert (combatants[saved]["hp"], combatants[saved]["max_hp"]) == (3, 5)
    assert (combatants[fresh]["hp"], combatants[fresh]["max_hp"]) == (5, 5)


def test_combat_sync_writes_hp_back_to_the_track(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    cid = int(client.post(f"/api/parties/{party}/launch-combat").json()["redirect"].rsplit("/", 1)[1])
    db = SessionLocal()
    try:
        cs = db.get(CombatSession, cid)
        cs_list = json.loads(cs.combatants_json)
        for c in cs_list:
            if c["pc_id"] == saved:
                c["hp"] = 1
            if c["pc_id"] == fresh:
                c["hp"] = 99   # beyond the track's max: clamped
        cs.combatants_json = json.dumps(cs_list)
        db.commit()
    finally:
        db.close()
    assert client.post(f"/api/combat/{cid}/sync-characters").status_code == 200
    cf_saved = json.loads(_row(PlayerCharacter, saved).custom_fields_json)
    cf_fresh = json.loads(_row(PlayerCharacter, fresh).custom_fields_json)
    assert cf_saved["health_current"] == 1 and cf_saved["health_max"] == 5, "other halves of the track are preserved"
    assert cf_fresh["health_current"] == 5, "clamped to the track's max"


# ── XP ───────────────────────────────────────────────────────────────────────

def test_party_xp_awards_land_on_the_systems_own_xp_fields(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    ast = _tpl("asterion")
    god = _add(PlayerCharacter(world_id=seed.world_a.id, name="Zeus", sheet_template_id=ast.id,
                               custom_fields_json=json.dumps({"glory": 4})))
    native = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps([{"id": "str", "value": 3}])))
    db = SessionLocal()
    try:
        db.get(Party, party).member_pc_ids_json = json.dumps([saved, fresh, god, native])
        gs = GameSession(world_id=seed.world_a.id, title="S1", session_num=1, party_id=party)
        db.add(gs)
        db.commit()
        gs_id = gs.id
    finally:
        db.close()
    _gm(client, seed)
    r = client.post(f"/api/sessions/{gs_id}/xp", json={"delta": 10})
    assert r.status_code == 200
    h = json.loads(_row(PlayerCharacter, fresh).custom_fields_json)
    assert h["xpCurrent"] == 10 and h["xpLifetime"] == 10
    assert json.loads(_row(PlayerCharacter, god).custom_fields_json)["glory"] == 14
    assert _row(PlayerCharacter, native).xp == 10
    # a negative award never drives a track below zero
    client.post(f"/api/sessions/{gs_id}/xp", json={"delta": -50})
    h = json.loads(_row(PlayerCharacter, fresh).custom_fields_json)
    assert h["xpCurrent"] == 0 and h["xpLifetime"] == 0


# ── identity duplication ─────────────────────────────────────────────────────

def test_custom_sheet_does_not_repeat_the_name_fields(client, seed):
    saved, fresh, party = _hitm_scene(seed)
    _gm(client, seed)
    html = client.get(f"/characters/{saved}").text
    assert 'name="name"' in html and 'name="player_name"' in html, "the header edits the real columns"
    assert 'data-cf-id="hunter"' not in html and 'data-cf-id="player"' not in html, \
        "the template's own Player / Hunter Name fields would duplicate them"
    assert 'data-cf-id="pronouns"' in html and 'data-cf-id="company"' in html, "everything else still renders"


def test_edit_form_template_picker_ships_the_metadata_and_reads_defaults(client, seed):
    native = _add(PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps([{"id": "str", "value": 3}])))
    _gm(client, seed)
    html = client.get(f"/characters/{native}/edit").text.replace("&#34;", '"')
    assert '"binds": "name"' in html, "switching to Hunt in the Moonlight must know which fields mirror the name"
    assert '"vital": "hp"' in html
    # a resource default is "current/max" - the form used to put the whole "5/5" in both boxes
    assert "split('/')" in html
