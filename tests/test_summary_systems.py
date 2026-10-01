"""Party summary / detail show a custom-sheet member by its own system, not as "Lvl 1"."""
import json

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _scene(seed):
    db = SessionLocal()
    try:
        tid = db.query(SheetTemplate).filter(SheetTemplate.slug == "hunt-in-the-moonlight").first().id
    finally:
        db.close()
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Anders", max_hp=0, sheet_template_id=tid,
                              stats_json=json.dumps([{"id": "str", "value": 3}]),
                              custom_fields_json=json.dumps({"health_current": 4, "health_max": 5})))
    nat = _add(PlayerCharacter(world_id=seed.world_a.id, name="Vex", level=3, stats_json=json.dumps([{"id": "str", "value": 3}])))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc, nat])))
    return pc, nat, party


@pytest.mark.parametrize("path", ["/parties/{p}/summary", "/parties/{p}"])
def test_custom_member_shows_its_system_not_a_level(client, seed, path):
    _, _, party = _scene(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(path.format(p=party)).text
    assert "Hunt in the Moonlight" in html
    assert "Lvl 3" in html, "the native member still shows its level"
    assert html.count("Lvl 1") == 0, "a Hunt in the Moonlight character has no N&D level"
    assert "Health 4/5" in html
