"""world.accent is interpolated directly into a <style> block in base.html
(and used as a CSS custom property in the world-switcher/world-card markup),
so both world_create and world_edit_post must reject anything that isn't a
plain hex color instead of trusting the submitted form value."""
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, login


def test_world_create_accepts_valid_hex_accent(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/worlds/new", data={"name": "Hex World", "accent": "#a1b2c3"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.slug == "hex-world").first()
        assert w.accent == "#a1b2c3"
    finally:
        db.close()


def test_world_create_rejects_malicious_accent(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        "/worlds/new",
        data={"name": "Injected World", "accent": "red; } body { display:none"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.slug == "injected-world").first()
        assert w.accent == "#00f0ff"  # falls back to the default instead of storing raw CSS
    finally:
        db.close()


def test_world_edit_accepts_valid_hex_accent(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "accent": "#123abc"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.accent == "#123abc"
    finally:
        db.close()


def test_world_edit_keeps_previous_accent_on_invalid_value(client, seed):
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.accent = "#654321"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "accent": "not-a-color"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.accent == "#654321"  # unchanged, not overwritten with garbage
    finally:
        db.close()


def test_world_edit_page_renders_hex_input_paired_with_picker(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    assert 'id="accent-hex"' in r.text
    assert 'data-hex-id="accent-hex"' in r.text
    assert 'data-pick-id="accent-pick"' in r.text


def test_worlds_list_renders_hex_input_for_new_world_form(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/worlds")
    assert r.status_code == 200
    assert 'id="new-accent-hex"' in r.text
    assert 'data-pick-id="new-accent-pick"' in r.text


def test_world_edit_accepts_shorthand_hex_accent(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "accent": "#0fc"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.accent == "#0fc"
    finally:
        db.close()
