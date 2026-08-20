"""Tests for the shared formatting toolbar's image-insert button: the two
upload endpoints it posts to (GM-only /api/upload-image, player-safe
/api/characters/upload-image) and the entity-note rendering change that
makes ![]() markdown in a note actually render as an <img>."""
import io

from app.database import SessionLocal
from app.models import Entity, EntityNote, PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


def _png_file(name="pic.png"):
    return {"file": (name, io.BytesIO(_PNG_BYTES), "image/png")}


def test_gm_upload_image_returns_url(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-image", files=_png_file())
    assert r.status_code == 200
    assert r.json()["url"].startswith("/uploads/")


def test_player_cannot_use_gm_upload_image(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/upload-image", files=_png_file())
    assert r.status_code == 403


def test_upload_image_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-image", files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_player_can_use_character_upload_image_for_own_notes(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/characters/upload-image", files=_png_file())
    assert r.status_code == 200
    assert r.json()["url"].startswith("/uploads/portraits/")


def test_gm_can_also_use_character_upload_image(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/characters/upload-image", files=_png_file())
    assert r.status_code == 200


def test_toolbar_js_supports_drag_and_drop_image_upload(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    assert r.status_code == 200
    js = r.text
    assert "function ndFmtSetupDragDrop(ta)" in js
    assert 'ta.addEventListener("drop"' in js
    assert 'ta.addEventListener("dragover"' in js
    assert "ndFmtSetupDragDrop(ta);" in js


def test_toolbar_js_drag_drop_ignores_non_file_drags(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    js = r.text
    fn_start = js.index("function ndFmtHasFiles")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "Files" in fn_body
    assert "ndFmtHasFiles(e.dataTransfer)" in js


def test_toolbar_js_page_wide_drop_guard_excludes_file_inputs(client, seed):
    """Regression test: the document-wide dragover/drop handler that stops a
    stray drop from navigating the browser away used to preventDefault() on
    *any* file-carrying drop, anywhere on the page — including a drop landing
    directly on a plain <input type=file> like the entity form's portrait
    upload (app/templates/entities/form.html), which has no drag-drop
    handling of its own and relied entirely on the browser's native
    "populate the input" default action. That default action only applies if
    nothing in the event's propagation chain called preventDefault(), so the
    blanket handler silently broke dragging an image onto that input."""
    r = client.get("/static/js/text-format-toolbar.js")
    assert r.status_code == 200
    js = r.text
    assert "function ndFmtIsFileInputTarget(target)" in js
    guard_start = js.index("function ndFmtIsFileInputTarget")
    guard_end = js.index("\n}", guard_start)
    assert 'input[type="file"]' in js[guard_start:guard_end]
    listeners_start = js.index('document.addEventListener("dragover"', guard_end)
    listeners_end = js.index("\n\n", listeners_start)
    listeners_body = js[listeners_start:listeners_end]
    assert "!ndFmtIsFileInputTarget(e.target)" in listeners_body


def test_toolbar_js_shares_upload_logic_between_click_and_drop(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    js = r.text
    assert "async function ndFmtUploadOneImage(" in js
    insert_fn_start = js.index("function ndFmtInsertImage")
    insert_fn_end = js.index("\n}", insert_fn_start)
    assert "ndFmtUploadOneImage(" in js[insert_fn_start:insert_fn_end]
    drop_fn_start = js.index("async function ndFmtHandleDroppedFiles")
    drop_fn_end = js.index("\n}", drop_fn_start)
    assert "ndFmtUploadOneImage(" in js[drop_fn_start:drop_fn_end]


def test_entity_note_content_renders_markdown_image(client, seed):
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="character", name="NPC With Note", visible_to_players=True)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        note = EntityNote(entity_id=ent.id, content="See attached: ![a clue](/uploads/clue.png)", visible_to_players=True)
        db.add(note)
        db.commit()
        ent_id = ent.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{ent_id}")
    assert r.status_code == 200
    assert '<img src="/uploads/clue.png"' in r.text
    assert "![a clue]" not in r.text
