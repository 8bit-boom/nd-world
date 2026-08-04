"""Entity hover-preview: GET /api/entity/{id}/preview (reuses the same
access gate as the /entity detail page via app.main._entity_view_gate),
GET /api/hover-preview/config (instance-wide on/off + delay, editable from
Settings > Options), and the POST /settings save round-trip for both.
"""
from app.database import SessionLocal
from app.models import AppSettings, Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_entity(world_id, **kwargs):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind=kwargs.pop("kind", "character"), name=kwargs.pop("name", "Preview Target"), **kwargs)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_preview_returns_entity_data_for_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    eid = _make_entity(seed.world_a.id, kind="feat", name="Arcane Bolt", summary="A basic bolt.", tags="rank1,mage")
    r = client.get(f"/api/entity/{eid}/preview")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Arcane Bolt"
    assert data["kind"] == "feat"
    assert data["summary"] == "A basic bolt."
    assert data["tags"] == ["rank1", "mage"]


def test_preview_404s_for_nonexistent_entity(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    assert client.get("/api/entity/999999/preview").status_code == 404


def test_preview_404s_for_hidden_entity_to_player(client, seed):
    eid = _make_entity(seed.world_a.id, visible_to_players=False)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/api/entity/{eid}/preview").status_code == 404


def test_preview_visible_entity_works_for_player(client, seed):
    eid = _make_entity(seed.world_a.id, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/api/entity/{eid}/preview").status_code == 200


def test_preview_404s_across_worlds(client, seed):
    """A player in world_a can't preview an entity that belongs to world_b —
    same world-membership gate the detail page already enforces."""
    eid = _make_entity(seed.world_b.id, visible_to_players=True)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    assert client.get(f"/api/entity/{eid}/preview").status_code == 404


def test_hover_preview_config_defaults(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/api/hover-preview/config")
    assert r.status_code == 200
    assert r.json() == {
        "enabled": True, "delay_ms": 5000,
        "hide_delay_ms": 400, "width_px": 340, "max_height_px": 420,
    }


def test_settings_roundtrip_persists_hover_preview(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "2.5",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_enabled is True
        assert settings.hover_preview_delay_ms == 2500
    finally:
        db.close()

    r = client.get("/api/hover-preview/config")
    assert r.json()["enabled"] is True
    assert r.json()["delay_ms"] == 2500


def test_settings_roundtrip_persists_hide_delay_and_size(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "5",
        "hover_preview_hide_delay_seconds": "1.2",
        "hover_preview_width_px": "500",
        "hover_preview_max_height_px": "600",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_hide_delay_ms == 1200
        assert settings.hover_preview_width_px == 500
        assert settings.hover_preview_max_height_px == 600
    finally:
        db.close()

    r = client.get("/api/hover-preview/config")
    data = r.json()
    assert data["hide_delay_ms"] == 1200
    assert data["width_px"] == 500
    assert data["max_height_px"] == 600


def test_settings_unchecking_disables_hover_preview(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    # Omitting the checkbox field entirely is how an unchecked HTML checkbox submits.
    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_delay_seconds": "5",
    }, follow_redirects=False)

    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_enabled is False
    finally:
        db.close()


def test_settings_clamps_out_of_range_delay(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "999",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_delay_ms == 30000
    finally:
        db.close()

    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "0",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_delay_ms == 500
    finally:
        db.close()


def test_settings_clamps_out_of_range_hide_delay(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "5",
        "hover_preview_hide_delay_seconds": "999",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.query(AppSettings).first().hover_preview_hide_delay_ms == 10000
    finally:
        db.close()

    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "5",
        "hover_preview_hide_delay_seconds": "-3",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.query(AppSettings).first().hover_preview_hide_delay_ms == 0
    finally:
        db.close()


def test_settings_clamps_out_of_range_size(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "5",
        "hover_preview_width_px": "50", "hover_preview_max_height_px": "10",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_width_px == 220
        assert settings.hover_preview_max_height_px == 150
    finally:
        db.close()

    client.post("/settings", data={
        "static_format": "avif", "animated_format": "avif",
        "hover_preview_enabled": "1", "hover_preview_delay_seconds": "5",
        "hover_preview_width_px": "5000", "hover_preview_max_height_px": "9000",
    }, follow_redirects=False)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first()
        assert settings.hover_preview_width_px == 800
        assert settings.hover_preview_max_height_px == 1000
    finally:
        db.close()


def test_settings_page_has_hover_preview_fields(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'name="hover_preview_enabled"' in r.text
    assert 'name="hover_preview_delay_seconds"' in r.text
    assert 'name="hover_preview_hide_delay_seconds"' in r.text
    assert 'name="hover_preview_width_px"' in r.text
    assert 'name="hover_preview_max_height_px"' in r.text


def _assert_no_min_max(html, input_id):
    idx = html.index(f'id="{input_id}"')
    input_tag = html[idx - 80: idx + 200]
    assert "min=" not in input_tag
    assert "max=" not in input_tag


def test_numeric_fields_have_no_native_min_max_constraint(client, seed):
    """Regression guard for a real bug caught during manual verification:
    an HTML5 min/max attribute on one of these fields, combined with a
    stored value ever outside that range (only reachable via direct DB
    access today, but cheap to guard against), makes the browser silently
    refuse to submit the *entire* Options form — no error, no console
    message, the Save button just stops doing anything. Clamping happens
    server-side in settings_save already for all four numeric fields; the
    client-side constraint added nothing but this footgun, so it should
    never come back on any of them."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        settings = db.query(AppSettings).first() or AppSettings(id=1)
        settings.hover_preview_delay_ms = 300  # 0.3s — below any sane "min"
        settings.hover_preview_hide_delay_ms = -50
        settings.hover_preview_width_px = 10
        settings.hover_preview_max_height_px = 5
        if settings.id is None:
            db.add(settings)
        db.commit()
    finally:
        db.close()
    r = client.get("/settings")
    _assert_no_min_max(r.text, "hover-preview-delay")
    _assert_no_min_max(r.text, "hover-preview-hide-delay")
    _assert_no_min_max(r.text, "hover-preview-width")
    _assert_no_min_max(r.text, "hover-preview-max-height")


def test_base_template_wires_up_hover_listener():
    """Source-level regression guard: the delegated mouseover/mouseout
    handlers and the config fetch are present, matching the established
    style of source-string checks elsewhere in this suite for frontend-only
    behavior with no JS test runner."""
    html = open("app/templates/base.html", encoding="utf-8").read()
    assert "/api/hover-preview/config" in html
    assert "a[href^=\"/entity/\"]" in html
    assert "addEventListener('mouseover'" in html
    assert "addEventListener('mouseout'" in html


def test_base_template_has_hide_grace_period_not_immediate_hide():
    """Regression guard for the "popup disappears before the mouse gets
    there" bug: the popup must have its own mouseenter (to cancel a
    pending hide) alongside mouseleave (to re-arm it) — mouseleave alone,
    firing hidePopup() directly, is the old broken behavior this was
    fixed from. scheduleHide/cancelHide must both exist and mouseout must
    go through scheduleHide rather than calling hidePopup() immediately."""
    html = open("app/templates/base.html", encoding="utf-8").read()
    assert "function scheduleHide" in html
    assert "function cancelHide" in html
    assert "popupEl.addEventListener('mouseenter', cancelHide)" in html
    assert "popupEl.addEventListener('mouseleave', scheduleHide)" in html
