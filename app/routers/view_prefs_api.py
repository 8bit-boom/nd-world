"""Saved layouts per account: GET /api/view-prefs (all of the signed-in user's choices) and POST /api/view-prefs/{section} (save one).

Open to every signed-in user (a player's layout of a read-only page is theirs to choose); each person only ever reads and writes their own
rows. What is stored is always the result of app.view_prefs.clean, so a hand-made request cannot park junk in the table."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import site_layout, view_prefs
from ..database import get_db
from ..models import SavedView, UserViewPref
from ..deps import parse_json, read_body  # async routes as plain def (docs/LEFTOVERS_PLAN.md, step 4)

router = APIRouter()


def _user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    return user


def saved_for(db: Session, user_id: int) -> dict:
    """{section: cleaned choice} for one user."""
    out = {}
    for row in db.query(UserViewPref).filter(UserViewPref.user_id == user_id).all():
        try:
            out[row.section] = view_prefs.clean(row.section, json.loads(row.prefs_json or "{}"))
        except ValueError:
            continue
    return out


@router.get("/api/view-prefs")
def view_prefs_get(request: Request, db: Session = Depends(get_db)):
    return saved_for(db, _user(request).id)


@router.post("/api/view-prefs/{section}")
def view_prefs_save(section: str, request: Request, db: Session = Depends(get_db), _body: bytes = Depends(read_body)):
    user = _user(request)
    if section not in view_prefs.SECTIONS:
        raise HTTPException(404, "Unknown section")
    try:
        body = parse_json(_body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    cleaned = view_prefs.clean(section, body)
    row = db.query(UserViewPref).filter(UserViewPref.user_id == user.id, UserViewPref.section == section).first()
    if row:
        row.prefs_json = json.dumps(cleaned)
    else:
        db.add(UserViewPref(user_id=user.id, section=section, prefs_json=json.dumps(cleaned)))
    db.commit()
    if section == "site":
        site_layout.remember(request, user.id, cleaned)       # the next page render reads it from the session, with no query
    return cleaned


def apply_preset(db: Session, user_id: int, preset_id: str) -> dict:
    """Save every layout a preset sets for one user (merged onto what they had); returns {section: cleaned}. KeyError if unknown."""
    current = saved_for(db, user_id)
    choices = view_prefs.preset_choices(preset_id, current)
    for section, cleaned in choices.items():
        row = db.query(UserViewPref).filter(UserViewPref.user_id == user_id, UserViewPref.section == section).first()
        if row:
            row.prefs_json = json.dumps(cleaned)
        else:
            db.add(UserViewPref(user_id=user_id, section=section, prefs_json=json.dumps(cleaned)))
    db.commit()
    return choices


@router.post("/api/view-prefs/preset/{preset_id}")
def view_prefs_preset(preset_id: str, request: Request, db: Session = Depends(get_db)):
    """One click, several layouts (view_prefs.PRESETS) - each person's own, like every saved layout."""
    user = _user(request)
    if preset_id not in view_prefs.PRESETS:
        raise HTTPException(404, "Unknown preset")
    choices = apply_preset(db, user.id, preset_id)
    if "site" in choices:
        site_layout.remember(request, user.id, choices["site"])
    return choices


@router.delete("/api/view-prefs/{section}")
def view_prefs_reset(section: str, request: Request, db: Session = Depends(get_db)):
    """Forget one saved layout (the page goes back to its default for this person)."""
    user = _user(request)
    if section not in view_prefs.SECTIONS:
        raise HTTPException(404, "Unknown section")
    db.query(UserViewPref).filter(UserViewPref.user_id == user.id, UserViewPref.section == section).delete()
    db.commit()
    if section == "site":
        site_layout.forget(request)
    return {"reset": section}


@router.delete("/api/view-prefs")
def view_prefs_reset_all(request: Request, db: Session = Depends(get_db)):
    user = _user(request)
    n = db.query(UserViewPref).filter(UserViewPref.user_id == user.id).delete()
    db.commit()
    site_layout.forget(request)
    return {"reset": n}


# ── named views (a layout saved under a name, picked again from the layout bar's Views menu) ─────────────────────────────────────────────
MAX_NAMED = 20          # per person and page


def _named(db: Session, user_id: int, section: str) -> list:
    rows = db.query(SavedView).filter(SavedView.user_id == user_id, SavedView.section == section).order_by(SavedView.name).all()
    out = []
    for r in rows:
        try:
            state = json.loads(r.prefs_json or "{}")
        except ValueError:
            state = {}
        out.append({"id": r.id, "name": r.name, "state": view_prefs.clean(section, state)})
    return out


@router.get("/api/saved-views/{section}")
def saved_views_list(section: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request)
    if section not in view_prefs.SECTIONS:
        raise HTTPException(404, "Unknown section")
    return {"views": _named(db, user.id, section)}


@router.post("/api/saved-views/{section}")
def saved_views_save(section: str, request: Request, db: Session = Depends(get_db), _body: bytes = Depends(read_body)):
    """Save the layout in the body (`{name, state}`) under its name - the same name again replaces it."""
    user = _user(request)
    if section not in view_prefs.SECTIONS:
        raise HTTPException(404, "Unknown section")
    try:
        body = parse_json(_body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    name = " ".join(str(body.get("name") or "").split())[:40]
    if not name:
        raise HTTPException(400, "Give the view a name")
    cleaned = view_prefs.clean(section, body.get("state"))
    row = db.query(SavedView).filter(SavedView.user_id == user.id, SavedView.section == section, SavedView.name == name).first()
    if row:
        row.prefs_json = json.dumps(cleaned)
    else:
        if db.query(SavedView).filter(SavedView.user_id == user.id, SavedView.section == section).count() >= MAX_NAMED:
            raise HTTPException(400, f"At most {MAX_NAMED} saved views per page - delete one first")
        db.add(SavedView(user_id=user.id, section=section, name=name, prefs_json=json.dumps(cleaned)))
    db.commit()
    return {"views": _named(db, user.id, section)}


@router.delete("/api/saved-views/{section}/{view_id}")
def saved_views_delete(section: str, view_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request)
    n = db.query(SavedView).filter(SavedView.id == view_id, SavedView.user_id == user.id, SavedView.section == section).delete()
    db.commit()
    if not n:
        raise HTTPException(404)
    return {"views": _named(db, user.id, section)}
