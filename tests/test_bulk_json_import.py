"""Tests for the Bulk JSON Import section of app/templates/import.html: select
several .json files at once, each analyzed and imported independently by
reusing the existing /api/import/detect and /api/import/execute endpoints —
no new backend route. Mirrors tests/test_bulk_image_import.py's established
source-string-regression style for the frontend half (no JS runtime in this
suite) and drives the real endpoints for the functional half, the same way a
sequential per-file fetch loop in the browser would.
"""
import json

from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_import_page_has_bulk_json_import_section(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/import")
    assert r.status_code == 200
    assert 'id="bulk-json-files-input"' in r.text
    assert "multiple" in r.text.split('id="bulk-json-files-input"')[1].split(">")[0]
    assert "async function analyzeBulkJsonFiles()" in r.text
    assert "/api/import/detect" in r.text
    assert "/api/import/execute" in r.text
    # Reuses the same defensive response parser as the single-file and
    # bulk-image flows, not a fresh ad-hoc res.json() call.
    assert r.text.count("parseJsonResponse(res)") >= 3
    assert "const BULK_JSON_MAX_FILES" in r.text


def test_bulk_json_import_two_entity_files_sequentially(client, seed):
    """Simulates what the browser's per-file loop does: detect, then
    execute, for each file in turn — proving the reused endpoints actually
    support being called this way for a real multi-file batch."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    file_a = [{"kind": "race", "name": "Bulk Test Race A", "body": "Race A body"}]
    file_b = [{"kind": "note", "name": "Bulk Test Note B", "subtype": "lore", "body": "Note B body"}]

    for payload in (file_a, file_b):
        text = json.dumps(payload)
        detect_r = client.post("/api/import/detect", json={"json_text": text})
        assert detect_r.status_code == 200
        detected = detect_r.json()
        assert detected["kind"] == "entity_bulk"
        assert detected["needs"] == []

        exec_r = client.post("/api/import/execute", json={"json_text": text, "kind": detected["kind"], "params": {}})
        assert exec_r.status_code == 200
        assert exec_r.json()["ok"] is True

    db = SessionLocal()
    try:
        assert db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.name == "Bulk Test Race A", Entity.kind == "race"
        ).first() is not None
        assert db.query(Entity).filter(
            Entity.world_id == seed.world_a.id, Entity.name == "Bulk Test Note B", Entity.kind == "note"
        ).first() is not None
    finally:
        db.close()


def test_bulk_json_import_one_bad_file_does_not_block_detect_of_another(client, seed):
    """A file with invalid JSON, or a kind /api/import/detect can't place,
    is a per-file concern in the bulk flow — it must not prevent a
    different, valid file's own detect/execute calls (each file is an
    independent request pair, not a shared transaction)."""
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    bad_r = client.post("/api/import/detect", json={"json_text": "{not valid json"})
    # The route itself may 400 on unparseable JSON, or return an "unknown"/
    # "invalid" kind — either way, it must respond, not crash.
    assert bad_r.status_code in (200, 400)

    good_payload = [{"kind": "item", "name": "Bulk Test Item", "body": "Item body"}]
    good_r = client.post("/api/import/detect", json={"json_text": json.dumps(good_payload)})
    assert good_r.status_code == 200
    assert good_r.json()["kind"] == "entity_bulk"


def test_bulk_json_import_is_gm_only(client, seed):
    """The underlying /api/import/detect and /api/import/execute endpoints
    the bulk flow calls per-file are already GM-only — the bulk UI doesn't
    add a new privilege boundary, it just calls them more than once."""
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    payload = [{"kind": "race", "name": "Player Should Not Import This", "body": "x"}]
    r = client.post("/api/import/execute", json={"json_text": json.dumps(payload), "kind": "entity_bulk", "params": {}})
    assert r.status_code == 403
