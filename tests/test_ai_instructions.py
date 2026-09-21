"""Tests for AiInstruction (app/models.py, app/ai_instructions.py,
app/routers/ai_instructions.py) — a GM-uploaded collection of markdown
files carrying STANDING behavior instructions ("always answer in a noir
detective's voice") that get appended, unconditionally, to every
AI-answering surface's system prompt: the GM's /ai chat page, the
player-facing /ai-chat page, an entity's "Ask AI" panel, and Chronicler.
Distinct from RAG (app.retrieval): these are never filtered by relevance
to the question asked.
"""
import io

from app.ai_instructions import enabled_instructions_text
from app.database import SessionLocal
from app.models import AiInstruction, Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _add_instruction(world_id, title="Tone Guide", content="Always answer in a noir detective voice.", enabled=True):
    db = SessionLocal()
    try:
        instr = AiInstruction(world_id=world_id, title=title, content=content, enabled=enabled)
        db.add(instr)
        db.commit()
        db.refresh(instr)
        return instr.id
    finally:
        db.close()


# ── enabled_instructions_text (unit-level, no HTTP) ─────────────────────────

def test_no_instructions_returns_empty_string(client, seed):
    db = SessionLocal()
    try:
        assert enabled_instructions_text(db, seed.world_a.id) == ""
    finally:
        db.close()


def test_enabled_instruction_is_included(client, seed):
    _add_instruction(seed.world_a.id, title="Tone Guide", content="Always answer in a noir detective voice.")
    db = SessionLocal()
    try:
        text = enabled_instructions_text(db, seed.world_a.id)
        assert "## Tone Guide" in text
        assert "Always answer in a noir detective voice." in text
    finally:
        db.close()


def test_disabled_instruction_is_excluded(client, seed):
    _add_instruction(seed.world_a.id, title="Off", content="Should not appear.", enabled=False)
    db = SessionLocal()
    try:
        assert enabled_instructions_text(db, seed.world_a.id) == ""
    finally:
        db.close()


def test_multiple_enabled_instructions_all_included_in_creation_order(client, seed):
    _add_instruction(seed.world_a.id, title="First", content="Be terse.")
    _add_instruction(seed.world_a.id, title="Second", content="Use British spelling.")
    db = SessionLocal()
    try:
        text = enabled_instructions_text(db, seed.world_a.id)
        assert text.index("First") < text.index("Second")
        assert "Be terse." in text
        assert "Use British spelling." in text
    finally:
        db.close()


def test_instructions_from_other_world_not_included(client, seed):
    _add_instruction(seed.world_b.id, title="Wrong World", content="Should never leak into World A.")
    db = SessionLocal()
    try:
        assert enabled_instructions_text(db, seed.world_a.id) == ""
    finally:
        db.close()


# ── Wired into all four system prompts ──────────────────────────────────────

def test_gm_ai_chat_includes_custom_instructions(client, seed):
    _add_instruction(seed.world_a.id, title="Tone Guide", content="Always answer in a noir detective voice.")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "Tone Guide" in r.text
    assert "Always answer in a noir detective voice." in r.text
    # Additive, not a replacement — the existing grounding clause is still there.
    assert "never invent specific names, numbers, or" in r.text


def test_gm_ai_chat_no_world_has_no_instructions_block(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "Additional GM-authored instructions" not in r.text


def test_player_ai_chat_includes_custom_instructions(client, seed):
    _add_instruction(seed.world_a.id, title="Tone Guide", content="Always answer in a noir detective voice.")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200
    assert "Tone Guide" in r.text
    assert "Always answer in a noir detective voice." in r.text


def test_player_ai_chat_no_instructions_renders_empty_constant(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.players_can_use_ai_chat = True
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai-chat")
    assert r.status_code == 200
    assert 'const PC_CUSTOM_INSTRUCTIONS = "";' in r.text


def test_entity_ask_ai_includes_custom_instructions(client, seed):
    _add_instruction(seed.world_a.id, title="Tone Guide", content="Always answer in a noir detective voice.")
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="item", name="Test Item")
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
    assert "Tone Guide" in r.text
    assert "Always answer in a noir detective voice." in r.text


def test_chronicler_system_prompt_includes_custom_instructions(client, seed):
    from app.routers.chronicler import build_chronicler_system_prompt

    _add_instruction(seed.world_a.id, title="Tone Guide", content="Always answer in a noir detective voice.")
    db = SessionLocal()
    try:
        prompt = build_chronicler_system_prompt(db, seed.world_a.id, "any question", seed.gm)
        assert "Tone Guide" in prompt
        assert "Always answer in a noir detective voice." in prompt
    finally:
        db.close()


def test_chronicler_system_prompt_no_instructions_unaffected(client, seed):
    from app.routers.chronicler import build_chronicler_system_prompt

    db = SessionLocal()
    try:
        prompt = build_chronicler_system_prompt(db, seed.world_a.id, "any question", seed.gm)
        assert "Additional GM-authored instructions" not in prompt
    finally:
        db.close()


# ── POST /worlds/{world_id}/ai-instructions/import ──────────────────────────

def _upload(client, world_id, filename, data, content_type, **form):
    files = {"file": (filename, io.BytesIO(data), content_type)}
    return client.post(f"/worlds/{world_id}/ai-instructions/import", files=files, data=form, follow_redirects=False)


def _instructions_for(world_id):
    db = SessionLocal()
    try:
        return db.query(AiInstruction).filter(AiInstruction.world_id == world_id).all()
    finally:
        db.close()


def test_import_md_file_creates_instruction(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload(client, seed.world_a.id, "tone.md", b"Always answer in a noir detective voice.", "text/markdown")
    assert r.status_code == 303
    rows = _instructions_for(seed.world_a.id)
    assert len(rows) == 1
    assert rows[0].title == "tone"
    assert rows[0].content == "Always answer in a noir detective voice."
    assert rows[0].enabled is True


def test_import_uses_explicit_title_over_filename(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload(client, seed.world_a.id, "tone.md", b"Content.", "text/markdown", title="My Tone Guide")
    assert r.status_code == 303
    rows = _instructions_for(seed.world_a.id)
    assert rows[0].title == "My Tone Guide"


def test_import_rejects_unsupported_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload(client, seed.world_a.id, "tone.pdf", b"%PDF-1.4 fake", "application/pdf")
    assert r.status_code == 400
    assert _instructions_for(seed.world_a.id) == []


def test_import_rejects_empty_file(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload(client, seed.world_a.id, "empty.md", b"   ", "text/markdown")
    assert r.status_code == 303  # redirects back, but nothing is created
    assert _instructions_for(seed.world_a.id) == []


def test_import_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload(client, seed.world_a.id, "tone.md", b"content", "text/markdown")
    assert r.status_code == 403
    assert _instructions_for(seed.world_a.id) == []


# ── toggle / delete ──────────────────────────────────────────────────────────

def test_toggle_flips_enabled_state(client, seed):
    iid = _add_instruction(seed.world_a.id, enabled=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/toggle", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid).enabled is False
    finally:
        db.close()
    r = client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/toggle", follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid).enabled is True
    finally:
        db.close()


def test_toggle_requires_gm(client, seed):
    iid = _add_instruction(seed.world_a.id, enabled=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/toggle", follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid).enabled is True  # unchanged
    finally:
        db.close()


def test_toggle_cannot_affect_another_worlds_instruction(client, seed):
    iid = _add_instruction(seed.world_b.id, enabled=True)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/toggle", follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid).enabled is True  # unchanged — wrong world_id in the URL
    finally:
        db.close()


def test_delete_removes_instruction(client, seed):
    iid = _add_instruction(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid) is None
    finally:
        db.close()


def test_delete_requires_gm(client, seed):
    iid = _add_instruction(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post(f"/worlds/{seed.world_a.id}/ai-instructions/{iid}/delete", follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid) is not None  # still there
    finally:
        db.close()


# ── World delete cascade ─────────────────────────────────────────────────────

def test_world_delete_removes_ai_instructions(client, seed):
    iid = _add_instruction(seed.world_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(AiInstruction, iid) is None
    finally:
        db.close()
