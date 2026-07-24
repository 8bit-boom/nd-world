import json
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import (
    Base, World, Schematic, MapOverlay, InvestBoard, PlayerCharacter, SheetTemplate,
    StarredImage, User, WorldMembership, InviteCode, EntityTemplate, RandomTable,
)

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
        if "template_id" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN template_id INTEGER"))
            conn.commit()
        if "custom_fields_json" not in cols:
            conn.execute(text("ALTER TABLE entities ADD COLUMN custom_fields_json TEXT DEFAULT '{}'"))
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
        if tpl_exists:
            tpl_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sheet_templates)")).fetchall()]
            if "sheet_mode" not in tpl_cols:
                conn.execute(text("ALTER TABLE sheet_templates ADD COLUMN sheet_mode VARCHAR(16) DEFAULT 'nd'"))
                conn.commit()
        # worlds table — add players_see_party if missing
        w_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worlds'"
        )).fetchone()
        if w_exists:
            w_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()]
            if "players_see_party" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN players_see_party BOOLEAN DEFAULT 1"))
                conn.commit()
            if "rules_md" not in w_cols:
                conn.execute(text("ALTER TABLE worlds ADD COLUMN rules_md TEXT"))
                conn.commit()
        # parties table — add location_json if missing (added after initial ship)
        p_exists = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='parties'"
        )).fetchone()
        if p_exists:
            p_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(parties)")).fetchall()]
            if "location_json" not in p_cols:
                conn.execute(text("ALTER TABLE parties ADD COLUMN location_json TEXT DEFAULT '{}'"))
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
