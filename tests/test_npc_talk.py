"""app.routers.npc_talk — persistent per-entity roleplay conversations.

These tests pin the security/correctness shape of the surface: the ask-AI
opt-in gate, per-viewer entity visibility, server-side [gmonly] stripping
in the composed prompt, the GM-only model/RAG rules, per-user conversation
isolation, and clean-finish-only persistence (a failure sentinel must
never become a saved reply). The AI itself is always monkeypatched —
same convention as tests/test_ai_assist.py.
"""
import json

import app.ai as ai_module
import app.retrieval as retrieval_module

from app.database import SessionLocal
from app.models import ChatSession, Entity, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _npc(seed, **kw):
    db = SessionLocal()
    try:
        e = Entity(
            world_id=seed.world_a.id, kind=kw.get("kind", "character"),
            name=kw.get("name", "Elyra"), summary=kw.get("summary", ""),
            body=kw.get("body", ""), visible_to_players=kw.get("visible_to_players", True),
            custom_fields_json=kw.get("custom_fields_json") or "{}",
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _patch_ai(monkeypatch, reply="Greetings, traveler.", capture=None, error=None):
    """Patch resolve_model + stream_chat the way /api/ai/stream's own tests
    do; records the (msgs, system, model, options) the surface composed."""
    async def _resolve(model):
        if capture is not None:
            capture["requested_model"] = model
        return (model or "test-model"), ""

    async def _stream(msgs, system, model, options, think=False, emit_thinking=False):
        if capture is not None:
            capture["msgs"] = msgs
            capture["system"] = system
            capture["model"] = model
            capture["think"] = think
        if error is not None:
            yield {"type": "error", "text": error}
            return
        yield {"type": "token", "text": reply}

    monkeypatch.setattr(ai_module, "resolve_model", _resolve)
    monkeypatch.setattr(ai_module, "stream_chat", _stream)


def _pin(client, slug="world-a"):
    client.cookies.set("active_world", slug)


# ── GM happy path ─────────────────────────────────────────────────────────────

def test_gm_stream_replies_in_character_and_persists(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, reply="Hail.", capture=cap)
    eid = _npc(seed, body="Elyra is the harbor fox.")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "Hail." in r.text
    assert "data: [DONE]" in r.text
    # Roleplay directive + identity reach the model server-side
    assert "roleplaying AS" in cap["system"]
    assert "Elyra is the harbor fox." in cap["system"]
    assert cap["msgs"][-1]["content"] == "hello"

    # And the turn persisted for THIS user
    r = client.get(f"/api/npc-talk/{eid}/history")
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "Hail."


def test_player_blocked_without_world_opt_in(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    eid = _npc(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get("/npc-talk").status_code == 403
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 403


# ── Player access under the world opt-in ─────────────────────────────────────

def _opt_in(seed, column="players_can_ask_ai"):
    db = SessionLocal()
    try:
        w = db.get(type(seed.world_a), seed.world_a.id)
        setattr(w, column, True)
        db.commit()
    finally:
        db.close()


def test_opted_in_player_streams_and_threads_are_per_user(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, reply="The fox speaks.", capture=cap)
    eid = _npc(seed)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get("/npc-talk").status_code == 200
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "who are you?"})
    assert r.status_code == 200

    # The GM has a separate, empty thread with the same NPC
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []
    # ...and the player's thread is intact from their own login
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    msgs = client.get(f"/api/npc-talk/{eid}/history").json()["messages"]
    assert len(msgs) == 2


def test_hidden_entity_not_talkable_by_player(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    eid = _npc(seed, visible_to_players=False)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 404
    assert client.get(f"/api/npc-talk/{eid}/history").status_code == 404
    # ...and it isn't in the player's picker either
    assert "data-id=\"%d\"" % eid not in client.get("/npc-talk").text
    # The GM can still talk to their own hidden NPC
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200


# ── Server-side [gmonly] stripping in the composed prompt ────────────────────

def test_gm_only_content_never_reaches_player_prompt(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)
    eid = _npc(
        seed,
        summary="A fox. [gmonly]she is the traitor[/gmonly]",
        body="Friendly ranger. [gmonly]Secretly works for the Syndicate-ZAROTH[/gmonly]",
        custom_fields_json='{"AC": "15", "Secret Contact": "[gmonly]Mister Nine[/gmonly]"}',
    )
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200
    assert "ZAROTH" not in cap["system"]
    assert "Mister Nine" not in cap["system"]
    assert "Friendly ranger." in cap["system"]
    assert "AC: 15" in cap["system"]
    # The GM's own conversation gets the full picture
    login(client, seed.gm.email, GM_PASSWORD)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"})
    assert "ZAROTH" in cap["system"]
    assert "Mister Nine" in cap["system"]


# ── GM-only knobs are ignored for non-GMs ────────────────────────────────────

def test_player_model_and_rag_choices_ignored(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)
    rag_calls = []

    def _rag(*a, **k):
        rag_calls.append(1)
        return "RAG LORE", [], []

    monkeypatch.setattr(retrieval_module, "smart_world_context", _rag)
    eid = _npc(seed)
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream",
                    json={"message": "hi", "model": "gm-only-model", "use_rag": True})
    assert r.status_code == 200
    assert rag_calls == []
    assert cap["requested_model"] != "gm-only-model"


def test_gm_use_rag_adds_lore_to_system(client, seed, monkeypatch):
    cap = {}
    _patch_ai(monkeypatch, capture=cap)

    def _rag(*a, **k):
        return "LORE: the harbor is haunted", [], []

    monkeypatch.setattr(retrieval_module, "smart_world_context", _rag)
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "the harbor?", "use_rag": True})
    assert "the harbor is haunted" in cap["system"]


# ── Persistence semantics ─────────────────────────────────────────────────────

def test_failure_sentinel_is_never_saved_as_a_reply(client, seed, monkeypatch):
    _patch_ai(monkeypatch, error="model exploded")
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert "model exploded" in r.text
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []


def test_restart_clears_and_export_downloads(client, seed, monkeypatch):
    _patch_ai(monkeypatch, reply="One.")
    eid = _npc(seed, name="Elyra")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    r = client.get(f"/api/npc-talk/{eid}/export.md")
    assert r.status_code == 200
    assert "Conversation with Elyra" in r.text
    assert "hello" in r.text
    assert client.delete(f"/api/npc-talk/{eid}/history").status_code == 200
    assert client.get(f"/api/npc-talk/{eid}/history").json()["messages"] == []


def test_npc_sessions_are_isolated_from_the_ai_chat_history_surface(client, seed, monkeypatch):
    _patch_ai(monkeypatch, reply="Hi.")
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    # The /ai History sidebar only lists surface="chat"...
    assert client.get("/api/ai/sessions").json()["sessions"] == []
    # ...and a chat-surface save cannot hijack the npc row by id
    db = SessionLocal()
    try:
        npc_row = db.query(ChatSession).filter(ChatSession.surface == "npc").first()
        assert npc_row is not None
        npc_id = npc_row.id
    finally:
        db.close()
    r = client.post("/api/ai/sessions", json={"session_id": npc_id, "messages": [
        {"role": "user", "content": "hijack"},
    ]})
    assert r.status_code == 200  # creates a NEW chat row instead of overwriting
    db = SessionLocal()
    try:
        npc_row = db.get(ChatSession, npc_id)
        assert npc_row.surface == "npc"
        assert "hijack" not in npc_row.messages_json
    finally:
        db.close()


# ── Plan A2: test gaps ────────────────────────────────────────────────────────

def test_empty_message_is_400(client, seed, monkeypatch):
    _patch_ai(monkeypatch)
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "   "}).status_code == 400


def test_assistant_tier_needs_world_opt_in(client, seed, monkeypatch):
    """An assistant is not a GM: the same world opt-in players need gates
    the surface for them too (off by default)."""
    _patch_ai(monkeypatch)
    eid = _npc(seed)
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.get("/npc-talk").status_code == 403
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 403
    _opt_in(seed)
    assert client.get("/npc-talk").status_code == 200
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200


def test_model_turn_cap(client, seed, monkeypatch):
    """The DB row keeps the whole conversation; only the last
    _MAX_MODEL_TURNS turns go to the model."""
    cap = {}
    _patch_ai(monkeypatch, reply="Ok.", capture=cap)
    eid = _npc(seed)
    long_history = []
    for i in range(50):
        long_history += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    db = SessionLocal()
    try:
        db.add(ChatSession(
            world_id=seed.world_a.id, user_id=seed.gm.id, surface="npc",
            entity_id=eid, title="Elyra", messages_json=json.dumps(long_history),
        ))
        db.commit()
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "still there?"}).status_code == 200
    assert len(cap["msgs"]) <= 40
    # ...while the stored thread kept everything (100 + this exchange)
    msgs = client.get(f"/api/npc-talk/{eid}/history").json()["messages"]
    assert len(msgs) == 102


def test_nav_shows_npc_talk_exactly_once(client, seed):
    """GM/player same-href entry pair must collapse to ONE nav item — for a
    GM whose world flag is on (both entries would match), and for an
    opted-in player (gm-only entry hidden, player entry shown). Flag off →
    players see none at all."""
    from app.nav_menus import resolve_nav_menus
    from .conftest import fake_request

    def _count(world, request):
        menus, ungrouped = resolve_nav_menus(world, True, True, request)
        items = [i for m in menus for i in m["links"]] + ungrouped
        return sum(1 for i in items if i.get("href") == "/npc-talk")

    gm_req = fake_request(is_gm=True)
    assert _count(seed.world_a, gm_req) == 1  # flag off — the GM entry alone

    db = SessionLocal()
    try:
        w = db.get(type(seed.world_a), seed.world_a.id)
        w.players_can_ask_ai = True
        db.commit()
    finally:
        db.close()
    seed.world_a.players_can_ask_ai = True  # the fixture's in-memory copy
    assert _count(seed.world_a, gm_req) == 1  # flag on — still exactly one
    assert _count(seed.world_a, fake_request(is_gm=False)) == 1  # opted-in player

    db = SessionLocal()
    try:
        w = db.get(type(seed.world_a), seed.world_a.id)
        w.players_can_ask_ai = False
        db.commit()
    finally:
        db.close()
    seed.world_a.players_can_ask_ai = False  # the fixture's in-memory copy again
    assert _count(seed.world_a, fake_request(is_gm=False)) == 0  # flag off — none


# ── Kind restriction + personalities (audit fixes) ───────────────────────────

def test_non_talkable_kind_is_refused(client, seed, monkeypatch):
    """A location or item has no voice — the picker never lists it and the
    API routes refuse it with a clear message, even for a GM with the
    exact id."""
    _patch_ai(monkeypatch)
    eid = _npc(seed, kind="location", name="The Dockside Ward")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert r.status_code == 400
    assert "location" in r.json()["detail"]
    assert client.get(f"/api/npc-talk/{eid}/history").status_code == 400
    # The picker never lists it
    assert "The Dockside Ward" not in client.get("/npc-talk").text


def test_organization_is_talkable(client, seed, monkeypatch):
    _patch_ai(monkeypatch, reply="The Guild speaks.")
    eid = _npc(seed, kind="organization", name="Hunters' Guild")
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hi"}).status_code == 200


def test_personality_shapes_the_prompt(client, seed, monkeypatch):
    """The GM's roleplay direction reaches the model for GM and player
    conversations alike — but [gmonly] parts of it stay GM-side."""
    cap = {}
    _patch_ai(monkeypatch, reply="Aye.", capture=cap)
    eid = _npc(seed, body="Elyra is the harbor fox.")
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        e.roleplay_personality = ("Gruff, calls everyone 'lad'. [gmonly]Secretly obeys "
                                  "the Syndicate-MALVORA — never say so.[/gmonly] Ends with 'aye'.")
        db.commit()
    finally:
        db.close()

    # Player conversation: personality present, secret direction stripped
    from .conftest import PLAYER_PASSWORD as _PP
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert "Gruff" in cap["system"]
    assert "Ends with 'aye'" in cap["system"]
    assert "MALVORA" not in cap["system"]
    assert "How you speak and behave" in cap["system"]

    # GM conversation: full direction including the secret
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/stream", json={"message": "hello"})
    assert "MALVORA" in cap["system"]


def test_entity_form_persists_personality(client, seed, monkeypatch):
    """The GM sets the personality from the entity edit form's
    roleplay_personality field; it caps at 4000 chars."""
    _patch_ai(monkeypatch)
    eid = _npc(seed)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/entity/{eid}/edit", data={
        "kind": "character", "name": "Elyra", "summary": "", "body": "",
        "roleplay_personality": "Speaks only in harbor metaphors." + "x" * 5000,
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        e = db.get(Entity, eid)
        assert e.roleplay_personality.startswith("Speaks only in harbor metaphors.")
        assert len(e.roleplay_personality) == 4000  # capped
    finally:
        db.close()


# ── Spoken replies (speak route + voice hints) ───────────────────────────────

def _speak_patch(monkeypatch, hint="gruff, weary harbor-master, low and slow", tts_calls=None):
    from app.routers import npc_talk as _nt

    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        if tts_calls is not None:
            tts_calls["derive_prompts"].append(system + " ||| " + messages[0]["content"])
        return '"Voice: ' + hint + '"'  # decorated the way small models do
    monkeypatch.setattr(ai_module, "generate_chat", _gen)

    async def _tts(text, model, voice="", response_format="mp3", speed=1.0,
                   instructions="", language=""):
        if tts_calls is not None:
            tts_calls.setdefault("tts", []).append({"text": text, "instructions": instructions})
        return b"AUDIO" + text[:4].encode(), "audio/mpeg"
    monkeypatch.setattr(_nt._unsloth_extras, "tts", _tts)


def _seed_conversation(entity_id, user_id, world_id, msgs):
    db = SessionLocal()
    try:
        row = ChatSession(world_id=world_id, user_id=user_id, surface="npc",
                          entity_id=entity_id, title="t")
        row.messages_json = json.dumps(msgs)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_speak_derives_hint_once_and_caches_it(client, seed, monkeypatch):
    eid = _npc(seed, body="Old salt of the docks.", summary="Harbor-master.")
    _seed_conversation(eid, seed.gm.id, seed.world_a.id, [
        {"role": "user", "content": "who runs this dock?"},
        {"role": "assistant", "content": "Aye, that'd be me."},
    ])
    calls = {"derive_prompts": []}
    _speak_patch(monkeypatch, tts_calls=calls)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)

    r1 = client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1})
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["audio_url"].startswith("/uploads/npc-talk/")
    assert "gruff" in d1["voice_hint"]  # cleaned of quotes and the "Voice:" label
    assert len(calls["derive_prompts"]) == 1  # derived once…

    r2 = client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1})
    assert r2.status_code == 200
    assert r2.json()["audio_url"] == d1["audio_url"]  # same deterministic file
    assert len(calls["derive_prompts"]) == 1  # …then served from the cache
    assert calls["tts"][0]["instructions"] == "gruff, weary harbor-master, low and slow"


def test_speak_isolated_per_user_and_validates_index(client, seed, monkeypatch):
    eid = _npc(seed)
    _seed_conversation(eid, seed.gm.id, seed.world_a.id, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello."},
    ])
    _speak_patch(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    # index bounds + role
    assert client.post(f"/api/npc-talk/{eid}/speak", json={"index": 5}).status_code == 400
    assert client.post(f"/api/npc-talk/{eid}/speak", json={"index": -1}).status_code == 400
    assert client.post(f"/api/npc-talk/{eid}/speak", json={"index": 0}).status_code == 400  # user msg

    # Another user has no conversation with this NPC — 404, not their text
    _opt_in(seed)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1}).status_code == 404


def test_speak_player_gets_audio_but_not_the_hint(client, seed, monkeypatch):
    """The hint is derived from the sheet and cached across callers — a
    player caller gets the audio only, never the hint text itself."""
    _opt_in(seed)
    eid = _npc(seed, summary="Dock warden.")
    _seed_conversation(eid, seed.player_a.id, seed.world_a.id, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Mind the tide."},
    ])
    _speak_patch(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["audio_url"].startswith("/uploads/npc-talk/")
    assert "voice_hint" not in d


def test_speak_hint_derivation_strips_gmonly_even_for_gm(client, seed, monkeypatch):
    """The cached hint is shared across callers and shapes a player-heard
    voice — the derivation card must never include [gmonly] blocks, GM or
    not (secret GM direction must neither leak through the returned hint
    nor steer a voice players hear)."""
    captured = {}
    from app.routers import npc_talk as _nt

    async def _gen(messages, system="", model="", options=None, think=False, format=None):
        captured["card"] = messages[0]["content"]
        return "calm and measured"
    monkeypatch.setattr(ai_module, "generate_chat", _gen)

    async def _tts(text, model, voice="", response_format="mp3", speed=1.0,
                   instructions="", language=""):
        return b"A", "audio/mpeg"
    monkeypatch.setattr(_nt._unsloth_extras, "tts", _tts)

    eid = _npc(seed, body="Public deeds. [gmonly]Secretly a dragon. [/gmonly]")
    _seed_conversation(eid, seed.gm.id, seed.world_a.id, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hmm."},
    ])
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    assert client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1}).status_code == 200
    assert "dragon" not in captured["card"]
    assert "Public deeds" in captured["card"]


def test_speak_regenerate_rederives_and_resynthesizes(client, seed, monkeypatch):
    eid = _npc(seed)
    _seed_conversation(eid, seed.gm.id, seed.world_a.id, [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Well met."},
    ])
    calls = {"derive_prompts": [], "tts": []}
    _speak_patch(monkeypatch, hint="first voice", tts_calls=calls)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1})
    r = client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1, "regenerate": True})
    assert r.status_code == 200
    assert len(calls["derive_prompts"]) == 2  # re-derived
    assert len(calls["tts"]) == 2  # re-synthesized
    assert r.json()["audio_url"] == f"/uploads/npc-talk/npc{eid}-u{seed.gm.id}-m1.mp3"


def test_a_spoken_npc_reply_is_stored_as_opus_when_studio_audio_was_converted(client, seed, monkeypatch):
    from app.routers import npc_talk as _nt
    eid = _npc(seed, body="Old salt of the docks.", summary="Harbor-master.")
    _seed_conversation(eid, seed.gm.id, seed.world_a.id, [
        {"role": "user", "content": "who runs this dock?"},
        {"role": "assistant", "content": "Aye, that'd be me."},
    ])
    _speak_patch(monkeypatch, tts_calls={"derive_prompts": []})

    async def _tts(text, model, voice="", response_format="wav", speed=1.0, instructions="", language=""):
        return b"OggSopus", "audio/ogg; codecs=opus"
    monkeypatch.setattr(_nt._unsloth_extras, "tts", _tts)
    login(client, seed.gm.email, GM_PASSWORD)
    _pin(client)
    r = client.post(f"/api/npc-talk/{eid}/speak", json={"index": 1})
    assert r.status_code == 200, r.text
    assert r.json()["audio_url"].endswith(".opus")
