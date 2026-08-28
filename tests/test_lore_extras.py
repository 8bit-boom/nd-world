"""Tests for the ported NeonDragonsWorld extras: dreamlands/king-in-yellow
pages, the two InvestBoard generators, and handouts — all GM-only by
default, all reusing existing Entity/InvestBoard machinery.

Dreamlands and King in Yellow are additionally gated behind an instance-wide
AppSettings toggle (off by default) — see Settings > System > Optional
extras — so a table that doesn't use them doesn't see the nav clutter.
"""
from app.database import SessionLocal, clear_app_settings_flags_cache
from app.models import AppSettings, Entity, InvestBoard

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _enable_lore_extras(world_a_slug=None):
    db = SessionLocal()
    try:
        s = db.query(AppSettings).first()
        if not s:
            s = AppSettings(id=1)
            db.add(s)
        s.dreamlands_enabled = True
        s.king_in_yellow_enabled = True
        db.commit()
    finally:
        db.close()
    # A raw DB write, not the real Settings-save route — the route clears
    # app.database's own short-TTL flags cache (Wave 4, Speed 4.5) itself,
    # but this test helper bypasses it entirely, so do the same here or
    # the very next page render could still see the pre-write, cached
    # value for up to _APP_SETTINGS_FLAGS_CACHE_TTL seconds.
    clear_app_settings_flags_cache()


def test_dreamlands_and_kiy_pages_are_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    assert client.get("/dreamlands").status_code == 403
    assert client.get("/king-in-yellow").status_code == 403


def test_dreamlands_and_kiy_disabled_by_default(client, seed):
    """Off by default — a GM hitting the URL directly sees a friendly
    'enable this in Settings' page, not the full feature (and definitely
    not a 404/500, since the routes themselves still exist)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.get("/dreamlands")
    assert r.status_code == 200
    assert "disabled" in r.text.lower()
    assert "Settings" in r.text

    r = client.get("/king-in-yellow")
    assert r.status_code == 200
    assert "disabled" in r.text.lower()


def test_dreamlands_and_kiy_nav_links_hidden_until_enabled(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    page = client.get("/")
    assert "/dreamlands" not in page.text
    assert "/king-in-yellow" not in page.text

    _enable_lore_extras()

    page = client.get("/")
    assert "/dreamlands" in page.text
    assert "/king-in-yellow" in page.text


def test_dreamlands_and_kiy_pages_load_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    _enable_lore_extras()

    assert client.get("/dreamlands").status_code == 200
    assert client.get("/king-in-yellow").status_code == 200


def test_generate_dreamlands_board_creates_invest_board(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post("/boards/generate-dreamlands", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/boards/atlas-of-dreams"

    db = SessionLocal()
    try:
        board = db.query(InvestBoard).filter(InvestBoard.slug == "atlas-of-dreams").first()
        assert board is not None
        assert board.world_id == seed.world_a.id
        assert "dl-kadath" in board.nodes_json
    finally:
        db.close()


def test_generate_dreamlands_board_replace_updates_existing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    client.post("/boards/generate-dreamlands", follow_redirects=False)

    db = SessionLocal()
    try:
        board_id_before = db.query(InvestBoard).filter(InvestBoard.slug == "atlas-of-dreams").first().id
    finally:
        db.close()

    r = client.post("/boards/generate-dreamlands", params={"replace": "atlas-of-dreams"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        boards = db.query(InvestBoard).filter(InvestBoard.slug.like("atlas-of-dreams%")).all()
        assert len(boards) == 1
        assert boards[0].id == board_id_before
    finally:
        db.close()


def test_generate_orgs_board_uses_world_organizations(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="organization", subtype="megacorp", name="Hughes Industries"))
        db.commit()
    finally:
        db.close()

    r = client.post("/boards/generate-orgs", follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        board = db.query(InvestBoard).filter(InvestBoard.world_id == seed.world_a.id, InvestBoard.slug.startswith("factions")).first()
        assert board is not None
        assert "Hughes Industries" in board.nodes_json
    finally:
        db.close()


def test_boards_generate_are_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    assert client.post("/boards/generate-orgs").status_code == 403
    assert client.post("/boards/generate-dreamlands").status_code == 403


def test_handout_single_rejects_entity_from_other_world(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        other = Entity(world_id=seed.world_b.id, kind="character", name="Foreign NPC")
        db.add(other)
        db.commit()
        db.refresh(other)
        other_id = other.id
    finally:
        db.close()

    r = client.get(f"/handout/{other_id}")
    assert r.status_code == 404


def test_handouts_gallery_and_print(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    db = SessionLocal()
    try:
        ent = Entity(world_id=seed.world_a.id, kind="character", name="Gandalf", body="## Entry\nA wizard.")
        db.add(ent)
        db.commit()
        db.refresh(ent)
        ent_id = ent.id
    finally:
        db.close()

    r = client.get("/handouts")
    assert r.status_code == 200
    assert "Gandalf" in r.text

    r = client.post("/handouts/print", json={"ids": [ent_id]})
    assert r.status_code == 200
    assert "Gandalf" in r.text
