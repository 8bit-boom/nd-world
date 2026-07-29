"""Authentication and authorization for nd-world.

Session-based auth (via Starlette's cookie SessionMiddleware, configured in main.py)
rather than token-based, since this is a server-rendered Jinja2 app. There is no open
signup: the GM account is bootstrapped from GM_EMAIL/GM_PASSWORD env vars (see
database.py::_seed), and every other account is a player created by redeeming a
GM-issued invite code (see routers/auth.py).
"""

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, WorldMembership, World

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, hex_digest = password_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hex_digest)


def generate_invite_code() -> str:
    return secrets.token_urlsafe(9)  # short, URL-safe, ~12 chars


# A real hash of a throwaway value, used to equalize login response time when the
# submitted email doesn't exist. Without it, `if not user or verify_password(...)`
# short-circuits: an unknown email answers in sub-milliseconds while a known one
# costs 600k PBKDF2 iterations, which is a trivially measurable enumeration oracle.
# Computed once at import (~200ms of startup).
_DUMMY_HASH = hash_password("nd-world-timing-equalizer")


def burn_password_verify() -> None:
    """Spend the same PBKDF2 cost as a real verification, discarding the result."""
    verify_password("wrong", _DUMMY_HASH)


def safe_next_url(candidate: Optional[str], fallback: str = "/") -> str:
    """Clamp a `next=` redirect target to a same-site relative path.

    `next` is supplied by the client (it's a form field on the login page), so an
    unvalidated value turns POST /login into an open redirect. Anything that isn't
    a single-slash-prefixed relative path is rejected — this blocks absolute URLs
    ("https://evil.example") and protocol-relative ones ("//evil.example"), which
    browsers resolve to a foreign origin.
    """
    if not candidate:
        return fallback
    c = candidate.strip()
    if not c.startswith("/") or c.startswith("//") or c.startswith("/\\"):
        return fallback
    return c


# ── Request-scoped current-user helpers ───────────────────────────────────────

def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def require_gm(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_login(request, db)
    if not user.is_gm:
        raise HTTPException(403, "GM access required")
    return user


def user_can_access_world(db: Session, user: Optional[User], world: Optional[World]) -> bool:
    if not user or not world:
        return False
    if user.is_gm:
        return True
    return db.query(WorldMembership).filter(
        WorldMembership.world_id == world.id, WorldMembership.user_id == user.id
    ).first() is not None


def accessible_world_ids(db: Session, user: Optional[User]) -> Optional[set]:
    """World ids a user may access, or None to mean 'all' (GM)."""
    if not user:
        return set()
    if user.is_gm:
        return None
    rows = db.query(WorldMembership.world_id).filter(WorldMembership.user_id == user.id).all()
    return {r[0] for r in rows}
