import json
import logging
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import (
    Base, World, Schematic, MapOverlay, InvestBoard, PlayerCharacter, SheetTemplate,
    StarredImage, User, WorldMembership, InviteCode, EntityTemplate, RandomTable,
    AppSettings,
)


_log = logging.getLogger("nd.db")


def get_app_settings(db):
    """The single instance-wide settings row, created lazily on first use."""
    s = db.query(AppSettings).first()
    if not s:
        s = AppSettings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s

def _heal_table(conn, table, columns, foreign_keys=None, indexes=None, unique_indexes=None):
    """Self-heal a table against its model's expected columns: add whatever's
    missing, and rebuild (SQLite can't ALTER to drop a NOT NULL constraint)
    if any column the model marks nullable ended up NOT NULL in the DB —
    the same class of issue seen on random_tables/game_sessions after some
    installs ended up with tables that don't fully match their models.

    `columns` is [(name, add_column_sql_defn, nullable), ...], excluding id.

    The rebuild path used to emit a bare `CREATE TABLE` with just those
    columns, silently dropping any FOREIGN KEY constraints, indexes, and
    UNIQUE indexes the model actually declares — `ALTER TABLE ADD COLUMN`
    can't add them, so once a table went through one rebuild without them
    they stayed missing forever (create_all() only creates tables that don't
    exist yet, it never alters an existing one to add a missing index).
    `foreign_keys` is [(column, ref_table, ref_column), ...],
    `indexes` / `unique_indexes` are plain lists of column names — pass the
    same ones the SQLAlchemy model declares via ForeignKey()/index=True/
    unique=True so a healed table matches the model again, not just its
    column list.
    """
    exists = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    ), {"t": table}).fetchone()
    if not exists:
        return
    info = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    existing = {r[1]: r[3] for r in info}  # name -> notnull flag
    for name, defn, _nullable in columns:
        if name not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {defn}"))
    info = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    existing = {r[1]: r[3] for r in info}
    if any(nullable and existing.get(name) == 1 for name, _, nullable in columns):
        tmp = f"{table}_old"
        conn.execute(text(f"ALTER TABLE {table} RENAME TO {tmp}"))
        col_sql = ",\n                ".join(f"{name} {defn}" for name, defn, _ in columns)
        fk_sql = "".join(
            f",\n                FOREIGN KEY ({col}) REFERENCES {ref_table}({ref_col})"
            for col, ref_table, ref_col in (foreign_keys or [])
        )
        conn.execute(text(
            f"CREATE TABLE {table} (\n                id INTEGER PRIMARY KEY,\n                {col_sql}{fk_sql}\n            )"
        ))
        col_names = ", ".join(["id"] + [c[0] for c in columns])
        conn.execute(text(f"INSERT INTO {table} ({col_names}) SELECT {col_names} FROM {tmp}"))
        conn.execute(text(f"DROP TABLE {tmp}"))
        for col in (indexes or []):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{col} ON {table} ({col})"))
        for col in (unique_indexes or []):
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_{col} ON {table} ({col})"))


def _f(id, label, type="text", section="", default_value=""):
    return {"id": id, "label": label, "type": type, "section": section, "default_value": default_value}

def _list_f(id, label, section, item_fields):
    return {"id": id, "label": label, "type": "list", "section": section, "item_fields": item_fields}

def _select_f(id, label, section, options, default_value=""):
    return {"id": id, "label": label, "type": "select", "section": section,
            "options": options, "default_value": default_value}

_NPC_DETAILS_FIELDS = [
    _f("title", "Title", "text", "Details"),
    _f("age", "Age", "text", "Details"),
    _f("gender", "Gender", "text", "Details"),
    _select_f("status", "Status", "Details", ["Alive", "Dead", "Missing", "Unknown"], "Alive"),
]

_STAT_BLOCK_FIELDS = [
    _f("attackPool", "Attack Pool", "text", "Stat Block", "1d10"),
    _f("defensePool", "Defense Pool", "text", "Stat Block", "1d10"),
    _f("health", "Health / HP", "number", "Stat Block", "5"),
    _f("armor", "Armor", "number", "Stat Block", "0"),
    _f("speed", "Speed", "text", "Stat Block"),
    _list_f("abilities", "Abilities", "Stat Block", [
        {"id": "name", "label": "Name", "type": "text"},
        {"id": "effect", "label": "Effect", "type": "textarea"},
    ]),
]

_ASTERION_FIELDS = [
    _f("origin", "Origin (Gods) / Lineage (Mythborn)", "text", "Identity"),
    _f("spark", "Divine Spark / Domain", "text", "Identity"),
    _f("sentence", "Character Sentence", "text", "Identity"),
    _f("appearance", "Appearance & Personality", "textarea", "Identity"),

    _f("sparkShield", "Spark Shield", "number", "Core Stats", "3"),
    _f("flesh", "Flesh", "number", "Core Stats", "5"),
    _f("ichor", "Ichor", "number", "Core Stats", "5"),
    _f("armor", "Armor", "number", "Core Stats", "0"),
    _f("attackPool", "Attacker Pool", "text", "Core Stats"),
    _f("defensePool", "Defender Pool", "text", "Core Stats"),
    _f("movement", "Movement", "text", "Core Stats", "30 ft / 6 hexes"),

    _f("abOriginName", "Origin/Lineage Ability — Name", "text", "Abilities"),
    _f("abOriginTier", "Origin/Lineage Ability — Tier/Type", "text", "Abilities"),
    _f("abOriginText", "Origin/Lineage Ability — Effect", "textarea", "Abilities"),
    _f("abSparkName", "Spark Ability — Name", "text", "Abilities"),
    _f("abSparkTier", "Spark Ability — Tier/Type", "text", "Abilities"),
    _f("abSparkText", "Spark Ability — Effect", "textarea", "Abilities"),
    _f("abDeedName", "Deed/Curse Ability — Name", "text", "Abilities"),
    _f("abDeedTier", "Deed/Curse Ability — Tier/Type", "text", "Abilities"),
    _f("abDeedText", "Deed/Curse Ability — Effect", "textarea", "Abilities"),
    _list_f("extraAbilities", "Additional Abilities", "Abilities", [
        {"id": "name", "label": "Name", "type": "text"},
        {"id": "tier", "label": "Tier / Type", "type": "text"},
        {"id": "text", "label": "Effect", "type": "textarea"},
    ]),

    _f("drachma", "Drachma (Currency)", "number", "Inventory & Equipment", "0"),
    _f("weapon", "Weapon Equipped", "text", "Inventory & Equipment"),
    _f("armorItem", "Armor Equipped", "text", "Inventory & Equipment"),
    _list_f("consumables", "Consumables (max 3)", "Inventory & Equipment", [
        {"id": "name", "label": "Item", "type": "text"},
    ]),
    _f("artifacts", "Artifacts / Notable Items", "textarea", "Inventory & Equipment"),

    _f("glory", "Glory (XP)", "number", "Progression", "0"),
    _f("domainRank", "Domain Rank", "text", "Progression"),
    _f("reputation", "Reputation / Titles", "text", "Progression"),
    _list_f("milestones", "Milestones Achieved", "Progression", [
        {"id": "name", "label": "Milestone", "type": "text"},
    ]),

    _f("sessionNum", "Current Session #", "text", "Campaign Log"),
    _list_f("sessionLog", "Session Notes", "Campaign Log", [
        {"id": "session", "label": "Session / Date", "type": "text"},
        {"id": "text", "label": "Summary", "type": "textarea"},
    ]),

    _list_f("relationships", "Relationships & Allies", "Relationships & Allies", [
        {"id": "name", "label": "Name", "type": "text"},
        {"id": "text", "label": "Relation / Notes", "type": "textarea"},
    ]),

    _f("freeNotes", "GM / Free Notes", "textarea", "Notes"),
]

DB_PATH = os.environ.get("DB_PATH", "/data/world.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
    try:
        _migrate()
    except Exception:
        # Startup dies either way, but without this the container just crash-loops
        # on a bare traceback. Say plainly that the schema was left untouched, so
        # the DB isn't suspected of being half-migrated (it can't be — _migrate
        # runs in a single transaction and rolls back as a unit).
        _log.exception("Schema migration failed; database left unchanged (rolled back)")
        raise
    _seed()


def _once(conn, key: str, fn) -> bool:
    """Run `fn(conn)` only if `key` has never been applied, then record it.

    Guards one-time data repairs. Schema DDL doesn't need this — it re-derives
    what's missing from PRAGMA table_info on every boot and is naturally
    idempotent — but data fixes are not: re-running them fights the GM's own
    edits, and a DELETE re-runs destructively forever.
    """
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        " key TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    ))
    already = conn.execute(
        text("SELECT 1 FROM schema_meta WHERE key = :k"), {"k": key}
    ).fetchone()
    if already:
        return False
    fn(conn)
    conn.execute(text("INSERT INTO schema_meta (key) VALUES (:k)"), {"k": key})
    _log.info("Applied one-time migration: %s", key)
    return True


def _migrate():
    # engine.begin() wraps the whole migration in one transaction: SQLite supports
    # transactional DDL, so a failure part-way rolls the entire thing back instead
    # of leaving a half-migrated schema (and, for the _heal_table rebuild path,
    # instead of leaving both `<table>_old` and a partial `<table>` behind).
    with engine.begin() as conn:
        # Add world_id column to existing entities table if missing
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(entities)")).fetchall()]
        if "world_id" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN world_id INTEGER NOT NULL DEFAULT 1"))
        if "folder" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN folder VARCHAR(256)"))
        if "visible_to_players" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN visible_to_players BOOLEAN DEFAULT 1"))
        if "template_id" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN template_id INTEGER"))
        if "custom_fields_json" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN custom_fields_json TEXT DEFAULT '{}'"))
        # One-time repair of damage done by the earliest lore-import runs. Gated by
        # _once because none of it is safe to repeat: the DELETE is destructive, and
        # the kind='item' → 'feat' reclassification silently overrides a GM who
        # deliberately sets such an entity back to 'item' — every container restart,
        # which with Watchtower polling is every few minutes.
        def _cleanup_legacy_import(c):
            # Clean up literal "None" strings stored by early import runs
            c.execute(text("UPDATE entities SET folder  = NULL WHERE folder  = 'None'"))
            c.execute(text("UPDATE entities SET summary = NULL WHERE summary = 'None'"))
            c.execute(text("UPDATE entities SET body    = NULL WHERE body    = 'None'"))
            c.execute(text("UPDATE entities SET subtype = NULL WHERE subtype = 'None'"))
            # Delete entities with missing names (string 'None', null, or blank)
            c.execute(text("DELETE FROM entities WHERE name IS NULL OR TRIM(name) = '' OR name = 'None'"))
            # Re-classify equipment feat directories: they were imported as items but are feats
            c.execute(text(
                "UPDATE entities SET kind = 'feat' WHERE kind = 'item' AND folder LIKE '%Feat%'"
            ))
            # Prefix bare Rank/Origin/Edge folders on feats with 'Common Feats/' parent
            c.execute(text(
                "UPDATE entities SET folder = 'Common Feats/' || folder "
                "WHERE kind = 'feat' AND folder IN ('Rank 1', 'Rank 2', 'Rank 3', 'Origin', 'Edge')"
            ))

        _once(conn, "cleanup_legacy_import_v1", _cleanup_legacy_import)

        # Add new Schematic columns if missing
        sch_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(schematics)")).fetchall()] if conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='schematics'")).fetchone() else []
        for col, defn in [("canvas_width", "INTEGER DEFAULT 2000"), ("canvas_height", "INTEGER DEFAULT 1500"), ("canvas_bg", "VARCHAR(32) DEFAULT 'dark'"), ("elements_json", "TEXT DEFAULT '[]'"), ("grid_type", "VARCHAR(16) DEFAULT 'none'"), ("grid_config_json", "TEXT DEFAULT '{}'"), ("combat_session_id", "INTEGER REFERENCES combat_sessions(id)")]:
            if sch_cols and col not in sch_cols:
                conn.execute(text(f"ALTER TABLE schematics ADD COLUMN {col} {defn}"))
        # app_settings: the original convert_images_avif/convert_animated_avif
        # booleans were replaced by static_format/animated_format ("none"/
        # "avif"/"webp") almost immediately after shipping — heal existing
        # installs onto the new columns and translate any already-customized
        # boolean values instead of silently resetting them back to "avif".
        as_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
        )).fetchone()
        if as_exists:
            as_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(app_settings)")).fetchall()]
            added_format_cols = False
            for col, defn in [("static_format", "VARCHAR(16) DEFAULT 'avif'"), ("animated_format", "VARCHAR(16) DEFAULT 'avif'")]:
                if col not in as_cols:
                    conn.execute(text(f"ALTER TABLE app_settings ADD COLUMN {col} {defn}"))
                    added_format_cols = True
            if added_format_cols and "convert_images_avif" in as_cols:
                conn.execute(text(
                    "UPDATE app_settings SET static_format = CASE WHEN convert_images_avif = 0 THEN 'none' ELSE 'avif' END"
                ))
                conn.execute(text(
                    "UPDATE app_settings SET animated_format = CASE WHEN convert_animated_avif = 0 THEN 'none' ELSE 'avif' END"
                ))
            for col, defn in [
                ("ollama_model", "VARCHAR(256) DEFAULT ''"),
                ("ollama_url", "VARCHAR(512) DEFAULT ''"),
                ("swarmui_external_url", "VARCHAR(512) DEFAULT ''"),
                ("android_emulator_url", "VARCHAR(512) DEFAULT ''"),
                ("editor_external_url", "VARCHAR(512) DEFAULT ''"),
                ("hover_preview_enabled", "BOOLEAN DEFAULT 1"),
                ("hover_preview_delay_ms", "INTEGER DEFAULT 5000"),
                ("hover_preview_hide_delay_ms", "INTEGER DEFAULT 400"),
                ("hover_preview_width_px", "INTEGER DEFAULT 340"),
                ("hover_preview_max_height_px", "INTEGER DEFAULT 420"),
                ("dreamlands_enabled", "BOOLEAN DEFAULT 0"),
                ("king_in_yellow_enabled", "BOOLEAN DEFAULT 0"),
            ]:
                if col not in as_cols:
                    conn.execute(text(f"ALTER TABLE app_settings ADD COLUMN {col} {defn}"))
        # users table — session_version backs the password-change "log out my other
        # sessions" feature (see /account/password and auth_gate in main.py).
        u_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )).fetchone()
        if u_exists:
            u_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if "session_version" not in u_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN session_version INTEGER DEFAULT 1"))
        # player_characters table — add any missing columns to existing installs
        pc_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='player_characters'"
        )).fetchone()
        if pc_exists:
            pc_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(player_characters)")).fetchall()]
            _pc_extra = [
                ("death_saves_success",        "INTEGER DEFAULT 0"),
                ("death_saves_failure",         "INTEGER DEFAULT 0"),
                ("skill_expertise",             "TEXT DEFAULT '[]'"),
                ("currency_ep",                 "INTEGER DEFAULT 0"),
                ("weight_app",                  "VARCHAR(32) DEFAULT ''"),
                ("stats_json",                  "TEXT DEFAULT '[]'"),
                ("skills_json",                 "TEXT DEFAULT '[]'"),
                ("currency_json",               "TEXT DEFAULT '[]'"),
                ("secondary_resource_name",     "VARCHAR(128) DEFAULT ''"),
                ("secondary_resource_max",      "INTEGER DEFAULT 0"),
                ("secondary_resource_current",  "INTEGER DEFAULT 0"),
                ("shock_max",                   "INTEGER DEFAULT 0"),
                ("shock_current",               "INTEGER DEFAULT 0"),
                ("pp_current",                  "INTEGER DEFAULT 0"),
                ("mp_current",                  "INTEGER DEFAULT 0"),
                ("minor_edge",                  "TEXT DEFAULT ''"),
                ("major_edge",                  "TEXT DEFAULT ''"),
                ("cyberware_json",              "TEXT DEFAULT '[]'"),
                ("conditions_json",             "TEXT DEFAULT '[]'"),
                ("sheet_template_id",           "INTEGER"),
                ("custom_fields_json",          "TEXT DEFAULT '{}'"),
                ("race_id",                     "VARCHAR(128) DEFAULT ''"),
                ("profession_id",                "VARCHAR(128) DEFAULT ''"),
                ("minor_edge_count",             "INTEGER DEFAULT 0"),
                ("major_edge_count",             "INTEGER DEFAULT 0"),
                ("owner_user_id",                "INTEGER"),
                ("app_extra_json",                "TEXT DEFAULT '{}'"),
            ]
            for col, defn in _pc_extra:
                if col not in pc_cols:
                    conn.execute(text(f"ALTER TABLE player_characters ADD COLUMN {col} {defn}"))
        # sheet_templates table migration — ensure it exists (Base.metadata handles creation,
        # but old DBs may lack the table until next create_all call)
        tpl_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sheet_templates'"
        )).fetchone()
        if tpl_exists:
            tpl_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sheet_templates)")).fetchall()]
            if "sheet_mode" not in tpl_cols:
                conn.execute(text("ALTER TABLE sheet_templates ADD COLUMN sheet_mode VARCHAR(16) DEFAULT 'nd'"))
        # worlds table — add players_see_party if missing
        w_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worlds'"
        )).fetchone()
        if w_exists:
            w_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()]
            if "players_see_party" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN players_see_party BOOLEAN DEFAULT 1"))
            if "rules_md" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN rules_md TEXT"))
            if "home_welcome_md" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_welcome_md TEXT"))
            if "home_sections_json" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_sections_json TEXT DEFAULT '[]'"))
            if "custom_kinds_json" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN custom_kinds_json TEXT DEFAULT '[]'"))
            if "home_title" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_title VARCHAR(200)"))
            if "home_subtitle" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_subtitle VARCHAR(300)"))
            if "home_background_url" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_background_url VARCHAR(512)"))
            if "home_pinned_tiles_json" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_pinned_tiles_json TEXT DEFAULT '[]'"))
            if "home_hidden_kinds_json" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN home_hidden_kinds_json TEXT DEFAULT '[]'"))
            if "nav_menus_json" not in w_cols:
                # Deliberately no DEFAULT clause — existing rows get NULL,
                # which app/nav_menus.py's load_nav_menus() treats as "never
                # customized" and falls back to the shipped Tools/AI Tools
                # grouping (see World.nav_menus_json's docstring).
                conn.execute(text("ALTER TABLE worlds ADD COLUMN nav_menus_json TEXT"))
        # random_tables table — some installs ended up with a table that
        # doesn't match the model: missing columns (e.g. slug), and/or
        # world_id incorrectly marked NOT NULL (the model allows NULL there
        # for global/built-in tables). Both crash _seed() on every boot,
        # since create_all() never alters an existing table. Patch in
        # whatever's missing first...
        rt_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='random_tables'"
        )).fetchone()
        if rt_exists:
            rt_info = conn.execute(text("PRAGMA table_info(random_tables)")).fetchall()
            rt_cols = [r[1] for r in rt_info]
            _rt_extra = [
                ("world_id",     "INTEGER"),
                ("name",         "VARCHAR(256)"),
                ("slug",         "VARCHAR(64)"),
                ("category",     "VARCHAR(64) DEFAULT 'general'"),
                ("description",  "TEXT DEFAULT ''"),
                ("is_builtin",   "BOOLEAN DEFAULT 0"),
                ("entries_json", "TEXT DEFAULT '[]'"),
                ("created_at",   "DATETIME"),
                ("updated_at",   "DATETIME"),
            ]
            for col, defn in _rt_extra:
                if col not in rt_cols:
                    conn.execute(text(f"ALTER TABLE random_tables ADD COLUMN {col} {defn}"))
            # ...then check for a world_id NOT NULL constraint left over from
            # however this table was first created. SQLite can't drop a NOT
            # NULL constraint via ALTER TABLE, so rebuild the table properly
            # (the standard SQLite "12-step" approach) if that's the case.
            rt_info = conn.execute(text("PRAGMA table_info(random_tables)")).fetchall()
            world_id_notnull = any(r[1] == "world_id" and r[3] == 1 for r in rt_info)
            if world_id_notnull:
                conn.execute(text("ALTER TABLE random_tables RENAME TO random_tables_old"))
                # FOREIGN KEY + the world_id index are declared here (not just
                # `slug ... UNIQUE`) so a healed table matches the model again —
                # ALTER TABLE ADD COLUMN can add neither retroactively, and
                # create_all() never alters a table that already exists.
                conn.execute(text("""
                    CREATE TABLE random_tables (
                        id INTEGER PRIMARY KEY,
                        world_id INTEGER,
                        name VARCHAR(256),
                        slug VARCHAR(64) UNIQUE,
                        category VARCHAR(64) DEFAULT 'general',
                        description TEXT DEFAULT '',
                        is_builtin BOOLEAN DEFAULT 0,
                        entries_json TEXT DEFAULT '[]',
                        created_at DATETIME,
                        updated_at DATETIME,
                        FOREIGN KEY (world_id) REFERENCES worlds(id)
                    )
                """))
                cols = "id, world_id, name, slug, category, description, is_builtin, entries_json, created_at, updated_at"
                conn.execute(text(f"INSERT INTO random_tables ({cols}) SELECT {cols} FROM random_tables_old"))
                conn.execute(text("DROP TABLE random_tables_old"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_random_tables_world_id ON random_tables (world_id)"))
            # slug has a UNIQUE index on the model but a column added via ALTER
            # TABLE can't carry that constraint retroactively — backfill any
            # NULL slugs (from rows that predate the column) so the app's own
            # slug-uniqueness checks still work going forward.
            conn.execute(text(
                "UPDATE random_tables SET slug = 'table-' || id WHERE slug IS NULL"
            ))

        # Other campaign-management tables shipped in the same batch as
        # random_tables turned out to have the same class of issue on some
        # installs (missing columns / stray NOT NULL constraints — see
        # random_tables above for why). Self-heal all of them the same way.
        _heal_table(conn, "combat_sessions", [
            ("world_id",         "INTEGER", False),
            ("name",             "VARCHAR(256)", False),
            ("combatants_json",  "TEXT DEFAULT '[]'", True),
            ("round_num",        "INTEGER DEFAULT 1", True),
            ("active_idx",       "INTEGER DEFAULT 0", True),
            ("game_session_id",  "INTEGER", True),
            ("created_at",       "DATETIME", True),
            ("updated_at",       "DATETIME", True),
        ], foreign_keys=[
            ("world_id", "worlds", "id"),
            ("game_session_id", "game_sessions", "id"),
        ], indexes=["world_id", "game_session_id"])
        _heal_table(conn, "parties", [
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
        _heal_table(conn, "quests", [
            ("world_id",              "INTEGER", False),
            ("title",                 "VARCHAR(256)", False),
            ("status",                "VARCHAR(32) DEFAULT 'active'", True),
            ("category",              "VARCHAR(32) DEFAULT 'main'", True),
            ("summary",               "VARCHAR(512) DEFAULT ''", True),
            ("body",                  "TEXT DEFAULT ''", True),
            ("linked_entities_json",  "TEXT DEFAULT '[]'", True),
            ("parent_id",             "INTEGER", True),
            ("assigned_party_id",     "INTEGER", True),
            ("visible_to_players",    "BOOLEAN DEFAULT 1", True),
            ("created_at",            "DATETIME", True),
            ("updated_at",            "DATETIME", True),
        ], foreign_keys=[
            ("world_id", "worlds", "id"),
            ("parent_id", "quests", "id"),
            ("assigned_party_id", "parties", "id"),
        ], indexes=["world_id", "parent_id", "assigned_party_id"])
        _heal_table(conn, "game_sessions", [
            ("world_id",      "INTEGER", False),
            ("title",         "VARCHAR(256)", False),
            ("session_num",   "INTEGER DEFAULT 1", True),
            ("session_date",  "VARCHAR(32)", True),
            ("summary",       "TEXT DEFAULT ''", True),
            ("prep_json",     "TEXT DEFAULT '[]'", True),
            ("npcs_json",     "TEXT DEFAULT '[]'", True),
            ("loot_json",     "TEXT DEFAULT '[]'", True),
            ("xp_awarded",    "INTEGER DEFAULT 0", True),
            ("party_id",      "INTEGER", True),
            ("created_at",    "DATETIME", True),
            ("updated_at",    "DATETIME", True),
        ], foreign_keys=[
            ("world_id", "worlds", "id"),
            ("party_id", "parties", "id"),
        ], indexes=["world_id", "party_id"])
        _heal_table(conn, "world_calendars", [
            ("world_id",     "INTEGER", False),
            ("config_json",  "TEXT DEFAULT '{}'", True),
            ("updated_at",   "DATETIME", True),
        ], foreign_keys=[("world_id", "worlds", "id")], unique_indexes=["world_id"])
        _heal_table(conn, "calendar_events", [
            ("world_id",    "INTEGER", False),
            ("day",         "INTEGER", False),
            ("title",       "VARCHAR(256)", False),
            ("notes",       "TEXT DEFAULT ''", True),
            ("entity_id",   "INTEGER", True),
            ("color",       "VARCHAR(16) DEFAULT '#4488ff'", True),
            ("created_at",  "DATETIME", True),
        ], foreign_keys=[
            ("world_id", "worlds", "id"),
            ("entity_id", "entities", "id"),
        ], indexes=["world_id", "entity_id"])

def _seed():
    db = SessionLocal()
    try:
        if db.query(World).count() == 0:
            db.add(World(
                id=1,
                name="Neon & Dragons",
                slug="neon-dragons",
                description="Cyberpunk-fantasy dystopia where megacorps and eldritch corruption collide.",
                accent="#00f0ff",
            ))
            db.commit()
        # Seed the two bundled HTML schematics if not yet present
        _html_schematics = [
            ("City of Hue", "city-of-hue", "The sprawling neon-soaked City of Hue.", "city-of-hue-complete.html"),
            ("Hughes Station", "hughes-station", "Hughes Station schematic.", "hughes-station-schematic.html"),
        ]
        for name, slug, desc, html_file in _html_schematics:
            if not db.query(Schematic).filter(Schematic.slug == slug).first():
                db.add(Schematic(
                    world_id=1, name=name, slug=slug, description=desc,
                    is_html=True, html_file=html_file,
                ))
        # Seed built-in N&D sheet template
        if not db.query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first():
            db.add(SheetTemplate(
                world_id=None,
                name="Neon & Dragons — Default",
                slug="nd-default",
                description="Standard N&D character sheet: 8 stats, HP/Shock/PP/MP, edges, cyberware, conditions, feats.",
                is_builtin=True,
                sheet_mode="nd",
                fields_json="[]",
            ))
        # Seed built-in Asterion sheet template (fully custom — no N&D stats/mechanics)
        if not db.query(SheetTemplate).filter(SheetTemplate.slug == "asterion").first():
            db.add(SheetTemplate(
                world_id=None,
                name="Asterion",
                slug="asterion",
                description="City of Nine Thousand Shrines — Gods & Mythborn dice-pool system "
                             "(Spark Shield/Flesh/Ichor, Glory, Domain Reclamation).",
                is_builtin=True,
                sheet_mode="custom",
                fields_json=json.dumps(_ASTERION_FIELDS),
            ))
        # Seed built-in entity field templates
        if not db.query(EntityTemplate).filter(EntityTemplate.slug == "npc-details").first():
            db.add(EntityTemplate(
                world_id=None, name="NPC Details", slug="npc-details", kind="character",
                description="Title, age, gender, and alive/dead/missing status for an NPC.",
                is_builtin=True, fields_json=json.dumps(_NPC_DETAILS_FIELDS),
            ))
        if not db.query(EntityTemplate).filter(EntityTemplate.slug == "stat-block").first():
            db.add(EntityTemplate(
                world_id=None, name="Stat Block", slug="stat-block", kind=None,
                description="Quick combat stats — attack/defense pools, health, armor, speed, abilities. "
                             "Usable on characters, creatures, or anything else that fights.",
                is_builtin=True, fields_json=json.dumps(_STAT_BLOCK_FIELDS),
            ))
        # Seed built-in random tables
        if not db.query(RandomTable).filter(RandomTable.slug == "loot-rarity").first():
            db.add(RandomTable(
                world_id=None, name="Loot Rarity", slug="loot-rarity", category="loot",
                description="Quick rarity roll for a found item.", is_builtin=True,
                entries_json=json.dumps([
                    {"label": "Common", "weight": 50},
                    {"label": "Uncommon", "weight": 30},
                    {"label": "Rare", "weight": 15},
                    {"label": "Legendary", "weight": 5},
                ]),
            ))
        if not db.query(RandomTable).filter(RandomTable.slug == "random-encounter").first():
            db.add(RandomTable(
                world_id=None, name="Random Encounter", slug="random-encounter", category="encounter",
                description="Generic encounter-pressure roll while traveling or exploring.", is_builtin=True,
                entries_json=json.dumps([
                    {"label": "Nothing happens", "weight": 40},
                    {"label": "Signs of danger ahead (tracks, wreckage, warnings)", "weight": 20},
                    {"label": "Hostile encounter", "weight": 20},
                    {"label": "Friendly or neutral encounter", "weight": 15},
                    {"label": "Rare/notable event", "weight": 5},
                ]),
            ))
        db.commit()

        # Bootstrap the GM account from env vars if no GM account exists yet.
        # This is the only way to create a GM account — there's no open signup;
        # players only ever join via GM-issued invite codes.
        from . import auth as _auth  # deferred: auth.py imports get_db from this module
        gm_email = (os.environ.get("GM_EMAIL") or "").strip().lower()
        gm_password = os.environ.get("GM_PASSWORD") or ""
        if gm_email and gm_password and not db.query(User).filter(User.is_gm == True).first():  # noqa: E712
            existing = db.query(User).filter(User.email == gm_email).first()
            if existing:
                # Don't silently hijack an existing account: if the GM row was ever
                # deleted, every restart would otherwise reset this user's password
                # and promote them to GM, just because their email matches GM_EMAIL.
                _log.warning(
                    "GM_EMAIL (%s) matches an existing non-GM user; not resetting their "
                    "password or promoting them. Delete/rename that account or change "
                    "GM_EMAIL if you intended to bootstrap a fresh GM account.",
                    gm_email,
                )
            else:
                db.add(User(
                    email=gm_email,
                    password_hash=_auth.hash_password(gm_password),
                    display_name=os.environ.get("GM_NAME", "GM"),
                    is_gm=True,
                ))
                db.commit()
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
