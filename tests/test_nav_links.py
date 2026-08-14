"""Regression test for the vanishing nav-link bug: because every router used to
build its own Jinja2Templates() (a fresh jinja2.Environment each time), only
main.py's and characters.py's ever registered the kinds/kind_icons globals
that base.html's nav loops over — so all 8 lore-kind links silently rendered
as nothing on /quests, /sessions, /tables, /combat, /parties, /calendar,
/import, and login. app/templating.py fixes this by sharing one environment
app-wide; this test locks that in.
"""
import re

import pytest

from app.constants import KINDS
from .conftest import GM_PASSWORD, login

# All of these previously rendered zero /kind/ nav links (main.py's own pages,
# and characters.py's, already worked and aren't the regression surface).
PAGES = ["/quests", "/sessions", "/tables", "/combat", "/parties", "/calendar", "/import", "/facts", "/chronicler", "/session-log"]


@pytest.mark.parametrize("page", PAGES)
def test_all_kind_nav_links_render(client, seed, page):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(page)
    assert r.status_code == 200, f"{page} returned {r.status_code}"
    # Kind nav links now carry a trailing ?w=<world slug> (see app/deps.py's
    # with_world / the `wq` template filter) so a copied link stays pinned
    # to the world it was generated from — strip that suffix before
    # comparing, since this test is about the /kind/<kind> links existing
    # at all, not their query string.
    found = {href.split("?")[0] for href in re.findall(r'href="(/kind/[a-z_]+(?:\?[^"]*)?)"', r.text)}
    expected = {f"/kind/{k}" for k in KINDS}
    assert found == expected, f"{page}: expected {len(expected)} kind nav links, found {found}"


def test_login_page_renders_without_error(client):
    """/login builds its own page before any user session exists — a good
    canary for the shared-templates wiring breaking something unrelated."""
    r = client.get("/login")
    assert r.status_code == 200


def test_tools_dropdowns_contain_every_relocated_link(client, seed):
    """A brief flat-tab experiment made the nav too crowded (~30 top-level
    tabs) — the GM Tools/AI Tools items are back to click-to-open dropdown
    menus (tools-switcher/tools-btn/tools-dropdown/tools-option). This just
    confirms every relocated page's link still renders somewhere in the nav,
    regardless of grouping."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/")
    assert r.status_code == 200
    for cls in ("tools-switcher", "tools-btn", "tools-dropdown", "tools-option"):
        assert cls in r.text
    for ref in ("/boards", "/tables", "/combat", "/parties", "/quests", "/sessions",
                "/facts", "/calendar", "/images", "/import", "/export",
                "/ai", "/imagestudio", "/editor"):
        assert f'data-ql-ref="{ref}"' in r.text, f"expected a nav link for {ref}"
