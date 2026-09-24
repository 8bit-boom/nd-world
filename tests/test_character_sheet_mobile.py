"""Regression tests for two character-sheet bugs from
docs/MOBILE_UI_UX_AUDIT.md's "character sheet mobile layout and unconfirmed
delete buttons" backlog item:

1. Mobile layout: characters/form.html's Feats/Equipment/Cyberware/Currency
   tables are entirely <input>/<select> cells with width:100% — but form
   controls have a browser-default intrinsic min-width regardless of that
   CSS, so a table full of them can end up wider than the viewport on a
   phone. With no scroll wrapper, that overflow was unreachable (mobile
   sets overflow-x:clip on html/body, which can't even be scrolled to).

2. Unconfirmed deletes: several "✕ remove" buttons deleted a row with zero
   confirmation — most seriously characters/sheet.html's removeFeatQuick/
   removeEquipmentQuick, which fire an immediate real API call (not gated
   behind a later Save, unlike the in-memory edit-form removals), so a
   single accidental tap on a small icon button permanently removed a
   feat/equipment item. Also fixed the lower-severity in-memory removals
   (characters/form.html's delFeat/delEquipment/delCyberware/delCurrency,
   characters/custom_sheet.html's list-item remove) for consistency with
   the page-level delete/retire buttons, which already used confirm()."""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login


def _make_pc(world_id, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name=kw.pop("name", "Test Hero"), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def _make_custom_sheet_pc(world_id, name="Custom Sheet Hero"):
    db = SessionLocal()
    try:
        tpl = SheetTemplate(
            world_id=world_id, name="Custom Test Template", slug=f"custom-test-{world_id}-{name}",
            sheet_mode="custom",
            fields_json=json.dumps([{
                "id": "abilities", "label": "Abilities", "type": "list", "section": "Main",
                "item_fields": [{"id": "name", "label": "Name", "type": "text"}],
            }]),
        )
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        pc = PlayerCharacter(world_id=world_id, name=name, sheet_template_id=tpl.id)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


def test_character_edit_form_wraps_dynamic_tables_in_a_scrollable_container(client, seed):
    pc = _make_pc(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc.id}/edit")
    assert r.status_code == 200
    assert r.text.count('class="cf-dynamic-table-wrap"') == 4
    wrap_block = r.text.split(".cf-dynamic-table-wrap {", 1)[1].split("}", 1)[0]
    assert "overflow-x:auto" in wrap_block.replace(" ", "")


def test_character_edit_form_delete_buttons_confirm_before_removing(client, seed):
    pc = _make_pc(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc.id}/edit")
    assert r.status_code == 200
    for fn_name in ("delCurrency", "delFeat", "delEquipment", "delCyberware"):
        fn_src = r.text.split(f"function {fn_name}(i) {{", 1)[1].split("}", 1)[0]
        assert "confirm(" in fn_src, f"{fn_name} removes without confirming"


def test_character_sheet_quick_remove_buttons_confirm_before_calling_the_api(client, seed):
    """removeFeatQuick/removeEquipmentQuick fire a real, immediate API
    request — unlike the edit form's in-memory-only removals, there's no
    later Save gate protecting an accidental tap here."""
    pc = _make_pc(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc.id}")
    assert r.status_code == 200
    for fn_name in ("removeFeatQuick", "removeEquipmentQuick"):
        fn_src = r.text.split(f"async function {fn_name}(index) {{", 1)[1].split("fetch(", 1)[0]
        assert "confirm(" in fn_src, f"{fn_name} calls the API without confirming"


def test_custom_sheet_list_item_remove_confirms(client, seed):
    pc = _make_custom_sheet_pc(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc.id}")
    assert r.status_code == 200
    assert "buildListItem" in r.text  # sanity: custom_sheet.html actually rendered
    onclick_src = r.text.split("rm.onclick = function () {", 1)[1].split("};", 1)[0]
    assert "confirm(" in onclick_src
