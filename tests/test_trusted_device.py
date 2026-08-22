"""Tests for the "Trust this device for 30 days" 2FA checkbox
(TrustedDevice in app/models.py, app/routers/auth.py's
_is_device_trusted/_begin_2fa_or_login). Builds on the login-gate
machinery already covered by test_two_step_auth.py — this file only
exercises the new trust-cookie behavior on top of it."""
import re

import pyotp
import pytest

from app.database import SessionLocal
from app.models import TrustedDevice, User
from app.routers import account as account_router
from app.routers import auth as auth_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _reset_throttles():
    """Same rationale as test_two_step_auth.py's fixture of the same name —
    these are process-local dicts that outlive the per-test DB reset."""
    auth_router._failed_logins.clear()
    auth_router._failed_2fa.clear()
    account_router._failed_password_changes.clear()
    yield
    auth_router._failed_logins.clear()
    auth_router._failed_2fa.clear()
    account_router._failed_password_changes.clear()


_SECRET_RE = re.compile(r'<code[^>]*>([A-Z2-7]{16,64})</code>')


def _extract_secret(html):
    m = _SECRET_RE.search(html)
    assert m, "couldn't find TOTP secret in setup page"
    return m.group(1)


def _enable_2fa(client, email, password):
    login(client, email, password)
    r = client.get("/account/2fa/setup")
    secret = _extract_secret(r.text)
    code = pyotp.TOTP(secret).now()
    r2 = client.post("/account/2fa/setup", data={"code": code})
    assert r2.status_code == 200
    return secret


def _trusted_device_count(user_id):
    db = SessionLocal()
    try:
        return db.query(TrustedDevice).filter(TrustedDevice.user_id == user_id).count()
    finally:
        db.close()


def test_trust_checkbox_sets_cookie_and_skips_2fa_on_next_login(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")

    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    r = client.post("/login/2fa", data={"code": code, "trust_device": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "trusted_device" in r.cookies
    assert _trusted_device_count(seed.gm.id) == 1

    client.get("/logout")
    r2 = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/settings"},
                      follow_redirects=False)
    # Trusted this time — straight to the destination, no /login/2fa detour.
    assert r2.status_code == 303
    assert r2.headers["location"] == "/settings"
    assert client.get("/settings").status_code == 200


def test_without_checkbox_still_requires_2fa_next_login(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")

    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    r = client.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303
    assert "trusted_device" not in r.cookies
    assert _trusted_device_count(seed.gm.id) == 0

    client.get("/logout")
    r2 = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                      follow_redirects=False)
    assert r2.headers["location"] == "/login/2fa"


def test_trust_does_not_carry_over_to_a_different_user(client, seed):
    gm_secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(gm_secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})
    client.get("/logout")

    # player_a shares the same browser/cookie jar but is a different account
    # with its own 2FA — the GM's trust must not leak over.
    player_secret = _enable_2fa(client, seed.player_a.email, PLAYER_PASSWORD)
    client.get("/logout")
    r = client.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"},
                     follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"


def test_expired_trusted_device_still_requires_2fa_and_gets_cleaned_up(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})
    assert _trusted_device_count(seed.gm.id) == 1

    from datetime import datetime, timedelta
    db = SessionLocal()
    try:
        device = db.query(TrustedDevice).filter(TrustedDevice.user_id == seed.gm.id).first()
        device.expires_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    client.get("/logout")
    r = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                     follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"
    assert _trusted_device_count(seed.gm.id) == 0  # stale row swept on the failed check


def test_password_change_revokes_trusted_devices(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})
    assert _trusted_device_count(seed.gm.id) == 1

    new_password = "a-brand-new-password-123"
    r = client.post("/account/password", data={
        "current_password": GM_PASSWORD, "new_password": new_password, "confirm_password": new_password,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _trusted_device_count(seed.gm.id) == 0

    client.get("/logout")
    r2 = client.post("/login", data={"email": seed.gm.email, "password": new_password, "next": "/"},
                      follow_redirects=False)
    assert r2.headers["location"] == "/login/2fa"


def test_disabling_2fa_clears_trusted_devices(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})
    assert _trusted_device_count(seed.gm.id) == 1

    r = client.post("/account/2fa/disable", data={"current_password": GM_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert _trusted_device_count(seed.gm.id) == 0


def test_account_page_lists_trusted_device_and_revoke_restores_2fa_prompt(client, seed):
    secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})

    r = client.get("/account")
    assert "Trusted devices" in r.text
    assert "Revoke" in r.text

    db = SessionLocal()
    try:
        device = db.query(TrustedDevice).filter(TrustedDevice.user_id == seed.gm.id).first()
        device_id = device.id
    finally:
        db.close()

    rr = client.post(f"/account/trusted-devices/{device_id}/revoke", follow_redirects=False)
    assert rr.status_code == 303
    assert _trusted_device_count(seed.gm.id) == 0

    client.get("/logout")
    r2 = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                      follow_redirects=False)
    assert r2.headers["location"] == "/login/2fa"


def test_account_trusted_device_revoke_scoped_to_own_devices(client, seed):
    """A player can't revoke another user's trusted device by guessing an id."""
    gm_secret = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    code = pyotp.TOTP(gm_secret).now()
    client.post("/login/2fa", data={"code": code, "trust_device": "1"})
    db = SessionLocal()
    try:
        gm_device_id = db.query(TrustedDevice).filter(TrustedDevice.user_id == seed.gm.id).first().id
    finally:
        db.close()
    client.get("/logout")

    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.post(f"/account/trusted-devices/{gm_device_id}/revoke")
    assert _trusted_device_count(seed.gm.id) == 1  # untouched
