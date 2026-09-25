"""Regression tests for the September 2026 full-audit visibility batch —
GM-only content reaching player/assistant surfaces through the paths that
audit found:

- /search served raw [gmonly] blocks in body snippets, note snippets, and
  the summary line the results list renders (the detail page stripped them;
  search was the one raw surface, and searching a word unique to a secret
  actively extracted it).
- /api/entity/{id}/preview (autolink hover popup + Image Studio prompt
  builder) stripped `body` but returned `summary` raw.
- Quest/party/fact/PC-note bodies rendered with plain |md, where an intact
  [gmonly] tag renders as a labeled "GM ONLY" box WITH its content to any
  viewer — fixed by the viewer-aware md_for(request) filter.
- /quests and /quests/{id} ignored Quest.visible_to_players (the MCP tool
  and world-summary pipeline already honored it).
- The Facts page and the session-detail Facts panel never filtered
  Fact.visible_to_players for non-GMs.
- POST /api/facts/from-job/{id} had no world scoping — an assistant of
  world A could write facts into world B by id.
- Assistant-reachable AI-job routes honored client-supplied use_rag; RAG
  retrieval includes GM-only lore with no per-viewer filter, so only a GM
  may opt in (the recap route already enforced this via its audience gate).
"""
import json

from app.database import SessionLocal
from app.models import AudioJob, Entity, EntityNote, Fact, GameSession, Quest, WorldMembership
from app.templating import templates

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, fake_request, login


def _make_assistant(seed, player):
    """Flip `player`'s membership in their world to role="assistant" (same
    helper shape as test_gm_assistant's)."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def _pin_world(client, slug):
    client.cookies.set("active_world", slug)


def _set_access(world_id, **section_levels):
    """_set_access(world_id, quests={"player": "read"}) — merges per-section
    levels into World.section_access_json, same helper shape as
    test_player_section_access's."""
    import json as _json
    from app.deps import world_section_access
    from app.models import World
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        current = world_section_access(w)
        for sid, levels in section_levels.items():
            current[sid].update(levels)
        w.section_access_json = _json.dumps(current)
        db.commit()
    finally:
        db.close()


def _gmonly_entity(seed):
    db = SessionLocal()
    try:
        e = Entity(
            world_id=seed.world_a.id, kind="character", name="Elyra",
            summary="A fox. [gmonly]summary-secret-ZARATHAX[/gmonly]",
            body="Public lead about the harbor. [gmonly]harbor-secret-MALACHOR[/gmonly]",
            visible_to_players=True,
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


# ── /search ───────────────────────────────────────────────────────────────────

def test_search_never_serves_gm_only_content_to_player(client, seed):
    _gmonly_entity(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    # Querying the secret word itself: the echoed input value legitimately
    # contains the query, so assert on the full secret phrases instead.
    r = client.get("/search", params={"q": "MALACHOR"})
    assert r.status_code == 200
    assert "harbor-secret-MALACHOR" not in r.text
    # Name match renders the summary line — must be the stripped one
    r = client.get("/search", params={"q": "Elyra"})
    assert "summary-secret-ZARATHAX" not in r.text
    assert "A fox." in r.text


def test_search_serves_gm_only_content_to_gm(client, seed):
    _gmonly_entity(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get("/search", params={"q": "Elyra"})
    assert "summary-secret-ZARATHAX" in r.text


def test_search_note_snippets_strip_gm_only(client, seed):
    db = SessionLocal()
    try:
        e = Entity(world_id=seed.world_a.id, kind="character", name="Quill",
                   visible_to_players=True)
        db.add(e)
        db.commit()
        db.refresh(e)
        db.add(EntityNote(
            entity_id=e.id, content="Scroll text. [gmonly]note-secret-VELDRA[/gmonly]",
            visible_to_players=True,
        ))
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get("/search", params={"q": "VELDRA"})
    assert r.status_code == 200
    assert "note-secret-VELDRA" not in r.text


# ── hover-preview API ─────────────────────────────────────────────────────────

def test_entity_preview_summary_stripped_for_player(client, seed):
    eid = _gmonly_entity(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/api/entity/{eid}/preview")
    assert r.status_code == 200
    assert "ZARATHAX" not in r.json()["summary"]
    assert "A fox." in r.json()["summary"]


# ── md_for filter + the surfaces that use it ─────────────────────────────────

def test_md_for_filter_strips_for_non_gm_keeps_for_gm():
    f = templates.env.filters["md_for"]
    text = "Public part. [gmonly]the-secret-part[/gmonly]"
    player_html = f(text, fake_request(is_gm=False))
    assert "the-secret-part" not in player_html
    assert "Public part." in player_html
    gm_html = f(text, fake_request(is_gm=True))
    assert "the-secret-part" in gm_html


def test_quest_body_gm_only_hidden_from_read_only_player(client, seed):
    """A player with quests READ (no edit form, rendered body only) must get
    the stripped render — an assistant with edit rights legitimately sees
    the raw text in the edit textarea, same as any editable GM-only field."""
    db = SessionLocal()
    try:
        q = Quest(world_id=seed.world_a.id, title="The Harbor Job", status="active",
                  body="Meet the contact. [gmonly]the contact is a traitor-XYLOPHONE[/gmonly]")
        db.add(q)
        db.commit()
        db.refresh(q)
        qid = q.id
    finally:
        db.close()
    _set_access(seed.world_a.id, quests={"player": "read"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get(f"/quests/{qid}")
    assert r.status_code == 200
    assert "Meet the contact." in r.text
    assert "XYLOPHONE" not in r.text


# ── Quest.visible_to_players ──────────────────────────────────────────────────

def test_hidden_quest_filtered_from_list_and_404_on_detail_for_non_gm(client, seed):
    db = SessionLocal()
    try:
        visible = Quest(world_id=seed.world_a.id, title="Public Plumb", status="active",
                        visible_to_players=True)
        hidden = Quest(world_id=seed.world_a.id, title="Secret Sabotage", status="active",
                       summary="hidden-summary-KRAKEN", visible_to_players=False)
        db.add_all([visible, hidden])
        db.commit()
        db.refresh(hidden)
        hidden_id = hidden.id
    finally:
        db.close()
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get("/quests")
    assert r.status_code == 200
    assert "Public Plumb" in r.text
    assert "Secret Sabotage" not in r.text
    assert client.get(f"/quests/{hidden_id}").status_code == 404
    # The GM still sees both
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/quests")
    assert "Secret Sabotage" in r.text


# ── Fact.visible_to_players ───────────────────────────────────────────────────

def test_hidden_facts_filtered_for_non_gm_on_facts_page(client, seed):
    db = SessionLocal()
    try:
        db.add_all([
            Fact(world_id=seed.world_a.id, content="Visible fact.", visible_to_players=True),
            Fact(world_id=seed.world_a.id, content="GM-only fact-NIGHTJAR.", visible_to_players=False),
        ])
        db.commit()
    finally:
        db.close()
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get("/facts")
    assert r.status_code == 200
    assert "Visible fact." in r.text
    assert "NIGHTJAR" not in r.text


def test_hidden_facts_filtered_in_session_panel_for_non_gm(client, seed):
    db = SessionLocal()
    try:
        gs = GameSession(world_id=seed.world_a.id, session_num=1, title="Session One")
        db.add(gs)
        db.commit()
        db.refresh(gs)
        db.add_all([
            Fact(world_id=seed.world_a.id, game_session_id=gs.id,
                 content="Panel fact.", visible_to_players=True),
            Fact(world_id=seed.world_a.id, game_session_id=gs.id,
                 content="Panel secret-OSPREY.", visible_to_players=False),
        ])
        db.commit()
        db.refresh(gs)
        sid = gs.id
    finally:
        db.close()
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    assert "Panel fact." in r.text
    assert "OSPREY" not in r.text


# ── /api/facts/from-job world scoping ─────────────────────────────────────────

def test_facts_from_job_is_world_scoped(client, seed):
    db = SessionLocal()
    try:
        other = AudioJob(world_id=seed.world_b.id, purpose="facts_parse", filename="Facts",
                         status="done", pending_facts_json=json.dumps(
                             [{"content": "draft", "visible_to_players": True}]))
        db.add(other)
        db.commit()
        db.refresh(other)
        other_id = other.id
    finally:
        db.close()
    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.post(f"/api/facts/from-job/{other_id}", json={"facts": []})
    assert r.status_code == 404
    # The other world's draft must be untouched (not consumed)
    db = SessionLocal()
    try:
        job = db.get(AudioJob, other_id)
        assert job.pending_facts_json != "[]"
        assert not db.query(Fact).filter(Fact.world_id == seed.world_a.id,
                                         Fact.content == "draft").first()
    finally:
        db.close()


# ── use_rag is GM-only opt-in on assistant-reachable routes ───────────────────

def test_assistant_cannot_opt_into_rag_on_facts_parse(client, seed, monkeypatch):
    import app.ai as ai_module
    import app.audio_jobs as audio_jobs_module

    rag_calls = []
    monkeypatch.setattr(
        audio_jobs_module, "_build_rag_context",
        lambda *a, **k: rag_calls.append(1) or "RAG CONTEXT",
    )

    async def _fake_parse(text, **k):
        return {"facts": []}

    monkeypatch.setattr(ai_module, "parse_facts_from_recap", _fake_parse)

    _make_assistant(seed, seed.player_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin_world(client, "world-a")
    r = client.post("/api/facts/parse", json={"text": "The party fled.", "use_rag": True})
    assert r.status_code == 200
    assert rag_calls == []
