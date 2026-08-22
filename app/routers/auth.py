import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from .. import totp as _totp
from ..database import get_db
from ..models import InviteCode, TrustedDevice, User, World, WorldMembership
from ..templating import templates

router = APIRouter()

# Duplicated locally rather than imported from main.py — main.py imports this
# router, so the reverse would be circular (same rationale as every router's
# own local copy of a main.py-defined constant elsewhere in this app).
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() == "true"
_TRUST_COOKIE_NAME = "trusted_device"
_TRUST_DAYS = 30

# ── Failed-login throttling ───────────────────────────────────────────────────
# Process-local, which is sufficient because the Dockerfile starts uvicorn with a
# single worker (no --workers flag). If workers are ever added this becomes
# per-worker and must move to the DB or a shared cache to stay effective.
_MAX_ATTEMPTS = 8
_LOCKOUT_SECONDS = 300
_failed_logins: dict = {}  # (ip, email) -> (count, window_started_monotonic)


def _rl_key(request: Request, email: str):
    return ((request.client.host if request.client else "?"), email)


def _rl_lock_remaining(key) -> int:
    """Seconds left on a lockout for this key, or 0 if not locked."""
    rec = _failed_logins.get(key)
    if not rec:
        return 0
    count, started = rec
    if count < _MAX_ATTEMPTS:
        return 0
    elapsed = time.monotonic() - started
    if elapsed >= _LOCKOUT_SECONDS:
        _failed_logins.pop(key, None)
        return 0
    return int(_LOCKOUT_SECONDS - elapsed)


def _rl_record_failure(key) -> None:
    count, started = _failed_logins.get(key, (0, time.monotonic()))
    if time.monotonic() - started >= _LOCKOUT_SECONDS:
        count, started = 0, time.monotonic()  # previous window aged out
    _failed_logins[key] = (count + 1, started)


def _rl_clear(key) -> None:
    _failed_logins.pop(key, None)


# ── Two-step (TOTP) verification-attempt throttling ─────────────────────────
# Separate dict from _failed_logins above: keyed by user id (the account is
# already password-verified at this point — this guards the second factor
# against online brute-forcing of a 6-digit code or a backup code), not
# (ip, email). Same shape/constants as app/routers/account.py's password-
# change throttle.
_failed_2fa: dict = {}  # user_id -> (count, window_started_monotonic)


def _rl_2fa_lock_remaining(user_id: int) -> int:
    rec = _failed_2fa.get(user_id)
    if not rec:
        return 0
    count, started = rec
    if count < _MAX_ATTEMPTS:
        return 0
    elapsed = time.monotonic() - started
    if elapsed >= _LOCKOUT_SECONDS:
        _failed_2fa.pop(user_id, None)
        return 0
    return int(_LOCKOUT_SECONDS - elapsed)


def _rl_2fa_record_failure(user_id: int) -> None:
    count, started = _failed_2fa.get(user_id, (0, time.monotonic()))
    if time.monotonic() - started >= _LOCKOUT_SECONDS:
        count, started = 0, time.monotonic()
    _failed_2fa[user_id] = (count + 1, started)


def _rl_2fa_clear(user_id: int) -> None:
    _failed_2fa.pop(user_id, None)


def _is_device_trusted(request: Request, db: Session, user: User) -> bool:
    """True if the request carries a still-valid "trust this device"
    cookie for `user` specifically — see login_2fa_submit's trust_device
    checkbox. Expired rows are opportunistically cleaned up here rather
    than needing a separate sweep job."""
    raw = request.cookies.get(_TRUST_COOKIE_NAME)
    if not raw:
        return False
    token_hash = _totp.hash_trust_token(raw)
    device = db.query(TrustedDevice).filter(TrustedDevice.token_hash == token_hash).first()
    if not device:
        return False
    if device.expires_at < datetime.utcnow():
        db.delete(device)
        db.commit()
        return False
    return device.user_id == user.id


def _begin_2fa_or_login(request: Request, db: Session, user: User) -> bool:
    """Call once password verification succeeds. If the account has
    two-step auth enabled *and* this isn't a device the user previously
    chose to trust, stashes a *pending* (not-yet-authenticated) marker in
    the session and returns True so the caller redirects to /login/2fa
    instead of finishing login; otherwise establishes the real session
    immediately and returns False, same as before this feature existed."""
    if user.totp_enabled and not _is_device_trusted(request, db, user):
        request.session["pending_2fa_user_id"] = user.id
        return True
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    return False


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    next_url = auth.safe_next_url(next)
    if request.session.get("user_id"):
        return RedirectResponse(next_url, status_code=303)
    return templates.TemplateResponse("auth/login.html", {"request": request, "next": next_url, "error": None})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    next_url = auth.safe_next_url(str(form.get("next") or "/"))

    key = _rl_key(request, email)
    locked_for = _rl_lock_remaining(key)
    if locked_for:
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "next": next_url,
            "error": f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).",
        }, status_code=429)

    user = db.query(User).filter(User.email == email).first()
    if user:
        ok = auth.verify_password(password, user.password_hash)
    else:
        auth.burn_password_verify()  # keep unknown-email timing indistinguishable
        ok = False
    if not ok:
        _rl_record_failure(key)
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "next": next_url, "error": "Incorrect email or password.",
        }, status_code=400)

    _rl_clear(key)
    if _begin_2fa_or_login(request, db, user):
        request.session["pending_2fa_next"] = next_url
        return RedirectResponse("/login/2fa", status_code=303)
    return RedirectResponse(next_url, status_code=303)


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    if not request.session.get("pending_2fa_user_id"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("auth/login_2fa.html", {"request": request, "error": None})


@router.post("/login/2fa")
async def login_2fa_submit(request: Request, db: Session = Depends(get_db)):
    pending_id = request.session.get("pending_2fa_user_id")
    if not pending_id:
        return RedirectResponse("/login", status_code=303)
    user = db.query(User).filter(User.id == pending_id).first()
    if not user or not user.totp_enabled:
        # Account was deleted or 2FA disabled elsewhere mid-flow — nothing
        # valid left to verify against.
        request.session.pop("pending_2fa_user_id", None)
        request.session.pop("pending_2fa_next", None)
        request.session.pop("pending_2fa_invite_code", None)
        return RedirectResponse("/login", status_code=303)

    locked_for = _rl_2fa_lock_remaining(pending_id)
    if locked_for:
        return templates.TemplateResponse("auth/login_2fa.html", {
            "request": request,
            "error": f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).",
        }, status_code=429)

    form = await request.form()
    code = str(form.get("code", "")).strip()

    ok = _totp.verify_code(user.totp_secret, code)
    if not ok:
        ok = _totp.consume_backup_code(user, code)
        if ok:
            db.commit()
    if not ok:
        _rl_2fa_record_failure(pending_id)
        return templates.TemplateResponse("auth/login_2fa.html", {
            "request": request, "error": "Incorrect code. Try again.",
        }, status_code=400)

    _rl_2fa_clear(pending_id)
    next_url = auth.safe_next_url(request.session.pop("pending_2fa_next", "/"))
    pending_invite_code = request.session.pop("pending_2fa_invite_code", None)
    request.session.pop("pending_2fa_user_id", None)

    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version

    resp = None
    if pending_invite_code:
        invite = db.query(InviteCode).filter(InviteCode.code == pending_invite_code).first()
        error = _invite_error(invite)
        if invite and not error:
            resp = _redeem(request, db, invite)
    if resp is None:
        resp = RedirectResponse(next_url, status_code=303)

    if str(form.get("trust_device", "")).strip():
        raw_token = _totp.generate_trust_token()
        db.add(TrustedDevice(
            user_id=user.id, token_hash=_totp.hash_trust_token(raw_token),
            label=request.headers.get("user-agent", "")[:256],
            expires_at=datetime.utcnow() + timedelta(days=_TRUST_DAYS),
        ))
        db.commit()
        resp.set_cookie(
            _TRUST_COOKIE_NAME, raw_token, max_age=_TRUST_DAYS * 24 * 60 * 60,
            httponly=True, secure=_COOKIE_SECURE, samesite="lax",
        )
    return resp


@router.post("/api/login")
async def api_login(request: Request, db: Session = Depends(get_db)):
    """JSON-friendly login for non-browser clients (e.g. the Android app) that
    can't submit an HTML form. Shares the same session cookie and failed-login
    lockout as the form-based /login above — a JSON client hammering this
    doesn't get a free pass around the throttle."""
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    totp_code = str(body.get("totp_code", "")).strip()

    key = _rl_key(request, email)
    locked_for = _rl_lock_remaining(key)
    if locked_for:
        return JSONResponse(
            {"ok": False, "detail": f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s)."},
            status_code=429,
        )

    user = db.query(User).filter(User.email == email).first()
    if user:
        ok = auth.verify_password(password, user.password_hash)
    else:
        auth.burn_password_verify()  # keep unknown-email timing indistinguishable
        ok = False
    if not ok:
        _rl_record_failure(key)
        return JSONResponse({"ok": False, "detail": "Incorrect email or password."}, status_code=401)

    if user.totp_enabled:
        # No separate pending-session round trip here (unlike the form-based
        # /login below) — a JSON client is expected to prompt for the code
        # and resend this same request with totp_code filled in, one extra
        # call rather than a page redirect. Still throttled per-user so a
        # client that already has the password can't hammer the 6-digit code.
        locked_2fa = _rl_2fa_lock_remaining(user.id)
        if locked_2fa:
            return JSONResponse({
                "ok": False, "requires_2fa": True,
                "detail": f"Too many failed attempts. Try again in {locked_2fa // 60 + 1} minute(s).",
            }, status_code=429)
        totp_ok = _totp.verify_code(user.totp_secret, totp_code)
        if not totp_ok:
            totp_ok = _totp.consume_backup_code(user, totp_code)
            if totp_ok:
                db.commit()
        if not totp_ok:
            _rl_2fa_record_failure(user.id)
            detail = "Two-step code required." if not totp_code else "Invalid two-step code."
            return JSONResponse({"ok": False, "requires_2fa": True, "detail": detail}, status_code=401)
        _rl_2fa_clear(user.id)

    _rl_clear(key)
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    return {"ok": True, "user": {"id": user.id, "email": user.email, "is_gm": user.is_gm}}


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/join/{code}", response_class=HTMLResponse)
def join_form(code: str, request: Request, db: Session = Depends(get_db)):
    invite = db.query(InviteCode).filter(InviteCode.code == code).first()
    error = _invite_error(invite)
    world = invite.world if invite else None
    if request.session.get("user_id") and not error:
        # Already logged in — redeem immediately.
        return _redeem(request, db, invite)
    return templates.TemplateResponse("auth/join.html", {
        "request": request, "code": code, "world": world, "error": error, "form_error": None,
    })


@router.post("/join/{code}")
async def join_submit(code: str, request: Request, db: Session = Depends(get_db)):
    invite = db.query(InviteCode).filter(InviteCode.code == code).first()
    error = _invite_error(invite)
    if error:
        return templates.TemplateResponse("auth/join.html", {
            "request": request, "code": code, "world": invite.world if invite else None,
            "error": error, "form_error": None,
        }, status_code=400)

    form = await request.form()
    mode = str(form.get("mode", "signup"))
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))

    if not email or not password:
        return templates.TemplateResponse("auth/join.html", {
            "request": request, "code": code, "world": invite.world, "error": None,
            "form_error": "Email and password are required.",
        }, status_code=400)

    user = db.query(User).filter(User.email == email).first()
    if mode == "login":
        key = _rl_key(request, email)
        locked_for = _rl_lock_remaining(key)
        if locked_for:
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": f"Too many failed attempts. Try again in {locked_for // 60 + 1} minute(s).",
            }, status_code=429)
        if user:
            ok = auth.verify_password(password, user.password_hash)
        else:
            auth.burn_password_verify()  # same timing equalization as POST /login
            ok = False
        if not ok:
            _rl_record_failure(key)
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": "Incorrect email or password.",
            }, status_code=400)
        _rl_clear(key)
    else:
        if user:
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": "An account with that email already exists — log in instead.",
            }, status_code=400)
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.",
            }, status_code=400)
        user = User(
            email=email,
            password_hash=auth.hash_password(password),
            display_name=str(form.get("display_name", "")).strip() or email.split("@")[0],
            is_gm=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    if _begin_2fa_or_login(request, db, user):
        request.session["pending_2fa_invite_code"] = code
        return RedirectResponse("/login/2fa", status_code=303)
    return _redeem(request, db, invite)


def _invite_error(invite):
    if not invite:
        return "This invite link is invalid."
    if invite.revoked:
        return "This invite link has been revoked."
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        return "This invite link has expired."
    if invite.max_uses is not None and invite.uses_count >= invite.max_uses:
        return "This invite link has already been used up."
    return None


def _redeem(request: Request, db: Session, invite: InviteCode):
    user_id = request.session.get("user_id")
    user = db.query(User).filter(User.id == user_id).first()
    if user and not user.is_gm:
        existing = db.query(WorldMembership).filter(
            WorldMembership.world_id == invite.world_id, WorldMembership.user_id == user.id
        ).first()
        if not existing:
            db.add(WorldMembership(world_id=invite.world_id, user_id=user.id))
            invite.uses_count = (invite.uses_count or 0) + 1
            db.commit()
    world = db.query(World).filter(World.id == invite.world_id).first()
    resp = RedirectResponse("/", status_code=303)
    if world:
        resp.set_cookie("active_world", world.slug, max_age=60 * 60 * 24 * 365)
    return resp
