"""Tests for the per-world Whisper name glossary: GET/POST
/api/ai/whisper/glossary (app/routers/ai.py) and the "prompt" hint it
threads into app.ai.transcribe_audio's request to whisper.cpp, so campaign
NPC names/places bias transcription instead of getting autocorrected.

Also covers audio_jobs._glossary_for_world/entity_glossary_terms/
_merge_glossary: World entity names (characters, creatures, locations,
organizations, races, professions) are merged into whatever the GM typed
by hand at transcribe time — the saved World.whisper_glossary text itself
is untouched, so a GM doesn't have to keep it in sync as entities are
added/renamed, and every actual transcription call still gets the merged
result without the GM needing to do anything.
"""
import asyncio
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob, Entity, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, kind, name, subtype=None):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kind, subtype=subtype, name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


async def _await_terminal(job_id, timeout=5.0):
    deadline = time.time() + timeout
    db = SessionLocal()
    try:
        job = None
        while time.time() < deadline:
            db.expire_all()
            job = db.get(AudioJob, job_id)
            if job.status in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        raise AssertionError(f"job never reached a terminal status, last seen status={job.status!r}")
    finally:
        db.close()


# ── GET/POST /api/ai/whisper/glossary ───────────────────────────────────────

def test_glossary_empty_by_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/glossary")
    assert r.status_code == 200
    assert r.json() == {"glossary": "", "entity_terms_count": 0}


def test_glossary_save_and_roundtrip(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/whisper/glossary", json={"glossary": "Elyndra, Karrveth Hollow"})
    assert r.status_code == 200
    assert r.json()["glossary"] == "Elyndra, Karrveth Hollow"
    r2 = client.get("/api/ai/whisper/glossary")
    assert r2.json() == {"glossary": "Elyndra, Karrveth Hollow", "entity_terms_count": 0}


def test_glossary_save_strips_whitespace(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/glossary", json={"glossary": "  Elyndra  \n"})
    r = client.get("/api/ai/whisper/glossary")
    assert r.json()["glossary"] == "Elyndra"


def test_glossary_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/glossary")
    assert r.status_code == 403
    r2 = client.post("/api/ai/whisper/glossary", json={"glossary": "x"})
    assert r2.status_code == 403


def test_glossary_scoped_per_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/api/ai/whisper/glossary", json={"glossary": "world-a-only-name"})

    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/api/ai/whisper/glossary")
    assert r.json() == {"glossary": "", "entity_terms_count": 0}


# ── app.ai.transcribe_audio threads glossary through as "prompt" ───────────

class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = ""

    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, captured):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self._captured["data"] = kw.get("data")
        return _FakeResponse(200, {"text": "ok"})


@pytest.fixture(autouse=True)
def _reset_whisper_override():
    ai_module.set_whisper_override("")
    yield
    ai_module.set_whisper_override("")


@pytest.mark.asyncio
async def test_transcribe_audio_sends_glossary_as_prompt(tmp_path, monkeypatch):
    captured = {}
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    monkeypatch.setattr(ai_module, "_httpx", type("M", (), {"AsyncClient": staticmethod(lambda **kw: _FakeAsyncClient(captured))}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3")
    await ai_module.transcribe_audio(f, glossary="Elyndra, Karrveth Hollow")
    assert captured["data"]["prompt"] == "Elyndra, Karrveth Hollow"


@pytest.mark.asyncio
async def test_transcribe_audio_omits_prompt_when_no_glossary(tmp_path, monkeypatch):
    captured = {}
    ai_module.set_whisper_override("http://127.0.0.1:8090")
    monkeypatch.setattr(ai_module, "_httpx", type("M", (), {"AsyncClient": staticmethod(lambda **kw: _FakeAsyncClient(captured))}))
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3")
    await ai_module.transcribe_audio(f)
    assert "prompt" not in captured["data"]


# ── audio_jobs looks up the job's world glossary at transcription time ─────

@pytest.mark.asyncio
async def test_background_job_uses_worlds_glossary(client, seed, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_glossary = "Elyndra"
        db.commit()
    finally:
        db.close()

    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        captured["glossary"] = glossary
        return "the party met elyndra"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True, attachment_url="/uploads/x.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert captured.get("glossary") == "Elyndra"


# ── entity names merged into the glossary at transcribe time ───────────────

def test_entity_glossary_terms_covers_expected_kinds(client, seed):
    npc_id = _make_entity(seed.world_a.id, "character", "Gareth Ashfall", subtype="NPC")
    pc_id = _make_entity(seed.world_a.id, "character", "Aria Nightshade", subtype="PC")
    loc_id = _make_entity(seed.world_a.id, "location", "The Bazaar")
    note_id = _make_entity(seed.world_a.id, "note", "Session 3 planning")
    terms = audio_jobs.entity_glossary_terms(seed.world_a.id)
    assert "Gareth Ashfall" in terms
    assert "Aria Nightshade" in terms  # PC-subtype characters count too
    assert "The Bazaar" in terms
    assert "Session 3 planning" not in terms  # notes are excluded


def test_entity_glossary_terms_respects_limit(client, seed):
    for i in range(5):
        _make_entity(seed.world_a.id, "character", f"NPC {i}")
    terms = audio_jobs.entity_glossary_terms(seed.world_a.id, limit=3)
    assert len(terms) == 3


def test_merge_glossary_appends_entity_terms_after_gm_text():
    result = audio_jobs._merge_glossary("Elyndra, the Undermarket", ["Gareth Ashfall", "The Bazaar"])
    assert result == "Elyndra, the Undermarket, Gareth Ashfall, The Bazaar"


def test_merge_glossary_works_with_no_gm_text():
    result = audio_jobs._merge_glossary("", ["Gareth Ashfall", "The Bazaar"])
    assert result == "Gareth Ashfall, The Bazaar"


def test_merge_glossary_dedups_case_insensitively_against_gm_text():
    result = audio_jobs._merge_glossary("gareth ashfall, Elyndra", ["Gareth Ashfall", "The Bazaar"])
    assert result.lower().count("gareth ashfall") == 1
    assert "The Bazaar" in result


def test_glossary_for_world_merges_entity_names(client, seed):
    _make_entity(seed.world_a.id, "character", "Gareth Ashfall")
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.whisper_glossary = "Elyndra"
        db.commit()
    finally:
        db.close()
    merged = audio_jobs._glossary_for_world(seed.world_a.id)
    assert "Elyndra" in merged
    assert "Gareth Ashfall" in merged


@pytest.mark.asyncio
async def test_background_job_glossary_includes_entity_names_even_with_no_gm_glossary(client, seed, tmp_path, monkeypatch):
    """End-to-end through the job engine: an entity name reaches
    transcribe_audio's glossary kwarg even when the GM never typed
    anything into World.whisper_glossary at all."""
    _make_entity(seed.world_a.id, "character", "Gareth Ashfall")

    captured = {}

    async def fake_transcribe(path, glossary="", **kwargs):
        captured["glossary"] = glossary
        return "the party met gareth"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"x")
    job_id = audio_jobs.create_job(
        world_id=seed.world_a.id, purpose="attachment", filename="clip.mp3",
        audio_path=audio, delete_after=True, attachment_url="/uploads/x.mp3",
    )
    job = await _await_terminal(job_id)
    assert job.status == "done", job.error
    assert "Gareth Ashfall" in captured.get("glossary", "")


def test_whisper_glossary_route_reports_entity_terms_count(client, seed):
    _make_entity(seed.world_a.id, "character", "Gareth Ashfall")
    _make_entity(seed.world_a.id, "location", "The Bazaar")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/whisper/glossary")
    assert r.json()["entity_terms_count"] == 2
