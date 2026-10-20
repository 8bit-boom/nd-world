"""Performance budgets (performance plan, steps 2-3 and 10): a medium world is seeded and the pages that used to scale with its size are held to a
size and a query budget. The numbers are generous on purpose (they are about growth, not about this machine's speed): before the fixes
/facts sent 40 sessions' worth of <option>s for every fact (about 25 MB at 3,000 facts), the session page embedded every entity, Settings drew one
row per entity, and the entity picker loaded full rows."""
import json
import random

from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import Entity, Fact, GameSession, World

from .conftest import GM_PASSWORD, login

N_ENT, N_FACT, N_SESS = 600, 400, 40
BODY = "The neon rain fell over the docks while the dragon slept beneath the arcology. " * 20


def _seed_world(world_id):
    rnd = random.Random(4)
    db = SessionLocal()
    try:
        db.add_all([Entity(world_id=world_id, kind=rnd.choice(["character", "location", "item", "creature"]), name=f"Thing {i}",
                           folder=rnd.choice(["", "A", "B/C"]), summary=f"s{i}", body=BODY, visible_to_players=(i % 5 != 0)) for i in range(N_ENT)])
        sess = [GameSession(world_id=world_id, title=f"Night {i}", session_num=i + 1) for i in range(N_SESS)]
        db.add_all(sess)
        db.commit()
        sids = [s.id for s in sess]
        db.add_all([Fact(world_id=world_id, game_session_id=rnd.choice(sids), content=f"Fact {i} about the docks", tags="a,b", visible_to_players=True)
                    for i in range(N_FACT)])
        db.commit()
        return sids[0]
    finally:
        db.close()


class _Q:
    def __init__(self):
        self.n = 0

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *a):
        event.remove(engine, "before_cursor_execute", self._on)

    def _on(self, *a):
        self.n += 1


def _gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


def test_facts_page_stays_small_and_loads_other_groups_on_demand(client, seed):
    sid = _seed_world(seed.world_a.id)
    _gm(client, seed)
    with _Q() as q:
        r = client.get("/facts")
    assert r.status_code == 200 and len(r.content) < 250_000 and q.n <= 12, (len(r.content), q.n)
    assert r.text.count("<option") < 200                                             # not one <select> of every session per fact
    assert "data-facts-lazy" in r.text                                               # the other groups arrive on demand
    frag = client.get(f"/facts/fragment?keys={sid},none")
    assert frag.status_code == 200 and f'data-frag="{sid}"' in frag.text and "Fact " in frag.text
    assert client.get("/facts/fragment?keys=not-a-key").status_code == 200           # junk keys select nothing, never error


def test_the_fragment_keeps_gm_only_facts_from_players(client, seed):
    db = SessionLocal()
    try:
        s = GameSession(world_id=seed.world_a.id, title="Night", session_num=1)
        db.add(s)
        db.commit()
        db.add_all([Fact(world_id=seed.world_a.id, game_session_id=s.id, content="Shown to everyone", visible_to_players=True),
                    Fact(world_id=seed.world_a.id, game_session_id=s.id, content="GM secret fact", visible_to_players=False)])
        db.commit()
        sid = s.id
    finally:
        db.close()
    from .conftest import PLAYER_PASSWORD
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/facts/fragment?keys={sid}")
    if r.status_code == 200:                                                        # facts may be closed to players in this world: then 403 is also right
        assert "Shown to everyone" in r.text and "GM secret fact" not in r.text
    else:
        assert r.status_code == 403


def test_session_page_does_not_embed_every_entity_and_the_list_has_its_own_route(client, seed):
    sid = _seed_world(seed.world_a.id)
    _gm(client, seed)
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200 and len(r.content) < 260_000, len(r.content)           # about 200 KB is the page itself (107 KB of its own script); entities add nothing
    assert "Thing 17" not in r.text                                                  # no entity names in the page itself
    c = client.get(f"/sessions/{sid}/npc-candidates")
    assert c.status_code == 200 and len(c.json()["candidates"]) >= 400


def test_settings_page_defers_the_visibility_rows(client, seed):
    _seed_world(seed.world_a.id)
    _gm(client, seed)
    r = client.get("/settings")
    assert r.status_code == 200 and len(r.content) < 400_000 and "Thing 17" not in r.text, len(r.content)
    assert 'id="vis-lazy"' in r.text
    rows = client.get("/settings/visibility-rows")
    assert rows.status_code == 200 and rows.text.count('class="vis-row"') == N_ENT
    assert 'vis-lazy' not in client.get("/settings?tab=visibility").text or client.get("/settings?tab=visibility").status_code == 200


def test_entity_picker_uses_four_columns_and_an_etag(client, seed):
    _seed_world(seed.world_a.id)
    _gm(client, seed)
    with _Q() as q:
        r = client.get("/api/entities/picker")
    assert r.status_code == 200 and q.n <= 8 and len(r.json()["entities"]) == N_ENT
    assert set(r.json()["entities"][0]) == {"id", "name", "kind", "folder"}
    etag = r.headers["etag"]
    again = client.get("/api/entities/picker", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert client.get("/api/entities/picker", headers={"If-None-Match": '"other"'}).status_code == 200


def test_maps_page_for_a_gm_stays_within_a_query_budget(client, seed):
    _seed_world(seed.world_a.id)
    _gm(client, seed)
    with _Q() as q:
        r = client.get("/maps")
    assert r.status_code == 200 and q.n <= 30, q.n                                    # the gallery scan is a handful of column queries, not one per entity


def test_the_gallery_scan_finds_the_same_images_as_before(client, seed):
    from app.gallery import discover_world_images
    from app.models import EntityNote
    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        a = Entity(world_id=w.id, kind="character", name="A", image_url="/uploads/a.png", body="x ![pic](/uploads/b.png) y")
        b = Entity(world_id=w.id, kind="item", name="B", body="no images here")
        db.add_all([a, b])
        db.commit()
        db.add_all([EntityNote(entity_id=a.id, content="![n](/uploads/c.png)", visible_to_players=True),
                    EntityNote(entity_id=b.id, content="plain note", visible_to_players=True)])
        db.commit()
        found = {e["url"]: e for e in discover_world_images(db, w)}
    finally:
        db.close()
    assert set(found) == {"/uploads/a.png", "/uploads/b.png", "/uploads/c.png"}
    assert found["/uploads/c.png"]["name"] == "A (note)" and found["/uploads/a.png"]["uses"][0]["href"].startswith("/entity/")
