"""GM Cockpit (Phase 3) — one full-screen session dashboard that composes
the surfaces a GM otherwise keeps open in five separate tabs: the live
battle map, every party's member-vitals strip, the dice tray, the active
quest list, and one-click floats for AI Chat / Talk to NPCs.

The panels are deliberately *embedded live pages* (iframes of /dice and the
schematic player view in ?embed=1 chrome-less mode) plus server-rendered
partials (vitals, quests) that re-fetch over the Phase-1 live-sync bus —
not re-implementations. Anything the map/dice/NPC pages learn later, the
cockpit inherits for free.

GM-only by default (no _is_player_safe/_is_assistant_safe entry): it's a
composition of GM-facing tools, and the nav entry (nav_menus.py, id
"cockpit") is gm_only to match.
"""

import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import Party, PlayerCharacter, Quest, Schematic
from ..templating import templates
from .parties import _member_vitals

router = APIRouter()


@router.get("/cockpit", response_class=HTMLResponse)
def cockpit(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)

    # Every party's vitals, stacked — a GM usually runs 1-3 parties and
    # this way there's no picker to get wrong; the live-sync refetch
    # (#cockpit-vitals) keeps all of them current at once.
    party_panels = []
    for p in db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all():
        pc_ids = json.loads(p.member_pc_ids_json or "[]")
        member_pcs = (
            db.query(PlayerCharacter).filter(PlayerCharacter.id.in_(pc_ids)).all()
            if pc_ids else []
        )
        party_panels.append({
            "id": p.id, "name": p.name,
            "members": _member_vitals(db, member_pcs),
        })

    # Top-level quests still in play, party assignment attached for context.
    quests = (
        db.query(Quest)
        .filter(Quest.world_id == world.id, Quest.status == "active", Quest.parent_id.is_(None))
        .order_by(Quest.category, Quest.title)
        .limit(10)
        .all()
    )
    party_names = {p.id: p.name for p in db.query(Party).filter(Party.world_id == world.id).all()}
    quest_rows = [{
        "id": q.id, "title": q.title, "category": q.category,
        "summary": q.summary or "",
        "party": party_names.get(q.assigned_party_id),
    } for q in quests]

    # Floatable maps: every non-HTML schematic (Leaflet-image maps and SVG
    # canvases — both have a player view the cockpit can dock). HTML-type
    # schematics are external files with no embeddable view.
    maps = (
        db.query(Schematic)
        .filter(Schematic.world_id == world.id, Schematic.is_html.is_(False))
        .order_by(Schematic.name)
        .all()
    )

    return templates.TemplateResponse("cockpit.html", {
        "request": request, "world": world, "worlds": worlds,
        "party_panels": party_panels,
        "quests": quest_rows,
        "maps": maps,
    })
