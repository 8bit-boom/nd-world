"""Tests for the [gmonly]...[/gmonly] GM-only content-redaction mechanism —
a genuine access-control boundary, unlike the pre-existing [spoiler] tag
(visible to everyone, just click-to-reveal — see app.rendering's own
comment on _SPOILER_TAG_RE). [gmonly] must:

  (a) never reach a non-GM viewer's rendered page, JS-embedded AI context,
      or downloaded .md at all (server-side text stripping, not CSS/JS
      hiding), and
  (b) never be pulled into a player's RAG context, across both AI Chat
      (app.retrieval.smart_world_context) and Chronicler (the primary
      player-facing lore-Q&A tool).

strip_gm_only() itself lives in app/rendering.py; this file covers that
unit plus every consuming call site.
"""
from app.database import SessionLocal
from app.models import Entity, EntityNote, PrivateNote, World
from app.rendering import render_md, strip_gm_only
from app.retrieval import format_context_from_entities, smart_world_context
from app.routers.chronicler import build_chronicler_system_prompt

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── strip_gm_only() unit behavior ───────────────────────────────────────────

def test_strip_gm_only_removes_tag_and_contents():
    text = "Public intro. [gmonly]The amulet is cursed.[/gmonly] Public outro."
    assert strip_gm_only(text) == "Public intro.  Public outro."


def test_strip_gm_only_is_case_insensitive():
    text = "Before [GMOnly]Secret[/GMONLY] After"
    assert strip_gm_only(text) == "Before  After"


def test_strip_gm_only_removes_multiple_blocks():
    text = "A [gmonly]one[/gmonly] B [gmonly]two[/gmonly] C"
    result = strip_gm_only(text)
    assert "one" not in result
    assert "two" not in result
    assert "A" in result and "B" in result and "C" in result


def test_strip_gm_only_leaves_plain_text_untouched():
    assert strip_gm_only("Nothing hidden here.") == "Nothing hidden here."


def test_strip_gm_only_handles_falsy_input():
    assert strip_gm_only("") == ""
    assert strip_gm_only(None) is None


def test_render_md_shows_gm_only_as_a_labeled_box():
    """render_md itself stays viewer-agnostic (same as [spoiler]) — an
    intact tag renders visibly because, by construction, only a
    GM-authorized call site ever hands render_md text that still has one."""
    html = render_md("Text. [gmonly]The real trigger is the third lever.[/gmonly]")
    assert 'class="gm-only-text"' in html
    assert "The real trigger is the third lever." in html


# ── format_context_from_entities(strip_gm_only=True) ────────────────────────

def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Entity"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_format_context_strips_gm_only_from_body_excerpt(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", summary="A friendly innkeeper.",
        body="He pours drinks and gossips. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], strip_gm_only=True)
        assert "cult spy" not in context
        assert "pours drinks" in context
    finally:
        db.close()


def test_format_context_strips_gm_only_from_summary_line(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        summary="A friendly innkeeper. [gmonly]Secretly a spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], strip_gm_only=True)
        assert "Secretly a spy" not in context
    finally:
        db.close()


def test_format_context_keeps_gm_only_when_not_stripping(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        context = format_context_from_entities([e], strip_gm_only=False)
        assert "cult spy" in context
    finally:
        db.close()


# ── smart_world_context wiring (AI Chat RAG) ────────────────────────────────

def test_smart_world_context_strips_gm_only_for_a_real_player(client, seed):
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks and gossips. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        from app.models import User
        player = db.get(User, seed.player_a.id)
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "innkeeper bram", user=player)
        assert "cult spy" not in context
        assert "pours drinks" in context
    finally:
        db.close()


def test_smart_world_context_keeps_gm_only_for_gm(client, seed):
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks and gossips. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        from app.models import User
        gm = db.get(User, seed.gm.id)
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "innkeeper bram", user=gm)
        assert "cult spy" in context
    finally:
        db.close()


def test_smart_world_context_keeps_gm_only_when_user_is_none(client, seed):
    """user=None is the unfiltered/already-GM-gated posture — Condense/
    Summarize and similar GM-only callers must not lose content either."""
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks and gossips. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        context, _non_notes, _notes = smart_world_context(db, seed.world_a.id, "innkeeper bram", user=None)
        assert "cult spy" in context
    finally:
        db.close()


def test_world_context_player_route_never_leaks_gm_only(client, seed):
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks and gossips. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/world-context-player", json={"query": "innkeeper bram"})
    assert r.status_code == 200
    assert "cult spy" not in r.json()["context"]


# ── Chronicler ───────────────────────────────────────────────────────────────

def test_chronicler_prompt_strips_gm_only_for_player(client, seed):
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        from app.models import User
        player = db.get(User, seed.player_a.id)
        prompt = build_chronicler_system_prompt(db, seed.world_a.id, "innkeeper bram", player)
        assert "cult spy" not in prompt
        assert "pours drinks" in prompt
    finally:
        db.close()


def test_chronicler_prompt_keeps_gm_only_for_gm(client, seed):
    _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        from app.models import User
        gm = db.get(User, seed.gm.id)
        prompt = build_chronicler_system_prompt(db, seed.world_a.id, "innkeeper bram", gm)
        assert "cult spy" in prompt
    finally:
        db.close()


# ── Entity detail page ──────────────────────────────────────────────────────

def test_detail_page_hides_gm_only_body_from_player(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "cult spy" not in r.text
    assert "pours drinks" in r.text
    assert "gm-only-text" not in r.text


def test_detail_page_shows_gm_only_body_to_gm(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "cult spy" in r.text
    assert "gm-only-text" in r.text


def test_detail_page_hides_gm_only_summary_from_player(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        summary="A friendly innkeeper. [gmonly]Secretly a spy.[/gmonly]",
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "Secretly a spy" not in r.text


def _make_note(entity_id, content, visible_to_players=True):
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=entity_id, content=content, visible_to_players=visible_to_players)
        db.add(n)
        db.commit()
        return n.id
    finally:
        db.close()


def test_detail_page_hides_gm_only_note_from_player(client, seed):
    eid = _make_entity(seed.world_a.id, name="Innkeeper Bram", visible_to_players=True)
    _make_note(eid, "He tends the bar most nights. [gmonly]He poisons rival adventurers.[/gmonly]")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "poisons rival" not in r.text
    assert "tends the bar" in r.text


def test_detail_page_shows_gm_only_note_to_gm(client, seed):
    eid = _make_entity(seed.world_a.id, name="Innkeeper Bram")
    _make_note(eid, "He tends the bar most nights. [gmonly]He poisons rival adventurers.[/gmonly]")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert "poisons rival" in r.text


# ── Hover-preview API ────────────────────────────────────────────────────────

def test_entity_preview_strips_gm_only_body_for_player(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/entity/{eid}/preview")
    assert r.status_code == 200
    data = r.json()
    assert "cult spy" not in data["body"]
    assert "cult spy" not in data["body_html"]
    assert "pours drinks" in data["body"]


def test_entity_preview_keeps_gm_only_body_for_gm(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/entity/{eid}/preview")
    assert r.status_code == 200
    data = r.json()
    assert "cult spy" in data["body"]


# ── Downloads ────────────────────────────────────────────────────────────────

def test_entity_download_md_strips_gm_only_for_player(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", visible_to_players=True,
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_download_entities = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 200
    assert "cult spy" not in r.text
    assert "pours drinks" in r.text


def test_entity_download_md_keeps_gm_only_for_gm(client, seed):
    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram",
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}/download.md")
    assert r.status_code == 200
    assert "cult spy" in r.text


def test_kind_download_zip_strips_gm_only_for_player(client, seed):
    import io
    import zipfile

    eid = _make_entity(
        seed.world_a.id, name="Innkeeper Bram", kind="character", visible_to_players=True,
        body="He pours drinks. [gmonly]He is secretly a cult spy.[/gmonly]",
    )
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_download_entities = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/kind/character/download.zip")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    contents = "\n".join(zf.read(n).decode() for n in zf.namelist())
    assert "cult spy" not in contents
    assert "pours drinks" in contents


# ── Private Notes (GM <-> one player) ───────────────────────────────────────

def _make_private_note(world_id, player_user_id, content):
    db = SessionLocal()
    try:
        n = PrivateNote(world_id=world_id, player_user_id=player_user_id, content=content)
        db.add(n)
        db.commit()
        return n.id
    finally:
        db.close()


def test_private_notes_page_hides_gm_only_from_the_target_player(client, seed):
    _make_private_note(
        seed.world_a.id, seed.player_a.id,
        "Your character finds a strange coin. [gmonly]It's the cult's mark — plant a hook next session.[/gmonly]",
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    assert "cult's mark" not in r.text
    assert "strange coin" in r.text


def test_private_notes_page_shows_gm_only_to_the_gm(client, seed):
    _make_private_note(
        seed.world_a.id, seed.player_a.id,
        "Your character finds a strange coin. [gmonly]It's the cult's mark — plant a hook next session.[/gmonly]",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert r.status_code == 200
    assert "cult's mark" in r.text
