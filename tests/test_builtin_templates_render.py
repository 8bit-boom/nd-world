"""The sheets render the rulebook-aligned built-in systems: dropdowns with the books'
option lists (and a value saved under an older list survives), the system's own
condition chips, and the template editor can author the same things."""
import json
import re

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug, SheetTemplate.is_builtin == True).first().id  # noqa: E712
    finally:
        db.close()


def _pc(seed, slug, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Subject", sheet_template_id=_tpl(slug), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _select_options(html, field_id):
    m = re.search(r'<select data-cf-id="%s"[^>]*>(.*?)</select>' % re.escape(field_id), html, re.S)
    assert m, f"no <select data-cf-id={field_id}> on the page"
    return re.findall(r'<option value="([^"]*)"', m.group(1))


def test_hitm_sheet_renders_race_as_a_dropdown_with_the_books_names(client, seed):
    pc = _pc(seed, "hunt-in-the-moonlight", custom_fields_json=json.dumps({"race": "Rootbound"}))
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    opts = _select_options(html, "race")
    assert opts[0] == "" and "Lamrossa Wrought" in opts and "The Unwritten" in opts and "Elf" not in opts
    assert re.search(r'<option value="Rootbound" selected', html), "the saved race is the selected one"


def test_a_value_saved_under_an_older_option_list_is_kept(client, seed):
    pc = _pc(seed, "hunt-in-the-moonlight", custom_fields_json=json.dumps({"race": "Elf", "moonPhase": "Crescent"}))
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert re.search(r'<option value="Elf" selected', html), "'Elf' predates the Rootbound rename; it must not be blanked"
    assert re.search(r'<option value="Crescent" selected', html)


def test_defaults_apply_to_unsaved_selects(client, seed):
    pc = _pc(seed, "hunt-in-the-moonlight")
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert re.search(r'<option value="Grey" selected', html), "the Moon starts Grey"
    assert re.search(r'<option value="Available" selected', html), "Press On starts available"


def test_condition_chips_follow_the_system(client, seed):
    hitm = _pc(seed, "hunt-in-the-moonlight")
    ast = _pc(seed, "asterion")
    _gm(client, seed)
    h = client.get(f"/characters/{hitm}").text
    assert 'data-cond="Prone"' in h and 'data-cond="Marked"' in h and 'data-cond="Yellow"' not in h
    a = client.get(f"/characters/{ast}").text
    assert 'data-cond="Vulnerable"' in a and 'data-cond="Weakened"' in a and 'data-cond="Freeze"' not in a


def test_native_sheet_keeps_the_nd_conditions(client, seed):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Nat", stats_json=json.dumps([{"id": "str", "value": 3}]))
        db.add(pc)
        db.commit()
        db.refresh(pc)
        pid = pc.id
    finally:
        db.close()
    _gm(client, seed)
    html = client.get(f"/characters/{pid}").text
    assert 'data-cond="Yellow"' in html and 'data-cond="Freeze"' in html


def test_list_subfields_ship_their_dropdown_options(client, seed):
    pc = _pc(seed, "hunt-in-the-moonlight")
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    assert '"type": "select"' in html.replace("&#34;", '"') or '"type":"select"' in html
    assert "Minor" in html and "Major" in html


def test_asterion_resources_show_their_rule_defaults(client, seed):
    pc = _pc(seed, "asterion")
    _gm(client, seed)
    html = client.get(f"/characters/{pc}").text
    vals = re.findall(r'data-cf-id="(sparkShield|flesh|ichor)_(current|max)" value="(\d+)"', html)
    assert sorted(vals) == [("flesh", "current", "5"), ("flesh", "max", "5"), ("ichor", "current", "5"),
                            ("ichor", "max", "5"), ("sparkShield", "current", "3"), ("sparkShield", "max", "3")]


def test_template_editor_can_author_dropdowns_and_system_links(client, seed):
    _gm(client, seed)
    html = client.get(f"/characters/templates/{_tpl('asterion')}/edit").text
    for needle in ("Dropdown", "setOptions", "setVital", "setBinds", "setXp", "Options (comma-separated)"):
        assert needle in html, needle
