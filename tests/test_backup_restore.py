"""Tests for restoring a Full Backup (app/main.py's admin_backup_restore_stage/
admin_backup_restore_cancel, app/database.py's _apply_staged_restore).

There's no in-app way to restore a Full Backup zip today — GET /admin/
backup.zip is download-only. This adds a "stage now, apply on restart"
counterpart rather than swapping the live database in place from inside a
running request: the app never opens more than one connection to world.db at
a time from its own code, but a StreamingResponse's background thread and
other in-flight requests could easily be mid-read when a restore lands, so
the actual file swap only ever happens at process startup, before this
process's first connection to world.db (see _apply_staged_restore's own
docstring).

_apply_staged_restore is tested in isolation (monkeypatched DB_PATH/
RESTORE_STAGING_DIR pointing at tmp_path, never the shared test-suite engine)
rather than by actually swapping the live test database out from under the
running test session's connection pool — see conftest.py's own client
fixture docstring for why unlinking/replacing that file while pooled
connections hold it open is exactly the "split-brain between tests" hazard
this suite already goes out of its way to avoid.
"""
import io
import json
import sqlite3
import zipfile

from app.database import RESTORE_STAGING_DIR

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _make_backup_zip(*, world_db_bytes=None, manifest=None, extra_files=None, valid_db=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if world_db_bytes is None:
            if valid_db:
                import tempfile
                from pathlib import Path
                with tempfile.TemporaryDirectory() as tmp:
                    p = Path(tmp) / "w.db"
                    conn = sqlite3.connect(str(p))
                    conn.execute("CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT)")
                    conn.execute("INSERT INTO worlds (name) VALUES ('Backed Up World')")
                    conn.commit()
                    conn.close()
                    world_db_bytes = p.read_bytes()
            else:
                world_db_bytes = b"not-a-real-sqlite-file"
        zf.writestr("world.db", world_db_bytes)
        zf.writestr("manifest.json", json.dumps(manifest or {"app": "nd-world", "tables": {"worlds": 1}}))
        for name, content in (extra_files or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


# ── Access control ───────────────────────────────────────────────────────

def test_restore_stage_player_forbidden(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/admin/backup/restore", files={"file": ("b.zip", io.BytesIO(_make_backup_zip()), "application/zip")},
                     data={"confirm_name": "whatever"})
    assert r.status_code == 403


def test_restore_cancel_player_forbidden(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/admin/backup/restore/cancel")
    assert r.status_code == 403


# ── Staging validation ───────────────────────────────────────────────────

def test_restore_stage_wrong_confirm_name_rejected(client, seed):
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(_make_backup_zip()), "application/zip")},
        data={"confirm_name": "definitely not the world name"},
    )
    assert r.status_code == 400
    assert not (RESTORE_STAGING_DIR / "world.db").exists()


def test_restore_stage_not_a_zip_rejected(client, seed):
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(b"not a zip at all"), "application/zip")},
        data={"confirm_name": seed.world_a.name},
    )
    assert r.status_code == 400
    assert not (RESTORE_STAGING_DIR / "world.db").exists()


def test_restore_stage_missing_world_db_rejected(client, seed):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{}")
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", buf.getvalue(), "application/zip")},
        data={"confirm_name": seed.world_a.name},
    )
    assert r.status_code == 400
    assert not RESTORE_STAGING_DIR.exists()


def test_restore_stage_corrupt_world_db_rejected(client, seed):
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(_make_backup_zip(valid_db=False)), "application/zip")},
        data={"confirm_name": seed.world_a.name},
    )
    assert r.status_code == 400
    assert not (RESTORE_STAGING_DIR / "world.db").exists()


def test_restore_stage_path_traversal_entry_is_ignored(client, seed):
    """A crafted zip entry with ".." components must not escape
    RESTORE_STAGING_DIR — ZipFile itself only sanitizes a leading "/" or
    drive letter, not ".." segments inside the path."""
    zip_bytes = _make_backup_zip(extra_files={"uploads/../../escaped.txt": b"should not escape"})
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(zip_bytes), "application/zip")},
        data={"confirm_name": seed.world_a.name},
    )
    assert r.status_code in (200, 303, 307)
    escaped = RESTORE_STAGING_DIR.parent / "escaped.txt"
    assert not escaped.exists()


# ── Successful staging ───────────────────────────────────────────────────

def test_restore_stage_success_extracts_expected_files(client, seed):
    zip_bytes = _make_backup_zip(
        manifest={"app": "nd-world", "created_at": "2026-01-01T00:00:00Z", "tables": {"worlds": 1}},
        extra_files={"uploads/photo.png": b"fake-png", "maps/city.json": b'{"markers": []}'},
    )
    _login_gm(client, seed)
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(zip_bytes), "application/zip")},
        data={"confirm_name": seed.world_a.name},
    )
    assert r.status_code in (200, 303, 307)
    assert (RESTORE_STAGING_DIR / "world.db").exists()
    assert (RESTORE_STAGING_DIR / "manifest.json").exists()
    assert (RESTORE_STAGING_DIR / "uploads" / "photo.png").read_bytes() == b"fake-png"
    assert (RESTORE_STAGING_DIR / "maps" / "city.json").read_bytes() == b'{"markers": []}'

    manifest = json.loads((RESTORE_STAGING_DIR / "manifest.json").read_text())
    assert manifest["tables"]["worlds"] == 1


def test_restore_stage_no_active_world_falls_back_to_restore_confirm_word(client, seed):
    from app.database import SessionLocal
    from app.models import World
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        db.query(World).delete()
        db.commit()
    finally:
        db.close()
    r = client.post(
        "/admin/backup/restore",
        files={"file": ("b.zip", io.BytesIO(_make_backup_zip()), "application/zip")},
        data={"confirm_name": "RESTORE"},
    )
    assert r.status_code in (200, 303, 307)
    assert (RESTORE_STAGING_DIR / "world.db").exists()


def test_restore_stage_twice_replaces_the_first_staged_backup(client, seed):
    _login_gm(client, seed)
    zip1 = _make_backup_zip(extra_files={"uploads/only-in-first.png": b"first"})
    client.post("/admin/backup/restore", files={"file": ("b.zip", io.BytesIO(zip1), "application/zip")},
                data={"confirm_name": seed.world_a.name})
    assert (RESTORE_STAGING_DIR / "uploads" / "only-in-first.png").exists()

    zip2 = _make_backup_zip(extra_files={"uploads/only-in-second.png": b"second"})
    client.post("/admin/backup/restore", files={"file": ("b.zip", io.BytesIO(zip2), "application/zip")},
                data={"confirm_name": seed.world_a.name})
    assert not (RESTORE_STAGING_DIR / "uploads" / "only-in-first.png").exists()
    assert (RESTORE_STAGING_DIR / "uploads" / "only-in-second.png").exists()


def test_restore_cancel_clears_staging(client, seed):
    _login_gm(client, seed)
    client.post("/admin/backup/restore", files={"file": ("b.zip", io.BytesIO(_make_backup_zip()), "application/zip")},
                data={"confirm_name": seed.world_a.name})
    assert RESTORE_STAGING_DIR.exists()

    r = client.post("/admin/backup/restore/cancel")
    assert r.status_code in (200, 303, 307)
    assert not RESTORE_STAGING_DIR.exists()


def test_restore_cancel_is_a_no_op_when_nothing_staged(client, seed):
    _login_gm(client, seed)
    r = client.post("/admin/backup/restore/cancel")
    assert r.status_code in (200, 303, 307)


# ── Export hub UI reflects staged state ──────────────────────────────────

def test_export_hub_shows_upload_form_when_nothing_staged(client, seed):
    _login_gm(client, seed)
    r = client.get("/export")
    assert "Restore from Full Backup" in r.text
    assert "Restore staged" not in r.text


def test_export_hub_shows_warning_banner_when_restore_staged(client, seed):
    _login_gm(client, seed)
    client.post("/admin/backup/restore", files={"file": ("b.zip", io.BytesIO(_make_backup_zip()), "application/zip")},
                data={"confirm_name": seed.world_a.name})
    r = client.get("/export")
    assert "Restore staged" in r.text
    assert "Cancel staged restore" in r.text


# ── _apply_staged_restore, in isolation ──────────────────────────────────

def test_apply_staged_restore_is_a_no_op_when_nothing_staged(tmp_path, monkeypatch):
    import app.database as database_module

    live_db = tmp_path / "world.db"
    live_db.write_bytes(b"UNCHANGED")
    monkeypatch.setattr(database_module, "DB_PATH", str(live_db))
    monkeypatch.setattr(database_module, "RESTORE_STAGING_DIR", tmp_path / "restore_staging")

    database_module._apply_staged_restore()

    assert live_db.read_bytes() == b"UNCHANGED"
    assert not list(tmp_path.glob("world.db.pre-restore-*"))


def test_apply_staged_restore_swaps_db_backs_up_the_old_one_and_merges_media(tmp_path, monkeypatch):
    import app.database as database_module

    live_db = tmp_path / "world.db"
    live_db.write_bytes(b"OLD-DB-CONTENT")
    (tmp_path / "world.db-wal").write_bytes(b"stale-wal")
    (tmp_path / "world.db-shm").write_bytes(b"stale-shm")
    live_uploads = tmp_path / "uploads"
    live_uploads.mkdir()
    (live_uploads / "only-on-live.png").write_bytes(b"live-only")
    (live_uploads / "shared.png").write_bytes(b"live-version")

    staging = tmp_path / "restore_staging"
    staging.mkdir()
    (staging / "world.db").write_bytes(b"NEW-DB-CONTENT")
    staged_uploads = staging / "uploads"
    staged_uploads.mkdir()
    (staged_uploads / "shared.png").write_bytes(b"backup-version")
    (staged_uploads / "only-in-backup.png").write_bytes(b"backup-only")

    monkeypatch.setattr(database_module, "DB_PATH", str(live_db))
    monkeypatch.setattr(database_module, "RESTORE_STAGING_DIR", staging)

    database_module._apply_staged_restore()

    assert live_db.read_bytes() == b"NEW-DB-CONTENT"
    assert not (tmp_path / "world.db-wal").exists()
    assert not (tmp_path / "world.db-shm").exists()

    backups = list(tmp_path.glob("world.db.pre-restore-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"OLD-DB-CONTENT"

    # Merge, not wipe: live-only file survives, shared name is overwritten
    # by the backup's version, and the backup's own extra file is added.
    assert (live_uploads / "only-on-live.png").read_bytes() == b"live-only"
    assert (live_uploads / "shared.png").read_bytes() == b"backup-version"
    assert (live_uploads / "only-in-backup.png").read_bytes() == b"backup-only"

    assert not staging.exists()


def test_incomplete_staging_without_world_db_is_discarded(tmp_path, monkeypatch):
    """A staging dir without world.db = an extraction that died partway
    (media is staged first, the DB is moved in last). Boot must discard it
    rather than leave the /export banner claiming a restore is pending
    forever — and must certainly not merge half-extracted media."""
    import app.database as database_module

    live_db = tmp_path / "world.db"
    live_db.write_bytes(b"OLD-DB-CONTENT")
    staging = tmp_path / "restore_staging"
    (staging / "uploads").mkdir(parents=True)
    (staging / "uploads" / "half.png").write_bytes(b"half-extracted")
    (staging / "manifest.json").write_text("{}")

    monkeypatch.setattr(database_module, "DB_PATH", str(live_db))
    monkeypatch.setattr(database_module, "RESTORE_STAGING_DIR", staging)

    database_module._apply_staged_restore()

    assert live_db.read_bytes() == b"OLD-DB-CONTENT"   # nothing applied
    assert not staging.exists()                         # incomplete stage cleaned
    assert not (tmp_path / "uploads").exists()          # media NOT merged
