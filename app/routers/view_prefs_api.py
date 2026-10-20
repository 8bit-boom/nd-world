"""Saved layouts per account: GET /api/view-prefs (all of the signed-in user's choices) and POST /api/view-prefs/{section} (save one).

Open to every signed-in user (a player's layout of a read-only page is theirs to choose); each person only ever reads and writes their own
rows. What is stored is always the result of app.view_prefs.clean, so a hand-made request cannot park junk in the table."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import site_layout, view_prefs
from ..database import get_db
from ..models import UserViewPref
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
