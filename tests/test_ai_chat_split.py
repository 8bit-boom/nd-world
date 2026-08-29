"""Regression tests for plan item UI 2.1: ai_chat.html was a single
5,617-line file (~700 lines of CSS, ~1700 lines of tab HTML, ~3900 lines of
JS all inlined into one <script> block). Split into a thin shell
(app/templates/ai_chat.html) that {% include %}s five per-tab Jinja
partials (app/templates/ai_chat/_tab_*.html) and loads four static JS
bundles plus one static CSS file — no behavior change, verified during
development by reconstructing the split output and diffing it byte-for-byte
against the pre-split render (DOM, JS line-multiset, and CSS all matched
exactly, modulo the one deliberate WORLD_NAME hoist)."""
from pathlib import Path

from .conftest import GM_PASSWORD, login

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"
STATIC = Path(__file__).parent.parent / "static"


def test_ai_page_still_renders_every_tab_and_loads_the_split_assets(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200

    for panel_id in ("image-panel", "models-panel", "whisper-panel", "starred-panel"):
        assert f'id="{panel_id}"' in r.text
    for tab_btn in ("tab-chat", "tab-image", "tab-models", "tab-whisper", "tab-starred"):
        assert f'id="{tab_btn}"' in r.text

    assert '<link rel="stylesheet" href="/static/css/ai-chat.css">' in r.text
    for js in ("ai-chat-core.js", "ai-chat-image.js", "ai-chat-models.js", "ai-chat-whisper.js"):
        assert f'<script src="/static/js/{js}"></script>' in r.text

    # The bootstrap block (server-rendered constants the split JS files need)
    # still appears, with real values substituted.
    assert "const WORLD_SYSTEM = " in r.text
    assert "const ENTITY_KINDS = " in r.text
    assert "const ENTITY_KIND_ICONS = " in r.text
    assert "const WORLD_NAME = " in r.text

    # The old single giant inline <script> is gone — the template shell
    # itself is now tiny.
    template_text = (TEMPLATES / "ai_chat.html").read_text()
    assert template_text.count("<script>") == 1  # just the bootstrap block
    assert len(template_text.splitlines()) < 60


def test_split_static_assets_are_actually_served(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    for path in (
        "/static/css/ai-chat.css",
        "/static/js/ai-chat-core.js",
        "/static/js/ai-chat-image.js",
        "/static/js/ai-chat-models.js",
        "/static/js/ai-chat-whisper.js",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert len(r.text) > 100, path


def test_bootstrap_constants_are_only_used_by_core_js_not_duplicated_elsewhere():
    # The four Jinja-templated globals belong in the template's own inline
    # bootstrap script; the static bundles reference them as plain globals
    # (classic, non-module scripts share global scope) without redeclaring
    # them, since a `const` redeclared across files would throw at load time.
    for js in ("ai-chat-core.js", "ai-chat-image.js", "ai-chat-models.js", "ai-chat-whisper.js"):
        content = (STATIC / "js" / js).read_text()
        assert "{{" not in content and "{%" not in content, f"{js} still contains Jinja syntax"
        assert "const WORLD_SYSTEM" not in content
        assert "const ENTITY_KINDS" not in content
        assert "const ENTITY_KIND_ICONS" not in content
        assert "const WORLD_NAME" not in content


def test_every_tab_partial_is_a_self_contained_panel():
    partials_dir = TEMPLATES / "ai_chat"
    expected = {
        "_tab_chat.html": None,  # the chat tab has no single wrapping #id — mobile bar + .ai-page
        "_tab_image.html": "image-panel",
        "_tab_models.html": "models-panel",
        "_tab_whisper.html": "whisper-panel",
        "_tab_starred.html": "starred-panel",
    }
    for name, panel_id in expected.items():
        content = (partials_dir / name).read_text()
        assert content.strip(), name
        if panel_id:
            assert content.count(f'id="{panel_id}"') == 1, name
