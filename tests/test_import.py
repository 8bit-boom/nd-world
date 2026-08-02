"""Tests for the /import feature: app/routers/importer.py (the JSON importer
that powers the /import page's "Analyze" + "Import" flow) and the legacy
POST /api/import route in app/main.py (still used by the standalone
import_chronicles.py / import_lore.py scripts, even though the web UI
doesn't call it).

Covers: cross-world writes via the map_overlay/schematic_elements import
kinds (the same class of bug already fixed for the direct
/api/maps/{slug}/overlay and schematic routes), missing row-locking on their
read-modify-write of elements_json/custom_markers_json (the same class of
race already fixed for move-token/pickup-item/buy-item/pull-push-combat),
unvalidated canvas dimensions, an unhandled-exception gap in the single-item
/api/import/execute path, an unbounded batch-import item count, and the
legacy /api/import route's missing validation (unhandled KeyError → non-JSON
500, no kind/world_id checks, no entity-count cap, no type coercion).

Also includes source-level regression tests for the corresponding
client-side fixes in app/templates/import.html (no JS runtime in this test
suite — see tests/test_bulk_image_import.py's
test_import_page_precheck_matches_server_cap for the established pattern
this follows).
"""
import asyncio
import json
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import main as main_module
from app.database import SessionLocal
from app.models import Entity, MapOverlay, PlayerCharacter, Schematic, User, World, WorldMembership
from app.routers import importer as importer_module

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _make_schematic(world_id, slug, name="Test Schematic", elements=None):
    db = SessionLocal()
    try:
        s = Schematic(world_id=world_id, name=name, slug=slug, is_html=False,
                      canvas_width=2000, canvas_height=1500, canvas_bg="dark",
                      elements_json=json.dumps(elements or []))
        db.add(s)
        db.commit()
        db.refresh(s)
        return s
    finally:
        db.close()


def _make_pc(world_id, owner_id, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_id, name=name)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc
    finally:
        db.close()


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeRequest:
    def __init__(self, user, body):
        self.state = _FakeState(user)
        self._body = body

    async def json(self):
        return self._body


def _import_execute(client, kind, data, params=None):
    return client.post("/api/import/execute", json={
        "json_text": json.dumps(data), "kind": kind, "params": params or {},
    })


# ── map_overlay import: cross-world + validation ────────────────────────────

def test_map_overlay_import_rejects_cross_world_map(client, seed):
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()
    (_MAPS_DIR / "b-only-map.json").write_text(json.dumps({
        "name": "B Map", "world_id": seed.world_b.id, "width": 2000, "height": 1500, "markers": [],
    }), encoding="utf-8")
    db = SessionLocal()
    try:
        db.add(MapOverlay(slug="b-only-map", custom_markers_json="[]", custom_regions_json="[]"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay",
                         {"custom_markers": [{"lat": 1, "lng": 2, "label": "hijack"}]},
                         {"map_slug": "b-only-map"})
    assert r.status_code == 400

    db = SessionLocal()
    try:
        overlay = db.query(MapOverlay).filter(MapOverlay.slug == "b-only-map").first()
        assert json.loads(overlay.custom_markers_json) == []
    finally:
        db.close()
    for jf in _MAPS_DIR.glob("*.json"):
        jf.unlink()


def test_map_overlay_import_rejects_nonexistent_map(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay", {"custom_markers": []}, {"map_slug": "does-not-exist"})
    assert r.status_code == 400


def test_map_overlay_import_rejects_non_list_custom_markers(client, seed):
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / "shape-map.json").write_text(json.dumps({
        "name": "Shape Map", "world_id": seed.world_a.id, "width": 2000, "height": 1500, "markers": [],
    }), encoding="utf-8")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay", {"custom_markers": "not-a-list"}, {"map_slug": "shape-map"})
    assert r.status_code == 400
    (_MAPS_DIR / "shape-map.json").unlink()


def test_map_overlay_import_rejects_dict_custom_markers_not_silently_corrupted(client, seed):
    """A dict for custom_markers used to get silently .extend()-ed in as if
    its keys were marker entries, rather than rejected."""
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / "dict-map.json").write_text(json.dumps({
        "name": "Dict Map", "world_id": seed.world_a.id, "width": 2000, "height": 1500, "markers": [],
    }), encoding="utf-8")

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay", {"custom_markers": {"a": 1, "b": 2}}, {"map_slug": "dict-map"})
    assert r.status_code == 400

    db = SessionLocal()
    try:
        overlay = db.query(MapOverlay).filter(MapOverlay.slug == "dict-map").first()
        assert overlay is None or json.loads(overlay.custom_markers_json) == []
    finally:
        db.close()
    (_MAPS_DIR / "dict-map.json").unlink()


def test_map_overlay_import_happy_path_merges_into_existing(client, seed):
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / "merge-map.json").write_text(json.dumps({
        "name": "Merge Map", "world_id": seed.world_a.id, "width": 2000, "height": 1500, "markers": [],
    }), encoding="utf-8")
    db = SessionLocal()
    try:
        db.add(MapOverlay(slug="merge-map",
                           custom_markers_json=json.dumps([{"lat": 0, "lng": 0, "label": "existing"}]),
                           custom_regions_json="[]"))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay",
                         {"custom_markers": [{"lat": 1, "lng": 1, "label": "new"}]},
                         {"map_slug": "merge-map"})
    assert r.status_code == 200

    db = SessionLocal()
    try:
        overlay = db.query(MapOverlay).filter(MapOverlay.slug == "merge-map").first()
        markers = json.loads(overlay.custom_markers_json)
        assert len(markers) == 2
        assert {m["label"] for m in markers} == {"existing", "new"}
    finally:
        db.close()
    (_MAPS_DIR / "merge-map.json").unlink()


def test_map_overlay_import_lock_contention_returns_clean_error(client, seed, monkeypatch):
    from app.main import _MAPS_DIR
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    (_MAPS_DIR / "lock-map.json").write_text(json.dumps({
        "name": "Lock Map", "world_id": seed.world_a.id, "width": 2000, "height": 1500, "markers": [],
    }), encoding="utf-8")

    original_execute = Session.execute

    def failing_execute(self, statement, *a, **kw):
        if "BEGIN IMMEDIATE" in str(statement):
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        return original_execute(self, statement, *a, **kw)

    monkeypatch.setattr(Session, "execute", failing_execute)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "map_overlay", {"custom_markers": [{"lat": 1, "lng": 1}]}, {"map_slug": "lock-map"})
    assert r.status_code == 400
    (_MAPS_DIR / "lock-map.json").unlink()


# ── schematic_elements import: cross-world + canvas bounds ──────────────────

def test_schematic_elements_import_rejects_cross_world_schematic(client, seed):
    s = _make_schematic(seed.world_b.id, "b-only-schem", elements=[{"id": "orig", "type": "rect"}])

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "schematic_elements",
                         [{"id": "hijack", "type": "rect"}],
                         {"schematic_slug": s.slug})
    assert r.status_code == 400

    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        assert json.loads(fresh.elements_json) == [{"id": "orig", "type": "rect"}]
    finally:
        db.close()


def test_schematic_elements_import_rejects_nonexistent_schematic(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "schematic_elements", [{"id": "x", "type": "rect"}], {"schematic_slug": "nope"})
    assert r.status_code == 400


def test_schematic_elements_import_new_schematic_rejects_non_numeric_canvas(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "schematic_elements", [{"id": "x", "type": "rect"}],
                         {"schematic_slug": "__new__", "new_canvas_width": "abc"})
    assert r.status_code == 400


def test_schematic_elements_import_new_schematic_rejects_out_of_bounds_canvas(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # Not 0: `int(params.get("new_canvas_width") or 2000)` treats a falsy 0
    # the same as "not provided" and silently falls back to the 2000
    # default (pre-existing behavior, same as new_schematic_name's `or
    # "Imported Schematic"` fallback — not something this fix changes).
    # A negative value is truthy, so it reaches the bounds check as-is.
    r = _import_execute(client, "schematic_elements", [{"id": "x", "type": "rect"}],
                         {"schematic_slug": "__new__", "new_canvas_width": -50})
    assert r.status_code == 400
    r2 = _import_execute(client, "schematic_elements", [{"id": "x", "type": "rect"}],
                          {"schematic_slug": "__new__", "new_canvas_width": 999999})
    assert r2.status_code == 400


def test_schematic_elements_import_new_schematic_happy_path(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "schematic_elements", [{"id": "x", "type": "rect"}],
                         {"schematic_slug": "__new__", "new_schematic_name": "From Import"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        s = db.query(Schematic).filter(Schematic.name == "From Import").first()
        assert s is not None
        assert s.world_id == seed.world_a.id
        assert json.loads(s.elements_json) == [{"id": "x", "type": "rect"}]
    finally:
        db.close()


def test_schematic_elements_import_lock_contention_returns_clean_error(client, seed, monkeypatch):
    s = _make_schematic(seed.world_a.id, "lock-schem", elements=[{"id": "orig", "type": "rect"}])

    original_execute = Session.execute

    def failing_execute(self, statement, *a, **kw):
        if "BEGIN IMMEDIATE" in str(statement):
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        return original_execute(self, statement, *a, **kw)

    monkeypatch.setattr(Session, "execute", failing_execute)

    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = _import_execute(client, "schematic_elements", [{"id": "new", "type": "rect"}], {"schematic_slug": s.slug})
    assert r.status_code == 400


def test_concurrent_schematic_elements_import_and_move_token_dont_clobber(client, seed, monkeypatch):
    """The core reliability claim for this batch: schematic_elements import's
    BEGIN IMMEDIATE lock must serialize it against a concurrent move-token on
    the same schematic, so neither write silently discards the other's —
    same deterministic-contention technique as
    test_schematic_combat_sync.py's concurrent pull-combat/move-token test:
    gate the first request mid-transaction (already holding SQLite's
    RESERVED lock) so the second's own BEGIN IMMEDIATE genuinely blocks on
    real lock contention rather than thread-scheduling luck.
    """
    pc = _make_pc(seed.world_a.id, seed.player_a.id)
    elements = [{"id": "tok1", "type": "token", "pc_id": pc.id, "x": 10, "y": 10, "visible_to_players": True}]
    s = _make_schematic(seed.world_a.id, "race-import", elements=elements)

    ready = threading.Event()
    release = threading.Event()
    calls = []
    original_dumps = json.dumps

    def gated_dumps(obj, *a, **kw):
        # execute_import's schematic_elements branch writes the merged
        # [tok1, wall1] list — pause there, after it has already read the
        # existing elements but before it commits, so move-token's own
        # BEGIN IMMEDIATE has something to block on.
        if (isinstance(obj, list) and obj and obj[0].get("id") == "tok1"
                and any(e.get("id") == "wall1" for e in obj) and len(calls) == 0):
            calls.append(1)
            ready.set()
            assert release.wait(timeout=5), "test deadlocked waiting to release the import"
        return original_dumps(obj, *a, **kw)

    monkeypatch.setattr(importer_module.json, "dumps", gated_dumps)

    results = {}

    def do_import():
        db = SessionLocal()
        try:
            world = db.query(World).filter(World.id == seed.world_a.id).first()
            ok, result = importer_module.execute_import(
                db, world, "schematic_elements",
                [{"id": "wall1", "type": "rect", "x": 0, "y": 0, "w": 10, "h": 10}],
                {"schematic_slug": s.slug},
            )
            results["import"] = (200 if ok else 400, result)
        finally:
            db.close()

    def do_move():
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == seed.player_a.email).first()
            req = _FakeRequest(user, {"token_id": "tok1", "x": 99, "y": 99})
            try:
                body = asyncio.run(main_module.schematic_move_own_token(s.slug, req, db))
                results["move"] = (200, body)
            except HTTPException as e:
                results["move"] = (e.status_code, None)
        finally:
            db.close()

    t_import = threading.Thread(target=do_import)
    t_import.start()
    assert ready.wait(timeout=5), "import never reached the locked section"

    t_move = threading.Thread(target=do_move)
    t_move.start()
    t_move.join(timeout=1)  # let move-token's own BEGIN IMMEDIATE actually attempt + block
    release.set()
    t_import.join(timeout=5)
    t_move.join(timeout=5)

    assert results["import"][0] == 200
    assert results["move"][0] == 200

    db = SessionLocal()
    try:
        fresh = db.query(Schematic).filter(Schematic.id == s.id).first()
        final_elements = json.loads(fresh.elements_json)
        tok = next(e for e in final_elements if e["id"] == "tok1")
        # move-token ran after the import committed (serialized by the
        # lock), so its write must survive rather than being clobbered.
        assert (tok["x"], tok["y"]) == (99, 99)
        # And the import's new element must also still be present — proving
        # move-token's write didn't itself clobber the import.
        assert any(e.get("id") == "wall1" for e in final_elements)
    finally:
        db.close()


# ── /api/import/execute: single-item exception handling + batch cap ─────────

def test_import_execute_single_item_commit_error_returns_400_not_500(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    # subtype as a list — Entity.subtype is String(64); `ent.get("subtype")
    # or None` passes the list through unchanged, so this dies at db.commit()
    # (sqlite3 can't bind a list) if not caught.
    r = _import_execute(client, "entity_single",
                         {"kind": "character", "name": "Bob", "subtype": ["a", "b"]})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    r.json()  # must not raise


def test_batch_import_rejects_too_many_items(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    items = [{"kind": "entity_single", "data": {"kind": "character", "name": f"NPC {i}"}}
             for i in range(importer_module._MAX_BATCH_IMPORT_ITEMS + 1)]
    r = client.post("/api/import/execute", json={
        "json_text": json.dumps({"imports": items}), "kind": "batch",
    })
    assert r.status_code == 400


def test_batch_import_accepts_items_within_cap(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    items = [{"kind": "entity_single", "data": {"kind": "character", "name": "Solo NPC"}}]
    r = client.post("/api/import/execute", json={
        "json_text": json.dumps({"imports": items}), "kind": "batch",
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["ok"] is True


# ── Legacy POST /api/import (used by import_chronicles.py / import_lore.py) ─

def test_api_import_rejects_missing_name_not_500(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={"world_id": seed.world_a.id, "entities": [{"kind": "character"}]})
    assert r.status_code == 400
    r.json()  # must not raise — the old bug returned a non-JSON body


def test_api_import_rejects_missing_kind_not_500(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={"world_id": seed.world_a.id, "entities": [{"name": "Bob"}]})
    assert r.status_code == 400
    r.json()


def test_api_import_rejects_invalid_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={
        "world_id": seed.world_a.id, "entities": [{"name": "Bob", "kind": "bogus-kind"}],
    })
    assert r.status_code == 400


def test_api_import_rejects_nonexistent_world_id(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={
        "world_id": 999999, "entities": [{"name": "Bob", "kind": "character"}],
    })
    assert r.status_code == 400


def test_api_import_rejects_too_many_entities(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    entities = [{"name": f"NPC {i}", "kind": "character"} for i in range(main_module._MAX_LEGACY_IMPORT_ENTITIES + 1)]
    r = client.post("/api/import", json={"world_id": seed.world_a.id, "entities": entities})
    assert r.status_code == 400


def test_api_import_coerces_non_string_tags_without_crash(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={
        "world_id": seed.world_a.id,
        "entities": [{"name": "Tagged NPC", "kind": "character", "tags": ["a", "b"]}],
    })
    assert r.status_code == 200
    assert r.json()["created"] == 1


def test_api_import_still_dedupes_existing_entity_by_name_and_kind(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r1 = client.post("/api/import", json={
        "world_id": seed.world_a.id,
        "entities": [{"name": "Repeat NPC", "kind": "character", "summary": "first"}],
    })
    assert r1.status_code == 200
    assert r1.json()["created"] == 1
    r2 = client.post("/api/import", json={
        "world_id": seed.world_a.id,
        "entities": [{"name": "Repeat NPC", "kind": "character", "summary": "second"}],
    })
    assert r2.status_code == 200
    assert r2.json()["created"] == 0  # deduped, not re-created

    db = SessionLocal()
    try:
        count = db.query(Entity).filter(Entity.name == "Repeat NPC", Entity.world_id == seed.world_a.id).count()
        assert count == 1
        ent = db.query(Entity).filter(Entity.name == "Repeat NPC", Entity.world_id == seed.world_a.id).first()
        assert ent.summary == "second"  # existing-entity update path still applies
    finally:
        db.close()


def test_api_import_defaults_world_id_to_1_when_omitted(client, seed):
    """import_lore.py's exact call shape: no world_id at all. Seed a
    World(id=1, ...) explicitly rather than relying on fixture ordering
    happening to land world_a at id 1."""
    db = SessionLocal()
    try:
        if not db.query(World).filter(World.id == 1).first():
            db.add(World(id=1, name="Default World", slug="default-world"))
            db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/api/import", json={"entities": [{"name": "Unworlded NPC", "kind": "character"}]})
    assert r.status_code == 200
    assert r.json()["created"] == 1

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.name == "Unworlded NPC").first()
        assert ent.world_id == 1
    finally:
        db.close()


def test_api_import_requires_gm(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/api/import", json={
        "world_id": seed.world_a.id, "entities": [{"name": "Bob", "kind": "character"}],
    })
    assert r.status_code == 403


# ── Source-level regression tests for app/templates/import.html ─────────────

def test_import_page_invalidates_stale_detection_on_textarea_edit(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    idx = r.text.index("json-input').addEventListener('input'")
    body = r.text[idx:idx + 300]
    assert "lastDetected = null" in body
    assert "lastData = null" in body
    assert "disableImport()" in body


def test_import_page_all_fetch_handlers_use_parseJsonResponse(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert r.text.count("parseJsonResponse(res)") >= 3


def test_import_page_buttons_disable_while_inflight(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "analyzeBtn.disabled = true" in r.text
    assert "importBtn.disabled = true" in r.text
    assert "imgBtn.disabled = true" in r.text


def test_import_page_fuzzy_match_uses_word_boundaries_not_raw_substring(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "en.includes(norm) || norm.includes(en)" not in r.text
    assert "normWords" in r.text and "enWords" in r.text


def test_import_page_fuzzy_match_is_prefix_tolerant_not_exact(client, seed):
    """Regression guard (source-level — no JS runtime in this test suite):
    word-boundary matching (the fix locked in by the test above) requires
    each side's word-set to be exactly contained in the other's, which broke
    on real bulk-import filenames combining a numeric ID prefix + entity
    name + descriptive suffix with minor spelling/gender-suffix variance
    from the entity name itself (e.g. filename word "Minotaura" vs. entity
    name word "Minotaur") — every file in a 100-file batch missed, confirmed
    live via Playwright against the actual matching function. wordsMatch()
    restores prefix tolerance for words >=4 chars with <=3 chars of drift,
    while the strict-equality Inn/Skinner guard (word-level, not raw
    substring — closed by the fix above and unaffected by this one) still
    holds, confirmed live: "Skinner.png" still doesn't match entity "Inn"."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "function wordsMatch(a, b)" in r.text
    assert "longer.startsWith(shorter)" in r.text
    assert "normWords.some(nw => wordsMatch(w, nw))" in r.text


def test_import_page_fuzzy_match_tolerates_dropped_connector_words(client, seed):
    """Regression guard (source-level — no JS runtime in this test suite):
    art-pack filenames routinely drop connector words ("the", "of", "a")
    that appear in a verbose entity name — e.g. entity "Raven of the
    Gallows Wind" vs. filename "Ravens of Gallows Winda" (note: "the" is
    simply gone, "of" is kept). Requiring every entity word verbatim in the
    filename made these miss entirely even with prefix-tolerant wordsMatch,
    confirmed against the exact filenames from a real live-site batch.
    Skipping only these specific stopwords (not any arbitrary missing word —
    that would risk cross-matching similarly-named entity variants, e.g.
    "Bronze War Golem" against art meant for a differently-suffixed "Bronze
    War ___") closes the gap without reopening the Inn/Skinner false
    positive, which the test above already locks in stays closed."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "MATCH_STOPWORDS" in r.text
    assert "new Set(['the', 'of', 'a', 'an', 'and'])" in r.text
    assert "enWords.every(w => MATCH_STOPWORDS.has(w) || normWords.some(nw => wordsMatch(w, nw)))" in r.text


def test_import_page_normalize_strips_commas(client, seed):
    """normalizeMatchName previously stripped underscores/hyphens/periods to
    spaces but left commas as literal characters glued onto the preceding
    word (e.g. entity "Garmr, Gate-Hound of the Deep" normalized to a first
    word of "garmr," with a trailing comma) — harmless for prefix-tolerant
    matches where the comma happened to fall within the drift allowance, but
    not a real fix. Commas (and other pure separators) are now stripped the
    same way hyphens/underscores already were."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "replace(/[_\\-.,:;]+/g, ' ')" in r.text


def test_import_page_warns_on_duplicate_entity_assignment(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "function updateDuplicateWarnings" in r.text


def test_import_page_revokes_object_urls_on_rerender(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert "revokeObjectURL" in r.text
    assert "imgObjectUrls" in r.text
