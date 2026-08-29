"""Tests for plan section 5's accessibility sweep (base.html, plus a few
per-template contrast/icon-label fixes): dropdown aria-haspopup/expanded +
Escape/focus-return, #ai-dot's visually-hidden status text, aria-label on
icon-only 📎/🎤/🔒/⌕ buttons, the lightbox's role=dialog/aria-modal, hover
previews mirrored via focusin/focusout, aria-live="polite" on the RAG
status line (not the rapidly-ticking token/timing counter), a skip-to-
content link, and the --text-dim/.rel-sub contrast fix. Mostly markup/CSS
presence checks — the interactive keyboard/focus behavior (Escape closing
a dropdown and returning focus, tabbing triggering the hover preview) is
covered by a live browser check (see session notes), not practical to
drive through a plain HTTP test client."""
from pathlib import Path

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login

REPO_ROOT = Path(__file__).parent.parent


def test_dropdown_buttons_have_aria_haspopup_and_expanded(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="world-btn" aria-haspopup="true" aria-expanded="false"' in r.text
    assert "function ndToggleDropdown(btn)" in r.text
    assert "function ndCloseDropdowns(returnFocus)" in r.text
    assert "e.key === 'Escape'" in r.text


def test_ai_dot_has_visually_hidden_status_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="ai-dot-status" class="sr-only"' in r.text
    assert "dotStatus.textContent" in r.text


def test_icon_only_buttons_have_aria_labels(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r_home = client.get("/")
    assert 'aria-label="Search"' in r_home.text

    r_kind = client.get("/kind/character")
    assert 'aria-label="Filter"' in r_kind.text

    eid = _make_entity(seed.world_a.id)
    r_entity = client.get(f"/entity/{eid}")
    assert 'aria-label="Attach a file"' in r_entity.text
    assert 'aria-label="Record a voice message"' in r_entity.text
    assert 'aria-label="Process a voice memo in the background"' in r_entity.text

    r_ai = client.get("/ai")
    assert 'aria-label="Attach a file"' in r_ai.text
    assert 'aria-label="Record a voice message"' in r_ai.text
    assert 'aria-label="Process a voice memo in the background"' in r_ai.text
    assert 'aria-label="Send as a background job"' in r_ai.text


def _make_entity(world_id):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="character", name="Test Entity")
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_lightbox_has_dialog_semantics_and_focus_management(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="lightbox-overlay" class="lightbox-overlay" role="dialog" aria-modal="true"' in r.text
    assert "_lightboxReturnFocus = document.activeElement" in r.text
    assert "document.querySelector('.lightbox-close').focus()" in r.text
    assert "_lightboxReturnFocus.focus()" in r.text


def test_hover_preview_mirrored_via_focusin_focusout(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert "function handleAnchorEnter(a)" in r.text
    assert "function handleAnchorLeave(a)" in r.text
    assert "document.addEventListener('focusin'" in r.text
    assert "document.addEventListener('focusout'" in r.text


def test_rag_status_line_is_a_live_region_not_the_token_counter(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/ai")
    assert r.status_code == 200
    assert 'id="ctx-status" role="status" aria-live="polite"' in r.text
    assert 'id="ctx-status-mob" role="status" aria-live="polite"' in r.text
    assert 'id="ctx-status-mob2" role="status" aria-live="polite"' in r.text
    # The rapidly-ticking (250ms) token/timing counter deliberately isn't a
    # live region — that would spam a screen reader far worse than the
    # per-token message text the plan explicitly said to avoid.
    assert 'id="gen-stats" role="status"' not in r.text
    assert 'id="gen-stats" aria-live' not in r.text


def test_skip_to_content_link_present(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    assert '<a href="#main-content" class="skip-link">Skip to content</a>' in r.text
    assert 'id="main-content"' in r.text
    assert r.text.index('class="skip-link"') < r.text.index('class="topbar"')


def test_text_dim_and_rel_sub_meet_wcag_aa_contrast():
    def luminance(hex_color):
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

        def chan(c):
            c = c / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)

    def contrast(c1, c2):
        l1, l2 = luminance(c1), luminance(c2)
        l1, l2 = max(l1, l2), min(l1, l2)
        return (l1 + 0.05) / (l2 + 0.05)

    with open(REPO_ROOT / "static" / "style.css") as f:
        css = f.read()
    assert "--text-dim: #7c849d;" in css
    for bg in ("#0a0a0f", "#0f0f1a", "#14141f"):  # --bg / --bg2 / --bg3
        assert contrast("#7c849d", bg) >= 4.5, f"--text-dim fails WCAG AA against {bg}"

    with open(REPO_ROOT / "app" / "templates" / "entities" / "detail.html") as f:
        detail_css = f.read()
    assert ".rel-sub { font-size:.72rem; color:#818181; }" in detail_css
    assert contrast("#818181", "#161616") >= 4.5  # .rel-card's own background
