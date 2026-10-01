"""Every AI prompt that mentions the party describes each character by its OWN system (digest line),
not "? ?, level 1"; and the chat RAG knows the player characters at all."""
import json

import pytest

from app import ai as ai_module
from app.audio_jobs import _format_pc_line
from app.database import SessionLocal
from app.models import GameSession, Party, PlayerCharacter, SheetTemplate, World
from app import retrieval

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")

STATS = json.dumps([{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")])


def _tpl(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _scene(seed, owner_id=None):
    db = SessionLocal()
    try:
        anders = PlayerCharacter(world_id=seed.world_a.id, owner_user_id=owner_id, name="Anders", player_name="Archie", max_hp=0,
                                 sheet_template_id=_tpl("hunt-in-the-moonlight"), conditions_json=json.dumps(["Stunned"]),
                                 backstory="Raised on the docks. [gmonly]Secretly the heir.[/gmonly]",
                                 custom_fields_json=json.dumps({"health_current": 2, "health_max": 5, "stamina_current": 4,
                                                                "stamina_max": 5, "race": "Human"}))
        vex = PlayerCharacter(world_id=seed.world_a.id, name="Vex Valdera", race="Elf", char_class="Rogue", level=3, max_hp=20,
                              current_hp=9, stats_json=STATS)
        db.add_all([anders, vex])
        db.commit()
        party = Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([anders.id, vex.id]))
        db.add(party)
        db.commit()
        return anders.id, vex.id, party.id
    finally:
        db.close()


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── party insights ───────────────────────────────────────────────────────────

def test_party_insights_prompt_describes_each_member_by_their_system(client, seed, monkeypatch):
    anders, vex, party = _scene(seed)
    seen = {}

    async def fake_chat(messages, system="", **kw):
        seen["user"] = messages[0]["content"]
        return json.dumps({"bonds": ["b"], "tensions": ["t"], "hooks": [{"who": "Anders", "hook": "h"}]})

    monkeypatch.setattr(ai_module, "generate_chat", fake_chat)
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")
    _gm(client, seed)
    assert client.post(f"/parties/{party}/ai-insights").status_code == 200
    u = seen["user"]
    assert "Anders (Hunt in the Moonlight, Human, played by Archie)" in u and "Health 2/5" in u and "Stunned" in u
    assert "? ?" not in u and "level 1" not in u
    assert "Vex Valdera (Elf Rogue, level 3)" in u
    assert "Raised on the docks" in u, "backstory is what 'who these characters are' means"


# ── session prep ─────────────────────────────────────────────────────────────

def test_session_prep_context_lists_the_party_with_their_state(client, seed, monkeypatch):
    _, _, party = _scene(seed)
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, title="Next", session_num=2, party_id=party)
        db.add(gs)
        db.commit()
        sid = gs.id
    finally:
        db.close()
    captured = {}

    async def fake_prep(context_text, model=""):
        captured["context"] = context_text
        return ["task"]

    monkeypatch.setattr(ai_module, "generate_session_prep", fake_prep)
    _gm(client, seed)
    assert client.post(f"/api/sessions/{sid}/prep/generate").status_code == 200
    assert "Health 2/5" in captured["context"] and "Anders (Hunt in the Moonlight" in captured["context"]


# ── audio hints ──────────────────────────────────────────────────────────────

def test_audio_pinned_character_line_is_system_aware(seed):
    anders, vex, _ = _scene(seed)
    db = SessionLocal()
    try:
        a, v = db.get(PlayerCharacter, anders), db.get(PlayerCharacter, vex)
        assert _format_pc_line(a).startswith("- [player character] Anders (Hunt in the Moonlight, Human")
        assert "(Rogue Elf)" in _format_pc_line(v)
        assert "? ?" not in _format_pc_line(a)
    finally:
        db.close()


# ── chat RAG ─────────────────────────────────────────────────────────────────

def _ctx(seed, query, user=None):
    db = SessionLocal()
    try:
        return retrieval.smart_world_context(db, seed.world_a.id, query, user=user)[0]
    finally:
        db.close()


def test_rag_includes_a_character_the_question_names(seed):
    _scene(seed)
    ctx = _ctx(seed, "How is Anders holding up after the fight?")
    assert "Player characters" in ctx and "Anders (Hunt in the Moonlight" in ctx and "Health 2/5" in ctx
    assert "Secretly the heir" in ctx, "GM context keeps GM-only backstory"
    assert "Vex Valdera" not in ctx, "only characters the question names, plus nothing unrelated"


def test_rag_lists_the_party_when_asked_about_the_party(seed):
    _scene(seed)
    ctx = _ctx(seed, "Who in the party is hurt?")
    assert "Anders (Hunt in the Moonlight" in ctx and "Vex Valdera (Elf Rogue" in ctx


def test_rag_adds_nothing_for_an_unrelated_question(seed):
    _scene(seed)
    assert "Player characters" not in _ctx(seed, "Tell me about the tavern")


def test_a_player_only_gets_characters_they_may_see_and_no_gm_secrets(client, seed):
    anders, vex, _ = _scene(seed, owner_id=None)
    db = SessionLocal()
    try:
        from app.models import User
        player = db.get(User, seed.player_a.id)
        db.get(PlayerCharacter, anders).owner_user_id = player.id
        db.get(World, seed.world_a.id).players_see_party = False
        db.commit()
        ctx = retrieval.smart_world_context(db, seed.world_a.id, "How are Anders and Vex Valdera?", user=player)[0]
    finally:
        db.close()
    assert "Anders (Hunt in the Moonlight" in ctx and "Raised on the docks" in ctx
    assert "Secretly the heir" not in ctx
    assert "Vex Valdera" not in ctx, "not theirs, and the world keeps parties private"
