"""Tests for the modernization + find/timer features."""
p = 'tests/test_live_cockpit.py'
src = open(p, encoding='utf-8').read()

def rep(old, new, tag):
    global src
    if new in src:
        print("SKIP", tag)
        return
    assert src.count(old) == 1, tag + " count=" + str(src.count(old))
    src = src.replace(old, new, 1)
    print("OK  ", tag)

# 1) sanitizer: find + timer roundtrip
rep('''        {"id": "f1", "type": "find", "ref": "", "title": "Find", "x": 0, "y": 0,
         "w": 480, "h": 540, "z": 11},
    ]}, "presets": {}}''',
'''        {"id": "f1", "type": "find", "ref": "", "title": "Find", "x": 0, "y": 0,
         "w": 480, "h": 540, "z": 11},
        {"id": "t1", "type": "timer", "ref": "", "title": "Round timer", "x": 0, "y": 0,
         "w": 340, "h": 260, "z": 12},
    ]}, "presets": {}}''',
"sanitizer +find +timer")

rep('''    assert types == ["ecard", "tables", "combat", "calendar", "wmap",
                     "gallery", "audio", "video", "imagestudio", "ai_chat", "find"]''',
'''    assert types == ["ecard", "tables", "combat", "calendar", "wmap",
                     "gallery", "audio", "video", "imagestudio", "ai_chat",
                     "find", "timer"]''',
"sanitizer types list")

# 2) shell: modernized UI markers
rep('''    assert 'id="ck-find-btn"' in html
    assert "Find using AI" in html''',
'''    assert 'id="ck-find-btn"' in html
    assert "Find using AI" in html''', "no-op guard") if False else None

src2 = src  # keep flow simple: do the shell rep directly below
old_shell = '''    assert "ai_chat:" in html                     # native streaming chat panel type
    assert 'id="ck-find-btn"' in html             # toolbar Find-AI button
    assert "Find using AI" in html                # find panel type in the modal'''
new_shell = '''    assert "ai_chat:" in html                     # native streaming chat panel type
    assert 'id="ck-find-btn"' in html             # toolbar Find-AI button
    assert "Find using AI" in html                # find panel type in the modal
    assert 'id="ck-fs-btn"' in html               # fullscreen (table mode)
    assert 'id="ck-status"' in html               # status bar
    assert 'id="ck-welcome-tiles"' in html        # quick-start tiles
    assert "timer:" in html                       # timer panel type
    assert "ck-toasts" in html or "ck-toast" in html  # toast CSS shipped'''
assert src2.count(old_shell) == 1, "shell anchor"
src = src2.replace(old_shell, new_shell, 1)
print("OK   shell modernization asserts")

# 3) find route tests appended at the end
src += '''

# ── AI find / connections ────────────────────────────────────────────────────

def _find_mk_entity(world_id, name, kind="character", summary=""):
    from app.models import Entity

    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, name=name, kind=kind, summary=summary)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _find_poll(client, job_id, seconds=10):
    import time as _time

    deadline = _time.time() + seconds
    data = {"status": "running"}
    while _time.time() < deadline:
        data = client.get(f"/api/cockpit/ai/find/{job_id}").json()
        if data["status"] != "running":
            break
        _time.sleep(0.05)
    return data


def _patch_ai(monkeypatch, reply):
    """Patch the module attrs cockpit's task actually reads — generate_chat
    plus the API key the start route gates on (the quest-sync test pattern:
    effective_llm_api_key() reads module attrs captured at import)."""
    from app.routers import cockpit as cockpit_module

    async def fake_generate_chat(messages, **kw):
        return reply(messages, kw)

    monkeypatch.setattr(cockpit_module._ai, "generate_chat", fake_generate_chat)
    monkeypatch.setattr(cockpit_module._ai, "UNSLOTH_API_KEY", "sk-test")


def test_ai_find_start_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/cockpit/ai/find/start", json={"query": "x"}).status_code == 403


def test_ai_find_start_needs_query_or_entity(client, seed):
    _login_gm(client, seed)
    assert client.post("/api/cockpit/ai/find/start", json={}).status_code == 400


def test_ai_find_poll_unknown_job_404(client, seed):
    _login_gm(client, seed)
    assert client.get("/api/cockpit/ai/find/999999").status_code == 404


def test_ai_find_flow_filters_hallucinations_and_enriches(client, seed, monkeypatch):
    vex_id = _find_mk_entity(seed.world_a.id, "Vex Rowan", summary="Rogue with debts")
    _find_mk_entity(seed.world_a.id, "Yorm Barkeep", summary="Runs the Prancing Pony")

    def reply(messages, kw):
        # the surface contract: thinking + RAG hardcoded on server-side
        assert kw.get("think") is True
        assert "=== FOCUS ===" in messages[0]["content"]
        assert "CANDIDATES" in messages[0]["content"]
        return ('{"results": [{"id": 424242, "reason": "hallucinated"}, '
                '{"id": ' + str(vex_id) + ', "reason": "owes the barkeep 5 gold"}]}')

    _patch_ai(monkeypatch, reply)
    _login_gm(client, seed)
    r = client.post("/api/cockpit/ai/find/start", json={"query": "who owes whom money?"})
    assert r.status_code == 200, r.text
    data = _find_poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    # the hallucinated id was filtered, the real one enriched from the DB
    assert len(data["results"]) == 1
    res = data["results"][0]
    assert res["id"] == vex_id and res["name"] == "Vex Rowan"
    assert res["reason"] == "owes the barkeep 5 gold"


def test_ai_find_connections_excludes_self(client, seed, monkeypatch):
    vex_id = _find_mk_entity(seed.world_a.id, "Vex Rowan")
    note_id = _find_mk_entity(seed.world_a.id, "Debt Ledger", kind="note")

    def reply(messages, kw):
        # the model "suggests" the focused entity itself — must be dropped
        return ('{"results": [{"id": ' + str(vex_id) + ', "reason": "itself"}, '
                '{"id": ' + str(note_id) + ', "reason": "ledger mentions Vex"}]}')

    _patch_ai(monkeypatch, reply)
    _login_gm(client, seed)
    r = client.post("/api/cockpit/ai/find/start", json={"entity_id": vex_id})
    assert r.status_code == 200
    data = _find_poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    ids = [x["id"] for x in data["results"]]
    assert vex_id not in ids and note_id in ids


def test_ai_find_error_path(client, seed, monkeypatch):
    def reply(messages, kw):
        return "the model rambled without any JSON"

    _patch_ai(monkeypatch, reply)
    _login_gm(client, seed)
    r = client.post("/api/cockpit/ai/find/start", json={"query": "anything"})
    assert r.status_code == 200
    data = _find_poll(client, r.json()["job_id"])
    assert data["status"] == "error"
    assert data["error"]
'''
open(p, 'w', encoding='utf-8', newline='\n').write(src)
print("TESTS UPDATED")
