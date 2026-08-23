"""Tests for POST /entity/{id}/notes/import (main.py) — creating an
EntityNote from an uploaded .md/.txt/.pdf/.html/.htm/image file instead of
typing it, and the two rendering helpers it depends on:
rendering.html_to_markdown (default .html/.htm handling) and
rendering.sanitize_note_html (the "preserve original formatting" mode).
"""
import io

import pytest

from app.database import SessionLocal
from app.models import Entity, EntityNote
from app.rendering import html_to_markdown, sanitize_note_html

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, name="Doomed City"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="location", name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _upload(client, entity_id, filename, data, content_type, **form):
    files = {"file": (filename, io.BytesIO(data), content_type)}
    return client.post(f"/entity/{entity_id}/notes/import", files=files, data=form, follow_redirects=False)


def _last_note(entity_id):
    db = SessionLocal()
    try:
        return db.query(EntityNote).filter(EntityNote.entity_id == entity_id).order_by(EntityNote.id.desc()).first()
    finally:
        db.close()


# ── rendering helpers (unit-level, no HTTP) ─────────────────────────────────

def test_html_to_markdown_converts_formatting():
    out = html_to_markdown("<h1>Title</h1><p>The party met <b>Elena</b>.</p><ul><li>Item one</li></ul>")
    assert "# Title" in out
    assert "**Elena**" in out
    assert "Item one" in out


def test_html_to_markdown_strips_script_and_style_content():
    out = html_to_markdown("<p>safe</p><script>alert(document.cookie)</script><style>body{display:none}</style>")
    assert "safe" in out
    assert "alert" not in out
    assert "display:none" not in out


def test_html_to_markdown_drops_images():
    out = html_to_markdown('<p>text</p><img src="https://evil.example/track.gif">')
    assert "evil.example" not in out


def test_html_to_markdown_empty_input():
    assert html_to_markdown("") == ""
    assert html_to_markdown("   ") == ""


def test_sanitize_note_html_keeps_allowed_formatting():
    out = sanitize_note_html('<h2>Heading</h2><p>The party met <b>Elena</b> at the '
                              '<span style="color:#ff00ff">Neon Bazaar</span>.</p>'
                              '<a href="https://example.com/map">the map</a>')
    assert "<h2>Heading</h2>" in out
    assert "<b>Elena</b>" in out
    assert 'color:#ff00ff' in out
    assert 'href="https://example.com/map"' in out


def test_sanitize_note_html_strips_script_style_and_content():
    out = sanitize_note_html("<p>safe</p><script>alert(1)</script><style>body{display:none}</style>")
    assert "safe" in out
    assert "alert" not in out
    assert "display:none" not in out
    assert "<script" not in out
    assert "<style" not in out


def test_sanitize_note_html_strips_event_handlers_and_dangerous_urls():
    out = sanitize_note_html(
        '<p onclick="alert(1)">click</p>'
        '<a href="javascript:alert(document.cookie)">bad</a>'
        '<a href="data:text/html,evil">bad2</a>'
        '<img src=x onerror=alert(1)>'
        '<svg onload=alert(1)></svg>'
        '<iframe src="evil.com"></iframe>'
    )
    assert "onclick" not in out
    assert "javascript:" not in out
    assert "data:" not in out
    assert "<img" not in out
    assert "<svg" not in out
    assert "<iframe" not in out
    assert "onerror" not in out
    assert "onload" not in out


def test_sanitize_note_html_disallows_dangerous_style_property():
    out = sanitize_note_html('<span style="color:red;background:url(https://evil.example/track.gif)">x</span>')
    assert "color:red" in out
    assert "url(" not in out
    assert "evil.example" not in out


# ── route: format handling ──────────────────────────────────────────────────

def test_import_txt_stored_as_plain_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "log.txt", b"The secret door is behind the waterfall.", "text/plain")
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note.content == "The secret door is behind the waterfall."
    assert note.content_is_html is False


def test_import_md_stored_as_plain_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "log.md", b"# Session 4\n\n**Elena** appeared.", "text/markdown")
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note.content == "# Session 4\n\n**Elena** appeared."
    assert note.content_is_html is False


def test_import_pdf_extracts_text_without_crashing(client, seed, tmp_path):
    from pypdf import PdfWriter

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "handout.pdf", pdf_path.read_bytes(), "application/pdf")
    assert r.status_code == 303
    # A blank page extracts no text, so no note is created — just confirm no crash/500.


def test_import_html_default_converts_to_markdown(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "session.html", b"<h1>Session 4</h1><p>The party met <b>Elena</b>.</p>", "text/html")
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note.content_is_html is False
    assert "# Session 4" in note.content
    assert "**Elena**" in note.content


def test_import_html_preserve_html_stores_sanitized_html(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(
        client, entity_id, "session.html",
        b'<h1>Session 4</h1><p>The party met <b>Elena</b>.</p><script>alert(1)</script>',
        "text/html", preserve_html="1",
    )
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note.content_is_html is True
    assert "<h1>Session 4</h1>" in note.content
    assert "<b>Elena</b>" in note.content
    assert "script" not in note.content
    assert "alert" not in note.content


def test_import_htm_extension_also_accepted(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "session.htm", b"<p>hello</p>", "text/html")
    assert r.status_code == 303
    assert _last_note(entity_id) is not None


def test_import_image_creates_note_with_markdown_image_ref(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id, name="Neon Bazaar")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
    r = _upload(client, entity_id, "handout.png", png, "image/png")
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note is not None
    assert note.content_is_html is False
    assert note.content.startswith("![Neon Bazaar](/uploads/")


def test_import_unsupported_extension_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "malware.exe", b"MZ\x00\x00", "application/octet-stream")
    assert r.status_code == 400


def test_import_oversized_file_rejected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    big = b"a" * (2 * 1024 * 1024)  # over the 1 MiB test MAX_NOTE_IMPORT_BYTES
    r = _upload(client, entity_id, "big.txt", big, "text/plain")
    assert r.status_code == 413
    assert _last_note(entity_id) is None


def test_import_nonexistent_entity_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = _upload(client, 999999, "log.txt", b"hi", "text/plain")
    assert r.status_code == 404


def test_import_visible_checkbox_respected(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "log.txt", b"visible note", "text/plain", visible="1")
    assert r.status_code == 303
    note = _last_note(entity_id)
    assert note.visible_to_players is True


def test_import_empty_extraction_creates_no_note(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "empty.html", b"<script>alert(1)</script>", "text/html")
    assert r.status_code == 303
    assert _last_note(entity_id) is None


# ── permissions: GM-only by default, same as /entity/{id}/notes/new ────────

def test_import_player_forbidden(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "log.txt", b"hi", "text/plain")
    assert r.status_code == 403  # auth_gate middleware: not GM, route isn't in _is_player_safe
    assert _last_note(entity_id) is None


def test_import_anonymous_forbidden(client, seed):
    entity_id = _make_entity(seed.world_a.id)
    r = _upload(client, entity_id, "log.txt", b"hi", "text/plain")
    assert r.status_code == 303  # auth_gate middleware: no session, redirected to /login
    assert _last_note(entity_id) is None
