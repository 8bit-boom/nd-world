import json
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import (
    Base, World, Schematic, MapOverlay, InvestBoard, PlayerCharacter, SheetTemplate,
    StarredImage, User, WorldMembership, InviteCode,
)

DB_PATH = os.environ.get("DB_PATH", "/data/world.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed()

def _migrate():
    with engine.connect() as conn:
        # Add world_id column to existing entities table if missing
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(entities)")).fetchall()]
        if "world_id" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN world_id INTEGER NOT NULL DEFAULT 1"))
            conn.commit()
        if "folder" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN folder VARCHAR(256)"))
            conn.commit()
        if "visible_to_players" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN visible_to_players BOOLEAN DEFAULT 1"))
            conn.commit()
        # Clean up literal "None" strings stored by early import runs
        conn.execute(text("UPDATE entities SET folder  = NULL WHERE folder  = 'None'"))
        conn.execute(text("UPDATE entities SET summary = NULL WHERE summary = 'None'"))
        conn.execute(text("UPDATE entities SET body    = NULL WHERE body    = 'None'"))
        conn.execute(text("UPDATE entities SET subtype = NULL WHERE subtype = 'None'"))
        # Delete entities with missing names (string 'None', null, or blank)
        conn.execute(text("DELETE FROM entities WHERE name IS NULL OR TRIM(name) = '' OR name = 'None'"))
        # Re-classify equipment feat directories: they were imported as items but are feats
        conn.execute(text(
            "UPDATE entities SET kind = 'feat' WHERE kind = 'item' AND folder LIKE '%Feat%'"
        ))
        # Prefix bare Rank/Origin/Edge folders on feats with 'Common Feats/' parent
        conn.execute(text(
            "UPDATE entities SET folder = 'Common Feats/' || folder "
            "WHERE kind = 'feat' AND folder IN ('Rank 1', 'Rank 2', 'Rank 3', 'Origin', 'Edge')"
        ))
        conn.commit()
        # Add new Schematic columns if missing
        sch_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(schematics)")).fetchall()] if conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='schematics'")).fetchone() else []
        for col, defn in [("canvas_width", "INTEGER DEFAULT 2000"), ("canvas_height", "INTEGER DEFAULT 1500"), ("canvas_bg", "VARCHAR(32) DEFAULT 'dark'"), ("elements_json", "TEXT DEFAULT '[]'")]:
            if sch_cols and col not in sch_cols:
                conn.execute(text(f"ALTER TABLE schematics ADD COLUMN {col} {defn}"))
        conn.commit()
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
            ]
            for col, defn in _pc_extra:
                if col not in pc_cols:
                    conn.execute(text(f"ALTER TABLE player_characters ADD COLUMN {col} {defn}"))
            conn.commit()
        # sheet_templates table migration — ensure it exists (Base.metadata handles creation,
        # but old DBs may lack the table until next create_all call)
        tpl_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sheet_templates'"
        )).fetchone()
        # table is created by Base.metadata.create_all above; nothing extra needed here
        # worlds table — add players_see_party if missing
        w_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worlds'"
        )).fetchone()
        if w_exists:
            w_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()]
            if "players_see_party" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN players_see_party BOOLEAN DEFAULT 1"))
                conn.commit()

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
                fields_json="[]",
            ))
        # Seed built-in Game of Gods (Asterion) sheet template — adds the dice-pool,
        # Spark Shield/Flesh/Ichor, and Glory/Reputation trackers used by the
        # Asterion Unified Rulebook on top of the base N&D sheet fields.
        if not db.query(SheetTemplate).filter(SheetTemplate.slug == "game-of-gods").first():
            _gog_fields = [
                {"id": "origin_lineage", "label": "Origin / Lineage", "type": "text", "section": "stats", "default_value": ""},
                {"id": "divine_spark", "label": "Divine Spark", "type": "text", "section": "stats", "default_value": ""},
                {"id": "epic_deed_curse", "label": "Epic Deed / Curse", "type": "text", "section": "stats", "default_value": ""},
                {"id": "role", "label": "Role", "type": "text", "section": "stats", "default_value": ""},
                {"id": "standard_pool", "label": "Standard Pool", "type": "text", "section": "stats", "default_value": "2d10"},
                {"id": "domain_pool", "label": "Domain Pool", "type": "text", "section": "stats", "default_value": "3d10"},
                {"id": "movement", "label": "Movement", "type": "text", "section": "stats", "default_value": "30 ft / 6 hexes"},
                {"id": "domain_rank", "label": "Domain Rank (0-3)", "type": "number", "section": "stats", "default_value": "0"},
                {"id": "spark_shield", "label": "Spark Shield", "type": "resource", "section": "resources", "default_value": "3"},
                {"id": "flesh", "label": "Flesh", "type": "resource", "section": "resources", "default_value": "5"},
                {"id": "ichor", "label": "Ichor", "type": "resource", "section": "resources", "default_value": "5"},
                {"id": "glory", "label": "Glory Available (unspent)", "type": "number", "section": "resources", "default_value": "0"},
                {"id": "reputation", "label": "Reputation", "type": "number", "section": "resources", "default_value": "0"},
                {"id": "divine_ambition", "label": "Divine Ambition", "type": "textarea", "section": "notes", "default_value": ""},
                {"id": "abilities", "label": "Abilities (name · type · tier · cost — effect, one per line)", "type": "textarea", "section": "notes", "default_value": ""},
                {"id": "trade_offs", "label": "Trade-Offs", "type": "textarea", "section": "notes", "default_value": ""},
            ]
            db.add(SheetTemplate(
                world_id=None,
                name="Game of Gods — Asterion",
                slug="game-of-gods",
                description="Dice pools, Spark Shield/Flesh/Ichor, Glory, Reputation, and ability/Trade-Off trackers for the Asterion Unified Rulebook, layered on top of the base sheet.",
                is_builtin=True,
                fields_json=json.dumps(_gog_fields),
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
                existing.is_gm = True
                existing.password_hash = _auth.hash_password(gm_password)
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
