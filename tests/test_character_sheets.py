"""Tests for player-fillable character sheets (app/routers/character_sheets.py,
CharacterSheet + PageDoc.is_character_sheet_template in app/models.py) — a
player's personal, fillable copy of a GM-marked Pages template. Access is GM
+ the owning player only, everywhere — not GM-Assistant, not another player
(even with players_see_party on), unlike Pages itself which lets an assistant
manage templates. See character_sheets.py's own module docstring for the
full reasoning.
"""
import io
import json

import pytest

from app.database import SessionLocal
from app.models import CharacterSheet, PageDoc, PlayerCharacter, User, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_HTML_BYTES = b"<!DOCTYPE html><html><body><input name=\"hp\"/></body></html>"


def _html_file(name="sheet.html"):
    return {"file": (name, io.BytesIO(_HTML_BYTES), "text/html")}


def _add_doc(world_id, **kw):
    db = SessionLocal()
    try:
        d = PageDoc(world_id=world_id, name=kw.pop("name", "Template"),
                    file_url=kw.pop("file_url", "/uploads/pages/x.html"), **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
        return d.id
    finally:
        db.close()


def _add_pc(world_id, owner_user_id, **kw):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_user_id,
                              name=kw.pop("name", "Hero"), **kw)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _add_sheet(world_id, template_id, owner_user_id, **kw):
    db = SessionLocal()
    try:
        s = CharacterSheet(world_id=world_id, template_id=template_id, owner_user_id=owner_user_id,
                            name=kw.pop("name", "My Sheet"), **kw)
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


def _make_assistant(world_id, user_id):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == world_id, WorldMembership.user_id == user_id,
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def _second_player_in_world(world_id, email="player-c@test.local"):
    from types import SimpleNamespace

    from .conftest import _PLAYER_PASSWORD_HASH
    db = SessionLocal()
    try:
        u = User(email=email, password_hash=_PLAYER_PASSWORD_HASH, display_name="Player C", is_gm=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.add(WorldMembership(world_id=world_id, user_id=u.id))
        db.commit()
        return SimpleNamespace(id=u.id, email=u.email)
    finally:
        db.close()


# ── Marking a template ──────────────────────────────────────────────────────

def test_gm_can_mark_page_as_character_sheet_template(client, seed):
    did = _add_doc(seed.world_a.id, name="Blank Sheet")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit",
                     data={"name": "Blank Sheet", "is_character_sheet_template": "1", "visible_to_players": "1"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(PageDoc, did).is_character_sheet_template is True
    finally:
        db.close()


def test_player_cannot_mark_page_as_template(client, seed):
    did = _add_doc(seed.world_a.id, name="Blank Sheet")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/{did}/edit", data={"is_character_sheet_template": "1"})
    assert r.status_code == 403


def test_upload_can_set_template_flag(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/upload", data={"is_character_sheet_template": "1"},
                     files=_html_file(), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        doc = db.query(PageDoc).filter(PageDoc.world_id == seed.world_a.id).first()
        assert doc.is_character_sheet_template is True
    finally:
        db.close()


def test_pages_library_shows_badge_and_new_sheet_button_for_template(client, seed):
    _add_doc(seed.world_a.id, name="Blank Sheet", is_character_sheet_template=True, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages")
    assert "sheet template" in r.text.lower()
    assert "/pages/sheets/new" in r.text


# ── Create ───────────────────────────────────────────────────────────────

def test_player_can_create_sheet_from_visible_template(client, seed):
    did = _add_doc(seed.world_a.id, name="Blank Sheet", is_character_sheet_template=True, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/pages/sheets/")
    db = SessionLocal()
    try:
        sheet = db.query(CharacterSheet).filter(CharacterSheet.world_id == seed.world_a.id).first()
        assert sheet is not None
        assert sheet.template_id == did
        assert sheet.owner_user_id == seed.player_a.id
    finally:
        db.close()


def test_create_from_hidden_template_404s_for_player(client, seed):
    did = _add_doc(seed.world_a.id, name="Hidden", is_character_sheet_template=True, visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did)})
    assert r.status_code == 404


def test_create_from_non_template_page_400s(client, seed):
    did = _add_doc(seed.world_a.id, name="Just a doc", is_character_sheet_template=False, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did)})
    assert r.status_code == 400


def test_player_can_link_own_pc_on_create(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did), "player_character_id": str(pc_id)},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        sheet = db.query(CharacterSheet).filter(CharacterSheet.world_id == seed.world_a.id).first()
        assert sheet.player_character_id == pc_id
        assert sheet.name == "Rook"
    finally:
        db.close()


def test_player_cannot_link_another_players_pc(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    other = _second_player_in_world(seed.world_a.id)
    other_pc_id = _add_pc(seed.world_a.id, other.id, name="NotYours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did), "player_character_id": str(other_pc_id)})
    assert r.status_code == 400


def test_gm_can_create_sheet_owned_by_themself(client, seed):
    """A sheet created via /new is always owned by the CALLER (see
    character_sheets.py's own _owned_pc_or_400 docstring: "the SHEET's
    owner, not necessarily the caller" only applies to /edit, where a GM
    can act on a player's existing sheet — /new has no such split, the
    caller and the new sheet's owner are the same person). So a GM hitting
    /new becomes the sheet's owner and can only link one of their OWN
    PlayerCharacters here, same as a player would."""
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did)}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        sheet = db.query(CharacterSheet).filter(CharacterSheet.world_id == seed.world_a.id).first()
        assert sheet.owner_user_id == seed.gm.id
        assert sheet.player_character_id is None
    finally:
        db.close()


def test_gm_creating_via_new_cannot_link_a_players_pc(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/pages/sheets/new", data={"template_id": str(did), "player_character_id": str(pc_id)})
    assert r.status_code == 400


def test_gm_can_relink_a_players_sheet_to_their_own_pc_via_edit(client, seed):
    """Unlike /new, /edit's ownership check is scoped to the SHEET's owner
    (see _owned_pc_or_400's docstring) — this is where "a GM editing on a
    player's behalf" actually applies."""
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id, name="Rooks Sheet")
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/edit", data={"name": "Rooks Sheet", "player_character_id": str(pc_id)},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(CharacterSheet, sid).player_character_id == pc_id
    finally:
        db.close()


def test_player_can_create_multiple_sheets(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/pages/sheets/new", data={"template_id": str(did)})
    client.post("/pages/sheets/new", data={"template_id": str(did)})
    db = SessionLocal()
    try:
        count = db.query(CharacterSheet).filter(
            CharacterSheet.world_id == seed.world_a.id, CharacterSheet.owner_user_id == seed.player_a.id,
        ).count()
        assert count == 2
    finally:
        db.close()


# ── Access control ───────────────────────────────────────────────────────

def test_owner_can_open_editor(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}").status_code == 200


def test_gm_can_open_any_sheet_editor(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}").status_code == 200


def test_other_player_404s_on_sheet_editor(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}").status_code == 404


def test_assistant_404s_on_sheet_editor(client, seed):
    """Confirms the GM + owner only decision: unlike Pages templates
    (which an assistant may manage via can_edit_content), a filled sheet
    is personal player data an assistant has zero special rights to."""
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id, email="assistant@test.local")
    _make_assistant(seed.world_a.id, other.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}").status_code == 404


def test_sheet_editor_cross_world_404s(client, seed):
    did = _add_doc(seed.world_b.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_b.id, did, seed.player_b.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}").status_code == 404


# ── Save ─────────────────────────────────────────────────────────────────

def test_owner_can_save_data(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/save", json={"data": {"hp": "12"}})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        sheet = db.get(CharacterSheet, sid)
        assert json.loads(sheet.data_json) == {"hp": "12"}
    finally:
        db.close()


def test_non_owner_cannot_save(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/save", json={"data": {"hp": "999"}})
    assert r.status_code == 404
    db = SessionLocal()
    try:
        assert db.get(CharacterSheet, sid).data_json == "{}"
    finally:
        db.close()


def test_save_rejects_non_dict_data(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/save", json={"data": "not a dict"})
    assert r.status_code == 400


# ── Render injection + sandboxed headers ────────────────────────────────

def _add_downloadable_template(tmp_path, monkeypatch, world_id, **kw):
    # character_sheets.py does `from .pages import _UPLOADS_DIR` — a direct
    # name import, not a module reference — so it must be monkeypatched on
    # character_sheets' own namespace too, not just pages'.
    from app.routers import character_sheets as character_sheets_module
    from app.routers import pages as pages_module
    monkeypatch.setattr(pages_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(character_sheets_module, "_UPLOADS_DIR", tmp_path)
    (tmp_path / "pages").mkdir(exist_ok=True)
    fname = kw.pop("fname", "sheet.html")
    (tmp_path / "pages" / fname).write_bytes(_HTML_BYTES)
    return _add_doc(world_id, file_url=f"/uploads/pages/{fname}",
                     is_character_sheet_template=True, visible_to_players=True, **kw)


def test_render_injects_bridge_and_data(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_template(tmp_path, monkeypatch, seed.world_a.id)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id, data_json=json.dumps({"hp": "7"}))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/sheets/{sid}/render")
    assert r.status_code == 200
    assert "character-sheet-bridge.js" in r.text
    assert f"window.__ND_SHEET_ID__={sid}" in r.text
    assert '"hp": "7"' in r.text or '"hp":"7"' in r.text
    assert r.headers["content-security-policy"] == "sandbox allow-scripts allow-popups"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_render_404s_for_other_player(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_template(tmp_path, monkeypatch, seed.world_a.id)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}/render").status_code == 404


# ── Rename / PC link ─────────────────────────────────────────────────────

def test_owner_can_rename_and_relink(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/edit",
                     data={"name": "Renamed", "player_character_id": str(pc_id)}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        sheet = db.get(CharacterSheet, sid)
        assert sheet.name == "Renamed"
        assert sheet.player_character_id == pc_id
    finally:
        db.close()


def test_edit_unlink_pc_with_blank_value(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id, player_character_id=pc_id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/pages/sheets/{sid}/edit", data={"name": "Rook", "player_character_id": ""})
    db = SessionLocal()
    try:
        assert db.get(CharacterSheet, sid).player_character_id is None
    finally:
        db.close()


# ── Delete ───────────────────────────────────────────────────────────────

def test_owner_can_delete_sheet(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(CharacterSheet, sid) is None
    finally:
        db.close()


def test_other_player_cannot_delete_sheet(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/pages/sheets/{sid}/delete")
    assert r.status_code == 404
    db = SessionLocal()
    try:
        assert db.get(CharacterSheet, sid) is not None
    finally:
        db.close()


# ── Download ─────────────────────────────────────────────────────────────

def test_owner_can_download_sheet(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_template(tmp_path, monkeypatch, seed.world_a.id)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id, name="My Sheet",
                      data_json=json.dumps({"hp": "9"}))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/pages/sheets/{sid}/download")
    assert r.status_code == 200
    assert 'attachment; filename="My Sheet.html"' in r.headers["content-disposition"]
    assert '"hp": "9"' in r.text or '"hp":"9"' in r.text


def test_other_player_404s_on_download(client, seed, tmp_path, monkeypatch):
    did = _add_downloadable_template(tmp_path, monkeypatch, seed.world_a.id)
    sid = _add_sheet(seed.world_a.id, did, seed.player_a.id)
    other = _second_player_in_world(seed.world_a.id)
    login(client, other.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/pages/sheets/{sid}/download").status_code == 404


# ── List page ────────────────────────────────────────────────────────────

def test_player_sees_only_own_sheets_on_list_page(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    other = _second_player_in_world(seed.world_a.id)
    _add_sheet(seed.world_a.id, did, seed.player_a.id, name="Mine")
    _add_sheet(seed.world_a.id, did, other.id, name="TheirsNotMine")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages/sheets")
    assert r.status_code == 200
    assert "Mine" in r.text
    assert "TheirsNotMine" not in r.text


def test_gm_sees_every_sheet_grouped_by_owner(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    other = _second_player_in_world(seed.world_a.id)
    _add_sheet(seed.world_a.id, did, seed.player_a.id, name="Mine")
    _add_sheet(seed.world_a.id, did, other.id, name="TheirsNotMine")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/pages/sheets")
    assert r.status_code == 200
    assert "Mine" in r.text
    assert "TheirsNotMine" in r.text


# ── PlayerCharacter integration panel ───────────────────────────────────

def test_pc_detail_page_lists_linked_sheet(client, seed):
    did = _add_doc(seed.world_a.id, is_character_sheet_template=True, visible_to_players=True)
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    _add_sheet(seed.world_a.id, did, seed.player_a.id, name="Rooks Sheet", player_character_id=pc_id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc_id}")
    assert r.status_code == 200
    assert "Rooks Sheet" in r.text


def test_pc_detail_page_hides_panel_when_no_linked_sheet(client, seed):
    """The panel title is "🧬 Character Sheets" (distinct from the nav's
    "🧬 My Character Sheets" link, which is always present)."""
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id, name="Rook")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc_id}")
    assert r.status_code == 200
    assert "🧬 Character Sheets<" not in r.text


# ── Migration heal ───────────────────────────────────────────────────────

def test_migration_heals_missing_is_character_sheet_template_column(tmp_path):
    """A pre-existing install's page_docs table won't have this column —
    _heal_table_from_model (wired into _migrate()'s heal-tuple for
    "page_docs") must add it, default False, without touching existing
    rows. Mirrors tests/test_heal_table.py's direct-heal-call pattern."""
    import sqlite3

    from sqlalchemy import create_engine

    from app import database as database_module

    db_path = tmp_path / "heal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE page_docs (
            id INTEGER PRIMARY KEY, world_id INTEGER, album_id INTEGER,
            name TEXT, description TEXT, file_url TEXT,
            visible_to_players BOOLEAN, created_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO page_docs (id, world_id, name, file_url, visible_to_players) VALUES (1, 1, 'Old', '/x.html', 1)"
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        database_module._heal_table_from_model(conn, "page_docs")
    engine.dispose()

    raw = sqlite3.connect(str(db_path))
    cols = {row[1] for row in raw.execute("PRAGMA table_info(page_docs)")}
    assert "is_character_sheet_template" in cols
    val = raw.execute("SELECT is_character_sheet_template FROM page_docs WHERE id=1").fetchone()[0]
    assert not val
    raw.close()
