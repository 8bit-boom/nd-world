"""/rules renders core_rules.md and per-world custom rules_md as HTML.
Regression coverage for a bug where legacy <a name="..."></a> anchors
(from a Word/Docs export) got HTML-escaped by render_md()'s XSS guard and
then double-escaped again in the sidebar TOC, showing literal "&lt;a
name=..." text instead of a clean heading — see app.main._world_rules_markdown.
"""
import json

from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_core_rules_anchor_tags_not_shown_as_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "<a name=" not in r.text.lower().replace("&lt;a name=", "")
    assert "&lt;a name=" not in r.text
    assert "&amp;lt;a name=" not in r.text


def test_custom_world_rules_anchor_tags_not_shown_as_text(client, seed):
    """Same bug, but via a world's own custom rules_md instead of the
    bundled core_rules.md fallback — both paths go through
    _world_rules_markdown()."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = '## <a name="test-heading"></a>1. Test Heading\n\nBody text.'
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "&lt;a name=" not in r.text
    assert "&amp;lt;a name=" not in r.text
    assert "Test Heading" in r.text
    assert "Body text." in r.text


def test_rules_toc_labels_are_clean(client, seed):
    """The sidebar TOC label for a heading must be plain text, not leak
    HTML-entity artifacts from the anchor-stripping/escaping pipeline."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = '## <a name="core-stats"></a>1. Core Stats\n\nBody text.'
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert '>1. Core Stats<' in r.text
    assert "&amp;lt;" not in r.text


def test_double_escaped_entities_render_as_characters(client, seed):
    """Doc-export MD often carries PRE-ESCAPED entities ("&amp;amp;" where
    the author typed "&") — markdown2 passes them through untouched, so the
    page rendered the literal text "&amp;". Unescaping at render time
    normalizes one level of escaping; render_md's safe_mode still escapes
    any raw tag the unescape resurrects."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = (
            "## Part II — Races &amp;amp; Optional Systems\n\n"
            "Tom &amp; Jerry & friends.\n"
            "Raw ampersand stays too: bread & butter.\n"
        )
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    # The double-escaped entity now displays as a single ampersand: the HTML
    # carries it once-escaped ("&amp;"), never as the literal text "&amp;".
    assert "&amp;amp;" not in r.text
    assert "Races &amp; Optional Systems" in r.text
    assert "Tom &amp; Jerry &amp; friends" in r.text
    assert "bread &amp; butter" in r.text
    # The TOC label is once-escaped too (it renders as "...Races &
    # Optional Systems" in the browser) and appears alongside the heading
    # itself, so the same once-escaped text shows up at least twice.
    assert "&amp;amp;" not in r.text
    assert r.text.count("Races &amp; Optional Systems") >= 2


# ── Markdown superpowers (app/rules_render.py) rendered through /rules ────────
#
# Unit-level coverage of the renderer itself lives in test_rules_render.py;
# these prove the wiring: that rules_page actually renders directive blocks
# and statblocks, and that the :::gm removal is real at the HTTP layer.

def _set_rules(world_id, rules_md=None, rules_json=None):
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == world_id).first()
        if rules_md is not None:
            w.rules_md = rules_md
        if rules_json is not None:
            w.rules_json = rules_json
        db.commit()
    finally:
        db.close()


def test_rules_gm_block_gm_sees_player_does_not(client, seed):
    """:::gm content ships for a GM but is REMOVED server-side for players —
    not CSS-hidden: neither the secret text nor the block's class hook may
    appear anywhere in the player's HTML (they could read view-source)."""
    _set_rules(seed.world_a.id, rules_md=(
        "## Open Section\n\nPublic text.\n\n"
        ":::gm\nCLASSIFIED-BRIEFING-TEXT\n:::\n"
    ))
    client.cookies.set("active_world", seed.world_a.slug)

    login(client, seed.gm.email, GM_PASSWORD)
    gm_page = client.get("/rules")
    assert gm_page.status_code == 200
    assert 'class="nd-callout nd-callout-gm' in gm_page.text
    assert "CLASSIFIED-BRIEFING-TEXT" in gm_page.text

    client.get("/logout")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    player_page = client.get("/rules")
    assert player_page.status_code == 200
    assert "CLASSIFIED-BRIEFING-TEXT" not in player_page.text
    assert 'class="nd-callout nd-callout-gm' not in player_page.text
    # The public content around the block is untouched.
    assert "Public text." in player_page.text


def test_rules_callout_and_statblock_render_on_page(client, seed):
    _set_rules(seed.world_a.id, rules_md=(
        "## Gear\n\n:::tip Salvage First\nStrip it before selling.\n:::\n\n"
        "```statblock\nRust Hound\nAC: 15\nHP: 40\n```\n"
    ))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert 'class="nd-callout nd-callout-tip"' in r.text
    assert "💡 Salvage First" in r.text
    assert 'class="nd-statblock-name">Rust Hound</div>' in r.text
    assert 'class="nd-statblock-copy"' in r.text


# ── rules.json overlay ────────────────────────────────────────────────────────

def test_rules_overlay_title_icon_order_applied(client, seed):
    """Overlay renames a section (heading AND sidebar TOC), and order
    reorders the flat page (Beta ordered before Alpha)."""
    _set_rules(seed.world_a.id, rules_md=(
        "## Alpha\n\na\n\n## Beta\n\nb\n\n## Gamma\n\ng\n"
    ), rules_json=(
        '{"sections": {'
        '"beta": {"order": 1}, "alpha": {"order": 2},'
        '"gamma": {"icon": "🧬", "title": "Races & Optional Systems"}}}'
    ))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    # TOC lists beta before alpha (both appear in sidebar TOC and body, in
    # the same final order, so the first occurrence comparison holds).
    assert r.text.index("Beta") < r.text.index("Alpha")
    assert "🧬" in r.text
    # Renamed heading + TOC label (both once-escaped for display).
    assert r.text.count("Races &amp; Optional Systems") >= 2


def test_rules_overlay_players_visible_hides_section_from_players(client, seed):
    _set_rules(seed.world_a.id, rules_md=(
        "## Public Stuff\n\nvisible\n\n## Council Secrets\n\nHIDDEN-SECTION-TEXT\n"
    ), rules_json=(
        '{"sections": {"council-secrets": {"players_visible": false}}}'
    ))
    client.cookies.set("active_world", seed.world_a.slug)
    login(client, seed.gm.email, GM_PASSWORD)
    gm_page = client.get("/rules")
    assert "HIDDEN-SECTION-TEXT" in gm_page.text

    client.get("/logout")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    player_page = client.get("/rules")
    assert player_page.status_code == 200
    assert "HIDDEN-SECTION-TEXT" not in player_page.text
    assert "Council Secrets" not in player_page.text  # heading + TOC entry gone
    assert "Public Stuff" in player_page.text


def test_rules_overlay_tabs_render_tab_bar(client, seed):
    _set_rules(seed.world_a.id, rules_md=(
        "## Core Mechanic\n\nroll\n\n## Races\n\nbodies\n\n## Secrets\n\nshh\n"
    ), rules_json=(
        '{"tabs": [{"id": "core", "label": "Core Rules",'
        ' "sections": ["core-mechanic", "races"]}]}'
    ))
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert 'class="rules-tab"' in r.text
    assert "Core Rules</button>" in r.text
    assert 'data-tab-id="core"' in r.text
    # Sections listed in no tab land in the trailing "More" tab.
    assert ">More</button>" in r.text
    # Panes carry their sections.
    assert 'class="rules-tab-pane' in r.text
    assert "HIDDEN" not in r.text  # sanity: no stray marker leaked


def test_rules_overlay_invalid_json_still_renders(client, seed):
    """A stored overlay that doesn't parse is logged + ignored: the page must
    still be 200 with the plain (no tabs, no renames) flow."""
    _set_rules(seed.world_a.id, rules_md=(
        "## Plain Heading\n\nbody text\n"
    ), rules_json='{"sections": [broken')
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "Plain Heading" in r.text
    assert "body text" in r.text
    assert 'class="rules-tab"' not in r.text  # no tab bar


def test_rules_blank_rules_json_no_behavior_change(client, seed):
    _set_rules(seed.world_a.id, rules_md="## Chapter One\n\nthe text\n", rules_json="")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "Chapter One" in r.text
    assert "the text" in r.text
    assert 'class="rules-tab"' not in r.text
    # Section splitting alone must not alter the single-page flow's content.
    assert '<section class="rules-section" data-section-id="chapter-one">' in r.text


# ── rules editor (rules_json textarea) ────────────────────────────────────────

def test_rules_edit_saves_rules_json(client, seed):
    overlay = '{"tabs": [{"id": "core", "label": "Core", "sections": []}]}'
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/rules/edit",
                    data={"rules_md": "# New Rules", "rules_json": overlay},
                    follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.rules_md == "# New Rules"
        assert w.rules_json == overlay
    finally:
        db.close()


def test_rules_edit_blank_rules_json_clears_it(client, seed):
    _set_rules(seed.world_a.id, rules_json='{"tabs": []}')
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/rules/edit",
                    data={"rules_md": "# Rules", "rules_json": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.rules_json is None
    finally:
        db.close()


def test_rules_edit_rejects_invalid_rules_json_with_400(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/rules/edit",
                    data={"rules_md": "# Rules", "rules_json": '{"tabs": ['})
    assert r.status_code == 400
    assert "rules_json" in r.json()["detail"]
    # Nothing persisted on a rejected save.
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert w.rules_md is None  # unchanged by the failed POST
    finally:
        db.close()


def test_rules_edit_form_shows_stored_overlay_error(client, seed):
    _set_rules(seed.world_a.id, rules_json="not-json{")
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/rules/edit")
    assert r.status_code == 200
    # The editor surfaces WHY the stored overlay is ignored so a GM can fix it.
    assert "ignored" in r.text
    assert "not valid JSON" in r.text
    assert "not-json{" in r.text  # stored value still shown for fixing


def test_rules_import_accepts_overlay_only_json(client, seed):
    """The import route accepts the rules.json UI OVERLAY by itself —
    {"sections": ..., "tabs": ...} with no rules_md — setting
    World.rules_json while leaving the uploaded markdown untouched."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = "## Existing rules\n\nKeep me."
        db.commit()
    finally:
        db.close()

    overlay = {"sections": {"part-i-core-rules": {"icon": "⚔️", "players_visible": False}},
               "tabs": [{"label": "Core", "sections": ["part-i-core-rules"]}]}
    r = client.post(f"/worlds/{seed.world_a.id}/rules/import",
                    files={"file": ("overlay.json", json.dumps(overlay), "application/json")},
                    follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        # Markdown untouched, overlay stored normalized.
        assert "Keep me." in w.rules_md
        stored = json.loads(w.rules_json)
        assert stored["sections"]["part-i-core-rules"]["icon"] == "⚔️"
        assert stored["sections"]["part-i-core-rules"]["players_visible"] is False
    finally:
        db.close()


def test_rules_import_overlay_invalid_json_is_400(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    bad = json.dumps({"sections": {"x": {"players_visible": "not-a-bool"}}})
    r = client.post(f"/worlds/{seed.world_a.id}/rules/import",
                    files={"file": ("overlay.json", bad, "application/json")})
    assert r.status_code == 400
    assert "overlay invalid" in r.json()["detail"]


def test_rules_import_both_md_and_overlay(client, seed):
    """A file carrying BOTH applies both — the docs promise it."""
    payload = {"rules_md": "## Fresh rules\n\nFresh body.",
               "rules_json": {"tabs": [{"label": "All", "sections": []}]}}
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/rules/import",
                    files={"file": ("rules.json", json.dumps(payload), "application/json")},
                    follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        assert "Fresh rules" in w.rules_md
        assert json.loads(w.rules_json)["tabs"][0]["label"] == "All"
    finally:
        db.close()
