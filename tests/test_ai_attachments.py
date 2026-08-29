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
    assert r.json()["text"] == ""  # Whisper not configured in this test — no crash, just no transcript


def test_attachment_upload_audio_is_transcribed_when_whisper_available(client, seed, monkeypatch):
    async def _fake_transcribe(path, glossary="", language=""):
        return "the secret door is behind the waterfall"
    monkeypatch.setattr(ai_router._ai, "transcribe_audio", _fake_transcribe)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "clip.mp3", _MP3_BYTES, "audio/mpeg")
    assert r.status_code == 200
    assert r.json()["text"] == "the secret door is behind the waterfall"


def test_attachment_upload_audio_applies_the_worlds_glossary_and_language(client, seed, monkeypatch):
    """The direct/blocking attachment-upload path used to call
    transcribe_audio with no glossary/language at all — unlike every other
    transcription call site including the background-job version of this
    same attachment flow — so the same voice memo would transcribe
    correctly as a background job but get force-decoded as English (or
    without campaign-name biasing) when uploaded directly."""
    _set_world(seed.world_a.id, whisper_glossary="Aldric, Vaelthorne", whisper_language="ru")
    received = {}

    async def _fake_transcribe(path, glossary="", language=""):
        received["glossary"] = glossary
        received["language"] = language
        return "ok"
    monkeypatch.setattr(ai_router._ai, "transcribe_audio", _fake_transcribe)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _upload_file(client, "clip.mp3", _MP3_BYTES, "audio/mpeg")
    assert r.status_code == 200
    assert received == {"glossary": "Aldric, Vaelthorne", "language": "ru"}




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


def test_non_wav_audio_attachment_is_noted_but_not_sent_to_model(tmp_path, monkeypatch):
    """Ollama's (still-unreleased) audio support only accepts WAV — an mp3
    attachment must stay text-only context, even though the file exists on
    disk and would otherwise be readable."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    audio_dir = tmp_path / "ai_attachments"
    audio_dir.mkdir()
    (audio_dir / "growl.mp3").write_bytes(_MP3_BYTES)

    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/growl.mp3", name="growl.mp3"),
    ])]
    built = _build_ollama_messages(msgs)
    assert "growl.mp3" in built[0]["content"]
    assert "images" not in built[0]
    assert "audios" not in built[0]


def test_wav_audio_attachment_missing_on_disk_is_noted_only(tmp_path, monkeypatch):
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/x.wav", name="growl.wav"),
    ])]
    built = _build_ollama_messages(msgs)
    assert "growl.wav" in built[0]["content"]
    assert "audios" not in built[0]  # file doesn't actually exist on disk


def test_wav_audio_attachment_is_base64_encoded_into_audios_field(tmp_path, monkeypatch):
    """Best-effort real audio support (e.g. for an audio-native model like
    Gemma 3n) — see _build_ollama_messages' docstring for why an unrecognized
    `audios` key still reaches Ollama's server instead of being stripped,
    and why WAV specifically."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages
    import base64

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    audio_dir = tmp_path / "ai_attachments"
    audio_dir.mkdir()
    wav_bytes = b"RIFF....WAVEfmt "
    (audio_dir / "growl.wav").write_bytes(wav_bytes)

    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/growl.wav", name="growl.wav"),
    ])]
    built = _build_ollama_messages(msgs)
    assert built[0]["audios"] == [base64.b64encode(wav_bytes).decode()]
    assert "images" not in built[0]
    assert "growl.wav" in built[0]["content"]


def test_non_wav_audio_with_transcript_is_folded_into_content(tmp_path, monkeypatch):
    """The reliable path: whisper.cpp transcribes any format (see
    app.ai.transcribe_audio, called at upload time), so an mp3's transcript
    still reaches the model as plain text even though it can't go in
    `audios` — this works regardless of which chat model is configured."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/growl.mp3", name="growl.mp3",
                       text="a low, threatening growl"),
    ])]
    built = _build_ollama_messages(msgs)
    assert "a low, threatening growl" in built[0]["content"]
    assert "growl.mp3" in built[0]["content"]
    assert "audios" not in built[0]
    assert "not sent to the model" not in built[0]["content"]


def test_wav_audio_with_transcript_sends_both_text_and_audios(tmp_path, monkeypatch):
    """A .wav attachment that also got transcribed sends both: the
    transcript as reliable text context, and the raw bytes as the
    best-effort audio-native path — not mutually exclusive."""
    from app.routers.ai import ChatAttachment, ChatMessage, _build_ollama_messages
    import base64

    monkeypatch.setattr(ai_router, "_uploads_root", lambda: tmp_path)
    audio_dir = tmp_path / "ai_attachments"
    audio_dir.mkdir()
    wav_bytes = b"RIFF....WAVEfmt "
    (audio_dir / "growl.wav").write_bytes(wav_bytes)

    msgs = [ChatMessage(role="user", content="Listen to this", attachments=[
        ChatAttachment(kind="audio", url="/uploads/ai_attachments/growl.wav", name="growl.wav",
                       text="a low, threatening growl"),
    ])]
    built = _build_ollama_messages(msgs)
    assert built[0]["audios"] == [base64.b64encode(wav_bytes).decode()]
    assert "a low, threatening growl" in built[0]["content"]
    assert "not sent to the model" not in built[0]["content"]


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
    return requested or "fake-model", None


async def _fake_stream_chat(messages, system="", model="", options=None, think=False):
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
