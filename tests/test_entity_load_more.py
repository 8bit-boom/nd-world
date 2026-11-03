"""Big entity card lists show the first page and a "Load more" link (?limit=N / ?limit=all)."""
import os
import re

import httpx
import pytest

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD

DIRECT = os.environ.get("ND_GOLDEN_DIRECT_URL", "")


def _login(c, seed):
    r = c.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    c.cookies.set("active_world", seed.world_a.slug)
    return c


@pytest.fixture()
def many(seed):
    db = SessionLocal()
    try:
        for i in range(150):
            db.add(Entity(world_id=seed.world_a.id, kind="location", name=f"Place {i:03d}", body="x"))
        db.commit()
    finally:
        db.close()


def _cards(html):
    return len(re.findall(r'class="[^"]*entity-card-wrap', html))


def test_first_page_then_more(client, seed, many):
    c = _login(client, seed)
    h = c.get("/kind/location").text
    assert _cards(h) == 120
    assert 'id="load-more-wrap"' in h and "Showing 120 of 150" in h and "limit=240" in h
    h = c.get("/kind/location?limit=240").text
    assert _cards(h) == 150 and 'id="load-more-wrap"' not in h
    h = c.get("/kind/location?limit=all").text
    assert _cards(h) == 150 and 'id="load-more-wrap"' not in h


def test_small_lists_have_no_button(client, seed):
    c = _login(client, seed)
    db = SessionLocal()
    try:
        db.add(Entity(world_id=seed.world_a.id, kind="npc", name="Solo", body="x"))
        db.commit()
    finally:
        db.close()
    assert 'id="load-more-wrap"' not in c.get("/kind/npc").text


@pytest.mark.skipif(not DIRECT or not os.environ.get("ND_TEST_BASE_URL"), reason="needs a gateway and the app's own address")
def test_same_through_the_gateway(client, seed, many):
    via = _login(client, seed)
    direct = _login(httpx.Client(base_url=DIRECT, timeout=120), seed)
    for q in ("", "?limit=240", "?limit=all", "?limit=7", "?q=Place+1", "?limit=240&q=Place"):
        a, b = direct.get("/kind/location" + q), via.get("/kind/location" + q)
        assert a.status_code == b.status_code == 200, q
        assert a.text == b.text, q
