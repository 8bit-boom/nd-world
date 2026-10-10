"""Tap-to-roll on the character sheet: stat boxes open a roll sheet, the roll (Stat + d10 + boost) goes into the shared
dice log with a label saying what it was for (app/routers/dice.py `label`)."""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import PLAYER_PASSWORD, login
from .test_character_hub import _as, _pc


def _stats(pc_id, stats):
    db = SessionLocal()
    try:
        db.get(PlayerCharacter, pc_id).stats_json = json.dumps(stats)
        db.commit()
    finally:
        db.close()


def test_a_labelled_roll_is_logged_for_the_table(client, seed):
    _as(client, seed.player_a)
    r = client.post("/api/dice/roll", json={"notation": "1d10+8+2", "label": "Intellect check — Mirabel"})
    assert r.status_code == 200
    d = r.json()
    assert d["label"] == "Intellect check — Mirabel" and d["breakdown"][0]["rolls"][0] in range(1, 11)
    assert d["total"] == d["breakdown"][0]["rolls"][0] + 10
    hist = client.get("/api/dice/history").json()["rolls"]
    assert hist[0]["label"] == "Intellect check — Mirabel"
    assert "Intellect check" in client.get("/dice").text


def test_label_is_cleaned_and_optional(client, seed):
    _as(client, seed.player_a)
    d = client.post("/api/dice/roll", json={"notation": "1d6", "label": "a\x00b\n" + "x" * 200}).json()
    assert "\x00" not in d["label"] and "\n" not in d["label"] and len(d["label"]) <= 80
    assert client.post("/api/dice/roll", json={"notation": "1d6"}).json()["label"] == ""


def test_the_owner_gets_tappable_stats_named_even_when_saved_bare(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, [{"id": "str", "value": 7}, {"id": "int", "value": 4}])    # {id, value} only, like an AI-drafted sheet
    _as(client, seed.player_a)
    html = client.get(f"/characters/{pc}").text
    assert 'data-roll-label="Strength"' in html and 'data-roll-val="7"' in html
    assert 'data-roll-label="Intellect"' in html
    assert 'id="rs"' in html and 'id="rs-go"' in html


def test_someone_who_cannot_manage_the_sheet_cannot_roll_from_it(client, seed):
    pc = _pc(seed.player_a, seed.world_a, "Hero")
    _stats(pc, [{"id": "str", "value": 7}])
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/characters/{pc}")
    assert 'data-roll-label' not in r.text
