"""Tests for the player-facing image generation feature (off by default):
World.players_can_use_image_gen unlocks a narrow, fixed-settings slice of
Image Studio's own generation surface (see the "player" imagegen routes in
app/routers/ai.py) so a player can make art for their own PlayerCharacter.

The core requirement (per the feature request) is PRIVACY: every generated
image is visible only to the player who made it and the GM — never another
player, even one in the same world. That's enforced by ImageJob.
created_by_user_id scoping in _player_image_job_or_404, not just the
players_can_use_image_gen toggle being off by default — these tests check
both axes separately.
"""
import time

import pytest

from app import ai as ai_module
from app.database import SessionLocal
from app.models import ImageJob, PlayerCharacter, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _isolated_ai_data_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_module, "_CUSTOM_MODELS_FILE", tmp_path / "ai_models.json")


def _set_world(world_id, **kw):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        for k, v in kw.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _add_to_world(user_id, world_id):
    """Adds an existing user as a second player of `world_id` — for
    same-world cross-player privacy tests, seed's own player_a/player_b are
    each members of a different world."""
    db = SessionLocal()
    try:
        db.add(WorldMembership(world_id=world_id, user_id=user_id))
        db.commit()
    finally:
        db.close()


def _fake_generate(urls=None):
    async def _gen(**kwargs):
        return urls if urls is not None else ["/uploads/ai-images/x.png"]
    return _gen


def _poll_until_terminal(client, url, timeout=5.0):
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        r = client.get(url)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.02)
    raise AssertionError(f"job never reached a terminal status, last seen: {data}")


# ── Access gating: players_can_use_image_gen, off by default ────────────────

def test_page_gm_always_allowed(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/image-gen")
    assert r.status_code == 200


def test_page_player_denied_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/image-gen")
    assert r.status_code == 403


def test_page_player_allowed_once_gm_enables_it(client, seed):
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/image-gen")
    assert r.status_code == 200


def test_page_toggle_is_per_world(client, seed):
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_b.email, PLAYER_PASSWORD)  # player_b is only a member of world_b
    client.cookies.set("active_world", seed.world_b.slug)
    r = client.get("/image-gen")
    assert r.status_code == 403


def test_generate_player_denied_by_default(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"})
    assert r.status_code == 403


def test_generate_player_allowed_once_gm_enables_it(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    data = _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")
    assert data["status"] == "done"
    assert data["urls"] == ["/uploads/ai-images/x.png"]


def test_generate_requires_a_prompt(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/player/generate", json={"prompt": "   "})
    assert r.status_code == 400


def test_generate_fixed_params_ignore_client_size_and_steps(client, seed, monkeypatch):
    """PlayerImagegenBody only ever accepts {prompt, negative} — even if a
    modified client sent width/height/steps, they're silently ignored,
    since api_imagegen_player_generate builds its own fixed ImagegenBody
    from PLAYER_IMAGEGEN_FIXED_PARAMS rather than trusting request data."""
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return ["/uploads/ai-images/x.png"]
    monkeypatch.setattr(ai_module, "imagegen_generate", _capture)
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/ai/imagegen/player/generate", json={
        "prompt": "a hero", "width": 4096, "height": 4096, "steps": 999, "batch_size": 8,
    })
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")
    assert captured["width"] == 512
    assert captured["height"] == 768
    assert captured["steps"] == 30
    assert captured["batch_size"] == 1


# ── Privacy: every job scoped to its own creator, GM sees everyone's ────────

def test_player_cannot_see_another_players_job_in_the_same_world(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    _add_to_world(seed.player_b.id, seed.world_a.id)  # a second player, same world

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/api/ai/imagegen/player/jobs/{job_id}")
    assert r.status_code == 404  # not 403 — matches character_sheets.py's hidden-content convention

    r2 = client.get("/api/ai/imagegen/player/jobs")
    assert r2.status_code == 200
    assert job_id not in [j["id"] for j in r2.json()]


def test_player_cannot_cancel_or_delete_another_players_job(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    _add_to_world(seed.player_b.id, seed.world_a.id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/ai/imagegen/player/jobs/{job_id}/cancel").status_code == 404
    assert client.delete(f"/api/ai/imagegen/player/jobs/{job_id}").status_code == 404


def test_player_own_job_list_excludes_others(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    _add_to_world(seed.player_b.id, seed.world_a.id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_a = client.post("/api/ai/imagegen/player/generate", json={"prompt": "hero A"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_a}")

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_b = client.post("/api/ai/imagegen/player/generate", json={"prompt": "hero B"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_b}")

    r = client.get("/api/ai/imagegen/player/jobs")
    ids = [j["id"] for j in r.json()]
    assert job_b in ids
    assert job_a not in ids


def test_gm_sees_every_players_job_with_owner_field(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/api/ai/imagegen/player/jobs")
    assert r.status_code == 200
    row = next(j for j in r.json() if j["id"] == job_id)
    assert row["owner"] == "Player A"

    # GM can also poll/cancel/delete any player's job directly.
    assert client.get(f"/api/ai/imagegen/player/jobs/{job_id}").status_code == 200
    assert client.delete(f"/api/ai/imagegen/player/jobs/{job_id}").status_code == 200


# ── Per-player / per-world job caps ─────────────────────────────────────────

def test_per_player_job_cap_enforced(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate())
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    from app.routers import ai as ai_router
    monkeypatch.setattr(ai_router, "_MAX_IMAGE_JOBS_PER_PLAYER", 1)
    monkeypatch.setattr(ai_router, "_PLAYER_IMAGEGEN_COOLDOWN_SECONDS", 0)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r1 = client.post("/api/ai/imagegen/player/generate", json={"prompt": "one"})
    assert r1.status_code == 200
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{r1.json()['job_id']}")
    r2 = client.post("/api/ai/imagegen/player/generate", json={"prompt": "two"})
    assert r2.status_code == 400


# ── Setting a generated image as a PlayerCharacter's portrait ───────────────

def _make_pc(world_id, owner_user_id, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_user_id, name=name)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def test_set_portrait_from_own_job(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate(["/uploads/ai-images/hero.png"]))
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    pc_id = _make_pc(seed.world_a.id, seed.player_a.id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    r = client.post(f"/api/characters/{pc_id}/portrait-from-url", json={
        "job_id": job_id, "url": "/uploads/ai-images/hero.png",
    })
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        pc = db.get(PlayerCharacter, pc_id)
        assert pc.portrait_url == "/uploads/ai-images/hero.png"
    finally:
        db.close()


def test_set_portrait_rejects_url_not_in_that_jobs_results(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate(["/uploads/ai-images/hero.png"]))
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    pc_id = _make_pc(seed.world_a.id, seed.player_a.id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    r = client.post(f"/api/characters/{pc_id}/portrait-from-url", json={
        "job_id": job_id, "url": "https://evil.example/tracker.png",
    })
    assert r.status_code == 400


def test_set_portrait_rejects_someone_elses_job(client, seed, monkeypatch):
    """A player can't claim another player's generated image as their own
    character's portrait, even if they correctly guess/know its url —
    the job_id must ALSO belong to them."""
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate(["/uploads/ai-images/hero.png"]))
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    _add_to_world(seed.player_b.id, seed.world_a.id)
    pc_id = _make_pc(seed.world_a.id, seed.player_b.id, name="B's Hero")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/characters/{pc_id}/portrait-from-url", json={
        "job_id": job_id, "url": "/uploads/ai-images/hero.png",
    })
    assert r.status_code == 404


def test_set_portrait_requires_owning_the_character(client, seed, monkeypatch):
    """Even with a job the caller genuinely owns, they can't set it as the
    portrait of a character they don't own."""
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate(["/uploads/ai-images/hero.png"]))
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    _add_to_world(seed.player_b.id, seed.world_a.id)
    pc_id = _make_pc(seed.world_a.id, seed.player_b.id, name="B's Hero")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")
    r = client.post(f"/api/characters/{pc_id}/portrait-from-url", json={
        "job_id": job_id, "url": "/uploads/ai-images/hero.png",
    })
    assert r.status_code == 403


def test_gm_can_set_portrait_from_any_players_job(client, seed, monkeypatch):
    monkeypatch.setattr(ai_module, "imagegen_generate", _fake_generate(["/uploads/ai-images/hero.png"]))
    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    pc_id = _make_pc(seed.world_a.id, seed.player_a.id)

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    job_id = client.post("/api/ai/imagegen/player/generate", json={"prompt": "a hero"}).json()["job_id"]
    _poll_until_terminal(client, f"/api/ai/imagegen/player/jobs/{job_id}")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/characters/{pc_id}/portrait-from-url", json={
        "job_id": job_id, "url": "/uploads/ai-images/hero.png",
    })
    assert r.status_code == 200


# ── Migration heal ───────────────────────────────────────────────────────────

def test_migration_heal_adds_players_can_use_image_gen_column(tmp_path, monkeypatch):
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import database as database_module
    from app.models import Base

    db_path = tmp_path / "image_gen_heal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE worlds (id INTEGER PRIMARY KEY, name TEXT, slug TEXT UNIQUE, description TEXT,
            accent TEXT, players_see_party BOOLEAN, players_can_ask_ai BOOLEAN, rules_md TEXT, created_at DATETIME)
    """)
    conn.execute("INSERT INTO worlds (id, name, slug) VALUES (1, 'Test World', 'test-world')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", Session)

    Base.metadata.create_all(bind=engine)
    database_module._migrate()

    raw = sqlite3.connect(str(db_path))
    cols = [r[1] for r in raw.execute("PRAGMA table_info(worlds)").fetchall()]
    value = raw.execute("SELECT players_can_use_image_gen FROM worlds WHERE id=1").fetchone()[0]
    raw.close()
    engine.dispose()

    assert "players_can_use_image_gen" in cols
    assert value == 0


# ── Nav visibility ───────────────────────────────────────────────────────────

def test_nav_image_gen_item_visible_only_when_toggle_on(seed):
    from app.nav_menus import resolve_nav_menus

    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        _, ungrouped = resolve_nav_menus(world, False, False, is_gm=False)
    finally:
        db.close()
    assert not any(i["id"] == "image_gen_player" for i in ungrouped)

    _set_world(seed.world_a.id, players_can_use_image_gen=True)
    db = SessionLocal()
    try:
        world = db.get(World, seed.world_a.id)
        _, ungrouped = resolve_nav_menus(world, False, False, is_gm=False)
    finally:
        db.close()
    item = next((i for i in ungrouped if i["id"] == "image_gen_player"), None)
    assert item is not None
    assert item["href"] == "/image-gen"


# ── world_edit.html toggle ───────────────────────────────────────────────────

def test_world_edit_post_persists_image_gen_toggle(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name, "players_can_use_image_gen": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.players_can_use_image_gen is True
    finally:
        db.close()
