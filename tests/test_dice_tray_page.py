"""The 3D dice tray on /dice: what the page loads and how it is wired. The physics and the mouse throw were driven in a real
browser (headless Chromium, software WebGL); here we pin the parts that fail silently - a missing vendored file, an import
that points nowhere, a CDN sneaking in (the app must work offline at a table with no internet), a button wired to nothing."""
import re
from pathlib import Path

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent


def _page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/dice")
    assert r.status_code == 200
    return r.text


def test_the_page_loads_the_geometry_script_and_the_tray_module(client, seed):
    page = _page(client, seed)
    assert re.search(r'<script src="/static/js/dice-geometry\.js\?v=[0-9a-f]+"></script>', page)
    assert re.search(r'<script type="module" src="/static/js/dice-tray\.js\?v=[0-9a-f]+"></script>', page)
    assert page.index("/static/js/dice-geometry.js?v=") < page.index("/static/js/dice-tray.js?v=")
    # the inline script reads window.ndDiceGeometry as it runs, so the classic geometry script must come first
    assert page.index("/static/js/dice-geometry.js?v=") < page.index("var G = window.ndDiceGeometry")
    for path in ("/static/js/dice-geometry.js", "/static/js/dice-tray.js"):
        assert client.get(path).status_code == 200


def test_the_tray_module_imports_only_files_that_ship_with_the_app():
    source = (ROOT / "static/js/dice-tray.js").read_text(encoding="utf-8")
    imports = re.findall(r"^import .* from '([^']+)';", source, re.M)
    assert len(imports) == 2
    for spec in imports:
        assert spec.startswith("/static/vendor/"), spec
        assert (ROOT / spec.lstrip("/")).is_file(), spec


def test_vendored_libraries_are_served_and_their_licences_travel_with_them(client, seed):
    for path in ("/static/vendor/three-0.159.0.module.min.js", "/static/vendor/cannon-es-0.20.0.js",
                 "/static/vendor/three.LICENSE", "/static/vendor/cannon-es.LICENSE"):
        r = client.get(path)
        assert r.status_code == 200 and len(r.content) > 500, path
    for name in ("three", "cannon-es"):      # both MIT: the permission notice must accompany every copy
        assert "Permission is hereby granted" in client.get(f"/static/vendor/{name}.LICENSE").text


def test_nothing_is_fetched_from_a_cdn():
    """A table with no internet must still have its dice: every dependency is vendored."""
    for rel in ("static/js/dice-tray.js", "static/js/dice-geometry.js", "app/templates/dice.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!www\.w3\.org)", text), rel


def test_the_page_has_the_tray_the_die_buttons_and_the_throw_button(client, seed):
    page = _page(client, seed)
    for marker in ('id="dice-tray"', 'id="dice-tray-box"', 'id="dice-throw-btn"', 'id="dice-sweep-btn"', 'id="dice-tray-result"', 'id="dice-sound"'):
        assert marker in page
    for sides in (4, 6, 8, 10, 12, 20, 100):
        assert f'data-sides="{sides}"' in page
    # hidden until the tray has actually started (no WebGL / an old browser leaves the page exactly as it was)
    assert re.search(r'id="dice-tray-box"[^>]*style="display:none', page)


def test_a_thrown_roll_is_recorded_through_the_server_and_the_log_is_refreshed(client, seed):
    page = _page(client, seed)
    assert "/api/dice/record" in page
    settled = page.split("onSettled", 1)[1][:1800]
    assert "values: " in settled and "refresh()" in settled
    # a lost connection must not lose the result the player is looking at
    assert "not saved to the shared log" in page


def test_the_canvas_does_not_scroll_the_page_while_throwing():
    source = (ROOT / "static/js/dice-tray.js").read_text(encoding="utf-8")
    assert "touch-action:none" in source and "setPointerCapture" in source


def test_the_roll_and_quick_buttons_still_roll_instantly(client, seed):
    page = _page(client, seed)
    assert 'class="dice-quick"' in page and "fetch('/api/dice/roll'" in page
