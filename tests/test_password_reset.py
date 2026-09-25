"""GM-initiated password reset for players/assistants (POST
/worlds/{id}/members/{user_id}/reset-password), and the GM's own
env-var-based recovery path (GM_PASSWORD_RESET, checked in
app/database.py's _seed on every boot). This app has no outbound email, so
neither path uses a mailed link — see the route's own docstring in
app/main.py and _seed's comment in app/database.py.
"""
import re
from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

import app.database as database_module
from app.database import SessionLocal
from app.main import app
from app.models import TrustedDevice, User, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

# The world_edit.html page has several unrelated <code> tags elsewhere (the
# Visual Theme section documents theme_json keys like <code>font_display</code>)
# — anchor on the reveal banner's own text so this doesn't grab one of those
# instead of the actual generated password.
_CODE_RE = re.compile(r"won't be shown again.*?<code[^>]*>([^<]+)</code>", re.DOTALL)


def _get_user(user_id):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def _extract_reset_password(html: str) -> str:
    m = _CODE_RE.search(html)
    assert m, "expected a <code>...</code> block with the new temp password"
    return m.group(1)


def test_gm_resets_player_password(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password")
    assert r.status_code == 200
    assert "won't be shown again" in r.text
    new_password = _extract_reset_password(r.text)
    assert len(new_password) >= 8

    client.get("/logout")
    r_old = client.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"},
                         follow_redirects=False)
    assert r_old.status_code == 400

    r_new = client.post("/login", data={"email": seed.player_a.email, "password": new_password, "next": "/"},
                         follow_redirects=False)
    assert r_new.status_code == 303


def test_reset_password_invalidates_existing_session(client, seed):
    session_a = client
    session_gm = TestClient(app)

    login(session_a, seed.player_a.email, PLAYER_PASSWORD)
    login(session_gm, seed.gm.email, GM_PASSWORD)

    assert session_a.get("/account").status_code == 200

    r = session_gm.post(f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password")
    assert r.status_code == 200

    r_a = session_a.get("/account", follow_redirects=False)
    assert r_a.status_code == 303


def test_reset_password_clears_trusted_devices(client, seed):
    db = SessionLocal()
    try:
        db.add(TrustedDevice(
            user_id=seed.player_a.id, token_hash="deadbeef" * 8,
            expires_at=datetime.utcnow() + timedelta(days=30),
        ))
        db.commit()
    finally:
        db.close()

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password")
    assert r.status_code == 200

    db = SessionLocal()
    try:
        assert db.query(TrustedDevice).filter(TrustedDevice.user_id == seed.player_a.id).count() == 0
    finally:
        db.close()


def test_reset_password_404s_for_user_not_a_member_of_this_world(client, seed):
    """player_b is a member of world_b, not world_a — resetting them via
    world_a's route must 404, same scoping as member_remove/member_set_role."""
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/members/{seed.player_b.id}/reset-password")
    assert r.status_code == 404


def test_player_cannot_reset_anyones_password(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password")
    assert r.status_code == 403


def test_assistant_cannot_reset_anyones_password(client, seed):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password")
    assert r.status_code == 403


def test_world_edit_page_shows_reset_password_button(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    assert f'/worlds/{seed.world_a.id}/members/{seed.player_a.id}/reset-password' in r.text
    assert "Reset password" in r.text


# ── GM_PASSWORD_RESET boot recovery (app/database.py _seed) ────────────────

def test_gm_password_reset_env_var_resets_existing_gm_account(seed, monkeypatch):
    monkeypatch.setenv("GM_EMAIL", seed.gm.email)
    monkeypatch.setenv("GM_PASSWORD_RESET", "brand-new-gm-password-789")
    try:
        database_module._seed()
    finally:
        monkeypatch.delenv("GM_EMAIL", raising=False)
        monkeypatch.delenv("GM_PASSWORD_RESET", raising=False)

    from app import auth
    updated = _get_user(seed.gm.id)
    assert auth.verify_password("brand-new-gm-password-789", updated.password_hash)
    assert not auth.verify_password(GM_PASSWORD, updated.password_hash)
    assert updated.session_version == seed.gm.session_version + 1


def test_gm_password_reset_env_var_bumps_session_version_and_clears_trusted_devices(seed, monkeypatch):
    db = SessionLocal()
    try:
        db.add(TrustedDevice(
            user_id=seed.gm.id, token_hash="cafebabe" * 8,
            expires_at=datetime.utcnow() + timedelta(days=30),
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setenv("GM_EMAIL", seed.gm.email)
    monkeypatch.setenv("GM_PASSWORD_RESET", "another-new-gm-password-000")
    try:
        database_module._seed()
    finally:
        monkeypatch.delenv("GM_EMAIL", raising=False)
        monkeypatch.delenv("GM_PASSWORD_RESET", raising=False)

    db = SessionLocal()
    try:
        assert db.query(TrustedDevice).filter(TrustedDevice.user_id == seed.gm.id).count() == 0
    finally:
        db.close()


def test_gm_password_reset_env_var_noop_without_matching_gm_email(seed, monkeypatch):
    """A typo'd GM_EMAIL (or one that doesn't match any GM account) must not
    silently reset the wrong account, or any account at all."""
    monkeypatch.setenv("GM_EMAIL", "not-the-real-gm@test.local")
    monkeypatch.setenv("GM_PASSWORD_RESET", "should-never-apply-000")
    try:
        database_module._seed()
    finally:
        monkeypatch.delenv("GM_EMAIL", raising=False)
        monkeypatch.delenv("GM_PASSWORD_RESET", raising=False)

    unchanged = _get_user(seed.gm.id)
    assert unchanged.password_hash == seed.gm.password_hash


def test_gm_password_reset_env_var_noop_when_unset(seed, monkeypatch):
    """GM_PASSWORD_RESET absent (the normal case after every boot but the
    one right after using it) must never touch the password."""
    monkeypatch.setenv("GM_EMAIL", seed.gm.email)
    monkeypatch.delenv("GM_PASSWORD_RESET", raising=False)
    try:
        database_module._seed()
    finally:
        monkeypatch.delenv("GM_EMAIL", raising=False)

    unchanged = _get_user(seed.gm.id)
    assert unchanged.password_hash == seed.gm.password_hash
    assert unchanged.session_version == seed.gm.session_version

