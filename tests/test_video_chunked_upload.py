"""Tests for the chunked video upload path (POST /video/upload/chunk +
POST /video/upload/complete, app/routers/video.py) — same purpose and
shape as tests/test_audio_chunked_upload.py: lets a file over
Cloudflare's fixed 100 MB free-tier request-body cap still get uploaded,
split client-side into sub-100MB parts sent as separate requests and
reassembled here. Unlike audio.py (which duplicates this logic locally),
video.py delegates to app/uploads.py's shared save_upload_chunk/
reassemble_upload_chunks, so the upload-id/chunk-index validation and the
stale-session sweep are read from there rather than a video-module-local
constant.
"""
import io
import os
import time

from app.database import SessionLocal
from app.models import VideoClip

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PART_A = b"\x00\x00\x00\x18ftypmp42" + b"\xaa" * 5000
_PART_B = b"\xbb" * 5000


def _chunk_file(data):
    return {"file": ("part", io.BytesIO(data), "application/octet-stream")}


def _upload_two_chunks(client, upload_id):
    r0 = client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files=_chunk_file(_PART_A))
    r1 = client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files=_chunk_file(_PART_B))
    return r0, r1


def test_chunk_upload_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload/chunk", data={"upload_id": "a" * 32, "chunk_index": "0"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 403


def test_chunk_upload_rejects_invalid_upload_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload/chunk", data={"upload_id": "not-a-hex-id", "chunk_index": "0"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 400


def test_chunk_upload_rejects_out_of_range_index(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload/chunk", data={"upload_id": "b" * 32, "chunk_index": "-1"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 400
    r = client.post("/video/upload/chunk", data={"upload_id": "b" * 32, "chunk_index": "999999"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 400


def test_complete_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/video/upload/complete", data={
        "upload_id": "c" * 32, "filename": "x.mp4", "total_chunks": "1",
    })
    assert r.status_code == 403


def test_complete_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "d" * 32
    client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "evil.exe", "total_chunks": "1",
    })
    assert r.status_code == 400


def test_complete_rejects_when_parts_missing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "e" * 32
    client.post("/video/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    # Claim 2 parts but only chunk 0 was ever uploaded.
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "x.mp4", "total_chunks": "2",
    })
    assert r.status_code == 400


def test_chunked_upload_reassembles_bytes_exactly(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "f" * 32
    r0, r1 = _upload_two_chunks(client, upload_id)
    assert r0.status_code == 200
    assert r1.status_code == 200

    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "big-session.mp4", "total_chunks": "2",
        "description": "Recorded session", "visible_to_players": "1",
    })
    assert r.status_code == 200

    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip is not None
        assert clip.name == "big-session"
        assert clip.description == "Recorded session"
        assert clip.visible_to_players is True
        stored_path = tmp_path / clip.file_url[len("/uploads/"):]
        assert stored_path.is_file()
        assert stored_path.read_bytes() == _PART_A + _PART_B
    finally:
        db.close()

    # Chunk temp files are cleaned up after a successful completion.
    chunks_dir = tmp_path / "video" / "_chunks" / upload_id
    assert not chunks_dir.exists()


def test_chunked_upload_honors_custom_name_and_album(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")
    from .test_video import _add_album
    album_id = _add_album(seed.world_a.id, name="Big Recordings")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "1" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "raw.mp4", "total_chunks": "2",
        "name": "Session 4 Recording", "album_id": str(album_id),
    })
    assert r.status_code == 200

    db = SessionLocal()
    try:
        clip = db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).first()
        assert clip.name == "Session 4 Recording"
        assert clip.album_id == album_id
    finally:
        db.close()


def test_chunked_upload_rejects_when_reassembled_total_exceeds_limit(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")
    monkeypatch.setattr(video_module, "_MAX_VIDEO_BYTES", len(_PART_A))  # smaller than PART_A + PART_B combined

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "2" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "toobig.mp4", "total_chunks": "2",
    })
    assert r.status_code == 413

    db = SessionLocal()
    try:
        assert db.query(VideoClip).filter(VideoClip.world_id == seed.world_a.id).count() == 0
    finally:
        db.close()
    # No partial final file and no leftover chunk dir.
    assert list((tmp_path / "video").glob("*.mp4")) == []
    assert not (tmp_path / "video" / "_chunks" / upload_id).exists()


def test_chunked_upload_respects_max_clips_per_world(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")
    monkeypatch.setattr(video_module, "_MAX_CLIPS_PER_WORLD", 0)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "3" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/video/upload/complete", data={
        "upload_id": upload_id, "filename": "x.mp4", "total_chunks": "2",
    })
    assert r.status_code == 400


def test_stale_chunk_session_is_swept_when_a_new_one_starts(client, seed, tmp_path, monkeypatch):
    import app.routers.video as video_module
    import app.uploads as uploads_module
    monkeypatch.setattr(video_module, "_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(video_module, "_CHUNKS_ROOT", tmp_path / "video" / "_chunks")
    monkeypatch.setattr(uploads_module, "STALE_CHUNK_SESSION_SECONDS", 1)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    stale_id = "4" * 32
    client.post("/video/upload/chunk", data={"upload_id": stale_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    stale_dir = tmp_path / "video" / "_chunks" / stale_id
    assert stale_dir.is_dir()
    old_time = time.time() - 10
    os.utime(stale_dir, (old_time, old_time))

    time.sleep(1.1)
    new_id = "5" * 32
    client.post("/video/upload/chunk", data={"upload_id": new_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))

    assert not stale_dir.exists()
    assert (tmp_path / "video" / "_chunks" / new_id).is_dir()
