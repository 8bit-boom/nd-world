"""Regression test for the one-time migration guard (app/database.py's
_once/schema_meta): the legacy-import cleanup used to run its destructive DML
(including a kind='item' -> 'feat' reclassification) unconditionally on every
boot, silently reverting a GM's deliberate edit on every container restart —
which with Watchtower polling happens every few minutes in production.
"""
from sqlalchemy import create_engine, text
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


def test_heals_pre_settings_expansion_schema(tmp_path, monkeypatch):
    """Simulates an install that predates the Settings-tab expansion: `users`
    has no session_version column and `app_settings` has no ollama_model/
    ollama_url/swarmui_external_url columns. init_db() must ALTER TABLE them
    into existence — without this, the very first request that touches
    User.session_version or AppSettings.ollama_model crashes every existing
    install (SQLAlchemy raises OperationalError for a column the DB doesn't
    have yet)."""
    db_path = tmp_path / "pre_expansion.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(256) UNIQUE NOT NULL, "
            "password_hash VARCHAR(256) NOT NULL, display_name VARCHAR(256), "
            "is_gm BOOLEAN, created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE app_settings (id INTEGER PRIMARY KEY, static_format VARCHAR(16), "
            "animated_format VARCHAR(16), updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash, display_name, is_gm) "
            "VALUES (1, 'pre-existing@test.local', 'x', 'Pre-existing', 0)"
        ))
        conn.execute(text(
            "INSERT INTO app_settings (id, static_format, animated_format) VALUES (1, 'avif', 'avif')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        user_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        settings_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()}
    assert "session_version" in user_cols
    assert {"ollama_model", "ollama_url", "swarmui_external_url"} <= settings_cols

    # The pre-existing row gets the column's default, not a crash or NULL that'd
    # trip up `!= user.session_version` comparisons downstream.
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT session_version FROM users WHERE id = 1")).fetchone()
        assert row[0] == 1
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_android_emulator_url_schema(tmp_path, monkeypatch):
    """Reproduces a real production incident: an install whose app_settings
    table predates the android_emulator_url/editor_external_url columns (they
    were added to the model but never wired into database.py's heal-table
    column list) crashed on every boot with `sqlalchemy.exc.OperationalError:
    no such column: app_settings.android_emulator_url`, raised from
    get_app_settings() -> db.query(AppSettings).first() -> startup(). init_db()
    must ALTER TABLE the missing columns into existence so an already-deployed
    database recovers on next boot instead of crash-looping."""
    from app.database import get_app_settings

    db_path = tmp_path / "pre_android_emulator_url.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE app_settings (id INTEGER PRIMARY KEY, static_format VARCHAR(16), "
            "animated_format VARCHAR(16), ollama_model VARCHAR(256), ollama_url VARCHAR(512), "
            "swarmui_external_url VARCHAR(512), hover_preview_enabled BOOLEAN, "
            "hover_preview_delay_ms INTEGER, hover_preview_hide_delay_ms INTEGER, "
            "hover_preview_width_px INTEGER, hover_preview_max_height_px INTEGER, "
            "updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO app_settings (id, static_format, animated_format) VALUES (1, 'avif', 'avif')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        settings_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()}
    assert {"android_emulator_url", "editor_external_url"} <= settings_cols

    # The exact call that crashed in production must now succeed.
    db = SessionLocal()
    try:
        s = get_app_settings(db)
        assert s.android_emulator_url == ""
        assert s.editor_external_url == ""
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_lore_extras_toggle_schema(tmp_path, monkeypatch):
    """Same class of bug as test_heals_pre_android_emulator_url_schema above,
    for the dreamlands_enabled/king_in_yellow_enabled columns added alongside
    the optional-lore-extras feature — an existing app_settings row predating
    them must heal onto a false/off default, not crash get_app_settings()."""
    from app.database import get_app_settings

    db_path = tmp_path / "pre_lore_extras.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE app_settings (id INTEGER PRIMARY KEY, static_format VARCHAR(16), "
            "animated_format VARCHAR(16), ollama_model VARCHAR(256), ollama_url VARCHAR(512), "
            "swarmui_external_url VARCHAR(512), android_emulator_url VARCHAR(512), "
            "editor_external_url VARCHAR(512), hover_preview_enabled BOOLEAN, "
            "hover_preview_delay_ms INTEGER, hover_preview_hide_delay_ms INTEGER, "
            "hover_preview_width_px INTEGER, hover_preview_max_height_px INTEGER, "
            "updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO app_settings (id, static_format, animated_format) VALUES (1, 'avif', 'avif')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        settings_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()}
    assert {"dreamlands_enabled", "king_in_yellow_enabled"} <= settings_cols

    db = SessionLocal()
    try:
        s = get_app_settings(db)
        assert not s.dreamlands_enabled
        assert not s.king_in_yellow_enabled
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_home_customization_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating home_title/home_subtitle/home_background_url/
    home_pinned_tiles_json (added for hero-text/background-image/pinned-
    dashboard-tile customization) must heal onto NULL/'[]' defaults, not
    break loading an existing world."""
    db_path = tmp_path / "pre_home_customization.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
            "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
            "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
            "home_sections_json TEXT, custom_kinds_json TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json) "
            "VALUES (1, 'Pre-existing World', 'pre-existing-world', '[]', '[]')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        world_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()}
    assert {"home_title", "home_subtitle", "home_background_url", "home_pinned_tiles_json"} <= world_cols

    db = SessionLocal()
    try:
        w = db.get(World, 1)
        assert w.home_title is None
        assert w.home_subtitle is None
        assert w.home_background_url is None
        assert w.home_pinned_tiles_json in (None, "[]")
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_spotlight_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating spotlight_image_url/spotlight_label/
    spotlight_version (added for the "send an album image to players as a
    popup" feature) must heal onto NULL/0 defaults, not break loading an
    existing world."""
    db_path = tmp_path / "pre_spotlight.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
            "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
            "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
            "home_sections_json TEXT, custom_kinds_json TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json) "
            "VALUES (1, 'Pre-existing World', 'pre-existing-world', '[]', '[]')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        world_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()}
    assert {"spotlight_image_url", "spotlight_label", "spotlight_version"} <= world_cols

    db = SessionLocal()
    try:
        w = db.get(World, 1)
        assert w.spotlight_image_url is None
        assert w.spotlight_label is None
        assert w.spotlight_version in (None, 0)
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_nested_albums_schema(tmp_path, monkeypatch):
    """An image_albums table predating parent_id (added so an album can
    nest inside another as a folder) must heal onto a NULL (top-level)
    default, not crash the /images gallery on every boot."""
    from app.models import ImageAlbum

    db_path = tmp_path / "pre_nested_albums.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
            "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
            "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
            "home_sections_json TEXT, custom_kinds_json TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json) "
            "VALUES (1, 'World', 'world', '[]', '[]')"
        ))
        conn.execute(text(
            "CREATE TABLE image_albums (id INTEGER PRIMARY KEY, world_id INTEGER NOT NULL, "
            "name VARCHAR(120) NOT NULL, image_urls_json TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO image_albums (id, world_id, name, image_urls_json) "
            "VALUES (1, 1, 'Pre-existing Album', '[]')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        album_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(image_albums)")).fetchall()}
    assert "parent_id" in album_cols

    db = SessionLocal()
    try:
        album = db.get(ImageAlbum, 1)
        assert album.parent_id is None  # healed as top-level, not lost
        assert album.name == "Pre-existing Album"
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_two_step_auth_schema(tmp_path, monkeypatch):
    """A users table predating totp_secret/totp_enabled/totp_backup_codes_json
    (added for optional two-step authentication, see app/totp.py) must heal
    onto NULL/false/'[]' defaults — a pre-existing user should come back with
    two-step authentication off, not crash the first login after upgrade."""
    from app.models import User

    db_path = tmp_path / "pre_two_step_auth.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(256) UNIQUE NOT NULL, "
            "password_hash VARCHAR(256) NOT NULL, display_name VARCHAR(256), "
            "is_gm BOOLEAN, created_at DATETIME, session_version INTEGER DEFAULT 1)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash, display_name, is_gm, session_version) "
            "VALUES (1, 'pre-existing@test.local', 'x', 'Pre-existing', 0, 1)"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        user_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
    assert {"totp_secret", "totp_enabled", "totp_backup_codes_json"} <= user_cols

    db = SessionLocal()
    try:
        u = db.get(User, 1)
        assert u.totp_secret is None
        assert not u.totp_enabled
        assert u.totp_backup_codes_json in (None, "[]")
    finally:
        db.close()

    engine.dispose()
