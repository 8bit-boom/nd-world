"""Regression test for the /uploads path traversal hole (see app/main.py's
serve_upload): a player could previously request /uploads/../world.db and
download the entire SQLite database — password hashes, invite codes, all of
it — because the old handler joined the URL path onto UPLOADS_DIR with no
containment check.
"""
from .conftest import login, GM_PASSWORD


def test_traversal_percent_encoded_blocked(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/%2e%2e/world.db")
    assert r.status_code == 404


def test_traversal_literal_dotdot_blocked(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/../world.db", follow_redirects=False)
    assert r.status_code in (404, 400)


def test_traversal_nested_percent_encoded_blocked(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/uploads/portraits/%2e%2e/%2e%2e/world.db")
    assert r.status_code == 404


def test_legitimate_upload_still_served(client, seed):
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    portraits = UPLOADS_DIR / "portraits"
    portraits.mkdir(parents=True, exist_ok=True)
    (portraits / "legit.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-but-fine")
    r = client.get("/uploads/portraits/legit.png")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_legitimate_upload_gets_immutable_cache_control(client, seed):
    """Every upload path is a uuid-prefixed, content-unique filename (see
    unique_upload_filename) EXCEPT maps/, so a long immutable cache is safe
    everywhere but there — cuts a full re-fetch/re-validate per view for
    every grid/gallery/thumbnail on the site."""
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    portraits = UPLOADS_DIR / "portraits"
    portraits.mkdir(parents=True, exist_ok=True)
    (portraits / "legit.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-but-fine")
    r = client.get("/uploads/portraits/legit.png")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_map_upload_is_not_cached_immutably(client, seed):
    """Map images are saved under stable slug-based names and overwritten
    in place on re-upload — caching them immutably would keep serving a
    stale image after a GM replaces one."""
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    maps_dir = UPLOADS_DIR / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    (maps_dir / "some-world.webp").write_bytes(b"fake-webp-bytes")
    r = client.get("/uploads/maps/some-world.webp")
    assert r.status_code == 200
    assert "immutable" not in r.headers.get("cache-control", "")


def test_legacy_svg_forced_to_download(client, seed):
    """New .svg uploads are rejected outright (ALLOWED_EXTS), but a file that
    predates that change may still exist on disk — it must never render inline,
    since SVG can carry <script> and is served from the app's own origin."""
    from app.main import UPLOADS_DIR
    login(client, seed.gm.email, GM_PASSWORD)
    portraits = UPLOADS_DIR / "portraits"
    portraits.mkdir(parents=True, exist_ok=True)
    (portraits / "legacy.svg").write_text("<svg onload='alert(1)'></svg>")
    r = client.get("/uploads/portraits/legacy.svg")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
