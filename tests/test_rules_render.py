"""Unit tests for app/rules_render.py — the rules-page markdown
"superpowers" renderer (::: directive callouts/collapses/GM-only blocks and
```statblock fences) plus the rules.json overlay parser/applier.

Pure function tests with no fixtures and no HTTP: the renderer is called
directly so every branch — including the is_gm=False secret-removal contract
that must hold server-side — is assertable without a server round-trip. The
/routes these functions back end-to-end (rules_page, the editor) are covered
in test_rules_page.py.
"""
import json

from app.rendering import render_md
from app.rules_render import (
    apply_rules_overlay,
    parse_rules_overlay,
    render_rules_markdown,
    split_rules_sections,
    suggest_tabs_overlay,
)


# Minimal section fixture shaped exactly like split_rules_sections output —
# used by the overlay applier tests so they don't depend on markdown2.
SECTIONS = [
    {"id": None, "level": 0, "title": "", "html": "<p>intro</p>"},
    {"id": "alpha", "level": 2, "title": "Alpha", "html": '<h2 id="alpha">Alpha</h2><p>a body</p>'},
    {"id": "beta", "level": 2, "title": "Beta", "html": '<h2 id="beta">Beta</h2><p>b body</p>'},
    {"id": "gamma", "level": 3, "title": "Gamma", "html": '<h3 id="gamma">Gamma</h3><p>g body</p>'},
]


# ── ::: directive callouts ────────────────────────────────────────────────────

def test_tip_callout_renders_themed_div_with_default_title():
    out = render_rules_markdown(":::tip\nSpend a **point**.\n:::", is_gm=False)
    assert 'class="nd-callout nd-callout-tip"' in out
    assert "💡 Tip" in out
    # Inner text went through the markdown pipeline (bold became <strong>).
    assert "<strong>point</strong>" in out


def test_callout_types_get_distinct_themes_and_default_titles():
    out = render_rules_markdown(
        ":::note\nn\n:::\n:::warning\nw\n:::\n:::danger\nd\n:::\n:::lore\nl\n:::",
        is_gm=False,
    )
    for css, icon, label in [
        ("note", "📝", "Note"), ("warning", "⚠️", "Warning"),
        ("danger", "☠️", "Danger"), ("lore", "📖", "Lore"),
    ]:
        assert f'nd-callout-{css}' in out
        assert f"{icon} {label}" in out


def test_callout_explicit_title_overrides_default_label():
    # The explicit title replaces the default LABEL but keeps the icon.
    out = render_rules_markdown(":::warning Overdrive Heat\nCoolant check.\n:::", is_gm=False)
    assert "⚠️ Overdrive Heat" in out
    assert "Warning" not in out


def test_collapse_renders_details_element():
    out = render_rules_markdown(":::collapse Deep Lore\nHidden **lore**.\n:::", is_gm=False)
    assert '<details class="nd-collapse"><summary>Deep Lore</summary>' in out
    assert "<strong>lore</strong>" in out


def test_collapse_without_title_defaults_to_details():
    out = render_rules_markdown(":::collapse\nBody.\n:::", is_gm=False)
    assert '<summary>Details</summary>' in out


def test_unknown_directive_renders_as_note_callout_never_dropped():
    out = render_rules_markdown(":::cipher\nDecoded text.\n:::", is_gm=False)
    # Unknown types become note callouts titled with the type name — the
    # GM's content must be visible, never silently dropped.
    assert 'class="nd-callout nd-callout-note"' in out
    assert "cipher" in out
    assert "Decoded text." in out


def test_gm_block_present_for_gm_absent_for_player():
    md = ":::gm\nThe secret passphrase is SUNDER-7.\n:::"
    gm_out = render_rules_markdown(md, is_gm=True)
    player_out = render_rules_markdown(md, is_gm=False)
    assert "SUNDER-7" in gm_out and "nd-callout-gm" in gm_out
    # Server-side removal: not CSS-hidden — the secret (and even the block's
    # own class hook) must be absent from the player HTML entirely.
    assert "SUNDER-7" not in player_out
    assert "nd-callout-gm" not in player_out


def test_unclosed_directive_at_eof_still_renders():
    out = render_rules_markdown("## Head\n\n:::tip\nNo closing fence", is_gm=False)
    assert "nd-callout-tip" in out
    assert "No closing fence" in out


def test_directive_inside_code_fence_stays_literal():
    out = render_rules_markdown("```\n:::tip not a callout\n```\n", is_gm=False)
    assert "nd-callout" not in out


def test_raw_html_inside_block_is_escaped():
    # The stored-XSS guard must hold inside ::: blocks: inner text runs
    # through the same safe_mode="escape" pipeline as the page body.
    out = render_rules_markdown(":::note\n<script>alert(1)</script>\n:::", is_gm=False)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_document_without_directives_matches_plain_render_md():
    md = "## A\n\nSome *text*.\n"
    assert render_rules_markdown(md, is_gm=False) == render_md(md)


# ── ```statblock ──────────────────────────────────────────────────────────────

def test_statblock_renders_name_rows_and_copy_button():
    md = "```statblock\nRust Hound\nAC: 15\nHP: 40\nIt bites hard.\n```"
    out = render_rules_markdown(md, is_gm=False)
    assert 'class="nd-statblock-name">Rust Hound</div>' in out
    assert 'class="nd-statblock-row"' in out
    assert "AC" in out and "15" in out and "HP" in out and "40" in out
    # Non "Label: value" lines render as free-text paragraphs.
    assert "It bites hard." in out
    assert 'class="nd-statblock-copy"' in out
    # Raw text carried for the copy-button wiring.
    assert "data-raw=" in out


def test_statblock_value_splits_on_first_colon_only():
    out = render_rules_markdown("```statblock\nChrono Drone\nUptime: 12:30\n```", is_gm=False)
    # "12:30" stays whole — the row split uses the FIRST colon.
    assert "12:30" in out
    assert '<span class="nd-statblock-label">Uptime</span>' in out


def test_statblock_bold_label_markers_stripped():
    out = render_rules_markdown("```statblock\nHound\n**AC**: 17\n```", is_gm=False)
    assert '<span class="nd-statblock-label">AC</span>' in out


def test_unclosed_statblock_at_eof_still_renders():
    out = render_rules_markdown("```statblock\nLone Sentry\nAC: 9", is_gm=False)
    assert 'class="nd-statblock-name">Lone Sentry</div>' in out


# ── overlay parser ────────────────────────────────────────────────────────────

def test_parse_rules_overlay_blank_is_noop():
    for blank in (None, "", "   \n"):
        overlay, err = parse_rules_overlay(blank)
        assert overlay is None and err is None


def test_parse_rules_overlay_invalid_json_reports_error():
    overlay, err = parse_rules_overlay("{oops")
    assert overlay is None and err and "JSON" in err


def test_parse_rules_overlay_wrong_top_level_shape_reports_error():
    for bad in ('[1, 2]', '"str"', '3', '{"sections": []}', '{"tabs": {}}',
                '{"tabs": [{"label": ""}]}', '{"sections": {"a": 3}}',
                '{"sections": {"a": {"players_visible": "yes"}}}',
                '{"sections": {"a": {"order": "x"}}}'):
        overlay, err = parse_rules_overlay(bad)
        assert overlay is None and err, bad


def test_parse_rules_overlay_normalizes_valid_shape():
    overlay, err = parse_rules_overlay(json.dumps({
        "sections": {"alpha": {"icon": "🧬", "title": "Races", "players_visible": False, "order": 2}},
        "tabs": [{"id": "core", "label": "Core", "sections": ["alpha"]}],
    }))
    assert err is None
    assert overlay["sections"]["alpha"] == {
        "icon": "🧬", "title": "Races", "players_visible": False, "order": 2,
    }
    assert overlay["tabs"] == [{"id": "core", "label": "Core", "sections": ["alpha"]}]


def test_parse_rules_overlay_tab_ids_dedupe():
    overlay, err = parse_rules_overlay('{"tabs": [{"label": "Dup"}, {"label": "Dup"}]}')
    assert err is None
    assert [t["id"] for t in overlay["tabs"]] == ["dup", "dup-2"]


# ── overlay applier ───────────────────────────────────────────────────────────

def test_overlay_renames_orders_and_hides_for_players():
    overlay, _ = parse_rules_overlay(json.dumps({
        "sections": {
            "beta": {"order": 1},
            "alpha": {"order": 2},
            "gamma": {"icon": "🧬", "title": "Races & Systems", "players_visible": False},
        },
    }))
    flat_gm, panes_gm = apply_rules_overlay(SECTIONS, overlay, is_gm=True)
    # order reorders the flat flow; unordered sections keep natural order after
    assert [s["id"] for s in flat_gm] == [None, "beta", "alpha", "gamma"]
    # icon+title rewrite the section heading (escaped — sections render |safe)
    assert "🧬 Races &amp; Systems" in flat_gm[-1]["html"]
    # GM sees player-hidden sections
    assert panes_gm is None  # no tabs -> flat flow
    flat_pl, _ = apply_rules_overlay(SECTIONS, overlay, is_gm=False)
    # players_visible:false is a hard server-side hide for players
    assert all(s["id"] != "gamma" for s in flat_pl)


def test_overlay_no_overlay_returns_sections_untouched():
    flat, panes = apply_rules_overlay(SECTIONS, None, is_gm=False)
    assert flat is SECTIONS and panes is None


def test_overlay_tabs_group_sections_with_trailing_more():
    overlay, _ = parse_rules_overlay(json.dumps({
        "tabs": [{"id": "core", "label": "Core", "sections": ["beta"]}],
    }))
    _, panes = apply_rules_overlay(SECTIONS, overlay, is_gm=False)
    assert [p["id"] for p in panes] == ["core", "more"]
    assert [s["id"] for s in panes[0]["sections"]] == [None, "beta"]  # preamble prepended
    assert [s["id"] for s in panes[1]["sections"]] == ["alpha", "gamma"]  # natural order


def test_overlay_fully_hidden_tab_falls_back_to_flat_for_players():
    overlay, _ = parse_rules_overlay(json.dumps({
        "sections": {"gamma": {"players_visible": False}},
        "tabs": [{"id": "x", "label": "X", "sections": ["gamma"]}],
    }))
    # Player: the only tab's content is hidden -> no tab bar at all
    _, panes_pl = apply_rules_overlay(SECTIONS, overlay, is_gm=False)
    assert panes_pl is None
    # GM: the tab survives; sections listed in no tab (alpha/beta) still land
    # in the trailing "More" tab — visibility filtering doesn't change grouping.
    _, panes_gm = apply_rules_overlay(SECTIONS, overlay, is_gm=True)
    assert [p["id"] for p in panes_gm] == ["x", "more"]


# ── overlay suggester ─────────────────────────────────────────────────────────

def test_suggest_tabs_overlay_groups_nested_headings_with_their_top_level_parent():
    # SECTIONS = [preamble, alpha(l2), beta(l2), gamma(l3)] — gamma is nested
    # under beta (the nearest preceding shallower heading), so it must ride
    # along into beta's tab rather than needing to be listed by hand.
    assert suggest_tabs_overlay(SECTIONS) == {
        "tabs": [
            {"label": "Alpha", "sections": ["alpha"]},
            {"label": "Beta", "sections": ["beta", "gamma"]},
        ],
    }


def test_suggest_tabs_overlay_no_id_bearing_sections_returns_no_tabs():
    preamble_only = [{"id": None, "level": 0, "title": "", "html": "<p>x</p>"}]
    assert suggest_tabs_overlay(preamble_only) == {"tabs": []}


def test_suggest_tabs_overlay_round_trips_with_no_leftovers_in_more():
    """This is the actual fix for the reported bug: hand-building a tab
    overlay by listing only a Part's own slug loses every chapter nested
    beneath it to the catch-all "More" tab (see
    test_overlay_tabs_group_sections_with_trailing_more, where listing only
    "beta" strands gamma in "more" even though gamma is beta's own chapter).
    The auto-built overlay must not have that problem: every section lands
    in a real tab, and no "more" pane is produced at all."""
    overlay, err = parse_rules_overlay(json.dumps(suggest_tabs_overlay(SECTIONS)))
    assert err is None
    _, panes = apply_rules_overlay(SECTIONS, overlay, is_gm=True)
    assert [p["id"] for p in panes] == ["alpha", "beta"]
    assert [s["id"] for s in panes[1]["sections"]] == ["beta", "gamma"]


# ── splitter ─────────────────────────────────────────────────────────────────

def test_split_rules_sections_preamble_and_headings():
    html = "<p>intro</p><h2 id=\"alpha\">Alpha</h2><p>a</p><h3 id=\"gamma\">Gamma</h3><p>g</p>"
    sections = split_rules_sections(html)
    assert [s["id"] for s in sections] == [None, "alpha", "gamma"]
    assert sections[0]["html"] == "<p>intro</p>"
    assert sections[1]["title"] == "Alpha" and sections[1]["level"] == 2
    # each section chunk starts at (and carries) its own heading
    assert sections[1]["html"].startswith('<h2 id="alpha">')


def test_split_rules_sections_no_headings_single_preamble():
    sections = split_rules_sections("<p>just text</p>")
    assert len(sections) == 1 and sections[0]["id"] is None
