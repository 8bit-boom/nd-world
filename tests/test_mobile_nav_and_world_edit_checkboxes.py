"""Regression tests for docs/MOBILE_UI_UX_AUDIT.md item 4.2 ("checkbox
stacks orphaned from their labels") plus a mobile nav tap-target
inconsistency found alongside it.

World edit checkboxes: each is a <div class="form-group" style="...
align-items:center..."> wrapping a sibling <input type=checkbox> +
<label> with a long descriptive sentence ("Players can generate images
for their own character (private to them and you — other players never
see it)"). align-items:center vertically centers the checkbox against
the label's full height — fine for a single line, but once the label
wraps to 2-3 lines on a narrow phone the checkbox ends up floating in
the middle of the block instead of next to the label's first line,
looking detached ("orphaned") from it. Fixed with align-items:flex-start
plus a small margin-top nudge on the checkbox for optical alignment.

Mobile nav tap targets: the "Improve touch targets" mobile block already
bumps .nav-kind (the hamburger dropdown's main links) to min-height:36px,
but left out .tools-btn/.tools-option (the Tools submenu nested inside
that same dropdown) and .world-btn (the World switcher, always visible
in the topbar) — smaller than their sibling nav controls in the exact
same menu."""
from pathlib import Path

from .conftest import GM_PASSWORD, login

STATIC = Path(__file__).parent.parent / "static"


def test_world_edit_checkbox_rows_top_align_instead_of_center(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    checkbox_ids = (
        "players-see-party", "players-download-rules", "players-download-entities",
        "players-ask-ai", "players-ai-chat", "players-world-summary", "players-image-gen",
    )
    assert r.text.count('align-items:flex-start;gap:.5rem') >= len(checkbox_ids)
    assert 'align-items:center;gap:.5rem"' not in r.text
    for cid in checkbox_ids:
        needle = f'id="{cid}"'
        idx = r.text.index(needle)
        tag_end = r.text.index(">", idx)
        input_tag = r.text[idx:tag_end]
        assert "margin-top:.2rem" in input_tag, f"#{cid} checkbox missing optical-alignment nudge"


def test_mobile_nav_submenu_and_world_switcher_get_the_same_touch_bump_as_nav_kind(client, seed):
    content = (STATIC / "style.css").read_text()
    coarse_block = content.split("/* Improve touch targets */", 1)[1].split("\n\n", 1)[0]
    assert ".nav-kind, .dash-card { min-height: 36px; }" in coarse_block
    assert ".world-btn, .tools-btn, .tools-option, .world-option { min-height: 36px; }" in coarse_block
