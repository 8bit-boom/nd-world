"""Tests for "Save & Add Another" on the new-entity form (app/main.py's
create() route) — the biggest friction in batch NPC/item entry was
re-navigating to /new (re-picking kind/folder) after every single save.
Submitting with save_and_new=1 now redirects back to a blank /new form
with the same kind/folder pre-filled, instead of the new entity's own
detail page."""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login


def test_save_and_new_redirects_to_new_form_with_kind_and_folder(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/new",
        data={"kind": "character", "subtype": "", "name": "Goblin #1", "folder": "Monsters",
              "tags": "", "image_url": "", "summary": "", "body": "", "visibility_mode": "everyone",
              "save_and_new": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/new?")
    assert "kind=character" in r.headers["location"]
    assert "folder=Monsters" in r.headers["location"]

    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.world_id == seed.world_a.id, Entity.name == "Goblin #1").first()
        assert ent is not None
    finally:
        db.close()


def test_plain_save_still_redirects_to_the_new_entity(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(
        "/new",
        data={"kind": "character", "subtype": "", "name": "Goblin #2", "folder": "",
              "tags": "", "image_url": "", "summary": "", "body": "", "visibility_mode": "everyone"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/entity/")


def test_new_form_prefills_from_save_and_new_redirect(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/new?kind=item&folder=Loot")
    assert r.status_code == 200
    assert 'value="Loot"' in r.text
