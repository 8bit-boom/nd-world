"""Neon & Dragons Player's Guide: CA <= 0 is Cyberpsychosis.

"If Cyber Adaptivity (CA) <= 0: if Physical > Mental you become a Cyberpsycho; if
Mental > Physical your body fails and you die." The sheet showed CA used vs CA but
never the remainder or the consequence.
"""
import json

from app.database import SessionLocal
from app.models import PlayerCharacter

from .conftest import GM_PASSWORD, login


def _stats(**v):
    base = {k: 1 for k in ("str", "dex", "bod", "per", "wil", "int", "cha", "itu")}
    base.update(v)
    return json.dumps([{"id": k, "value": x} for k, x in base.items()])


def _pc(seed, stats, cyber):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=seed.world_a.id, name="Subject", stats_json=stats, cyberware_json=json.dumps(cyber))
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc.id
    finally:
        db.close()


def _page(client, seed, pid):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    return client.get(f"/characters/{pid}").text


def test_remaining_ca_is_shown(client, seed):
    pid = _pc(seed, _stats(wil=3, bod=3), [{"name": "Optic", "ca_cost": 2}])      # CA 6, used 2
    html = _page(client, seed, pid)
    assert "remaining" in html and ">4<" in html.replace(" ", "")
    assert 'id="ca-warning"' not in html


def test_physical_heavy_character_becomes_a_cyberpsycho(client, seed):
    pid = _pc(seed, _stats(str=6, dex=6, bod=2, wil=1), [{"name": "Arm", "ca_cost": 3}])   # CA 3, used 3, phys > ment
    html = _page(client, seed, pid)
    warning = html.split('id="ca-warning"')[1].split("</div>")[0]
    assert "becomes a Cyberpsycho" in warning and "so the body fails" not in warning


def test_mental_heavy_character_dies(client, seed):
    pid = _pc(seed, _stats(int=6, cha=6, wil=1, bod=1), [{"name": "Link", "ca_cost": 5}])  # CA 2 < used 5, ment > phys
    html = _page(client, seed, pid)
    warning = html.split('id="ca-warning"')[1].split("</div>")[0]
    assert "so the body fails" in warning and "becomes a Cyberpsycho" not in warning


def test_a_cyberware_free_character_gets_no_warning(client, seed):
    pid = _pc(seed, _stats(wil=1, bod=0), [])
    assert 'id="ca-warning"' not in _page(client, seed, pid)


def test_bad_ca_cost_values_do_not_break_the_sheet(client, seed):
    pid = _pc(seed, _stats(wil=3, bod=3), [{"name": "Odd"}, {"name": "Odder", "ca_cost": "two"}, "junk", {"name": "Ok", "ca_cost": "2"}])
    html = _page(client, seed, pid)
    assert "Ok" in html
