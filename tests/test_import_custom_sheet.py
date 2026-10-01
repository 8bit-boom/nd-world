"""Importing a character onto a custom sheet (Hunt in the Moonlight, Asterion, a GM's own)
must not stamp the standard N&D stats and currency on it: those made the character look
like a native N&D sheet everywhere (roster, vitals, combat)."""
import json

import pytest

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import GM_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


def _import(client, seed, row):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/import/execute", json={"kind": "player_character", "json_text": json.dumps(row), "params": {}})
    assert r.status_code == 200, r.text
    return r.json()


def _row(name):
    db = SessionLocal()
    try:
        pc = db.query(PlayerCharacter).filter(PlayerCharacter.name == name).first()
        db.expunge(pc)
        return pc
    finally:
        db.close()


def test_custom_sheet_import_gets_no_default_nd_stats(client, seed):
    _import(client, seed, {"name": "Anders", "sheet_template_id": "hunt-in-the-moonlight",
                            "custom_fields": {"health_current": 4, "health_max": 5}})
    pc = _row("Anders")
    assert pc.sheet_template_id
    assert json.loads(pc.stats_json or "[]") == [], "no N&D stats on a Hunt in the Moonlight character"
    assert json.loads(pc.custom_fields_json)["health_current"] == 4


def test_native_import_still_gets_the_default_spread(client, seed):
    _import(client, seed, {"name": "Plain Joe"})
    assert len(json.loads(_row("Plain Joe").stats_json)) == 8


def test_explicit_stats_on_a_custom_sheet_are_kept(client, seed):
    stats = [{"id": "str", "value": 2}]
    _import(client, seed, {"name": "Odd", "sheet_template_id": "asterion", "stats": stats})
    assert json.loads(_row("Odd").stats_json) == stats
