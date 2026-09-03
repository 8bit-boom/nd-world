"""Tests for app/backups.py: snapshot correctness (VACUUM INTO roundtrip),
retention pruning, and the GM-only API surface."""
import sqlite3

from app import backups

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE thing (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO thing (name) VALUES ('keeper')")
    conn.commit()
    conn.close()


def test_snapshot_roundtrip(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    dest = tmp_path / "backups"
    snap = backups.create_snapshot(src, dest)
    assert snap.exists()
    assert snap.parent == dest
    conn = sqlite3.connect(str(snap))
    rows = conn.execute("SELECT name FROM thing").fetchall()
    conn.close()
    assert rows == [("keeper",)]


def test_snapshot_same_second_gets_unique_names(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    dest = tmp_path / "backups"
    a = backups.create_snapshot(src, dest)
    b = backups.create_snapshot(src, dest)
    assert a != b
    assert a.exists() and b.exists()


def test_prune_keeps_newest_n(tmp_path):
    names = ["world-20260101-000000.db", "world-20260102-000000.db", "world-20260103-000000.db"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    deleted = backups.prune_snapshots(tmp_path, keep=2)
    assert deleted == 1
    assert not (tmp_path / names[0]).exists()
    assert (tmp_path / names[1]).exists()
    assert (tmp_path / names[2]).exists()


def test_list_snapshots_newest_first(tmp_path):
    for n in ["world-20260101-000000.db", "world-20260102-000000.db"]:
        (tmp_path / n).write_bytes(b"x")
    listed = backups.list_snapshots(tmp_path)
    assert [l["name"] for l in listed] == ["world-20260102-000000.db", "world-20260101-000000.db"]


def test_api_run_and_list_as_gm(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ND_BACKUP_KEEP", "3")
    login(client, seed.gm.email, GM_PASSWORD)

    r = client.post("/api/backups/run")
    assert r.status_code == 200
    name = r.json()["name"]
    assert (tmp_path / "backups" / name).exists()

    listed = client.get("/api/backups").json()["backups"]
    assert [b["name"] for b in listed] == [name]


def test_api_player_forbidden(client, seed, tmp_path, monkeypatch):
    monkeypatch.setenv("ND_BACKUP_DIR", str(tmp_path / "backups"))
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get("/api/backups").status_code == 403
    assert client.post("/api/backups/run").status_code == 403


def test_api_reports_unconfigured(client, seed, monkeypatch):
    monkeypatch.delenv("ND_BACKUP_DIR", raising=False)
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get("/api/backups").status_code == 400
    assert client.post("/api/backups/run").status_code == 400


def test_scheduler_noop_without_env_dir(monkeypatch):
    monkeypatch.delenv("ND_BACKUP_DIR", raising=False)
    backups.start()
    assert backups._thread is None
