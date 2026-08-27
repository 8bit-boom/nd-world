"""Tests for the chunked AI-attachment upload path (POST
/api/ai/attachments/upload/chunk + POST /api/ai/attachments/upload/complete,
app/routers/ai.py) — lets a large voice-memo attachment (Whisper Test tab,
AI Chat compose bar, entity Ask AI panel) clear Cloudflare's fixed 100MB
free-tier request-body cap, by splitting it client-side (static/js/
chunked-upload.js's ndChunkedUpload) into sub-100MB parts sent as separate
requests and reassembled here. Same pattern as
tests/test_audio_chunked_upload.py, driving the two routes directly rather
than through the client-side splitting logic, which is plain JS.
"""
import io

from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PART_A = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xaa" * 5000
_PART_B = b"\xbb" * 5000


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _chunk_file(data):
    return {"file": ("part", io.BytesIO(data), "application/octet-stream")}


def _upload_two_chunks(client, upload_id):
    r0 = client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                      files=_chunk_file(_PART_A))
    r1 = client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                      files=_chunk_file(_PART_B))
    return r0, r1


def test_chunk_upload_requires_ask_ai_access(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/upload/chunk", data={"upload_id": "a" * 32, "chunk_index": "0"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 403


def test_chunk_upload_rejects_invalid_upload_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/upload/chunk", data={"upload_id": "not-a-hex-id", "chunk_index": "0"},
                     files=_chunk_file(_PART_A))
    assert r.status_code == 400


def test_complete_requires_ask_ai_access(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": "c" * 32, "filename": "x.mp3", "total_chunks": "1",
    })
    assert r.status_code == 403


def test_complete_rejects_bad_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "d" * 32
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "evil.exe", "total_chunks": "1",
    })
    assert r.status_code == 400


def test_complete_rejects_when_parts_missing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "e" * 32
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "x.mp3", "total_chunks": "2",
    })
    assert r.status_code == 400


def test_chunked_upload_reassembles_audio_and_transcribes(client, seed, monkeypatch):
    from app import ai as ai_module

    async def fake_transcribe(path, glossary="", language=""):
        assert path.read_bytes() == _PART_A + _PART_B
        return "reassembled transcript"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "f" * 32
    r0, r1 = _upload_two_chunks(client, upload_id)
    assert r0.status_code == 200
    assert r1.status_code == 200

    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "big-clip.mp3", "total_chunks": "2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "audio"
    assert body["text"] == "reassembled transcript"
    assert body["url"].startswith("/uploads/ai_attachments/")

    # Chunk temp files are cleaned up after a successful completion.
    from app.routers.ai import _attach_chunks_root
    assert not (_attach_chunks_root() / upload_id).exists()


def test_chunked_upload_reassembles_document_bytes_exactly(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "1" * 32
    part_a, part_b = b"The secret door ", b"is behind the waterfall."
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files={"file": ("part", io.BytesIO(part_a), "application/octet-stream")})
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                 files={"file": ("part", io.BytesIO(part_b), "application/octet-stream")})
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "notes.txt", "total_chunks": "2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "document"
    assert body["text"] == "The secret door is behind the waterfall."


def test_chunked_upload_rejects_when_reassembled_document_exceeds_limit(client, seed, monkeypatch):
    import app.routers.ai as ai_router
    monkeypatch.setattr(ai_router, "_MAX_ATTACHMENT_BYTES", len(_PART_A))  # smaller than PART_A + PART_B combined

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "2" * 32
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "0"},
                 files=_chunk_file(_PART_A))
    client.post("/api/ai/attachments/upload/chunk", data={"upload_id": upload_id, "chunk_index": "1"},
                 files=_chunk_file(_PART_B))
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "toobig.txt", "total_chunks": "2",
    })
    assert r.status_code == 413


def test_chunked_upload_player_allowed_once_gm_enables_ask_ai(client, seed, monkeypatch):
    from app import ai as ai_module

    async def fake_transcribe(path, glossary="", language=""):
        return "ok"
    monkeypatch.setattr(ai_module, "transcribe_audio", fake_transcribe)

    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    upload_id = "3" * 32
    _upload_two_chunks(client, upload_id)
    r = client.post("/api/ai/attachments/upload/complete", data={
        "upload_id": upload_id, "filename": "voice.mp3", "total_chunks": "2",
    })
    assert r.status_code == 200
