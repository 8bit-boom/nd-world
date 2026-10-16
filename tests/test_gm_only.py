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
    assert 'class="gm-only-text gm-inline"' in html          # inside a sentence: stays inline
    assert "<div" not in html.split("Text.", 1)[1]
    block = render_md("[gmonly]\n\nFirst secret.\n\nSecond secret.\n\n[/gmonly]")
    assert '<div class="gm-only-text"' in block              # holding paragraphs: the labelled box
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


# ── Authoring convenience: toolbar button, right-click menu, keyboard shortcut ──

def _fn_body(js, name, next_name="function "):
    start = js.index(f"function {name}")
    end = js.index(f"\n{next_name}", start + 1)
    return js[start:end]


def test_toolbar_js_has_gmonly_button(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    assert r.status_code == 200
    js = r.text
    assert 'ndFmtWrapSelection(ta, "[gmonly]", "[/gmonly]")' in js


def test_gmonly_button_is_opt_in_not_global(client, seed):
    """Offered only on textareas the caller marks data-fmt-gmonly, not on
    every data-fmt field — Rules already has its own :::gm block mechanism,
    and Facts/PC notes/board nodes have no strip_gm_only() enforcement to
    back the button up."""
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtBuildToolbar")
    assert 'ta.hasAttribute("data-fmt-gmonly")' in fn


def test_gmonly_shortcuts_wired_into_init_only_for_opted_in_textareas(client, seed):
    # ndFmtInit is the last function declared in the file (followed only by
    # the DOMContentLoaded bootstrap), so there's no trailing "\nfunction "
    # to bound it with _fn_body — slice to end-of-file instead.
    r = client.get("/static/js/text-format-toolbar.js")
    fn = r.text[r.text.index("function ndFmtInit"):]
    assert 'if (ta.hasAttribute("data-fmt-gmonly")) ndFmtSetupGmOnlyShortcuts(ta);' in fn


def test_gmonly_context_menu_only_intercepts_an_actual_selection(client, seed):
    """A right-click with nothing selected (paste, spellcheck, the
    browser's own menu) must be left alone — only a non-collapsed
    selection triggers the custom menu."""
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtSetupGmOnlyShortcuts")
    assert '"contextmenu"' in fn
    assert "ta.selectionStart === ta.selectionEnd" in fn
    assert "e.preventDefault()" in fn
    assert 'ndFmtWrapSelection(ta, "[gmonly]", "[/gmonly]")' in fn


def test_gmonly_keyboard_shortcut_requires_ctrl_or_meta_and_shift_g(client, seed):
    r = client.get("/static/js/text-format-toolbar.js")
    fn = _fn_body(r.text, "ndFmtSetupGmOnlyShortcuts")
    assert '"keydown"' in fn
    assert "e.ctrlKey || e.metaKey" in fn
    assert "e.shiftKey" in fn
    assert 'e.key.toLowerCase() !== "g"' in fn


def test_selecting_text_offers_a_bubble_at_the_pointer_to_hide_it_from_players(client, seed):
    """Not only a right-click: letting go of a mouse selection shows a small bubble with the two hiding actions; the right-click menu is the
    full one - all through one shared menu for the editor and the read view."""
    js = client.get("/static/js/text-format-toolbar.js").text
    fn = _fn_body(js, "ndFmtSetupGmOnlyShortcuts")
    assert '"mouseup"' in fn and "e.button !== 0" in fn and "ndFmtOpenMenu(e.pageX + 6, e.pageY + 12, _ndFmtEditorContext(ta), true)" in fn
    assert "ndFmtOpenMenu(e.pageX, e.pageY, ctx, false)" in fn
    tags = js.split("NDFMT_ACTION_TAGS", 1)[1][:600]
    for tag in ('"[gmonly]", "[/gmonly]"', '"[spoiler]", "[/spoiler]"', '"[mark]", "[/mark]"', '"**", "**"'):
        assert tag in tags, tag
    menu = _fn_body(js, "ndFmtOpenMenu")
    assert "Hide from players" in menu and "if (compact)" in menu


def test_an_entity_page_has_a_breadcrumb_back_through_every_folder_and_subfolder(client, seed):
    eid = _make_entity(seed.world_a.id, kind="creature", name="Silt Wyrm", folder="Beasts/Water/Big")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/entity/{eid}").text
    nav = html.split('id="entity-breadcrumb"', 1)[1].split("</nav>", 1)[0]
    assert "/kind/creature" in nav
    for path in ("Beasts", "Beasts/Water", "Beasts/Water/Big"):
        assert "folder=" + path + "&amp;" in nav, path
    assert "Silt Wyrm" in nav.split("bc-current", 1)[1]
    # an unfiled entity still links back to its list
    eid2 = _make_entity(seed.world_a.id, kind="creature", name="Loose Thing")
    nav2 = client.get(f"/entity/{eid2}").text.split('id="entity-breadcrumb"', 1)[1].split("</nav>", 1)[0]
    assert "/kind/creature" in nav2 and "folder=" not in nav2


# ── marking up from the read view: POST /api/entity/{id}/markup ─────────────

def _body_of(eid):
    db = SessionLocal()
    try:
        return db.get(Entity, eid).body
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_markup_endpoint_wraps_a_selection_from_the_read_view(client, seed):
    eid = _make_entity(seed.world_a.id, body="The innkeeper is a **cult** spy.\n\nThe door is old.")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "gmonly", "text": "cult spy", "before": "innkeeper is a", "after": ""})
    assert r.status_code == 200, r.text
    assert _body_of(eid) == "The innkeeper is a [gmonly]**cult** spy[/gmonly].\n\nThe door is old."
    # …and the same endpoint takes it off again
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "unwrap:gmonly", "index": 0, "snippet": "cult spy"})
    assert r.status_code == 200
    assert _body_of(eid) == "The innkeeper is a **cult** spy.\n\nThe door is old."


def test_markup_endpoint_refuses_what_it_cannot_place_and_saves_nothing(client, seed):
    eid = _make_entity(seed.world_a.id, body="The door. The door.")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "gmonly", "text": "The door"})
    assert r.status_code == 422 and "more than once" in r.json()["error"]
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "gmonly", "text": "not there"})
    assert r.status_code == 422
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "evil", "text": "The door."})
    assert r.status_code == 422
    assert _body_of(eid) == "The door. The door."


def test_markup_endpoint_is_gm_only_and_world_scoped(client, seed):
    eid = _make_entity(seed.world_a.id, body="Secret words here")
    payload = {"field": "body", "action": "gmonly", "text": "Secret"}
    assert client.post(f"/api/entity/{eid}/markup", json=payload).status_code in (401, 403, 302, 303, 307)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/entity/{eid}/markup", json=payload).status_code == 403
    assert _body_of(eid) == "Secret words here"


def test_markup_endpoint_handles_notes(client, seed):
    eid = _make_entity(seed.world_a.id, body="x")
    db = SessionLocal()
    try:
        n = EntityNote(entity_id=eid, content="Meet at dawn", content_is_html=False)
        h = EntityNote(entity_id=eid, content="<p>Meet at dawn</p>", content_is_html=True)
        db.add_all([n, h]); db.commit()
        nid, hid = n.id, h.id
    finally:
        db.close()
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "note", "note_id": nid, "action": "spoiler", "text": "at dawn"})
    assert r.status_code == 200
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "note", "note_id": hid, "action": "spoiler", "text": "at dawn"})
    assert r.status_code == 422
    db = SessionLocal()
    try:
        assert db.get(EntityNote, nid).content == "Meet [spoiler]at dawn[/spoiler]"
        assert db.get(EntityNote, hid).content == "<p>Meet at dawn</p>"
    finally:
        db.close()


def test_entity_page_marks_its_fields_for_the_read_view_menu_only_for_a_gm(client, seed):
    eid = _make_entity(seed.world_a.id, body="Some text")
    _gm(client, seed)
    html = client.get(f"/entity/{eid}").text
    assert f'data-markup-entity="{eid}"' in html and 'data-markup-field="body"' in html
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/entity/{eid}").text
    assert "data-markup-entity" not in html


# ── undo, alias, send to a player, AI rewrite ───────────────────────────────

def test_markup_returns_what_is_needed_to_undo_and_restore_refuses_after_a_later_edit(client, seed):
    eid = _make_entity(seed.world_a.id, body="The innkeeper is a cult spy.")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "gmonly", "text": "cult spy"})
    undo = r.json()["undo"]
    assert undo["previous"] == "The innkeeper is a cult spy." and undo["expected"] == "The innkeeper is a [gmonly]cult spy[/gmonly]."
    # edited in between: undo must not clobber it
    db = SessionLocal()
    try:
        e = db.get(Entity, eid); e.body = undo["expected"] + " More."; db.commit()
    finally:
        db.close()
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "restore", "previous": undo["previous"], "expected": undo["expected"]})
    assert r.status_code == 422 and _body_of(eid).endswith("More.")
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "restore", "previous": undo["previous"], "expected": undo["expected"] + " More."})
    assert r.status_code == 200 and _body_of(eid) == "The innkeeper is a cult spy."


def test_markup_replace_puts_the_accepted_rewrite_in_place(client, seed):
    eid = _make_entity(seed.world_a.id, body="He is nice.\n\nThe door is old.")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "replace", "text": "He is nice.", "replacement": "He smiles too widely."})
    assert r.status_code == 200 and _body_of(eid) == "He smiles too widely.\n\nThe door is old."


def test_add_alias_is_gm_only_and_rejects_duplicates(client, seed):
    eid = _make_entity(seed.world_a.id, name="Edmund Vosk", aliases="Vosk")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/alias", json={"alias": "the Greyfather"})
    assert r.status_code == 200 and r.json()["aliases"] == "Vosk, the Greyfather"
    assert client.post(f"/api/entity/{eid}/alias", json={"alias": "VOSK"}).status_code == 422
    assert client.post(f"/api/entity/{eid}/alias", json={"alias": "edmund vosk"}).status_code == 422
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/entity/{eid}/alias", json={"alias": "Nope"}).status_code == 403


def test_send_to_player_uses_the_source_and_never_sends_gm_only_text(client, seed):
    from app.models import PrivateNote, WorldMembership
    db = SessionLocal()
    try:
        if not db.query(WorldMembership).filter_by(world_id=seed.world_a.id, user_id=seed.player_a.id).first():
            db.add(WorldMembership(world_id=seed.world_a.id, user_id=seed.player_a.id)); db.commit()
    finally:
        db.close()
    eid = _make_entity(seed.world_a.id, name="Old Map", body="Meet at the mill. [gmonly]The miller is the traitor.[/gmonly] Bring rope.")
    _gm(client, seed)
    players = client.get("/api/world-players").json()
    assert any(p["id"] == seed.player_a.id for p in players)
    # a selection that spans the secret: the secret is stripped from what is sent
    r = client.post(f"/api/entity/{eid}/send-to-player", json={"user_id": seed.player_a.id, "field": "body", "text": "Meet at the mill. The miller is the traitor. Bring rope."})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        notes = db.query(PrivateNote).filter_by(player_user_id=seed.player_a.id).all()
        assert len(notes) == 1 and "traitor" not in notes[0].content and "Meet at the mill." in notes[0].content
        assert notes[0].title == "From Old Map"
    finally:
        db.close()
    # a selection that is only the secret sends nothing
    r = client.post(f"/api/entity/{eid}/send-to-player", json={"user_id": seed.player_a.id, "field": "body", "text": "The miller is the traitor."})
    assert r.status_code == 422
    # a player of another world cannot be a target
    assert client.post(f"/api/entity/{eid}/send-to-player", json={"user_id": seed.player_b.id, "field": "body", "text": "Bring rope."}).status_code == 404
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/entity/{eid}/send-to-player", json={"user_id": seed.player_a.id, "field": "body", "text": "Bring rope."}).status_code == 403
    assert client.get("/api/world-players").status_code == 403


def test_ai_rewrite_returns_a_preview_and_is_gm_only(client, seed, monkeypatch):
    from app.routers import ai as ai_router
    seen = {}

    async def fake_chat(messages, system="", model="", options=None, **kw):
        seen["prompt"] = messages[0]["content"]; seen["system"] = system
        return "He smiles too widely."
    monkeypatch.setattr(ai_router._ai, "generate_chat", fake_chat)
    _gm(client, seed)
    r = client.post("/api/ai/rewrite", json={"text": "He is nice.", "style": "creepier"})
    assert r.status_code == 200 and r.json() == {"text": "He smiles too widely."}
    assert "eerie" in seen["prompt"] and "He is nice." in seen["prompt"] and "gmonly" in seen["system"]
    assert client.post("/api/ai/rewrite", json={"text": "x"}).status_code == 400            # no style or instruction
    assert client.post("/api/ai/rewrite", json={"text": "  ", "style": "shorter"}).status_code == 400
    assert client.post("/api/ai/rewrite", json={"text": "x" * 4001, "style": "shorter"}).status_code == 400
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.post("/api/ai/rewrite", json={"text": "He is nice.", "style": "shorter"}).status_code == 403


def test_the_menu_script_offers_the_new_actions(client, seed):
    js = client.get("/static/js/text-format-toolbar.js").text
    for needle in ("/api/dice/roll", "/api/display/show", "/api/ai/rewrite", "/send-to-player", "/alias", "/api/world-players",
                   "Hide this whole section from players", "Undo", 'action: "restore"', "_ndFmtLinkItems", "_ndFmtBlockItems"):
        assert needle in js, needle
    assert "NDFMT_DICE_RE" in js


# ── view as players, read-aloud box, quest / fact from a selection ──────────

def test_gm_can_preview_an_entity_page_as_a_player_and_nobody_else_can_ask_for_it(client, seed):
    eid = _make_entity(seed.world_a.id, name="Preview Me", body="Public line. [gmonly]Hidden twist.[/gmonly]", summary="Sum [gmonly]hidden sum[/gmonly]")
    _gm(client, seed)
    gm_html = client.get(f"/entity/{eid}").text
    assert "Hidden twist." in gm_html and "data-markup-entity" in gm_html and "As players" in gm_html
    pv = client.get(f"/entity/{eid}?as=player").text
    assert "Public line." in pv and "Hidden twist." not in pv and "hidden sum" not in pv
    assert "data-markup-entity" not in pv and "Previewing this page as a player" in pv
    client.cookies.clear()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/entity/{eid}?as=player").text
    assert "Previewing this page" not in html and "Hidden twist." not in html


def test_read_aloud_tag_renders_a_box_and_can_be_marked_from_the_read_view(client, seed):
    assert '<span class="read-aloud">The cellar door creaks.</span>' in render_md("[read]The cellar door creaks.[/read]")
    from app import markup_locator as ml
    out = ml.wrap("The cellar door creaks. Then silence.", "The cellar door creaks.", "", "", "read")
    assert out == "[read]The cellar door creaks.[/read] Then silence."
    assert ml.unwrap(out, "read", 0, "The cellar") == "The cellar door creaks. Then silence."
    eid = _make_entity(seed.world_a.id, body="Intro line.")
    _gm(client, seed)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "read", "text": "Intro line."})
    assert r.status_code == 200 and _body_of(eid) == "[read]Intro line.[/read]"


def test_menu_script_has_quest_fact_block_format_and_read_aloud_actions(client, seed):
    js = client.get("/static/js/text-format-toolbar.js").text
    for needle in ("/quests/new", "/facts/new", "Make a quest from this", "Save as a fact", "lineFormat", "Clear formatting", "Read-aloud box", "[read]"):
        assert needle in js, needle


# ── per-player secrets [for=IDs] and timed reveals [reveal=DATE] ─────────────

from datetime import datetime as _dt


def test_strip_resolves_for_and_reveal_fail_closed():
    now = _dt(2026, 10, 16, 12, 0)
    t = "A [for=3]s3[/for] B [reveal=2026-10-01]old[/reveal] C [reveal=2026-12-01]new[/reveal] D"
    assert strip_gm_only(t, now=now) == "A  B old C  D"                     # no viewer: no [for] text; the past reveal is plain text
    assert strip_gm_only(t, 3, now=now) == "A s3 B old C  D"
    assert strip_gm_only(t, 4, now=now) == "A  B old C  D"
    assert strip_gm_only("x [reveal=2026-10-16T12:00]at[/reveal]", now=now) == "x at"          # the moment itself counts
    assert strip_gm_only("x [reveal=2026-10-16T12:01]soon[/reveal]", now=now) == "x "
    assert strip_gm_only("x [FOR=3]caps[/FOR]", 3) == "x caps"


def test_strip_nested_and_malformed_access_tags_never_leak():
    # inner resolves first, then the outer: a player of the inner block only still gets nothing from the outer one
    t = "x [for=1]a [for=2]b[/for] c[/for] y"
    assert strip_gm_only(t, 1) == "x a  c y"
    assert strip_gm_only(t, 2) == "x  y"
    assert strip_gm_only(t) == "x  y"
    # a reveal inside a secret, and a secret inside a not-yet reveal
    assert strip_gm_only("[for=1]a [reveal=2020-01-01]b[/reveal][/for]", 2) == ""
    assert strip_gm_only("[reveal=2099-01-01]a [for=1]b[/for][/reveal]", 1) == ""
    # [gmonly] inside still wins
    assert strip_gm_only("[for=1]a [gmonly]g[/gmonly] b[/for]", 1) == "a  b"
    # an opening tag that never closes hides the rest; a stray closing tag disappears; a bad date stays hidden
    assert strip_gm_only("pub [for=1]secret forever", 1) == "pub "
    assert strip_gm_only("pub [reveal=2020-01-01]secret forever") == "pub "
    assert strip_gm_only("pub [/for] [/reveal] end") == "pub   end"
    assert strip_gm_only("a [reveal=2026-13-45]x[/reveal] b") == "a  b"
    assert strip_gm_only("a [for=]x[/for] b", 1) == "a "                       # a tag with no ids is malformed: hides the rest


def test_gm_sees_access_parts_labelled_in_the_rendered_page():
    html = render_md("Hi [for=2,3]sec[/for] and [reveal=2026-12-01T18:30]later[/reveal]")
    assert 'class="for-only-text gm-inline" data-for="2,3"' in html and 'data-reveal="2026-12-01T18:30"' in html
    block = render_md("[for=2]\n\npara one\n\npara two\n\n[/for]")
    assert "for-only-text" in block


def _member(world_id, user_id):
    from app.models import WorldMembership
    db = SessionLocal()
    try:
        if not db.query(WorldMembership).filter_by(world_id=world_id, user_id=user_id).first():
            db.add(WorldMembership(world_id=world_id, user_id=user_id)); db.commit()
    finally:
        db.close()


def test_a_player_sees_only_their_own_secret_and_only_a_due_reveal(client, seed):
    _member(seed.world_a.id, seed.player_a.id)
    body = (f"Public. [for={seed.player_a.id}]For A only.[/for] [for={seed.player_b.id}]For B only.[/for] "
            "[reveal=2020-01-01]Already revealed.[/reveal] [reveal=2099-01-01]Not yet revealed.[/reveal]")
    eid = _make_entity(seed.world_a.id, name="Secrets", body=body, summary=f"S [for={seed.player_b.id}]sum for B[/for]")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id); w.players_can_download_entities = True; db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    html = client.get(f"/entity/{eid}").text
    assert "Public." in html and "For A only." in html and "Already revealed." in html
    for hidden in ("For B only.", "Not yet revealed.", "sum for B", "data-for", "data-reveal", "[for=", "[reveal="):
        assert hidden not in html, hidden
    md = client.get(f"/entity/{eid}/download.md").text
    assert "For A only." in md and "For B only." not in md and "Not yet revealed." not in md and "sum for B" not in md
    prev = client.get(f"/api/entity/{eid}/preview").json()
    assert "For B only." not in prev["body"] and "For A only." in prev["body"] and "sum for B" not in prev["summary"]
    # a secret word searched for is not quoted back, from the secret that is not theirs
    sr = client.get("/search", params={"q": "Not yet revealed"}).text
    assert "Not yet revealed." not in sr


def test_the_gm_sees_everything_labelled_and_can_preview_as_one_player(client, seed):
    _member(seed.world_a.id, seed.player_a.id)
    body = f"[for={seed.player_a.id}]For A only.[/for] [reveal=2099-01-01]Later text.[/reveal]"
    eid = _make_entity(seed.world_a.id, name="GM Sees", body=body)
    _gm(client, seed)
    html = client.get(f"/entity/{eid}").text
    assert "For A only." in html and "Later text." in html and f'data-for="{seed.player_a.id}"' in html and 'data-reveal="2099-01-01T00:00"' in html
    as_a = client.get(f"/entity/{eid}?as=player&player={seed.player_a.id}").text
    assert "For A only." in as_a and "Later text." not in as_a
    as_nobody = client.get(f"/entity/{eid}?as=player").text
    assert "For A only." not in as_nobody and "Later text." not in as_nobody


def test_player_rag_and_second_screen_never_get_the_secret_even_for_its_own_player(client, seed):
    """Fail-closed: only the pages that know the viewer show their own [for] text; every other consumer of strip_gm_only (AI context,
    the second screen) drops it - a leak-proof default rather than a feature."""
    from app.models import Entity as _E
    db = SessionLocal()
    try:
        ent = _E(world_id=seed.world_a.id, kind="character", name="Rag Test", visible_to_players=True,
                 body=f"Open. [for={seed.player_a.id}]Private clue.[/for] [reveal=2099-01-01]Future twist.[/reveal]")
        db.add(ent); db.commit(); db.refresh(ent)
        text = format_context_from_entities([ent], strip_gm_only=True)
    finally:
        db.close()
    assert "Open." in text and "Private clue." not in text and "Future twist." not in text


def test_markup_endpoint_makes_player_secrets_and_timed_reveals(client, seed):
    _member(seed.world_a.id, seed.player_a.id)
    eid = _make_entity(seed.world_a.id, body="The key is under the mat. Say nothing.")
    _gm(client, seed)
    pid = seed.player_a.id
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": f"for:{pid}", "text": "under the mat"})
    assert r.status_code == 200, r.text
    assert _body_of(eid) == f"The key is [for={pid}]under the mat[/for]. Say nothing."
    # no nesting; a player of another world cannot be named; bad date refused
    assert client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": f"for:{pid}", "text": "key is under the mat"}).status_code == 422
    assert client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": f"for:{seed.player_b.id}", "text": "Say nothing"}).status_code == 422
    assert client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "for:999999", "text": "Say nothing"}).status_code == 422
    assert client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "reveal:2026-13-45", "text": "Say nothing"}).status_code == 422
    assert client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "reveal:nonsense", "text": "Say nothing"}).status_code == 422
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "reveal:2099-01-01T18:30", "text": "Say nothing"})
    assert r.status_code == 200
    assert _body_of(eid).endswith("[reveal=2099-01-01T18:30]Say nothing[/reveal].")
    # taking them off again
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "unwrap:reveal", "index": 0, "snippet": "Say nothing"})
    assert r.status_code == 200 and "[reveal" not in _body_of(eid)
    r = client.post(f"/api/entity/{eid}/markup", json={"field": "body", "action": "unwrap:for", "index": 0, "snippet": "under"})
    assert r.status_code == 200 and "[for" not in _body_of(eid)


def test_menu_script_offers_player_secrets_and_timed_reveals(client, seed):
    js = client.get("/static/js/text-format-toolbar.js").text
    for needle in ("Secret for one player", "Reveal to players later", "_ndFmtRevealDialog", "_ndFmtLabelAccessParts", '"for:"', '"reveal:"', "for-only-text", "reveal-later-text"):
        assert needle in js, needle


def test_search_does_not_reveal_that_an_entity_mentions_a_word_only_a_secret_contains(client, seed):
    """The snippet was always stripped; the entity showing up in a player's results at all used to confirm the secret word."""
    from app.models import EntityNote
    _member(seed.world_a.id, seed.player_a.id)
    body = (f"Common words here. [gmonly]zorblax gm secret[/gmonly] [for={seed.player_b.id}]quillnor for b[/for] "
            f"[for={seed.player_a.id}]flumpet for a[/for] [reveal=2099-01-01]vexwhistle later[/reveal] [reveal=2020-01-01]snarfle due[/reveal]")
    eid = _make_entity(seed.world_a.id, name="Probe Target", body=body, summary="[gmonly]summarysecret[/gmonly] plain summary")
    db = SessionLocal()
    try:
        db.add(EntityNote(entity_id=eid, content="note [gmonly]noteleak[/gmonly] visible", visible_to_players=True)); db.commit()
    finally:
        db.close()
    from app.retrieval import find_relevant_entities
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    def found(word):
        return "Probe Target" in client.get("/search", params={"q": word}).text

    for secret in ("zorblax", "quillnor", "vexwhistle", "summarysecret", "noteleak"):
        assert not found(secret), secret
    for visible in ("flumpet", "snarfle", "Common"):
        assert found(visible), visible
    # the same through the RAG entity lookup a player's AI uses
    db = SessionLocal()
    try:
        from app.models import User
        player = db.get(User, seed.player_a.id)
        assert not [e for e in find_relevant_entities(db, seed.world_a.id, "zorblax", user=player) if e.name == "Probe Target"]
        assert [e for e in find_relevant_entities(db, seed.world_a.id, "flumpet", user=player) if e.name == "Probe Target"]
    finally:
        db.close()
    # the GM still finds secrets
    client.cookies.clear()
    _gm(client, seed)
    assert "Probe Target" in client.get("/search", params={"q": "zorblax"}).text
