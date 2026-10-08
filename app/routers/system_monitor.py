"""System Monitor: live GPU (VRAM, temperature, power, load), CPU and RAM for the machine nd-world runs on.

GM-only (it describes the server): the route is not on any player / assistant allowlist, and each handler also checks. The numbers
come from app/system_stats.py; the page polls /api/system/stats every few seconds while it is visible.
"""
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import system_stats
from ..database import get_db
from ..deps import get_world_ctx
from ..templating import templates

router = APIRouter()


def _gm(request: Request):
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)


@router.get("/system", response_class=HTMLResponse)
def system_monitor_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _gm(request)
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("system_monitor.html", {"request": request, "world": world, "worlds": worlds})


@router.get("/api/system/stats")
def system_stats_json(request: Request):
    """One reading: {time, cpu: {percent, cores, per_core, load, temp_c, model}, memory: {...}, gpu: {available, reason?, devices: [...]}}."""
    _gm(request)
    return JSONResponse(system_stats.snapshot(), headers={"Cache-Control": "no-store"})


@router.post("/api/system/gpu-power")
async def system_set_gpu_power(request: Request):
    """Set a GPU's power limit. Body: {"index": 0, "watts": 100}. The value must be a whole number inside the card's own min..max
    (checked against nvidia-smi before anything runs). GM-only; the container needs permission to change it, otherwise the reply says
    so and gives the host command. Returns {"ok", "message"}."""
    _gm(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    result = system_stats.set_power_limit(body.get("index"), body.get("watts"))
    return JSONResponse(result, status_code=200 if result["ok"] else 400)
