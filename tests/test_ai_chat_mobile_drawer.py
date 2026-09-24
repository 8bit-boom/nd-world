"""Regression tests for the AI Chat mobile sidebar drawer.

Before this, .ai-sidebar was plain `display: none` below ~700-768px width
(two separate mobile stylesheets both hid it, one with !important) with no
replacement — History (New chat + saved sessions), Save chat, Compact chat,
and Clear chat were all unreachable on a phone. The separate mobile-model-bar
and mobile-rag-bar only ever re-implemented the model picker and RAG
sliders, not chat management.

The fix turns the existing sidebar into an off-canvas drawer (slide-in via
a #ai-sidebar-toggle button, closable via backdrop tap or Escape) instead of
hiding it or duplicating its contents under new mobile-only ids — so every
control inside it (including newChat()/saveChat()/compactChat()/clearChat()
and #session-list) is reachable unchanged."""
from pathlib import Path

from .conftest import GM_PASSWORD, login

STATIC = Path(__file__).parent.parent / "static"


def test_ai_page_renders_the_drawer_toggle_and_backdrop(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert 'id="ai-sidebar-toggle"' in r.text
    assert 'onclick="toggleAiSidebarDrawer()"' in r.text
    assert 'aria-controls="ai-page-sidebar"' in r.text
    assert 'id="ai-sidebar-backdrop"' in r.text
    assert 'onclick="closeAiSidebarDrawer()"' in r.text


def test_sidebar_keeps_history_new_save_compact_clear_reachable_in_the_drawer(client, seed):
    """The whole point: the drawer is the SAME sidebar element (now given an
    id + off-canvas CSS), not a stripped-down mobile duplicate — so it must
    still contain every one of these controls unchanged."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    sidebar_start = r.text.index('id="ai-page-sidebar"')
    # .ai-main (the chat column) starts right after the sidebar closes —
    # bound the slice so a match doesn't accidentally come from elsewhere.
    sidebar_end = r.text.index('id="ai-main"', sidebar_start)
    sidebar_html = r.text[sidebar_start:sidebar_end]
    assert 'onclick="newChat()"' in sidebar_html
    assert 'id="session-list"' in sidebar_html
    assert 'onclick="saveChat()"' in sidebar_html
    assert 'onclick="compactChat()"' in sidebar_html
    assert 'onclick="clearChat()"' in sidebar_html


def test_mobile_css_slides_the_sidebar_instead_of_hiding_it():
    content = (STATIC / "css" / "ai-chat.css").read_text()
    media_block = content.split("@media (max-width: 700px) {", 1)[1].split("\n@media", 1)[0]
    sidebar_block = media_block.split(".ai-sidebar {", 1)[1].split("}", 1)[0]
    assert "display: none" not in sidebar_block
    assert "position: fixed" in sidebar_block
    assert "transform: translateX(-100%)" in sidebar_block
    assert ".ai-sidebar.open" in media_block
    assert "translateX(0)" in media_block.split(".ai-sidebar.open", 1)[1].split("}", 1)[0]


def test_sidebar_toggle_hidden_on_desktop_shown_on_mobile():
    content = (STATIC / "css" / "ai-chat.css").read_text()
    base_block = content.split(".ai-sidebar-toggle {", 1)[1].split("}", 1)[0]
    assert "display: none" in base_block
    media_block = content.split("@media (max-width: 700px) {", 1)[1].split("\n@media", 1)[0]
    mobile_toggle_block = media_block.split(".ai-sidebar-toggle {", 1)[1].split("}", 1)[0]
    assert "display: inline-flex" in mobile_toggle_block


def test_old_style_css_no_longer_force_hides_the_sidebar():
    """The old rule lived in a completely separate stylesheet (style.css,
    not ai-chat.css) with !important — if it were still there it would win
    the cascade and keep the drawer permanently hidden regardless of the
    .open class. (A comment referencing .ai-sidebar is fine — only an actual
    rule, i.e. a selector followed by "{", would re-break the drawer.)"""
    content = (STATIC / "style.css").read_text()
    assert ".ai-sidebar {" not in content


def test_drawer_js_functions_and_escape_handler_present():
    content = (STATIC / "js" / "ai-chat-core.js").read_text()
    for fn in ("function openAiSidebarDrawer()", "function closeAiSidebarDrawer()", "function toggleAiSidebarDrawer()"):
        assert fn in content
    assert "e.key === 'Escape'" in content
    assert "closeAiSidebarDrawer()" in content.split("keydown", 1)[1]
