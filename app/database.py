import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base, World

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
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
