"""Tests for automatic entity-name linking: a name belonging to another
entity that shows up in an entity's body or notes becomes a clickable link
to that entity, without the GM having to hand-write [Name](/entity/id)
every time. See rendering.autolink_entities (the pure text-rewriting
function) and app.main's detail() route (the viewer-visibility-aware
wiring — see _autolink_name_map).
"""
from app.database import SessionLocal
from app.models import Entity, EntityNote, World

from app.rendering import autolink_entities, render_md

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _add_entity(world_id, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kw.pop("kind", "character"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _add_note(entity_id, content, **kw):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content, **kw)
        db.add(n)
        db.commit()
        db.refresh(n)
        return n.id
    finally:
        db.close()


# ── rendering.autolink_entities — pure function ─────────────────────────────

def test_autolink_links_a_single_mention():
    html = render_md("Bob went to the tavern.")
    out = autolink_entities(html, {"Bob": 7})
    assert '<a href="/entity/7" class="auto-entity-link">Bob</a>' in out


def test_autolink_links_multiple_different_entities_in_one_text():
    html = render_md("Bob met Alice at the Rusty Anchor.")
    out = autolink_entities(html, {"Bob": 1, "Alice": 2, "Rusty Anchor": 3})
    assert '<a href="/entity/1" class="auto-entity-link">Bob</a>' in out
    assert '<a href="/entity/2" class="auto-entity-link">Alice</a>' in out
    assert '<a href="/entity/3" class="auto-entity-link">Rusty Anchor</a>' in out


def test_autolink_is_case_insensitive_but_keeps_original_casing():
    html = render_md("BOB and bob and Bob all refer to the same guy.")
    out = autolink_entities(html, {"Bob": 1})
    assert '>BOB</a>' in out
    assert '>bob</a>' in out
    assert '>Bob</a>' in out


def test_autolink_longer_name_wins_over_shorter_overlapping_name():
    html = render_md("Bob Smith is here.")
    out = autolink_entities(html, {"Bob": 1, "Bob Smith": 2})
    assert '<a href="/entity/2" class="auto-entity-link">Bob Smith</a>' in out
    assert 'href="/entity/1"' not in out


def test_autolink_does_not_match_inside_a_longer_word():
    html = render_md("Ashley is not the same as Ash.")
    out = autolink_entities(html, {"Ash": 1})
    assert "Ashley" in out
    assert '<a href="/entity/1" class="auto-entity-link">Ash</a>' in out
    # Ashley itself must not have been split/linked
    assert '<a href="/entity/1" class="auto-entity-link">Ash</a>ley' not in out


def test_autolink_skips_text_already_inside_a_link():
    html = render_md("See [Bob](/entity/9) for details.")
    out = autolink_entities(html, {"Bob": 1})
    assert out.count("<a ") == 1
    assert 'href="/entity/9"' in out
    assert 'href="/entity/1"' not in out


def test_autolink_skips_text_inside_code_spans():
    html = render_md("Use the `Bob` variable name.")
    out = autolink_entities(html, {"Bob": 1})
    assert "<code>Bob</code>" in out
    assert "auto-entity-link" not in out


def test_autolink_ignores_names_shorter_than_3_chars():
    html = render_md("Ok, go.")
    out = autolink_entities(html, {"Ok": 1})
    assert "auto-entity-link" not in out


def test_autolink_no_names_returns_html_unchanged():
    html = render_md("Nothing to link here.")
    assert autolink_entities(html, {}) == html


def test_autolink_no_html_returns_falsy_unchanged():
    assert autolink_entities("", {"Bob": 1}) == ""


# ── Route wiring (app.main.detail) ──────────────────────────────────────────

def test_entity_body_autolinks_mention_of_another_entity(client, seed):
    bob_id = _add_entity(seed.world_a.id, name="Bob", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="The Rusty Anchor", kind="location",
                          body="Bob is the owner here.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'<a href="/entity/{bob_id}" class="auto-entity-link">Bob</a>' in r.text


def test_entity_does_not_autolink_its_own_name(client, seed):
    eid = _add_entity(seed.world_a.id, name="Bob", body="Bob nodded to himself.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "auto-entity-link" not in r.text


def test_autolink_links_more_than_one_entity_at_once(client, seed):
    bob_id = _add_entity(seed.world_a.id, name="Bob", visible_to_players=True)
    alice_id = _add_entity(seed.world_a.id, name="Alice", visible_to_players=True)
    scene_id = _add_entity(
        seed.world_a.id, name="Tavern Scene", kind="event",
        body="Bob and Alice argued all night.", visible_to_players=True,
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{scene_id}")
    assert f'<a href="/entity/{bob_id}" class="auto-entity-link">Bob</a>' in r.text
    assert f'<a href="/entity/{alice_id}" class="auto-entity-link">Alice</a>' in r.text


def test_player_does_not_get_autolink_to_hidden_entity(client, seed):
    secret_id = _add_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    loc_id = _add_entity(seed.world_a.id, name="Dark Tower", kind="location",
                          body="Secret Villain lives here.", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'href="/entity/{secret_id}"' not in r.text
    assert "auto-entity-link" not in r.text


def test_gm_does_get_autolink_to_hidden_entity(client, seed):
    secret_id = _add_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    loc_id = _add_entity(seed.world_a.id, name="Dark Tower", kind="location",
                          body="Secret Villain lives here.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert f'<a href="/entity/{secret_id}" class="auto-entity-link">Secret Villain</a>' in r.text


def test_autolink_does_not_cross_worlds(client, seed):
    _add_entity(seed.world_b.id, name="Bob", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="Some Place", kind="location",
                          body="Bob is not from this world.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert "auto-entity-link" not in r.text


def test_entity_note_autolinks_markdown_note(client, seed):
    bob_id = _add_entity(seed.world_a.id, name="Bob", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="The Rusty Anchor", kind="location", visible_to_players=True)
    _add_note(loc_id, "Bob stops by every evening.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert f'<a href="/entity/{bob_id}" class="auto-entity-link">Bob</a>' in r.text


def test_entity_note_autolinks_imported_html_note(client, seed):
    bob_id = _add_entity(seed.world_a.id, name="Bob", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="The Rusty Anchor", kind="location", visible_to_players=True)
    _add_note(loc_id, "<p>Bob stops by every evening.</p>", content_is_html=True, visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert f'<a href="/entity/{bob_id}" class="auto-entity-link">Bob</a>' in r.text
    assert 'data-html-content="1"' in r.text


# ── Private Notes (GM ↔ one player) — a separate feature from EntityNote,
# same autolinking wiring (app.main.private_notes_view) ───────────────────

def test_private_note_autolinks_mention_of_an_entity(client, seed):
    bob_id = _add_entity(seed.world_a.id, name="Bob", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}/new",
        data={"title": "Hook", "content": "Bob has a job for you."},
        follow_redirects=False,
    )
    assert r.status_code == 303

    view = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert view.status_code == 200
    assert f'<a href="/entity/{bob_id}" class="auto-entity-link">Bob</a>' in view.text


def test_private_note_does_not_autolink_hidden_entity_for_player(client, seed):
    secret_id = _add_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(
        f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}/new",
        data={"title": "", "content": "Secret Villain is watching you."},
        follow_redirects=False,
    )

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    view = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert view.status_code == 200
    assert f'href="/entity/{secret_id}"' not in view.text
    assert "auto-entity-link" not in view.text
