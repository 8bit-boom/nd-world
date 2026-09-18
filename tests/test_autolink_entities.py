"""Tests for automatic entity-name linking: a name belonging to another
entity that shows up in an entity's body or notes becomes a clickable link
to that entity, without the GM having to hand-write [Name](/entity/id)
every time. See rendering.autolink_entities (the pure text-rewriting
function) and app.main's detail() route (the viewer-visibility-aware
wiring — see _autolink_name_map).
"""
from app.database import SessionLocal
from app.models import Entity, EntityNote, World, WorldMembership

from app.rendering import autolink_entities, derive_name_variants, render_md

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


def _make_assistant(seed, player):
    """Flip `player`'s membership in world_a to role="assistant" — same
    helper shape as test_gm_assistant.py's own. An assistant may edit world
    content but its VISIBILITY stays keyed on is_gm (False for an
    assistant), so it should see exactly what a regular player sees here —
    never a hidden entity's autolink just because it also has edit rights."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "assistant"
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


def test_player_gets_autolink_to_hidden_entity_specifically_shared_with_them(client, seed):
    # A hidden entity a GM has shared with one specific player (Entity's own
    # per-player access grant, entity_player_access — distinct from the
    # world-wide visible_to_players flag) must still autolink for that
    # player, same as any other entity they can actually open.
    from app.models import entity_player_access

    secret_id = _add_entity(seed.world_a.id, name="Secret Contact", visible_to_players=False)
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=secret_id, user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()
    loc_id = _add_entity(seed.world_a.id, name="Safehouse", kind="location",
                          body="Secret Contact meets you here.", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'<a href="/entity/{secret_id}" class="auto-entity-link">Secret Contact</a>' in r.text


def test_assistant_does_not_get_autolink_to_hidden_entity(client, seed):
    # A GM-Assistant (WorldMembership.role == "assistant") may edit world
    # content, but is_gm is still False for them — visibility filtering
    # (including autolinking) must treat them exactly like a regular
    # player, never leaking a hidden entity's existence just because they
    # also happen to have content-editing rights.
    _make_assistant(seed, seed.player_a)
    secret_id = _add_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    loc_id = _add_entity(seed.world_a.id, name="Dark Tower", kind="location",
                          body="Secret Villain lives here.", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'href="/entity/{secret_id}"' not in r.text
    assert "auto-entity-link" not in r.text


def test_assistant_gets_autolink_to_entity_specifically_shared_with_them(client, seed):
    # The flip side: an assistant IS a player for visibility purposes, so a
    # hidden entity specifically shared with them (entity_player_access)
    # must still autolink, same as for any other player with that grant.
    from app.models import entity_player_access

    _make_assistant(seed, seed.player_a)
    secret_id = _add_entity(seed.world_a.id, name="Secret Contact", visible_to_players=False)
    db = SessionLocal()
    try:
        db.execute(entity_player_access.insert().values(entity_id=secret_id, user_id=seed.player_a.id))
        db.commit()
    finally:
        db.close()
    loc_id = _add_entity(seed.world_a.id, name="Safehouse", kind="location",
                          body="Secret Contact meets you here.", visible_to_players=True)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'<a href="/entity/{secret_id}" class="auto-entity-link">Secret Contact</a>' in r.text


def test_hover_preview_404s_for_hidden_entity_not_shared_with_player(client, seed):
    # If a GM hand-writes a markdown link straight to a hidden entity's id
    # (bypassing autolinking entirely — [Foo](/entity/999) works regardless
    # of what generated the href), the hover-preview endpoint every
    # /entity/ link on the page wires up (base.html) must still 404 for a
    # player without access — the same _entity_view_gate the full detail
    # page itself uses, so a hand-typed or copy-pasted link can't leak a
    # name/summary/image through the preview popup either.
    secret_id = _add_entity(seed.world_a.id, name="Secret Villain", visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/entity/{secret_id}/preview")
    assert r.status_code == 404


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


# ── Entity.aliases — short/alternate names also feed autolinking ───────────

def test_autolink_matches_alias_when_full_name_is_not_used(client, seed):
    # A GM-authored table/prose commonly refers to a character by a short
    # form ("Edmund Vosk") even though the entity's registered name carries
    # a title and epithet ("Hunter Edmund Vosk, the Greyfather") — before
    # aliases existed, only the exact full name ever matched.
    vosk_id = _add_entity(
        seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather",
        aliases="Edmund Vosk, Vosk", visible_to_players=True,
    )
    loc_id = _add_entity(seed.world_a.id, name="Hunter's Hall", kind="location",
                          body="Edmund Vosk runs the place.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'<a href="/entity/{vosk_id}" class="auto-entity-link">Edmund Vosk</a>' in r.text


def test_autolink_still_matches_full_name_alongside_aliases(client, seed):
    vosk_id = _add_entity(
        seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather",
        aliases="Edmund Vosk, Vosk", visible_to_players=True,
    )
    loc_id = _add_entity(
        seed.world_a.id, name="Hunter's Hall", kind="location",
        body="Hunter Edmund Vosk, the Greyfather runs the place.", visible_to_players=True,
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert f'<a href="/entity/{vosk_id}" class="auto-entity-link">Hunter Edmund Vosk, the Greyfather</a>' in r.text


def test_player_does_not_get_autolink_via_alias_of_hidden_entity(client, seed):
    secret_id = _add_entity(
        seed.world_a.id, name="Secret Villain", aliases="the Whisperer", visible_to_players=False,
    )
    loc_id = _add_entity(seed.world_a.id, name="Dark Tower", kind="location",
                          body="the Whisperer lives here.", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'href="/entity/{secret_id}"' not in r.text
    assert "auto-entity-link" not in r.text


def test_entity_form_renders_and_saves_aliases(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/new",
        data={"kind": "character", "name": "Test NPC", "aliases": "Testy, the Test"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        e = db.query(Entity).filter(Entity.name == "Test NPC").first()
        assert e.aliases == "Testy, the Test"
        eid = e.id
    finally:
        db.close()

    r = client.get(f"/entity/{eid}/edit")
    assert r.status_code == 200
    assert 'name="aliases"' in r.text
    assert 'value="Testy, the Test"' in r.text


# ── rendering.derive_name_variants — pure function ──────────────────────────

def test_derive_variants_strips_epithet_and_leading_title():
    assert derive_name_variants("Hunter Edmund Vosk, the Greyfather") == \
        ["Hunter Edmund Vosk", "Edmund Vosk"]


def test_derive_variants_strips_leading_title_with_no_epithet():
    assert derive_name_variants("Hunter Mara Vell") == ["Mara Vell"]


def test_derive_variants_handles_multi_word_title():
    assert derive_name_variants("Senior Resident Hunter Edmund Vosk") == ["Edmund Vosk"]


def test_derive_variants_rejects_lowercase_connector_words():
    # "in"/"of the" etc. failing the all-capitalized check is what keeps
    # this from generating "in Yellow" or "the Yard" as bogus aliases.
    assert derive_name_variants("The King in Yellow") == []
    assert derive_name_variants("Chief Inspector of the Yard") == []


def test_derive_variants_empty_for_short_plain_names():
    assert derive_name_variants("Bob Smith") == []
    assert derive_name_variants("Bob") == []
    assert derive_name_variants("") == []
    assert derive_name_variants(None) == []


# ── Automatic derivation wired into autolinking (no alias needed) ─────────

def test_autolink_derives_short_form_automatically_with_no_alias_set(client, seed):
    # This is the exact scenario reported: a full name carrying a title and
    # epithet must still autolink from its short form even when the GM set
    # no aliases at all.
    vosk_id = _add_entity(seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="Hunter's Hall", kind="location",
                          body="Edmund Vosk runs the place.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'<a href="/entity/{vosk_id}" class="auto-entity-link">Edmund Vosk</a>' in r.text


def test_player_does_not_get_autolink_via_derived_variant_of_hidden_entity(client, seed):
    secret_id = _add_entity(seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather", visible_to_players=False)
    loc_id = _add_entity(seed.world_a.id, name="Hunter's Hall", kind="location",
                          body="Edmund Vosk runs the place.", visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert r.status_code == 200
    assert f'href="/entity/{secret_id}"' not in r.text
    assert "auto-entity-link" not in r.text


def test_explicit_name_wins_over_another_entitys_derived_variant(client, seed):
    # "Edmund Vosk" would be derived from "Hunter Edmund Vosk, the
    # Greyfather" — but if a DIFFERENT entity is literally named
    # "Edmund Vosk", that real entity must win, not the guess.
    vosk_title_id = _add_entity(seed.world_a.id, name="Hunter Edmund Vosk, the Greyfather", visible_to_players=True)
    real_vosk_id = _add_entity(seed.world_a.id, name="Edmund Vosk", visible_to_players=True)
    loc_id = _add_entity(seed.world_a.id, name="Hunter's Hall", kind="location",
                          body="Edmund Vosk runs the place.", visible_to_players=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{loc_id}")
    assert f'<a href="/entity/{real_vosk_id}" class="auto-entity-link">Edmund Vosk</a>' in r.text
    assert f'href="/entity/{vosk_title_id}"' not in r.text
