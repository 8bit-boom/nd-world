"""Save endpoint for GM-manageable top-nav dropdown menus — edited from the
Navigation tab on /settings (app/main.py's settings_page/settings_save
already build that page's other context; this only adds the one write
route, since the read side is handled entirely by app/templating.py's
context processor + app/nav_menus.py's load_nav_menus)."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import sanitize_section_access
from ..models import World
from ..nav_menus import sanitize_nav_menus

router = APIRouter()


@router.post("/worlds/{world_id}/nav-menus/edit")
async def nav_menus_edit_save(world_id: int, request: Request, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    form = await request.form()
    w.nav_menus_json = json.dumps(sanitize_nav_menus(str(form.get("nav_menus_json", "[]") or "[]"), w))
    # section_access_json: the per-section Players/Assistants None/Read/
    # Edit matrix, edited from the same Navigation tab form (see
    # deps.SECTION_PERMISSION_IDS) — saved alongside nav grouping since
    # they're now one combined "everything about this nav item" page.
    if "section_access_json" in form:
        w.section_access_json = sanitize_section_access(str(form.get("section_access_json", "{}") or "{}"))
    db.commit()
    return RedirectResponse("/settings?tab=navigation", status_code=303)
