"""The Media tab of an entity page (templates/entities/_media.html): what a GM / assistant's picker may choose from, and every way of adding
to the tab or changing what is on it - files dropped on the tab, or pictures and clips picked from this world's galleries.

The entity page itself shows the tab server-side to every viewer (only what that viewer may see); these routes are for whoever may edit the
entity. A file's extension says which kind it is, as on the session page (app/routers/session_media.py): a picture is kept on the entity
(app/entity_media.py), audio and video become clips of their libraries that belong to this entity. Clips keep the library's rules - its size
limit, its per-world maximum and its "players can hear / watch" switch - so touching one needs that section's edit level too. Large files
arrive in parts (static/js/chunked-upload.js), as everywhere else.
"""
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from .. import entity_media as em
from .. import gallery as _gallery
from ..database import get_db
from ..deps import get_world_ctx, is_gm as _is_gm, parse_json, read_body, require_can_edit as _require_can_edit, world_can_edit_section
from ..models import AudioClip, Entity, VideoClip, World
from ..uploads import copy_upload_bounded, reassemble_upload_chunks, save_upload_chunk
from .audio import _MAX_DESCRIPTION, _MAX_NAME, _clear_now_playing
from .gallery import _finalize_album_image
from .session_media import _CHUNK_MAX_BYTES, _KIND_EXTS, _TRUE, _check_library_room, _kind_for, _max_bytes, _stage
from .session_audio import _UPLOADS

router = APIRouter()

_CHUNKS_ROOT = _UPLOADS / "entity_media" / "_chunks"
_LIBRARY_SHOWN = 200      # the most recent clips of each library the picker lists
_PICTURES_SHOWN = 2000    # and the most pictures
_KINDS = ("image", "audio", "video")


def _editable(request: Request, db: Session, entity_id: int, active_world: str):
    """The entity of the active world, for someone who may edit content (403 / 404 otherwise)."""
    _require_can_edit(request)
    world, _worlds = get_world_ctx(request, db, active_world)
    entity = db.get(Entity, entity_id)
    if not world or not entity or entity.world_id != world.id:
        raise HTTPException(404)
    return world, entity


def _need_section(request: Request, world, kind: str) -> None:
    """A clip belongs to its library: changing one needs that section's edit level (always true for a GM)."""
    if kind in ("audio", "video") and not world_can_edit_section(request, world, kind):
        raise HTTPException(403)


def _body_of(raw: bytes) -> dict:
    try:
        body = parse_json(raw)
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


def _state(db: Session, request: Request, world: World, entity: Entity, *, libraries: bool = False) -> dict:
    """What is on the tab, for whoever is asking: everything for a GM, only what players may see for an assistant (who always sees what
    players see). With `libraries`, what the picker may add from this world's galleries."""
    gm = _is_gm(request)
    out = {"pictures": [em.picture_dict(i) for i in em.pictures(entity) if gm or i["visible"]],
           "audio": [_audio_dict(c) for c in em.attached_audio(db, entity, is_gm=gm)],
           "video": [_video_dict(c) for c in em.attached_video(db, entity, is_gm=gm)],
           "max_pictures": em.MAX_PICTURES}
    if not libraries:
        return out
    attached = {"audio": {c.id for c in em.attached_audio(db, entity, is_gm=True)},
                "video": {c.id for c in em.attached_video(db, entity, is_gm=True)}}
    kept = {i["url"] for i in em.pictures(entity)}
    vis = _is_gm(request)

    def clips(model, kind):
        q = db.query(model).filter(model.world_id == world.id)
        if not vis:
            q = q.filter(model.visible_to_players.is_(True))
        rows = q.order_by(model.created_at.desc(), model.id.desc()).limit(_LIBRARY_SHOWN).all()
        return [c for c in rows if c.id not in attached[kind]]

    out["library"] = {
        "pictures": [{"url": e["url"], "name": e["name"]} for e in _gallery.all_world_image_urls(db, world) if e["url"] not in kept][:_PICTURES_SHOWN],
        "audio": [{"id": c.id, "name": c.name, "visible_to_players": bool(c.visible_to_players), "file_url": c.file_url,
                   "entity_id": c.entity_id} for c in clips(AudioClip, "audio")],
        "video": [{"id": c.id, "name": c.name, "visible_to_players": bool(c.visible_to_players), "file_url": c.file_url,
                   "poster_url": c.poster_url or "", "entity_id": c.entity_id} for c in clips(VideoClip, "video")],
    }
    return out


def _audio_dict(c: AudioClip) -> dict:
    return {"id": c.id, "name": c.name, "file_url": c.file_url, "visible_to_players": bool(c.visible_to_players), "transcript": c.transcript or ""}


def _video_dict(c: VideoClip) -> dict:
    return {"id": c.id, "name": c.name, "file_url": c.file_url, "poster_url": c.poster_url or "", "visible_to_players": bool(c.visible_to_players)}


def _require_picture_room(entity) -> None:
    """400 when the entity keeps as many pictures as it may. Call BEFORE writing any file."""
    if len(em.pictures(entity)) >= em.MAX_PICTURES:
        raise HTTPException(400, f"This entity already keeps the maximum of {em.MAX_PICTURES} pictures - remove one first.")


async def _finish(db: Session, request: Request, world: World, entity: Entity, kind: str, dest: Path, filename: str, name: str, visible: bool) -> dict:
    """The upload is saved at `dest`: keep it on the entity / add it to its library. The file is removed again if anything fails."""
    try:
        label = (name or "").strip()
        stem = Path(filename).stem
        if kind == "image":
            url = _finalize_album_image(dest, db)
            item = {"uid": em.new_uid(), "url": url, "caption": (label or stem)[:em.MAX_CAPTION], "visible": visible}
            em.store_pictures(entity, em.pictures(entity) + [item])
            db.commit()
            return em.picture_dict(item)
        about = f"Media for {entity.name}"[:_MAX_DESCRIPTION]
        if kind == "video":
            from .video import _finish_stored_file, _generate_poster
            final = await _finish_stored_file(dest, dest.parent, world)
            poster = await _generate_poster(final, dest.parent)
            clip = VideoClip(world_id=world.id, name=(label[:_MAX_NAME] or stem[:_MAX_NAME] or "Entity video"), description=about,
                             file_url=f"/uploads/video/{final.name}", poster_url=poster, visible_to_players=visible, entity_id=entity.id)
            db.add(clip)
            db.commit()
            db.refresh(clip)
            return _video_dict(clip)
        clip = AudioClip(world_id=world.id, name=(label[:_MAX_NAME] or stem[:_MAX_NAME] or "Entity audio"), description=about,
                         file_url=f"/uploads/audio/{dest.name}", visible_to_players=visible, entity_id=entity.id)
        db.add(clip)
        db.commit()
        db.refresh(clip)
        return _audio_dict(clip)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


# ── reading ──────────────────────────────────────────────────────────────────────────────────────────────────

@router.get("/api/entity/{entity_id}/media")
def media_list(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The tab's own state and what may be added to it from this world."""
    world, entity = _editable(request, db, entity_id, active_world)
    return _state(db, request, world, entity, libraries=True)


# ── uploading ────────────────────────────────────────────────────────────────────────────────────────────────

@router.post("/api/entity/{entity_id}/media/upload")
async def media_upload(entity_id: int, request: Request, file: UploadFile = File(...), name: str = Form(""),
                       visible_to_players: str = Form(""), kind: str = Form(""),
                       db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Add one dropped file: a picture, a video or an audio clip, decided by its extension."""
    world, entity = _editable(request, db, entity_id, active_world)
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    which, ext = _kind_for(file.filename, kind)
    _need_section(request, world, which)
    if which == "image":
        _require_picture_room(entity)
    else:
        _check_library_room(db, world, which)
    dest = _stage(which, file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_max_bytes(db, which))
    item = await _finish(db, request, world, entity, which, dest, file.filename, name, visible_to_players.strip().lower() in _TRUE)
    return {**_state(db, request, world, entity), "kind": which, "item": item}


@router.post("/api/entity/{entity_id}/media/upload/chunk")
def media_upload_chunk(entity_id: int, request: Request, file: UploadFile = File(...), upload_id: str = Form(...),
                       chunk_index: int = Form(...), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """One part of a large file (static/js/chunked-upload.js); /media/upload/complete puts them together."""
    _world, _entity = _editable(request, db, entity_id, active_world)
    cap = max(_max_bytes(db, k) for k in _KINDS)
    save_upload_chunk(_CHUNKS_ROOT, upload_id, chunk_index, file, max_bytes=min(_CHUNK_MAX_BYTES, cap))
    return {"ok": True}


@router.post("/api/entity/{entity_id}/media/upload/complete")
async def media_upload_complete(entity_id: int, request: Request, upload_id: str = Form(...), filename: str = Form(...),
                                total_chunks: int = Form(...), name: str = Form(""), visible_to_players: str = Form(""),
                                kind: str = Form(""), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, entity = _editable(request, db, entity_id, active_world)
    which, ext = _kind_for(filename, kind)
    _need_section(request, world, which)
    if which == "image":
        _require_picture_room(entity)
    else:
        _check_library_room(db, world, which)
    dest = _stage(which, filename, ext)
    reassemble_upload_chunks(_CHUNKS_ROOT, upload_id, total_chunks, dest, max_bytes=_max_bytes(db, which))
    item = await _finish(db, request, world, entity, which, dest, filename, name, visible_to_players.strip().lower() in _TRUE)
    return {**_state(db, request, world, entity), "kind": which, "item": item}


# ── picking from the galleries ───────────────────────────────────────────────────────────────────────────────

@router.post("/api/entity/{entity_id}/media/attach")
def media_attach(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None), _body: bytes = Depends(read_body)):
    """Add something that is already in this world: {kind: "image", url} - a picture of the gallery, kept on the entity hidden from players
    unless asked otherwise - or {kind: "audio"|"video", id | url}: a clip of its library, which from now on belongs to this entity."""
    world, entity = _editable(request, db, entity_id, active_world)
    body = _body_of(_body)
    kind, vis = body.get("kind"), body.get("visible_to_players")
    vis = vis if isinstance(vis, bool) else None
    if kind in ("audio", "video"):
        _need_section(request, world, kind)
        model = AudioClip if kind == "audio" else VideoClip
        cid = body.get("id")
        if cid is None and isinstance(body.get("url"), str):
            # a clip named by its address (copied in the library): this world's clip with that file
            clip = db.query(model).filter(model.world_id == world.id, model.file_url == body["url"].strip()[:512]).first()
            if not clip:
                raise HTTPException(404, f"That address is not one of this world's {kind} clips")
            cid = clip.id
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise HTTPException(400, "id must be a clip id (or url its file address)")
        clip = db.get(model, cid)
        if not clip or clip.world_id != world.id:
            raise HTTPException(404, f"No such {kind} clip in this world")
        clip.entity_id = entity.id
        if vis is not None:
            _set_visible(db, world, clip, vis)
        db.commit()
    elif kind == "image":
        url = body.get("url")
        names = {e["url"]: e["name"] for e in _gallery.all_world_image_urls(db, world)}
        if not isinstance(url, str) or url not in names:
            raise HTTPException(404, "That picture is not one of this world's images")
        _require_picture_room(entity)
        em.add_picture(entity, url, str(names[url])[:em.MAX_CAPTION], bool(vis))
        db.commit()
    else:
        raise HTTPException(400, "kind must be image, audio or video")
    return _state(db, request, world, entity)


# ── per-item switches ────────────────────────────────────────────────────────────────────────────────────────

def _set_visible(db: Session, world: World, clip, vis: bool) -> None:
    """The library's switch; a clip the GM hides must not keep playing for the players (app/routers/audio.py `audio_edit`)."""
    clip.visible_to_players = vis
    if not vis and world.now_playing_url == clip.file_url:
        _clear_now_playing(world)
        from .. import main as _main_module
        _main_module._spotlight_cache.clear()


def _clip_attached(db: Session, world: World, entity: Entity, kind: str, ref: str):
    """The audio / video clip `ref` names, attached to this entity, or 404."""
    try:
        cid = int(ref)
    except ValueError:
        raise HTTPException(404)
    clip = db.get(AudioClip if kind == "audio" else VideoClip, cid)
    if not clip or clip.world_id != world.id or clip.entity_id != entity.id:
        raise HTTPException(404)
    return clip


def _kept(entity, uid: str):
    """(the entity's pictures, the one with this uid) - change the picture in that list, then store it back - or 404."""
    items = em.pictures(entity)
    hit = next((i for i in items if i["uid"] == uid), None)
    if not hit:
        raise HTTPException(404)
    return items, hit


@router.post("/api/entity/{entity_id}/media/{kind}/{ref}/visibility")
def media_visibility(entity_id: int, kind: str, ref: str, request: Request, db: Session = Depends(get_db),
                     active_world: str = Cookie(None), _body: bytes = Depends(read_body)):
    """Let players see (or not) one item: a picture's own switch, or - for audio and video - the same switch as in their library."""
    world, entity = _editable(request, db, entity_id, active_world)
    if kind not in _KINDS:
        raise HTTPException(404)
    _need_section(request, world, kind)
    vis = _body_of(_body).get("visible_to_players")
    if not isinstance(vis, bool):
        raise HTTPException(400, "visible_to_players must be true or false")
    if kind == "image":
        items, hit = _kept(entity, ref)
        hit["visible"] = vis
        em.store_pictures(entity, items)
    else:
        _set_visible(db, world, _clip_attached(db, world, entity, kind, ref), vis)
    db.commit()
    return _state(db, request, world, entity)


@router.post("/api/entity/{entity_id}/media/{kind}/{ref}/rename")
def media_rename(entity_id: int, kind: str, ref: str, request: Request, db: Session = Depends(get_db),
                 active_world: str = Cookie(None), _body: bytes = Depends(read_body)):
    """Name an item as the tab shows it: a picture's caption (may be emptied), or the clip's own name, the one its library shows too."""
    world, entity = _editable(request, db, entity_id, active_world)
    if kind not in _KINDS:
        raise HTTPException(404)
    _need_section(request, world, kind)
    name = _body_of(_body).get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "name must be text")
    name = " ".join(name.split())
    if kind == "image":
        items, hit = _kept(entity, ref)
        hit["caption"] = name[:em.MAX_CAPTION]
        em.store_pictures(entity, items)
    else:
        if not name:
            raise HTTPException(400, "A clip needs a name")
        _clip_attached(db, world, entity, kind, ref).name = name[:em.MAX_NAME]
    db.commit()
    return _state(db, request, world, entity)


@router.post("/api/entity/{entity_id}/media/{kind}/{ref}/remove")
def media_remove(entity_id: int, kind: str, ref: str, request: Request, db: Session = Depends(get_db),
                 active_world: str = Cookie(None)):
    """Take an item off the tab. A picture is only unlinked (the file stays in the gallery, like an album's); audio and video stay in their
    libraries, no longer belonging to this entity."""
    world, entity = _editable(request, db, entity_id, active_world)
    if kind not in _KINDS:
        raise HTTPException(404)
    _need_section(request, world, kind)
    if kind == "image":
        items, hit = _kept(entity, ref)
        em.store_pictures(entity, [i for i in items if i is not hit])
    else:
        _clip_attached(db, world, entity, kind, ref).entity_id = None
    db.commit()
    return _state(db, request, world, entity)
