"""Regression tests for docs/MOBILE_UI_UX_AUDIT.md item 4.6 ("Combat tracker
touch targets ~18px") plus an unconfirmed-delete papercut found alongside it:
combat/detail.html's remove-combatant handler spliced the combatant and
immediately persisted via save() with zero confirmation — a single
accidental tap on the small ✕ button (font-size:1.1rem, padding:0 .2rem,
exactly the kind of target that's easy to mis-tap on a touchscreen)
permanently dropped a combatant's HP/shock/conditions/initiative mid-fight."""
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


def test_remove_combatant_confirms_before_removing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    combat_id = _make_combat(seed.world_a.id)
    r = client.get(f"/combat/{combat_id}")
    assert r.status_code == 200
    handler = r.text.split("dataset.action === 'remove') {", 1)[1].split("} else if", 1)[0]
    assert "confirm(" in handler
    # The condition-remove ("remove-cond") handler is deliberately left
    # alone — removing a status-effect chip is trivial and quick to redo
    # mid-fight, unlike losing a whole combatant's tracked stats.
    cond_handler = r.text.split("dataset.action === 'remove-cond') {", 1)[1].split("} else if", 1)[0]
    assert "confirm(" not in cond_handler


def test_stat_buttons_and_remove_button_get_a_pointer_coarse_touch_bump(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    combat_id = _make_combat(seed.world_a.id)
    r = client.get(f"/combat/{combat_id}")
    assert r.status_code == 200

    coarse_block = r.text.split("@media (pointer:coarse) {", 1)[1].split("\n}\n", 1)[0]
    stat_btn_block = coarse_block.split(".stat-btn {", 1)[1].split("}", 1)[0]
    assert "min-height:36px" in stat_btn_block.replace(" ", "")

    remove_block = coarse_block.split(".combatant-remove {", 1)[1].split("}", 1)[0]
    assert "min-height:36px" in remove_block.replace(" ", "")

    # The base (non-coarse) .stat-btn rule is still the original small size
    # — the bump is additive for touch input, not a global change.
    base_block = r.text.split(".stat-btn {", 1)[1].split("}", 1)[0]
    assert "padding:.15rem .4rem" in base_block


def test_combat_detail_still_renders_and_functions_after_the_confirm_guard(client, seed):
    """Sanity: the confirm() early-return doesn't break the normal render
    path (combatants_json still parses, page still renders combatant data)."""
    db = SessionLocal()
    try:
        cs = CombatSession(
            world_id=seed.world_a.id, name="Ambush",
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
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/combat/{combat_id}")
    assert r.status_code == 200
    assert "Chrome Syndicate Thug" in r.text
