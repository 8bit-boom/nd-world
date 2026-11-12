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


def _schema_copy(raw):
    """An empty in-memory database with the real schema (tables and indexes) and NO statistics. The question here is whether the indexes exist; which plan the
    planner picks on the shared test database also depends on what earlier tests left in it and on the statistics `PRAGMA optimize` made from that."""
    mem = sqlite3.connect(":memory:")
    rows = raw.execute("SELECT type, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND type IN ('table', 'index') ORDER BY type DESC").fetchall()
    for _, ddl in rows:
        try:
            mem.execute(ddl)
        except sqlite3.Error:
            pass      # a virtual (full-text) table the plain connection cannot make: none of the queries here use one
    return mem


@pytest.mark.parametrize("name", list(PLANS))
def test_the_hot_queries_use_an_index(client, name):
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        mem = _schema_copy(raw)
        try:
            stats = raw.execute("SELECT tbl, idx, stat FROM sqlite_stat1 WHERE tbl IN ('entity_links', 'entity_player_access')").fetchall()
        except sqlite3.Error:
            stats = "no statistics"
        counts = {t: raw.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("entity_links", "entity_player_access")}
        indexes = [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name IN ('entity_links', 'entity_player_access')")]
    plan = [r[3] for r in mem.execute("EXPLAIN QUERY PLAN " + PLANS[name]).fetchall()]
    scans = [p for p in plan if p.startswith("SCAN") and "USING" not in p]
    # the message names what the shared database held, for a failure that only shows up in a full run
    assert not scans, (name, plan, {"indexes": indexes, "stats": stats, "rows": counts})
