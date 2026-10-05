"""The AI media renamer: scan audio clips, video clips and images (their names, tags, lyrics, transcripts, usage, and
optionally the picture itself) and propose better names - reviewed by the GM, applied in one batch, undoable.

The logic is in app/media_rename.py; this is the HTTP surface and the page. GM-only like the libraries it works on, plus
assistants (content tier): every call checks the "audio" / "video" / "images" edit level for the kinds it touches.
"""
import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import media_rename as mr
from .. import media_rename_jobs as jobs
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section
from ..models import AudioAlbum, AudioClip, ImageAlbum, MediaRenameRun, User, VideoAlbum, VideoClip
from ..templating import templates, thumb_url

router = APIRouter()

SECTION = {"audio": "audio", "video": "video", "image": "images"}
MAX_SUGGEST_ITEMS = 12
MAX_LISTED = 1500


def _ctx(request: Request, db: Session, active_world):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    return world, worlds


def _require_edit(request: Request, world, kinds) -> None:
    """Renaming is editing: the viewer needs edit level on the section of every kind they touch (a GM always has it;
    Settings -> Navigation can dial an assistant down)."""
    for kind in set(kinds):
        if kind not in SECTION or not world_can_edit_section(request, world, SECTION[kind]):
            raise HTTPException(403)


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Send a JSON body.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Send a JSON object.")
    return data


def _album_labels(db: Session, model, world_id: int) -> dict:
    """{album id: "Parent / Child"} for one library's albums."""
    albums = db.query(model).filter(model.world_id == world_id).all()
    by_id = {a.id: a for a in albums}

    def label(a, depth=0):
        parent = by_id.get(a.parent_id) if a.parent_id and depth < 20 else None
        return (label(parent, depth + 1) + " / " if parent else "") + a.name
    return {a.id: label(a) for a in albums}


@router.get("/media-rename", response_class=HTMLResponse)
def media_rename_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _ctx(request, db, active_world)
    if not any(world_can_edit_section(request, world, s) for s in SECTION.values()):
        raise HTTPException(403)
    from .. import ai as _ai
    kind = request.query_params.get("kind")
    try:
        album = int(request.query_params.get("album") or 0)
    except ValueError:
        album = 0
    editable = [k for k, s in SECTION.items() if world_can_edit_section(request, world, s)]
    return templates.TemplateResponse("media_rename/index.html", {
        "request": request, "world": world, "worlds": worlds,
        "start": {"kind": kind if kind in editable else editable[0], "album": album, "kinds": editable},
        "presets": {k: v for k, v in mr.STYLE_PRESETS.items()},
        "ai_ready": bool(_ai.effective_llm_api_key()),
        "history": mr.recent_batches(db, world),
    })


@router.get("/api/media-rename/items")
def media_rename_items(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The library's items to choose from: ?kind=audio|video|image, optional ?album=<id> and ?q=<text>. Each carries
    `generic` - True when its name says nothing a person would search for."""
    world, _ = _ctx(request, db, active_world)
    kind = request.query_params.get("kind")
    _require_edit(request, world, [kind])
    q = (request.query_params.get("q") or "").strip().lower()
    try:
        album = int(request.query_params.get("album") or 0)
    except ValueError:
        album = 0
    items: list = []
    if kind in ("audio", "video"):
        model, album_model = (AudioClip, AudioAlbum) if kind == "audio" else (VideoClip, VideoAlbum)
        labels = _album_labels(db, album_model, world.id)
        query = db.query(model).filter(model.world_id == world.id)
        if album:
            query = query.filter(model.album_id == album)
        for row in query.order_by(model.name, model.id).limit(MAX_LISTED + 1).all():
            fname = mr.readable_filename(row.file_url)
            if q and q not in (row.name or "").lower() and q not in fname.lower():
                continue
            items.append({
                "kind": kind, "id": row.id, "name": row.name or "", "filename": fname,
                "album": labels.get(row.album_id, ""), "generic": mr.looks_generic(row.name, fname),
                "has_transcript": bool((row.transcript or "").strip()), "visible": bool(row.visible_to_players),
                "thumb": thumb_url(row.poster_url) if kind == "video" and row.poster_url else None,
            })
        albums = [{"id": i, "name": n} for i, n in sorted(labels.items(), key=lambda kv: kv[1].lower())]
    elif kind == "image":
        usage = mr.image_usage(db, world)
        labels = _album_labels(db, ImageAlbum, world.id)
        in_album = None
        if album:
            chosen = db.query(ImageAlbum).filter(ImageAlbum.id == album, ImageAlbum.world_id == world.id).first()
            try:
                in_album = set(json.loads(chosen.image_urls_json or "[]")) if chosen else set()
            except (TypeError, ValueError):
                in_album = set()
        for url, info in sorted(usage.items()):
            if in_album is not None and url not in in_album:
                continue
            fname = mr.readable_filename(url)
            if q and q not in info["name"].lower() and q not in fname.lower():
                continue
            items.append({
                "kind": "image", "url": url, "name": info["name"], "filename": fname, "album": ", ".join(info["albums"][:3]),
                "used_in": info["uses"][:3], "thumb": thumb_url(url),
                "generic": mr.looks_generic(info["name"], fname) and not info["uses"],
            })
        albums = [{"id": i, "name": n} for i, n in sorted(labels.items(), key=lambda kv: kv[1].lower())]
    else:
        raise HTTPException(400, "kind must be audio, video or image.")
    truncated = len(items) > MAX_LISTED
    return {"kind": kind, "items": items[:MAX_LISTED], "albums": albums, "truncated": truncated,
            "generic_count": sum(1 for i in items[:MAX_LISTED] if i["generic"])}


def _candidates(db: Session, world, kind: str, album: int = 0) -> list:
    """Every item of one kind as light dicts {ref, generic}: what "select everything" and the counts work from."""
    if kind in ("audio", "video"):
        model = AudioClip if kind == "audio" else VideoClip
        query = db.query(model.id, model.name, model.file_url).filter(model.world_id == world.id)
        if album:
            query = query.filter(model.album_id == album)
        return [{"ref": {"kind": kind, "id": i}, "name": name or "", "generic": mr.looks_generic(name, mr.readable_filename(url))}
                for i, name, url in query.order_by(model.name, model.id).all()]
    in_album = None
    if album:
        chosen = db.query(ImageAlbum).filter(ImageAlbum.id == album, ImageAlbum.world_id == world.id).first()
        try:
            in_album = set(json.loads(chosen.image_urls_json or "[]")) if chosen else set()
        except (TypeError, ValueError):
            in_album = set()
    out = []
    for url, info in sorted(mr.image_usage(db, world).items()):
        if in_album is not None and url not in in_album:
            continue
        out.append({"ref": {"kind": "image", "url": url}, "name": info["name"],
                    "generic": mr.looks_generic(info["name"], mr.readable_filename(url)) and not info["uses"]})
    return out


@router.get("/api/media-rename/summary")
def media_rename_summary(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """How much of each library still needs a name: {audio|video|image: {total, needs_work}} for the kinds the viewer may edit."""
    world, _ = _ctx(request, db, active_world)
    kinds = [k for k, s in SECTION.items() if world_can_edit_section(request, world, s)]
    if not kinds:
        raise HTTPException(403)
    out = {}
    for kind in kinds:
        rows = _candidates(db, world, kind)
        out[kind] = {"total": len(rows), "needs_work": sum(1 for r in rows if r["generic"])}
    return out


@router.post("/api/media-rename/select")
async def media_rename_select(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Everything that matches, across the whole library (not one page of it): {kinds, scope: "needs_work" | "all",
    album?} -> {items:[{kind, id|url}], count, capped}. `count` is the real total, `items` is cut at MAX_RUN_ITEMS."""
    world, _ = _ctx(request, db, active_world)
    body = await _json(request)
    kinds = body.get("kinds")
    if not isinstance(kinds, list):
        raise HTTPException(400, "kinds must be a list.")
    kinds = [k for k in ("audio", "video", "image") if k in kinds] if all(k in SECTION for k in kinds) else kinds
    _require_edit(request, world, kinds)
    only_generic = body.get("scope", "needs_work") != "all"
    try:
        album = int(body.get("album") or 0) if len(kinds) == 1 else 0
    except (TypeError, ValueError):
        album = 0
    refs = []
    for kind in kinds:
        refs += [r["ref"] for r in _candidates(db, world, kind, album) if r["generic"] or not only_generic]
    return {"items": refs[:jobs.MAX_RUN_ITEMS], "count": len(refs), "capped": len(refs) > jobs.MAX_RUN_ITEMS}


def _run_view(run, names: dict, with_results: bool = False) -> dict:
    try:
        results = json.loads(run.results_json or "[]")
        errors = json.loads(run.errors_json or "[]")
    except ValueError:
        results, errors = [], ["Stored results were unreadable."]
    out = {
        "id": run.id, "status": run.status, "done": run.done or 0, "total": run.total or 0, "error": run.error or "",
        "resumed": run.resumed_count or 0, "proposals": len(results), "errors_count": len(errors),
        "created_at": run.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if run.created_at else None,
        "by": names.get(run.created_by_user_id, "someone"),
    }
    if with_results:
        out["results"], out["errors"] = results, errors
    return out


def _run_names(db: Session, runs: list) -> dict:
    ids = {r.created_by_user_id for r in runs if r.created_by_user_id}
    users = db.query(User).filter(User.id.in_(ids or {0})).all()
    return {u.id: (u.display_name or u.email.split("@")[0]) for u in users}


def _run_or_404(db: Session, world, run_id: int):
    run = db.query(MediaRenameRun).filter(MediaRenameRun.id == run_id, MediaRenameRun.world_id == world.id).first()
    if not run:
        raise HTTPException(404)
    return run


def _require_any_edit(request: Request, world) -> None:
    if not any(world_can_edit_section(request, world, s) for s in SECTION.values()):
        raise HTTPException(403)


@router.post("/api/media-rename/runs")
async def media_rename_run_start(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Start a bulk run in the background: {items:[{kind,id|url}] (up to MAX_RUN_ITEMS), preset?, style?, pictures?,
    listen?} -> {id}. It keeps going if the page is closed; poll /runs/{id}."""
    world, _ = _ctx(request, db, active_world)
    body = await _json(request)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Choose at least one item.")
    _require_edit(request, world, [i.get("kind") if isinstance(i, dict) else None for i in items])
    from .. import ai as _ai
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")
    user = getattr(request.state, "user", None)
    try:
        run_id = jobs.create_run(world.id, user.id if user else None, [mr_ref(i) for i in items], body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": run_id}


def mr_ref(item: dict) -> dict:
    return {"kind": item["kind"], "url": item.get("url")} if item["kind"] == "image" else {"kind": item["kind"], "id": item.get("id")}


@router.get("/api/media-rename/runs")
def media_rename_runs(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _ctx(request, db, active_world)
    _require_any_edit(request, world)
    runs = db.query(MediaRenameRun).filter(MediaRenameRun.world_id == world.id).order_by(MediaRenameRun.id.desc()).limit(20).all()
    names = _run_names(db, runs)
    return {"runs": [_run_view(r, names) for r in runs]}


@router.get("/api/media-rename/runs/{run_id}")
def media_rename_run(run_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _ctx(request, db, active_world)
    _require_any_edit(request, world)
    run = _run_or_404(db, world, run_id)
    return _run_view(run, _run_names(db, [run]), with_results=True)


@router.post("/api/media-rename/runs/{run_id}/cancel")
def media_rename_run_cancel(run_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _ctx(request, db, active_world)
    _require_any_edit(request, world)
    _run_or_404(db, world, run_id)
    if not jobs.cancel_run(run_id):
        raise HTTPException(400, "That run is not running.")
    return {"ok": True}


@router.post("/api/media-rename/runs/{run_id}/delete")
def media_rename_run_delete(run_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _ctx(request, db, active_world)
    _require_any_edit(request, world)
    _run_or_404(db, world, run_id)
    if not jobs.delete_run(run_id):
        raise HTTPException(400, "Cancel the run before deleting it.")
    return {"ok": True}


@router.post("/api/media-rename/suggest")
async def media_rename_suggest(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Proposed names for up to MAX_SUGGEST_ITEMS items: {items:[{kind,id|url}], preset?, style?, pictures?, listen?}.
    Changes nothing. A background AI task (it queues behind other AI work)."""
    world, _ = _ctx(request, db, active_world)
    body = await _json(request)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Choose at least one item.")
    if len(items) > MAX_SUGGEST_ITEMS:
        raise HTTPException(400, f"At most {MAX_SUGGEST_ITEMS} items per request.")
    _require_edit(request, world, [i.get("kind") if isinstance(i, dict) else None for i in items])
    from .. import ai as _ai
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")
    return await mr.suggest(
        db, world, items, style=str(body.get("style") or ""), preset=str(body.get("preset") or ""),
        pictures=bool(body.get("pictures")), listen=bool(body.get("listen")),
    )


@router.post("/api/media-rename/apply")
async def media_rename_apply(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Apply reviewed renames: {renames:[{kind, id|url, name}]}. Returns {batch_id, applied, skipped}."""
    world, _ = _ctx(request, db, active_world)
    body = await _json(request)
    renames = body.get("renames")
    if not isinstance(renames, list) or not renames:
        raise HTTPException(400, "Nothing to rename.")
    _require_edit(request, world, [r.get("kind") if isinstance(r, dict) else None for r in renames])
    user = getattr(request.state, "user", None)
    try:
        out = mr.apply_renames(db, world, user.id if user else None, renames)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.get("run_id") is not None:          # the proposals came from a background run: they are dealt with now
        jobs.forget_results(db, world.id, body.get("run_id"), renames)
    return out


@router.post("/api/media-rename/undo")
async def media_rename_undo(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Put a batch of renames back: {batch_id}. Items renamed again since are left alone and listed."""
    world, _ = _ctx(request, db, active_world)
    if not any(world_can_edit_section(request, world, s) for s in SECTION.values()):
        raise HTTPException(403)
    body = await _json(request)
    batch_id = str(body.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(400, "Say which batch to undo.")
    return mr.undo_batch(db, world, batch_id)


@router.get("/api/media-rename/history")
def media_rename_history(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _ctx(request, db, active_world)
    if not any(world_can_edit_section(request, world, s) for s in SECTION.values()):
        raise HTTPException(403)
    return {"batches": mr.recent_batches(db, world)}
