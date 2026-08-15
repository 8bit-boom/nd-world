"""Self-service account management (display name, password, two-step auth)
for any logged-in user — GM or player. Kept separate from the GM-only
/settings page/router: /settings controls instance-wide config, this
controls "my own account."
"""
import json
import time
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from .. import totp as _totp
from ..database import get_db
from ..deps import get_world_ctx
from ..models import ApiToken, User
from ..templating import templates

router = APIRouter()

# ── Failed-password-change throttling ───────────────────────────────────────────
# Keyed by user_id, not (ip, email) like the login throttle in routers/auth.py: the
# caller here is already authenticated, so there's no anonymous-identity/enumeration
# concern to protect against, and user_id is stable and unspoofable (unlike an IP,
# which changes across shared NAT/VPN). Only a wrong *current password* increments
# this — a confirm-mismatch or too-short new password is a typo, not a guess.
_MAX_ATTEMPTS = 8
_LOCKOUT_SECONDS = 300
_failed_password_changes: dict = {}  # user_id -> (count, window_started_monotonic)


def _rl_lock_remaining(user_id: int) -> int:
    rec = _failed_password_changes.get(user_id)
    if not rec:
        return 0
    count, started = rec
    if count < _MAX_ATTEMPTS:
        return 0
    elapsed = time.monotonic() - started
    if elapsed >= _LOCKOUT_SECONDS:
        _failed_password_changes.pop(user_id, None)
        return 0
    return int(_LOCKOUT_SECONDS - elapsed)


def _rl_record_failure(user_id: int) -> None:
    count, started = _failed_password_changes.get(user_id, (0, time.monotonic()))
    if time.monotonic() - started >= _LOCKOUT_SECONDS:
        count, started = 0, time.monotonic()
    _failed_password_changes[user_id] = (count + 1, started)


def _rl_clear(user_id: int) -> None:
    _failed_password_changes.pop(user_id, None)


def _render(request: Request, db: Session, user: User, active_world: Optional[str],
            name_error: str = None, password_error: str = None, status_code: int = 200,
            new_token: str = None, totp_error: str = None, new_backup_codes: list = None):
    world, worlds = get_world_ctx(request, db, active_world)
    tokens = db.query(ApiToken).filter(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()).all()
    return templates.TemplateResponse("account.html", {
        "request": request, "world": world, "worlds": worlds,
        "user": user, "name_error": name_error, "password_error": password_error,
        "tokens": tokens, "new_token": new_token,
        "totp_error": totp_error, "new_backup_codes": new_backup_codes,
    }, status_code=status_code)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    return _render(request, db, user, active_world)


@router.post("/account/name")
def account_update_name(request: Request, display_name: str = Form(""),
                         db: Session = Depends(get_db),
                         user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    name = display_name.strip()
    if not name:
        return _render(request, db, user, active_world, name_error="Display name can't be blank.", status_code=400)
    if len(name) > 256:
        return _render(request, db, user, active_world, name_error="Display name is too long.", status_code=400)
    user.display_name = name
    db.commit()
    return RedirectResponse("/account?saved=name", status_code=303)


@router.post("/account/password")
def account_change_password(request: Request, current_password: str = Form(""),
                             new_password: str = Form(""), confirm_password: str = Form(""),
                             db: Session = Depends(get_db),
                             user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    locked_for = _rl_lock_remaining(user.id)
    if locked_for:
        return _render(request, db, user, active_world, status_code=429,
                        password_error=f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).")

    if not auth.verify_password(current_password, user.password_hash):
        _rl_record_failure(user.id)
        return _render(request, db, user, active_world, password_error="Current password is incorrect.", status_code=400)

    if len(new_password) < auth.MIN_PASSWORD_LENGTH:
        return _render(request, db, user, active_world,
                        password_error=f"New password must be at least {auth.MIN_PASSWORD_LENGTH} characters.",
                        status_code=400)
    if new_password != confirm_password:
        return _render(request, db, user, active_world, password_error="New passwords don't match.", status_code=400)

    _rl_clear(user.id)
    user.password_hash = auth.hash_password(new_password)
    # Invalidate every other session for this user (see auth_gate in main.py) —
    # bump first, then immediately re-stamp *this* session so the request that
    # just changed the password doesn't log itself out too.
    user.session_version += 1
    db.commit()
    request.session["session_version"] = user.session_version
    return RedirectResponse("/account?saved=password", status_code=303)


# ── Two-step (TOTP) authentication ───────────────────────────────────────────

@router.get("/account/2fa/setup", response_class=HTMLResponse)
def account_2fa_setup(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    if user.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    # Reused across reloads of this page (not regenerated every GET) so a
    # QR code already scanned into an authenticator app doesn't go stale if
    # the user refreshes before entering the confirmation code.
    secret = request.session.get("pending_totp_secret")
    if not secret:
        secret = _totp.generate_secret()
        request.session["pending_totp_secret"] = secret
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("account_2fa_setup.html", {
        "request": request, "world": world, "worlds": worlds, "user": user,
        "secret": secret, "qr_data_uri": _totp.qr_code_data_uri(_totp.provisioning_uri(secret, user.email)),
        "error": None,
    })


@router.post("/account/2fa/setup")
async def account_2fa_confirm(request: Request, code: str = Form(""),
                               db: Session = Depends(get_db),
                               user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    if user.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    secret = request.session.get("pending_totp_secret")
    if not secret:
        return RedirectResponse("/account/2fa/setup", status_code=303)

    if not _totp.verify_code(secret, code):
        world, worlds = get_world_ctx(request, db, active_world)
        return templates.TemplateResponse("account_2fa_setup.html", {
            "request": request, "world": world, "worlds": worlds, "user": user,
            "secret": secret, "qr_data_uri": _totp.qr_code_data_uri(_totp.provisioning_uri(secret, user.email)),
            "error": "Incorrect code — try again.",
        }, status_code=400)

    backup_codes = _totp.generate_backup_codes()
    user.totp_secret = secret
    user.totp_enabled = True
    user.totp_backup_codes_json = json.dumps([_totp.hash_backup_code(c) for c in backup_codes])
    db.commit()
    request.session.pop("pending_totp_secret", None)
    return _render(request, db, user, active_world, new_backup_codes=backup_codes)


@router.post("/account/2fa/disable")
def account_2fa_disable(request: Request, current_password: str = Form(""),
                         db: Session = Depends(get_db),
                         user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    locked_for = _rl_lock_remaining(user.id)
    if locked_for:
        return _render(request, db, user, active_world, status_code=429,
                        totp_error=f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).")
    if not auth.verify_password(current_password, user.password_hash):
        _rl_record_failure(user.id)
        return _render(request, db, user, active_world, totp_error="Current password is incorrect.", status_code=400)
    _rl_clear(user.id)

    user.totp_enabled = False
    user.totp_secret = None
    user.totp_backup_codes_json = "[]"
    db.commit()
    return RedirectResponse("/account?saved=2fa-off", status_code=303)


@router.post("/account/2fa/backup-codes/regenerate")
def account_2fa_regen_backup_codes(request: Request, current_password: str = Form(""),
                                    db: Session = Depends(get_db),
                                    user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    if not user.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    locked_for = _rl_lock_remaining(user.id)
    if locked_for:
        return _render(request, db, user, active_world, status_code=429,
                        totp_error=f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).")
    if not auth.verify_password(current_password, user.password_hash):
        _rl_record_failure(user.id)
        return _render(request, db, user, active_world, totp_error="Current password is incorrect.", status_code=400)
    _rl_clear(user.id)

    backup_codes = _totp.generate_backup_codes()
    user.totp_backup_codes_json = json.dumps([_totp.hash_backup_code(c) for c in backup_codes])
    db.commit()
    return _render(request, db, user, active_world, new_backup_codes=backup_codes)


@router.post("/account/tokens/new")
def account_token_new(request: Request, label: str = Form(""),
                       db: Session = Depends(get_db),
                       user: User = Depends(auth.require_login), active_world: str = Cookie(None)):
    """Issues a new MCP bearer token for this user (see app/mcp_server.py).
    Rendered directly (not a redirect) so the raw token can be shown once in
    this response without ever touching a URL/query string, where it could
    end up in browser history or a server access log."""
    raw = auth.generate_api_token()
    token = ApiToken(user_id=user.id, token_hash=auth.hash_api_token(raw),
                      label=label.strip()[:256] or "Unlabeled token")
    db.add(token)
    db.commit()
    return _render(request, db, user, active_world, new_token=raw)


@router.post("/account/tokens/{token_id}/revoke")
def account_token_revoke(token_id: int, db: Session = Depends(get_db),
                          user: User = Depends(auth.require_login)):
    # Scoped to this user's own tokens — a player can't revoke someone
    # else's token by guessing an id.
    token = db.query(ApiToken).filter(ApiToken.id == token_id, ApiToken.user_id == user.id).first()
    if token:
        db.delete(token)
        db.commit()
    return RedirectResponse("/account", status_code=303)
