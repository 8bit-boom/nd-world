"""Saved layouts per account: GET /api/view-prefs (all of the signed-in user's choices) and POST /api/view-prefs/{section} (save one).

Open to every signed-in user (a player's layout of a read-only page is theirs to choose); each person only ever reads and writes their own
rows. What is stored is always the result of app.view_prefs.clean, so a hand-made request cannot park junk in the table."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import view_prefs
from ..database import get_db
from ..models import UserViewPref

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
async def view_prefs_save(section: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request)
    if section not in view_prefs.SECTIONS:
        raise HTTPException(404, "Unknown section")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    cleaned = view_prefs.clean(section, body)
    row = db.query(UserViewPref).filter(UserViewPref.user_id == user.id, UserViewPref.section == section).first()
    if row:
        row.prefs_json = json.dumps(cleaned)
    else:
        db.add(UserViewPref(user_id=user.id, section=section, prefs_json=json.dumps(cleaned)))
    db.commit()
    return cleaned
