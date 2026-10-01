"""A character on a *custom* sheet template is never treated as a native N&D
character — even with N&D stats lying around in its stats_json (leftovers from
the wizard, an import that defaulted them, or switching template on the edit
form). The roster used to show such a Hunt in the Moonlight character with
"0 / 22 DOWN", PP/MP and a STR..ITU column while its real Health was 5/5."""
import json

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate
from app.pc_stats import pc_maxima

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

STATS = [{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]


def _tpl_id(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
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


def _hunter(seed, **kw):
    return _add(PlayerCharacter(
        world_id=seed.world_a.id, name=kw.pop("name", "Mirabel"), stats_json=json.dumps(STATS), current_hp=0, max_hp=0,
        sheet_template_id=_tpl_id("hunt-in-the-moonlight"), custom_fields_json=json.dumps(
            {"health_current": 5, "health_max": 5, "stamina_current": 5, "stamina_max": 5}), **kw))


def _load(pc_id):
    db = SessionLocal()
    try:
        return db.get(PlayerCharacter, pc_id)  # kept attached: the test closes nothing it still needs
    finally:
        pass


def test_custom_template_wins_over_leftover_stats(seed):
    pc = _load(_hunter(seed))
    m = pc_maxima(pc)
    assert m["native"] is False
    assert (m["pp"], m["mp"], m["phys"], m["ment"]) == (0, 0, 0, 0)
    assert m["hp"] == 0, "no invented N&D hit points on a custom sheet"


def test_nd_template_with_stats_stays_native(seed):
    pc_id = _add(PlayerCharacter(world_id=seed.world_a.id, name="Vex", stats_json=json.dumps(STATS),
                                 sheet_template_id=_tpl_id("nd-default")))
    assert pc_maxima(_load(pc_id))["native"] is True


def test_no_template_stays_native(seed):
    pc_id = _add(PlayerCharacter(world_id=seed.world_a.id, name="Plain", stats_json=json.dumps(STATS), max_hp=0))
    m = pc_maxima(_load(pc_id))
    assert m["native"] is True and m["hp"] == 22


def test_a_detached_character_does_not_crash(seed):
    db = SessionLocal()
    pc = db.get(PlayerCharacter, _hunter(seed))
    db.expunge(pc)
    db.close()
    assert pc_maxima(pc)["native"] in (True, False)  # no DetachedInstanceError


def test_party_vitals_show_the_real_health_not_a_phantom_down(client, seed):
    pc = _hunter(seed)
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc])))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    v = client.get(f"/api/parties/{party}/vitals").json()["members"][0]
    assert v["native"] is False and v["down"] is False
    assert (v["hp"], v["max_hp"]) == (5, 5) and v["hp_label"].lower().startswith("health")
