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
os.environ["MAX_NOTE_IMPORT_BYTES"] = "1048576"  # same reason, for /entity/{id}/notes/import
# app.job_shutdown.STOP_GRACE_SECONDS is read once at import time, so this
# has to be set here (before any app.* module is imported) same as DB_PATH/
# SECRET_KEY above. The `client` fixture below enters/exits the ASGI
# lifespan once per test (~1000x in the full suite) — a non-zero grace
# would add its full value to every test that deliberately leaves a
# hanging background task in flight (_hanging_transcribe and friends).
# Zero here means shutdown cancels immediately, which also stops those
# tasks from leaking across tests.
os.environ["ND_JOB_STOP_GRACE_SECONDS"] = "0"
os.environ.pop("GM_EMAIL", None)
os.environ.pop("GM_PASSWORD", None)

import asyncio
import shutil

import pytest
from starlette.testclient import TestClient

from app import ai as ai_module
from app import auth
from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, User, World, WorldMembership

# Production PBKDF2 (600_000 iterations, app/auth.py) costs ~0.4s per
# hash/verify call on typical hardware. The `seed` fixture below calls
# hash_password 3x per test and login() calls verify_password once — at
# ~970/840 call sites respectively across this suite, that's the large
# majority of its wall-clock time (measured: a 9.7x full-suite speedup,
# zero test failures, from this one change). Both functions read
# _PBKDF2_ITERATIONS at call time, not at import time, so overriding the
# module attribute here covers every hash/verify call the whole app makes
# during tests — login, invite redemption, password change, 2FA re-auth —
# without touching application code. No test in this suite asserts on
# hash format, salt, or timing, and hash/verify stay symmetric at any
# iteration count, so this only affects speed, never correctness. The
# assert guards the one thing that actually matters: that the PRODUCTION
# default hasn't silently drifted out from under this override.
assert auth._PBKDF2_ITERATIONS == 600_000, (
    "auth._PBKDF2_ITERATIONS changed — update the security assumption this test override documents"
)
auth._PBKDF2_ITERATIONS = 1000

GM_PASSWORD = "gm-password-123"
PLAYER_PASSWORD = "player-password-123"
# Hashed once at collection time rather than per-test-per-user (971 tests x
# up to 3 calls each) — see the _PBKDF2_ITERATIONS override above for why
# each individual call is already cheap; this removes the remaining
# redundant work of re-hashing the exact same two constants thousands of
# times over. Both test users of the same role sharing one hash string is
# fine: nothing in this suite asserts hashes are unique per user.
_GM_PASSWORD_HASH = auth.hash_password(GM_PASSWORD)
_PLAYER_PASSWORD_HASH = auth.hash_password(PLAYER_PASSWORD)


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
    # Dropping/recreating the tables above restarts autoincrement ids from 1
    # every test, so a user id gets reused across tests — a process-local,
    # user-id-keyed dict like app.deps._llm_cooldowns would otherwise leak a
    # timestamp from one test into the "same" id in the next and produce a
    # false 429 there. Clear it here for the same reason the DB/uploads
    # above are reset per test.
    from app.deps import _llm_cooldowns
    _llm_cooldowns.clear()
    # app.ai.whisper_job_semaphore/ollama_job_semaphore are module-level
    # singletons that lazily bind to whichever asyncio event loop first
    # actually contends them (see asyncio.Semaphore.acquire — it only
    # calls _get_loop(), and so only binds, once a second waiter shows up)
    # — harmless in production, where the process has exactly one event
    # loop for its whole lifetime, but pytest-asyncio hands each test
    # function its own fresh loop, so a semaphore genuinely contended (two
    # jobs queued at once) in one test would raise "bound to a different
    # event loop" in any later test whose own contention is the first to
    # reach it. Give every test fresh, as-yet-unbound instances, same
    # reasoning as _llm_cooldowns above.
    ai_module.whisper_job_semaphore = asyncio.Semaphore(ai_module.WHISPER_JOB_CONCURRENCY)
    ai_module.ollama_job_semaphore = asyncio.Semaphore(ai_module.OLLAMA_JOB_CONCURRENCY)
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
        gm = User(email="gm@test.local", password_hash=_GM_PASSWORD_HASH,
                  display_name="GM", is_gm=True)
        world_a = World(name="World A", slug="world-a")
        world_b = World(name="World B", slug="world-b")
        db.add_all([gm, world_a, world_b])
        db.commit()
        db.refresh(world_a)
        db.refresh(world_b)

        player_a = User(email="player-a@test.local", password_hash=_PLAYER_PASSWORD_HASH,
                         display_name="Player A", is_gm=False)
        player_b = User(email="player-b@test.local", password_hash=_PLAYER_PASSWORD_HASH,
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
