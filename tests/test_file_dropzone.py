"""Tests for static/js/file-dropzone.js — the shared drag-and-drop wrapper
extending the app's dropzone pattern (previously hand-rolled independently in
map_form.html/maps.html/gallery_album.html) to every remaining bare
<input type=file> across the app. See app/routers/gallery.py-adjacent
templates for the earlier hand-rolled versions this generalizes.
"""
from app.database import SessionLocal
from app.models import PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login


# ── Shared JS source ─────────────────────────────────────────────────────────

def test_file_dropzone_js_core_functions(client, seed):
    r = client.get("/static/js/file-dropzone.js")
    assert r.status_code == 200
    js = r.text
    assert "function ndDropzoneHasFiles(dt)" in js
    assert "function ndDropzoneSetup(zone)" in js
    assert 'document.querySelectorAll("[data-dropzone]")' in js


def test_file_dropzone_js_drop_sets_files_and_dispatches_change(client, seed):
    """The core behavior that makes this a drop-in replacement for a bare
    input's own onchange handler: populate .files then fire a native
    "change" event, so whatever the page already does on change (preview,
    auto-submit) runs unmodified."""
    r = client.get("/static/js/file-dropzone.js")
    js = r.text
    setup_start = js.index("function ndDropzoneSetup")
    setup_end = js.index("\ndocument.addEventListener", setup_start)
    body = js[setup_start:setup_end]
    assert "input.files = e.dataTransfer.files;" in body
    assert 'input.dispatchEvent(new Event("change"' in body
    assert "e.preventDefault();" in body


def test_file_dropzone_js_ignores_non_file_drags(client, seed):
    r = client.get("/static/js/file-dropzone.js")
    js = r.text
    fn_start = js.index("function ndDropzoneHasFiles")
    fn_end = js.index("\n}", fn_start)
    assert "Files" in js[fn_start:fn_end]


# ── Rollout: every touched route gets the wrapper + script include ─────────

def _setup_custom_sheet_pc(world_id):
    """A PC on a sheet_mode="custom" template, whose sheet lives on
    characters/custom_sheet.html (GET /characters/{id}) — same setup shape
    as tests/test_world_export_split.py's _add_sheet_template."""
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=world_id, name="Custom", slug="dropzone-custom",
                             sheet_mode="custom", fields_json="[]")
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        pc = PlayerCharacter(world_id=world_id, name="Custom PC", sheet_template_id=tpl.id)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _setup_standard_pc(world_id):
    """A PC with no sheet template — the standard sheet, characters/form.html
    (GET /characters/{id}/edit)."""
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, name="Standard PC")
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def test_dropzone_rollout_across_every_touched_route(client, seed):
    """Table-driven: every route that gained a dropzone in this sweep must
    render both the data-dropzone wrapper and the shared script include —
    catches a template where either was forgotten."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    custom_pc_id = _setup_custom_sheet_pc(seed.world_a.id)
    standard_pc_id = _setup_standard_pc(seed.world_a.id)

    routes = [
        "/new",                                          # entities/form.html
        "/races/new",                                     # race_new.html
        "/professions/new",                                # profession_new.html
        "/characters/new",                                 # characters/wizard.html
        f"/characters/{custom_pc_id}",                     # characters/custom_sheet.html
        f"/characters/{standard_pc_id}/edit",              # characters/form.html
        f"/worlds/{seed.world_a.id}/home/edit",            # home_edit.html
        f"/worlds/{seed.world_a.id}/rules/edit",           # rules_edit.html
        "/export",                                         # export_hub.html
        "/tables",                                         # tables/list.html
        "/import",                                         # import.html
        "/ai",                                             # ai_chat.html
    ]
    for url in routes:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert "data-dropzone" in r.text, f"{url} missing data-dropzone wrapper"
        assert "/static/js/file-dropzone.js" in r.text, f"{url} missing script include"


def test_import_page_has_three_dropzones(client, seed):
    """import.html has 3 independent file inputs (single JSON, bulk JSON,
    bulk images) — each must get its own wrapper, not just the first."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert r.text.count("data-dropzone") == 3


def test_ai_chat_page_has_three_dropzones(client, seed):
    """ai_chat.html has 3 independent reference-image pickers (img2img,
    ControlNet, IP-Adapter)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert r.text.count("data-dropzone") == 3


def test_schematic_embed_tool_intentionally_not_wrapped(client, seed):
    """schematic.html's embed-image input is deliberately excluded from this
    rollout — the page already has a whole-canvas drop zone that replaces
    the background on drop; wiring the embed input into the same canvas
    would create an ambiguous "replace background vs. embed element"
    conflict that needs a deliberate product decision, not a mechanical
    wrap. Guards against someone reflexively "completing the sweep" here."""
    from app.database import SessionLocal as SL
    from app.models import Schematic
    db = SL()
    try:
        s = Schematic(world_id=seed.world_a.id, name="Dropzone Check", slug="dropzone-check",
                       is_html=False, canvas_width=2000, canvas_height=1500,
                       canvas_bg="dark", elements_json="[]")
        db.add(s)
        db.commit()
        slug = s.slug
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/maps/schematic/{slug}")
    assert r.status_code == 200
    input_start = r.text.index('id="img-input"')
    surrounding = r.text[max(0, input_start - 200):input_start]
    assert "data-dropzone" not in surrounding
