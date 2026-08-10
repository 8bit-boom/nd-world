"""Tests for the MCP access-token management UI on /account (see
app/routers/account.py's account_token_new/account_token_revoke)."""
from app.database import SessionLocal
from app.models import ApiToken

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def test_any_logged_in_user_can_generate_a_token(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post("/account/tokens/new", data={"label": "My Phone"})
    assert r.status_code == 200
    assert "My Phone" in r.text
    # The raw token is shown once in the response body.
    db = SessionLocal()
    try:
        tokens = db.query(ApiToken).filter(ApiToken.user_id == seed.player_a.id).all()
        assert len(tokens) == 1
        assert tokens[0].label == "My Phone"
    finally:
        db.close()


def test_token_defaults_to_unlabeled(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/account/tokens/new", data={"label": "   "})
    db = SessionLocal()
    try:
        token = db.query(ApiToken).filter(ApiToken.user_id == seed.gm.id).first()
        assert token.label == "Unlabeled token"
    finally:
        db.close()


def test_user_can_revoke_own_token(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/account/tokens/new", data={"label": "temp"})
    db = SessionLocal()
    try:
        token_id = db.query(ApiToken).filter(ApiToken.user_id == seed.gm.id).first().id
    finally:
        db.close()

    r = client.post(f"/account/tokens/{token_id}/revoke", follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        assert db.get(ApiToken, token_id) is None
    finally:
        db.close()


def test_user_cannot_revoke_someone_elses_token(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.post("/account/tokens/new", data={"label": "gm token"})
    db = SessionLocal()
    try:
        token_id = db.query(ApiToken).filter(ApiToken.user_id == seed.gm.id).first().id
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.post(f"/account/tokens/{token_id}/revoke", follow_redirects=False)

    db = SessionLocal()
    try:
        assert db.get(ApiToken, token_id) is not None
    finally:
        db.close()
