"""Tests for GET /admin/backup.zip — the "Full Backup" download on the
Export & Backup hub. This used to build the ENTIRE zip in an in-memory
BytesIO before sending a single byte (see admin_backup's own comment) — on
a real install with actual uploaded/AI-generated media that's a
multi-minute gap with nothing flowing to the browser, which either looks
like an infinite spinner or gets its connection killed by a reverse
proxy's idle-byte timeout. It now streams real zip bytes out continuously
via app.streaming_export's shared background-thread producer + queue
(see tests/test_streaming_export.py for that mechanism's own tests).

Permission gating (GM-only) is already covered by test_player_safe.py and
test_gm_assistant.py's route matrices — these tests focus on the actual
zip content."""
import io
import json
import sqlite3
import zipfile

from app.database import SessionLocal
from app.main import UPLOADS_DIR, _MAPS_DIR
from app.models import Entity, World

from .conftest import GM_PASSWORD, login


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)


def test_admin_backup_returns_a_valid_zip(client, seed):
    _login_gm(client, seed)
    r = client.get("/admin/backup.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment; filename=" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.testzip() is None  # no corrupted entries
    names = zf.namelist()
    assert "world.db" in names
    assert "manifest.json" in names


def test_admin_backup_manifest_matches_actual_counts(client, seed):
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="character", name="Elyra"))
        db.commit()
        # init_db() auto-seeds a default world alongside whatever the seed
        # fixture adds — assert against the DB's own actual count rather
        # than assuming a fixed number of worlds.
        expected_worlds = db.query(World).count()
    finally:
        db.close()
    _login_gm(client, seed)
    r = client.get("/admin/backup.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["app"] == "nd-world"
    assert manifest["tables"]["worlds"] == expected_worlds
    assert manifest["tables"]["entities"] == 1


def test_admin_backup_includes_uploaded_and_map_files(client, seed):
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "photo.png").write_bytes(b"fake-png-bytes")
    (_MAPS_DIR / "world-a.json").write_text('{"markers": []}')
    _login_gm(client, seed)
    r = client.get("/admin/backup.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.read("uploads/photo.png") == b"fake-png-bytes"
    assert zf.read("maps/world-a.json") == b'{"markers": []}'


def test_admin_backup_snapshot_is_a_standalone_openable_sqlite_db(client, seed, tmp_path):
    """VACUUM INTO — the archived world.db must be a real, independently
    openable sqlite file (not a half-copied live one)."""
    db = SessionLocal()
    try:
        expected_worlds = db.query(World).count()
    finally:
        db.close()
    _login_gm(client, seed)
    r = client.get("/admin/backup.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    out = tmp_path / "world.db"
    out.write_bytes(zf.read("world.db"))
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("SELECT COUNT(*) FROM worlds").fetchone()[0] == expected_worlds
    finally:
        conn.close()


def test_admin_backup_streamed_via_client_still_reads_back_correctly(client, seed):
    """Exercises the route through httpx's streaming client API (a
    different code path than a plain client.get) end-to-end — the actual
    incremental-delivery property this fix adds is verified independently
    in tests/test_streaming_export.py at the writer level, since
    TestClient's in-process ASGI transport collapses a StreamingResponse's
    separate chunks by the time iter_bytes() sees them, so it can't
    observe chunk-by-chunk delivery itself."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / "big.bin").write_bytes(b"x" * (64 * 1024))
    _login_gm(client, seed)
    with client.stream("GET", "/admin/backup.zip") as r:
        assert r.status_code == 200
        content = b"".join(r.iter_bytes())
    zf = zipfile.ZipFile(io.BytesIO(content))
    assert zf.testzip() is None
    assert zf.read("uploads/big.bin") == b"x" * (64 * 1024)
