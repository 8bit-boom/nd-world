"""System Monitor: live GPU (VRAM, temperature, power, load), CPU and RAM for the machine nd-world runs on.

GM-only (it describes the server): the route is not on any player / assistant allowlist, and each handler also checks. The numbers
come from app/system_stats.py; the page polls /api/system/stats every few seconds while it is visible.
"""
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from .. import fan_control, system_stats
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


@router.get("/api/system/fans")
def system_fans(request: Request):
    """The motherboard fan headers hwmon exposes ({id, chip, label, rpm, percent, manual, writable}) plus the saved "follow the GPU
    temperature" settings. `fans` is empty when no fan-chip driver is loaded (or the host's /sys is not mounted) - that is not an error."""
    _gm(request)
    return JSONResponse(fan_control.status(), headers={"Cache-Control": "no-store"})


@router.post("/api/system/fans")
async def system_fans_set(request: Request):
    """Change a fan. Body: {"fan": "<id>", "percent": 20..100} holds it (and stops "follow the GPU" for it), {"fan": "<id>", "auto": true}
    hands it back to the BIOS curve, {"follow_gpu": true, "fans": ["<id>", ...], "curve": [[temp, %], ...]} saves the follow-the-GPU
    settings and starts the loop (false stops it). GM-only. A speed under 20 % is raised to 20 %; the curve is validated."""
    _gm(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    if "follow_gpu" in body:
        cfg = fan_control.load_config()
        ids = body.get("fans", cfg["fan_ids"])
        known = {f["id"] for f in fan_control.list_fans()}
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in known for i in ids):
            raise HTTPException(400, "Unknown fan")
        curve = fan_control.clean_curve(body["curve"]) if "curve" in body else cfg["curve"]
        if curve is None:
            raise HTTPException(400, "The curve needs 2-8 points: rising temperatures (20-110 C) and fan speeds that never fall.")
        on = bool(body["follow_gpu"])
        if on and not ids:
            raise HTTPException(400, "Pick at least one fan to follow the GPU.")
        fan_control.save_config({"follow_gpu": on, "fan_ids": ids, "curve": curve})
        if on:
            fan_control.apply_once(fan_control._hottest_gpu())
            fan_control.start()
        else:
            fan_control.stop()
        return JSONResponse({"ok": True, "message": "Following the GPU temperature." if on else "Stopped following the GPU."})
    fan = body.get("fan")
    if not isinstance(fan, str):
        raise HTTPException(400, "fan is required")
    if body.get("auto"):
        result = fan_control.give_back(fan)
    else:
        cfg = fan_control.load_config()
        if fan in cfg["fan_ids"] and cfg["follow_gpu"]:
            fan_control.save_config({**cfg, "fan_ids": [i for i in cfg["fan_ids"] if i != fan], "follow_gpu": len(cfg["fan_ids"]) > 1})
        result = fan_control.set_percent(fan, body.get("percent"))
    return JSONResponse(result, status_code=200 if result["ok"] else 400)
