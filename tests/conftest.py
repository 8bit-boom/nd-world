"""Shared pytest fixtures for the nd-world test suite.

DB_PATH, SECRET_KEY, and friends are read from the environment at *module
import time* by app.database, app.main, and several routers — so they must be
set here, before any app.* module is ever imported, rather than inside a
fixture. conftest.py is always the first thing pytest imports in this
directory, which is what makes setting them at module level reliable.
"""
import os
from pathlib import Path

_TEST_DATA_DIR = Path(__file__).parent / "_data"
_TEST_DATA_DIR.mkdir(exist_ok=True)

os.environ["DB_PATH"] = str(_TEST_DATA_DIR / "world.db")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["MAX_UPLOAD_BYTES"] = "1048576"  # 1 MiB — small so the 413 test stays fast
os.environ.pop("GM_EMAIL", None)
os.environ.pop("GM_PASSWORD", None)

import shutil

import pytest
from starlette.testclient import TestClient

from app import auth
from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, User, World, WorldMembership

GM_PASSWORD = "gm-password-123"
PLAYER_PASSWORD = "player-password-123"


@pytest.fixture()
def client():
    """A TestClient against a freshly emptied database and uploads dir — every
    test gets a clean environment so tests can't see each other's data.

    Drops and recreates tables on the shared engine rather than deleting the
    underlying SQLite file: the engine's connection pool holds file
    descriptors open for the whole test session, so unlinking the file out
    from under it leaves old connections writing to a deleted-but-still-open
    inode while new connections open a different, empty file at the same
    path — silent split-brain state between tests.
    """
    Base.metadata.drop_all(bind=engine)
    from app.main import UPLOADS_DIR
    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
    with TestClient(app) as c:
        yield c


class Seed:
    def __init__(self, gm, world_a, world_b, player_a, player_b):
        self.gm = gm
        self.world_a = world_a
        self.world_b = world_b
        self.player_a = player_a
        self.player_b = player_b


@pytest.fixture()
def seed(client):
    """A GM plus one player each in two separate worlds — the standard fixture
    for cross-world authorization tests. Player A is a member of World A only,
    Player B of World B only."""
    db = SessionLocal()
    try:
        gm = User(email="gm@test.local", password_hash=auth.hash_password(GM_PASSWORD),
                  display_name="GM", is_gm=True)
        world_a = World(name="World A", slug="world-a")
        world_b = World(name="World B", slug="world-b")
        db.add_all([gm, world_a, world_b])
        db.commit()
        db.refresh(world_a)
        db.refresh(world_b)

        player_a = User(email="player-a@test.local", password_hash=auth.hash_password(PLAYER_PASSWORD),
                         display_name="Player A", is_gm=False)
        player_b = User(email="player-b@test.local", password_hash=auth.hash_password(PLAYER_PASSWORD),
                         display_name="Player B", is_gm=False)
        db.add_all([player_a, player_b])
        db.commit()
        db.refresh(player_a)
        db.refresh(player_b)

        db.add_all([
            WorldMembership(world_id=world_a.id, user_id=player_a.id),
            WorldMembership(world_id=world_b.id, user_id=player_b.id),
        ])
        db.commit()
        for obj in (gm, world_a, world_b, player_a, player_b):
            db.refresh(obj)
        return Seed(gm, world_a, world_b, player_a, player_b)
    finally:
        db.close()


def login(c, email, password):
    r = c.post("/login", data={"email": email, "password": password, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, f"login failed for {email}: {r.status_code} {r.text[:300]}"
    return c
