"""Tests for the Private Notes sidebar TOC + search (app/main.py's
private_notes_view / _note_toc_label, app/templates/private_notes.html) —
the same "navigate by content" idea as the Rules page's TOC (rules.html),
adapted for a flat list of notes rather than one continuous document: each
note becomes one sidebar entry (labeled by its title, or a derived label
for an untitled note) instead of a markdown heading.
"""
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import PrivateNote

from .conftest import GM_PASSWORD, login


def _add_note(world_id, player_user_id, **kw):
    db = SessionLocal()
    try:
        n = PrivateNote(world_id=world_id, player_user_id=player_user_id,
                         title=kw.pop("title", ""), content=kw.pop("content", "Note body"), **kw)
        db.add(n)
        db.commit()
        db.refresh(n)
        return n.id
    finally:
        db.close()


def test_toc_lists_titled_notes_by_title(client, seed):
    _add_note(seed.world_a.id, seed.player_a.id, title="Session 14 hook", content="Some content")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    assert 'class="toc-entry"' in r.text
    assert ">Session 14 hook<" in r.text


def test_toc_derives_label_for_untitled_note_from_first_content_line(client, seed):
    _add_note(seed.world_a.id, seed.player_a.id, title="", content="## The docks are watched\nMore detail here.")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    # Leading markdown heading markers are stripped from the derived label.
    assert ">The docks are watched<" in r.text


def test_toc_falls_back_to_untitled_note_label_for_blank_content_line(client, seed):
    _add_note(seed.world_a.id, seed.player_a.id, title="", content="   \n\n   ")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    assert ">Untitled note<" in r.text


def test_toc_entry_links_to_note_anchor(client, seed):
    nid = _add_note(seed.world_a.id, seed.player_a.id, title="Hook", content="x")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert f'href="#note-{nid}"' in r.text
    assert f'id="note-{nid}"' in r.text


def test_no_sidebar_when_zero_notes(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    assert "No notes yet." in r.text
    assert 'id="pn-search"' not in r.text
    assert 'class="toc-entry"' not in r.text


def test_search_input_and_count_line_present_when_notes_exist(client, seed):
    _add_note(seed.world_a.id, seed.player_a.id, title="Hook", content="x")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert 'id="pn-search"' in r.text
    assert 'id="pn-search-count"' in r.text


def test_toc_order_matches_note_display_order(client, seed):
    now = datetime.utcnow()
    _add_note(seed.world_a.id, seed.player_a.id, title="First", content="a", created_at=now)
    _add_note(seed.world_a.id, seed.player_a.id, title="Second", content="b", created_at=now + timedelta(minutes=1))
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.text.index(">Second<") < r.text.index(">First<"), (
        "TOC order should match the notes list order (newest first, same as PrivateNote.created_at.desc())"
    )
