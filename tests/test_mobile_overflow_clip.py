"""Regression tests for docs/MOBILE_UI_UX_AUDIT.md items 4.1 and 4.5: on
mobile the app sets `overflow-x: clip` on html/body (to stop page-wide
horizontal scroll from occasional wide content) — but content that's wider
than its own container and has no *local* scroll affordance gets silently
cut off with no way to reach it at all (unlike plain `overflow: hidden`,
`clip` can't even be scrolled to programmatically).

4.1: the Settings page's 4-tab bar (`.settings-tab-bar`) had no wrap/scroll,
so the 4th tab ("Navigation") was clipped to an untappable sliver on a
390px-wide phone.

4.5: the entity list's table view (`.stat-table`) hid every column past the
3rd on mobile (`nth-child(n+4) { display: none }`), permanently dropping
stat columns and feat descriptions instead of letting the table scroll."""
from pathlib import Path

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login

STATIC = Path(__file__).parent.parent / "static"
TEMPLATES = Path(__file__).parent.parent / "app" / "templates"


def _make_feat_entities(world_id, count=3):
    db = SessionLocal()
    try:
        made = []
        for i in range(count):
            e = Entity(world_id=world_id, kind="feat", name=f"Table Test Feat {i+1}",
                       summary=f"Description for feat {i+1}")
            db.add(e)
            made.append(e)
        db.commit()
        for e in made:
            db.refresh(e)
        return [e.id for e in made]
    finally:
        db.close()


# ── 4.1: Settings tab bar ────────────────────────────────────────────────────

def test_settings_tab_bar_scrolls_instead_of_clipping(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/settings")
    assert r.status_code == 200
    # Not `.split("<style>")` — base.html's own theming <style> block (in
    # <head>, world accent-color overrides) comes first in the page.
    tab_bar_block = r.text.split(".settings-tab-bar {", 1)[1].split("}", 1)[0]
    assert "overflow-x:auto" in tab_bar_block.replace(" ", "")
    tab_block = r.text.split(".settings-tab {", 1)[1].split("}", 1)[0]
    assert "white-space:nowrap" in tab_block.replace(" ", "")
    assert "flex-shrink:0" in tab_block.replace(" ", "")
    # All 4 tabs still present and reachable in the markup regardless of
    # viewport — scrolling only affects what's visible, not what's rendered.
    for tab_id in ("tab-options", "tab-visibility", "tab-system", "tab-navigation"):
        assert f'id="{tab_id}"' in r.text


# ── 4.5: Entity list table view ──────────────────────────────────────────────

def test_entity_table_view_wraps_table_in_a_scrollable_container(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    _make_feat_entities(seed.world_a.id)
    r = client.get("/kind/feat?view=table")
    assert r.status_code == 200
    assert 'class="stat-table-wrap"' in r.text
    wrap_start = r.text.index('class="stat-table-wrap"')
    wrap_html = r.text[wrap_start:wrap_start + 400]
    assert "stat-table" in wrap_html
    # The actual description text a mobile user needs to reach must still be
    # present in the DOM (reachable via the wrapper's scroll), not omitted.
    assert "Description for feat 1" in r.text


def test_stat_table_wrap_css_scrolls_and_columns_are_no_longer_hidden():
    content = (STATIC / "style.css").read_text()
    wrap_block = content.split(".stat-table-wrap {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto" in wrap_block
    # The old fix hid every column past the 3rd on mobile instead of
    # scrolling — that selector must be gone now that .stat-table-wrap
    # handles it.
    assert "nth-child(n+4)" not in content


def test_render_table_macro_wraps_both_table_branches():
    """render_table() has two branches (table_cols set vs the feat/rank
    fallback) — both must be wrapped, not just one."""
    content = (TEMPLATES / "entities" / "list.html").read_text()
    macro_src = content.split("{% macro render_table(rows) %}", 1)[1].split("{% endmacro %}", 1)[0]
    assert macro_src.count('<table class="stat-table">') == 2
    assert macro_src.count('class="stat-table-wrap"') == 1
    assert macro_src.strip().startswith('<div class="stat-table-wrap">')
