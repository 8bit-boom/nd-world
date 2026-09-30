"""Tests for the three tool-assist AI routes added 2026-09-30 (the Tools
menu's remaining non-AI surfaces): combat tactics, party insights, and
calendar day flavor. All are suggestion panels — they READ tool state,
return drafts, and write nothing; the deterministic mechanics (dice,
initiative, scheduling) are untouched by design."""
import json

import app.ai as ai_module
from app.database import SessionLocal
from app.models import CombatSession, Party, PlayerCharacter

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _mock_chat(monkeypatch, payload):
    # The routes import ai locally inside the handler, so patch the source
    # module attribute they resolve at call time.
    monkeypatch.setattr(ai_module, "effective_llm_api_key", lambda: "sk-test")

    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        return json.dumps(payload)
    monkeypatch.setattr(ai_module, "generate_chat", _gen)


# ── combat tactics ───────────────────────────────────────────────────────────

def _combat(seed, combatants):
    db = SessionLocal()
    try:
        cs = CombatSession(world_id=seed.world_a.id, name="Dock Ambush",
                           combatants_json=json.dumps(combatants))
        db.add(cs)
        db.commit()
        db.refresh(cs)
        return cs.id
    finally:
        db.close()


def test_combat_tactics_returns_suggestions(client, seed, monkeypatch):
    _mock_chat(monkeypatch, {"tactics": [{"who": "Dock Thug", "move": "Flanks via the containers."}],
                             "beat": "A crane swings loose overhead."})
    cid = _combat(seed, [{"name": "Dock Thug", "hp": 12}, {"name": "Rust (PC)", "hp": 20}])
    _gm(client, seed)
    r = client.post(f"/combat/{cid}/ai-tactics")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tactics"][0]["who"] == "Dock Thug"
    assert "crane" in d["beat"]


def test_combat_tactics_needs_combatants_and_gm(client, seed, monkeypatch):
    cid = _combat(seed, [])
    _gm(client, seed)
    assert client.post(f"/combat/{cid}/ai-tactics").status_code == 400
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/combat/{cid}/ai-tactics").status_code == 403


# ── party insights ───────────────────────────────────────────────────────────

def _party_with_pcs(seed, n=2):
    db = SessionLocal()
    try:
        ids = []
        for i in range(n):
            pc = PlayerCharacter(world_id=seed.world_a.id, name=f"Hero {i}",
                                 race="Human", char_class="Fixer", level=3)
            db.add(pc)
            db.flush()
            ids.append(pc.id)
        p = Party(world_id=seed.world_a.id, name="The Crew",
                  member_pc_ids_json=json.dumps(ids))
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def test_party_insights_returns_draft(client, seed, monkeypatch):
    _mock_chat(monkeypatch, {"bonds": ["Both owe the same fixer."],
                             "tensions": ["Hero 0 hides a debt."],
                             "hooks": [{"who": "Hero 0", "hook": "A courier with her name."}]})
    pid = _party_with_pcs(seed)
    _gm(client, seed)
    r = client.post(f"/parties/{pid}/ai-insights")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["bonds"] and d["hooks"][0]["who"] == "Hero 0"


def test_party_insights_needs_two_members(client, seed):
    db = SessionLocal()
    try:
        p = Party(world_id=seed.world_a.id, name="Lonely", member_pc_ids_json="[]")
        db.add(p); db.commit(); db.refresh(p); pid = p.id
    finally:
        db.close()
    _gm(client, seed)
    assert client.post(f"/parties/{pid}/ai-insights").status_code == 400


def test_party_insights_player_denied(client, seed):
    pid = _party_with_pcs(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/parties/{pid}/ai-insights").status_code == 403


# ── calendar day flavor ──────────────────────────────────────────────────────

def test_calendar_ai_day_returns_texture(client, seed, monkeypatch):
    _mock_chat(monkeypatch, {"weather": "Acid drizzle by noon.",
                             "omen": "Gulls fly inland.",
                             "sights": ["Neon reflections in puddles.", "A sealed shrine.", "Sirens far off."]})
    _gm(client, seed)
    r = client.post("/calendar/ai-day", data={"date_label": "3rd of Rain, 2099"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "drizzle" in d["weather"] and len(d["sights"]) == 3


def test_calendar_ai_day_player_denied(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/calendar/ai-day", data={"date_label": "x"}).status_code == 403
