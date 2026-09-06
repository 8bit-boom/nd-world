"""Tests for the GM-only, preview-only Code Assist panel
(app/routers/code_assist.py + app/ai_assist.py's OP_CODE_EDIT).

Nothing here ever contacts real Ollama — the ai_assist job engine's
run_assist is monkeypatched, same pattern test_ai_assist.py uses for every
other assist-job test. A throwaway fixture file is written under static/
(one of the two sandboxed-readable roots) rather than depending on any
real source file's exact current content staying stable.
"""
import json
import time
from pathlib import Path

import pytest

from app import audio_jobs as audio_jobs_module
from app.routers import code_assist as code_assist_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

_FIXTURE_REL = "static/_code_assist_test_fixture.txt"
_FIXTURE_CONTENT = "line one\nline two\nline three\n"


@pytest.fixture()
def fixture_file():
    path = code_assist_module._INSTALL_ROOT / _FIXTURE_REL
    path.write_text(_FIXTURE_CONTENT, encoding="utf-8")
    try:
        yield _FIXTURE_REL
    finally:
        path.unlink(missing_ok=True)


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _await_done_or_error(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        data = client.get(f"/tools/code-assist/generate/{job_id}").json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"job never finished, last seen {data!r}")


# ── Path sandboxing ─────────────────────────────────────────────────────────

def test_resolve_safe_path_accepts_real_file(fixture_file):
    p = code_assist_module._resolve_safe_path(fixture_file)
    assert p.read_text(encoding="utf-8") == _FIXTURE_CONTENT


def test_resolve_safe_path_rejects_traversal():
    with pytest.raises(Exception) as exc:
        code_assist_module._resolve_safe_path("../../../etc/passwd")
    assert getattr(exc.value, "status_code", None) == 400


def test_resolve_safe_path_rejects_embedded_dotdot():
    with pytest.raises(Exception) as exc:
        code_assist_module._resolve_safe_path("app/../../../etc/passwd.py")
    assert getattr(exc.value, "status_code", None) == 400


def test_resolve_safe_path_rejects_outside_allowed_roots():
    # A real file that exists in the repo, but under tests/ — not one of
    # the two directories the Dockerfile actually ships (app/, static/).
    with pytest.raises(Exception) as exc:
        code_assist_module._resolve_safe_path("tests/conftest.py")
    assert getattr(exc.value, "status_code", None) == 404


def test_resolve_safe_path_rejects_disallowed_extension():
    with pytest.raises(Exception) as exc:
        code_assist_module._resolve_safe_path("app/database.py.pyc")
    assert getattr(exc.value, "status_code", None) == 400


def test_resolve_safe_path_rejects_nonexistent_file():
    with pytest.raises(Exception) as exc:
        code_assist_module._resolve_safe_path("app/this_file_does_not_exist.py")
    assert getattr(exc.value, "status_code", None) == 404


def test_list_source_files_only_lists_allowed_extensions_under_allowed_roots():
    files = code_assist_module._list_source_files()
    assert "app/ai_assist.py" in files
    assert all(f.startswith("app/") or f.startswith("static/") for f in files)
    assert all(Path(f).suffix.lower() in code_assist_module._ALLOWED_EXTS for f in files)


# ── Access control ───────────────────────────────────────────────────────────

def test_code_assist_page_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/tools/code-assist")
    assert r.status_code == 403


def test_code_assist_generate_is_gm_only(client, seed, fixture_file):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/tools/code-assist/generate", data={"file": fixture_file, "instruction": "do it"})
    assert r.status_code == 403


def test_code_assist_page_loads_for_gm(client, seed):
    _login_gm(client, seed)
    r = client.get("/tools/code-assist")
    assert r.status_code == 200
    assert "Code Assist" in r.text
    assert "Preview only" in r.text


# ── Generate validation ──────────────────────────────────────────────────────

def test_generate_requires_instruction(client, seed, fixture_file):
    _login_gm(client, seed)
    r = client.post("/tools/code-assist/generate", data={"file": fixture_file, "instruction": "  "})
    assert r.status_code == 400


def test_generate_rejects_path_traversal(client, seed):
    _login_gm(client, seed)
    r = client.post("/tools/code-assist/generate",
                     data={"file": "../../../etc/passwd", "instruction": "do it"})
    assert r.status_code == 400


def test_generate_rejects_file_too_large(client, seed, fixture_file, monkeypatch):
    monkeypatch.setattr(code_assist_module, "_MAX_FILE_BYTES", 3)
    _login_gm(client, seed)
    r = client.post("/tools/code-assist/generate", data={"file": fixture_file, "instruction": "do it"})
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]


# ── Full submit -> poll -> diff round trip ───────────────────────────────────

def test_generate_and_poll_round_trip(client, seed, fixture_file, monkeypatch):
    captured = {}

    async def fake_run_assist(op, **kwargs):
        captured["op"] = op
        captured["kwargs"] = kwargs
        revised = _FIXTURE_CONTENT.replace("line two", "line TWO (edited)")
        return {"op": op, "mode": "text", "text": revised, "model": "coder-model"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    _login_gm(client, seed)

    r = client.post("/tools/code-assist/generate",
                     data={"file": fixture_file, "instruction": "capitalize line two", "think": "false"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # The op actually reaches run_assist with the file's real content and
    # the GM's instruction — not something silently substituted.
    data = _await_done_or_error(client, job_id)
    assert data["status"] == "done", data
    assert captured["op"] == "code_edit"
    assert captured["kwargs"]["content"] == _FIXTURE_CONTENT
    assert captured["kwargs"]["instruction"] == "capitalize line two"
    assert fixture_file in captured["kwargs"]["meta"]

    assert data["file"] == fixture_file
    assert data["original"] == _FIXTURE_CONTENT
    assert "line TWO (edited)" in data["revised"]
    assert data["model"] == "coder-model"
    # A real diff, not a placeholder — shows the actual line change.
    assert "-line two" in data["diff"]
    assert "+line TWO (edited)" in data["diff"]

    # Nothing was ever written back to the real file on disk.
    on_disk = (code_assist_module._INSTALL_ROOT / fixture_file).read_text(encoding="utf-8")
    assert on_disk == _FIXTURE_CONTENT


def test_generate_sentinel_failure_becomes_error_status(client, seed, fixture_file, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "[AI unavailable: down]", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    _login_gm(client, seed)
    r = client.post("/tools/code-assist/generate", data={"file": fixture_file, "instruction": "do it"})
    job_id = r.json()["job_id"]
    data = _await_done_or_error(client, job_id)
    assert data["status"] == "error"
    assert "AI unavailable" in data["error"]


@pytest.mark.asyncio
async def test_poll_route_404s_for_a_job_of_a_different_op(client, seed, monkeypatch):
    """A plain "improve" assist job (a different surface entirely) must not
    be servable through this op-specific polling route."""
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "x", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    _login_gm(client, seed)
    # create_assist_job schedules a background task via asyncio.create_task,
    # which needs a running loop — this test is async for exactly that
    # reason (an HTTP round trip would give one too, but there's no route
    # for creating an arbitrary non-code_edit op scoped to a specific world).
    job_id = audio_jobs_module.create_assist_job(seed.world_a.id, op="improve", content="x")
    r = client.get(f"/tools/code-assist/generate/{job_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_poll_route_404s_for_a_job_under_a_different_world(client, seed, fixture_file, monkeypatch):
    async def fake_run_assist(op, **kwargs):
        return {"op": op, "mode": "text", "text": "x", "model": "m"}

    monkeypatch.setattr(audio_jobs_module._ai_assist, "run_assist", fake_run_assist)
    job_id = audio_jobs_module.create_assist_job(
        seed.world_b.id, op="code_edit", surface="code_assist",
        content=_FIXTURE_CONTENT, meta=f"File: {fixture_file}", instruction="do it",
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/tools/code-assist/generate/{job_id}")
    assert r.status_code == 404
