"""The party roster is built from each member's OWN system. A Hunt in the Moonlight or
Asterion party must not get a wall of N&D rows (Shock, PP/MP, STR..ITU, Cyber Adapt.,
Edges, Cyberware) full of dashes — and a character on a custom sheet must never
inherit another system's numbers or a phantom "DOWN" from leftover N&D columns."""
import json
import re

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

STATS = [{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]
ND_ROWS = ("Shock", "PP / MP", "STR", "DEX", "Cyber Adapt.", "Speed", "Edges", "Cyberware")


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _hunter(seed, name, **cf):
    base = {"health_current": 5, "health_max": 5, "stamina_current": 4, "stamina_max": 5,
            "hunger_current": 2, "hunger_max": 10, "race": "Human", "armor": 2, "xpCurrent": 7}
    base.update(cf)
    return _add(PlayerCharacter(world_id=seed.world_a.id, name=name, player_name="Archie", max_hp=0, current_hp=0,
                                sheet_template_id=_tpl("hunt-in-the-moonlight"), custom_fields_json=json.dumps(base)))


def _god(seed, name):
    return _add(PlayerCharacter(world_id=seed.world_a.id, name=name, max_hp=0, sheet_template_id=_tpl("asterion"),
                                custom_fields_json=json.dumps({"flesh_current": 2, "flesh_max": 5, "glory": 12, "kind": "God (Origin)"})))


def _native(seed, name):
    return _add(PlayerCharacter(world_id=seed.world_a.id, name=name, stats_json=json.dumps(STATS), max_hp=0,
                                current_hp=9, shock_current=3, pp_current=4, mp_current=2))


def _roster(client, seed, ids, name="Crew"):
    party = _add(Party(world_id=seed.world_a.id, name=name, member_pc_ids_json=json.dumps(ids)))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{party}/roster")
    assert r.status_code == 200
    return r.text


def _row_labels(html):
    return [re.sub(r"<[^>]+>", "", m).strip() for m in re.findall(r'<th scope="row">(.*?)</th>', html, re.S)]


def _row(html, label):
    m = re.search(r'<th scope="row">' + re.escape(label) + r"</th>(.*?)</tr>", html, re.S)
    assert m, f"no {label!r} row; rows are {_row_labels(html)}"
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<td>(.*?)</td>", m.group(1), re.S)]


def test_a_hunter_only_party_has_no_nd_rows(client, seed):
    html = _roster(client, seed, [_hunter(seed, "Anders"), _hunter(seed, "Mirabel", health_current=3)])
    labels = _row_labels(html)
    for nd in ND_ROWS:
        assert nd not in labels, f"{nd} is an N&D row and does not belong on a Hunt in the Moonlight roster"
    for want in ("Health", "Conditions", "Stamina", "Hunger", "Armor", "Race", "Current XP"):
        assert want in labels, (want, labels)
    assert _row(html, "Health") == ["5 / 5", "3 / 5"]
    assert _row(html, "Stamina") == ["4 / 5", "4 / 5"]
    assert _row(html, "Race") == ["Human", "Human"]


def test_leftover_nd_stats_never_make_a_hunter_native_or_down(client, seed):
    ghost = _add(PlayerCharacter(world_id=seed.world_a.id, name="Ghost", stats_json=json.dumps(STATS), current_hp=0,
                                 max_hp=0, sheet_template_id=_tpl("hunt-in-the-moonlight"),
                                 custom_fields_json=json.dumps({"health_current": 5, "health_max": 5})))
    html = _roster(client, seed, [ghost])
    assert "DOWN" not in html and "STR" not in _row_labels(html) and "Shock" not in _row_labels(html)
    assert _row(html, "Health") == ["5 / 5"]


def test_a_downed_hunter_is_flagged_by_their_real_health(client, seed):
    html = _roster(client, seed, [_hunter(seed, "Fallen", health_current=0)])
    assert "DOWN" in html


def test_asterion_rows(client, seed):
    html = _roster(client, seed, [_god(seed, "Zeus")])
    labels = _row_labels(html)
    assert "Flesh" in labels and "Glory" in labels and "Shock" not in labels
    assert _row(html, "Glory") == ["12"]
    assert _row(html, "Flesh") == ["2 / 5"]


def test_a_mixed_party_dashes_only_where_the_system_lacks_the_stat(client, seed):
    # columns sort by name: Anders (Hunt in the Moonlight), Vex (native N&D), Zeus (Asterion)
    html = _roster(client, seed, [_native(seed, "Vex"), _hunter(seed, "Anders"), _god(seed, "Zeus")])
    labels = _row_labels(html)
    assert "HP / vital" in labels, "members with different vitals get a generic label"
    for nd in ("Shock", "PP / MP", "STR", "Cyber Adapt."):
        assert nd in labels
    assert _row(html, "Shock") == ["—", "3 / 12", "—"]
    assert _row(html, "STR") == ["—", "3", "—"]
    assert _row(html, "Stamina") == ["4 / 5", "—", "—"]
    assert _row(html, "Glory") == ["—", "—", "12"]
    assert _row(html, "Race")[0] == "Human"


def test_header_names_the_system_for_custom_sheets(client, seed):
    html = _roster(client, seed, [_native(seed, "Vex"), _hunter(seed, "Anders")])
    assert "Hunt in the Moonlight" in html
    assert "Lv 1" in html  # the native member still shows its level


def test_unknown_custom_template_falls_back_to_its_number_and_select_fields(client, seed):
    tid = _add(SheetTemplate(world_id=seed.world_a.id, name="Homebrew", slug="homebrew-x", sheet_mode="custom",
                             fields_json=json.dumps([
                                 {"id": "grit", "label": "Grit", "type": "resource", "default_value": "3/3", "section": "A"},
                                 {"id": "luck", "label": "Luck", "type": "number", "section": "A"},
                                 {"id": "path", "label": "Path", "type": "select", "options": ["Fox", "Owl"], "section": "A"},
                                 {"id": "bio", "label": "Biography", "type": "textarea", "section": "A"}])))
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, name="Pip", sheet_template_id=tid, max_hp=0,
                              custom_fields_json=json.dumps({"grit_current": 2, "luck": 7, "path": "Owl", "bio": "long text"})))
    html = _roster(client, seed, [pc])
    labels = _row_labels(html)
    assert {"Grit", "Luck", "Path"} <= set(labels) and "Biography" not in labels
    assert _row(html, "Luck") == ["7"] and _row(html, "Path") == ["Owl"]
