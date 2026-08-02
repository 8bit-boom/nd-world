"""Filesystem-backed maps (JSON files under _MAPS_DIR) previously had no
delete or rename at all; Schematic battle-maps had delete but no rename.
Covers both new code paths, plus the world-ownership check that keeps a GM
from renaming/deleting a map that belongs to a different world.
"""
import io
import json

import pytest

from app.database import SessionLocal
from app.main import _MAPS_DIR
from app.models import MapOverlay, Schematic

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _clean_maps_dir():
    """_MAPS_DIR isn't reset by the `client` fixture (only the DB and uploads
    dir are), so a map file written by one test would otherwise leak into the
    next test's fresh (but same-slug-space) DB."""
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    yield
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def _make_map(world_id, slug, name="Test Map"):
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / f"{slug}.json").write_text(json.dumps({
        "name": name, "world_id": world_id, "width": 3072, "height": 3072, "markers": [],
    }), encoding="utf-8")


def test_map_rename(client, seed):
    _make_map(seed.world_a.id, "old-cave")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/old-cave/rename", data={"name": "New Cave"}, follow_redirects=False)
    assert r.status_code == 303
    data = json.loads((_MAPS_DIR / "old-cave.json").read_text())
    assert data["name"] == "New Cave"
    # The slug/URL is unchanged — rename only touches the display name.
    assert (_MAPS_DIR / "old-cave.json").exists()


def test_map_rename_blank_rejected(client, seed):
    _make_map(seed.world_a.id, "old-cave")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/old-cave/rename", data={"name": "   "})
    assert r.status_code == 400
    data = json.loads((_MAPS_DIR / "old-cave.json").read_text())
    assert data["name"] == "Test Map"


def test_map_rename_wrong_world_404s(client, seed):
    _make_map(seed.world_b.id, "b-only-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/b-only-map/rename", data={"name": "Hijacked"})
    assert r.status_code == 404
    data = json.loads((_MAPS_DIR / "b-only-map.json").read_text())
    assert data["name"] == "Test Map"


def test_map_delete_removes_file_and_overlay(client, seed):
    _make_map(seed.world_a.id, "doomed-map")
    db = SessionLocal()
    try:
        db.add(MapOverlay(slug="doomed-map", custom_markers_json="[]", custom_regions_json="[]"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/doomed-map/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not (_MAPS_DIR / "doomed-map.json").exists()

    db = SessionLocal()
    try:
        assert db.query(MapOverlay).filter(MapOverlay.slug == "doomed-map").first() is None
    finally:
        db.close()


def test_map_delete_wrong_world_404s(client, seed):
    _make_map(seed.world_b.id, "b-only-map-2")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/b-only-map-2/delete")
    assert r.status_code == 404
    assert (_MAPS_DIR / "b-only-map-2.json").exists()


def test_map_delete_missing_404s(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/does-not-exist/delete")
    assert r.status_code == 404


def test_map_new_symbol_only_name_rejected_not_a_zombie(client, seed):
    """Phase 7 regression guard: _slug_from_name strips everything but
    [a-z0-9], so a name with no letters or digits (emoji, punctuation-only)
    used to slugify to "" — writing a map to the bare filename ".json" and
    redirecting to /maps/, which /maps/{slug} can never route back to. The
    map still showed up in the /maps listing (glob "*.json" matches ".json")
    but was permanently unreachable and undeletable: a zombie record. Reject
    the name up front instead."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/new", data={"name": "!!! 🐉🐉🐉 ???"})
    assert r.status_code == 400
    assert not (_MAPS_DIR / ".json").exists()


def test_schematic_new_symbol_only_name_rejected_not_a_zombie(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/schematic/new", data={"name": "!!! 🐉🐉🐉 ???"})
    assert r.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(Schematic).filter(Schematic.slug == "").first() is None
    finally:
        db.close()


def test_schematic_new_no_active_world_rejected_not_500(client, seed):
    """Phase 16 regression guard: map_new already guarded this identically
    (`if not world: raise HTTPException(400, ...)`), but schematic_new never
    did — world.id would 500 with an unhandled AttributeError instead of a
    clean 400. Reproduced here via the state right after Phase 15's
    world-delete cascade removes the last world (or a fresh install with
    none created yet): get_active_world has nothing to fall back to."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        db.query(Schematic).delete()
        from app.models import World
        db.query(World).delete()
        db.commit()
    finally:
        db.close()
    r = client.post("/maps/schematic/new", data={"name": "Orphan Schematic"})
    assert r.status_code == 400


def test_schematic_new_rejects_invalid_canvas_dimensions(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for width, height in [(0, 1500), (-100, 1500), (2000, 0), (2000, 999_999)]:
        r = client.post("/maps/schematic/new",
                         data={"name": f"Bad Canvas {width}x{height}", "canvas_width": width, "canvas_height": height})
        assert r.status_code == 400, f"{width}x{height} should have been rejected"


def test_schematic_new_accepts_valid_canvas_dimensions(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/maps/schematic/new",
                     data={"name": "Good Canvas", "canvas_width": 3000, "canvas_height": 2200},
                     follow_redirects=False)
    assert r.status_code == 303


def test_map_upload_404s_for_nonexistent_slug(client, seed):
    """Phase 8 regression guard: map_upload_image previously took no db/world
    params at all, so it would write an image for *any* slug — map or no
    map — with no existence check. A nonexistent slug left an orphan upload
    file that would silently attach itself to a later map created with that
    same slug."""
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    r = client.post("/maps/no-such-map/upload", files={"file": ("map.png", small_png, "image/png")})
    assert r.status_code == 404
    assert not (UPLOADS_DIR / "maps" / "no-such-map.png").exists()


def test_map_upload_404s_wrong_world(client, seed):
    _make_map(seed.world_b.id, "b-only-map-3")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    r = client.post("/maps/b-only-map-3/upload", files={"file": ("map.png", small_png, "image/png")})
    assert r.status_code == 404


def test_map_upload_succeeds_for_own_world_map(client, seed):
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "own-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    r = client.post("/maps/own-map/upload", files={"file": ("map.png", small_png, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    assert (UPLOADS_DIR / "maps" / "own-map.png").exists()


def test_map_overlay_404s_for_nonexistent_slug(client, seed):
    """Same missing-existence-check hazard as map_upload_image, for the
    overlay save route: previously any slug got (and kept) a MapOverlay row
    with no check the map even existed."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/maps/no-such-map/overlay", json={"custom_markers": [], "custom_regions": []})
    assert r.status_code == 404


def test_map_overlay_404s_wrong_world(client, seed):
    _make_map(seed.world_b.id, "b-only-map-4")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/maps/b-only-map-4/overlay", json={"custom_markers": [], "custom_regions": []})
    assert r.status_code == 404


def test_map_overlay_rejects_non_list_payload(client, seed):
    _make_map(seed.world_a.id, "shape-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/maps/shape-map/overlay", json={"custom_markers": "not-a-list", "custom_regions": []})
    assert r.status_code == 400


def test_map_overlay_rejects_too_many_items(client, seed):
    _make_map(seed.world_a.id, "flood-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    markers = [{"lat": 0, "lng": 0, "label": "x", "color": "#ff4466"} for _ in range(501)]
    r = client.post("/api/maps/flood-map/overlay", json={"custom_markers": markers, "custom_regions": []})
    assert r.status_code == 400


def test_map_overlay_saves_for_own_world_map(client, seed):
    _make_map(seed.world_a.id, "save-map")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    markers = [{"lat": 1, "lng": 2, "label": "Cache", "color": "#ff4466"}]
    r = client.post("/api/maps/save-map/overlay", json={"custom_markers": markers, "custom_regions": []})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        overlay = db.query(MapOverlay).filter(MapOverlay.slug == "save-map").first()
        assert json.loads(overlay.custom_markers_json) == markers
    finally:
        db.close()


def test_map_viewer_marker_color_is_sanitized_before_svg_interpolation(client, seed):
    """Phase 5 regression guard: custom-marker colors are GM-settable via
    /api/maps/{slug}/overlay and previously flowed straight into an SVG
    template literal that Leaflet injects as raw innerHTML (L.divIcon's
    `html` option). A color like `"/><image src=x onerror=alert(1)>` would
    break out of the fill="..." attribute and execute for every viewer of the
    map, players included — this locks in that makeCustomIcon runs colors
    through safeColor() (a strict hex check) before interpolating, rather
    than using the raw value.

    This can't drive the browser to prove the payload doesn't execute (no JS
    runtime in this test suite), so it locks in the source-level guard
    instead: the SVG template must reference the sanitized `c`, not `color`.

    The map-viewer script block only renders when an image_url is resolved
    (`{% if image_url %}` wraps the whole <script>), so a background image
    file has to exist on disk for this route to emit any of the JS this test
    inspects. UPLOADS_DIR is wiped by the `client` fixture between tests, so
    writing there (rather than the real static/maps dir) needs no cleanup.
    """
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "paint-cave")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "paint-cave.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/paint-cave")
    assert r.status_code == 200
    assert "function safeColor(c)" in r.text
    assert 'fill="${c}"' in r.text and 'stroke="${c}"' in r.text
    assert 'fill="${color}"' not in r.text and 'stroke="${color}"' not in r.text


def test_map_viewer_move_tool_drags_markers_and_mutations_autosave(client, seed):
    """Phase 9 regression guard, source-level (no JS runtime in this test
    suite — see the marker-color test above for why). Two bugs bundled into
    one fix:

    1. Every custom marker was created with draggable:false hardcoded, so the
       toolbar's "Move" tool did nothing at all — clicking a marker while
       Move was active just opened the same edit dialog as the Marker tool.
       Markers must now be draggable exactly when editMode && tool==='move',
       and dragend must persist the new position.
    2. Adding/editing/deleting a marker or region only ever updated in-memory
       state — nothing was written back to the server until the GM
       remembered to click the manual "💾 Save" button, so navigating away
       (e.g. via a marker popup's "Open schematic" link) silently discarded
       the edit. Every mutation must now call saveOverlay() itself.
    """
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "drag-cave")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "drag-cave.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/drag-cave")
    assert r.status_code == 200
    assert "draggable: false" not in r.text
    assert "const draggable = editMode && tool === 'move'" in r.text
    assert "lm.on('dragend'" in r.text
    for fn_start in (
        "function confirmMarker() {",
        "function deleteMarker() {",
        "function removeCustomMarker(i) {",
        "function confirmRegion() {",
        "function removeRegion(i) {",
    ):
        idx = r.text.index(fn_start)
        # removeCustomMarker/removeRegion are one-liners; the others are
        # multi-statement functions — either way saveOverlay() must appear
        # before the next top-level function declaration.
        next_fn = r.text.index("\nfunction ", idx + len(fn_start))
        assert "saveOverlay()" in r.text[idx:next_fn], f"{fn_start} never calls saveOverlay()"


def test_map_viewer_move_tool_also_drags_regions(client, seed):
    """Phase 17: Move only ever worked for markers (Phase 9) — regions had no
    drag support at all, since Leaflet polygons aren't draggable without the
    (unloaded) leaflet.path.drag plugin, so the only way to reposition one
    was delete + redraw from scratch. Source-level guard (no JS runtime in
    this test suite) for the hand-rolled drag: mousedown starts it (gated on
    editMode && tool==='move'), mousemove translates every point by the
    pointer's lat/lng delta, and mouseup persists via saveOverlay()."""
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "region-drag-cave")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "region-drag-cave.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/region-drag-cave")
    assert r.status_code == 200
    assert "poly.on('mousedown'" in r.text
    assert "if (!(editMode && tool === 'move')) return;" in r.text
    assert "map.dragging.disable()" in r.text
    assert "map.dragging.enable()" in r.text
    idx = r.text.index("poly.on('mousedown'")
    onup_idx = r.text.index("const onUp", idx)
    assert "saveOverlay()" in r.text[onup_idx:onup_idx + 600]


def test_map_viewer_move_tool_drags_party_pins(client, seed):
    """Party pins were created with no `draggable` option at all and their
    click handler only ever checked for the Delete tool — Move never worked
    for them, unlike markers (Phase 9) and regions (Phase 17). Confirmed
    live via Playwright (drag a placed party pin with Move active, reload,
    confirm the new position persisted through a POST to
    /api/parties/{id}/location) before fixing. Source-level guard here."""
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "party-drag-cave")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "party-drag-cave.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/party-drag-cave")
    assert r.status_code == 200
    idx = r.text.index("function renderPartyPins() {")
    render_end = r.text.index("\nfunction ", idx + len("function renderPartyPins() {"))
    block = r.text[idx:render_end]
    assert "const draggable = editMode && tool === 'move';" in block
    assert "draggable}" in block
    assert "pm.on('dragend'" in block
    assert "/api/parties/${p.id}/location" in block
    # setTool() re-renders draggable-ness for both markers and party pins —
    # only re-rendering renderCustomMarkers() here would leave a pin's
    # draggable state stuck at whatever it was when the page first loaded.
    set_tool_idx = r.text.index("function setTool(t) {")
    set_tool_end = r.text.index("\n}", set_tool_idx)
    assert "renderPartyPins();" in r.text[set_tool_idx:set_tool_end]


def _make_schematic(db, world_id, slug, name="Test Schematic"):
    s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                   canvas_width=2000, canvas_height=1500, canvas_bg="dark", elements_json="[]")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_schematic_rename(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "old-lair")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/old-lair/rename", data={"name": "New Lair"}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.slug == "old-lair").first()
        assert s.name == "New Lair"
        assert s.slug == "old-lair"  # slug/URL stays stable
    finally:
        db.close()


def test_schematic_rename_blank_rejected(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "old-lair")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/maps/schematic/old-lair/rename", data={"name": ""})
    assert r.status_code == 400


def test_schematic_editor_shows_background_image(client, seed):
    """Regression test: image_url used to be saved on the Schematic row (via
    the upload endpoint) but the editor's SVG never actually referenced it —
    an uploaded background silently never appeared. Now it must render as an
    <image> tied to schematic.image_url."""
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "with-bg")
        s.image_url = "/uploads/schematics/with-bg.webp"
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/with-bg")
    assert r.status_code == 200
    assert 'id="bg-image"' in r.text
    assert "/uploads/schematics/with-bg.webp" in r.text


def test_schematic_editor_no_background_image_element_when_unset(client, seed):
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "no-bg")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/no-bg")
    assert r.status_code == 200
    assert 'id="bg-image"' not in r.text


def test_maps_page_hides_gm_controls_from_players(client, seed):
    """Phase 6 regression guard: /maps is player-safe (GET), but the write
    routes behind its New/Upload/Rename/Delete controls (POST) are already
    GM-only at the middleware level — so a player who clicked any of these
    previously got a confirm() dialog followed by a 403, not a working
    button. Players should not see controls they can't use."""
    _make_map(seed.world_a.id, "player-visible-map")
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "player-visible-schem")
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    for forbidden in ("+ New Map", "+ New Schematic", "✏ Rename", "🗑 Delete",
                       "Upload image", "Upload background"):
        assert forbidden not in r.text, f"player should not see {forbidden!r} on /maps"
    assert "/maps/schematic/player-visible-schem/view" in r.text


def test_maps_page_shows_gm_controls_for_gm(client, seed):
    _make_map(seed.world_a.id, "gm-visible-map")
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "gm-visible-schem")
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    for expected in ("+ New Map", "+ New Schematic", "✏ Rename", "🗑 Delete"):
        assert expected in r.text
    assert 'href="/maps/schematic/gm-visible-schem"' in r.text


def test_maps_page_html_schematic_not_linked_for_players(client, seed):
    """HTML-type schematics have no player-safe view route at all
    (schematic_player_view 404s them) — a player clicking through would hit
    the GM-only editor and 403. The card must render without a link."""
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "html-schem")
        s.is_html = True
        s.html_file = "html-schem.html"
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    assert "/maps/schematic/html-schem" not in r.text


# Phase 13: the editor's "Embed Image" tool used to base64-encode the picked
# file client-side (FileReader.readAsDataURL) and stuff the whole data: URI
# straight into the new element's href — landing verbatim in elements_json,
# which every read of the schematic (editor, player view, move-token,
# pickup/buy, pull/push-combat) had to parse and transmit whole, with no
# size cap at all. schematic_embed_image gives it the same upload-and-
# reference-by-URL treatment as every other image in the app.

_OVERSIZED_BYTES = 1_048_576 + 200_000  # conftest.py sets MAX_UPLOAD_BYTES=1MiB


def test_embed_image_rejects_bad_extension(client, seed):
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "embed-badext")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/maps/schematic/{s.slug}/embed-image",
                     files={"file": ("evil.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")})
    assert r.status_code == 400


def test_embed_image_rejects_oversized_file(client, seed):
    from app.main import UPLOADS_DIR
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "embed-oversized")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    big_file = io.BytesIO(b"\x00" * _OVERSIZED_BYTES)
    r = client.post(f"/maps/schematic/{s.slug}/embed-image",
                     files={"file": ("huge.png", big_file, "image/png")})
    assert r.status_code == 413
    embeds_dir = UPLOADS_DIR / "schematics" / "embeds"
    leftover = list(embeds_dir.glob("*")) if embeds_dir.exists() else []
    assert leftover == [], f"oversized embed left partial file(s) behind: {leftover}"


def test_embed_image_happy_path_returns_url_not_data_uri(client, seed):
    from app.main import UPLOADS_DIR
    db = SessionLocal()
    try:
        s = _make_schematic(db, seed.world_a.id, "embed-ok")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)
    r = client.post(f"/maps/schematic/{s.slug}/embed-image",
                     files={"file": ("small.png", small_png, "image/png")})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("/uploads/schematics/embeds/")
    assert url.endswith(".png")
    fname = url.rsplit("/", 1)[-1]
    assert (UPLOADS_DIR / "schematics" / "embeds" / fname).exists()


def test_embed_image_404s_for_nonexistent_schematic(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    small_png = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    r = client.post("/maps/schematic/does-not-exist/embed-image",
                     files={"file": ("small.png", small_png, "image/png")})
    assert r.status_code == 404


def test_schematic_editor_uploads_image_instead_of_embedding_base64(client, seed):
    """Source-level guard (no JS runtime in this test suite): onImageFile must
    POST to /embed-image and use the returned URL as href, not
    FileReader.readAsDataURL."""
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "embed-source-check")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/embed-source-check")
    assert r.status_code == 200
    assert "reader.readAsDataURL" not in r.text
    assert "/embed-image" in r.text
    assert "href:url" in r.text


def test_schematic_editor_dialogs_close_on_escape_and_backdrop_click(client, seed):
    """Phase 14 regression guard (source-level — no JS runtime in this test
    suite): the editor's five modal dialogs (text/party/token/merchant-
    inventory/grid) previously closed only via their own Cancel button —
    Escape and clicking the dark backdrop outside the box did nothing. Locks
    in that a dedicated Escape handler and a backdrop click listener exist
    and dispatch through DLG_CANCEL (which routes grid-dlg to
    cancelGridDlg() specifically, since plain hideDlg() would leave its
    live-previewed gridType/gridConfig changes applied instead of reverting
    them)."""
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "dlg-escape-check")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/maps/schematic/dlg-escape-check")
    assert r.status_code == 200
    assert "function openDialogId()" in r.text
    assert "'grid-dlg': () => cancelGridDlg()" in r.text
    assert "back.addEventListener('click'" in r.text
    assert "e.stopImmediatePropagation()" in r.text


def test_map_viewer_editmode_state_declared_before_first_render_call(client, seed):
    """Regression guard (source-level — no JS runtime in this test suite):
    renderCustomMarkers() is called at the script's top level and reads
    editMode/tool on its first line, but `let editMode`/`let tool` used to be
    declared later in the same script — a `let` binding is in the temporal
    dead zone until its own declaration executes, so every single map page
    load threw "Cannot access 'editMode' before initialization" the moment
    renderCustomMarkers() ran, aborting the rest of the script (no party
    pins, no custom regions, no map click handlers, no working Edit/Move/
    Delete tools — confirmed live via Playwright, not just by inspection).
    Locks in that the state block now appears before the render call site."""
    from app.main import UPLOADS_DIR
    _make_map(seed.world_a.id, "editmode-order-check")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    (maps_upload_dir / "editmode-order-check.png").write_bytes(b"fake-png")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/editmode-order-check")
    assert r.status_code == 200
    let_idx = r.text.index("let editMode = false;")
    render_call_idx = r.text.index("renderCustomMarkers();")
    assert let_idx < render_call_idx, (
        "editMode must be declared before renderCustomMarkers() is first called"
    )


def test_maps_page_rename_button_does_not_embed_tojson_in_onclick(client, seed):
    """Regression guard (source-level): the rename buttons used to build
    their onclick as `onclick="...prompt('Rename map:', {{ m.name|tojson }})..."`
    — tojson wraps its output in literal double quotes, which terminates a
    double-quoted HTML *attribute* early (confirmed live: for a map named
    "Central District" the browser parsed the attribute as ending right
    after `prompt('Rename map:', "`, leaving the rest of the name and the
    remaining JS as bogus trailing attributes on the button — this broke for
    every name, not just ones with special characters). The fix reads the
    name from a data-name attribute (Jinja's normal HTML autoescaping is
    safe in that position) via a shared renameViaPrompt() helper instead."""
    _make_map(seed.world_a.id, "rename-escaping-check")
    db = SessionLocal()
    try:
        _make_schematic(db, seed.world_a.id, "rename-escaping-check-schem")
    finally:
        db.close()
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    assert "name|tojson" not in r.text
    assert "function renameViaPrompt(btn, label)" in r.text
    assert r.text.count("onclick=\"renameViaPrompt(this,") == 2
    assert 'data-name="Test Map"' in r.text
    assert 'data-name="Test Schematic"' in r.text


def test_maps_page_upload_labels_support_drag_and_drop(client, seed):
    """Feature-parity guard: the schematic editor's background image drop
    zone (dragenter/dragover/dragleave/drop on #sch-canvas-wrap) already
    worked (confirmed live via a synthetic DataTransfer+File drop event);
    the maps list had no drag&drop at all, only click-to-browse — a real gap
    the user hit, not a regression. Locks in that the same four listeners
    are now wired to the .map-upload-label upload zones."""
    _make_map(seed.world_a.id, "dragdrop-check")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps")
    assert r.status_code == 200
    assert "querySelectorAll('.map-upload-label')" in r.text
    for evt in ("dragenter", "dragover", "dragleave", "drop"):
        assert f"label.addEventListener('{evt}'" in r.text


def test_map_new_form_supports_drag_and_drop(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/maps/new")
    assert r.status_code == 200
    for evt in ("dragenter", "dragover", "dragleave", "drop"):
        assert f"zone.addEventListener('{evt}'" in r.text
