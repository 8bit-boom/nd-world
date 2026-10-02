"""Studio API key: save-and-verify in one step, which key/URL is in use, and the STT-model hint.

Reported: "new key is invalid for some reason". The red text came from the Studio-settings Save button,
whose first request uses the key nd-world has STORED — and the key box (write-only since the key is no
longer printed back) was a separate part of a different form, so pasting a new key and pressing the
nearer button tested the old, dead key. These pin the replacement: a dedicated Save & verify action that
checks the key with Studio BEFORE replacing a working one and says what is wrong, plus a status that
names the key and URL actually in use (a key saved in Settings silently overrides UNSLOTH_API_KEY).
Also: a speech model Studio lists as "On Device" can still be refused by its OpenAI-style API (seen with
Qwen3-ASR) — the STT test now says so and names the Whisper models that work.
"""
import asyncio
import json
import re

import httpx
import pytest

from app import ai as _ai
from app import unsloth_extras as ux
from app.database import SessionLocal
from app.models import AppSettings

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

GOOD = "sk-unsloth-" + "0123456789abcdef0123456789abcdef"
OLD = "sk-unsloth-" + "ffffffffffffffffffffffffffffffff"


def _mock(monkeypatch, handler, seen=None):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(*a, transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _accepts(*keys):
    def handler(req):
        auth = req.headers.get("authorization", "")
        if req.url.path == "/api/hub/cached-gguf":
            if any(auth == f"Bearer {k}" for k in keys):
                return httpx.Response(200, json=[])
            return httpx.Response(401, json={"detail": "Invalid or expired API key"})
        return httpx.Response(404, json={})
    return handler


def _studio(monkeypatch, saved="", env=""):
    monkeypatch.setattr(_ai, "_llm_api_key_override", saved)
    monkeypatch.setattr(_ai, "UNSLOTH_API_KEY", env)
    monkeypatch.setattr(_ai, "_llm_url_override", "http://studio:8000")


def _stored():
    db = SessionLocal()
    try:
        s = db.query(AppSettings).first()
        return (s.llm_api_key or "") if s else ""
    finally:
        db.close()


def _post(client, **body):
    return client.post("/settings/system/studio-key", json=body)


# ── verify_key ───────────────────────────────────────────────────────────────

def test_verify_key_says_what_studio_said(monkeypatch):
    _studio(monkeypatch, saved=GOOD)
    _mock(monkeypatch, _accepts(GOOD))
    r = asyncio.run(ux.verify_key())
    assert r["ok"] is True and r["url"] == "http://studio:8000"
    r = asyncio.run(ux.verify_key(OLD))
    assert r["ok"] is False and r["status"] == 401 and r["message"] == "Invalid or expired API key"


def test_verify_key_reports_an_unreachable_studio_and_a_missing_key(monkeypatch):
    _studio(monkeypatch, saved=GOOD)

    def boom(req):
        raise httpx.ConnectError("refused")

    _mock(monkeypatch, boom)
    r = asyncio.run(ux.verify_key())
    assert r["ok"] is False and r["unreachable"] is True and "unreachable" in r["message"].lower()
    _studio(monkeypatch, saved="", env="")
    r = asyncio.run(ux.verify_key())
    assert r["ok"] is False and "no" in r["message"].lower() and "key" in r["message"].lower()


# ── the save-and-verify route ────────────────────────────────────────────────

def test_a_good_key_is_saved_verified_and_active_at_once(client, seed, monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(GOOD))
    login(client, seed.gm.email, GM_PASSWORD)
    d = _post(client, api_key=GOOD).json()
    assert d["ok"] is True and d["saved"] is True and d["saved_hint"] == "…cdef"
    assert _stored() == GOOD and _ai.effective_llm_api_key() == GOOD
    assert GOOD not in json.dumps(d), "the key is never echoed back"


def test_a_rejected_key_does_not_replace_a_working_one(client, seed, monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(OLD))
    login(client, seed.gm.email, GM_PASSWORD)
    assert _post(client, api_key=OLD).json()["ok"] is True
    _mock(monkeypatch, _accepts(OLD))           # Studio only knows the old key: the "new" one is invalid
    d = _post(client, api_key=GOOD).json()
    assert d["ok"] is False and d["saved"] is False
    assert "Invalid or expired API key" in d["message"] and "unchanged" in d["message"].lower()
    assert _stored() == OLD and _ai.effective_llm_api_key() == OLD


def test_a_rejected_first_key_says_nothing_was_saved_not_that_a_key_is_unchanged(client, seed, monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(OLD))
    login(client, seed.gm.email, GM_PASSWORD)
    d = _post(client, api_key=GOOD).json()
    assert d["saved"] is False and "nothing was saved" in d["message"] and "unchanged" not in d["message"]
    assert _stored() == ""


def test_a_key_is_saved_when_studio_cannot_be_asked(client, seed, monkeypatch):
    """Studio down or restarting: refusing to save would lose what was pasted, and nothing says it is wrong."""
    _studio(monkeypatch)

    def boom(req):
        raise httpx.ConnectError("refused")

    _mock(monkeypatch, boom)
    login(client, seed.gm.email, GM_PASSWORD)
    d = _post(client, api_key=GOOD).json()
    assert d["saved"] is True and d["ok"] is False and d["unreachable"] is True
    assert _stored() == GOOD


@pytest.mark.parametrize("pasted, expected", [
    ("  " + GOOD + "\n", GOOD), ("Bearer " + GOOD, GOOD), ('"' + GOOD + '"', GOOD), ("'" + GOOD + "'", GOOD),
])
def test_what_gets_pasted_is_cleaned(client, seed, monkeypatch, pasted, expected):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(GOOD))
    login(client, seed.gm.email, GM_PASSWORD)
    assert _post(client, api_key=pasted).json()["saved"] is True
    assert _stored() == expected


@pytest.mark.parametrize("bad", ["", "   ", "sk-unsloth-abc def", "two\nlines"])
def test_an_empty_or_spaced_key_is_refused_with_a_reason(client, seed, monkeypatch, bad):
    _studio(monkeypatch)
    login(client, seed.gm.email, GM_PASSWORD)
    r = _post(client, api_key=bad)
    assert r.status_code == 400 and r.json()["detail"]


def test_the_saved_key_can_be_removed(client, seed, monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(GOOD))
    login(client, seed.gm.email, GM_PASSWORD)
    _post(client, api_key=GOOD)
    d = _post(client, clear=True).json()
    assert d["saved"] is False and _stored() == "" and d["in_use"] == "none"


def test_only_the_gm_can_set_the_key(client, seed, monkeypatch):
    _studio(monkeypatch)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert _post(client, api_key=GOOD).status_code == 403
    assert _stored() == ""


# ── which key is in use ──────────────────────────────────────────────────────

def test_the_status_names_the_key_and_url_actually_in_use(client, seed, monkeypatch):
    login(client, seed.gm.email, GM_PASSWORD)
    _mock(monkeypatch, _accepts(GOOD))
    _studio(monkeypatch, saved=OLD, env=GOOD)
    d = client.get("/api/ai/unsloth/key-check").json()
    assert d["in_use"] == "settings" and d["saved_hint"] == "…ffff" and d["env_hint"] == "…cdef"
    assert d["ok"] is False and d["status"] == 401 and d["url"] == "http://studio:8000"
    assert "overrides" in d["explain"].lower() and "UNSLOTH_API_KEY" in d["explain"], \
        "a stale key saved in Settings wins over a fresh UNSLOTH_API_KEY - the surprise this has to explain"
    _studio(monkeypatch, saved="", env=GOOD)
    d = client.get("/api/ai/unsloth/key-check").json()
    assert d["in_use"] == "env" and d["ok"] is True
    _studio(monkeypatch, saved="", env="")
    d = client.get("/api/ai/unsloth/key-check").json()
    assert d["in_use"] == "none" and d["ok"] is False


def test_the_key_status_is_gm_only(client, seed, monkeypatch):
    _studio(monkeypatch, saved=GOOD)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/ai/unsloth/key-check").status_code == 403


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_key_box_is_not_part_of_the_big_form_and_has_its_own_buttons(client, seed, monkeypatch):
    _studio(monkeypatch)
    _mock(monkeypatch, _accepts(GOOD))
    login(client, seed.gm.email, GM_PASSWORD)
    assert _post(client, api_key=GOOD).json()["saved"] is True
    html = client.get("/settings?tab=system").text
    tag = re.search(r'<input[^>]*id="llm-api-key"[^>]*>', html).group(0)
    assert 'type="password"' in tag and "name=" not in tag, \
        "no name: the big Save can never submit (or password-manager-fill) over a working key"
    assert 'autocomplete="off"' in tag and "data-1p-ignore" in tag
    assert GOOD not in html and "…cdef" in html
    assert 'id="llm-key-save"' in html and 'id="llm-key-clear"' in html and 'id="llm-key-status"' in html
    assert "/settings/system/studio-key" in html and "/api/ai/unsloth/key-check" in html


def test_save_studio_settings_will_not_run_with_an_unsaved_typed_key(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    html = client.get("/settings?tab=system").text
    handler = html.split("saveBtn.addEventListener('click'", 1)[1][:1100]
    assert "keyBox.value.trim()" in handler and "Save & verify key" in handler
    assert handler.index("keyBox.value.trim()") < handler.index("saveBtn.disabled = true"), "the guard returns before anything is sent"
    assert html.index("keyBox.value.trim()") < html.index("/api/ai/unsloth/auto-switch', {"), "...and before the first request to Studio"
    assert "r1.status === 401" in html, "a 401 from the first Studio call points at the key box, not at raw JSON"
    assert "e.key === 'Enter'" in html, "Enter in the key box saves the key instead of submitting the big form"


# ── STT test: on device but refused ──────────────────────────────────────────

def _stt_handler(listed):
    def handler(req):
        if req.url.path == "/api/hub/cached-gguf":
            return httpx.Response(200, json={"cached": listed})
        if req.url.path == "/v1/audio/transcriptions":
            return httpx.Response(409, json={"error": {"message": "STT model 'unslothai/Qwen3-ASR-1.7B-GGUF' is not downloaded. "
                                                                   "Download it in Settings, then Voice, before loading it."}})
        return httpx.Response(404, json={})
    return handler


_LISTED = [
    {"repo_id": "unslothai/Qwen3-ASR-1.7B-GGUF", "task": "automatic-speech-recognition", "size_bytes": 2_200_000_000},
    {"repo_id": "unsloth/whisper-large-v3", "task": "automatic-speech-recognition", "size_bytes": 3_100_000_000},
    {"repo_id": "unsloth/whisper-large-v3-turbo", "task": "automatic-speech-recognition", "size_bytes": 1_600_000_000},
]


def test_an_on_device_model_that_the_api_refuses_gets_an_actionable_hint(monkeypatch):
    _studio(monkeypatch, saved=GOOD)
    _mock(monkeypatch, _stt_handler(_LISTED))
    r = asyncio.run(ux.stt_health("unslothai/Qwen3-ASR-1.7B-GGUF"))
    assert r["ok"] is False and r["status"] == 409
    msg = r["message"]
    assert "not downloaded" in msg, "Studio's own words are kept"
    assert "on device" in msg.lower() and "refus" in msg.lower()
    assert "large-v3-turbo" in msg and "large-v3" in msg, "names the Whisper models that ARE on device, as the API spells them"
    assert "Qwen" in msg


def test_a_model_that_really_is_missing_says_to_download_it(monkeypatch):
    _studio(monkeypatch, saved=GOOD)
    _mock(monkeypatch, _stt_handler([]))
    r = asyncio.run(ux.stt_health("small"))
    assert r["ok"] is False and "Download it in Studio" in r["message"] and "refus" not in r["message"].lower()


def test_the_hint_never_breaks_the_test_itself(monkeypatch):
    """If listing Studio's models fails too, the original answer still comes back."""
    _studio(monkeypatch, saved=GOOD)

    def handler(req):
        if req.url.path == "/v1/audio/transcriptions":
            return httpx.Response(409, json={"error": {"message": "STT model 'x/y' is not downloaded."}})
        return httpx.Response(500, json={"error": "boom"})

    _mock(monkeypatch, handler)
    r = asyncio.run(ux.stt_health("x/y"))
    assert r["ok"] is False and "not downloaded" in r["message"]
