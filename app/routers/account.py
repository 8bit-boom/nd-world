"""Self-service account management (display name, password) for any logged-in
user — GM or player. Kept separate from the GM-only /settings page/router:
/settings controls instance-wide config, this controls "my own account."
"""
import time
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..deps import get_world_ctx
from ..models import User
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
            name_error: str = None, password_error: str = None, status_code: int = 200):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("account.html", {
        "request": request, "world": world, "worlds": worlds,
        "user": user, "name_error": name_error, "password_error": password_error,
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
