"""Tests for the shared formatting toolbar's "Import .md file" button and its
drag-and-drop equivalent: both load a local markdown file's text straight
into any data-fmt textarea (EntityNote content, Entity body/"Notes", World
rules, etc.) via FileReader — no server round trip, unlike the image-insert
button/drop path covered in test_inline_images.py. The actual FileReader/
confirm/replace logic lives in one shared helper, ndFmtLoadMdFileIntoTextarea,
called by both the button's file-picker handler (ndFmtImportMdFile) and the
drop handler (ndFmtHandleDroppedFiles)."""


def _fn_body(js, name, next_name="function "):
    start = js.index(f"function {name}")
    end = js.index(f"\n{next_name}", start + 1)
    return js[start:end]


def test_toolbar_js_has_import_md_button(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    assert r.status_code == 200
    js = r.text
    assert "function ndFmtImportMdFile(ta, btn)" in js
    assert 'ndFmtButton("📄 Import .md"' in js
    assert "ndFmtImportMdFile(ta, importBtn)" in js


def test_import_md_button_accepts_markdown_and_text_files(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtImportMdFile")
    assert ".md" in fn
    assert "text/markdown" in fn
    assert "text/plain" in fn


def test_import_md_button_reaches_every_data_fmt_textarea_via_shared_toolbar(client, seed):
    """data-fmt is generic — EntityNote's add-note textarea (see
    entities/detail.html) and Entity's body/"Notes" field (see
    entities/form.html) already carry data-fmt, so wiring this button into
    ndFmtBuildToolbar (the function every data-fmt textarea gets a toolbar
    from) reaches both with zero per-template changes."""
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtBuildToolbar")
    assert "ndFmtImportMdFile(ta, importBtn)" in fn


def test_load_md_file_reads_file_client_side_only_no_upload(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtLoadMdFileIntoTextarea")
    assert "new FileReader()" in fn
    assert "readAsText(file)" in fn
    assert "fetch(" not in fn  # unlike ndFmtUploadOneImage, nothing is ever uploaded


def test_load_md_file_confirms_before_replacing_nonempty_content(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtLoadMdFileIntoTextarea")
    assert "confirm(" in fn
    assert "ta.value.trim()" in fn


def test_load_md_file_dispatches_input_event_so_listeners_stay_in_sync(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtLoadMdFileIntoTextarea")
    assert 'new Event("input", { bubbles: true })' in fn


def test_import_md_button_shares_the_load_helper_with_drag_and_drop(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    js = r.text
    btn_fn = _fn_body(js, "ndFmtImportMdFile")
    assert "ndFmtLoadMdFileIntoTextarea(" in btn_fn
    drop_fn = _fn_body(js, "ndFmtHandleDroppedFiles")
    assert "ndFmtLoadMdFileIntoTextarea(" in drop_fn


def test_dropped_markdown_file_is_recognized_by_extension_or_mime_type(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtIsMarkdownFile")
    assert '.endsWith(".md")' in fn
    assert '.endsWith(".markdown")' in fn
    assert '.endsWith(".txt")' in fn
    assert '"text/markdown"' in fn
    assert '"text/plain"' in fn


def test_dropped_markdown_file_takes_priority_over_images_in_the_same_drop(client, seed):
    """A drop mixing a .md file with image files is ambiguous (import vs.
    insert) — the handler must resolve it by treating the whole drop as an
    import and returning before it ever reaches the image-upload branch,
    rather than doing both."""
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtHandleDroppedFiles")
    md_branch = fn[fn.index("mdFile"):]
    assert "return" in md_branch.split("ndFmtUploadOneImage")[0]


def test_entity_note_textarea_has_data_fmt(client, seed):
    from .conftest import GM_PASSWORD, login
    from app.database import SessionLocal
    from app.models import Entity

    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Note Target")
        db.add(e)
        db.commit()
        db.refresh(e)
        eid = e.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert '<textarea name="content" rows="2" placeholder="Add a note about this entity…" required data-fmt>' in r.text


def test_entity_body_notes_field_has_data_fmt(client, seed):
    from .conftest import GM_PASSWORD, login

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/new?kind=character")
    assert r.status_code == 200
    assert 'id="body-field"' in r.text
    assert 'data-fmt' in r.text.split('id="body-field"')[1].split(">")[0]
