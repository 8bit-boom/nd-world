import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base, World, Schematic, MapOverlay, InvestBoard

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
        db.commit()
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
