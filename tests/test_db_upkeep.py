"""Database upkeep (efficiency plan, step 14): optimize, and give free space back after big deletes - never delete rows."""
import sqlite3

from app import db_upkeep


def test_a_mostly_empty_file_is_switched_to_incremental_vacuum_and_shrinks(tmp_path):
    p = str(tmp_path / "w.db")
    con = sqlite3.connect(p)
    con.execute("create table t (x text)")
    con.executemany("insert into t values (?)", [("y" * 2000,) for _ in range(2000)])
    con.execute("create table keep (x text)")
    con.execute("insert into keep values ('still here')")
    con.commit()
    con.execute("delete from t")
    con.commit()
    con.close()
    import os
    before = os.path.getsize(p)
    out = db_upkeep.run_once(p)
    assert out["optimized"] and out["vacuumed"] and out["free_share"] > 0.25
    assert os.path.getsize(p) < before / 4
    con = sqlite3.connect(p)
    assert con.execute("pragma auto_vacuum").fetchone()[0] == 2 and con.execute("select x from keep").fetchone()[0] == "still here"
    con.execute("insert into t values (?)", ("z" * 50000,)); con.commit(); con.execute("delete from t"); con.commit(); con.close()
    again = db_upkeep.run_once(p)
    assert not again["vacuumed"] and again["incremental_pages"] > 0


def test_a_tidy_file_is_only_optimized(tmp_path):
    p = str(tmp_path / "w.db")
    con = sqlite3.connect(p); con.execute("create table t (x)"); con.execute("insert into t values (1)"); con.commit(); con.close()
    out = db_upkeep.run_once(p)
    assert out["optimized"] and not out["vacuumed"]
