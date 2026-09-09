"""Tests for the shared formatting toolbar's generalized media button and
image resizing: app/rendering.py's _transform_media_tags (a resized image's
"size:NN" title marker becomes a width style; an "audio"/"video" title
marker becomes a real <audio>/<video> tag), the new /api/upload-media +
/api/characters/upload-media endpoints (image/audio/video, superset of the
existing image-only upload routes), and the toolbar JS's generalized
insert/drag-drop/paste/resize logic.
"""
import io

from app.database import SessionLocal
from app.models import Entity, EntityNote
from app.rendering import render_md, strip_md

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000


def _file(name, content_type, size=1000):
    return {"file": (name, io.BytesIO(b"\x00" * size), content_type)}


def _toolbar_js(client):
    r = client.get("/static/js/text-format-toolbar.js")
    assert r.status_code == 200
    return r.text.replace("\r\n", "\n")


# ── app.rendering._transform_media_tags ──────────────────────────────────

def test_render_md_resizes_image_via_size_title():
    html = render_md('![a tavern](/uploads/x.png "size:50")')
    assert 'style="width:50%"' in html
    assert "title=" not in html


def test_render_md_clamps_size_below_minimum():
    html = render_md('![a](/uploads/x.png "size:1")')
    assert 'style="width:10%"' in html


def test_render_md_clamps_size_above_maximum():
    html = render_md('![a](/uploads/x.png "size:999")')
    assert 'style="width:300%"' in html


def test_render_md_default_size_leaves_image_plain():
    html = render_md('![a](/uploads/x.png)')
    assert "<img" in html
    assert "style=" not in html
    assert "title=" not in html


def test_render_md_genuine_title_is_left_untouched():
    html = render_md('![a](/uploads/x.png "A real caption")')
    assert 'title="A real caption"' in html
    assert "<audio" not in html
    assert "style=" not in html


def test_render_md_audio_title_becomes_audio_tag():
    html = render_md('![clip](/uploads/x.mp3 "audio")')
    assert "<audio controls" in html
    assert 'src="/uploads/x.mp3"' in html
    assert "<img" not in html


def test_render_md_video_title_becomes_video_tag():
    html = render_md('![clip](/uploads/x.mp4 "video")')
    assert "<video controls" in html
    assert 'src="/uploads/x.mp4"' in html
    assert "<img" not in html


def test_strip_md_strips_sized_and_av_references_from_summaries():
    assert strip_md('See: ![a](/uploads/x.png "size:50") done') == "See: done"
    assert strip_md('Listen: ![clip](/uploads/x.mp3 "audio") done') == "Listen: done"


def test_entity_note_renders_resized_image(client, seed):
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="character", name="Resize Target", visible_to_players=True)
        db.add(ent)
        db.commit()
        db.refresh(ent)
        note = EntityNote(entity_id=ent.id, content='![a clue](/uploads/clue.png "size:50")', visible_to_players=True)
        db.add(note)
        db.commit()
        ent_id = ent.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{ent_id}")
    assert r.status_code == 200
    assert 'style="width:50%"' in r.text


# ── /api/upload-media + /api/characters/upload-media ─────────────────────

def test_gm_upload_media_accepts_image(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-media", files={"file": ("pic.png", io.BytesIO(_PNG_BYTES), "image/png")})
    assert r.status_code == 200
    data = r.json()
    assert data["url"].startswith("/uploads/")
    assert data["kind"] == "image"


def test_gm_upload_media_accepts_audio(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-media", files=_file("clip.mp3", "audio/mpeg"))
    assert r.status_code == 200
    data = r.json()
    assert data["url"].startswith("/uploads/")
    assert data["url"].endswith(".mp3")
    assert data["kind"] == "audio"


def test_gm_upload_media_accepts_video(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-media", files=_file("clip.mp4", "video/mp4"))
    assert r.status_code == 200
    data = r.json()
    assert data["url"].startswith("/uploads/")
    assert data["url"].endswith(".mp4")
    assert data["kind"] == "video"


def test_gm_upload_media_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-media", files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_gm_upload_media_rejects_desktop_only_video_container(client, seed):
    """.mkv/.avi are accepted by the Video Library (which transcodes them),
    but an inline attachment has no transcode step — accepting one would
    save a file that never plays in the <video> tag it's embedded in."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/upload-media", files=_file("clip.mkv", "video/x-matroska"))
    assert r.status_code == 400


def test_player_cannot_use_gm_upload_media(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/upload-media", files=_file("clip.mp3", "audio/mpeg"))
    assert r.status_code == 403


def test_player_can_use_character_upload_media_for_own_notes(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/characters/upload-media", files=_file("clip.mp4", "video/mp4"))
    assert r.status_code == 200
    assert r.json()["kind"] == "video"


def test_gm_can_also_use_character_upload_media(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/characters/upload-media", files=_file("clip.mp3", "audio/mpeg"))
    assert r.status_code == 200


# ── Toolbar JS: generalized insert/drag-drop/paste/resize ────────────────

def test_toolbar_js_file_picker_accepts_image_audio_video(client, seed):
    js = _toolbar_js(client)
    fn_start = js.index("function ndFmtInsertImage")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert 'input.accept = "image/*,audio/*,video/*"' in fn_body


def test_toolbar_js_drag_drop_accepts_audio_and_video(client, seed):
    js = _toolbar_js(client)
    fn_start = js.index("async function ndFmtHandleDroppedFiles")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "audio" in fn_body
    assert "video" in fn_body
    assert "ndFmtUploadOneImage(" in fn_body


def test_toolbar_js_has_paste_handler(client, seed):
    js = _toolbar_js(client)
    assert "function ndFmtSetupPaste(ta)" in js
    fn_start = js.index("function ndFmtSetupPaste")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert 'addEventListener("paste"' in fn_body
    assert "clipboardData" in fn_body
    assert "ndFmtUploadOneImage(" in fn_body
    # Wired into every data-fmt textarea alongside drag-drop.
    init_start = js.index("function ndFmtInit")
    init_end = js.index("\n}", init_start)
    assert "ndFmtSetupPaste(ta)" in js[init_start:init_end]


def test_toolbar_js_paste_only_intercepts_actual_files(client, seed):
    """A plain text paste must not be swallowed — preventDefault() only
    happens once a matching image/audio/video File is actually found."""
    js = _toolbar_js(client)
    fn_start = js.index("function ndFmtSetupPaste")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "if (!files.length) return;" in fn_body
    assert fn_body.index("if (!files.length) return;") < fn_body.index("e.preventDefault();")


def test_toolbar_js_upload_uses_media_endpoint_and_tags_av_kind(client, seed):
    js = _toolbar_js(client)
    fn_start = js.index("async function ndFmtUploadOneImage")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert '"/api/upload-media"' in fn_body
    assert "fmtUploadMedia" in fn_body
    assert '"audio"' in fn_body
    assert '"video"' in fn_body


def test_toolbar_js_has_resize_button_and_size_options(client, seed):
    js = _toolbar_js(client)
    assert "NDFMT_RESIZE_PCTS" in js
    assert "function ndFmtSetImageSize(" in js
    assert "function ndFmtFindImageRefAtCursor(" in js
    build_start = js.index("function ndFmtBuildToolbar")
    build_end = js.index("\n}", build_start)
    build_body = js[build_start:build_end]
    assert "fmt-popup" in build_body
    assert "ndFmtSetImageSize(ta, pct)" in build_body


def test_toolbar_js_resize_rejects_audio_video_and_defaults_to_100_percent_clears_marker(client, seed):
    js = _toolbar_js(client)
    fn_start = js.index("function ndFmtSetImageSize")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "NDFMT_AV_TITLES.has(parsed.title)" in fn_body
    assert "pct === 100" in fn_body


def test_toolbar_js_render_inline_supports_title_marker_for_board_preview(client, seed):
    """ndFmtRenderInline has no server round trip (investigation board node
    body), so it needs its own client-side mirror of the same size/audio/
    video title-marker logic render_md applies server-side."""
    js = _toolbar_js(client)
    fn_start = js.index("function ndFmtRenderInline")
    fn_end = js.index("\n}", fn_start)
    fn_body = js[fn_start:fn_end]
    assert "NDFMT_SIZE_TITLE_RE" in fn_body
    assert '"audio"' in fn_body
    assert '"video"' in fn_body
