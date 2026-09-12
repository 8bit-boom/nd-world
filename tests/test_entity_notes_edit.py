"""Tests for POST /entity/{id}/notes/{note_id}/edit (main.py) — reopening a
saved EntityNote for editing. Added so a GM can bring an already-embedded
`![]()` image reference back into the data-fmt textarea and use the
Resize toolbar control on it (see test_media_resize.py for the "size:NN"
title-marker rendering this unlocks); before this route existed, a saved
note only offered Un-hide/Delete, so an oversized image already in a note
could never be shrunk.
"""
from app.database import SessionLocal
from app.models import Entity, EntityNote

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


def _make_note(entity_id, content="Original text", visible=False):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content, visible_to_players=visible)
        db.add(n)
        db.commit()
        db.refresh(n)
        return n.id
    finally:
        db.close()


def _get_note(note_id):
    db = SessionLocal()
    try:
        return db.get(EntityNote, note_id)
    finally:
        db.close()


def _edit(client, entity_id, note_id, **form):
    return client.post(f"/entity/{entity_id}/notes/{note_id}/edit", data=form, follow_redirects=False)


def test_gm_can_edit_note_content(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="Old content")

    r = _edit(client, entity_id, note_id, content="New content")
    assert r.status_code == 303
    assert r.headers["location"] == f"/entity/{entity_id}"
    note = _get_note(note_id)
    assert note.content == "New content"


def test_edit_can_resize_an_embedded_image_reference(client, seed):
    """The actual motivating case: an oversized image already in a note
    gets a size:NN title marker added via the same edit route the Resize
    toolbar button posts through."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id, name="Neon Bazaar")
    note_id = _make_note(entity_id, content="![Neon Bazaar](/uploads/portrait.png)")

    r = _edit(client, entity_id, note_id, content='![Neon Bazaar](/uploads/portrait.png "size:40")')
    assert r.status_code == 303
    note = _get_note(note_id)
    assert note.content == '![Neon Bazaar](/uploads/portrait.png "size:40")'

    detail = client.get(f"/entity/{entity_id}")
    assert detail.status_code == 200
    assert 'style="width:40%"' in detail.text


def test_edit_updates_visibility_checkbox(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="hi", visible=False)

    r = _edit(client, entity_id, note_id, content="hi", visible="1")
    assert r.status_code == 303
    assert _get_note(note_id).visible_to_players is True

    # Omitting the checkbox on a later edit clears it again, same as /new.
    r2 = _edit(client, entity_id, note_id, content="hi")
    assert r2.status_code == 303
    assert _get_note(note_id).visible_to_players is False


def test_edit_blank_content_leaves_existing_content_unchanged(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="Keep me")

    r = _edit(client, entity_id, note_id, content="   ")
    assert r.status_code == 303
    assert _get_note(note_id).content == "Keep me"


def test_edit_preserves_content_is_html_flag(client, seed):
    """The edit form's textarea edits raw stored content either way — it
    doesn't re-decide markdown vs. HTML, so an HTML-preserved import stays
    content_is_html after a plain-text edit."""
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="location", name="Loc")
        db.add(e)
        db.commit()
        db.refresh(e)
        n = EntityNote(entity_id=e.id, content="<h2>Old</h2>", content_is_html=True, visible_to_players=False)
        db.add(n)
        db.commit()
        entity_id, note_id = e.id, n.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _edit(client, entity_id, note_id, content="<h2>New</h2>")
    assert r.status_code == 303
    note = _get_note(note_id)
    assert note.content == "<h2>New</h2>"
    assert note.content_is_html is True


def test_edit_nonexistent_note_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = _edit(client, entity_id, 999999, content="hi")
    assert r.status_code == 404


def test_edit_note_belonging_to_different_entity_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_a = _make_entity(seed.world_a.id, name="A")
    entity_b = _make_entity(seed.world_a.id, name="B")
    note_id = _make_note(entity_a, content="belongs to A")

    r = _edit(client, entity_b, note_id, content="hijacked")
    assert r.status_code == 404
    assert _get_note(note_id).content == "belongs to A"


def test_edit_player_forbidden(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="hi")

    r = _edit(client, entity_id, note_id, content="hijacked")
    assert r.status_code == 403  # auth_gate middleware: not GM, route isn't in _is_player_safe
    assert _get_note(note_id).content == "hi"


def test_edit_anonymous_forbidden(client, seed):
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="hi")
    r = _edit(client, entity_id, note_id, content="hijacked")
    assert r.status_code == 303  # auth_gate middleware: no session, redirected to /login
    assert _get_note(note_id).content == "hi"


# ── UI: the Edit button/form only render for the GM ────────────────────────

def test_note_edit_form_present_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    note_id = _make_note(entity_id, content="hi")

    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    assert f'id="en-edit-{note_id}"' in r.text
    assert f"toggleNoteEdit({note_id})" in r.text
    assert f'/entity/{entity_id}/notes/{note_id}/edit' in r.text


# ── UI: drag-corner resize handles (GM only) ────────────────────────────────

def test_image_resize_handle_script_present_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    _make_note(entity_id, content="![pic](/uploads/x.png)")

    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    assert "en-img-handle" in r.text
    assert "Drag to resize" in r.text
    assert "pointerdown" in r.text


def test_image_resize_handle_script_absent_for_player(client, seed):
    entity_id = _make_entity(seed.world_a.id)
    db = SessionLocal()
    try:
        e = db.get(Entity, entity_id)
        e.visible_to_players = True
        db.commit()
    finally:
        db.close()
    _make_note(entity_id, content="![pic](/uploads/x.png)", visible=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    # The CSS rule is always present (harmless with no handles to style),
    # but the script that actually creates and wires up handle elements —
    # gated by can_edit(request) — must not render for a player.
    assert "Drag to resize" not in r.text
    assert "pointerdown" not in r.text


def test_html_preserved_note_content_flagged_to_skip_resize_handles(client, seed):
    """content_is_html notes render real <img> tags, not markdown ![]()
    references — the size:NN rewrite scheme can't target them, so the
    drag-resize script must skip any note flagged data-html-content."""
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="location", name="Loc")
        db.add(e)
        db.commit()
        db.refresh(e)
        n = EntityNote(entity_id=e.id, content='<p><img src="/uploads/x.png"></p>', content_is_html=True)
        db.add(n)
        db.commit()
        entity_id = e.id
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    assert 'data-html-content="1"' in r.text
