"""Regression test for the N+1 fix in app.gallery.discover_world_images
(plan item Speed 4.6): the loop over each world entity used to lazy-load
`e.notes` one query at a time (EntityNote.entity's own backref) — one extra
SQL query per entity, every single /images page load. selectinload(Entity.
notes) batches that into a single additional query regardless of how many
entities exist."""
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.gallery import discover_world_images
from app.models import Entity, EntityNote

from .conftest import GM_PASSWORD, login


def _make_entity_with_note(world_id, i):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="character", name=f"NPC {i}")
        db.add(e)
        db.commit()
        db.refresh(e)
        db.add(EntityNote(entity_id=e.id, content=f"![clue{i}](/uploads/clue{i}.png)", visible_to_players=True))
        db.commit()
        return e.id
    finally:
        db.close()


class _QueryCounter:
    def __init__(self):
        self.count = 0

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1


def test_discover_world_images_query_count_does_not_scale_with_entity_count(client, seed):
    for i in range(20):
        _make_entity_with_note(seed.world_a.id, i)

    db = SessionLocal()
    try:
        with _QueryCounter() as counter:
            result = discover_world_images(db, seed.world_a)
        assert len(result) == 20  # sanity: the notes' images were actually found
    finally:
        db.close()

    # One query for entities, one batched selectinload for their notes, one
    # for player characters — a small constant, not one-per-entity (which
    # would be 20+ on its own before even counting the base queries).
    assert counter.count <= 5, f"expected O(1) queries, got {counter.count}"


def test_images_route_still_finds_note_images_after_the_selectinload_change(client, seed):
    _make_entity_with_note(seed.world_a.id, 0)
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    assert "/uploads/clue0.png" in r.text


# ── Batched client-side rendering past the initial batch ───────────────────

def _make_entity_with_portrait(world_id, i):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="character", name=f"NPC {i}", image_url=f"/uploads/portrait{i}.png")
        db.add(e)
        db.commit()
    finally:
        db.close()


def test_under_the_batch_threshold_no_load_more_ships(client, seed, monkeypatch):
    import app.routers.gallery as gallery_router
    monkeypatch.setattr(gallery_router, "_GALLERY_INITIAL_BATCH", 5)
    for i in range(3):
        _make_entity_with_portrait(seed.world_a.id, i)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    # The always-present JS below references both identifiers (guarded by
    # `typeof NDGalleryRemaining !== 'undefined'` and getElementById), so
    # the precise thing that must be absent is the actual declaration/
    # element, not the bare identifier substring, which legitimately
    # appears in that unconditional JS either way.
    assert "const NDGalleryRemaining" not in r.text
    assert 'id="gallery-load-more-btn"' not in r.text
    for i in range(3):
        assert f"/uploads/portrait{i}.png" in r.text


def test_over_the_batch_threshold_ships_the_remainder_as_json(client, seed, monkeypatch):
    import app.routers.gallery as gallery_router
    monkeypatch.setattr(gallery_router, "_GALLERY_INITIAL_BATCH", 2)
    for i in range(5):
        _make_entity_with_portrait(seed.world_a.id, i)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/images")
    assert r.status_code == 200
    assert "gallery-load-more-btn" in r.text
    assert "const NDGalleryRemaining" in r.text
    # Exactly 2 rendered server-side as real <label class="gallery-img-cell">
    # elements — the double-quoted HTML attribute form, so this can't also
    # match the CSS rules (.gallery-img-cell {...}) or JS string literals
    # ('gallery-img-cell', single-quoted) that legitimately appear elsewhere
    # on the same page.
    assert r.text.count('class="gallery-img-cell"') == 2
    # ...and all 5 portraits are present SOMEWHERE on the page — either in
    # the server-rendered batch or the embedded JSON for the rest.
    for i in range(5):
        assert f"/uploads/portrait{i}.png" in r.text
