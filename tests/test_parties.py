"""Tests for the Parties feature (app/routers/parties.py): creating a
party with a name, and renaming one afterward via the party detail page's
edit form — previously the detail page's edit form had no name input at
all, so a party stuck with its create-time name (or the "New Party"
default) could never be renamed through the UI even though the server
route already accepted a `name` field.
"""
from app.database import SessionLocal
from app.models import Party

from .conftest import GM_PASSWORD, login


def _make_party(world_id, name="New Party"):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name=name)
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id
    finally:
        db.close()


def test_party_create_uses_given_name(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/parties/new", data={"name": "The Silver Vanguard"}, follow_redirects=False)
    assert r.status_code == 303
    party_id = int(r.headers["location"].rsplit("/", 1)[-1])
    db = SessionLocal()
    try:
        assert db.get(Party, party_id).name == "The Silver Vanguard"
    finally:
        db.close()


def test_party_detail_edit_form_has_a_name_input(client, seed):
    party_id = _make_party(seed.world_a.id, name="New Party")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{party_id}")
    assert r.status_code == 200
    assert 'name="name"' in r.text
    assert 'value="New Party"' in r.text


def test_party_rename_via_edit_form(client, seed):
    party_id = _make_party(seed.world_a.id, name="New Party")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{party_id}/edit", data={"name": "The Ashfall Company", "notes": "met in a tavern"},
                     follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        party = db.get(Party, party_id)
        assert party.name == "The Ashfall Company"
        assert party.notes == "met in a tavern"
    finally:
        db.close()

    r2 = client.get(f"/parties/{party_id}")
    assert "The Ashfall Company" in r2.text


def test_party_edit_blank_name_keeps_existing_name(client, seed):
    """The route falls back to the existing name rather than blanking it out
    — matches _apply_form-style conventions elsewhere in this app where an
    empty submitted value doesn't clobber a required field."""
    party_id = _make_party(seed.world_a.id, name="Original Name")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/parties/{party_id}/edit", data={"name": "   ", "notes": ""}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(Party, party_id).name == "Original Name"
    finally:
        db.close()
