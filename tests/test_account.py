"""/account is the self-service page every logged-in user (GM or player) can
reach — unlike /settings, which stays GM-only. Covers display-name updates,
password changes (including the failed-attempt throttle), and that a password
change invalidates every *other* session for that user while leaving the
session that made the change intact.
"""
import pytest
from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import User

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _reset_password_change_throttle():
    from app.routers import account as account_router
    account_router._failed_password_changes.clear()
    yield
    account_router._failed_password_changes.clear()


def _get_user(user_id):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def test_player_can_reach_account_page(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.get("/account")
    assert r.status_code == 200
    # Contrast: the same player is blocked from the GM-only settings page.
    r2 = client.get("/settings")
    assert r2.status_code == 403


def test_gm_can_reach_account_page(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/account")
    assert r.status_code == 200


def test_update_display_name(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/name", data={"display_name": "New Name"}, follow_redirects=False)
    assert r.status_code == 303
    assert _get_user(seed.player_a.id).display_name == "New Name"


def test_update_display_name_blank_rejected(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/name", data={"display_name": "   "})
    assert r.status_code == 400
    assert _get_user(seed.player_a.id).display_name == "Player A"


def test_change_password_success_and_relogin(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/password", data={
        "current_password": PLAYER_PASSWORD,
        "new_password": "brand-new-pass-456",
        "confirm_password": "brand-new-pass-456",
    }, follow_redirects=False)
    assert r.status_code == 303

    client.get("/logout")
    r_old = client.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"},
                         follow_redirects=False)
    assert r_old.status_code == 400
    r_new = client.post("/login", data={"email": seed.player_a.email, "password": "brand-new-pass-456", "next": "/"},
                         follow_redirects=False)
    assert r_new.status_code == 303


def test_change_password_wrong_current_rejected(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/password", data={
        "current_password": "totally-wrong",
        "new_password": "brand-new-pass-456",
        "confirm_password": "brand-new-pass-456",
    })
    assert r.status_code == 400

    client.get("/logout")
    r_old = client.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"},
                         follow_redirects=False)
    assert r_old.status_code == 303


def test_change_password_mismatch_rejected_and_not_rate_limited(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/password", data={
        "current_password": PLAYER_PASSWORD,
        "new_password": "brand-new-pass-456",
        "confirm_password": "does-not-match",
    })
    assert r.status_code == 400

    from app.routers import account as account_router
    assert seed.player_a.id not in account_router._failed_password_changes


def test_change_password_too_short_rejected(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/password", data={
        "current_password": PLAYER_PASSWORD,
        "new_password": "short",
        "confirm_password": "short",
    })
    assert r.status_code == 400


def test_change_password_lockout(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    for _ in range(8):
        r = client.post("/account/password", data={
            "current_password": "wrong", "new_password": "brand-new-pass-456", "confirm_password": "brand-new-pass-456",
        })
        assert r.status_code == 400
    r = client.post("/account/password", data={
        "current_password": PLAYER_PASSWORD, "new_password": "brand-new-pass-456", "confirm_password": "brand-new-pass-456",
    })
    assert r.status_code == 429

    # A different user's own password-change attempts are unaffected — proves the
    # throttle dict is keyed by user_id, not a shared/global counter.
    login(client, seed.player_b.email, PLAYER_PASSWORD)
    r_other = client.post("/account/password", data={
        "current_password": "wrong", "new_password": "brand-new-pass-456", "confirm_password": "brand-new-pass-456",
    })
    assert r_other.status_code == 400


def test_password_change_invalidates_other_sessions_but_not_this_one(client, seed):
    session_a = client
    session_b = TestClient(app)

    login(session_a, seed.player_a.email, PLAYER_PASSWORD)
    login(session_b, seed.player_a.email, PLAYER_PASSWORD)

    r = session_a.post("/account/password", data={
        "current_password": PLAYER_PASSWORD,
        "new_password": "brand-new-pass-456",
        "confirm_password": "brand-new-pass-456",
    }, follow_redirects=False)
    assert r.status_code == 303

    # The session that made the change stays logged in.
    assert session_a.get("/account").status_code == 200

    # The other session is now invalidated.
    r_b = session_b.get("/account", follow_redirects=False)
    assert r_b.status_code == 303
    assert r_b.headers["location"].startswith("/login")
