"""The character-sync JSON API (/api/login, /api/me, /api/characters/{id}/sync,
/api/worlds/{id}/characters/sync) is how NeonDragonsApp pulls/pushes a player's
character over HTTP. It reuses the same session-cookie auth and ownership rules
as the existing form-based routes, so these tests mirror
test_character_ownership.py's pattern (own vs. other-player vs. GM) plus cover
the JSON-login flow and the one-character-per-player-per-world rule that's
unique to the create route.
"""
import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter
from app.routers import auth as auth_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """_failed_logins (app/routers/auth.py) is a process-local dict that outlives
    any single test's `client` fixture, since the DB reset doesn't touch it —
    without this, an earlier test's wrong-password attempts (or the lockout test
    itself) leak into later tests and make an unrelated login start 429ing."""
    auth_router._failed_logins.clear()
    yield
    auth_router._failed_logins.clear()


def _make_pc(world, owner_id, name="Owned Character"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world.id, name=name, owner_user_id=owner_id,
                              max_hp=20, current_hp=20)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


# ── /api/login ───────────────────────────────────────────────────────────────

def test_api_login_success(client, seed):
    r = client.post("/api/login", json={"email": seed.player_a.email, "password": PLAYER_PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["user"]["email"] == seed.player_a.email
    assert body["user"]["is_gm"] is False

    # session cookie actually works for subsequent requests
    r2 = client.get("/api/me")
    assert r2.status_code == 200


def test_api_login_wrong_password(client, seed):
    r = client.post("/api/login", json={"email": seed.player_a.email, "password": "not-the-password"})
    assert r.status_code == 401
    assert r.json()["ok"] is False

    r2 = client.get("/api/me")
    assert r2.status_code == 401


def test_api_login_unknown_email(client, seed):
    r = client.post("/api/login", json={"email": "nobody@test.local", "password": "whatever123"})
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_api_login_lockout(client, seed):
    for _ in range(8):
        r = client.post("/api/login", json={"email": seed.player_a.email, "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"email": seed.player_a.email, "password": PLAYER_PASSWORD})
    assert r.status_code == 429
    assert r.json()["ok"] is False


# ── /api/me ──────────────────────────────────────────────────────────────────

def test_api_me_requires_login(client, seed):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_api_me_reflects_membership_and_character(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == seed.player_a.email
    world_ids = {w["id"] for w in body["worlds"]}
    assert seed.world_a.id in world_ids
    assert seed.world_b.id not in world_ids  # player A isn't a member of world B

    world_a_entry = next(w for w in body["worlds"] if w["id"] == seed.world_a.id)
    assert world_a_entry["character_id"] is None

    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    r2 = client.get("/api/me")
    world_a_entry2 = next(w for w in r2.json()["worlds"] if w["id"] == seed.world_a.id)
    assert world_a_entry2["character_id"] == pc.id


def test_api_me_gm_sees_all_worlds(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/api/me")
    assert r.status_code == 200
    world_ids = {w["id"] for w in r.json()["worlds"]}
    assert {seed.world_a.id, seed.world_b.id} <= world_ids


# ── GET/PUT /api/characters/{id}/sync ───────────────────────────────────────

def test_sync_get_owner_can_pull(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id, name="Vex")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get(f"/api/characters/{pc.id}/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == pc.id
    assert body["name"] == "Vex"
    assert body["world_id"] == seed.world_a.id
    assert body["stats_json"] == []
    assert body["custom_fields_json"] == {}
    assert body["app_extra_json"] == {}


def test_sync_get_other_player_forbidden(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r = client.get(f"/api/characters/{pc.id}/sync")
    assert r.status_code == 403


def test_sync_get_gm_can_pull_any(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/api/characters/{pc.id}/sync")
    assert r.status_code == 200
    assert r.json()["id"] == pc.id


def test_sync_get_missing_character_404(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/characters/999999/sync")
    assert r.status_code == 404


def test_sync_put_owner_can_push(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.put(f"/api/characters/{pc.id}/sync", json={
        "name": "Updated Name",
        "max_hp": 30, "current_hp": 25,
    })
    assert r.status_code == 200
    assert r.json()["name"] == "Updated Name"
    assert r.json()["max_hp"] == 30

    db = SessionLocal()
    try:
        refreshed = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc.id).first()
        assert refreshed.name == "Updated Name"
        assert refreshed.max_hp == 30
    finally:
        db.close()


def test_sync_put_other_player_forbidden(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r = client.put(f"/api/characters/{pc.id}/sync", json={"name": "Hijacked"})
    assert r.status_code == 403

    db = SessionLocal()
    try:
        refreshed = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc.id).first()
        assert refreshed.name == "Owned Character"
    finally:
        db.close()


def test_sync_put_gm_can_push_any(client, seed):
    pc = _make_pc(seed.world_a, owner_id=seed.player_a.id)
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.put(f"/api/characters/{pc.id}/sync", json={"name": "GM Edited"})
    assert r.status_code == 200
    assert r.json()["name"] == "GM Edited"


# ── POST /api/worlds/{id}/characters/sync ───────────────────────────────────

def test_sync_create_requires_world_membership(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/worlds/{seed.world_b.id}/characters/sync", json={"name": "Intruder"})
    assert r.status_code == 404


def test_sync_create_and_enforces_one_per_world(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/api/worlds/{seed.world_a.id}/characters/sync", json={"name": "New Hero"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New Hero"
    assert body["world_id"] == seed.world_a.id

    r2 = client.post(f"/api/worlds/{seed.world_a.id}/characters/sync", json={"name": "Second Hero"})
    assert r2.status_code == 400


def test_sync_create_requires_login(client, seed):
    r = client.post(f"/api/worlds/{seed.world_a.id}/characters/sync", json={"name": "Nobody"})
    assert r.status_code == 401


# ── Full push -> pull round trip ────────────────────────────────────────────

def test_full_roundtrip_push_then_pull(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    payload = {
        "name": "Roundtrip Hero", "player_name": "Alex", "race": "Human",
        "char_class": "Hacker", "level": 5, "xp": 1200,
        "backstory": "Grew up in the sprawl.", "notes": "Owes a favor to Nekrith.",
        "race_id": "human", "profession_id": "hacker",
        "max_hp": 5, "current_hp": 4,
        "shock_max": 5, "shock_current": 3,
        "pp_current": 4, "mp_current": 5,
        "minor_edge": "Grease Palms", "major_edge": "Auto-Chatter",
        "minor_edge_count": 1, "major_edge_count": 1,
        "stats_json": ["Charisma", "Intellect"],
        "currency_json": ["100 credits"],
        "equipment_json": ["Deck", "Pistol"],
        "feats_json": ["Quickdraw"],
        "cyberware_json": ["Neural Jack"],
        "conditions_json": ["Winded"],
        "custom_fields_json": {"hometown": "Neo-Kyoto"},
        "app_extra_json": {"psyPowers": ["Mindspike"], "yellowSaturation": 2},
    }
    created = client.post(f"/api/worlds/{seed.world_a.id}/characters/sync", json=payload)
    assert created.status_code == 200
    pc_id = created.json()["id"]

    pulled = client.get(f"/api/characters/{pc_id}/sync")
    assert pulled.status_code == 200
    body = pulled.json()

    for key, expected in payload.items():
        assert body[key] == expected, f"{key}: expected {expected!r}, got {body[key]!r}"

    assert body["id"] == pc_id
    assert body["world_id"] == seed.world_a.id
    assert body["updated_at"] is not None
