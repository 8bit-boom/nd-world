"""What goes with a session: audio, pictures and videos - the leaf module (no router imports) the routers share.

A session remembers which clips of the /audio library go with it (GameSession.recap_audio_json, a list of ids), which clips
of the /video library (and which pictures) in GameSession.recap_media_json. The libraries own the audio and video files
and their "players can hear / watch" switch; a picture has no library row of its own, so the session item carries the
URL, a caption, its own visibility, and whether it was uploaded for this session ("own": it is deleted with it).

One limit covers all three kinds together: Settings -> System (AppSettings.session_media_max), 10 by default. Lowering it
never drops what a session already holds - it only stops adding more.

Players see only the items marked visible (a clip's own switch for audio and video); a GM sees everything.
"""
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import media_albums
from .database import get_app_settings
from .models import AudioClip, GameSession, VideoClip

_log = logging.getLogger("nd.session_media")

DEFAULT_MAX = 10
HARD_MAX = 50           # the most Settings accepts, and the most a stored list may ever hold
_MAX_CAPTION = 120


# ── the limit ────────────────────────────────────────────────────────────────────────────────────────

def clamp_limit(raw) -> Optional[int]:
    """A stored/typed limit as a usable number, or None when it means "the default" (blank, zero, junk, negative)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return min(n, HARD_MAX) if n >= 1 else None


def limit(db: Session) -> int:
    return clamp_limit(getattr(get_app_settings(db), "session_media_max", None)) or DEFAULT_MAX


def used(gs) -> int:
    return len(recap_clip_ids(gs)) + len(media_items(gs))


def require_room(db: Session, gs) -> None:
    """400 when the session already holds as many items as the limit allows. Call BEFORE writing any file."""
    cap = limit(db)
    if used(gs) >= cap:
        raise HTTPException(400, f"A session can carry at most {cap} pictures, videos and audio clips in all - remove one first "
                                 "(the limit can be changed in Settings > System).")


# ── what is stored ───────────────────────────────────────────────────────────────────────────────────

def recap_clip_ids(gs) -> list:
    """The session's attached audio clip ids as a clean, de-duplicated list of ints."""
    try:
        raw = json.loads(getattr(gs, "recap_audio_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, int) and not isinstance(v, bool) and v not in out:
            out.append(v)
    return out


def store_clip_ids(gs, ids: list) -> None:
    gs.recap_audio_json = json.dumps(ids[:HARD_MAX])


def new_uid() -> str:
    return uuid.uuid4().hex[:10]


def media_items(gs) -> list:
    """The session's picture / video items, cleaned: unknown kinds, junk and duplicates dropped, every item given a uid."""
    try:
        raw = json.loads(getattr(gs, "recap_media_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    out, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        if kind == "image":
            url = it.get("url")
            if not isinstance(url, str) or not url.startswith("/uploads/") or ("image", url) in seen:
                continue
            seen.add(("image", url))
            out.append({"uid": str(it.get("uid") or new_uid())[:32], "kind": "image", "url": url,
                        "caption": str(it.get("caption") or "")[:_MAX_CAPTION], "visible": bool(it.get("visible")),
                        "own": bool(it.get("own"))})
        elif kind == "video":
            cid = it.get("clip_id")
            if isinstance(cid, bool) or not isinstance(cid, int) or ("video", cid) in seen:
                continue
            seen.add(("video", cid))
            out.append({"uid": str(it.get("uid") or new_uid())[:32], "kind": "video", "clip_id": cid})
    return out[:HARD_MAX]


def store_media(gs, items: list) -> None:
    gs.recap_media_json = json.dumps(items[:HARD_MAX])


# ── what the viewer may see ──────────────────────────────────────────────────────────────────────────

def audio_dict(c: AudioClip) -> dict:
    return {"id": c.id, "name": c.name, "description": c.description or "", "file_url": c.file_url,
            "visible_to_players": bool(c.visible_to_players), "transcript": c.transcript or ""}


def video_dict(c: VideoClip, uid: str = "") -> dict:
    return {"id": c.id, "uid": uid, "name": c.name, "description": c.description or "", "file_url": c.file_url,
            "poster_url": c.poster_url or "", "visible_to_players": bool(c.visible_to_players)}


def image_dict(it: dict) -> dict:
    return {"uid": it["uid"], "url": it["url"], "caption": it["caption"], "visible_to_players": bool(it["visible"]),
            "own": bool(it["own"])}


def attached_clips(db: Session, gs, *, is_gm: bool) -> list:
    """The audio clips attached to this session, in order, that the viewer may hear: a GM sees them all, a player only the
    ones marked visible. Ids whose clip is gone are skipped."""
    ids = recap_clip_ids(gs)
    if not ids:
        return []
    by_id = {c.id: c for c in db.query(AudioClip).filter(AudioClip.id.in_(ids), AudioClip.world_id == gs.world_id).all()}
    return [audio_dict(by_id[i]) for i in ids if i in by_id and (is_gm or by_id[i].visible_to_players)]


def attached_videos(db: Session, gs, *, is_gm: bool) -> list:
    items = [it for it in media_items(gs) if it["kind"] == "video"]
    if not items:
        return []
    by_id = {c.id: c for c in db.query(VideoClip).filter(VideoClip.id.in_([i["clip_id"] for i in items]),
                                                          VideoClip.world_id == gs.world_id).all()}
    return [video_dict(by_id[i["clip_id"]], i["uid"]) for i in items
            if i["clip_id"] in by_id and (is_gm or by_id[i["clip_id"]].visible_to_players)]


def attached_images(gs, *, is_gm: bool) -> list:
    return [image_dict(i) for i in media_items(gs) if i["kind"] == "image" and (is_gm or i["visible"])]


def viewer_media(db: Session, gs, *, is_gm: bool) -> dict:
    """Everything of this session the viewer may see, for the Session Log page."""
    return {"audio": attached_clips(db, gs, is_gm=is_gm), "videos": attached_videos(db, gs, is_gm=is_gm),
            "images": attached_images(gs, is_gm=is_gm)}


def sessions_with_audio_for_players(db: Session, sessions: list) -> set:
    """Ids of the sessions among `sessions` that have at least one player-audible clip (one query)."""
    wanted = {s.id: recap_clip_ids(s) for s in sessions}
    all_ids = {i for ids in wanted.values() for i in ids}
    if not all_ids:
        return set()
    audible = {row[0] for row in db.query(AudioClip.id).filter(AudioClip.id.in_(all_ids), AudioClip.visible_to_players.is_(True)).all()}
    return {sid for sid, ids in wanted.items() if audible.intersection(ids)}


def sessions_with_pictures_or_video(db: Session, sessions: list, *, is_gm: bool) -> set:
    """Ids of the sessions that have a picture or video the viewer can see (a GM: any attached one)."""
    found, video_ids = set(), {}
    for s in sessions:
        for it in media_items(s):
            if it["kind"] == "image" and (is_gm or it["visible"]):
                found.add(s.id)
            elif it["kind"] == "video":
                video_ids.setdefault(s.id, []).append(it["clip_id"])
    wanted = {cid for ids in video_ids.values() for cid in ids}
    if wanted:
        q = db.query(VideoClip.id).filter(VideoClip.id.in_(wanted))
        if not is_gm:
            q = q.filter(VideoClip.visible_to_players.is_(True))
        watchable = {row[0] for row in q.all()}
        found |= {sid for sid, ids in video_ids.items() if watchable.intersection(ids)}
    return found


# ── the GM's panel ───────────────────────────────────────────────────────────────────────────────────

def panel(db: Session, gs) -> dict:
    """Everything the GM's panel shows: what is attached (audio under `clips`, as it always was), what could be attached
    from the libraries, and how much of the limit is used."""
    audio_ids = set(recap_clip_ids(gs))
    video_ids = {i["clip_id"] for i in media_items(gs) if i["kind"] == "video"}
    audio_lib = (db.query(AudioClip).filter(AudioClip.world_id == gs.world_id).order_by(AudioClip.created_at.desc()).limit(200).all())
    video_lib = (db.query(VideoClip).filter(VideoClip.world_id == gs.world_id).order_by(VideoClip.created_at.desc()).limit(200).all())
    return {
        "clips": attached_clips(db, gs, is_gm=True),
        "library": [{"id": c.id, "name": c.name, "visible_to_players": bool(c.visible_to_players)}
                    for c in audio_lib if c.id not in audio_ids],
        "videos": attached_videos(db, gs, is_gm=True),
        "video_library": [{"id": c.id, "name": c.name, "visible_to_players": bool(c.visible_to_players)}
                          for c in video_lib if c.id not in video_ids],
        "images": attached_images(gs, is_gm=True),
        "max": limit(db),
        "used": used(gs),
    }


# ── files that belong to a session ───────────────────────────────────────────────────────────────────

def _upload_path(url: str) -> Optional[Path]:
    """The file an /uploads/... URL names, or None when it is not a local file inside the uploads folder."""
    if not isinstance(url, str) or not url.startswith("/uploads/"):
        return None
    root = media_albums.UPLOADS_DIR.resolve()
    try:
        path = (root / url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def delete_picture_file(url: str) -> None:
    """Remove a picture uploaded for a session, and its small preview, when there is nothing else to protect."""
    path = _upload_path(url)
    if not path:
        return
    try:
        from .imaging import thumbnail_path_for
        thumb = thumbnail_path_for(path)
        path.unlink()
        thumb.unlink(missing_ok=True)
    except OSError:
        _log.warning("could not delete session picture %s", url, exc_info=True)


def delete_own_pictures(gs) -> None:
    """The pictures uploaded for this session go with it; pictures picked from the gallery stay in the gallery."""
    for it in media_items(gs):
        if it["kind"] == "image" and it["own"]:
            delete_picture_file(it["url"])
