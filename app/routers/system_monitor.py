"""System Monitor: live GPU (VRAM, temperature, power, load), CPU and RAM for the machine nd-world runs on.

GM-only (it describes the server): the route is not on any player / assistant allowlist, and each handler also checks. The numbers
come from app/system_stats.py; the page polls /api/system/stats every few seconds while it is visible.
"""
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
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
    """The motherboard fan headers hwmon exposes ({id, chip, label, rpm, percent, manual, writable}), the saved "follow the GPU
    temperature" settings, the last follow tick (`state`), fans that were saved but are not there now (`missing`) and the result of any
    check made this run (`checked`). `fans` is empty when no fan-chip driver is loaded - that is not an error."""
    _gm(request)
    return JSONResponse(fan_control.status(), headers={"Cache-Control": "no-store"})


def _drop_from_following(fan: str) -> bool:
    """Take one fan out of the follow set (the loop would otherwise undo whatever is done to it by hand). True when it was in it."""
    cfg = fan_control.load_config()
    if fan not in cfg["fan_ids"]:
        return False
    rest = [i for i in cfg["fan_ids"] if i != fan]
    fan_control.save_config({**cfg, "fan_ids": rest, "follow_gpu": cfg["follow_gpu"] and bool(rest)})
    return cfg["follow_gpu"]


@router.post("/api/system/fans")
async def system_fans_set(request: Request):
    """Change a fan. Body: {"fan": "<id>", "percent": 20..100} holds it (and takes it out of "follow the GPU"), {"fan": "<id>", "auto": true}
    puts back what the BIOS had set, {"follow_gpu": true, "fans": ["<id>", ...], "curve": [[temp, %], ...]} saves the follow-the-GPU
    settings and starts the loop; {"follow_gpu": false} stops it and puts every followed fan back as the BIOS had it. GM-only. A speed
    under 20 % is raised to 20 %; the curve is validated (it must reach 80 % well before the panic temperature); a fan whose check this
    run found it inverted is refused."""
    _gm(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    if "follow_gpu" in body:
        prev = fan_control.load_config()
        ids = body.get("fans", prev["fan_ids"])
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise HTTPException(400, "fans must be a list of fan ids")
        on = bool(body["follow_gpu"])
        if on:                                   # switching OFF must work even when the driver is not loaded (the ids may be missing)
            known = {f["id"] for f in fan_control.list_fans()}
            if any(i not in known for i in ids):
                raise HTTPException(400, "Unknown fan (is the fan-chip driver loaded?)")
        curve = fan_control.clean_curve(body["curve"]) if "curve" in body else prev["curve"]
        if curve is None:
            raise HTTPException(400, "The curve needs 2-8 points: rising temperatures and fan speeds that never fall, and it must reach at least %d %% by %d C."
                                % (fan_control.TOP_PERCENT, fan_control.PANIC_TEMP - fan_control.TOP_MARGIN))
        if on and not ids:
            raise HTTPException(400, "Pick at least one fan to follow the GPU.")
        bad = [i for i in ids if on and fan_control.verdict(i) == "inverted"]
        if bad:
            raise HTTPException(400, "The check found that output inverted (a higher duty makes it slower) - it must not follow the GPU.")
        fan_control.save_config({"follow_gpu": on, "fan_ids": ids, "curve": curve})
        notes = []
        if prev["follow_gpu"]:                                  # fans the loop was driving and no longer is: back to the BIOS
            for fid in prev["fan_ids"]:
                if (not on) or fid not in ids:
                    notes.append(fan_control.give_back(fid)["message"])
        if on:
            fan_control.apply_once(fan_control.gpu_temperature())
            fan_control.start()
            msg = "Following the GPU temperature."
        else:
            fan_control.stop()
            msg = "Stopped following the GPU."
        return JSONResponse({"ok": True, "message": " ".join([msg] + sorted(set(notes)))})
    fan = body.get("fan")
    if not isinstance(fan, str):
        raise HTTPException(400, "fan is required")
    before = fan_control.load_config()
    was_following = _drop_from_following(fan)             # first: the loop must not undo what is about to be done
    if body.get("auto"):
        result = fan_control.give_back(fan)
    else:
        result = fan_control.set_percent(fan, body.get("percent"))
        if result["ok"] and was_following:
            result["message"] += " It no longer follows the GPU."
    if not result["ok"] and was_following:                # nothing was changed: it keeps following
        fan_control.save_config(before)
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@router.post("/api/system/fans/check")
async def system_fans_check(request: Request):
    """Check one fan: run it at 100 % for about six seconds, compare its rpm, then put it back exactly as it was. Body: {"fan": "<id>"}.
    Returns {"ok", "verdict": speeds_up | inverted | no_change | no_signal, "message", "rpm_before", "rpm_after"}. Never slows a fan.
    GM-only. Takes about six seconds."""
    _gm(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON body required")
    fan = body.get("fan") if isinstance(body, dict) else None
    if not isinstance(fan, str):
        raise HTTPException(400, "fan is required")
    result = await run_in_threadpool(fan_control.check_fan, fan)
    return JSONResponse(result, status_code=200 if result["ok"] else 400)
