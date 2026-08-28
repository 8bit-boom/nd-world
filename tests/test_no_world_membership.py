"""Regression test: a logged-in user with zero WorldMembership rows must
not 500 on routes that dereference get_active_world()'s result — it
returns None for exactly this case (see main.py's get_active_world), and
home() already guards it (redirects to /worlds), but /search and
/kind/{kind} didn't."""
from app.database import SessionLocal
from app.models import User

from .conftest import PLAYER_PASSWORD, _PLAYER_PASSWORD_HASH, login


def _create_memberless_player():
    db = SessionLocal()
    try:
        u = User(email="lonely-player@test.local", password_hash=_PLAYER_PASSWORD_HASH,
                 display_name="Lonely Player", is_gm=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        return u
    finally:
        db.close()


def test_search_redirects_instead_of_500_with_no_world_memberships(client, seed):
    u = _create_memberless_player()
    login(client, u.email, PLAYER_PASSWORD)
    r = client.get("/search?q=dragon", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/worlds"


def test_kind_list_redirects_instead_of_500_with_no_world_memberships(client, seed):
    u = _create_memberless_player()
    login(client, u.email, PLAYER_PASSWORD)
    r = client.get("/kind/character", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/worlds"
