"""Audio on a session recap — a folk song about the session, a read-aloud of the recap.

The files live in the /audio library (AudioClip), so albums, the "players can hear this" switch and the
lyrics (AudioClip.transcript) all keep working; a session just remembers WHICH clips go with its recap
(GameSession.recap_audio_json, a list of clip ids, in order). The GM can attach a clip from the library,
upload a new one (a song made elsewhere), or have Studio text-to-speech perform lyrics (the sessions
page's "Turn into folk tale/song" writes them) with a delivery style such as "sea shanty". Players hear
only the attached clips marked visible, on the Session Log page.

Writes need the Sessions section's edit level (GM, or an assistant the world allows) — checked in each
handler, because the route allowlist alone admits every assistant."""
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from .. import ai as _ai
from .. import unsloth_extras as _unsloth_extras
from ..database import get_db
from ..deps import get_world_ctx, world_can_edit_section
from ..models import AudioClip, GameSession
from ..uploads import copy_upload_bounded, unique_upload_filename
from .audio import _ALLOWED_EXTS, _MAX_CLIPS_PER_WORLD, _MAX_DESCRIPTION, _MAX_NAME, _effective_audio_bytes

router = APIRouter()

MAX_RECAP_CLIPS = 6
MAX_LYRICS = 20000
_UPLOADS = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"

# How a song is delivered. Studio's TTS takes a free-text style, so the "singing" is a request, not a
# guarantee — which model is loaded decides how melodic it gets.
SONG_STYLES = {
    "folk song": "a warm, unhurried folk ballad",
    "sea shanty": "a rousing sea shanty with a strong, steady beat, as if sung by a crew hauling rope",
    "ballad": "a slow, sorrowful ballad",
    "lament": "a quiet, mournful lament",
    "drinking song": "a rowdy, cheerful tavern drinking song",
    "lullaby": "a soft, gentle lullaby",
    "hymn": "a solemn, resonant hymn",
}


def _require_gm_edit(request: Request, db: Session, session_id: int, active_world):
    """(world, session) the caller may change; 403 for a player, 404 for a session of another world."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_edit_section(request, world, "sessions"):
        raise HTTPException(403)
    gs = db.get(GameSession, session_id)
    if not gs or gs.world_id != world.id:
        raise HTTPException(404)
    return world, gs


def recap_clip_ids(gs) -> list:
    """The session's attached clip ids as a clean, de-duplicated list of ints."""
    try:
        raw = json.loads(getattr(gs, "recap_audio_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, int) and not isinstance(v, bool) and v not in out:
            out.append(v)
    return out


def _store(gs, ids: list) -> None:
    gs.recap_audio_json = json.dumps(ids[:MAX_RECAP_CLIPS])


def _clip_dict(c: AudioClip) -> dict:
    return {"id": c.id, "name": c.name, "description": c.description or "", "file_url": c.file_url,
            "visible_to_players": bool(c.visible_to_players), "transcript": c.transcript or ""}


def attached_clips(db: Session, gs, *, is_gm: bool) -> list:
    """The clips attached to this session, in order, that the viewer may hear: a GM sees them all, a
    player only the ones marked visible. Ids whose clip is gone are skipped."""
    ids = recap_clip_ids(gs)
    if not ids:
        return []
    by_id = {c.id: c for c in db.query(AudioClip).filter(AudioClip.id.in_(ids), AudioClip.world_id == gs.world_id).all()}
    return [_clip_dict(by_id[i]) for i in ids if i in by_id and (is_gm or by_id[i].visible_to_players)]


def sessions_with_audio_for_players(db: Session, sessions: list) -> set:
    """Ids of the sessions among `sessions` that have at least one player-audible clip (one query)."""
    wanted = {s.id: recap_clip_ids(s) for s in sessions}
    all_ids = {i for ids in wanted.values() for i in ids}
    if not all_ids:
        return set()
    audible = {row[0] for row in db.query(AudioClip.id).filter(AudioClip.id.in_(all_ids), AudioClip.visible_to_players.is_(True)).all()}
    return {sid for sid, ids in wanted.items() if audible.intersection(ids)}


def _panel(db: Session, gs) -> dict:
    ids = set(recap_clip_ids(gs))
    library = (db.query(AudioClip).filter(AudioClip.world_id == gs.world_id).order_by(AudioClip.created_at.desc()).limit(200).all())
    return {"clips": attached_clips(db, gs, is_gm=True),
            "library": [{"id": c.id, "name": c.name, "visible_to_players": bool(c.visible_to_players)}
                        for c in library if c.id not in ids],
            "max": MAX_RECAP_CLIPS}


def _attach(db: Session, gs, clip: AudioClip, visible: Optional[bool]) -> None:
    ids = recap_clip_ids(gs)
    if clip.id not in ids:
        if len(ids) >= MAX_RECAP_CLIPS:
            raise HTTPException(400, f"A recap can carry at most {MAX_RECAP_CLIPS} audio clips — remove one first.")
        ids.append(clip.id)
        _store(gs, ids)
    if visible is not None:
        clip.visible_to_players = bool(visible)
    db.commit()


@router.get("/api/sessions/{session_id}/recap-audio")
def recap_audio_list(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    _world, gs = _require_gm_edit(request, db, session_id, active_world)
    return _panel(db, gs)


@router.post("/api/sessions/{session_id}/recap-audio")
async def recap_audio_attach(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Attach a clip that is already in this world's audio library."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    cid = body.get("clip_id")
    if isinstance(cid, bool) or not isinstance(cid, int):
        raise HTTPException(400, "clip_id must be an audio clip id")
    clip = db.get(AudioClip, cid)
    if not clip or clip.world_id != world.id:
        raise HTTPException(404, "No such audio clip in this world")
    vis = body.get("visible_to_players")
    _attach(db, gs, clip, vis if isinstance(vis, bool) else None)
    return _panel(db, gs)


@router.post("/api/sessions/{session_id}/recap-audio/upload")
async def recap_audio_upload(session_id: int, request: Request, file: UploadFile = File(...), name: str = Form(""),
                             lyrics: str = Form(""), visible_to_players: str = Form(""),
                             db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Upload a new audio file (a song made elsewhere), add it to the library and attach it."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")
    if len(recap_clip_ids(gs)) >= MAX_RECAP_CLIPS:
        raise HTTPException(400, f"A recap can carry at most {MAX_RECAP_CLIPS} audio clips — remove one first.")
    if db.query(AudioClip).filter(AudioClip.world_id == world.id).count() >= _MAX_CLIPS_PER_WORLD:
        raise HTTPException(400, f"This world already has the maximum of {_MAX_CLIPS_PER_WORLD} audio clips.")
    target = _UPLOADS / "audio"
    target.mkdir(parents=True, exist_ok=True)
    dest = target / unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, dest, max_bytes=_effective_audio_bytes(db))
    clip = AudioClip(world_id=world.id, name=(name.strip()[:_MAX_NAME] or Path(file.filename).stem[:_MAX_NAME] or "Recap audio"),
                     description=f"Recap audio for {gs.title}"[:_MAX_DESCRIPTION], file_url=f"/uploads/audio/{dest.name}",
                     visible_to_players=visible_to_players.strip().lower() in ("1", "true", "on", "yes"),
                     transcript=(lyrics or "").strip()[:MAX_LYRICS])
    db.add(clip)
    db.commit()
    db.refresh(clip)
    _attach(db, gs, clip, None)
    return {**_panel(db, gs), "clip": _clip_dict(clip)}


@router.post("/api/sessions/{session_id}/recap-audio/{clip_id}/visibility")
async def recap_audio_visibility(session_id: int, clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Let players hear (or not) an attached clip — the same switch as in the audio library."""
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    if clip_id not in recap_clip_ids(gs):
        raise HTTPException(404)
    clip = db.get(AudioClip, clip_id)
    if not clip or clip.world_id != world.id:
        raise HTTPException(404)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    vis = body.get("visible_to_players") if isinstance(body, dict) else None
    if not isinstance(vis, bool):
        raise HTTPException(400, "visible_to_players must be true or false")
    clip.visible_to_players = vis
    db.commit()
    return _panel(db, gs)


@router.post("/api/sessions/{session_id}/recap-audio/{clip_id}/remove")
def recap_audio_remove(session_id: int, clip_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Detach a clip from the recap. The clip itself stays in the audio library."""
    _world, gs = _require_gm_edit(request, db, session_id, active_world)
    _store(gs, [i for i in recap_clip_ids(gs) if i != clip_id])
    db.commit()
    return _panel(db, gs)


# ── performing lyrics with Studio text-to-speech ─────────────────────────────

_MD_NOISE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)")
_STAGE = re.compile(r"\[[^\]\n]{0,40}\]|\([^)\n]{0,40}(?:chorus|repeat|x\d)[^)\n]{0,20}\)", re.IGNORECASE)


def lyrics_for_speech(text: str) -> str:
    """Lyrics as plain text for the voice: no markdown marks or [chorus]-style directions, verses kept
    apart by single blank lines."""
    t = _MD_NOISE.sub("", text or "")
    t = _STAGE.sub("", t)
    t = _EMPHASIS.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def song_instructions(style: str) -> str:
    """The delivery instruction sent with the lyrics: the chosen style, then the instance's own TTS style."""
    feel = SONG_STYLES.get((style or "").strip().lower(), SONG_STYLES["folk song"])
    base = f"Sing these lyrics as {feel}: melodic, rhythmic and expressive, as a bard would perform it, not read flatly."
    own = (_ai.get_tts_instructions() or "").strip()
    return f"{base} {own}".strip()


@router.post("/api/sessions/{session_id}/recap-song")
async def recap_song(session_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Have Studio's TTS perform lyrics as a song, save it as a library clip (GM-only until reviewed,
    lyrics kept as its transcript) and attach it to the recap. JSON: {lyrics, name?, style?, voice?,
    visible_to_players?}. Synchronous — a song is short. GM-only (not merely session-editors): it
    spends Studio's GPU on TTS and may load the TTS model, exactly what the /api/ai/tts route
    already reserves for the GM."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        raise HTTPException(403)
    world, gs = _require_gm_edit(request, db, session_id, active_world)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    lyrics = str(body.get("lyrics") or "").strip()
    if not lyrics:
        raise HTTPException(400, "No lyrics to perform")
    if len(lyrics) > MAX_LYRICS:
        raise HTTPException(400, f"Lyrics are too long (over {MAX_LYRICS} characters)")
    if len(recap_clip_ids(gs)) >= MAX_RECAP_CLIPS:
        raise HTTPException(400, f"A recap can carry at most {MAX_RECAP_CLIPS} audio clips — remove one first.")
    spoken = lyrics_for_speech(lyrics)
    if not spoken:
        raise HTTPException(400, "No lyrics to perform")
    model = _ai.get_tts_model()
    try:
        audio, content_type = await _unsloth_extras.tts(
            spoken, model=model, voice=str(body.get("voice") or "").strip() or _ai.get_tts_voice(),
            instructions=song_instructions(str(body.get("style") or "")), language=_ai.get_tts_language())
    except _unsloth_extras.StudioMissing as exc:
        raise HTTPException(400, str(exc))
    except _unsloth_extras.StudioError as exc:
        raise HTTPException(exc.status_code, f"Unsloth Studio TTS: {exc}")
    ext = ".wav" if "wav" in (content_type or "") else ".mp3"
    target = _UPLOADS / "audio"
    target.mkdir(parents=True, exist_ok=True)
    dest = target / unique_upload_filename(f"song-{uuid.uuid4().hex[:8]}{ext}", ext)
    dest.write_bytes(audio)
    vis = body.get("visible_to_players")
    clip = AudioClip(world_id=world.id, name=(str(body.get("name") or "").strip()[:_MAX_NAME] or f"Song: {gs.title}"[:_MAX_NAME]),
                     description=f"Performed lyrics for {gs.title} ({model or 'TTS'}, {time.strftime('%Y-%m-%d')})"[:_MAX_DESCRIPTION],
                     file_url=f"/uploads/audio/{dest.name}", visible_to_players=vis if isinstance(vis, bool) else False,
                     transcript=lyrics)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    _attach(db, gs, clip, None)
    return {**_panel(db, gs), "clip": _clip_dict(clip)}
