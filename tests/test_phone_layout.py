"""Phone layout for Player Characters, Parties and the character hub.

Pages opt into the phone rules through `<main class="mp">` (the base template's
`main_attrs` block); static/style.css then gives everything under `.mp` 16px
fields (iOS zooms into smaller ones), >=44px tap targets, stacked-card edit tables,
a folded "Export & more" menu and a sticky Save bar. A visual regression can't be
unit-tested, so these pin the wiring the audit relies on: every character/party
page carries the scope class, the rules exist, the risky actions sit behind the
menu, and the dynamic edit rows are labelled for the stacked layout.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate, World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text()
MP_MAIN = re.compile(r'<main id="main-content"[^>]*\bclass="mp"')


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj.id
    finally:
        db.close()


def _scene(seed):
    stats = [{"id": k, "value": 3} for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")]
    tpl = _add(SheetTemplate(world_id=seed.world_a.id, name="Plain", slug="plain-phone", sheet_mode="custom",
                             fields_json=json.dumps([{"id": "grit", "label": "Grit", "type": "number"}])))
    pc = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Auto",
                              stats_json=json.dumps(stats), max_hp=0, current_hp=14))
    custom = _add(PlayerCharacter(world_id=seed.world_a.id, owner_user_id=seed.player_a.id, name="Plain One",
                                  sheet_template_id=tpl))
    party = _add(Party(world_id=seed.world_a.id, name="Crew", member_pc_ids_json=json.dumps([pc, custom])))
    db = SessionLocal()
    try:
        db.get(World, seed.world_a.id).section_access_json = json.dumps({"parties": {"player": "read"}})
        db.commit()
    finally:
        db.close()
    return pc, custom, party


def _login(client, seed, who):
    if who == "gm":
        login(client, seed.gm.email, GM_PASSWORD)
    else:
        login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def _pages(pc, custom, party):
    return ["/characters", f"/characters/{pc}", f"/characters/{custom}", f"/characters/{pc}/edit",
            "/characters/templates", "/parties", f"/parties/{party}", f"/parties/{party}/summary",
            f"/parties/{party}/roster"]


def test_every_character_and_party_page_carries_the_phone_scope(client, seed):
    pc, custom, party = _scene(seed)
    for who in ("player", "gm"):
        _login(client, seed, who)
        for path in _pages(pc, custom, party):
            r = client.get(path)
            if r.status_code == 403:  # e.g. template admin for a player
                continue
            assert r.status_code == 200, f"{who} {path}: {r.status_code}"
            assert MP_MAIN.search(r.text), f"{who} {path} is missing <main class=\"mp\">"


def test_other_pages_are_not_swept_into_the_phone_scope(client, seed):
    _scene(seed)
    _login(client, seed, "gm")
    r = client.get("/")
    assert r.status_code == 200
    assert not MP_MAIN.search(r.text) and '<main id="main-content">' in r.text


def test_phone_rules_exist_for_fields_targets_and_cards():
    block = CSS[CSS.index("Phone layout for Player Characters"):]
    for needle in (
        "font-size: 16px !important",           # no iOS zoom-on-focus
        ".mp .pch-tab { min-height: 44px",       # hub tabs
        ".mp .cs-res-btn { width: 44px; height: 44px; }",  # +/- steppers
        ".mp .cf-submit-row {",                  # sticky Save bar
        "position: sticky",
        ".mp .cf-dynamic-table tr {",            # stacked edit rows
        "content: attr(data-label)",
        ".mp .cs-more.js:not(.open) .cs-more-body { display: none; }",
        ".mp .nd-float-btn { display: none !important; }",
    ):
        assert needle in block, f"phone CSS lost: {needle}"
    assert block.count("@media (max-width: 768px)") >= 1 and "@media (max-width: 640px)" in block


class _Ancestors(HTMLParser):
    """For every <a>/<form> record the ids of the elements it is nested in."""
    VOID = {"input", "br", "img", "meta", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("a", "form"):
            self.found.append((tag, a.get("href") or a.get("action") or "", [i for _, i in self.stack if i]))
        if tag not in self.VOID:
            self.stack.append((tag, a.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _nesting(html):
    p = _Ancestors()
    p.feed(html)
    return p.found


def test_export_retire_and_delete_sit_behind_the_more_menu(client, seed):
    pc, custom, _ = _scene(seed)
    for who in ("player", "gm"):
        _login(client, seed, who)
        for cid in (pc, custom):
            html = client.get(f"/characters/{cid}").text
            found = _nesting(html)

            def inside_menu(suffix):
                hits = [ids for _, target, ids in found if target.endswith(suffix) and f"/characters/{cid}/" in target]
                assert hits, f"{who} {cid}: no {suffix} control rendered"
                return all("cs-more-body" in ids for ids in hits)

            assert inside_menu("/export.json") and inside_menu("/export.pdf"), f"{who} {cid}: exports not folded"
            assert inside_menu("/delete"), f"{who} {cid}: Delete must not sit outside the menu"
            assert 'class="cs-more-toggle"' in html and "classList.add('js')" in html
            if cid == pc:  # Edit stays one tap away, outside the menu
                edit = [ids for _, target, ids in found if f"/characters/{cid}/edit" in target]
                assert edit and all("cs-more-body" not in ids for ids in edit)


def test_edit_form_rows_are_labelled_for_the_stacked_layout(client, seed):
    pc, _, _ = _scene(seed)
    _login(client, seed, "player")
    html = client.get(f"/characters/{pc}/edit").text
    for label in ("Feat", "Type", "Rank", "Item", "Qty", "Weight", "Equipped", "Implant", "CA cost", "Currency", "Amount"):
        assert f'data-label="{label}"' in html, f"row cell {label!r} has no data-label"
    assert "data-wide" in html and "data-check" in html
    # the delete button cell stays unlabelled (positioned top-right by the CSS)
    assert "<td>${delBtn(" in html


def test_party_loot_row_controls_are_classed_for_big_targets(client, seed):
    _, _, party = _scene(seed)
    _login(client, seed, "gm")
    html = client.get(f"/parties/{party}").text
    for cls in ("loot-row", "loot-claim-btn", "loot-claims", "loot-give", "loot-remove"):
        assert cls in html, cls


def test_party_members_list_uses_the_effective_hp_maximum(client, seed):
    """An auto-HP character (stored max 0) is 14/22 everywhere — the read-only
    Members list used to print the raw column ("HP 14/0")."""
    pc, _, party = _scene(seed)
    _login(client, seed, "player")
    html = client.get(f"/parties/{party}").text
    assert "14/0" not in html
    assert "14/22" in html
