import time
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..models import InviteCode, User, World, WorldMembership
from ..templating import templates

router = APIRouter()

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
    request.session["user_id"] = user.id
    return RedirectResponse(next_url, status_code=303)


@router.post("/api/login")
async def api_login(request: Request, db: Session = Depends(get_db)):
    """JSON-friendly login for non-browser clients (e.g. the Android app) that
    can't submit an HTML form. Shares the same session cookie and failed-login
    lockout as the form-based /login above — a JSON client hammering this
    doesn't get a free pass around the throttle."""
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

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

    _rl_clear(key)
    request.session["user_id"] = user.id
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
        if len(password) < 8:
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": "Password must be at least 8 characters.",
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

    request.session["user_id"] = user.id
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
