"""A session recording can be transcribed first and summarized later: the job stops at status "transcribed" (review_before_summary), then the GM picks the
model and the summary settings (min/max tokens, strictness, Thinking, RAG, instructions) and the summary runs over the saved transcript."""
import asyncio
import time

import pytest

from app import ai as ai_module
from app import audio_jobs
from app.database import SessionLocal
from app.models import AudioJob

from .conftest import GM_PASSWORD, login

_real_summarize_transcript = ai_module.summarize_transcript  # the fixture below replaces the module's


@pytest.fixture(autouse=True)
def _fake_ai(monkeypatch):
    calls = {"summarize": []}

    async def fake_transcribe(path, glossary="", **kwargs):
        return "the party met elena at the bazaar"

    async def fake_summarize(transcript, model="", extra_instructions="", **kwargs):
        calls["summarize"].append({"transcript": transcript, "model": model, "extra": extra_instructions, **kwargs})
        return "The party met Elena."

    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ai_module, "summarize_transcript", fake_summarize)
    return calls


def _status(job_id):
    db = SessionLocal()
    try:
        return db.get(AudioJob, job_id).status
    finally:
        db.close()


async def _until(job_id, statuses, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if _status(job_id) in statuses:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job stayed {_status(job_id)!r}, wanted {statuses}")


@pytest.mark.asyncio
async def test_a_reviewed_recording_stops_after_transcribing_and_keeps_the_transcript(client, seed, tmp_path, _fake_ai):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake")
    job_id = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap", filename="clip.mp3", audio_path=audio, delete_after=True,
                                   review_before_summary=True)
    await _until(job_id, ("transcribed", "error"))
    db = SessionLocal()
    try:
        job = db.get(AudioJob, job_id)
        assert job.status == "transcribed", job.error
        assert job.transcript == "the party met elena at the bazaar" and not job.recap and job.finished_at is not None
    finally:
        db.close()
    assert _fake_ai["summarize"] == []  # nothing summarized yet
    assert not audio.exists()  # the recording is deleted as after any run
    await asyncio.sleep(0.1)
    assert _status(job_id) == "transcribed"


@pytest.mark.asyncio
async def test_other_purposes_and_unreviewed_jobs_run_straight_through(client, seed, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"fake")
    plain = audio_jobs.create_job(world_id=seed.world_a.id, purpose="session_recap", filename="a.mp3", audio_path=audio, delete_after=True)
    await _until(plain, ("done", "error"))
    assert _status(plain) == "done"
    audio2 = tmp_path / "b.mp3"
    audio2.write_bytes(b"fake")
    att = audio_jobs.create_job(world_id=seed.world_a.id, purpose="attachment", filename="b.mp3", audio_path=audio2, delete_after=True, review_before_summary=True)
    await _until(att, ("done", "error"))
    assert _status(att) == "done"


def test_summarizing_a_transcribed_job_uses_the_chosen_settings(client, seed, tmp_path, _fake_ai):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribed", filename="x.mp3", transcript="hello there", review_before_summary=True)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data={
        "model": "llama3.1", "extra_instructions": "keep names", "think": "false", "min_tokens": "300", "max_tokens": "900", "strictness": "firm",
        "use_rag": "true", "rag_entity_limit": "7", "rag_notes_limit": "3"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "summarizing"
    end = time.time() + 5
    while time.time() < end and _status(job_id) != "done":
        time.sleep(0.02)
    assert _status(job_id) == "done"
    c = _fake_ai["summarize"][0]
    assert (c["model"], c["min_tokens"], c["max_tokens"], c["strictness"], c["think"]) == ("llama3.1", 300, 900, "firm", False)
    assert "keep names" in c["extra"]
    d = client.get(f"/api/audio-jobs/{job_id}").json()
    assert (d["min_tokens"], d["max_tokens"], d["strictness"], d["use_rag"], d["rag_entity_limit"], d["rag_notes_limit"]) == (300, 900, "firm", True, 7, 3)


def test_bad_summary_settings_are_refused(client, seed):
    db = SessionLocal()
    try:
        job = AudioJob(world_id=seed.world_a.id, purpose="session_recap", status="transcribed", filename="x.mp3", transcript="hello")
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for data in ({"min_tokens": "900", "max_tokens": "300"}, {"max_tokens": "lots"}, {"strictness": "strict"}):
        r = client.post(f"/api/audio-jobs/{job_id}/resummarize", data=data)
        assert r.status_code == 400, (data, r.text)
    assert _status(job_id) == "transcribed"  # a refused request changes nothing


@pytest.mark.asyncio
async def test_the_length_settings_reach_the_prompt_and_the_token_cap(monkeypatch):
    seen = []

    async def fake_chat(messages, system="", model="", options=None, think=False, **kw):
        seen.append({"system": system, "options": options})
        return "ok"

    monkeypatch.setattr(ai_module, "generate_chat", fake_chat)
    await _real_summarize_transcript("a short transcript", model="m", think=False, min_tokens=200, max_tokens=800, strictness="firm")
    s = seen[-1]
    assert "REQUIRED" in s["system"] and "~200 tokens" in s["system"] and "~800 tokens" in s["system"]
    assert s["options"]["num_predict"] == 800
    # nothing set: the prompt is exactly what it was before
    await _real_summarize_transcript("a short transcript", model="m", think=False)
    assert "Length" not in seen[-1]["system"]
