"""?w=<slug> query-param world resolution (app.deps.resolve_world_slug),
used by every "current world" page (via get_active_world/get_world_ctx) so a
shared link names its world explicitly instead of falling back to whichever
world the recipient's browser has cached in the active_world cookie.
"""
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_query_param_overrides_cookie_on_main_py_route(client, seed):
    """/rules is a main.py route resolved via get_active_world."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules", params={"w": seed.world_b.slug})
    assert r.status_code == 200
    title = r.text.split("<title>")[1].split("</title>")[0]
    assert seed.world_b.name in title
    assert seed.world_a.name not in title


def test_query_param_overrides_cookie_on_router_route(client, seed):
    """/races is a routers/races.py route resolved via get_world_ctx."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/races", params={"w": seed.world_b.slug})
    assert r.status_code == 200
    title = r.text.split("<title>")[1].split("</title>")[0]
    assert seed.world_b.name in title


def test_no_query_param_falls_back_to_cookie(client, seed):
    """Unchanged behavior for old links/bookmarks with no ?w= at all."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    title = r.text.split("<title>")[1].split("</title>")[0]
    assert seed.world_a.name in title


def test_query_param_naming_inaccessible_world_falls_back_safely(client, seed):
    """A player's ?w= can't be used to peek at a world they aren't a member
    of — same silent-fallback behavior an invalid cookie value gets today,
    not a 403/404 that would confirm the world's existence."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/rules", params={"w": seed.world_b.slug})
    assert r.status_code == 200
    title = r.text.split("<title>")[1].split("</title>")[0]
    assert seed.world_a.name in title
    assert seed.world_b.name not in title


def test_query_param_naming_nonexistent_world_falls_back_safely(client, seed):
    """A slug matching no World anywhere doesn't crash and doesn't leak
    anything — it falls through to the same default-world selection an
    invalid active_world cookie already falls through to today (not
    specifically back to the cookie's world, since the pre-existing
    cookie-only code never had a two-tier fallback either)."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/rules", params={"w": "does-not-exist"})
    assert r.status_code == 200
    title = r.text.split("<title>")[1].split("</title>")[0]
    # player_a's only accessible world is world_a, so the fallback is
    # deterministic regardless of what else exists in the database.
    assert seed.world_a.name in title


def test_home_page_links_carry_w(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/", params={"w": seed.world_a.slug})
    assert r.status_code == 200
    assert f"w={seed.world_a.slug}" in r.text


def test_world_switch_returns_to_next_page_qualified(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(
        f"/worlds/switch/{seed.world_b.slug}",
        params={"next": "/rules"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/rules?w={seed.world_b.slug}"


def test_world_switch_defaults_to_home_when_no_next(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/switch/{seed.world_b.slug}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/?w={seed.world_b.slug}"


def test_world_switch_rejects_unsafe_next(client, seed):
    """next= is passed through auth.safe_next_url — an absolute/protocol-
    relative target must not turn this into an open redirect."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(
        f"/worlds/switch/{seed.world_b.slug}",
        params={"next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/?w={seed.world_b.slug}"


def test_wq_filter_registered():
    from app.templating import templates
    assert "wq" in templates.env.filters


def test_base_template_nav_links_use_wq_filter():
    html = open("app/templates/base.html", encoding="utf-8").read()
    assert "'/rules'|wq" in html
    assert "'/maps'|wq" in html


def test_base_template_draggable_links_carry_clean_data_ql_ref(client, seed):
    """Regression guard: once nav hrefs carry ?w=<slug>, the drag-and-drop
    Quick Links feature's dragstart handler must not fall back to reading
    that decorated href as target_ref (app/templates/index.html's
    computeHref would then bake the world active at drag-time into the
    saved link forever) — every non-kind draggable anchor needs its own
    clean data-ql-ref.

    /boards, /ai, etc. are GM-manageable nav-menu items (see
    app/nav_menus.py) rendered from a Jinja macro rather than hardcoded
    markup, so this renders the real page (as a GM, so those items are
    actually in the DOM) instead of just grepping base.html's source."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert r.status_code == 200
    for ref in ("/maps", "/races", "/professions", "/boards", "/rules", "/characters", "/ai", "/imagestudio"):
        assert f'data-ql-ref="{ref}"' in r.text, f"missing clean data-ql-ref for {ref}"
        assert f'data-ql-ref="{ref}?w=' not in r.text, f"data-ql-ref for {ref} was decorated with ?w="


def test_search_form_has_hidden_w_input():
    html = open("app/templates/base.html", encoding="utf-8").read()
    assert 'name="w" value="{{ world.slug if world else \'\' }}"' in html
