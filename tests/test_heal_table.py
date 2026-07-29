"""Regression test for _heal_table's constraint loss: its rebuild path (used
when a column needs a NOT NULL constraint relaxed, since SQLite can't ALTER
that away) used to emit a bare CREATE TABLE with just the column list —
silently dropping any FOREIGN KEY constraints, indexes, and UNIQUE indexes
the model declares. ALTER TABLE ADD COLUMN can't add them retroactively, and
create_all() never alters a table that already exists, so once a table went
through one rebuild without them they stayed missing forever.
"""
import sqlite3

from sqlalchemy import create_engine

import app.database as database_module


def test_heal_table_rebuild_restores_foreign_key_and_index(tmp_path):
    db_path = tmp_path / "heal_fk_test.db"
    conn = sqlite3.connect(str(db_path))
    # A legacy `parties` table: `notes` is wrongly NOT NULL (this is what
    # triggers the rebuild path), and world_id has no FK/index at all —
    # mirrors a real pre-fix installation.
    conn.execute("""
        CREATE TABLE parties (
            id INTEGER PRIMARY KEY,
            world_id INTEGER NOT NULL,
            name VARCHAR(256) NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO worlds (id, name) VALUES (1, 'Test World')")
    conn.execute("INSERT INTO parties (world_id, name, notes) VALUES (1, 'The Fellowship', 'notes here')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        database_module._heal_table(conn, "parties", [
            ("world_id",                "INTEGER", False),
            ("name",                    "VARCHAR(256)", False),
            ("member_pc_ids_json",      "TEXT DEFAULT '[]'", True),
            ("member_entity_ids_json",  "TEXT DEFAULT '[]'", True),
            ("loot_json",               "TEXT DEFAULT '[]'", True),
            ("notes",                   "TEXT DEFAULT ''", True),
            ("location_json",           "TEXT DEFAULT '{}'", True),
            ("created_at",              "DATETIME", True),
            ("updated_at",              "DATETIME", True),
        ], foreign_keys=[("world_id", "worlds", "id")], indexes=["world_id"])
    engine.dispose()

    raw = sqlite3.connect(str(db_path))
    schema = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='parties'"
    ).fetchone()[0]
    indexes = raw.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='parties'"
    ).fetchall()
    rows = raw.execute("SELECT id, world_id, name, notes FROM parties").fetchall()

    assert "FOREIGN KEY (world_id) REFERENCES worlds(id)" in schema
    assert any(name == "ix_parties_world_id" for (name,) in indexes)
    assert rows == [(1, 1, "The Fellowship", "notes here")], "rebuild lost data"

    raw.execute("PRAGMA foreign_keys = ON")
    try:
        raw.execute("INSERT INTO parties (world_id, name) VALUES (999, 'Orphaned Party')")
        raw.commit()
        assert False, "FK constraint did not reject an orphaned world_id"
    except sqlite3.IntegrityError:
        pass
    raw.close()


def test_heal_table_rebuild_restores_unique_index(tmp_path):
    db_path = tmp_path / "heal_unique_test.db"
    conn = sqlite3.connect(str(db_path))
    # world_calendars has world_id unique=True in the model (one calendar per
    # world) — a legacy table with updated_at wrongly NOT NULL triggers the
    # rebuild, and had no unique index at all beforehand.
    conn.execute("""
        CREATE TABLE world_calendars (
            id INTEGER PRIMARY KEY,
            world_id INTEGER NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at DATETIME NOT NULL
        )
    """)
    conn.execute("CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO worlds (id, name) VALUES (1, 'Test World')")
    conn.execute("INSERT INTO world_calendars (world_id, config_json, updated_at) VALUES (1, '{}', '2024-01-01')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        database_module._heal_table(conn, "world_calendars", [
            ("world_id",     "INTEGER", False),
            ("config_json",  "TEXT DEFAULT '{}'", True),
            ("updated_at",   "DATETIME", True),
        ], foreign_keys=[("world_id", "worlds", "id")], unique_indexes=["world_id"])
    engine.dispose()

    raw = sqlite3.connect(str(db_path))
    indexes = raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='world_calendars'"
    ).fetchall()
    assert any(name == "ix_world_calendars_world_id" and "UNIQUE" in (sql or "") for name, sql in indexes)

    try:
        raw.execute("INSERT INTO world_calendars (world_id, config_json) VALUES (1, '{}')")
        raw.commit()
        assert False, "unique index did not reject a duplicate world_id"
    except sqlite3.IntegrityError:
        pass
    raw.close()


def test_random_tables_rebuild_restores_foreign_key_and_index(tmp_path, monkeypatch):
    """random_tables uses its own hand-rolled rebuild (not _heal_table) for
    historical reasons — same gap, same fix, verified end-to-end via a real
    _migrate() run rather than calling a helper directly."""
    from app.models import Base

    db_path = tmp_path / "random_tables_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE random_tables (
            id INTEGER PRIMARY KEY,
            world_id INTEGER NOT NULL,
            name VARCHAR(256)
        )
    """)
    conn.execute("""
        CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT, slug TEXT UNIQUE, description TEXT,
            accent TEXT, players_see_party BOOLEAN, rules_md TEXT, created_at DATETIME)
    """)
    conn.execute("INSERT INTO worlds (id, name, slug) VALUES (1, 'Test World', 'test-world')")
    conn.execute("INSERT INTO random_tables (world_id, name) VALUES (1, 'Loot Table')")
    conn.commit()
    conn.close()

    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    Base.metadata.create_all(bind=engine)  # fills in every other table fresh
    database_module._migrate()

    raw = sqlite3.connect(str(db_path))
    schema = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='random_tables'"
    ).fetchone()[0]
    indexes = raw.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='random_tables'"
    ).fetchall()
    rows = raw.execute("SELECT id, world_id, name FROM random_tables").fetchall()
    raw.close()
    engine.dispose()

    assert "FOREIGN KEY (world_id) REFERENCES worlds(id)" in schema
    assert any(name == "ix_random_tables_world_id" for (name,) in indexes)
    assert rows == [(1, 1, "Loot Table")], "rebuild lost data"
