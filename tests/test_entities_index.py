"""Tests for the composite (world_id, kind) index on entities (plan item
Speed 4.7) — Entity.__table_args__ for a fresh database via Base.metadata.
create_all, and database._migrate's own CREATE INDEX IF NOT EXISTS for an
existing database upgrading in place. Isolated engine/DB file per test,
same pattern tests/test_migration.py already uses, since this needs full
control over exactly what schema exists before running the heal."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.database as database_module
from app.models import Base


def _index_names(engine, table="entities"):
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
    return {r[1] for r in rows}


def test_fresh_database_gets_the_composite_index(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    assert "ix_entities_world_id_kind" in _index_names(engine)


def test_existing_database_gets_the_index_via_migrate(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Simulate a pre-existing database that predates this index: drop it,
    # matching a real upgrade's starting point.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_entities_world_id_kind"))
    assert "ix_entities_world_id_kind" not in _index_names(engine)

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)
    database_module._migrate()

    assert "ix_entities_world_id_kind" in _index_names(engine)


def test_migrate_is_idempotent_on_an_already_indexed_database(tmp_path, monkeypatch):
    db_path = tmp_path / "already_indexed.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)
    database_module._migrate()
    database_module._migrate()

    assert "ix_entities_world_id_kind" in _index_names(engine)
