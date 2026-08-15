"""Tests for optional TOTP-based two-step authentication (app/totp.py):
enabling/disabling from /account/2fa, the login-time second-factor gate
(/login/2fa) across all three login entry points (/login, /api/login,
/join/{code} in "login" mode), backup codes, and rate limiting."""
import re

import pyotp
import pytest

from app.database import SessionLocal
from app.models import InviteCode, User
from app.routers import account as account_router
from app.routers import auth as auth_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


@pytest.fixture(autouse=True)
def _reset_throttles():
    """_failed_logins/_failed_2fa (app/routers/auth.py) and
    _failed_password_changes (app/routers/account.py) are process-local
    dicts that outlive any single test's `client` fixture, since the DB
    reset doesn't touch them — without this, one test's failed attempts
    (the deliberate lockout tests especially) leak into later tests keyed
    on the same user id and make an unrelated login/disable/regenerate
    start 429ing. Same pattern as test_character_sync.py/test_account.py."""
    auth_router._failed_logins.clear()
    auth_router._failed_2fa.clear()
    account_router._failed_password_changes.clear()
    yield
    auth_router._failed_logins.clear()
    auth_router._failed_2fa.clear()
    account_router._failed_password_changes.clear()

_SECRET_RE = re.compile(r'<code[^>]*>([A-Z2-7]{16,64})</code>')
_BACKUP_CODE_RE = re.compile(r'<code style="font-size:\.85rem">([0-9a-f]{10})</code>')


def _extract_secret(html):
    m = _SECRET_RE.search(html)
    assert m, "couldn't find TOTP secret in setup page"
    return m.group(1)


def _extract_backup_codes(html):
    codes = _BACKUP_CODE_RE.findall(html)
    assert codes, "couldn't find backup codes in response"
    return codes


def _enable_2fa(client, email, password):
    """Logs in as the given user and completes the setup flow. Returns
    (secret, backup_codes). Leaves the client logged in as that user."""
    login(client, email, password)
    r = client.get("/account/2fa/setup")
    assert r.status_code == 200
    secret = _extract_secret(r.text)
    code = pyotp.TOTP(secret).now()
    r2 = client.post("/account/2fa/setup", data={"code": code})
    assert r2.status_code == 200
    backup_codes = _extract_backup_codes(r2.text)
    assert len(backup_codes) == 8
    return secret, backup_codes


def _totp_enabled_in_db(user_id):
    db = SessionLocal()
    try:
        return db.get(User, user_id).totp_enabled
    finally:
        db.close()


# ── Off by default ───────────────────────────────────────────────────────────

def test_totp_disabled_by_default(client, seed):
    db = SessionLocal()
    try:
        gm = db.get(User, seed.gm.id)
        assert gm.totp_enabled is False
        assert gm.totp_secret is None
    finally:
        db.close()


def test_login_unaffected_when_2fa_off(client, seed):
    r = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ── Setup ─────────────────────────────────────────────────────────────────────

def test_setup_page_requires_login(client, seed):
    r = client.get("/account/2fa/setup", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_setup_page_shows_qr_and_manual_key(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/account/2fa/setup")
    assert r.status_code == 200
    assert "data:image/png;base64," in r.text
    secret = _extract_secret(r.text)
    assert len(secret) >= 16


def test_setup_reuses_secret_across_reloads(client, seed):
    """A refresh of the setup page before confirming must not invalidate a
    QR code already scanned into an authenticator app."""
    login(client, seed.gm.email, GM_PASSWORD)
    secret1 = _extract_secret(client.get("/account/2fa/setup").text)
    secret2 = _extract_secret(client.get("/account/2fa/setup").text)
    assert secret1 == secret2


def test_setup_confirm_with_wrong_code_does_not_enable(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.get("/account/2fa/setup")
    r = client.post("/account/2fa/setup", data={"code": "000000"})
    assert r.status_code == 400
    assert "Incorrect code" in r.text
    assert _totp_enabled_in_db(seed.gm.id) is False


def test_setup_confirm_with_correct_code_enables_and_shows_backup_codes(client, seed):
    secret, backup_codes = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    assert _totp_enabled_in_db(seed.gm.id) is True
    assert len(set(backup_codes)) == 8  # all distinct

    db = SessionLocal()
    try:
        gm = db.get(User, seed.gm.id)
        assert gm.totp_secret == secret
    finally:
        db.close()


def test_account_page_reflects_enabled_state(client, seed):
    _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/account")
    assert "Enabled" in r.text
    assert "Enable two-step authentication" not in r.text


# ── Login gate (form-based /login) ──────────────────────────────────────────

def test_login_with_2fa_enabled_does_not_finish_until_second_step(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")

    r = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"

    # Not actually authenticated yet — a GM-only page still bounces to /login.
    r2 = client.get("/settings", follow_redirects=False)
    assert r2.status_code in (303, 401)


def test_login_2fa_correct_code_completes_login(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/settings"})

    code = pyotp.TOTP(secret).now()
    r = client.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"

    r2 = client.get("/settings")
    assert r2.status_code == 200


def test_login_2fa_wrong_code_stays_unauthenticated(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})

    r = client.post("/login/2fa", data={"code": "000000"})
    assert r.status_code == 400
    r2 = client.get("/settings", follow_redirects=False)
    assert r2.status_code in (303, 401)


def test_login_2fa_page_redirects_to_login_when_nothing_pending(client, seed):
    r = client.get("/login/2fa", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_2fa_backup_code_works_once(client, seed):
    secret, backup_codes = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})

    used_code = backup_codes[0]
    r = client.post("/login/2fa", data={"code": used_code}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/settings").status_code == 200

    # Reusing the same backup code for a second login must fail.
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    r2 = client.post("/login/2fa", data={"code": used_code})
    assert r2.status_code == 400


def test_login_2fa_rate_limited_after_repeated_failures(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})

    for _ in range(8):
        client.post("/login/2fa", data={"code": "000000"})
    r = client.post("/login/2fa", data={"code": "000000"})
    assert r.status_code == 429
    assert "Too many failed attempts" in r.text


# ── Login gate (JSON /api/login) ────────────────────────────────────────────

def test_api_login_requires_2fa_flag_when_enabled(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    r = client.post("/api/login", json={"email": seed.gm.email, "password": GM_PASSWORD})
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["requires_2fa"] is True


def test_api_login_succeeds_with_correct_totp_code(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    code = pyotp.TOTP(secret).now()
    r = client.post("/api/login", json={"email": seed.gm.email, "password": GM_PASSWORD, "totp_code": code})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_api_login_rejects_wrong_totp_code(client, seed):
    secret, _ = _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    client.get("/logout")
    r = client.post("/api/login", json={"email": seed.gm.email, "password": GM_PASSWORD, "totp_code": "000000"})
    assert r.status_code == 401
    assert r.json()["requires_2fa"] is True


def test_api_login_unaffected_when_2fa_off(client, seed):
    r = client.post("/api/login", json={"email": seed.player_a.email, "password": PLAYER_PASSWORD})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── Login gate via invite "login" mode (/join/{code}) ───────────────────────

def _make_invite(world_id):
    db = SessionLocal()
    try:
        inv = InviteCode(world_id=world_id, code="testinvite2fa")
        db.add(inv)
        db.commit()
        return inv.code
    finally:
        db.close()


def test_join_login_mode_requires_2fa_before_redeeming(client, seed):
    secret, _ = _enable_2fa(client, seed.player_a.email, PLAYER_PASSWORD)
    client.get("/logout")
    code = _make_invite(seed.world_b.id)

    r = client.post(f"/join/{code}", data={"mode": "login", "email": seed.player_a.email, "password": PLAYER_PASSWORD},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"

    # Not redeemed yet — player_a still only belongs to world_a.
    from app.database import SessionLocal as SL
    from app.models import WorldMembership
    db = SL()
    try:
        membership = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert membership is None
    finally:
        db.close()

    totp_code = pyotp.TOTP(secret).now()
    r2 = client.post("/login/2fa", data={"code": totp_code}, follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/"

    db = SL()
    try:
        membership = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert membership is not None
    finally:
        db.close()


# ── Disable ───────────────────────────────────────────────────────────────────

def test_disable_requires_correct_current_password(client, seed):
    _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/account/2fa/disable", data={"current_password": "wrong-password"})
    assert r.status_code == 400
    assert _totp_enabled_in_db(seed.gm.id) is True


def test_disable_clears_secret_and_backup_codes(client, seed):
    _enable_2fa(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/account/2fa/disable", data={"current_password": GM_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        gm = db.get(User, seed.gm.id)
        assert gm.totp_enabled is False
        assert gm.totp_secret is None
        assert gm.totp_backup_codes_json == "[]"
    finally:
        db.close()

    # Logging in now needs only the password again.
    client.get("/logout")
    r2 = client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"},
                      follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/"


# ── Regenerate backup codes ──────────────────────────────────────────────────

def test_regenerate_backup_codes_requires_password_and_replaces_old_codes(client, seed):
    secret, old_codes = _enable_2fa(client, seed.gm.email, GM_PASSWORD)

    bad = client.post("/account/2fa/backup-codes/regenerate", data={"current_password": "wrong"})
    assert bad.status_code == 400

    r = client.post("/account/2fa/backup-codes/regenerate", data={"current_password": GM_PASSWORD})
    assert r.status_code == 200
    new_codes = _extract_backup_codes(r.text)
    assert len(new_codes) == 8
    assert set(new_codes).isdisjoint(set(old_codes))

    # An old code no longer works to log in; a new one does.
    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    assert client.post("/login/2fa", data={"code": old_codes[0]}).status_code == 400

    client.get("/logout")
    client.post("/login", data={"email": seed.gm.email, "password": GM_PASSWORD, "next": "/"})
    r2 = client.post("/login/2fa", data={"code": new_codes[0]}, follow_redirects=False)
    assert r2.status_code == 303


def test_regenerate_requires_2fa_already_enabled(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post("/account/2fa/backup-codes/regenerate", data={"current_password": GM_PASSWORD},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account"


# ── Players can use this too, not just GMs ──────────────────────────────────

def test_player_can_enable_and_use_2fa(client, seed):
    secret, _ = _enable_2fa(client, seed.player_a.email, PLAYER_PASSWORD)
    client.get("/logout")
    r = client.post("/login", data={"email": seed.player_a.email, "password": PLAYER_PASSWORD, "next": "/"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"

    code = pyotp.TOTP(secret).now()
    r2 = client.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert r2.status_code == 303
    assert client.get("/account").status_code == 200
