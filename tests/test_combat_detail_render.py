"""Regression test for GET /combat/{id}: the page's own inline <script>
block used to contain a literal "</script>" inside a JS comment explaining
an XSS defense (ironic — the comment itself broke the exact thing it was
describing). Browsers match a closing script tag on the raw byte sequence
regardless of JS syntax (comments included), so that stray substring closed
the tag early and none of the tracker's JS ever ran. No test previously
rendered this route at all, which is how it went uncaught.
"""
import json

from app.database import SessionLocal
from app.models import CombatSession

from .conftest import GM_PASSWORD, login


def _make_combat(world_id, name="Test Fight"):
    db = SessionLocal()
    try:
        cs = CombatSession(world_id=world_id, name=name, combatants_json="[]", round_num=1, active_idx=0)
        db.add(cs)
        db.commit()
        db.refresh(cs)
        return cs.id
    finally:
        db.close()


def test_combat_detail_script_block_is_not_closed_early(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    combat_id = _make_combat(seed.world_a.id)

    r = client.get(f"/combat/{combat_id}")
    assert r.status_code == 200
    html = r.text

    marker = "const COMBAT_ID"
    marker_idx = html.index(marker)
    # Same rule a real browser's HTML parser applies: the first
    # case-insensitive literal "</script" byte sequence after the opening
    # tag closes it, regardless of what JS/comment syntax surrounds it.
    close_idx = html.lower().index("</script", marker_idx)
    script_body = html[marker_idx:close_idx]

    # function render()/save() are defined well past the comment that used
    # to contain the stray closing tag — if the tag closed early, this
    # span would end at the comment and never reach them.
    assert "function render()" in script_body
    assert "function save()" in script_body
    assert "</script>" not in script_body.lower().replace("</script", "")  # sanity: no other close hid inside


def test_combat_detail_renders_combatant_data(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        cs = CombatSession(
            world_id=seed.world_a.id, name="Ambush", round_num=2, active_idx=1,
            combatants_json=json.dumps([{
                "id": "c-1", "name": "Chrome Syndicate Thug", "source": "manual",
                "pc_id": None, "entity_id": None, "initiative": 12,
                "max_hp": 10, "hp": 7, "max_shock": 0, "shock": 0, "armor": 1,
                "conditions": [], "notes": "",
            }]),
        )
        db.add(cs)
        db.commit()
        db.refresh(cs)
        combat_id = cs.id
    finally:
        db.close()

    r = client.get(f"/combat/{combat_id}")
    assert r.status_code == 200
    assert "Chrome Syndicate Thug" in r.text
    assert "let combatants = JSON.parse(" in r.text
