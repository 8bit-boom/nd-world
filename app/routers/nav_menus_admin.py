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
    db.commit()
    return RedirectResponse("/settings?tab=navigation", status_code=303)
