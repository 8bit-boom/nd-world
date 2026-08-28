"""Regression test: GET /api/ai/attachments/audio-jobs and
GET /api/ai/attachments/audio-jobs/{id} filtered only by world_id+purpose,
not by uploader — with World.players_can_ask_ai on, any player could read
another user's (including the GM's) voice-memo transcripts. Non-GM callers
must now only see their own AudioJob attachment rows."""
import pytest

from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _enable_ask_ai(world_id):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.players_can_ask_ai = True
        db.commit()
    finally:
        db.close()


def _make_attachment_job(world_id, filename, created_by_user_id, tmp_path):
    from app import audio_jobs
    audio = tmp_path / filename
    audio.write_bytes(b"x")
    return audio_jobs.create_job(
        world_id=world_id, purpose="attachment", filename=filename,
        audio_path=audio, delete_after=True, attachment_url=f"/uploads/{filename}",
        created_by_user_id=created_by_user_id,
    )


@pytest.mark.asyncio
async def test_player_only_sees_their_own_attachment_jobs_in_the_list(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    player_job_id = _make_attachment_job(seed.world_a.id, "player-memo.mp3", seed.player_a.id, tmp_path)

    r = client.get("/api/ai/attachments/audio-jobs")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()]
    assert player_job_id in ids
    assert gm_job_id not in ids


@pytest.mark.asyncio
async def test_player_gets_404_fetching_another_users_attachment_job(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/ai/attachments/audio-jobs/{gm_job_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_gm_still_sees_every_attachment_job(client, seed, tmp_path):
    _enable_ask_ai(seed.world_a.id)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    gm_job_id = _make_attachment_job(seed.world_a.id, "gm-memo.mp3", seed.gm.id, tmp_path)
    player_job_id = _make_attachment_job(seed.world_a.id, "player-memo.mp3", seed.player_a.id, tmp_path)

    r = client.get("/api/ai/attachments/audio-jobs")
    ids = [j["id"] for j in r.json()]
    assert gm_job_id in ids
    assert player_job_id in ids
    assert client.get(f"/api/ai/attachments/audio-jobs/{player_job_id}").status_code == 200
