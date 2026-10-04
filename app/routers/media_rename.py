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
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section
from ..models import AudioAlbum, AudioClip, ImageAlbum, VideoAlbum, VideoClip
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
        return mr.apply_renames(db, world, user.id if user else None, renames)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
