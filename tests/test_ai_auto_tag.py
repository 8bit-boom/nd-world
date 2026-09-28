"""Tests for bulk AI auto-tagging (entities + notes) — the entity list's
bulk bar feature. The AI is monkeypatched (quest-sync convention); the tests
pin the important contracts: fill-empty mode never touches GM-written tags,
hallucinated ids are dropped, tags are normalized (deduped/trimmed/capped),
overwrite replaces, and the job reports progress."""
import re

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _mk_entity(world_id, name, kind="character", summary="", tags=None, body=""):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, name=name, kind=kind, summary=summary,
                   tags=tags, body=body)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _entity_tags(entity_id):
    db = SessionLocal()
    try:
        return db.get(Entity, entity_id).tags
    finally:
        db.close()


def _patch_generate(monkeypatch, reply):
    from app.routers import ai as ai_module

    async def fake_generate_chat(messages, **kw):
        return reply(messages, kw)

    monkeypatch.setattr(ai_module._ai, "generate_chat", fake_generate_chat)


def _start(client, payload):
    return client.post("/api/ai/auto-tag/start", json=payload)


def _poll(client, job_id, seconds=10):
    import time as _time

    deadline = _time.time() + seconds
    data = {"status": "running"}
    while _time.time() < deadline:
        data = client.get(f"/api/ai/auto-tag/{job_id}").json()
        if data["status"] != "running":
            break
        _time.sleep(0.05)
    return data


def test_auto_tag_start_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _start(client, {"entity_ids": [1]}).status_code == 403


def test_auto_tag_start_requires_ids(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _start(client, {"entity_ids": []}).status_code == 400
    assert _start(client, {}).status_code == 400


def test_auto_tag_poll_unknown_job_404(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/api/ai/auto-tag/999999").status_code == 404


def test_auto_tag_fill_empty_never_touches_gm_tags(client, seed, monkeypatch):
    """The core promise of fill-empty mode: entities with tags keep theirs
    exactly, untagged ones get AI tags, and hallucinated ids are dropped."""
    vex_id = _mk_entity(seed.world_a.id, "Vex Rowan", summary="Rogue",
                        body="A quick blade and quicker tongue.")
    tagged_id = _mk_entity(seed.world_a.id, "Mirabel Vane", kind="note",
                           tags="hand-written, keep-me")

    def reply(messages, kw):
        assert kw.get("think") is True  # default thinking on
        ids = re.findall(r"id=(\d+)", messages[0]["content"])
        # the tagged entity must not even be sent to the model in fill-empty
        assert str(tagged_id) not in ids
        out = [{"id": int(i), "tags": ["rogue", "Rogue", "", "quick", "x" * 60]}
               for i in ids]
        out.append({"id": 999999, "tags": ["hallucinated"]})
        return '{"entities": ' + repr(out).replace("'", '"') + '}'

    _patch_generate(monkeypatch, reply)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"entity_ids": [vex_id, tagged_id]})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1  # only the untagged one is work
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done", data
    assert data["processed"] == 1 and data["tagged"] == 1
    # Vex got normalized tags (dup case dropped, empty dropped, 40-char cap)
    tags = _entity_tags(vex_id)
    assert "rogue" in tags and "quick" in tags
    assert "Rogue" not in tags.lower().replace("rogue", "ROGUE", 1) or tags.lower().count("rogue") == 1
    assert ("x" * 40) in tags and ("x" * 60) not in tags
    # Mirabel's hand-written tags untouched
    assert _entity_tags(tagged_id) == "hand-written, keep-me"


def test_auto_tag_overwrite_replaces(client, seed, monkeypatch):
    ent_id = _mk_entity(seed.world_a.id, "Mirabel Vane", kind="note",
                        tags="old, stale")

    def reply(messages, kw):
        ids = re.findall(r"id=(\d+)", messages[0]["content"])
        assert str(ent_id) in ids  # overwrite mode sends tagged entities too
        return ('{"entities": [{"id": ' + str(ent_id) +
                ', "tags": ["journal", "fresh"]}]}')

    _patch_generate(monkeypatch, reply)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"entity_ids": [ent_id], "overwrite": True, "think": False})
    assert r.status_code == 200
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done"
    assert _entity_tags(ent_id) == "journal, fresh"


def test_auto_tag_error_path(client, seed, monkeypatch):
    _mk_entity(seed.world_a.id, "Vex Rowan")

    async def exploding_generate_chat(messages, **kw):
        raise RuntimeError("backend down")

    from app.routers import ai as ai_module
    monkeypatch.setattr(ai_module._ai, "generate_chat", exploding_generate_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"entity_ids": [1]})
    assert r.status_code == 200
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "error"
    assert "backend down" in data["error"]


def test_auto_tag_think_off_reaches_model(client, seed, monkeypatch):
    ent_id = _mk_entity(seed.world_a.id, "Vex Rowan")
    seen = {}

    def reply(messages, kw):
        seen["think"] = kw.get("think")
        return '{"entities": [{"id": ' + str(ent_id) + ', "tags": ["rogue"]}]}'

    _patch_generate(monkeypatch, reply)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _start(client, {"entity_ids": [ent_id], "think": False})
    assert r.status_code == 200
    data = _poll(client, r.json()["job_id"])
    assert data["status"] == "done"
    assert seen["think"] is False
    assert _entity_tags(ent_id) == "rogue"
