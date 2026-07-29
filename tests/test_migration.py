"""Regression test for the one-time migration guard (app/database.py's
_once/schema_meta): the legacy-import cleanup used to run its destructive DML
(including a kind='item' -> 'feat' reclassification) unconditionally on every
boot, silently reverting a GM's deliberate edit on every container restart —
which with Watchtower polling happens every few minutes in production.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as database_module
from app.models import Base, Entity, World


def test_legacy_cleanup_runs_once_not_every_boot(tmp_path, monkeypatch):
    """Independent of the shared `client`/`seed` fixtures on purpose: this test
    needs full control over exactly when the *first* init_db() runs, to prove
    the reclassification fires once (the rule still works) and never again
    (the guard actually guards).

    Patches app.database's module-level engine/SessionLocal directly (rather
    than reloading the module via a DB_PATH env var) so monkeypatch's teardown
    fully restores the shared test database other tests rely on.
    """
    db_path = tmp_path / "migration_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    db = SessionLocal()
    try:
        world = World(name="Test World", slug="test-world")
        db.add(world)
        db.commit()
        db.refresh(world)
        ent = Entity(world_id=world.id, kind="item", name="Legacy Feat Item",
                     folder="Common Feats/Rank 1")
        db.add(ent)
        db.commit()
        db.refresh(ent)
        ent_id = ent.id
    finally:
        db.close()

    # First boot: the legacy-import cleanup should still fire and reclassify it.
    database_module.init_db()
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.id == ent_id).first()
        assert ent.kind == "feat", "the one-time cleanup rule itself no longer reclassifies legacy data"
    finally:
        db.close()

    # A GM deliberately reverts it back to 'item'.
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.id == ent_id).first()
        ent.kind = "item"
        db.commit()
    finally:
        db.close()

    # Two more simulated restarts must NOT revert the GM's edit again.
    database_module.init_db()
    database_module.init_db()
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.id == ent_id).first()
        assert ent.kind == "item", (
            "kind='item' was reclassified back to 'feat' on a later restart — "
            "the legacy-import cleanup is re-running every boot instead of once"
        )
    finally:
        db.close()

    engine.dispose()
