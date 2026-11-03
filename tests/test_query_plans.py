"""The hot queries of the entity page use an index (no full table scan): the backlinks of an entity and a player's visible entities. A table that grows with the
world must not be read from start to end on every page (docs/FORK_STATUS.md, speed plan step 6)."""
import sqlite3

import pytest

from app.database import engine

PLANS = {
    "backlinks": "SELECT e.* FROM entities e JOIN entity_links l ON l.source_id = e.id WHERE l.target_id = 5",
    "outlinks": "SELECT e.* FROM entities e WHERE e.id IN (SELECT target_id FROM entity_links WHERE source_id = 5)",
    "player access": "SELECT entity_id FROM entity_player_access WHERE user_id = 2",
    "notes of an entity": "SELECT * FROM entity_notes WHERE entity_id = 5 ORDER BY created_at DESC",
    "list of a kind": "SELECT * FROM entities WHERE world_id = 1 AND kind = 'character' ORDER BY name",
    "a folder": "SELECT * FROM entities WHERE world_id = 1 AND folder = 'NPCs'",
}


@pytest.mark.parametrize("name", list(PLANS))
def test_the_hot_queries_use_an_index(client, name):
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        plan = [r[3] for r in raw.execute("EXPLAIN QUERY PLAN " + PLANS[name]).fetchall()]
    scans = [p for p in plan if p.startswith("SCAN") and "USING" not in p]
    assert not scans, (name, plan)
