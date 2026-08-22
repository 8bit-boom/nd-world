"""Tests for the AI chat/Ask AI attachment picker: POST
/api/ai/attachments/upload (app/routers/ai.py) and how an attachment gets
folded into the outgoing Ollama message in _build_ollama_messages. Ollama
itself is mocked out — these exercise the permission gate, the upload/
extraction pipeline, and the message-building logic, not the model.
"""
import io

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import World
from app.routers import ai as ai_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
_TXT_BYTES = b"The secret door is behind the waterfall."
_MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 200


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _upload_file(client, name, data, content_type):
    return client.post("/api/ai/attachments/upload", files={"file": (name, io.BytesIO(data), content_type)})


# ── Upload endpoint: permission gate ────────────────────────────────────────

def test_attachment_upload_gm_always_allowed(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "note.txt", _TXT_BYTES, "text/plain")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "document"
    assert body["text"] == _TXT_BYTES.decode()
    assert body["url"].startswith("/uploads/ai_attachments/")


def test_attachment_upload_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "note.txt", _TXT_BYTES, "text/plain")
    assert r.status_code == 403


def test_attachment_upload_player_allowed_once_gm_enables_it(client, seed):
    _set_world(seed.world_a.id, players_can_ask_ai=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "note.txt", _TXT_BYTES, "text/plain")
    assert r.status_code == 200


# ── Upload endpoint: file handling ──────────────────────────────────────────

def test_attachment_upload_image_kind_and_no_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "portrait.png", _PNG_BYTES, "image/png")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image"
    assert body["text"] == ""


def test_attachment_upload_audio_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "clip.mp3", _MP3_BYTES, "audio/mpeg")
    assert r.status_code == 200
    assert r.json()["kind"] == "audio"


def test_attachment_upload_rejects_unsupported_extension(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "evil.exe", b"MZ", "application/octet-stream")
    assert r.status_code == 400


def test_attachment_upload_rejects_file_over_configured_limit(client, seed, monkeypatch):
    monkeypatch.setattr(ai_router, "_MAX_ATTACHMENT_BYTES", 10)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "note.txt", _TXT_BYTES, "text/plain")
    assert r.status_code == 413


def test_attachment_upload_pdf_extracts_text_without_crashing(client, seed, tmp_path):
    from pypdf import PdfWriter

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "handout.pdf", pdf_path.read_bytes(), "application/pdf")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "document"
    assert body["text"] == ""  # blank page has no text, but extraction shouldn't error


# ── _build_ollama_messages: how attachments reach the model ────────────────

def test_document_attachment_text_is_folded_into_content():
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    msgs = [ChatMessage(role="user", content="What's in the notes?", attachments=[
        ChatAttachment(kind="document", url="/uploads/ai_attachments/x.txt", name="notes.txt",
                       text="The secret door is behind the waterfall."),
    ])]
    built = _build_ollama_messages(msgs)
    assert len(built) == 1
    assert "notes.txt" in built[0]["content"]
    assert "The secret door is behind the waterfall." in built[0]["content"]
    assert "images" not in built[0]


def test_audio_attachment_is_noted_and_not_sent_as_images(tmp_path, monkeypatch):
    """No disk path resolves (no such file), so the attachment can only
    produce the text note — confirms it never gets misfiled into `images`
    regardless of whether the encode step succeeds."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/x.mp3", name="growl.mp3"),
    ])]
    built = _build_ollama_messages(msgs)
    assert "growl.mp3" in built[0]["content"]
    assert "images" not in built[0]
    assert "audio" not in built[0]  # file doesn't actually exist on disk


def test_audio_attachment_is_base64_encoded_into_audio_field(tmp_path, monkeypatch):
    """Best-effort real audio support (e.g. for an audio-native model like
    Gemma 3n) — see _build_ollama_messages' docstring for why an unrecognized
    `audio` key still reaches Ollama's server instead of being stripped."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages
    import base64

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    audio_dir = tmp_path / "ai_attachments"
    audio_dir.mkdir()
    (audio_dir / "growl.mp3").write_bytes(_MP3_BYTES)

    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/growl.mp3", name="growl.mp3"),
    ])]
    built = _build_ollama_messages(msgs)
    assert built[0]["audio"] == [base64.b64encode(_MP3_BYTES).decode()]
    assert "images" not in built[0]
    assert "growl.mp3" in built[0]["content"]


def test_image_attachment_is_base64_encoded_into_images_field(tmp_path, monkeypatch):
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    img_dir = tmp_path / "ai_attachments"
    img_dir.mkdir()
    (img_dir / "pic.png").write_bytes(_PNG_BYTES)

    msgs = [ChatMessage(role="user", content="What is this?", attachments=[
        ChatAttachment(kind="image", url="/uploads/ai_attachments/pic.png", name="pic.png"),
    ])]
    built = _build_ollama_messages(msgs)
    assert built[0]["content"] == "What is this?"
    assert built[0]["images"] == [__import__("base64").b64encode(_PNG_BYTES).decode()]


def test_image_attachment_path_traversal_is_ignored(tmp_path, monkeypatch):
    """A crafted url trying to escape the uploads root must not be read —
    matches the same guard shape as audio.py's _delete_clip_file."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(_PNG_BYTES)

    msgs = [ChatMessage(role="user", content="hi", attachments=[
        ChatAttachment(kind="image", url="/uploads/../secret.png", name="secret.png"),
    ])]
    built = _build_ollama_messages(msgs)
    assert "images" not in built[0]


# ── /api/ai/stream end to end with an attachment ────────────────────────────

async def _fake_resolve_model(requested):
    return requested or "fake-model"


async def _fake_stream_chat(messages, system="", model=""):
    for tok in ["ok"]:
        yield tok


def test_ai_stream_accepts_message_with_attachments(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "resolve_model", _fake_resolve_model)
    monkeypatch.setattr(ai_module, "stream_chat", _fake_stream_chat)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/stream", json={
        "messages": [{
            "role": "user", "content": "hi",
            "attachments": [{"kind": "document", "url": "/uploads/ai_attachments/x.txt",
                              "name": "x.txt", "text": "some notes"}],
        }],
    })
    assert r.status_code == 200
    assert "ok" in r.text
