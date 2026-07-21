from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import auth
from ..database import get_db
from ..models import InviteCode, User, World, WorldMembership

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if request.session.get("user_id"):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse("auth/login.html", {"request": request, "next": next, "error": None})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    next_url = str(form.get("next") or "/")
    user = db.query(User).filter(User.email == email).first()
    if not user or not auth.verify_password(password, user.password_hash):
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "next": next_url, "error": "Incorrect email or password.",
        }, status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse(next_url or "/", status_code=303)


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
        if not user or not auth.verify_password(password, user.password_hash):
            return templates.TemplateResponse("auth/join.html", {
                "request": request, "code": code, "world": invite.world, "error": None,
                "form_error": "Incorrect email or password.",
            }, status_code=400)
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
