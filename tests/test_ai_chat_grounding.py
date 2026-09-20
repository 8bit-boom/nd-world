"""Tests for a grounding clause added to the two general-purpose AI Chat
system prompts (GM's /ai page — app.main.ai_chat_page's world_system — and
the standalone player-facing /ai-chat page — ai_chat_player.html's
PC_SYSTEM).

The bug this fixes: asked "is there a Brutal trait for weapons?" against a
world whose actual Rules table defines Brutal as a plain flavor trait with
"no mechanical bonus", the GM AI Chat instead invented a whole list of
named weapons ("Hand axe", "War pick", "Butcher's cleaver", ...) with
fabricated "Load" stats that don't exist anywhere in that world's lore —
confidently answering a factual "does the rules say X" question by
generating fiction, rather than reporting what the retrieved lore/Rules
text actually says. Chronicler's system prompt (app/routers/chronicler.py)
already guards against exactly this failure mode for its own narrower
Q&A surface ("using ONLY the facts and entities listed below — never
invent... If the answer isn't in the provided context, say you don't
know"); these two general chat surfaces had no equivalent instruction at
all, despite being used for exactly this kind of factual lookup as well as
legitimate creative generation ("write a quest hook") — the fix adds the
same honesty requirement without removing the creative-generation
permission that's the whole point of these pages."""
from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_gm_ai_chat_system_prompt_includes_grounding_clause_with_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "never invent specific names, numbers, or" in r.text
    assert "say so plainly" in r.text
    # The creative-generation half of the prompt must still be there —
    # this is an addition, not a replacement.
    assert "Help the Game Master with world-building" in r.text


def test_gm_ai_chat_system_prompt_includes_grounding_clause_with_no_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "never invent specific names, numbers, or" in r.text


def test_player_ai_chat_system_prompt_includes_grounding_clause(client, seed):
    from app.database import SessionLocal
    from app.models import World

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
    # PC_SYSTEM is built via JS string concatenation across several lines
    # (see ai_chat_player.html), so a phrase split across a "+" boundary
    # never appears contiguous in the raw page source — these substrings
    # are each taken from within a single line's literal.
    assert "never invent specific names, numbers, or details to fill" in r.text
    assert "say so plainly rather than presenting an" in r.text
    assert "invented answer as fact." in r.text
    # The original scope-limiting instruction (no GM-only secrets) is
    # still there too.
    assert "You don't have access to any GM-only secrets" in r.text


# ── Table-fidelity directive: "quote the table, don't summarize it" ────────
#
# A second, distinct real bug on top of the original grounding gap: asked
# to "list the ordinary weapons", a model given BOTH a chapter's short
# overview paragraph and the actual item table nested under it answered
# with the overview's vague categories/price ranges instead of the
# table's real named rows — technically grounded (every word came from
# retrieved text) but not what "list the weapons" was actually asking
# for. Fixed by adding an explicit "a table's rows are the answer, not
# the overview prose" instruction to every AI Chat/Ask AI system prompt.

def test_gm_ai_chat_system_prompt_includes_table_fidelity_clause(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert "that table IS the answer" in r.text
    assert "list its actual named rows verbatim" in r.text


def test_player_ai_chat_system_prompt_includes_table_fidelity_clause(client, seed):
    from app.database import SessionLocal
    from app.models import World

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
    assert "that table IS the answer" in r.text
    assert "list its actual named rows verbatim" in r.text


def test_entity_ask_ai_system_prompt_has_grounding_and_table_fidelity_clauses(client, seed):
    """EP_SYSTEM_ASK (the entity detail page's "Ask AI about {entity}"
    panel) previously had NO grounding clause at all — just "answer
    questions about the entity provided. Be concise and in-world." —
    unlike the two general AI Chat surfaces above."""
    from app.database import SessionLocal
    from app.models import Entity

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
    assert "never invent specific names, numbers, or details to fill a gap" in r.text
    assert "that table IS the answer" in r.text
    assert "list its actual named rows verbatim" in r.text
