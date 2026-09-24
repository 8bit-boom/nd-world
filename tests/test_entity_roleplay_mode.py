"""Tests for two bugs in the entity detail page's Ask AI/Roleplay panel
(entities/detail.html):

1. GM-authored standing instructions (app.ai_instructions, e.g. "the
   kingdom is currently at war") were appended to EP_SYSTEM_ASK only —
   Roleplay mode never saw them at all, even though standing world-
   building context is exactly as relevant to an in-character
   conversation as it is to a narrator-style Q&A.

2. epSetMode() never reset epHistory/the visible transcript when
   switching between Ask and Roleplay — the two modes carry contradictory
   system prompts (narrator-answering-questions vs. speaking-in-first-
   person-as-the-character), so reusing the same conversation history
   across a switch mixed both voices into one transcript the model could
   see in its own history, undermining whichever mode was actually
   active.

JS-source assertion tests, reading the rendered page directly — matches
this repo's established convention for template-JS regression coverage
(see test_ai_chat_stream_errors.py).
"""
from app.database import SessionLocal
from app.models import AiInstruction, Entity, World

from .conftest import GM_PASSWORD, login


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


def _add_instruction(world_id, content, enabled=True):
    db = SessionLocal()
    try:
        instr = AiInstruction(world_id=world_id, title="Setting note", content=content, enabled=enabled)
        db.add(instr)
        db.commit()
    finally:
        db.close()


def _get_entity_page(client, seed, entity_id):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200
    return r.text


# ── Custom instructions reach both modes ────────────────────────────────────

def test_custom_instructions_appended_to_ask_system_prompt(client, seed):
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow")
    _add_instruction(seed.world_a.id, "The kingdom of Veyra is currently at war with its northern neighbor.")
    page = _get_entity_page(client, seed, eid)
    assert "The kingdom of Veyra is currently at war" in page
    ask_system = page.split("const EP_SYSTEM_ASK", 1)[1].split("const EP_SYSTEM_ROLEPLAY", 1)[0]
    assert "EP_CUSTOM_INSTRUCTIONS" in ask_system


def test_custom_instructions_appended_to_roleplay_system_prompt(client, seed):
    """The real bug: EP_SYSTEM_ROLEPLAY never referenced
    EP_CUSTOM_INSTRUCTIONS at all before this fix."""
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow")
    _add_instruction(seed.world_a.id, "The kingdom of Veyra is currently at war with its northern neighbor.")
    page = _get_entity_page(client, seed, eid)
    roleplay_system = page.split("const EP_SYSTEM_ROLEPLAY", 1)[1].split("let epMode", 1)[0]
    assert "EP_CUSTOM_INSTRUCTIONS" in roleplay_system


def test_roleplay_instructions_framed_as_never_breaking_character(client, seed):
    """The injected instructions must be framed so the model doesn't read
    them as license to step out of character and narrate/explain them."""
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow")
    page = _get_entity_page(client, seed, eid)
    roleplay_system = page.split("const EP_SYSTEM_ROLEPLAY", 1)[1].split("let epMode", 1)[0]
    assert "never break character" in roleplay_system


def test_disabled_instruction_does_not_reach_the_page(client, seed):
    eid = _make_entity(seed.world_a.id, name="Old Man Harrow")
    _add_instruction(seed.world_a.id, "This should never appear.", enabled=False)
    page = _get_entity_page(client, seed, eid)
    assert "This should never appear." not in page


# ── Mode switch resets the transcript ───────────────────────────────────────

def _ep_set_mode_body():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app" / "templates" / "entities" / "detail.html").read_text()
    start = js.index("window.epSetMode = function(mode) {")
    end = js.index("window.epAttachments =", start)
    return js[start:end]


def test_mode_switch_is_a_no_op_when_already_in_that_mode():
    body = _ep_set_mode_body()
    assert "if (mode === epMode) return;" in body


def test_mode_switch_clears_history_and_transcript():
    body = _ep_set_mode_body()
    assert "epHistory = [];" in body
    assert "document.getElementById('ep-messages').innerHTML = '';" in body
    # Both clears must happen AFTER the no-op guard, not before it (or every
    # click — even on the already-active mode — would wipe the transcript).
    guard_pos = body.index("if (mode === epMode) return;")
    assert body.index("epHistory = [];") > guard_pos
    assert body.index("innerHTML = '';") > guard_pos
