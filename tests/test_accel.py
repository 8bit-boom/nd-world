"""A file handed to the Rust gateway to send (app/accel.py): only on a request that carries the shared token, never otherwise."""
from .conftest import GM_PASSWORD, login


def _picture():
    from app.main import UPLOADS_DIR
    (UPLOADS_DIR / "p").mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / "p" / "accel.png"
    path.write_bytes(b"\x89PNG-not-really" * 10)
    return path


def test_without_a_token_in_the_environment_the_file_is_sent_by_the_app(client, seed, monkeypatch):
    monkeypatch.delenv("ND_ACCEL_TOKEN", raising=False)
    login(client, seed.gm.email, GM_PASSWORD)
    path = _picture()
    r = client.get("/uploads/p/accel.png", headers={"X-Nd-Accel": "anything"})
    assert r.status_code == 200 and r.content == path.read_bytes() and "x-nd-accel-path" not in r.headers


def test_a_request_with_the_right_token_gets_the_path_and_the_headers_but_no_body(client, seed, monkeypatch):
    monkeypatch.setenv("ND_ACCEL_TOKEN", "s3cret")
    login(client, seed.gm.email, GM_PASSWORD)
    path = _picture()
    r = client.get("/uploads/p/accel.png", headers={"X-Nd-Accel": "s3cret"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["x-nd-accel-path"] == str(path.resolve())
    assert r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"


def test_a_wrong_or_missing_token_gets_the_file_itself(client, seed, monkeypatch):
    monkeypatch.setenv("ND_ACCEL_TOKEN", "s3cret")
    login(client, seed.gm.email, GM_PASSWORD)
    path = _picture()
    for headers in ({}, {"X-Nd-Accel": "wrong"}, {"X-Nd-Accel": ""}):
        r = client.get("/uploads/p/accel.png", headers=headers)
        assert r.content == path.read_bytes() and "x-nd-accel-path" not in r.headers


def test_the_checks_still_run_first_a_stranger_gets_no_path(client, seed, monkeypatch):
    monkeypatch.setenv("ND_ACCEL_TOKEN", "s3cret")
    _picture()
    r = client.get("/uploads/p/accel.png", headers={"X-Nd-Accel": "s3cret"}, follow_redirects=False)
    assert "x-nd-accel-path" not in r.headers and r.status_code in (302, 303, 401, 403)


def test_the_live_recording_archive_is_never_handed_over(client, seed, monkeypatch):
    from app.main import UPLOADS_DIR
    monkeypatch.setenv("ND_ACCEL_TOKEN", "s3cret")
    login(client, seed.gm.email, GM_PASSWORD)
    (UPLOADS_DIR / "live" / "1" / "1").mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "live" / "1" / "1" / "000001.webm").write_bytes(b"audio")
    r = client.get("/uploads/live/1/1/000001.webm", headers={"X-Nd-Accel": "s3cret"})
    assert r.status_code == 404 and "x-nd-accel-path" not in r.headers


def test_the_rust_session_fixture_is_what_python_signs_today():
    """rust/nd-gateway/tests/fixtures/session.json holds cookies made by the app's signer; if Starlette / itsdangerous ever changes the format,
    regenerate it (scripts/gen_session_fixture.py) and the Rust reader's tests will show what broke."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("gen", Path(__file__).resolve().parent.parent / "scripts" / "gen_session_fixture.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    import json
    assert json.loads(gen.OUT.read_text()) == gen.make()
