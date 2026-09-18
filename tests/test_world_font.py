"""world.font/world.font_size are the simple, no-JSON-file direct fields on
the World edit page (same spirit as world.accent) — see World.font's
docstring in app/models.py. world.font is interpolated raw (|safe) into
base.html's <style> block exactly like theme_json's own font/font_heading,
so world_edit_post must reject anything that isn't a plain CSS font-family
value instead of trusting the submitted form value; world.font_size (a
75-150 <input type="range"> on the edit page) is clamped into that range
for the same reason, since it's rendered the same raw way.
"""
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _upload_theme(client, world_id, payload):
    import io
    import json
    return client.post(
        f"/worlds/{world_id}/theme/import",
        files={"file": ("theme.json", io.BytesIO(json.dumps(payload).encode()), "application/json")},
        follow_redirects=False,
    )


# ── POST /worlds/{id}/edit — font ───────────────────────────────────────────

def test_world_edit_accepts_valid_font(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font": "'EB Garamond', Georgia, serif"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font == "'EB Garamond', Georgia, serif"
    finally:
        db.close()


def test_world_edit_rejects_malicious_font_keeps_previous(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font = "'EB Garamond', Georgia, serif"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font": "serif; } body { background: url(javascript:alert(1))"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font == "'EB Garamond', Georgia, serif"  # unchanged, not overwritten with garbage
    finally:
        db.close()


def test_world_edit_clears_font_when_blank(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font = "'EB Garamond', Georgia, serif"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font is None
    finally:
        db.close()


# ── POST /worlds/{id}/edit — font_size ──────────────────────────────────────

def test_world_edit_accepts_valid_font_size(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font_size": "130"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font_size == 130
    finally:
        db.close()


def test_world_edit_clamps_font_size_above_max(client, seed):
    # A real <input type="range" max="150"> can never send this, but a
    # hand-crafted request shouldn't be able to push the stored value past
    # what the slider itself allows.
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font_size": "999"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font_size == 150
    finally:
        db.close()


def test_world_edit_clamps_font_size_below_min(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font_size": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font_size == 75
    finally:
        db.close()


def test_world_edit_rejects_non_numeric_font_size_keeps_previous(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font_size = 115
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/edit",
        data={"name": seed.world_a.name, "font_size": "not-a-number"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert w.font_size == 115  # unchanged, not overwritten with garbage
    finally:
        db.close()


def test_world_edit_page_renders_font_fields(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    assert 'name="font"' in r.text
    assert 'name="font_size"' in r.text
    assert 'id="font-suggestions"' in r.text


# ── base.html renders the direct font/font_size overrides ──────────────────

def test_page_renders_direct_font_when_set(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font = "'Orbitron', sans-serif"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font: 'Orbitron', sans-serif !important" in r.text


def test_page_omits_direct_font_when_unset(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font:" not in r.text


def test_uploaded_theme_font_wins_over_direct_font(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font = "'Orbitron', sans-serif"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    _upload_theme(client, seed.world_a.id, {"font": "'EB Garamond', Georgia, serif"})
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font: 'EB Garamond', Georgia, serif !important" in r.text
    assert r.text.count("--font:") == 1  # the direct field's value never also renders


def test_page_renders_direct_font_size_when_customized(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font_size = 130
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font-size-base: 130% !important" in r.text


def test_page_omits_font_size_override_at_default(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font-size-base:" not in r.text


def test_font_visible_to_players_reading_the_world(client, seed):
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        w.font = "'Orbitron', sans-serif"
        w.font_size = 115
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "--font: 'Orbitron', sans-serif !important" in r.text
    assert "--font-size-base: 115% !important" in r.text
