"""The viewer's SITE layout (navigation, width, density, theme ...) for every page render, without a database query per request.

The choice lives in `user_view_prefs` (section "site") like every layout; what a page needs on EVERY request is kept in the signed session
cookie instead, written at login and whenever the person changes it, and re-read from the database now and then (so a change made on the
phone reaches the laptop). `for_request` never raises: a missing user, a session-less test render or a bad row all give the defaults.
"""
import json
import time

from . import view_prefs

SESSION_KEY = "site_layout"
REFRESH_SECONDS = 15 * 60


def defaults() -> dict:
    c = view_prefs.clean("site", {})
    return {"options": c["options"], "pins": c["pins"]}


def _from_db(user_id: int) -> dict:
    from .database import SessionLocal
    from .models import UserViewPref
    db = SessionLocal()
    try:
        row = db.query(UserViewPref).filter(UserViewPref.user_id == user_id, UserViewPref.section == "site").first()
        if not row:
            return defaults()
        try:
            c = view_prefs.clean("site", json.loads(row.prefs_json or "{}"))
        except ValueError:
            return defaults()
        return {"options": c["options"], "pins": c["pins"]}
    finally:
        db.close()


def remember(request, user_id: int, cleaned: dict | None = None) -> dict:
    """Put the person's site layout in the session (from `cleaned` - what was just saved - or from the database)."""
    value = {"options": cleaned["options"], "pins": cleaned["pins"]} if cleaned else _from_db(user_id)
    try:
        request.session[SESSION_KEY] = {**value, "at": int(time.time()), "uid": user_id}
    except Exception:       # no session middleware (a bare test render): nothing to remember it in
        pass
    return value


def forget(request) -> None:
    try:
        request.session.pop(SESSION_KEY, None)
    except Exception:
        pass


def for_request(request) -> dict:
    """{"options": {...}, "pins": [...], "signed": bool} for the page being rendered."""
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        return {**defaults(), "signed": False}
    try:
        cached = request.session.get(SESSION_KEY)
    except Exception:
        cached = None
    fresh = (isinstance(cached, dict) and cached.get("uid") == user.id and isinstance(cached.get("options"), dict)
             and time.time() - int(cached.get("at") or 0) < REFRESH_SECONDS)
    value = cached if fresh else remember(request, user.id)
    c = view_prefs.clean("site", {"options": value.get("options"), "pins": value.get("pins")})     # never trust a cookie more than a row
    return {"options": c["options"], "pins": c["pins"], "signed": True}
