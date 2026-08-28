"""Regression test for the one-time migration guard (app/database.py's
_once/schema_meta): the legacy-import cleanup used to run its destructive DML
(including a kind='item' -> 'feat' reclassification) unconditionally on every
boot, silently reverting a GM's deliberate edit on every container restart —
which with Watchtower polling happens every few minutes in production.
"""
import json

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


def test_heals_pre_ollama_server_env_schema(tmp_path, monkeypatch):
    """An app_settings row predating the Bucket-C per-request fields (min_p,
    num_batch, etc.) and ollama_server_env_json/ollama_vram_override_mb
    (the server-level Ollama tuning + hardware-detection feature) must heal
    onto NULL/'' defaults, not crash get_app_settings()."""
    from app.database import get_app_settings

    db_path = tmp_path / "pre_ollama_server_env.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE app_settings (id INTEGER PRIMARY KEY, static_format VARCHAR(16), "
            "animated_format VARCHAR(16), ollama_model VARCHAR(256), ollama_url VARCHAR(512), "
            "ollama_temperature FLOAT, ollama_num_gpu INTEGER, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO app_settings (id, static_format, animated_format) VALUES (1, 'avif', 'avif')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        settings_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()}
    assert {
        "ollama_min_p", "ollama_typical_p", "ollama_repeat_last_n", "ollama_presence_penalty",
        "ollama_frequency_penalty", "ollama_num_keep", "ollama_num_batch", "ollama_num_thread",
        "ollama_main_gpu", "ollama_use_mmap", "ollama_server_env_json", "ollama_vram_override_mb",
    } <= settings_cols

    db = SessionLocal()
    try:
        s = get_app_settings(db)
        assert s.ollama_min_p is None
        assert s.ollama_num_batch is None
        assert s.ollama_use_mmap == "" or s.ollama_use_mmap is None
        assert json.loads(s.ollama_server_env_json or "{}") == {}
        assert s.ollama_vram_override_mb is None
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_whisper_url_schema(tmp_path, monkeypatch):
    """Same class of bug as the android_emulator_url/lore-extras heal tests
    above, for the whisper_url column added alongside the optional Whisper
    audio-transcription integration — an existing app_settings row predating
    it must heal onto a blank default, not crash get_app_settings()."""
    from app.database import get_app_settings

    db_path = tmp_path / "pre_whisper_url.db"
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
            "dreamlands_enabled BOOLEAN, king_in_yellow_enabled BOOLEAN, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO app_settings (id, static_format, animated_format) VALUES (1, 'avif', 'avif')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        settings_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()}
    assert "whisper_url" in settings_cols

    db = SessionLocal()
    try:
        s = get_app_settings(db)
        assert s.whisper_url == ""
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


def test_heals_pre_hero_style_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating hero_style (the GM's off/home/everywhere
    toggle for where the hero banner shows up) must heal onto the same
    default the column itself declares ('home') — the one behavior that
    already existed for every world before this toggle was added — not
    NULL, which base.html/index.html's `or 'home'` fallbacks handle too,
    but the column default is the first line of defense."""
    db_path = tmp_path / "pre_hero_style.db"
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
    assert "hero_style" in world_cols

    db = SessionLocal()
    try:
        w = db.get(World, 1)
        assert w.hero_style == "home"
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_whisper_denoise_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating whisper_denoise (the per-World speech-
    enhancement opt-in toggle) must heal onto False, not break loading an
    existing world."""
    db_path = tmp_path / "pre_whisper_denoise.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
            "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
            "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
            "home_sections_json TEXT, custom_kinds_json TEXT, whisper_glossary TEXT, "
            "whisper_language VARCHAR(16), created_at DATETIME)"
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
    assert "whisper_denoise" in world_cols

    db = SessionLocal()
    try:
        w = db.get(World, 1)
        assert not w.whisper_denoise
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


def test_heals_pre_video_conversion_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating video_convert_enabled/video_convert_max_height/
    video_convert_bitrate_kbps (added for the Video Library's space-saving
    AV1 conversion option) must heal onto NULL/0 defaults, not break
    loading an existing world."""
    db_path = tmp_path / "pre_video_conversion.db"
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
    assert {"video_convert_enabled", "video_convert_max_height", "video_convert_bitrate_kbps"} <= world_cols

    db = SessionLocal()
    try:
        w = db.get(World, 1)
        assert w.video_convert_enabled in (None, False)
        assert w.video_convert_max_height is None
        assert w.video_convert_bitrate_kbps is None
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


def test_heals_pre_job_resume_audio_jobs_schema(tmp_path, monkeypatch):
    """An audio_jobs table predating audio_path/delete_after/checkpoint_json/
    resumed_count (added so a transcription/summarization job can resume
    from its last checkpoint after a server restart instead of losing the
    work — see app/job_shutdown.py) must heal onto usable defaults, and a
    pre-existing row's own data (its transcript in particular) must survive
    the heal untouched."""
    from app.models import AudioJob

    db_path = tmp_path / "pre_job_resume_audio.db"
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
            "CREATE TABLE audio_jobs (id INTEGER PRIMARY KEY, world_id INTEGER NOT NULL, "
            "created_by_user_id INTEGER, purpose VARCHAR(32) NOT NULL, game_session_id INTEGER, "
            "filename VARCHAR(256), status VARCHAR(32), error TEXT, transcript TEXT, recap TEXT, "
            "attachment_url VARCHAR(512), model VARCHAR(128), extra_instructions TEXT, "
            "chunk_current INTEGER, chunk_total INTEGER, run_started_at DATETIME, "
            "finished_at DATETIME, created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO audio_jobs (id, world_id, purpose, status, transcript) "
            "VALUES (1, 1, 'session_recap', 'done', 'the party explored the ruins')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(audio_jobs)")).fetchall()}
    assert {"audio_path", "delete_after", "checkpoint_json", "resumed_count"} <= cols

    db = SessionLocal()
    try:
        job = db.get(AudioJob, 1)
        assert job.transcript == "the party explored the ruins"  # untouched by the heal
        assert job.audio_path == ""
        assert job.delete_after is True
        assert job.checkpoint_json == ""
        assert job.resumed_count == 0
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_thinking_toggle_audio_jobs_schema(tmp_path, monkeypatch):
    """An audio_jobs table predating the think/fit_context/min_tokens/
    max_tokens columns (the "Thinking" checkbox and Condense's "fit
    context"/length-target options) must heal onto usable defaults on both
    a fresh DB and one with existing rows, without disturbing that row's
    own data."""
    from app.models import AudioJob

    db_path = tmp_path / "pre_thinking_toggle_audio.db"
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
            "CREATE TABLE audio_jobs (id INTEGER PRIMARY KEY, world_id INTEGER NOT NULL, "
            "created_by_user_id INTEGER, purpose VARCHAR(32) NOT NULL, game_session_id INTEGER, "
            "filename VARCHAR(256), status VARCHAR(32), error TEXT, transcript TEXT, recap TEXT, "
            "attachment_url VARCHAR(512), model VARCHAR(128), extra_instructions TEXT, "
            "chunk_current INTEGER, chunk_total INTEGER, run_started_at DATETIME, "
            "finished_at DATETIME, created_at DATETIME, updated_at DATETIME, "
            "audio_path VARCHAR(1024), delete_after BOOLEAN, checkpoint_json TEXT, resumed_count INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO audio_jobs (id, world_id, purpose, status, transcript) "
            "VALUES (1, 1, 'session_recap', 'done', 'the party explored the ruins')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(audio_jobs)")).fetchall()}
    assert {"think", "fit_context", "min_tokens", "max_tokens", "use_rag", "rag_entity_limit", "rag_notes_limit"} <= cols

    db = SessionLocal()
    try:
        job = db.get(AudioJob, 1)
        assert job.transcript == "the party explored the ruins"  # untouched by the heal
        assert job.think is None  # heals to NULL — treated as True by the caller
        assert job.fit_context in (0, False, None)
        assert job.min_tokens is None
        assert job.max_tokens is None
        assert job.use_rag in (0, False, None)
        assert job.rag_entity_limit is None
        assert job.rag_notes_limit is None
    finally:
        db.close()

    engine.dispose()


def test_heals_pre_job_resume_image_and_chat_jobs_schema(tmp_path, monkeypatch):
    """image_jobs/chat_jobs tables predating resumed_count (see
    app/job_shutdown.py — caps how many times a job auto-restarts itself
    after being interrupted by a server restart) must heal onto 0, not
    crash resume_interrupted_jobs on the first boot after upgrade."""
    from app.models import ChatJob, ImageJob

    db_path = tmp_path / "pre_job_resume_image_chat.db"
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
            "CREATE TABLE image_jobs (id INTEGER PRIMARY KEY, world_id INTEGER NOT NULL, "
            "created_by_user_id INTEGER, prompt TEXT, params_json TEXT, status VARCHAR(32), "
            "error TEXT, result_urls_json TEXT, created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO image_jobs (id, world_id, prompt, status, result_urls_json) "
            "VALUES (1, 1, 'a neon skyline', 'done', '[\"/uploads/ai-images/x.png\"]')"
        ))
        conn.execute(text(
            "CREATE TABLE chat_jobs (id INTEGER PRIMARY KEY, world_id INTEGER NOT NULL, "
            "created_by_user_id INTEGER, prompt TEXT, messages_json TEXT, system TEXT, "
            "model VARCHAR(128), options_json TEXT, status VARCHAR(32), error TEXT, result TEXT, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO chat_jobs (id, world_id, prompt, status, result) "
            "VALUES (1, 1, 'summarize the lore', 'done', 'a short answer')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocal)

    database_module.init_db()

    with engine.begin() as conn:
        image_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(image_jobs)")).fetchall()}
        chat_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(chat_jobs)")).fetchall()}
    assert "resumed_count" in image_cols
    assert "resumed_count" in chat_cols

    db = SessionLocal()
    try:
        image_job = db.get(ImageJob, 1)
        assert image_job.resumed_count == 0
        assert image_job.result_urls_json == '["/uploads/ai-images/x.png"]'  # untouched by the heal
        chat_job = db.get(ChatJob, 1)
        assert chat_job.resumed_count == 0
        assert chat_job.result == "a short answer"
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


def test_sqlite_wal_mode_and_busy_timeout_enabled(client):
    """Regression test for a production incident: SQLite's default
    rollback-journal mode blocks every reader behind any in-flight writer,
    and base.html's spotlight poller alone hits GET /api/spotlight every 4s
    from every open tab — so a single slow write backed up enough concurrent
    readers to exhaust the SQLAlchemy pool (5 + 10 overflow) and the app
    started failing app-wide with QueuePool TimeoutError until the write
    cleared. See app/database.py's engine "connect" event."""
    with database_module.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000
