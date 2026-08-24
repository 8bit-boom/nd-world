from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File, Cookie, Query
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import Headers
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, text
from sqlalchemy.exc import OperationalError
from typing import List, Optional
from urllib.parse import quote
import asyncio
import re
import html
import logging
import os
import secrets
import uuid
import shutil
import json
import base64
import io
from pathlib import Path

from . import deps
from . import nav_menus as _nav_menus_module
from .database import init_db, get_db, SessionLocal, get_app_settings
from .deps import get_world_ctx, resolve_world_slug, with_world
from .imaging import convert_image
from .rendering import parse_stats, render_md, html_to_markdown, sanitize_note_html
from .templating import templates
from .uploads import copy_upload_bounded, read_upload_bounded, unique_upload_filename, BULK_IMAGE_MAX_FILES
from .models import Entity, World, Schematic, MapOverlay, InvestBoard, entity_links, entity_player_access, User, InviteCode, WorldMembership, PrivateNote, EntityNote, EntityTemplate, GameSession, Quest, Party, CombatSession, PlayerCharacter, RandomTable, WorldCalendar, CalendarEvent, ApiToken, ImageAlbum, AudioClip, AudioAlbum
from .routers.ai import router as ai_router
from .routers.account import router as account_router
from .routers.characters import router as characters_router
from .routers.characters import _pc_to_foundry_journal
from .routers.auth import router as auth_router
from .routers.tables import router as tables_router
from .routers.combat import router as combat_router
from .routers.combat import _candidates as _combat_candidates
from .routers.parties import router as parties_router
from .routers.quests import router as quests_router
from .routers.sessions import router as sessions_router
from .routers.calendar import router as calendar_router
from .routers.importer import router as importer_router
from .routers.races import router as races_router
from .routers.professions import router as professions_router
from .routers.lore_extras import router as lore_extras_router
from .routers.boards_generate import router as boards_generate_router
from .routers.handouts import router as handouts_router
from .routers.home_content import router as home_content_router
from .routers.export import router as export_router
from .routers.kinds_admin import router as kinds_admin_router
from .routers.facts import router as facts_router
from .routers.chronicler import router as chronicler_router
from .routers.gallery import router as gallery_router
from .routers.audio import router as audio_router
from .routers.nav_menus_admin import router as nav_menus_admin_router
from . import gallery as _gallery_module
from . import mcp_server
from . import ai as _ai_module
from . import audio_jobs as _audio_jobs
from . import auth as _auth
from .constants import KINDS, SUBTYPES, KIND_ICONS

BASE_DIR = Path(__file__).parent.parent
UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
# GM-created maps must live under /data (the persistent volume) — the app
# image itself is rebuilt/replaced on every update, so anything written to a
# path inside the app source tree (like the old `app/maps/`) is silently
# wiped the next time the container is recreated from a fresh image.
_MAPS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "maps"
# The maps bundled with the repo (default city maps) — read-only reference
# copies used to seed _MAPS_DIR on first boot, or when a new one is added.
_BUNDLED_MAPS_DIR = Path(__file__).parent / "maps"
SWARMUI_EXTERNAL_URL = os.getenv("SWARMUI_EXTERNAL_URL", "").rstrip("/")
ANDROID_EMULATOR_URL = os.getenv("ANDROID_EMULATOR_URL", "").rstrip("/")
EDITOR_EXTERNAL_URL = os.getenv("EDITOR_EXTERNAL_URL", "").rstrip("/")

app = FastAPI(title="N&D World")
_allowed = [h.strip() for h in os.getenv("ND_ALLOWED_HOSTS", "*").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed)
app.include_router(ai_router)
app.include_router(account_router)
app.include_router(characters_router)
app.include_router(auth_router)
app.include_router(tables_router)
app.include_router(combat_router)
app.include_router(parties_router)
app.include_router(quests_router)
app.include_router(sessions_router)
app.include_router(calendar_router)
app.include_router(importer_router)
app.include_router(races_router)
app.include_router(professions_router)
app.include_router(lore_extras_router)
app.include_router(boards_generate_router)
app.include_router(handouts_router)
app.include_router(home_content_router)
app.include_router(export_router)
app.include_router(kinds_admin_router)
app.include_router(facts_router)
app.include_router(chronicler_router)
app.include_router(gallery_router)
app.include_router(audio_router)
app.include_router(nav_menus_admin_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
SCHEMATICS_STATIC_DIR = BASE_DIR / "static" / "schematics"

# KINDS, SUBTYPES, KIND_ICONS imported from .constants
# Raster only, deliberately. SVG is excluded because it can contain <script> and is
# served from this app's own origin — imaging.convert_image can't neutralize it either
# (re-encoding a vector would rasterize it). Every raster format here is auto-converted
# to AVIF/WebP on upload, so dropping SVG costs little. AVIF is included as an accepted
# *source* format too (not just a conversion target) — Pillow already decodes it
# everywhere else in this app (imaging.py's _CONVERTIBLE_EXTS, the bulk converter), and
# the "Choose from Gallery" picker (static/js/gallery-picker.js) re-uploads an existing
# gallery image through these same routes, which is very often already AVIF (the
# default conversion target) by the time it's picked.
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
ENTITY_COLS = {"kind", "subtype", "folder", "name", "tags", "summary", "body", "image_url", "world_id"}

def _refresh_settings_overrides(db: Session = None):
    """Push AppSettings' System/Integrations fields into app.ai's in-memory Ollama
    overrides. Called at boot and again after every POST /settings/system save, so
    a saved override takes effect without a container restart."""
    owns = db is None
    if owns:
        db = SessionLocal()
    try:
        settings = get_app_settings(db)
        _ai_module.set_ollama_override(settings.ollama_url or "", settings.ollama_model or "")
        _ai_module.set_whisper_override(settings.whisper_url or "")
    finally:
        if owns:
            db.close()


@app.on_event("startup")
def startup():
    init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _seed_bundled_maps()
    _refresh_settings_overrides()
    _audio_jobs.sweep_interrupted_jobs()




def _seed_bundled_maps():
    """Copy any bundled default map (app/maps/*.json) into the persistent maps
    directory if it isn't there yet — never overwrites an existing file, so
    this is safe to run on every startup and only fills in what's missing
    (first boot, or a new bundled map added in a later update)."""
    if not _BUNDLED_MAPS_DIR.exists():
        return
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for jf in _BUNDLED_MAPS_DIR.glob("*.json"):
        dest = _MAPS_DIR / jf.name
        if not dest.exists():
            shutil.copyfile(jf, dest)


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Auth gate ──────────────────────────────────────────────────────────────────
# Everything requires login by default. Non-GM (player) accounts are additionally
# restricted to a explicit allowlist of "player-safe" paths — world/lore browsing
# (read-only, further filtered to visible_to_players inside the handlers) and their
# own character(s). Anything not allowlisted is GM-only. New routes are therefore
# GM-only by default unless deliberately added to _is_player_safe.

_PUBLIC_PATHS = {"/login", "/login/2fa", "/api/login", "/logout", "/health", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/join/", "/static/")


def _is_player_safe(method: str, path: str) -> bool:
    if path.startswith("/characters/templates"):
        return False
    if path.startswith("/characters") or path.startswith("/api/characters/"):
        return True
    if path == "/api/me":
        return True
    if path == "/api/hover-preview/config":
        return True
    if path == "/api/spotlight":
        return True
    if re.match(r"^/api/entity/\d+/preview$", path):
        return True
    if path == "/account" or path.startswith("/account/"):
        return True
    if re.match(r"^/api/worlds/\d+/characters/sync$", path):
        return True
    if re.match(r"^/api/worlds/\d+/content-pack$", path):
        return True
    if re.match(r"^/api/maps/schematic/[^/]+/move-token$", path):
        return True
    if re.match(r"^/api/maps/schematic/[^/]+/pickup-item$", path):
        return True
    if re.match(r"^/api/maps/schematic/[^/]+/buy-item$", path):
        return True
    if path == "/api/chronicler/ask":
        return True
    if path == "/api/ai/stream":
        # Middleware only decides "reachable" — the handler (app/routers/ai.py)
        # still gates a non-GM caller behind World.players_can_ask_ai, off by
        # default. The dedicated GM "/ai" World Chat page is a GET route and
        # stays off this allowlist entirely, so this only opens the shared
        # streaming endpoint, not that page's GM-only quick-prompt toolkit.
        return True
    if path in (
        "/api/ai/attachments/upload",
        "/api/ai/attachments/upload/chunk",
        "/api/ai/attachments/upload/complete",
        "/api/ai/attachments/audio-jobs",
        "/api/ai/attachments/audio-jobs/chunk",
        "/api/ai/attachments/audio-jobs/complete",
    ):
        # Same shape as /api/ai/stream above — a player attaching a file to
        # a chat message needs this reachable too (including a large one
        # split into parts by ndChunkedUpload, or one processed as a durable
        # background job instead of a blocking request), and the handler
        # applies the identical players_can_ask_ai gate before accepting
        # anything.
        return True
    if re.match(r"^/api/ai/attachments/audio-jobs/\d+$", path):
        return True
    if re.match(r"^/api/session-log/\d+/recap$", path):
        return True
    if method != "GET":
        return False
    if path in ("/", "/rules", "/rules/download.md", "/search", "/maps", "/races", "/professions", "/androidapp", "/chronicler", "/session-log", "/audio"):
        return True
    if path.startswith("/kind/") or path.startswith("/uploads/"):
        return True
    if re.match(r"^/audio/albums/\d+$", path):
        return True
    if re.match(r"^/entity/\d+(/download\.md)?$", path):
        return True
    if re.match(r"^/session-log/\d+$", path):
        return True
    if path.startswith("/maps/") and not (path == "/maps/schematic" or path.startswith("/maps/schematic/")) and path != "/maps/new":
        return True
    if re.match(r"^/maps/schematic/[^/]+/view(\.json)?$", path):
        return True
    if path.startswith("/worlds/switch/"):
        return True
    if re.match(r"^/worlds/\d+/notes/\d+$", path):
        return True
    return False


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    # /mcp is deliberately NOT handled here — see ASGI_APP at the bottom of
    # this file. auth_gate is @app.middleware("http") (BaseHTTPMiddleware
    # under the hood), which bridges every request/response through its own
    # background task; that breaks the streamable-http transport's own
    # task-group-based streaming underneath it (cancel-scope errors —
    # confirmed by reproducing it directly, not a guess) regardless of what
    # this function's body does for that path, since the wrapping happens at
    # the middleware-registration level before this dispatch function ever
    # runs. /mcp requests are routed around this entire middleware stack
    # instead, authenticated by their own bearer-token check.

    user_id = request.session.get("user_id")
    if not user_id:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Login required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()
    if not user:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    if request.session.get("session_version") != user.session_version:
        # Either this session predates session_version existing, or a password
        # change elsewhere bumped it — either way, the cookie no longer represents
        # a currently-valid session.
        request.session.clear()
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Session expired — please log in again."}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)

    if not user.is_gm and not _is_player_safe(request.method, path):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "GM access required"}, status_code=403)
        return HTMLResponse(
            "<body style='background:#0a0a0f;color:#c8d0e0;font-family:monospace;padding:2rem'>"
            "<h1 style='color:#ff2d78'>403 — GM access required</h1>"
            "<p><a href='/' style='color:#00f0ff'>&larr; Back</a></p></body>",
            status_code=403,
        )

    request.state.user = user
    return await call_next(request)


# SessionMiddleware must be added AFTER auth_gate above (Starlette wraps middleware
# in reverse-of-addition order, so the *last* added middleware runs *first* per
# request — this makes SessionMiddleware run before auth_gate, so request.session
# is already populated when auth_gate reads it).
_log = logging.getLogger("nd.main")
_env_secret_key = os.environ.get("SECRET_KEY")
if not _env_secret_key:
    _log.warning(
        "SECRET_KEY is not set — falling back to a random per-process key. Every "
        "logged-in session will be invalidated on the next restart. Set SECRET_KEY "
        "in the environment (e.g. docker-compose) to avoid this."
    )
SECRET_KEY = _env_secret_key or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() == "true"
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=COOKIE_SECURE, same_site="lax")


def _rules_toc(html: str):
    toc = []
    def _repl(m):
        lvl, inner = m.group(1), m.group(2)
        text = re.sub(r'<[^>]+>', '', inner)
        slug = re.sub(r'[^\w]+', '-', text.lower()).strip('-') or 'sec'
        toc.append({'level': int(lvl), 'text': text, 'id': slug})
        return f'<h{lvl} id="{slug}">{inner}</h{lvl}>'
    html = re.sub(r'<h([23])>(.*?)</h\1>', _repl, html, flags=re.DOTALL)
    return html, toc

def save_upload(file: UploadFile, subdir: str = "", db: Optional[Session] = None):
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    filename = unique_upload_filename(file.filename, ext)
    target_dir = UPLOADS_DIR / subdir if subdir else UPLOADS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    copy_upload_bounded(file, dest)
    if db is not None:
        settings = get_app_settings(db)
        dest = convert_image(dest, static_format=settings.static_format,
                              animated_format=settings.animated_format)
    else:
        dest = convert_image(dest)
    url_path = f"/uploads/{subdir}/{dest.name}" if subdir else f"/uploads/{dest.name}"
    return url_path

# ── World helpers ─────────────────────────────────────────────────────────────

DEFAULT_WORLD_COOKIE = "active_world"

def get_active_world(request: Request, db: Session, active_world: str = Cookie(None)) -> World:
    active_world = resolve_world_slug(request, active_world)
    user = getattr(request.state, "user", None)
    accessible = _auth.accessible_world_ids(db, user)  # None = GM (all worlds)

    if active_world:
        w = db.query(World).filter(World.slug == active_world).first()
        if w and (accessible is None or w.id in accessible):
            return w

    q = db.query(World)
    if accessible is not None:
        if not accessible:
            return None
        q = q.filter(World.id.in_(accessible))
    return q.order_by(World.id).first()


def _filter_visible_entities(q, request: Request):
    """Restrict an Entity query to visible_to_players rows for non-GM viewers,
    plus any hidden entities specifically shared with this player."""
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        if user:
            shared = q.session.query(entity_player_access.c.entity_id).filter(
                entity_player_access.c.user_id == user.id
            )
            q = q.filter(or_(Entity.visible_to_players.isnot(False), Entity.id.in_(shared)))
        else:
            q = q.filter(Entity.visible_to_players.isnot(False))
    return q


def _kind_counts(db: Session, world: Optional[World], request: Request) -> dict:
    """Entity count per kind for one world, as a single GROUP BY query instead
    of one COUNT(*) per kind (the homepage and /ai page both used to run 8
    separate queries — one per KINDS entry — to build this same dict).
    Seeds every kind (including this world's custom ones) at 0 so a
    freshly-added custom kind's home stat tile shows "0" immediately
    instead of being absent from the dict."""
    world_id = world.id if world else None
    q = _filter_visible_entities(
        db.query(Entity.kind, func.count(Entity.id)).filter(Entity.world_id == world_id),
        request,
    )
    rows = q.group_by(Entity.kind).all()
    counts = {k: 0 for k in deps.effective_kinds(world)[0]}
    counts.update({k: c for k, c in rows})
    return counts


def _kind_folders(db: Session, world_id: int, kind: str):
    """Distinct folder paths already in use for this world+kind, for the folder
    picker on the entity form (existing folders to choose from)."""
    rows = db.query(Entity.folder).filter(
        Entity.world_id == world_id, Entity.kind == kind, Entity.folder.isnot(None)
    ).distinct().all()
    return sorted({r[0] for r in rows if r[0]})


def _world_player_list(db: Session, world_id: int):
    """Player accounts with membership in this world, for the per-entity visibility checklist."""
    return (
        db.query(User)
        .join(WorldMembership, WorldMembership.user_id == User.id)
        .filter(WorldMembership.world_id == world_id)
        .order_by(User.display_name)
        .all()
    )


def _sync_entity_access(db: Session, entity_id: int, player_ids):
    """Replace the set of players explicitly allowed to see a hidden entity."""
    db.execute(entity_player_access.delete().where(entity_player_access.c.entity_id == entity_id))
    for uid in {int(uid) for uid in player_ids}:
        db.execute(entity_player_access.insert().values(entity_id=entity_id, user_id=uid))
    db.commit()


def _entity_templates_for(db: Session, world_id: int, kind: str = None):
    """Templates visible to this world (global + world-scoped), optionally
    narrowed to ones usable on a given entity kind (a template with kind=NULL
    is usable on any kind)."""
    q = db.query(EntityTemplate).filter(
        (EntityTemplate.world_id.is_(None)) | (EntityTemplate.world_id == world_id)
    )
    if kind:
        q = q.filter((EntityTemplate.kind.is_(None)) | (EntityTemplate.kind == kind))
    return q.order_by(EntityTemplate.is_builtin.desc(), EntityTemplate.name).all()


def _entity_templates_payload(db: Session, world_id: int):
    """JSON-serializable form of _entity_templates_for, for the entity
    form's Jinja dropdown loop and its embedded JS data in one shot."""
    return [
        {"id": t.id, "name": t.name, "kind": t.kind, "fields": json.loads(t.fields_json or "[]")}
        for t in _entity_templates_for(db, world_id)
    ]


def _group_by_section(tpl_fields):
    """Groups template fields by their `section` label, preserving both field
    order and first-seen section order (Jinja's groupby filter re-sorts
    alphabetically, which would scramble an intentionally-ordered layout)."""
    sections, lookup = [], {}
    for f in tpl_fields:
        sec = f.get("section") or "Custom"
        if sec not in lookup:
            lookup[sec] = []
            sections.append((sec, lookup[sec]))
        lookup[sec].append(f)
    return sections


def _visible_worlds(request: Request, db: Session):
    """Worlds list for the nav world-switcher — GMs see all worlds, players only
    see worlds they've been invited to (so world names/existence aren't leaked)."""
    user = getattr(request.state, "user", None)
    accessible = _auth.accessible_world_ids(db, user)
    if accessible is None:
        return db.query(World).order_by(World.id).all()
    if not accessible:
        return []
    return db.query(World).filter(World.id.in_(accessible)).order_by(World.id).all()

# ── Uploads ───────────────────────────────────────────────────────────────────

@app.get("/uploads/{filepath:path}")
def serve_upload(filepath: str):
    # Containment check: without it, `filepath="../world.db"` escapes UPLOADS_DIR
    # (/data/uploads) and serves the SQLite database one level up — every password
    # hash and all GM-only content — to any logged-in account, since _is_player_safe
    # allows all GETs under /uploads/. Resolving both sides also closes the
    # symlink-escape variant, because FileResponse follows symlinks.
    root = UPLOADS_DIR.resolve()
    try:
        path = (root / filepath).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(404)
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404)
    headers = {"X-Content-Type-Options": "nosniff"}
    # SVG can carry <script>, and it's served from this app's own origin. New SVG
    # uploads are rejected outright (see ALLOWED_EXTS), but files uploaded before
    # that change still exist on disk — force them to download instead of render.
    if path.suffix.lower() == ".svg":
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(path, headers=headers)

# ── Worlds management ─────────────────────────────────────────────────────────

_ACCENT_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

def _sanitize_accent(value: str, fallback: str = "#00f0ff") -> str:
    # world.accent is interpolated directly into a <style> block in base.html
    # (see the world-switcher/world-card CSS custom properties too) — reject
    # anything that isn't a plain hex color so a malformed/malicious value
    # can never break out of that declaration.
    value = (value or "").strip()
    return value if _ACCENT_HEX_RE.match(value) else fallback

@app.get("/worlds", response_class=HTMLResponse)
def worlds_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    worlds = _visible_worlds(request, db)
    current = get_active_world(request, db, active_world)
    return templates.TemplateResponse("worlds.html", {"request": request, "worlds": worlds, "current": current})

@app.post("/worlds/new")
def world_create(
    name: str = Form(...),
    description: str = Form(""),
    accent: str = Form("#00f0ff"),
    db: Session = Depends(get_db),
):
    slug = name.lower().replace(" ", "-").replace("&", "and")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    w = World(name=name, slug=slug, description=description or None, accent=_sanitize_accent(accent))
    db.add(w)
    db.commit()
    db.refresh(w)
    resp = RedirectResponse("/worlds", status_code=303)
    resp.set_cookie(DEFAULT_WORLD_COOKIE, w.slug, max_age=60*60*24*365)
    return resp

@app.post("/worlds/{world_id}/delete")
def world_delete(world_id: int, db: Session = Depends(get_db)):
    """Deletes every row and file this world owns before removing the World
    row itself. Previously just did db.delete(w) — SQLite's foreign_keys
    pragma is never turned on (see database.py) and World has no ORM cascade
    configured for most of its child tables (only Entity does), so every
    PlayerCharacter, Schematic, WorldMembership, InviteCode, CombatSession,
    Party, Quest, GameSession, PrivateNote, InvestBoard, RandomTable,
    WorldCalendar/CalendarEvent row — plus every filesystem-backed Map JSON
    file, its MapOverlay row, and every uploaded map/schematic image — was
    silently orphaned forever on every world delete.

    Deliberately out of scope: uploaded Entity/PlayerCharacter/Party
    portrait images. Those use a flat /uploads/{filename} naming scheme
    (unlike maps/schematics, which are already carefully cleaned up
    elsewhere in this file) that isn't safely reversible from just the DB
    row without real risk of deleting the wrong file — a pre-existing,
    broader orphaned-upload problem that predates and outlives world-delete
    specifically."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)

    entity_ids = [row[0] for row in db.query(Entity.id).filter(Entity.world_id == world_id).all()]
    if entity_ids:
        db.query(EntityNote).filter(EntityNote.entity_id.in_(entity_ids)).delete(synchronize_session=False)
        db.execute(entity_links.delete().where(
            entity_links.c.source_id.in_(entity_ids) | entity_links.c.target_id.in_(entity_ids)
        ))
        db.execute(entity_player_access.delete().where(entity_player_access.c.entity_id.in_(entity_ids)))

    for s in db.query(Schematic).filter(Schematic.world_id == world_id).all():
        _delete_schematic_files(s)

    # Unlike ImageAlbum (URLs that may be shared/reused elsewhere), each
    # AudioClip row owns exactly one file, so it's always safe to delete here.
    for clip in db.query(AudioClip).filter(AudioClip.world_id == world_id).all():
        if clip.file_url and clip.file_url.startswith("/uploads/"):
            p = (UPLOADS_DIR / clip.file_url[len("/uploads/"):]).resolve()
            if p.is_relative_to(UPLOADS_DIR.resolve()) and p.is_file():
                p.unlink()

    for slug, _data in list(_iter_world_maps(world_id)):
        jf = _MAPS_DIR / f"{slug}.json"
        if jf.exists():
            jf.unlink()
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
            img = UPLOADS_DIR / "maps" / (slug + ext)
            if img.exists():
                img.unlink()
        db.query(MapOverlay).filter(MapOverlay.slug == slug).delete(synchronize_session=False)

    for model in (Entity, PlayerCharacter, Schematic, WorldMembership, InviteCode, PrivateNote,
                  InvestBoard, RandomTable, CombatSession, Party, Quest, GameSession,
                  WorldCalendar, CalendarEvent, ImageAlbum, AudioClip, AudioAlbum):
        db.query(model).filter(model.world_id == world_id).delete(synchronize_session=False)

    db.delete(w)
    db.commit()
    resp = RedirectResponse("/worlds", status_code=303)
    resp.delete_cookie(DEFAULT_WORLD_COOKIE)
    return resp

@app.get("/worlds/switch/{slug}")
def world_switch(slug: str, request: Request, next: str = "/", db: Session = Depends(get_db)):
    w = db.query(World).filter(World.slug == slug).first()
    if not w or not _auth.user_can_access_world(db, getattr(request.state, "user", None), w):
        raise HTTPException(404)
    dest = _auth.safe_next_url(next)
    resp = RedirectResponse(with_world(dest, w), status_code=303)
    resp.set_cookie(DEFAULT_WORLD_COOKIE, slug, max_age=60*60*24*365)
    return resp

@app.get("/worlds/{world_id}/edit", response_class=HTMLResponse)
def world_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)
    invites = db.query(InviteCode).filter(InviteCode.world_id == world_id).order_by(InviteCode.created_at.desc()).all()
    members = (
        db.query(WorldMembership, User)
        .join(User, User.id == WorldMembership.user_id)
        .filter(WorldMembership.world_id == world_id)
        .order_by(User.display_name)
        .all()
    )
    return templates.TemplateResponse("world_edit.html", {
        "request": request, "world": world, "worlds": worlds,
        "edit_world": w, "kinds": KINDS, "kind_icons": KIND_ICONS,
        "invites": invites, "members": members,
    })

@app.post("/worlds/{world_id}/edit")
def world_edit_post(
    world_id: int,
    name: str = Form(...),
    description: str = Form(""),
    accent: str = Form("#00f0ff"),
    players_see_party: Optional[str] = Form(None),
    players_can_download_rules: Optional[str] = Form(None),
    players_can_download_entities: Optional[str] = Form(None),
    players_can_ask_ai: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    w.name = name.strip() or w.name
    w.description = description
    w.accent = _sanitize_accent(accent, fallback=w.accent)
    w.players_see_party = bool(players_see_party)
    w.players_can_download_rules = bool(players_can_download_rules)
    w.players_can_download_entities = bool(players_can_download_entities)
    w.players_can_ask_ai = bool(players_can_ask_ai)
    db.commit()
    return RedirectResponse("/worlds", status_code=303)


# ── Invites & Members ──────────────────────────────────────────────────────────

@app.post("/worlds/{world_id}/invites/new")
def invite_create(
    world_id: int,
    request: Request,
    expires_days: str = Form(""),
    max_uses: str = Form(""),
    db: Session = Depends(get_db),
):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    from datetime import datetime as _dt, timedelta
    expires_at = None
    if expires_days.strip().isdigit() and int(expires_days) > 0:
        expires_at = _dt.utcnow() + timedelta(days=int(expires_days))
    invite = InviteCode(
        code=_auth.generate_invite_code(),
        world_id=world_id,
        created_by_id=user.id if user else None,
        expires_at=expires_at,
        max_uses=int(max_uses) if max_uses.strip().isdigit() and int(max_uses) > 0 else None,
    )
    db.add(invite)
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)


@app.post("/worlds/{world_id}/invites/{invite_id}/revoke")
def invite_revoke(world_id: int, invite_id: int, db: Session = Depends(get_db)):
    invite = db.query(InviteCode).filter(InviteCode.id == invite_id, InviteCode.world_id == world_id).first()
    if not invite:
        raise HTTPException(404)
    invite.revoked = True
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)


@app.post("/worlds/{world_id}/members/{user_id}/remove")
def member_remove(world_id: int, user_id: int, db: Session = Depends(get_db)):
    m = db.query(WorldMembership).filter(
        WorldMembership.world_id == world_id, WorldMembership.user_id == user_id
    ).first()
    if m:
        db.delete(m)
        db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)


# ── Private Notes (GM ↔ one player) ─────────────────────────────────────────────

@app.get("/worlds/{world_id}/notes/{user_id}", response_class=HTMLResponse)
def private_notes_view(
    world_id: int, user_id: int, request: Request,
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    viewer = getattr(request.state, "user", None)
    is_gm = bool(viewer and viewer.is_gm)
    if not is_gm and (not viewer or viewer.id != user_id or not _auth.user_can_access_world(db, viewer, w)):
        raise HTTPException(403)
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(404)
    notes = (
        db.query(PrivateNote)
        .filter(PrivateNote.world_id == world_id, PrivateNote.player_user_id == user_id)
        .order_by(PrivateNote.created_at.desc())
        .all()
    )
    worlds = _visible_worlds(request, db)
    return templates.TemplateResponse("private_notes.html", {
        "request": request, "world": w, "worlds": worlds,
        "target_user": target_user, "notes": notes, "can_manage": is_gm,
    })


@app.post("/worlds/{world_id}/notes/{user_id}/new")
def private_note_create(
    world_id: int, user_id: int, request: Request,
    title: str = Form(""), content: str = Form(...),
    db: Session = Depends(get_db),
):
    if not db.get(World, world_id) or not db.get(User, user_id):
        raise HTTPException(404)
    author = getattr(request.state, "user", None)
    note = PrivateNote(
        world_id=world_id, player_user_id=user_id, author_id=author.id if author else None,
        title=title.strip(), content=content,
    )
    db.add(note)
    db.commit()
    return RedirectResponse(f"/worlds/{world_id}/notes/{user_id}", status_code=303)


@app.post("/worlds/{world_id}/notes/{user_id}/{note_id}/delete")
def private_note_delete(world_id: int, user_id: int, note_id: int, db: Session = Depends(get_db)):
    note = db.query(PrivateNote).filter(
        PrivateNote.id == note_id, PrivateNote.world_id == world_id, PrivateNote.player_user_id == user_id
    ).first()
    if note:
        db.delete(note)
        db.commit()
    return RedirectResponse(f"/worlds/{world_id}/notes/{user_id}", status_code=303)

@app.post("/folders/rename")
def folder_rename(
    request: Request,
    kind: str = Form(...),
    old_path: str = Form(...),
    new_path: str = Form(""),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    old_path = old_path.strip()
    new_path = new_path.strip()
    if not world or not old_path:
        raise HTTPException(400)
    # Scoped to this kind — folders are namespaced per entity kind, so renaming
    # "Locations/East" must never touch an unrelated "Items/East" folder.
    ents = db.query(Entity).filter(
        Entity.world_id == world.id, Entity.kind == kind,
        or_(Entity.folder == old_path, Entity.folder.like(old_path + "/%"))
    ).all()
    for e in ents:
        if not new_path:
            # Blank new_path = delete/ungroup the folder — its entities (and any
            # subfolder's) become Unfiled, rather than trying to guess where else
            # a half-renamed path should live.
            e.folder = None
        elif e.folder == old_path:
            e.folder = new_path
        else:
            e.folder = new_path + e.folder[len(old_path):]
    db.commit()
    redirect_url = f"/kind/{kind}"
    if new_path:
        redirect_url += f"?folder={quote(new_path)}"
    return RedirectResponse(redirect_url, status_code=303)

@app.get("/worlds/{world_id}/export")
def world_export(world_id: int, db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    entities = db.query(Entity).filter(Entity.world_id == world_id).all()
    export_entities = []
    for e in entities:
        d = {
            "name": e.name, "kind": e.kind, "subtype": e.subtype,
            "folder": e.folder, "tags": e.tags, "summary": e.summary,
            "body": e.body, "image_url": e.image_url, "image_data": None,
        }
        # Embed local uploaded images as base64
        if e.image_url and e.image_url.startswith("/uploads/"):
            img_path = UPLOADS_DIR / Path(e.image_url).name
            if img_path.exists():
                ext = img_path.suffix.lower().lstrip(".")
                d["image_data"] = f"data:image/{ext};base64," + base64.b64encode(img_path.read_bytes()).decode()
        export_entities.append(d)
    payload = json.dumps({
        "world": {"name": w.name, "slug": w.slug, "description": w.description, "accent": w.accent},
        "entities": export_entities,
    }, ensure_ascii=False, indent=2)
    filename = f"{w.slug}-export.json"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/worlds/{world_id}/import")
async def world_import(world_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    raw = await file.read()
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON file")
    entities = data.get("entities", [])
    created = updated = 0
    for item in entities:
        name = (item.get("name") or "").strip()
        kind = item.get("kind", "note")
        if not name:
            continue
        # Restore embedded image
        image_url = item.get("image_url")
        img_data = item.get("image_data")
        if img_data and img_data.startswith("data:image/"):
        # parse data URI
            header, b64 = img_data.split(",", 1)
            ext = header.split("/")[1].split(";")[0]
            ext = "." + ext if not ext.startswith(".") else ext
            if ext not in ALLOWED_EXTS:
                ext = ".jpg"
            # No original upload filename survives in a full-fidelity backup's
            # embedded data: URI — the entity's own name is the best available
            # label for the restored file.
            filename = unique_upload_filename(name, ext)
            (UPLOADS_DIR / filename).write_bytes(base64.b64decode(b64))
            image_url = f"/uploads/{filename}"
        existing = db.query(Entity).filter(
            Entity.name == name, Entity.kind == kind, Entity.world_id == world_id
        ).first()
        if existing:
            existing.subtype  = item.get("subtype") or existing.subtype
            existing.folder   = item.get("folder")  or existing.folder
            existing.tags     = item.get("tags")     or existing.tags
            existing.summary  = item.get("summary")  or existing.summary
            existing.body     = item.get("body")     or existing.body
            if image_url:
                existing.image_url = image_url
            updated += 1
        else:
            db.add(Entity(
                name=name, kind=kind, world_id=world_id,
                subtype=item.get("subtype"), folder=item.get("folder"),
                tags=item.get("tags"), summary=item.get("summary"),
                body=item.get("body"), image_url=image_url,
            ))
            created += 1
    db.commit()
    return RedirectResponse(f"/export?w={w.slug}&imported={created}&updated={updated}", status_code=303)

# ── Home ──────────────────────────────────────────────────────────────────────

def _resolve_home_link_href(db: Session, world: World, link: dict, is_gm: bool) -> Optional[str]:
    """A stored home-page link -> a live href, or None if its target no
    longer exists / doesn't belong to this world (deleted since the link was
    added) — the caller drops it silently in that case. See
    app/routers/home_content.py's _sanitize_link for what shapes a stored
    link can take; this only has to handle already-sanitized entries."""
    target_type = link.get("target_type")
    ref = link.get("target_ref") or ""
    try:
        if target_type == "entity":
            e = db.get(Entity, int(ref))
            return f"/entity/{e.id}" if e and e.world_id == world.id else None
        if target_type == "session":
            s = db.get(GameSession, int(ref))
            return f"/sessions/{s.id}" if s and s.world_id == world.id else None
        if target_type == "quest":
            q = db.get(Quest, int(ref))
            return f"/quests/{q.id}" if q and q.world_id == world.id else None
    except (TypeError, ValueError):
        return None
    if target_type == "board":
        b = db.query(InvestBoard).filter(InvestBoard.slug == ref, InvestBoard.world_id == world.id).first()
        return f"/boards/{b.slug}" if b else None
    if target_type == "schematic":
        s = db.query(Schematic).filter(Schematic.slug == ref, Schematic.world_id == world.id).first()
        if not s:
            return None
        # GM gets the editor, players get the real read-only view route —
        # /maps/schematic/{slug} (no suffix) is GM-only.
        return f"/maps/schematic/{s.slug}" if is_gm else f"/maps/schematic/{s.slug}/view"
    if target_type == "map":
        jf = _MAPS_DIR / f"{ref}.json"
        data = _map_data(jf) if jf.exists() else None
        return f"/maps/{ref}" if data and data.get("world_id", 1) == world.id else None
    if target_type == "kind":
        return f"/kind/{ref}" if ref in deps.effective_kinds(world)[0] else None
    if target_type == "url":
        if ref.startswith("http://") or ref.startswith("https://") or ref.startswith("/"):
            return ref
        return None
    return None


def _resolve_home_sections(db: Session, world: World, request: Request) -> list[dict]:
    """world.home_sections_json -> renderable sections for index.html:
    [{name, links: [{label, icon, href}]}, ...]. A section or link hidden
    from the current viewer (GM sees everything; players only see
    visible_to_players=True on both the section and the link itself) is
    dropped, as is any link whose target no longer resolves. Sections with
    no links left after filtering are dropped for players — nothing useful
    to show for them on the live page — but kept for the GM, so a section
    they've named via the edit page but not filled in yet still appears as
    a drop target for the drag-a-nav-tab-here feature (index.html); this
    also means a GM's returned list here has the same length/order as the
    raw stored array, which index.html's drop handlers rely on to pass the
    right section_index back to the quick-link endpoint."""
    user = getattr(request.state, "user", None)
    is_gm = bool(user and getattr(user, "is_gm", False))
    try:
        raw_sections = json.loads(world.home_sections_json or "[]")
    except Exception:
        raw_sections = []
    out = []
    for sec in raw_sections if isinstance(raw_sections, list) else []:
        if not isinstance(sec, dict):
            continue
        if not is_gm and not sec.get("visible_to_players", True):
            continue
        links = []
        for l in (sec.get("links") or []):
            if not isinstance(l, dict):
                continue
            if not is_gm and not l.get("visible_to_players", True):
                continue
            href = _resolve_home_link_href(db, world, l, is_gm)
            if href is None:
                continue
            links.append({"label": l.get("label", ""), "icon": l.get("icon", ""), "href": with_world(href, world)})
        if links or is_gm:
            out.append({"name": sec.get("name") or "Untitled", "links": links})
    return out


def _resolve_pinned_tiles(db: Session, world: World, request: Request, counts: dict) -> list[dict]:
    """world.home_pinned_tiles_json -> renderable dashboard tiles:
    [{label, icon, href, count}, ...] (count is None for anything that
    isn't a kind tile — the built-in counters are the only stat this app
    tracks per-target). Same visibility/href-resolution rules as
    _resolve_home_sections above, just flat instead of nested in sections."""
    user = getattr(request.state, "user", None)
    is_gm = bool(user and getattr(user, "is_gm", False))
    try:
        raw_tiles = json.loads(world.home_pinned_tiles_json or "[]")
    except Exception:
        raw_tiles = []
    out = []
    for t in raw_tiles if isinstance(raw_tiles, list) else []:
        if not isinstance(t, dict):
            continue
        if not is_gm and not t.get("visible_to_players", True):
            continue
        href = _resolve_home_link_href(db, world, t, is_gm)
        if href is None:
            continue
        count = counts.get(t.get("target_ref")) if t.get("target_type") == "kind" else None
        out.append({
            "label": t.get("label", ""), "icon": t.get("icon", ""),
            "href": with_world(href, world), "count": count,
        })
    return out


def _resolve_hidden_kinds(world: World) -> set:
    """world.home_hidden_kinds_json -> a set of kind ids to skip in the home
    page's default stat-tile dashboard loop (index.html). GM-only concept —
    a hidden tile is just not rendered for anyone, there's no
    visible_to_players split here since it's about the GM's own dashboard
    clutter, not player-facing spoilers."""
    try:
        raw = json.loads(world.home_hidden_kinds_json or "[]")
    except Exception:
        raw = []
    return {k for k in raw if isinstance(k, str)} if isinstance(raw, list) else set()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    counts = _kind_counts(db, world, request)
    recent = _filter_visible_entities(db.query(Entity).filter(Entity.world_id == world.id), request).order_by(Entity.updated_at.desc()).limit(8).all()
    worlds = _visible_worlds(request, db)
    # collect a few maps for the homepage preview
    preview_maps = []
    _sm = BASE_DIR / "static" / "maps"
    for s, d in list(_iter_world_maps(world.id))[:6]:
        img = None
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".avif"):
            if (_sm / (s + ext)).exists(): img = f"/static/maps/{s}{ext}"; break
            if (UPLOADS_DIR / "maps" / (s + ext)).exists(): img = f"/uploads/maps/{s}{ext}"; break
        preview_maps.append({"slug": s, "name": d.get("name", s), "image_url": img})
    # Most-linked entities
    most_linked = []
    if world:
        most_linked_q = (
            db.query(Entity, func.count(entity_links.c.source_id).label('link_count'))
            .join(entity_links, entity_links.c.target_id == Entity.id)
            .filter(Entity.world_id == world.id)
        )
        most_linked_q = _filter_visible_entities(most_linked_q, request)
        most_linked = (
            most_linked_q.group_by(Entity.id)
            .order_by(func.count(entity_links.c.source_id).desc())
            .limit(6).all()
        )

    # Tag cloud
    top_tags = []
    if world:
        raw_tags = _filter_visible_entities(db.query(Entity.tags).filter(
            Entity.world_id == world.id, Entity.tags.isnot(None)
        ), request).all()
        tag_counts: dict = {}
        for (ts,) in raw_tags:
            for t in (ts or '').split(','):
                t = t.strip()
                if t: tag_counts[t] = tag_counts.get(t, 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:40]

    # Recent boards + schematics
    recent_boards = db.query(InvestBoard).filter(
        InvestBoard.world_id == world.id
    ).order_by(InvestBoard.updated_at.desc()).limit(3).all() if world else []
    recent_schematics = db.query(Schematic).filter(
        Schematic.world_id == world.id
    ).order_by(Schematic.updated_at.desc()).limit(3).all() if world else []
    recent_sessions = db.query(GameSession).filter(
        GameSession.world_id == world.id
    ).order_by(GameSession.session_num.desc()).limit(3).all() if world else []
    active_quests = db.query(Quest).filter(
        Quest.world_id == world.id, Quest.status == "active"
    ).order_by(Quest.updated_at.desc()).limit(3).all() if world else []

    home_sections = _resolve_home_sections(db, world, request) if world else []
    pinned_tiles = _resolve_pinned_tiles(db, world, request, counts) if world else []
    hidden_kinds = _resolve_hidden_kinds(world) if world else set()
    world_is_empty = sum(counts.values()) == 0

    return templates.TemplateResponse("index.html", {
        "request": request, "counts": counts, "recent": recent,
        "world": world, "worlds": worlds, "preview_maps": preview_maps,
        "most_linked": most_linked, "top_tags": top_tags,
        "recent_boards": recent_boards, "recent_schematics": recent_schematics,
        "recent_sessions": recent_sessions, "active_quests": active_quests,
        "home_sections": home_sections, "world_is_empty": world_is_empty,
        "pinned_tiles": pinned_tiles, "hidden_kinds": hidden_kinds,
    })

def _map_data(jf: Path) -> Optional[dict]:
    try:
        return json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_world_maps(world_id: int):
    """Yield (slug, data) for maps belonging to world_id. Legacy map files with no
    "world_id" key predate multi-world support and belong to world 1 (the original
    seeded world)."""
    if not _MAPS_DIR.exists():
        return
    for jf in sorted(_MAPS_DIR.glob("*.json")):
        data = _map_data(jf)
        if data is not None and data.get("world_id", 1) == world_id:
            yield jf.stem, data

@app.get("/maps", response_class=HTMLResponse)
def maps_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    worlds = _visible_worlds(request, db)
    maps = []
    _STATIC_MAPS = BASE_DIR / "static" / "maps"
    for slug, data in _iter_world_maps(world.id):
        image_url = None
        # Check static/maps first (bundled), then uploads/maps (user-uploaded)
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".avif"):
            if (_STATIC_MAPS / (slug + ext)).exists():
                image_url = f"/static/maps/{slug}{ext}"
                break
            if (UPLOADS_DIR / "maps" / (slug + ext)).exists():
                image_url = f"/uploads/maps/{slug}{ext}"
                break
        maps.append({
            "slug": slug,
            "name": data.get("name", slug),
            "width": data.get("width", 0),
            "height": data.get("height", 0),
            "markers": len(data.get("markers", [])),
            "image_url": image_url,
        })
    schematics = db.query(Schematic).filter(Schematic.world_id == world.id).order_by(Schematic.name).all()
    # /maps is player-safe (read-only) unlike the entity form's picker, which
    # only GM-only routes ever reach — all_world_image_urls() returns every
    # image used anywhere in the world, including on GM-only/hidden entities,
    # so this must stay empty for a non-GM viewer rather than leaking that
    # list into the page.
    is_gm = bool(request.state.user and request.state.user.is_gm)
    gallery_images = _gallery_module.all_world_image_urls(db, world) if is_gm else []
    return templates.TemplateResponse("maps.html", {
        "request": request, "world": world, "worlds": worlds, "maps": maps,
        "schematics": schematics, "gallery_images": gallery_images,
    })

@app.get("/maps/new", response_class=HTMLResponse)
def map_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds")
    worlds = _visible_worlds(request, db)
    gallery_images = _gallery_module.all_world_image_urls(db, world)
    return templates.TemplateResponse("map_form.html", {
        "request": request, "world": world, "worlds": worlds, "gallery_images": gallery_images,
    })

@app.post("/maps/new")
async def map_new(
    request: Request,
    name: str = Form(...),
    width: int = Form(3072),
    height: int = Form(3072),
    image_file: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world = get_active_world(request, db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    slug = _slug_from_name(name)
    if not slug:
        # A name with no letters/digits (e.g. "???" or emoji-only) slugifies to
        # "" — f"{slug}.json" would then write to the bare filename ".json",
        # which /maps/{slug} can never route back to: a zombie map, visible in
        # the listing but unreachable and undeletable through the app.
        raise HTTPException(400, "Name must contain at least one letter or number")
    base = slug; i = 2
    _MAPS_DIR.mkdir(parents=True, exist_ok=True)
    while (_MAPS_DIR / f"{slug}.json").exists():
        slug = f"{base}-{i}"; i += 1
    (_MAPS_DIR / f"{slug}.json").write_text(json.dumps({
        "name": name, "world_id": world.id, "width": width, "height": height, "markers": [],
    }), encoding="utf-8")
    if image_file and image_file.filename:
        ext = Path(image_file.filename).suffix.lower()
        if ext in ALLOWED_EXTS:
            maps_upload_dir = UPLOADS_DIR / "maps"
            maps_upload_dir.mkdir(parents=True, exist_ok=True)
            copy_upload_bounded(image_file, maps_upload_dir / (slug + ext))
    return RedirectResponse(f"/maps/{slug}", status_code=303)

@app.post("/maps/{slug}/rename")
def map_rename(slug: str, request: Request, name: str = Form(...),
                db: Session = Depends(get_db), active_world: str = Cookie(None)):
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = _map_data(jf)
    if map_data is None:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or map_data.get("world_id", 1) != world.id:
        raise HTTPException(404)
    new_name = name.strip()
    if not new_name:
        raise HTTPException(400, "Name can't be blank")
    # Renaming here only changes the display name, not the slug/URL — the slug
    # is also how the uploaded image file, MapOverlay row, and any party pins
    # (Party.location_json {"kind": "map", "slug": ...}) reference this map, so
    # keeping it stable avoids cascading updates and broken links/bookmarks.
    map_data["name"] = new_name
    jf.write_text(json.dumps(map_data), encoding="utf-8")
    return RedirectResponse("/maps", status_code=303)

@app.post("/maps/{slug}/delete")
def map_delete(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = _map_data(jf)
    if map_data is None:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or map_data.get("world_id", 1) != world.id:
        raise HTTPException(404)
    jf.unlink()
    maps_upload_dir = UPLOADS_DIR / "maps"
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
        img = maps_upload_dir / (slug + ext)
        if img.exists():
            img.unlink()
    # Clean up the overlay row too — otherwise a later map that happens to
    # slugify to the same value would silently inherit this one's markers.
    overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
    if overlay:
        db.delete(overlay)
        db.commit()
    return RedirectResponse("/maps", status_code=303)

@app.post("/maps/{slug}/upload")
async def map_upload_image(slug: str, request: Request, file: UploadFile = File(...),
                            db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # Unlike its rename/delete siblings, this route previously took no db/world
    # params at all — it would happily write to any slug, map or no map,
    # regardless of which world was active. A nonexistent slug left an orphan
    # upload file that would silently "haunt" a later map created with that
    # same slug (the same squatting hazard map_delete's overlay cleanup guards
    # against).
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = _map_data(jf)
    if map_data is None:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or map_data.get("world_id", 1) != world.id:
        raise HTTPException(404)
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported file type")
    maps_upload_dir = UPLOADS_DIR / "maps"
    maps_upload_dir.mkdir(parents=True, exist_ok=True)
    for old_ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
        old = maps_upload_dir / (slug + old_ext)
        if old.exists():
            old.unlink()
    dest = maps_upload_dir / (slug + ext)
    copy_upload_bounded(file, dest)
    return RedirectResponse("/maps", status_code=303)

def _world_parties_payload(db: Session, world_id: int):
    """[{id, name, member_count}] for the world's parties, for the "place party
    here" pickers on maps and schematics."""
    return [
        {
            "id": p.id, "name": p.name,
            "member_count": len(json.loads(p.member_pc_ids_json or "[]")) + len(json.loads(p.member_entity_ids_json or "[]")),
        }
        for p in db.query(Party).filter(Party.world_id == world_id).order_by(Party.name).all()
    ]


def _party_pins_for(db: Session, world_id: int, kind: str, slug: str):
    """Parties currently located on this specific map/schematic, with member
    counts, for rendering as pins/markers."""
    pins = []
    for p in db.query(Party).filter(Party.world_id == world_id).all():
        loc = json.loads(p.location_json or "{}")
        if loc.get("kind") != kind or loc.get("slug") != slug:
            continue
        member_count = len(json.loads(p.member_pc_ids_json or "[]")) + len(json.loads(p.member_entity_ids_json or "[]"))
        pin = {"id": p.id, "name": p.name, "member_count": member_count}
        if kind == "map":
            pin["lat"] = loc.get("lat"); pin["lng"] = loc.get("lng")
        else:
            pin["x"] = loc.get("x"); pin["y"] = loc.get("y")
        pins.append(pin)
    return pins


@app.get("/maps/{slug}", response_class=HTMLResponse)
def map_viewer(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = _map_data(jf)
    if map_data is None:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or map_data.get("world_id", 1) != world.id:
        raise HTTPException(404)
    _sm = BASE_DIR / "static" / "maps"
    image_url = None
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".avif"):
        if (_sm / (slug + ext)).exists(): image_url = f"/static/maps/{slug}{ext}"; break
        if (UPLOADS_DIR / "maps" / (slug + ext)).exists(): image_url = f"/uploads/maps/{slug}{ext}"; break
    worlds = _visible_worlds(request, db)
    overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
    if not overlay:
        overlay = MapOverlay(slug=slug, custom_markers_json="[]", custom_regions_json="[]")
        db.add(overlay); db.commit()
    # build name→id map for local entity linking. Filtered by visibility: this map is
    # serialized into the page, and /maps/{slug} is player-safe, so an unfiltered query
    # would hand players the names and IDs of GM-only hidden entities.
    ename_map = {}
    if world:
        ename_q = _filter_visible_entities(
            db.query(Entity.name, Entity.id).filter(Entity.world_id == world.id), request
        )
        for e in ename_q.all():
            ename_map[e.name.lower()] = e.id
    schematics = db.query(Schematic).filter(Schematic.world_id == world.id).order_by(Schematic.name).all()
    user = getattr(request.state, "user", None)
    is_gm = bool(user and user.is_gm)
    return templates.TemplateResponse("map_viewer.html", {
        "request": request, "world": world, "worlds": worlds,
        "map_data": map_data, "image_url": image_url or "", "slug": slug,
        "overlay": overlay, "ename_map": json.dumps(ename_map), "is_gm": is_gm,
        "schematics_json": json.dumps([{"slug": s.slug, "name": s.name} for s in schematics]),
        "world_parties_json": json.dumps(_world_parties_payload(db, world.id)),
        "party_pins_json": json.dumps(_party_pins_for(db, world.id, "map", slug)),
    })

_MAX_OVERLAY_ITEMS = 500  # per list — a GM-authored battle map, not a data dump

@app.post("/api/maps/{slug}/overlay")
async def save_map_overlay(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # This route previously took no world param and did no existence check at
    # all — any slug, real map or not, would get (and keep) an overlay row.
    jf = _MAPS_DIR / f"{slug}.json"
    if not jf.exists():
        raise HTTPException(404)
    map_data = _map_data(jf)
    if map_data is None:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or map_data.get("world_id", 1) != world.id:
        raise HTTPException(404)
    body = await request.json()
    custom_markers = body.get("custom_markers", [])
    custom_regions = body.get("custom_regions", [])
    if not isinstance(custom_markers, list) or not isinstance(custom_regions, list):
        raise HTTPException(400, "custom_markers and custom_regions must be lists")
    if len(custom_markers) > _MAX_OVERLAY_ITEMS or len(custom_regions) > _MAX_OVERLAY_ITEMS:
        raise HTTPException(400, f"Too many markers/regions — limit is {_MAX_OVERLAY_ITEMS} each")
    overlay = db.query(MapOverlay).filter(MapOverlay.slug == slug).first()
    if not overlay:
        overlay = MapOverlay(slug=slug); db.add(overlay)
    overlay.custom_markers_json = json.dumps(custom_markers)
    overlay.custom_regions_json = json.dumps(custom_regions)
    db.commit()
    return {"ok": True}

_RULES_LEGACY_ANCHOR_RE = re.compile(r'<a\s+name="[^"]*">\s*</a>', re.IGNORECASE)


def _world_rules_markdown(world) -> str:
    """This world's own rules if the GM has set any, else the bundled N&D
    core rules — so a world running a different system doesn't show N&D's
    stats/feats/psionics by default."""
    if world and (world.rules_md or "").strip():
        md = world.rules_md
    else:
        rules_path = Path(__file__).parent / "core_rules.md"
        md = rules_path.read_text(encoding="utf-8", errors="ignore") if rules_path.exists() else ""
    # Docs exported from Word/Google Docs often carry a raw <a name="..."></a>
    # anchor on every heading for their own in-document TOC links. render_md()
    # HTML-escapes raw tags (a stored-XSS guard for user-typed entity content),
    # which turns these into visible "&lt;a name=...&gt;" text instead of an
    # invisible anchor — and since _rules_toc() below already generates its
    # own heading ids, these legacy anchors are redundant. Strip them here
    # rather than weakening the shared renderer's escaping for everyone.
    return _RULES_LEGACY_ANCHOR_RE.sub("", md)


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    md = _world_rules_markdown(world)
    content, toc = _rules_toc(render_md(md) if md else "<p>No rules have been added for this world yet.</p>")
    user = getattr(request.state, "user", None)
    is_custom = bool(world and (world.rules_md or "").strip())
    return templates.TemplateResponse("rules.html", {
        "request": request, "world": world, "worlds": worlds, "content": content, "toc": toc,
        "is_custom_rules": is_custom, "can_edit": bool(user and user.is_gm),
    })


@app.get("/rules/download.md")
def rules_download(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        if not (world and world.players_can_download_rules):
            raise HTTPException(403)
    content = _world_rules_markdown(world)
    filename = f"{world.slug}-rules.md" if world else "core-rules.md"
    return StreamingResponse(
        io.BytesIO(content.encode()), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/worlds/{world_id}/rules/edit", response_class=HTMLResponse)
def world_rules_edit_form(world_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("rules_edit.html", {
        "request": request, "world": world, "worlds": worlds, "edit_world": w,
    })


@app.post("/worlds/{world_id}/rules/edit")
def world_rules_edit_post(world_id: int, rules_md: str = Form(""), db: Session = Depends(get_db)):
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    w.rules_md = rules_md.strip() or None
    db.commit()
    return RedirectResponse("/rules", status_code=303)


@app.post("/worlds/{world_id}/rules/import")
async def world_rules_import(world_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import rules from a JSON file shaped {"rules_md": "...markdown..."} (a
    "name" key is accepted too but only rules_md is used) — an alternative to
    pasting the whole document into the textarea by hand."""
    w = db.get(World, world_id)
    if not w:
        raise HTTPException(404)
    try:
        payload = json.loads((await file.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Not valid JSON")
    md = str(payload.get("rules_md", "")).strip()
    if not md:
        raise HTTPException(400, 'JSON must have a non-empty "rules_md" string field')
    w.rules_md = md
    db.commit()
    return RedirectResponse("/rules", status_code=303)

# ── Schematics ────────────────────────────────────────────────────────────────

def _slug_from_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s

@app.get("/maps/schematic/new", response_class=HTMLResponse)
def schematic_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    return templates.TemplateResponse("schematic_form.html", {
        "request": request, "world": world, "worlds": worlds, "schematic": None,
    })

@app.post("/maps/schematic/new")
async def schematic_new(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    canvas_width: int = Form(2000),
    canvas_height: int = Form(1500),
    canvas_bg: str = Form("dark"),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world = get_active_world(request, db, active_world)
    if not world:
        # map_new already guards this identically — schematic_new never did,
        # so world.id below would 500 with an unhandled AttributeError
        # instead of a clean 400 (e.g. right after deleting your only world).
        raise HTTPException(400, "No world selected")
    slug = _slug_from_name(name)
    if not slug:
        # Same zombie-record hazard as map_new: an empty slug would create a
        # Schematic that /maps/schematic/{slug} can never route back to.
        raise HTTPException(400, "Name must contain at least one letter or number")
    base = slug; i = 2
    while db.query(Schematic).filter(Schematic.slug == slug).first():
        slug = f"{base}-{i}"; i += 1
    # canvas_width/height feed the SVG viewBox and (for hex grids) the
    # renderBattleGrid tiling loop's iteration bounds — unvalidated, a
    # zero/negative value degenerates the canvas and a client-supplied
    # multi-million-pixel value could make that loop iterate absurdly.
    if not (100 <= canvas_width <= 20000) or not (100 <= canvas_height <= 20000):
        raise HTTPException(400, "Canvas width/height must be between 100 and 20000")
    s = Schematic(world_id=world.id, name=name, slug=slug,
                  description=description or None, is_html=False,
                  canvas_width=canvas_width, canvas_height=canvas_height,
                  canvas_bg=canvas_bg, elements_json="[]")
    db.add(s); db.commit(); db.refresh(s)
    return RedirectResponse(f"/maps/schematic/{s.slug}", status_code=303)

@app.get("/maps/schematic/{slug}", response_class=HTMLResponse)
def schematic_view(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    if s.is_html and s.html_file:
        html_path = SCHEMATICS_STATIC_DIR / s.html_file
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8", errors="ignore"))
        raise HTTPException(404, "HTML schematic file not found")
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    elements = json.loads(s.elements_json or "[]")
    _BG = {"dark": "#111111", "blueprint": "#0d1b2a", "grid-light": "#1a1a2e", "light": "#f0f0f0"}
    canvas_bg_color = _BG.get(s.canvas_bg or "dark", "#111111")
    _, _, pc_payload, entity_payload = _combat_candidates(db, s.world_id)
    item_entities = db.query(Entity).filter(
        Entity.world_id == s.world_id, Entity.kind == "item"
    ).order_by(Entity.name).all()
    item_payload = [{"id": e.id, "name": e.name, "summary": e.summary or "", "image_url": e.image_url or ""} for e in item_entities]
    combat_sessions = db.query(CombatSession).filter(CombatSession.world_id == s.world_id).order_by(CombatSession.updated_at.desc()).all()
    linked_combat = db.query(CombatSession).filter(CombatSession.id == s.combat_session_id).first() if s.combat_session_id else None
    active_combatant_id, combat_round = None, None
    if linked_combat:
        combatants = json.loads(linked_combat.combatants_json or "[]")
        ordered = sorted(combatants, key=lambda c: -(c.get("initiative") or 0))
        if ordered and 0 <= linked_combat.active_idx < len(ordered):
            active_combatant_id = ordered[linked_combat.active_idx].get("id")
        combat_round = linked_combat.round_num
    gallery_images = _gallery_module.all_world_image_urls(db, world) if world else []
    return templates.TemplateResponse("schematic.html", {
        "request": request, "world": world, "worlds": worlds,
        "schematic": s, "elements_json": json.dumps(elements),
        "canvas_bg_color": canvas_bg_color,
        "world_parties_json": json.dumps(_world_parties_payload(db, s.world_id)),
        "party_pins_json": json.dumps(_party_pins_for(db, s.world_id, "schematic", slug)),
        "grid_type": s.grid_type or "none",
        "grid_config_json": s.grid_config_json or "{}",
        "pc_payload_json": json.dumps(pc_payload),
        "entity_payload_json": json.dumps(entity_payload),
        "item_payload_json": json.dumps(item_payload),
        "combat_sessions": combat_sessions,
        "linked_combat": linked_combat,
        "combat_active_combatant_id": active_combatant_id,
        "combat_round": combat_round,
        "gallery_images": gallery_images,
    })

@app.post("/maps/schematic/{slug}/link-combat")
def schematic_link_combat(slug: str, combat_session_id: str = Form(""), db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    if combat_session_id.strip():
        try:
            cs_id = int(combat_session_id)
        except ValueError:
            raise HTTPException(400, "Invalid combat session")
        cs = db.query(CombatSession).filter(CombatSession.id == cs_id, CombatSession.world_id == s.world_id).first()
        if not cs:
            raise HTTPException(404, "Combat session not found in this world")
        s.combat_session_id = cs.id
    db.commit()
    return RedirectResponse(f"/maps/schematic/{slug}", status_code=303)

@app.post("/maps/schematic/{slug}/unlink-combat")
def schematic_unlink_combat(slug: str, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    s.combat_session_id = None
    db.commit()
    return RedirectResponse(f"/maps/schematic/{slug}", status_code=303)

@app.post("/maps/schematic/{slug}/pull-combat")
def schematic_pull_combat(slug: str, db: Session = Depends(get_db)):
    """One-way sync Combat → Map: create/refresh tokens for every combatant in
    the linked CombatSession, matched by the token's combatant_id.

    Runs inside the same BEGIN IMMEDIATE pattern as move-token/pickup-item/
    buy-item (see those for why with_for_update() alone is a no-op on
    SQLite): without it, a player's concurrent move-token — a plain
    read-modify-write of the same elements_json column — could read the
    pre-pull elements, and its write would then land after this one and
    silently discard every token this pull just created or refreshed.
    """
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or not s.combat_session_id:
        raise HTTPException(404)
    try:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        s2 = db.query(Schematic).filter(Schematic.id == s.id).with_for_update().first()
        cs = db.query(CombatSession).filter(CombatSession.id == s2.combat_session_id).first()
        if not cs:
            raise HTTPException(404)
        combatants = json.loads(cs.combatants_json or "[]")
        elements = json.loads(s2.elements_json or "[]")
        by_combatant_id = {e.get("combatant_id"): e for e in elements if e.get("type") == "token" and e.get("combatant_id")}
        cx, cy = (s2.canvas_width or 2000) / 2, (s2.canvas_height or 1500) / 2
        new_count = 0
        for combatant in combatants:
            token = by_combatant_id.get(combatant["id"])
            if token:
                token["name"] = combatant.get("name", token.get("name"))
                token["hp"] = combatant.get("hp", token.get("hp"))
                token["max_hp"] = combatant.get("max_hp", token.get("max_hp"))
                token["conditions"] = list(combatant.get("conditions", []))
            else:
                source = combatant.get("source", "manual")
                elements.append({
                    "id": str(uuid.uuid4())[:8], "type": "token",
                    "x": cx + (new_count % 5) * 45 - 90, "y": cy + (new_count // 5) * 45 - 45,
                    "r": 20, "layer": "Tracks", "combatant_id": combatant["id"],
                    "source": source, "pc_id": combatant.get("pc_id"), "entity_id": combatant.get("entity_id"),
                    "name": combatant.get("name", "Combatant"),
                    "color": "#4488ff" if source == "pc" else ("#e63946" if source == "entity" else "#888888"),
                    "hp": combatant.get("hp", 0), "max_hp": combatant.get("max_hp", 0),
                    "conditions": list(combatant.get("conditions", [])),
                    "visible_to_players": source in ("pc", "entity"),
                })
                new_count += 1
        s2.elements_json = json.dumps(elements)
        db.commit()
        return {"elements": elements}
    except HTTPException:
        db.rollback()
        raise
    except OperationalError:
        db.rollback()
        raise HTTPException(409, "Someone else is updating this map — try again")

@app.post("/maps/schematic/{slug}/push-combat")
def schematic_push_combat(slug: str, db: Session = Depends(get_db)):
    """One-way sync Map → Combat: write token hp/max_hp/conditions back onto
    the matching combatant (matched by combatant_id, not pc_id/entity_id, so
    duplicate entities in one combat stay distinguishable).

    Same BEGIN IMMEDIATE pattern as pull-combat, locking the CombatSession
    row this time — a player's move-token doesn't touch combatants_json, but
    another push-combat (or the combat tracker's own hp/condition edits)
    could race this one's read-modify-write of it.
    """
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or not s.combat_session_id:
        raise HTTPException(404)
    try:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        cs = db.query(CombatSession).filter(CombatSession.id == s.combat_session_id).with_for_update().first()
        if not cs:
            raise HTTPException(404)
        elements = json.loads(s.elements_json or "[]")
        combatants = json.loads(cs.combatants_json or "[]")
        by_id = {c["id"]: c for c in combatants}
        synced = []
        for el in elements:
            if el.get("type") != "token" or not el.get("combatant_id"):
                continue
            c = by_id.get(el["combatant_id"])
            if not c:
                continue
            c["hp"] = el.get("hp", c.get("hp"))
            c["max_hp"] = el.get("max_hp", c.get("max_hp"))
            c["conditions"] = list(el.get("conditions", []))
            synced.append(c.get("name"))
        cs.combatants_json = json.dumps(combatants)
        db.commit()
        return {"synced": synced}
    except HTTPException:
        db.rollback()
        raise
    except OperationalError:
        db.rollback()
        raise HTTPException(409, "Someone else is updating this combat — try again")


def _schematic_player_payload(db: Session, s: Schematic, user):
    """Elements filtered for the player-facing live view: any element the GM
    marked "hidden" (the 👁 toggle in the editor) is dropped, and tokens are
    further filtered by visible_to_players. This is the only server-side gate
    that matters for secrecy — the GM editor's own client-side hidden check
    is just a local display convenience, not something players' browsers ever
    see. Also resolves the viewer's own PlayerCharacter (for the
    draggable-own-token feature) and the linked combat's active-turn
    combatant, if any."""
    elements = json.loads(s.elements_json or "[]")
    visible = [
        el for el in elements
        if not el.get("hidden")
        and (el.get("type") != "token" or el.get("visible_to_players", True))
    ]
    own_pc_id, own_pc_currency = None, []
    if user and not user.is_gm:
        pc = db.query(PlayerCharacter).filter(
            PlayerCharacter.world_id == s.world_id, PlayerCharacter.owner_user_id == user.id
        ).first()
        if pc:
            own_pc_id = pc.id
            own_pc_currency = json.loads(pc.currency_json or "[]")
    active_combatant_id, combat_round = None, None
    if s.combat_session_id:
        cs = db.query(CombatSession).filter(CombatSession.id == s.combat_session_id).first()
        if cs:
            combatants = json.loads(cs.combatants_json or "[]")
            ordered = sorted(combatants, key=lambda c: -(c.get("initiative") or 0))
            if ordered and 0 <= cs.active_idx < len(ordered):
                active_combatant_id = ordered[cs.active_idx].get("id")
            combat_round = cs.round_num
    return elements, visible, own_pc_id, own_pc_currency, active_combatant_id, combat_round


@app.get("/maps/schematic/{slug}/view", response_class=HTMLResponse)
def schematic_player_view(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or (s.is_html and s.html_file):
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or s.world_id != world.id:
        raise HTTPException(404)
    worlds = _visible_worlds(request, db)
    user = getattr(request.state, "user", None)
    _, visible, own_pc_id, own_pc_currency, active_combatant_id, combat_round = _schematic_player_payload(db, s, user)
    _BG = {"dark": "#111111", "blueprint": "#0d1b2a", "grid-light": "#1a1a2e", "light": "#f0f0f0"}
    return templates.TemplateResponse("schematic_view.html", {
        "request": request, "world": world, "worlds": worlds,
        "schematic": s, "elements_json": json.dumps(visible),
        "canvas_bg_color": _BG.get(s.canvas_bg or "dark", "#111111"),
        "party_pins_json": json.dumps(_party_pins_for(db, s.world_id, "schematic", slug)),
        "grid_type": s.grid_type or "none",
        "grid_config_json": s.grid_config_json or "{}",
        "own_pc_id": own_pc_id,
        "own_pc_currency_json": json.dumps(own_pc_currency),
        "combat_active_combatant_id": active_combatant_id,
        "combat_round": combat_round,
    })


@app.get("/maps/schematic/{slug}/view.json")
def schematic_player_view_json(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s or (s.is_html and s.html_file):
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    if not world or s.world_id != world.id:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    _, visible, own_pc_id, own_pc_currency, active_combatant_id, combat_round = _schematic_player_payload(db, s, user)
    return {
        "elements": visible,
        "image_url": s.image_url,
        "party_pins": _party_pins_for(db, s.world_id, "schematic", slug),
        "own_pc_id": own_pc_id,
        "own_pc_currency": own_pc_currency,
        "combat_active_combatant_id": active_combatant_id,
        "combat_round": combat_round,
    }


def _require_schematic_world_access(db: Session, s: Schematic, user) -> None:
    """Write-route guard: a GM always passes; a player must currently hold a
    WorldMembership row for s.world_id. The read routes (schematic_player_view)
    already enforce this via get_active_world/accessible_world_ids — these
    write routes previously only checked "does the caller own a PlayerCharacter
    in this world," which member_remove() never revokes (it only deletes the
    WorldMembership row), so a removed player kept live-play write access
    forever even though the read view correctly 404s for them."""
    if user.is_gm:
        return
    world = db.query(World).filter(World.id == s.world_id).first()
    if not _auth.user_can_access_world(db, user, world):
        raise HTTPException(403, "You no longer have access to this world")


@app.post("/api/maps/schematic/{slug}/move-token")
async def schematic_move_own_token(slug: str, request: Request, db: Session = Depends(get_db)):
    """Players may move only the one token linked to their own PlayerCharacter.
    Ownership is re-derived from the session user server-side, never trusted
    from the client.

    An element's `locked` flag (toggled in the GM editor via lockSel()/the
    per-element lock button) previously only gated the *editor's* own select
    tool client-side — this route, pickup-item, and buy-item all ignored it
    entirely, so a player could drag/pick up/buy a token the GM had locked
    by hitting the API directly (or even just via the ordinary player-view
    drag handle, which never checked it either). Enforced here and in the
    other two player-write routes below; the GM's own editor writes go
    through a different route (/elements) and aren't affected.

    Also runs inside the same BEGIN IMMEDIATE pattern as pickup-item/buy-item
    (Phase 4) and pull/push-combat (Phase 11): this was previously the one
    read-modify-write of elements_json left completely unlocked, so even
    with pull-combat's own lock added, a concurrent move-token could still
    read the pre-pull elements, block on commit until pull-combat released
    its lock, and then commit its own stale copy — silently erasing every
    token pull-combat had just created or refreshed."""
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    _require_schematic_world_access(db, s, user)
    body = await request.json()
    token_id = body.get("token_id")
    x, y = body.get("x"), body.get("y")
    if token_id is None or x is None or y is None:
        raise HTTPException(400)
    try:
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        s2 = db.query(Schematic).filter(Schematic.id == s.id).with_for_update().first()
        elements = json.loads(s2.elements_json or "[]")
        el = next((e for e in elements if e.get("id") == token_id and e.get("type") == "token"), None)
        if not el:
            raise HTTPException(404)
        if not user.is_gm:
            pc = db.query(PlayerCharacter).filter(
                PlayerCharacter.world_id == s2.world_id, PlayerCharacter.owner_user_id == user.id
            ).first()
            if not pc or el.get("pc_id") != pc.id:
                raise HTTPException(403, "Not your character's token")
            if not el.get("visible_to_players", True):
                raise HTTPException(403)
            if el.get("locked"):
                raise HTTPException(403, "This token is locked")
        el["x"] = float(x)
        el["y"] = float(y)
        s2.elements_json = json.dumps(elements)
        db.commit()
        return {"ok": True}
    except HTTPException:
        db.rollback()
        raise
    except OperationalError:
        db.rollback()
        raise HTTPException(409, "Someone else is updating this map — try again")


def _merge_equipment_item(pc: PlayerCharacter, name: str, qty: int):
    """Add qty of an item into pc.equipment_json, merging into an existing row
    with the same name (case-insensitive) rather than duplicating it."""
    equipment = json.loads(pc.equipment_json or "[]")
    existing = next((it for it in equipment if (it.get("name") or "").strip().lower() == name.strip().lower()), None)
    if existing:
        existing["qty"] = (existing.get("qty") or 0) + qty
    else:
        equipment.append({"name": name, "qty": qty, "weight": 0, "equipped": False, "notes": ""})
    pc.equipment_json = json.dumps(equipment)


def _own_pc_for_schematic(db: Session, s: Schematic, user) -> Optional[PlayerCharacter]:
    return db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == s.world_id, PlayerCharacter.owner_user_id == user.id
    ).first()


@app.post("/api/maps/schematic/{slug}/pickup-item")
async def schematic_pickup_item(slug: str, request: Request, db: Session = Depends(get_db)):
    """A player picks up an entire item-token stack into their own character's
    equipment. Ownership of the calling PlayerCharacter is re-derived from the
    session user server-side, same pattern as move-token. The read-check-write
    below runs inside a BEGIN IMMEDIATE transaction so two concurrent pickups
    of the same token can't both succeed — with_for_update() is a no-op on
    SQLite, so BEGIN IMMEDIATE is the actual lock."""
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    _require_schematic_world_access(db, s, user)
    body = await request.json()
    token_id = body.get("token_id")
    if not token_id:
        raise HTTPException(400)
    try:
        db.rollback()  # close whatever transaction the reads above opened
        db.execute(text("BEGIN IMMEDIATE"))
        s2 = db.query(Schematic).filter(Schematic.id == s.id).with_for_update().first()
        elements = json.loads(s2.elements_json or "[]")
        el = next((e for e in elements if e.get("id") == token_id and e.get("type") == "token" and e.get("source") == "item"), None)
        if not el:
            raise HTTPException(404)
        if not el.get("visible_to_players", True):
            raise HTTPException(403)
        if el.get("locked"):
            raise HTTPException(403, "This item is locked")
        pc = _own_pc_for_schematic(db, s2, user)
        if not pc:
            raise HTTPException(400, "You don't have a character in this world")
        qty = int(el.get("qty") or 1)
        name = el.get("name") or "Item"
        _merge_equipment_item(pc, name, qty)
        elements = [e for e in elements if e.get("id") != token_id]
        s2.elements_json = json.dumps(elements)
        db.commit()
        return {"ok": True, "name": name, "qty": qty}
    except HTTPException:
        db.rollback()
        raise
    except OperationalError:
        db.rollback()
        raise HTTPException(409, "Someone else is completing this action — try again")


@app.post("/api/maps/schematic/{slug}/buy-item")
async def schematic_buy_item(slug: str, request: Request, db: Session = Depends(get_db)):
    """A player buys one stock row from a merchant token, deducting currency
    and merging the item into their own equipment. Ownership of the calling
    PlayerCharacter is re-derived server-side, same pattern as pickup-item.
    Wrapped in the same BEGIN IMMEDIATE pattern as pickup-item so a concurrent
    double-buy can't oversell stock or double-charge currency."""
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    _require_schematic_world_access(db, s, user)
    body = await request.json()
    token_id = body.get("token_id")
    stock_id = body.get("stock_id")
    if not token_id or not stock_id:
        raise HTTPException(400)
    try:
        db.rollback()  # close whatever transaction the reads above opened
        db.execute(text("BEGIN IMMEDIATE"))
        s2 = db.query(Schematic).filter(Schematic.id == s.id).with_for_update().first()
        elements = json.loads(s2.elements_json or "[]")
        el = next((e for e in elements if e.get("id") == token_id and e.get("type") == "token" and e.get("source") == "merchant"), None)
        if not el:
            raise HTTPException(404)
        if not el.get("visible_to_players", True):
            raise HTTPException(403)
        if el.get("locked"):
            raise HTTPException(403, "This merchant is locked")
        inventory = el.get("inventory") or []
        stock = next((row for row in inventory if row.get("id") == stock_id), None)
        qty = stock.get("qty", -1) if stock else -1
        if not stock or (qty != -1 and qty <= 0):
            raise HTTPException(404, "That item is out of stock")
        pc = _own_pc_for_schematic(db, s2, user)
        if not pc:
            raise HTTPException(400, "You don't have a character in this world")
        price = float(stock.get("price") or 0)
        abbr = (stock.get("currency_abbr") or "CR").strip().lower()
        currency = json.loads(pc.currency_json or "[]")
        entry = next((c for c in currency if (c.get("abbr") or "").strip().lower() == abbr), None)
        if not entry:
            raise HTTPException(400, f"Your character doesn't have {stock.get('currency_abbr', 'CR')}")
        if float(entry.get("value") or 0) < price:
            raise HTTPException(400, "Insufficient funds")
        entry["value"] = float(entry.get("value") or 0) - price
        pc.currency_json = json.dumps(currency)
        if stock.get("qty", -1) != -1:
            stock["qty"] = stock["qty"] - 1
        _merge_equipment_item(pc, stock.get("name") or "Item", 1)
        s2.elements_json = json.dumps(elements)
        db.commit()
        return {"ok": True, "item": {"name": stock.get("name"), "qty": 1}, "currency": {"abbr": entry.get("abbr"), "value": entry.get("value")}}
    except HTTPException:
        db.rollback()
        raise
    except OperationalError:
        db.rollback()
        raise HTTPException(409, "Someone else is completing this action — try again")

@app.post("/maps/schematic/{slug}/grid")
async def schematic_save_grid(slug: str, request: Request, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    body = await request.json()
    grid_type = body.get("grid_type") if body.get("grid_type") in ("none", "hex", "square") else "none"
    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    s.grid_type = grid_type
    s.grid_config_json = json.dumps(config)
    db.commit()
    return {"ok": True}

@app.post("/maps/schematic/{slug}/elements")
async def schematic_save_elements(slug: str, request: Request, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s: raise HTTPException(404)
    body = await request.json()
    elements = body.get("elements", [])
    # Every read path — the editor itself, the player view, move-token,
    # pickup-item, buy-item, pull/push-combat — assumes elements_json decodes
    # to a list of dicts. This was the only write route that never checked
    # that before persisting, so a malformed body (elements missing, or not a
    # list) would silently brick the schematic: every subsequent view 500s
    # trying to iterate/index into whatever got stored instead.
    if not isinstance(elements, list) or not all(isinstance(e, dict) for e in elements):
        raise HTTPException(400, "elements must be a list of objects")
    s.elements_json = json.dumps(elements)
    db.commit()
    return {"ok": True}

@app.post("/maps/schematic/{slug}/upload")
async def schematic_upload_image(slug: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported file type")
    sch_dir = UPLOADS_DIR / "schematics"
    sch_dir.mkdir(parents=True, exist_ok=True)
    for old_ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
        old = sch_dir / (slug + old_ext)
        if old.exists(): old.unlink()
    dest = sch_dir / (slug + ext)
    copy_upload_bounded(file, dest)
    s.image_url = f"/uploads/schematics/{slug}{ext}"
    db.commit()
    return RedirectResponse(f"/maps/schematic/{slug}", status_code=303)

@app.post("/maps/schematic/{slug}/embed-image")
async def schematic_embed_image(slug: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """The editor's 🖼 Embed Image tool used to read the picked file as a
    base64 data: URI client-side (FileReader.readAsDataURL) and stuff the
    whole thing directly into the new element's `href` — which then landed
    verbatim in elements_json. Every read of that schematic (the editor
    itself, the player view, and every move-token/pickup-item/buy-item/
    pull-push-combat call in between) had to parse and transmit that entire
    blob just to touch one token's x/y, with no size cap at all (unlike the
    background-image upload above, which goes through copy_upload_bounded).

    This route gives embedded images the same treatment as every other
    upload in the app: validated, size-bounded, written to disk under a
    unique filename, and referenced by URL — so elements_json stays cheap to
    read regardless of how many images a GM has embedded, and the browser
    can actually cache the image instead of re-transmitting it inline on
    every poll."""
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "Unsupported file type")
    embeds_dir = UPLOADS_DIR / "schematics" / "embeds"
    embeds_dir.mkdir(parents=True, exist_ok=True)
    fname = unique_upload_filename(file.filename, ext)
    copy_upload_bounded(file, embeds_dir / fname)
    return {"url": f"/uploads/schematics/embeds/{fname}"}

@app.post("/maps/schematic/{slug}/rename")
def schematic_rename(slug: str, name: str = Form(...), db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    new_name = name.strip()
    if not new_name:
        raise HTTPException(400, "Name can't be blank")
    # Slug (the URL/id) stays stable — combat-session links, party pins, and
    # any bookmarked /maps/schematic/{slug} URL all key off it.
    s.name = new_name
    db.commit()
    return RedirectResponse(f"/maps/schematic/{slug}", status_code=303)

def _delete_schematic_files(s: Schematic) -> None:
    """Remove every file this schematic owns on disk: its HTML file (is_html
    schematics) or background image, plus any images embedded into elements
    via the 🖼 Embed Image tool (Phase 13's /embed-image uploads, stored
    under uploads/schematics/embeds/ and referenced by URL from element.href).
    schematic_delete previously only ever deleted the DB row, leaking every
    one of these on every delete — the same class of bug map_delete's own
    overlay-row cleanup comment already flags for maps."""
    if s.is_html and s.html_file:
        html_path = SCHEMATICS_STATIC_DIR / s.html_file
        if html_path.exists():
            html_path.unlink()
    elif s.image_url:
        for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif"):
            img = UPLOADS_DIR / "schematics" / (s.slug + ext)
            if img.exists():
                img.unlink()
    embeds_dir = UPLOADS_DIR / "schematics" / "embeds"
    for el in json.loads(s.elements_json or "[]"):
        href = el.get("href") if isinstance(el, dict) else None
        if isinstance(href, str) and href.startswith("/uploads/schematics/embeds/"):
            f = embeds_dir / href.rsplit("/", 1)[-1]
            if f.exists():
                f.unlink()


@app.post("/maps/schematic/{slug}/delete")
def schematic_delete(slug: str, db: Session = Depends(get_db)):
    s = db.query(Schematic).filter(Schematic.slug == slug).first()
    if not s:
        raise HTTPException(404)
    _delete_schematic_files(s)
    db.delete(s); db.commit()
    return RedirectResponse("/maps", status_code=303)

# ── Investigation Boards ──────────────────────────────────────────────────────

@app.get("/api/ai/world-context")
def ai_world_context(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        return {"context": "", "world_name": ""}
    lines = [f"# {world.name}", world.description or "", ""]
    # Characters, locations, orgs, creatures, events: name + subtype + summary
    for kind in ["character", "location", "organization", "creature", "event"]:
        ents = db.query(Entity).filter(
            Entity.world_id == world.id, Entity.kind == kind
        ).order_by(Entity.name).all()
        if not ents:
            continue
        lines.append(f"## {kind.upper()}S ({len(ents)})")
        for e in ents:
            line = f"- **{e.name}**"
            if e.subtype:
                line += f" [{e.subtype}]"
            if e.summary:
                line += f": {e.summary}"
            lines.append(line)
        lines.append("")
    # Notes: full body text (lore documents)
    notes = db.query(Entity).filter(
        Entity.world_id == world.id, Entity.kind == "note"
    ).order_by(Entity.name).all()
    if notes:
        lines.append("## LORE DOCUMENTS (full text)")
        for e in notes:
            lines.append(f"\n### {e.name}" + (f" [{e.subtype}]" if e.subtype else ""))
            if e.summary:
                lines.append(e.summary)
            if e.body:
                lines.append(e.body[:5000])
        lines.append("")
    # Items and feats: grouped by subtype, names only
    for kind in ["item", "feat"]:
        ents = db.query(Entity.name, Entity.subtype).filter(
            Entity.world_id == world.id, Entity.kind == kind
        ).order_by(Entity.subtype, Entity.name).all()
        if not ents:
            continue
        lines.append(f"## {kind.upper()}S ({len(ents)} total)")
        by_sub: dict = {}
        for name, sub in ents:
            by_sub.setdefault(sub or "other", []).append(name)
        for sub, names in by_sub.items():
            snippet = ", ".join(names[:25])
            if len(names) > 25:
                snippet += f" … +{len(names)-25} more"
            lines.append(f"  [{sub}]: {snippet}")
        lines.append("")
    return {"context": "\n".join(lines), "world_name": world.name}


def _find_relevant_entities(db: Session, world_id: int, query: str, limit: int = 25) -> list:
    words = [w for w in re.split(r'\W+', query.lower()) if len(w) > 3]
    if not words:
        return (
            db.query(Entity)
            .filter(Entity.world_id == world_id)
            .order_by(Entity.kind, Entity.name)
            .limit(limit)
            .all()
        )
    filters = [
        or_(
            Entity.name.ilike(f'%{w}%'),
            Entity.summary.ilike(f'%{w}%'),
            Entity.tags.ilike(f'%{w}%'),
        )
        for w in words
    ]
    return (
        db.query(Entity)
        .filter(Entity.world_id == world_id, or_(*filters))
        .order_by(Entity.kind, Entity.name)
        .limit(limit)
        .all()
    )


def _format_context_from_entities(entities: list) -> str:
    lines = []
    for e in entities:
        line = f"- [{e.kind}] {e.name}"
        if e.subtype:
            line += f" ({e.subtype})"
        if e.summary:
            line += f": {e.summary}"
        lines.append(line)
    return "\n".join(lines)


class _SmartCtxBody(BaseModel):
    query: str = ""
    limit: int = 25
    notes_limit: int = 5


@app.post("/api/ai/world-context-smart")
def ai_world_context_smart(
    body: _SmartCtxBody,
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        return {"context": "", "count": 0, "notes": 0}
    entities = _find_relevant_entities(db, world.id, body.query, limit=body.limit)
    notes = [e for e in entities if e.kind == "note"]
    non_notes = [e for e in entities if e.kind != "note"]
    # Also search notes separately if notes_limit > 0
    if body.notes_limit > 0:
        note_entities = (
            db.query(Entity)
            .filter(Entity.world_id == world.id, Entity.kind == "note")
            .order_by(Entity.name)
            .limit(body.notes_limit)
            .all()
        )
        seen_ids = {e.id for e in entities}
        extra_notes = [e for e in note_entities if e.id not in seen_ids]
        notes = notes + extra_notes
    combined = non_notes + notes
    return {
        "context": _format_context_from_entities(combined),
        "count": len(non_notes),
        "notes": len(notes),
    }


class _SaveNoteBody(BaseModel):
    title: str
    content: str


@app.post("/api/ai/save-note")
def ai_save_note(
    body: _SaveNoteBody,
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    note = Entity(
        world_id=world.id,
        kind="note",
        name=body.title,
        body=body.content,
        summary=body.content[:120],
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"id": note.id, "name": note.name}


class _SmartGenBody(BaseModel):
    name: str
    kind: str
    subtype: str = ""
    summary: str = ""


@app.post("/api/ai/generate/entity-smart")
async def gen_entity_smart(
    body: _SmartGenBody,
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    related_ctx = ""
    if world:
        related = _find_relevant_entities(db, world.id, f"{body.name} {body.summary}", limit=12)
        related_ctx = _format_context_from_entities(related)
    prompt = (
        f"Write an expanded lore entry for this {body.kind}"
        + (f" ({body.subtype})" if body.subtype else "")
        + f" named '{body.name}'."
        + (f" Summary: {body.summary}." if body.summary else "")
        + ("\n\nRelated world lore for context:\n" + related_ctx if related_ctx else "")
    )
    return {"result": await _ai_module.generate(prompt)}


@app.get("/ai", response_class=HTMLResponse)
def ai_chat_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    entity_counts = {}
    world_system = (
        "You are a creative world-building AI assistant for a Neon & Dragons "
        "cyberpunk-fantasy TTRPG setting. Help the Game Master with world-building, "
        "lore, NPC backstories, plot hooks, and creative writing. Be vivid and immersive."
    )
    if world:
        entity_counts = _kind_counts(db, world, request)
        counts_str = ", ".join(f"{v} {k}s" for k, v in entity_counts.items() if v > 0)
        world_system = (
            f"You are a creative world-building AI assistant for '{world.name}', "
            f"a Neon & Dragons cyberpunk-fantasy TTRPG setting. "
            f"The world currently contains: {counts_str}. "
            f"Help the Game Master with world-building, lore, NPC backstories, plot hooks, "
            f"and creative writing. Be vivid, immersive, and consistent with the cyberpunk-fantasy tone. "
            f"Keep responses focused; expand only when asked."
        )
    return templates.TemplateResponse("ai_chat.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
        "entity_counts": entity_counts, "world_system": world_system,
    })

@app.get("/imagestudio", response_class=HTMLResponse)
def imagestudio(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    settings = get_app_settings(db)
    swarmui_url = (settings.swarmui_external_url or SWARMUI_EXTERNAL_URL).rstrip("/")
    return templates.TemplateResponse("imagestudio.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
        "swarmui_url": swarmui_url,
    })

@app.get("/androidapp", response_class=HTMLResponse)
def androidapp(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    settings = get_app_settings(db)
    android_url = (settings.android_emulator_url or ANDROID_EMULATOR_URL).rstrip("/")
    return templates.TemplateResponse("androidapp.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
        "android_url": android_url,
    })

@app.get("/editor", response_class=HTMLResponse)
def content_editor(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    settings = get_app_settings(db)
    editor_url = (settings.editor_external_url or EDITOR_EXTERNAL_URL).rstrip("/")
    return templates.TemplateResponse("editor_embed.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
        "editor_url": editor_url,
    })

def _settings_context(request: Request, db: Session, active_world: str, tab: str, system_error: str = None):
    world, worlds = get_world_ctx(request, db, active_world)
    settings = get_app_settings(db)
    vis_entities = []
    world_players = []
    if world:
        vis_entities = (
            db.query(Entity)
            .filter(Entity.world_id == world.id)
            .order_by(Entity.kind, Entity.name)
            .all()
        )
        world_players = _world_player_list(db, world.id)
    allowed_by_entity = {}
    if vis_entities:
        rows = db.query(entity_player_access.c.entity_id, entity_player_access.c.user_id).filter(
            entity_player_access.c.entity_id.in_([e.id for e in vis_entities])
        ).all()
        for eid, uid in rows:
            allowed_by_entity.setdefault(eid, set()).add(uid)
    return {
        "request": request, "world": world, "worlds": worlds,
        "settings": settings,
        "active_tab": tab if tab in ("options", "system", "visibility", "navigation") else "options",
        "env_ollama_model": _ai_module.OLLAMA_MODEL,
        "env_ollama_url": _ai_module.OLLAMA_URL,
        "env_swarmui_external_url": SWARMUI_EXTERNAL_URL,
        "env_android_emulator_url": ANDROID_EMULATOR_URL,
        "env_editor_external_url": EDITOR_EXTERNAL_URL,
        "env_whisper_url": _ai_module.WHISPER_URL,
        "system_error": system_error,
        "vis_entities": vis_entities,
        "world_players": world_players,
        "allowed_by_entity": allowed_by_entity,
        "nav_catalog": _nav_menus_module.build_catalog(world),
        "initial_nav_menus": _nav_menus_module.load_nav_menus(world) if world else [],
        "nav_max_menus": _nav_menus_module.MAX_NAV_MENUS,
    }

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None), tab: str = "options"):
    return templates.TemplateResponse("settings.html", _settings_context(request, db, active_world, tab))

@app.post("/settings")
def settings_save(
    static_format: str = Form("avif"),
    animated_format: str = Form("avif"),
    hover_preview_enabled: Optional[str] = Form(None),
    hover_preview_delay_seconds: float = Form(5.0),
    hover_preview_hide_delay_seconds: float = Form(0.4),
    hover_preview_width_px: int = Form(340),
    hover_preview_max_height_px: int = Form(420),
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    settings.static_format = static_format if static_format in ("none", "avif", "webp") else "avif"
    settings.animated_format = animated_format if animated_format in ("none", "avif", "webp") else "avif"
    settings.hover_preview_enabled = hover_preview_enabled is not None
    # Clamped rather than rejected with an error page — a GM fat-fingering
    # "0" or "9999" should just get a sane bound, not a round trip to fix a form.
    settings.hover_preview_delay_ms = int(max(0.5, min(30.0, hover_preview_delay_seconds)) * 1000)
    settings.hover_preview_hide_delay_ms = int(max(0.0, min(10.0, hover_preview_hide_delay_seconds)) * 1000)
    settings.hover_preview_width_px = max(220, min(800, hover_preview_width_px))
    settings.hover_preview_max_height_px = max(150, min(1000, hover_preview_max_height_px))
    db.commit()
    return RedirectResponse("/settings", status_code=303)

@app.post("/settings/system")
def settings_system_save(
    request: Request,
    ollama_model: str = Form(""),
    ollama_url: str = Form(""),
    swarmui_external_url: str = Form(""),
    android_emulator_url: str = Form(""),
    editor_external_url: str = Form(""),
    whisper_url: str = Form(""),
    dreamlands_enabled: Optional[str] = Form(None),
    king_in_yellow_enabled: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    ollama_model = ollama_model.strip()
    ollama_url = ollama_url.strip().rstrip("/")
    swarmui_external_url = swarmui_external_url.strip().rstrip("/")
    android_emulator_url = android_emulator_url.strip().rstrip("/")
    editor_external_url = editor_external_url.strip().rstrip("/")
    whisper_url = whisper_url.strip().rstrip("/")
    for label, val in (
        ("Ollama URL", ollama_url),
        ("SwarmUI external URL", swarmui_external_url),
        ("Android emulator URL", android_emulator_url),
        ("Content editor URL", editor_external_url),
        ("Whisper URL", whisper_url),
    ):
        if val and not (val.startswith("http://") or val.startswith("https://")):
            return templates.TemplateResponse(
                "settings.html",
                _settings_context(request, db, active_world, "system",
                                   system_error=f"{label} must start with http:// or https://"),
                status_code=400,
            )
    settings = get_app_settings(db)
    settings.ollama_model = ollama_model
    settings.ollama_url = ollama_url
    settings.swarmui_external_url = swarmui_external_url
    settings.android_emulator_url = android_emulator_url
    settings.editor_external_url = editor_external_url
    settings.whisper_url = whisper_url
    settings.dreamlands_enabled = dreamlands_enabled is not None
    settings.king_in_yellow_enabled = king_in_yellow_enabled is not None
    db.commit()
    _refresh_settings_overrides(db)
    return RedirectResponse("/settings?tab=system", status_code=303)

@app.get("/boards", response_class=HTMLResponse)
def boards_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    boards = db.query(InvestBoard).filter(InvestBoard.world_id == (world.id if world else 1)).all()
    return templates.TemplateResponse("boards.html", {
        "request": request, "world": world, "worlds": worlds,
        "boards": boards, "kinds": KINDS, "kind_icons": KIND_ICONS,
    })

@app.get("/boards/new", response_class=HTMLResponse)
def board_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("board_new.html", {
        "request": request, "world": world, "worlds": worlds,
        "kinds": KINDS, "kind_icons": KIND_ICONS,
    })

@app.post("/boards/new")
def board_new_post(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    canvas_bg: str = Form("cork"),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"
    base_slug = slug
    i = 2
    while db.query(InvestBoard).filter(InvestBoard.slug == slug).first():
        slug = f"{base_slug}-{i}"; i += 1
    b = InvestBoard(world_id=world.id if world else 1, name=name, slug=slug,
                    description=description, canvas_bg=canvas_bg)
    db.add(b); db.commit()
    return RedirectResponse(f"/boards/{slug}", status_code=303)

@app.get("/boards/{slug}", response_class=HTMLResponse)
def board_view(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    # Build entity name→{id,kind,image_url} map for quick lookup — scoped to the
    # board's own world (not every entity in the database) so a board with a
    # handful of nodes doesn't load every lore entry across every world.
    entities = db.query(Entity.id, Entity.name, Entity.kind, Entity.image_url).filter(
        Entity.world_id == b.world_id
    ).all()
    entity_list = [{"id": e.id, "name": e.name, "kind": e.kind, "image_url": e.image_url} for e in entities]
    # nodes_json may be a legacy bare array OR the new {nodes, groups} object
    raw_nodes = json.loads(b.nodes_json or "[]")
    if isinstance(raw_nodes, list):
        nodes_payload = {"nodes": raw_nodes, "groups": []}
    else:
        nodes_payload = {"nodes": raw_nodes.get("nodes", []), "groups": raw_nodes.get("groups", [])}
    edges = json.loads(b.edges_json or "[]")
    return templates.TemplateResponse("board.html", {
        "request": request, "world": world, "worlds": worlds,
        "board": b, "kinds": KINDS, "kind_icons": KIND_ICONS,
        "nodes_json": json.dumps(nodes_payload),
        "edges_json": json.dumps(edges),
        "entity_list_json": json.dumps(entity_list),
    })

@app.post("/boards/{slug}/save")
async def board_save(slug: str, request: Request, db: Session = Depends(get_db)):
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    body = await request.json()
    # stash nodes + groups together to avoid schema change
    b.nodes_json = json.dumps({"nodes": body.get("nodes", []), "groups": body.get("groups", [])})
    b.edges_json = json.dumps(body.get("edges", []))
    db.commit()
    return {"ok": True}

@app.post("/boards/{slug}/delete")
def board_delete(slug: str, db: Session = Depends(get_db)):
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b: raise HTTPException(404)
    db.delete(b); db.commit()
    return RedirectResponse("/boards", status_code=303)

@app.get("/boards/{slug}/export", response_class=HTMLResponse)
def board_export(slug: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    from datetime import date as _date
    world, worlds = get_world_ctx(request, db, active_world)
    b = db.query(InvestBoard).filter(InvestBoard.slug == slug).first()
    if not b:
        raise HTTPException(404)
    raw_nodes = json.loads(b.nodes_json or "[]")
    if isinstance(raw_nodes, list):
        nodes = raw_nodes
        groups = []
    else:
        nodes = raw_nodes.get("nodes", [])
        groups = raw_nodes.get("groups", [])
    edges = json.loads(b.edges_json or "[]")
    xs = [n.get("x", 0) for n in nodes] or [0]
    ys = [n.get("y", 0) for n in nodes] or [0]
    canvas_w = max(xs) + 300
    canvas_h = max(ys) + 200
    resp = templates.TemplateResponse("board_export.html", {
        "request": request, "world": world, "worlds": worlds,
        "board": b, "nodes": nodes, "edges": edges, "groups": groups,
        "canvas_w": canvas_w, "canvas_h": canvas_h,
        "export_date": _date.today().isoformat(),
    })
    resp.headers["Content-Disposition"] = f'attachment; filename="{slug}-board.html"'
    return resp

# ── Export & Backup hub ─────────────────────────────────────────────────────────
# nd-world has several distinct export/backup mechanisms (full DB+files backup,
# a readable "book" export, single-file JSON, split-file JSON) that all sounded
# like "Export" scattered across the nav bar and the /worlds page — this single
# page gathers them with a one-line explanation of when to use which, instead of
# guessing from near-identical button labels in two different places.

@app.get("/export", response_class=HTMLResponse)
def export_hub(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("export_hub.html", {
        "request": request, "world": world, "worlds": worlds,
    })

@app.get("/export/book.zip")
def world_export_book(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    import zipfile, io as _io
    from datetime import date
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        return RedirectResponse("/worlds")

    image_files: dict[str, bytes] = {}  # zip path -> bytes

    entities_raw = (
        db.query(Entity)
        .filter(Entity.world_id == world.id)
        .order_by(Entity.kind, Entity.name)
        .all()
    )
    entities_by_kind: dict[str, list] = {}
    for ent in entities_raw:
        ent.image_rel = None  # type: ignore[attr-defined]
        if ent.image_url and ent.image_url.startswith("/uploads/"):
            try:
                rel = ent.image_url[len("/uploads/"):]
                img_path = UPLOADS_DIR / rel
                if img_path.exists():
                    zip_img_path = "assets/images/" + rel.replace("\\", "/")
                    image_files[zip_img_path] = img_path.read_bytes()
                    ent.image_rel = "./" + zip_img_path  # type: ignore[attr-defined]
            except Exception:
                pass
        raw_html = render_md(ent.body) if ent.body else ""
        ent.body_html = re.sub(r'^<h1[^>]*>.*?</h1>\s*', '', raw_html, count=1, flags=re.DOTALL)  # type: ignore[attr-defined]
        entities_by_kind.setdefault(ent.kind, []).append(ent)

    raw_boards = db.query(InvestBoard).filter(InvestBoard.world_id == world.id).all()
    boards_export = []
    for b in raw_boards:
        raw_nodes = json.loads(b.nodes_json or "[]")
        nodes = raw_nodes if isinstance(raw_nodes, list) else raw_nodes.get("nodes", [])
        edges = json.loads(b.edges_json or "[]")
        boards_export.append({"name": b.name, "description": b.description, "nodes": nodes, "edges": edges})

    maps_export = [
        {"name": d.get("name", s), "markers": len(d.get("markers", []))}
        for s, d in _iter_world_maps(world.id)
    ]

    rules_md = _world_rules_markdown(world)
    rules_html = render_md(rules_md) if rules_md else ""

    export_kinds, export_kind_icons = deps.effective_kinds(world)
    html = templates.env.get_template("world_export.html").render(
        world=world, worlds=worlds, kinds=export_kinds, kind_icons=export_kind_icons,
        entities_by_kind=entities_by_kind, boards=boards_export,
        maps=maps_export, rules_html=rules_html,
        export_date=date.today().isoformat(),
    )

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html.encode("utf-8"))
        css_path = BASE_DIR / "static" / "style.css"
        if css_path.exists():
            zf.writestr("assets/style.css", css_path.read_bytes())
        for zpath, data in image_files.items():
            zf.writestr(zpath, data)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{world.slug}-worldbook.zip"'},
    )


@app.get("/export/rules-and-notes.md")
def export_rules_and_notes(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # Not in _is_player_safe, so /export and everything under it is already
    # GM-only via the auth_gate middleware — unlike the per-entity/kind
    # downloads above, this always includes every note unfiltered (it's a GM
    # prep/archival export, same trust level as Full Backup and World Book).
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    parts = [f"# {world.name} — Rules & Notes", "", _world_rules_markdown(world).rstrip(), ""]
    notes = (
        db.query(EntityNote)
        .join(Entity, EntityNote.entity_id == Entity.id)
        .filter(Entity.world_id == world.id)
        .order_by(Entity.kind, Entity.name, EntityNote.created_at)
        .all()
    )
    if notes:
        parts += ["---", "", "## Notes", ""]
        last_entity_id = None
        for note in notes:
            if note.entity_id != last_entity_id:
                parts += [f"### {note.entity.name} ({note.entity.kind.capitalize()})", ""]
                last_entity_id = note.entity_id
            parts += [note.content, ""]
    content = "\n".join(parts).rstrip() + "\n"
    filename = f"{world.slug}-rules-and-notes.md"
    return StreamingResponse(
        io.BytesIO(content.encode()), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _entity_to_foundry_journal(db: Session, entity: Entity) -> dict:
    """A single Foundry VTT JournalEntry document (v10+ page-based schema)
    for one entity — same system-agnostic approach as characters.py's
    _pc_to_foundry_journal, so it imports cleanly into any Foundry world
    regardless of which game system module is installed there. GM-only
    export, so (like Rules and Notes) notes are included unfiltered."""
    kind_label = entity.kind.capitalize() + (f" — {entity.subtype}" if entity.subtype else "")
    parts = []
    if entity.summary:
        parts.append(f"<p><em>{html.escape(entity.summary)}</em></p>")
    parts.append(render_md(entity.body) if entity.body else "<p><em>No description.</em></p>")
    pages = [{"name": "Overview", "type": "text", "text": {"format": 1, "content": "".join(parts)}, "sort": 0}]

    notes = db.query(EntityNote).filter(EntityNote.entity_id == entity.id).order_by(EntityNote.created_at).all()
    if notes:
        notes_html = "".join(render_md(n.content) for n in notes)
        pages.append({"name": "Notes", "type": "text", "text": {"format": 1, "content": notes_html}, "sort": 100})

    return {
        "name": f"[{kind_label}] {entity.name}",
        "folder": None,
        "pages": pages,
        "flags": {"nd-world": {"source": "nd-world", "entity_id": entity.id, "kind": entity.kind}},
    }


def _rules_to_foundry_journal(world: World) -> dict:
    content = render_md(_world_rules_markdown(world))
    return {
        "name": "Rules",
        "folder": None,
        "pages": [{"name": "Rules", "type": "text", "text": {"format": 1, "content": content}, "sort": 0}],
        "flags": {"nd-world": {"source": "nd-world", "kind": "rules"}},
    }


@app.get("/export/foundry.json")
def export_foundry(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    # Not in _is_player_safe, so /export and everything under it is already
    # GM-only via the auth_gate middleware — same trust level as Rules and
    # Notes, Full Backup, and World Book.
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    documents = [_rules_to_foundry_journal(world)]
    entities = db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.kind, Entity.name).all()
    documents += [_entity_to_foundry_journal(db, e) for e in entities]
    pcs = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id).order_by(PlayerCharacter.name).all()
    documents += [_pc_to_foundry_journal(pc) for pc in pcs]
    payload = json.dumps(documents, ensure_ascii=False, indent=2)
    filename = f"{world.slug}-foundry.json"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/backup.zip")
def admin_backup(db: Session = Depends(get_db)):
    # Not in _is_player_safe, so the auth_gate middleware already denies this to
    # non-GM users by default — this is the only thing standing between a player
    # and a copy of the whole database, so don't add it to the allowlist above.
    import sqlite3
    import tempfile
    import zipfile
    from datetime import datetime, timezone

    db_path = Path(os.environ.get("DB_PATH", "/data/world.db"))

    manifest = {
        "app": "nd-world",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": {
            "worlds": db.query(World).count(),
            "entities": db.query(Entity).count(),
            "users": db.query(User).count(),
            "world_memberships": db.query(WorldMembership).count(),
            "invite_codes": db.query(InviteCode).count(),
            "player_characters": db.query(PlayerCharacter).count(),
            "combat_sessions": db.query(CombatSession).count(),
            "quests": db.query(Quest).count(),
            "parties": db.query(Party).count(),
            "game_sessions": db.query(GameSession).count(),
            "schematics": db.query(Schematic).count(),
            "map_overlays": db.query(MapOverlay).count(),
            "invest_boards": db.query(InvestBoard).count(),
            "private_notes": db.query(PrivateNote).count(),
            "entity_notes": db.query(EntityNote).count(),
            "entity_templates": db.query(EntityTemplate).count(),
        },
    }

    with tempfile.TemporaryDirectory() as tmp:
        # VACUUM INTO produces a consistent, defragmented snapshot in one statement —
        # copying world.db directly while uvicorn holds it open risks capturing a
        # half-written page mid-write.
        snapshot_path = Path(tmp) / "world.db"
        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute("VACUUM INTO ?", (str(snapshot_path),))
        finally:
            raw.close()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot_path, "world.db")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            if UPLOADS_DIR.exists():
                for f in UPLOADS_DIR.rglob("*"):
                    if f.is_file():
                        zf.write(f, "uploads/" + str(f.relative_to(UPLOADS_DIR)))
            if _MAPS_DIR.exists():
                for f in _MAPS_DIR.rglob("*"):
                    if f.is_file():
                        zf.write(f, "maps/" + str(f.relative_to(_MAPS_DIR)))
        buf.seek(0)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="nd-world-backup-{stamp}.zip"'},
    )

# ── List ──────────────────────────────────────────────────────────────────────

_COL_PRIORITY = [
    "Rank",
    "Rarity", "Damage", "Armor", "Rounds", "Strength", "Body", "Dexterity",
    "Perception", "Augment Slots", "Max Health", "Max PP", "Feats", "Cost",
    "Difficulty to craft", "Effect", "Type of Feat", "Requirement",
    "Requirements", "Special conditions",
]
_COL_PRIORITY_IDX = {c.lower(): i for i, c in enumerate(_COL_PRIORITY)}

@app.get("/kind/{kind}", response_class=HTMLResponse)
def list_entities(request: Request, kind: str, q: str = "", folder: Optional[str] = None,
                  view: str = "", db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)

    # Base searchable query (no folder filter — used for counts/sidebar)
    base_q = db.query(Entity).filter(Entity.kind == kind, Entity.world_id == world.id)
    base_q = _filter_visible_entities(base_q, request)
    if q:
        base_q = base_q.filter(or_(
            Entity.name.ilike(f"%{q}%"), Entity.tags.ilike(f"%{q}%"),
            Entity.summary.ilike(f"%{q}%"), Entity.body.ilike(f"%{q}%"),
        ))

    # Folder counts and list (computed before entity query so we can detect parent folders)
    count_rows = (base_q.with_entities(Entity.folder, func.count(Entity.id))
                  .group_by(Entity.folder).all())
    folder_counts = {(row[0] or ""): row[1] for row in count_rows}
    total_count = sum(folder_counts.values())
    folders = sorted(k for k in folder_counts if k)

    # Detect parent folder (has child folders) and apply recursive or exact query
    is_parent_folder = False
    query = base_q
    if folder is not None:
        if folder:
            child_folders = [f for f in folders if f.startswith(folder + '/')]
            is_parent_folder = bool(child_folders)
            if is_parent_folder:
                query = query.filter(
                    or_(Entity.folder == folder, Entity.folder.like(folder + '/%'))
                )
            else:
                query = query.filter(Entity.folder == folder)
        else:
            query = query.filter(Entity.folder.is_(None))

    entities = query.order_by(Entity.folder.nulls_last(), Entity.name).all()

    # Group by folder for root view display
    grouped: dict[str, list] = {}
    for e in entities:
        grouped.setdefault(e.folder or "", []).append(e)

    # Subfolder groups for parent folder table view
    # Key = immediate child path (e.g. "Weapons/Handguns"), value = entities in that subtree
    subfolder_groups: list[tuple[str, list]] = []
    if is_parent_folder:
        gmap: dict[str, list] = {}
        prefix = folder + '/'
        for e in entities:
            if not e.folder or e.folder == folder:
                key = folder
            elif e.folder.startswith(prefix):
                seg = e.folder[len(prefix):].split('/')[0]
                key = prefix + seg
            else:
                key = folder
            gmap.setdefault(key, []).append(e)
        subfolder_groups = sorted(gmap.items(),
                                  key=lambda x: (x[0] == folder, x[0].lower()))

    # Kinds that get stat-column extraction vs. description-only table
    _STAT_KINDS = {"item", "feat", "creature", "character"}
    _DESC_ONLY_KINDS = {"character", "organization", "location", "event", "note"}

    # Character-creation feat folders get a simple Rank+Description table only
    _CHAR_FEAT_ROOTS = ("Race Feats", "Common Feats", "Profession Feats")
    char_feat_folder = (
        kind == "feat" and bool(folder) and
        any(folder == r or folder.startswith(r + "/") for r in _CHAR_FEAT_ROOTS)
    )

    # Stat table columns (derived from all entities currently shown)
    entity_stats: dict[int, dict[str, str]] = {}
    table_cols: list[str] = []
    _rank_re = re.compile(r'\b(rank\s+(\d+)|edge|origin)\b', re.IGNORECASE)
    if folder is not None:
        col_freq: dict[str, int] = {}
        for e in entities:
            d: dict[str, str] = {}
            if kind in _STAT_KINDS and not char_feat_folder:
                rows = parse_stats(e.body or "")
                if rows:
                    d = {r["key"]: r["val"] for r in rows}
            # inject Rank from folder path for character-creation feats
            if kind == "feat" and e.folder:
                m = _rank_re.search(e.folder)
                if m:
                    seg = m.group(1).lower()
                    d["Rank"] = seg.split()[-1] if seg.startswith("rank") else seg.title()
            if d:
                entity_stats[e.id] = d
                for k in d:
                    col_freq[k] = col_freq.get(k, 0) + 1
        if kind in _STAT_KINDS and not char_feat_folder:
            table_cols = sorted(
                col_freq,
                key=lambda k: (_COL_PRIORITY_IDX.get(k.lower(), 999), -col_freq[k])
            )[:8]

    if not view:
        view = "table" if folder is not None else "grid"

    worlds = _visible_worlds(request, db)
    return templates.TemplateResponse("entities/list.html", {
        "request": request, "kind": kind, "entities": entities,
        "grouped": grouped, "folders": folders, "active_folder": folder,
        "folder_counts": folder_counts, "total_count": total_count,
        "q": q, "world": world, "worlds": worlds,
        "view": view, "entity_stats": entity_stats, "table_cols": table_cols,
        "is_parent_folder": is_parent_folder, "subfolder_groups": subfolder_groups,
        "char_feat_folder": char_feat_folder,
    })

# ── Detail ────────────────────────────────────────────────────────────────────

def _entity_view_gate(db: Session, request: Request, entity_id: int) -> Entity:
    """The single entity by id, only if the current viewer is actually
    allowed to see it — raises 404 (not 403) either way, same as the /entity
    detail route this was factored out of, so a player can't distinguish
    "doesn't exist" from "exists but you can't see it" by the status code.
    Shared by the detail page and the hover-preview API so both enforce the
    exact same rule instead of two hand-maintained copies drifting apart."""
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    # World-membership gate: visible_to_players alone is not enough, or a player in one
    # world could read every player-visible entity in every other world by walking IDs.
    ent_world = db.get(World, entity.world_id) if entity.world_id else None
    if not _auth.user_can_access_world(db, user, ent_world):
        raise HTTPException(404)
    if not entity.visible_to_players and not (user and user.is_gm):
        shared = user and db.query(entity_player_access).filter(
            entity_player_access.c.entity_id == entity.id,
            entity_player_access.c.user_id == user.id,
        ).first()
        if not shared:
            raise HTTPException(404)
    return entity


def _visible_entity_notes(db: Session, entity_id: int, request: Request):
    """This entity's notes, filtered to visible_to_players for a non-GM
    viewer — the same rule the detail page and the .md download both need,
    factored out so they can't drift apart."""
    user = getattr(request.state, "user", None)
    notes_q = db.query(EntityNote).filter(EntityNote.entity_id == entity_id)
    if not (user and user.is_gm):
        notes_q = notes_q.filter(EntityNote.visible_to_players.is_(True))
    return notes_q.order_by(EntityNote.created_at).all()


def _entity_to_markdown(db: Session, entity: Entity, request: Request) -> str:
    """Render an entity (name/kind/summary/body) plus whatever notes the
    current viewer can see into a single standalone .md document — used by
    both the single-entity download and the per-kind bulk zip."""
    ent_world = db.get(World, entity.world_id) if entity.world_id else None
    kind_icons = deps.effective_kinds(ent_world)[1]
    kind_label = f"{kind_icons.get(entity.kind, '')} {entity.kind.capitalize()}".strip()
    if entity.subtype:
        kind_label += f" — {entity.subtype}"
    parts = [f"# {entity.name}", "", f"*{kind_label}*", ""]
    if entity.summary:
        parts += [f"*{entity.summary}*", ""]
    parts += ["---", "", entity.body or "", ""]
    notes = _visible_entity_notes(db, entity.id, request)
    if notes:
        parts += ["## Notes", ""]
        for note in notes:
            parts += [note.content, "", "---", ""]
    return "\n".join(parts).rstrip() + "\n"


@app.get("/entity/{entity_id}/download.md")
def entity_download(entity_id: int, request: Request, db: Session = Depends(get_db)):
    entity = _entity_view_gate(db, request, entity_id)
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm):
        ent_world = db.get(World, entity.world_id) if entity.world_id else None
        if not (ent_world and ent_world.players_can_download_entities):
            raise HTTPException(403)
    content = _entity_to_markdown(db, entity, request)
    fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (entity.name or "entity")) or "entity"
    return StreamingResponse(
        io.BytesIO(content.encode()), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}.md"'},
    )


def _entities_zip(db: Session, entities, request: Request) -> io.BytesIO:
    """Zip one .md file per entity (via _entity_to_markdown) — shared by the
    per-kind bulk download and the "Download Selected" bulk-action-bar button
    so both produce identically-shaped zips."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in entities:
            fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (e.name or "entity")) or "entity"
            zf.writestr(f"{fname}-{e.id}.md", _entity_to_markdown(db, e, request))
    buf.seek(0)
    return buf


@app.get("/kind/{kind}/download.zip")
def kind_download(kind: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm) and not world.players_can_download_entities:
        raise HTTPException(403)
    q = db.query(Entity).filter(Entity.world_id == world.id, Entity.kind == kind)
    entities = _filter_visible_entities(q, request).order_by(Entity.name).all()
    buf = _entities_zip(db, entities, request)
    filename = f"{world.slug}-{kind}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/kind/{kind}/download-selected.zip")
def kind_download_selected(
    kind: str, request: Request, db: Session = Depends(get_db),
    active_world: str = Cookie(None), ids: list[int] = Query(default=[], alias="id"),
):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not (user and user.is_gm) and not world.players_can_download_entities:
        raise HTTPException(403)
    q = db.query(Entity).filter(Entity.world_id == world.id, Entity.kind == kind, Entity.id.in_(ids))
    # Same _filter_visible_entities pass as the bulk-kind download — a player
    # can't smuggle a hidden entity's id into the query string to bypass
    # visible_to_players, even with the download toggle on.
    entities = _filter_visible_entities(q, request).order_by(Entity.name).all()
    buf = _entities_zip(db, entities, request)
    filename = f"{world.slug}-{kind}-selected.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/entity/{entity_id}/preview")
def entity_preview(entity_id: int, request: Request, db: Session = Depends(get_db)):
    """Hover-preview popup content (see base.html's dragstart-adjacent
    mouseover handler) — same access rule as the full detail page via
    _entity_view_gate, just returning a small JSON summary instead of the
    whole rendered page."""
    entity = _entity_view_gate(db, request, entity_id)
    ent_world = db.get(World, entity.world_id) if entity.world_id else None
    return {
        "id": entity.id,
        "name": entity.name,
        "kind": entity.kind,
        "kind_icon": deps.effective_kinds(ent_world)[1].get(entity.kind, ""),
        "subtype": entity.subtype,
        "summary": entity.summary,
        "image_url": entity.image_url,
        "tags": [t.strip() for t in (entity.tags or "").split(",") if t.strip()],
        "body_html": render_md(entity.body) if entity.body else "",
    }


@app.get("/api/hover-preview/config")
def hover_preview_config(db: Session = Depends(get_db)):
    """Instance-wide hover-preview settings (Settings > Options), fetched
    once by base.html on every page load — mirrors the existing /api/ai/status
    fetch-on-load pattern rather than needing every route handler to
    remember to pass this through its own template context."""
    settings = get_app_settings(db)
    return {
        "enabled": bool(settings.hover_preview_enabled),
        "delay_ms": settings.hover_preview_delay_ms or 5000,
        "hide_delay_ms": settings.hover_preview_hide_delay_ms if settings.hover_preview_hide_delay_ms is not None else 400,
        "width_px": settings.hover_preview_width_px or 340,
        "max_height_px": settings.hover_preview_max_height_px or 420,
    }


@app.get("/api/spotlight")
def api_spotlight(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Polled every 4s by base.html's spotlight poller (both GM and
    players) — reports the image, if any, a GM has pushed to the world via
    POST /images/spotlight (app/routers/gallery.py). world=None (no active
    world, or the active_world cookie points at a world this user can't
    access — get_world_ctx already filters that out silently) is not an
    error here since this is polled unconditionally on every page."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        return {"version": 0, "image_url": None, "label": None}
    return {
        "version": world.spotlight_version or 0,
        "image_url": world.spotlight_image_url,
        "label": world.spotlight_label,
    }


@app.get("/entity/{entity_id}", response_class=HTMLResponse)
def detail(request: Request, entity_id: int, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    entity = _entity_view_gate(db, request, entity_id)
    user = getattr(request.state, "user", None)
    world = get_active_world(request, db, active_world)
    all_entities = _filter_visible_entities(
        db.query(Entity).filter(Entity.id != entity_id, Entity.world_id == entity.world_id), request
    ).order_by(Entity.name).all()
    worlds = _visible_worlds(request, db)
    backlinks = _filter_visible_entities(
        db.query(Entity)
        .join(entity_links, entity_links.c.source_id == Entity.id)
        .filter(entity_links.c.target_id == entity_id),
        request,
    ).order_by(Entity.kind, Entity.name).all()
    entity_notes = _visible_entity_notes(db, entity_id, request)
    custom_sections = []
    if entity.template_id:
        tpl_fields = json.loads(entity.template.fields_json or "[]") if entity.template else []
        custom_fields = json.loads(entity.custom_fields_json or "{}")
        custom_sections = _group_by_section(tpl_fields)
    else:
        custom_fields = {}
    return templates.TemplateResponse("entities/detail.html", {
        "request": request, "entity": entity, "all_entities": all_entities,
        "world": world, "worlds": worlds, "backlinks": backlinks,
        "entity_notes": entity_notes,
        "custom_sections": custom_sections, "custom_fields": custom_fields,
    })

# ── Entity Field Templates ──────────────────────────────────────────────────

@app.get("/entity-templates", response_class=HTMLResponse)
def entity_templates_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    tpls = _entity_templates_for(db, world.id if world else None)
    tpl_fields_map = {tpl.id: json.loads(tpl.fields_json or "[]") for tpl in tpls}
    return templates.TemplateResponse("entity_templates_list.html", {
        "request": request, "world": world, "worlds": worlds, "tpls": tpls, "kinds": KINDS,
        "tpl_fields_map": tpl_fields_map,
    })


@app.get("/entity-templates/new", response_class=HTMLResponse)
def entity_template_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    return templates.TemplateResponse("entity_template_form.html", {
        "request": request, "world": world, "worlds": worlds, "kinds": KINDS,
        "tpl": None, "fields": [],
    })


@app.post("/entity-templates/new")
async def entity_template_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    form = await request.form()
    name = str(form.get("name", "")).strip() or "Unnamed Template"
    desc = str(form.get("description", "")).strip()
    kind = str(form.get("kind", "")).strip() or None
    if kind not in deps.effective_kinds(world)[0]:
        kind = None
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    base_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50] or "template"
    slug = base_slug
    n = 1
    while db.query(EntityTemplate).filter(EntityTemplate.slug == slug).first():
        slug = f"{base_slug}-{n}"; n += 1
    tpl = EntityTemplate(
        world_id=world.id if world else None, name=name, slug=slug, kind=kind,
        description=desc, is_builtin=False, fields_json=raw_fields,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return RedirectResponse(f"/entity-templates/{tpl.id}/edit", status_code=303)


@app.get("/entity-templates/{tpl_id}/edit", response_class=HTMLResponse)
def entity_template_edit_form(tpl_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    tpl = db.query(EntityTemplate).filter(EntityTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    fields = json.loads(tpl.fields_json or "[]")
    return templates.TemplateResponse("entity_template_form.html", {
        "request": request, "world": world, "worlds": worlds, "kinds": KINDS,
        "tpl": tpl, "fields": fields,
    })


@app.post("/entity-templates/{tpl_id}/edit")
async def entity_template_update(tpl_id: int, request: Request, db: Session = Depends(get_db)):
    tpl = db.query(EntityTemplate).filter(EntityTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    form = await request.form()
    if not tpl.is_builtin:
        tpl.name = str(form.get("name", tpl.name)).strip() or tpl.name
        tpl.description = str(form.get("description", "")).strip()
        kind = str(form.get("kind", "")).strip() or None
        tpl_world = db.get(World, tpl.world_id) if tpl.world_id else None
        tpl.kind = kind if kind in deps.effective_kinds(tpl_world)[0] else None
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    tpl.fields_json = raw_fields
    db.commit()
    return RedirectResponse(f"/entity-templates/{tpl_id}/edit?saved=1", status_code=303)


@app.post("/entity-templates/{tpl_id}/delete")
def entity_template_delete(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.query(EntityTemplate).filter(EntityTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    if tpl.is_builtin:
        raise HTTPException(403, "Cannot delete built-in templates")
    db.query(Entity).filter(Entity.template_id == tpl_id).update({"template_id": None})
    db.delete(tpl)
    db.commit()
    return RedirectResponse("/entity-templates", status_code=303)


# ── Create ────────────────────────────────────────────────────────────────────

@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request, kind: str = "character", folder: str = "",
             db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    world_players = _world_player_list(db, world.id) if world else []
    folder_options = _kind_folders(db, world.id, kind) if world else []
    entity_templates = _entity_templates_payload(db, world.id) if world else []
    gallery_images = _gallery_module.all_world_image_urls(db, world) if world else []
    return templates.TemplateResponse("entities/form.html", {
        "request": request, "entity": None, "kind": kind,
        "world": world, "worlds": worlds,
        "world_players": world_players, "allowed_player_ids": set(),
        "folder_options": folder_options, "prefill_folder": folder,
        "entity_templates": entity_templates, "custom_fields": {},
        "gallery_images": gallery_images,
    })

@app.post("/new")
async def create(
    request: Request,
    kind: str = Form(...), subtype: str = Form(""), name: str = Form(...),
    folder: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
    image_file: UploadFile = File(None), summary: str = Form(""), body: str = Form(""),
    visibility_mode: str = Form("everyone"),
    allowed_player_ids: List[int] = Form([]),
    template_id: Optional[str] = Form(None),
    custom_fields_json: str = Form("{}"),
    db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    world = get_active_world(request, db, active_world)
    final_image = save_upload(image_file, db=db) or (image_url.strip() or None)
    try:
        json.loads(custom_fields_json)
    except Exception:
        custom_fields_json = "{}"
    e = Entity(world_id=world.id, kind=kind, subtype=subtype or None, name=name,
               folder=folder.strip() or None, tags=tags or None,
               image_url=final_image, summary=summary or None, body=body or None,
               visible_to_players=(visibility_mode == "everyone"),
               template_id=int(template_id) if template_id and template_id.isdigit() else None,
               custom_fields_json=custom_fields_json)
    db.add(e)
    db.commit()
    db.refresh(e)
    _sync_entity_access(db, e.id, allowed_player_ids if visibility_mode == "players" else [])
    return RedirectResponse(f"/entity/{e.id}", status_code=303)

# ── Edit ──────────────────────────────────────────────────────────────────────

@app.get("/entity/{entity_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, entity_id: int, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    world = get_active_world(request, db, active_world)
    worlds = _visible_worlds(request, db)
    world_players = _world_player_list(db, entity.world_id)
    allowed_player_ids = {
        r[0] for r in db.query(entity_player_access.c.user_id)
        .filter(entity_player_access.c.entity_id == entity_id).all()
    }
    folder_options = _kind_folders(db, entity.world_id, entity.kind)
    entity_templates = _entity_templates_payload(db, entity.world_id)
    custom_fields = json.loads(entity.custom_fields_json or "{}")
    # entity's own world, not the ambient active_world cookie (`world` above) —
    # same reasoning as world_players just above using entity.world_id: the GM
    # could be editing an entity while a different world is active.
    entity_world = db.get(World, entity.world_id)
    gallery_images = _gallery_module.all_world_image_urls(db, entity_world) if entity_world else []
    return templates.TemplateResponse("entities/form.html", {
        "request": request, "entity": entity, "kind": entity.kind,
        "world": world, "worlds": worlds,
        "world_players": world_players, "allowed_player_ids": allowed_player_ids,
        "folder_options": folder_options, "prefill_folder": "",
        "entity_templates": entity_templates, "custom_fields": custom_fields,
        "gallery_images": gallery_images,
    })

@app.post("/entity/{entity_id}/edit")
async def update(
    entity_id: int,
    kind: str = Form(...), subtype: str = Form(""), name: str = Form(...),
    folder: str = Form(""), tags: str = Form(""), image_url: str = Form(""),
    image_file: UploadFile = File(None), summary: str = Form(""), body: str = Form(""),
    visibility_mode: str = Form("everyone"),
    allowed_player_ids: List[int] = Form([]),
    remove_image: Optional[str] = Form(None),
    template_id: Optional[str] = Form(None),
    custom_fields_json: str = Form("{}"),
    db: Session = Depends(get_db),
):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    uploaded = save_upload(image_file, db=db)
    entity.kind = kind
    entity.subtype = subtype or None
    entity.folder = folder.strip() or None
    entity.name = name
    entity.tags = tags or None
    # A new upload or pasted URL replaces the image; the "Remove image" checkbox
    # clears it explicitly; otherwise leave the existing image untouched — the
    # image_url text field is deliberately blank for uploaded images (an internal
    # /uploads/... path isn't meant to be edited there), so treating "blank" as
    # "clear the image" would wipe out every uploaded image on every edit.
    if uploaded:
        entity.image_url = uploaded
    elif image_url.strip():
        entity.image_url = image_url.strip()
    elif remove_image:
        entity.image_url = None
    entity.summary = summary or None
    entity.body = body or None
    entity.visible_to_players = (visibility_mode == "everyone")
    entity.template_id = int(template_id) if template_id and template_id.isdigit() else None
    try:
        json.loads(custom_fields_json)
        entity.custom_fields_json = custom_fields_json
    except Exception:
        entity.custom_fields_json = "{}"
    db.commit()
    _sync_entity_access(db, entity_id, allowed_player_ids if visibility_mode == "players" else [])
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# ── Delete ────────────────────────────────────────────────────────────────────

@app.post("/entity/{entity_id}/delete")
def delete(entity_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    # World-ownership check — previously missing here (every other delete
    # route added this session, e.g. races/professions, already had it): a
    # GM with access to only one world could otherwise delete any entity in
    # any world on this instance just by walking IDs, since nothing tied the
    # id to the currently active world.
    world = get_active_world(request, db, active_world)
    if not world or entity.world_id != world.id:
        raise HTTPException(404)
    db.execute(entity_links.delete().where(
        (entity_links.c.source_id == entity_id) | (entity_links.c.target_id == entity_id)
    ))
    db.execute(entity_player_access.delete().where(entity_player_access.c.entity_id == entity_id))
    db.query(EntityNote).filter(EntityNote.entity_id == entity_id).delete()
    db.delete(entity)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/kind/{kind}/bulk-delete")
async def bulk_delete_entities(kind: str, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    form = await request.form()
    raw_ids = form.getlist("entity_ids")
    ids = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    if ids:
        # Scoped to this kind and the active world — the same boundary every
        # other delete/edit route in this app enforces, so a batch of ids
        # can't be used to reach into another world or another entity kind.
        entities = db.query(Entity).filter(
            Entity.id.in_(ids), Entity.kind == kind, Entity.world_id == world.id
        ).all()
        matched_ids = [e.id for e in entities]
        if matched_ids:
            db.execute(entity_links.delete().where(
                entity_links.c.source_id.in_(matched_ids) | entity_links.c.target_id.in_(matched_ids)
            ))
            db.execute(entity_player_access.delete().where(entity_player_access.c.entity_id.in_(matched_ids)))
            db.query(EntityNote).filter(EntityNote.entity_id.in_(matched_ids)).delete(synchronize_session=False)
            for e in entities:
                db.delete(e)
            db.commit()

    folder = form.get("folder")
    q = form.get("q") or ""
    redirect = f"/kind/{kind}"
    params = []
    if folder is not None:
        params.append(f"folder={quote(folder)}")
    if q:
        params.append(f"q={quote(q)}")
    if params:
        redirect += "?" + "&".join(params)
    return RedirectResponse(with_world(redirect, world), status_code=303)


@app.post("/api/entities/bulk-visibility")
async def bulk_set_visibility(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Set visible_to_players (and, for 'players' mode, the per-player
    allow-list) on a batch of entities at once — the Settings > Visibility
    tab's bulk-apply action. Same visibility semantics as the per-entity
    edit form's visibility_mode radio group, just applied to many rows."""
    world = get_active_world(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    ids = []
    for v in body.get("entity_ids") or []:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    mode = str(body.get("visibility_mode", "")).strip()
    if mode not in ("everyone", "gm", "players"):
        raise HTTPException(400, "visibility_mode must be one of: everyone, gm, players")
    allowed_player_ids = body.get("allowed_player_ids") or []

    entities = (
        db.query(Entity).filter(Entity.id.in_(ids), Entity.world_id == world.id).all()
        if ids else []
    )
    for e in entities:
        e.visible_to_players = (mode == "everyone")
    db.commit()
    for e in entities:
        _sync_entity_access(db, e.id, allowed_player_ids if mode == "players" else [])
    return {"updated": len(entities)}

# ── Relations ─────────────────────────────────────────────────────────────────

@app.post("/entity/{entity_id}/link/{target_id}")
def link(entity_id: int, target_id: int, db: Session = Depends(get_db)):
    src = db.get(Entity, entity_id)
    tgt = db.get(Entity, target_id)
    if not src or not tgt:
        raise HTTPException(404)
    if tgt not in src.related:
        src.related.append(tgt)
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

@app.post("/entity/{entity_id}/unlink/{target_id}")
def unlink(entity_id: int, target_id: int, db: Session = Depends(get_db)):
    src = db.get(Entity, entity_id)
    tgt = db.get(Entity, target_id)
    if src and tgt and tgt in src.related:
        src.related.remove(tgt)
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# ── Entity notes (GM-only management; hide/un-hide independent of the entity) ──

@app.post("/entity/{entity_id}/notes/new")
def add_entity_note(
    entity_id: int, request: Request,
    content: str = Form(...), visible: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    content = content.strip()
    if content:
        db.add(EntityNote(
            entity_id=entity_id, author_id=user.id if user else None,
            content=content, visible_to_players=bool(visible),
        ))
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# A converted/extracted note is just text — this is generous headroom for a
# real session-notes export (which can carry a few embedded images even
# though the html/pdf paths below never keep them) or a portrait-sized
# image, without being an unbounded upload.
MAX_NOTE_IMPORT_BYTES = int(os.environ.get("MAX_NOTE_IMPORT_BYTES", str(10 * 1024 * 1024)))
_NOTE_IMPORT_TEXT_EXTS = {".md", ".markdown", ".txt"}
_NOTE_IMPORT_HTML_EXTS = {".html", ".htm"}
_NOTE_IMPORT_PDF_EXTS = {".pdf"}
_NOTE_IMPORT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

@app.post("/entity/{entity_id}/notes/import")
def import_entity_note(
    entity_id: int, request: Request,
    file: UploadFile = File(...), visible: Optional[str] = Form(None),
    preserve_html: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Create a note from an uploaded file instead of typing it — see the
    "Import note from file" form on entities/detail.html. Each format lands
    in EntityNote.content differently:
      .md/.markdown/.txt — read as-is; it's already markdown/plain text.
      .pdf               — text extracted via pypdf (same approach as the
                            AI-attachment document extractor in
                            routers/ai.py), no layout/formatting preserved.
      .html/.htm         — converted to markdown by default (safe, but
                            drops original styling); check "preserve_html"
                            to instead keep it as sanitized HTML
                            (content_is_html=True) — see
                            rendering.sanitize_note_html for what survives.
      .png/.jpg/.jpeg/.gif/.webp — saved as an upload, note content is just
                            a markdown image reference to it.
    """
    entity = db.get(Entity, entity_id)
    if not entity:
        raise HTTPException(404)
    ext = Path(file.filename or "").suffix.lower()
    is_html = False

    if ext in _NOTE_IMPORT_IMAGE_EXTS:
        url = save_upload(file, subdir="entity_notes", db=db)
        if not url:
            raise HTTPException(400, "Could not save image")
        content = f"![{entity.name}]({url})"
    elif ext in _NOTE_IMPORT_TEXT_EXTS:
        raw = read_upload_bounded(file, max_bytes=MAX_NOTE_IMPORT_BYTES)
        content = raw.decode("utf-8", errors="replace").strip()
    elif ext in _NOTE_IMPORT_PDF_EXTS:
        raw = read_upload_bounded(file, max_bytes=MAX_NOTE_IMPORT_BYTES)
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            content = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception:
            raise HTTPException(400, "Could not extract text from this PDF")
    elif ext in _NOTE_IMPORT_HTML_EXTS:
        raw = read_upload_bounded(file, max_bytes=MAX_NOTE_IMPORT_BYTES)
        text = raw.decode("utf-8", errors="replace")
        if preserve_html:
            content = sanitize_note_html(text).strip()
            is_html = True
        else:
            content = html_to_markdown(text).strip()
    else:
        allowed = sorted(_NOTE_IMPORT_TEXT_EXTS | _NOTE_IMPORT_HTML_EXTS | _NOTE_IMPORT_PDF_EXTS | _NOTE_IMPORT_IMAGE_EXTS)
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(allowed)}")

    if content:
        user = getattr(request.state, "user", None)
        db.add(EntityNote(
            entity_id=entity_id, author_id=user.id if user else None,
            content=content, visible_to_players=bool(visible), content_is_html=is_html,
        ))
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

@app.post("/entity/{entity_id}/notes/{note_id}/toggle")
def toggle_entity_note(entity_id: int, note_id: int, db: Session = Depends(get_db)):
    note = db.get(EntityNote, note_id)
    if not note or note.entity_id != entity_id:
        raise HTTPException(404)
    note.visible_to_players = not note.visible_to_players
    db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

@app.post("/entity/{entity_id}/notes/{note_id}/delete")
def delete_entity_note(entity_id: int, note_id: int, db: Session = Depends(get_db)):
    note = db.get(EntityNote, note_id)
    if note and note.entity_id == entity_id:
        db.delete(note)
        db.commit()
    return RedirectResponse(f"/entity/{entity_id}", status_code=303)

# ── Search ────────────────────────────────────────────────────────────────────

def _snippet(text: str, q: str, window: int = 120) -> str:
    if not text or not q:
        return ""
    low = text.lower()
    idx = low.find(q.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window // 2)
    end = min(len(text), idx + window // 2)
    snippet = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
    # Escape before highlighting — entity/rules bodies are raw author-supplied
    # text, and this snippet is rendered with |safe, so any literal HTML in it
    # (e.g. a <script> tag typed into an entity body) must be inert by the time
    # it reaches the template.
    escaped = html.escape(snippet)
    pattern = re.compile(re.escape(html.escape(q)), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group()}</mark>", escaped)

_SEARCH_RESULT_CAP = 25  # per result type — this is a quick-jump search box, not a report


def _search_characters(db: Session, world: World, request: Request, q: str) -> list[dict]:
    """Player-reachable: a player may see their own character(s) plus, if the
    GM has players_see_party on, other party members' — the same rule
    characters.py's _can_view_character applies to the character sheet itself,
    so search can't surface anything a player couldn't already click through to."""
    user = getattr(request.state, "user", None)
    is_gm = bool(user and user.is_gm)
    pc_q = db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world.id,
        or_(
            PlayerCharacter.name.ilike(f"%{q}%"),
            PlayerCharacter.player_name.ilike(f"%{q}%"),
            PlayerCharacter.race.ilike(f"%{q}%"),
            PlayerCharacter.char_class.ilike(f"%{q}%"),
            PlayerCharacter.backstory.ilike(f"%{q}%"),
            PlayerCharacter.notes.ilike(f"%{q}%"),
        ),
    )
    if not is_gm:
        if not user:
            return []
        if world.players_see_party:
            pc_q = pc_q.filter(or_(
                PlayerCharacter.owner_user_id == user.id,
                PlayerCharacter.owner_user_id.isnot(None),
            ))
        else:
            pc_q = pc_q.filter(PlayerCharacter.owner_user_id == user.id)
    out = []
    for pc in pc_q.order_by(PlayerCharacter.name).limit(_SEARCH_RESULT_CAP).all():
        subtitle = " / ".join(x for x in (pc.race, pc.char_class) if x)
        snippet = "" if q.lower() in (pc.name or "").lower() else _snippet(pc.backstory or pc.notes or "", q)
        out.append({"title": pc.name, "subtitle": subtitle, "url": f"/characters/{pc.id}",
                    "icon": "🎲", "snippet": snippet})
    return out


def _search_quests(db: Session, world: World, q: str) -> list[dict]:
    quest_q = db.query(Quest).filter(
        Quest.world_id == world.id,
        or_(Quest.title.ilike(f"%{q}%"), Quest.summary.ilike(f"%{q}%"), Quest.body.ilike(f"%{q}%")),
    )
    out = []
    for quest in quest_q.order_by(Quest.title).limit(_SEARCH_RESULT_CAP).all():
        snippet = "" if q.lower() in (quest.title or "").lower() else _snippet(quest.body or quest.summary or "", q)
        out.append({"title": quest.title, "subtitle": (quest.status or "").capitalize(),
                    "url": f"/quests/{quest.id}", "icon": "📜", "snippet": snippet})
    return out


def _search_sessions(db: Session, world: World, q: str) -> list[dict]:
    sess_q = db.query(GameSession).filter(
        GameSession.world_id == world.id,
        or_(GameSession.title.ilike(f"%{q}%"), GameSession.summary.ilike(f"%{q}%")),
    )
    out = []
    for s in sess_q.order_by(GameSession.session_num.desc()).limit(_SEARCH_RESULT_CAP).all():
        snippet = "" if q.lower() in (s.title or "").lower() else _snippet(s.summary or "", q)
        out.append({"title": f"Session #{s.session_num}: {s.title}", "subtitle": s.session_date or "",
                    "url": f"/sessions/{s.id}", "icon": "📓", "snippet": snippet})
    return out


def _search_notes(db: Session, world: World, request: Request, q: str) -> list[dict]:
    """EntityNote, not PrivateNote — these are GM-authored notes pinned to a
    lore entity, so they already carry the visible_to_players flag that
    determines whether search may surface them to a non-GM viewer."""
    user = getattr(request.state, "user", None)
    is_gm = bool(user and user.is_gm)
    note_q = (
        db.query(EntityNote, Entity.name, Entity.id)
        .join(Entity, Entity.id == EntityNote.entity_id)
        .filter(Entity.world_id == world.id, EntityNote.content.ilike(f"%{q}%"))
    )
    if not is_gm:
        note_q = note_q.filter(EntityNote.visible_to_players.is_(True))
    out = []
    for note, ent_name, ent_id in note_q.limit(_SEARCH_RESULT_CAP).all():
        out.append({"title": ent_name, "subtitle": "Note", "url": f"/entity/{ent_id}",
                    "icon": "🔒", "snippet": _snippet(note.content or "", q)})
    return out


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", kind: str = "",
           db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world = get_active_world(request, db, active_world)
    results = []
    grouped: dict[str, list] = {}
    snippets: dict[int, str] = {}
    other_grouped: dict[str, list] = {}

    if q:
        query = db.query(Entity).filter(
            Entity.world_id == world.id,
            or_(
                Entity.name.ilike(f"%{q}%"),
                Entity.tags.ilike(f"%{q}%"),
                Entity.summary.ilike(f"%{q}%"),
                Entity.body.ilike(f"%{q}%"),
            )
        )
        query = _filter_visible_entities(query, request)
        if kind:
            query = query.filter(Entity.kind == kind)
        results = query.order_by(Entity.kind, Entity.name).all()

        for e in results:
            grouped.setdefault(e.kind, []).append(e)
            # build snippet from body if name/summary didn't match
            if q.lower() not in (e.name or "").lower() and q.lower() not in (e.summary or "").lower():
                snippets[e.id] = _snippet(e.body or "", q)

        # The entity-kind filter dropdown only makes sense against entities —
        # characters/quests/sessions/notes aren't entity kinds, so leave them
        # out of a kind-filtered search rather than force them under it.
        if not kind:
            user = getattr(request.state, "user", None)
            is_gm = bool(user and user.is_gm)
            other_sections = [("Characters", _search_characters(db, world, request, q))]
            if is_gm:
                other_sections += [
                    ("Quests", _search_quests(db, world, q)),
                    ("Sessions", _search_sessions(db, world, q)),
                ]
            other_sections.append(("Notes", _search_notes(db, world, request, q)))
            other_grouped = {label: items for label, items in other_sections if items}

    worlds = _visible_worlds(request, db)
    return templates.TemplateResponse("search.html", {
        "request": request, "results": results, "grouped": grouped, "other_grouped": other_grouped,
        "snippets": snippets, "q": q, "kind_filter": kind,
        "world": world, "worlds": worlds,
    })

# ── Import API ────────────────────────────────────────────────────────────────

# Not dead code despite predating /import's importer.py — still actively used
# by the standalone import_chronicles.py and import_lore.py scripts in the
# repo root (both POST {"world_id"?, "entities": [...]}). Their request
# shape and the upsert-by-(name,kind,world_id) dedup behavior below must stay
# unchanged; only the validation is new.
_MAX_LEGACY_IMPORT_ENTITIES = 500  # both scripts batch in groups of 10 — generous headroom

@app.post("/api/import")
def api_import(payload: dict, db: Session = Depends(get_db)):
    # item["name"]/item["kind"] (bracket access) previously threw an
    # unhandled KeyError on a malformed entity — a 500 with a non-JSON body
    # (no exception handler is registered anywhere in this app), the same
    # failure mode a client-side JSON.parse() chokes on. world_id was never
    # checked to exist either (SQLite FK enforcement is never turned on —
    # see database.py — so a bogus id silently orphaned rows), and nothing
    # capped how many entities one request could carry.
    try:
        world_id = int(payload.get("world_id", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "world_id must be a number")
    world = db.query(World).filter(World.id == world_id).first()
    if not world:
        raise HTTPException(400, f"World {world_id} does not exist")
    legacy_kinds = deps.effective_kinds(world)[0]

    items = payload.get("entities", [])
    if not isinstance(items, list):
        raise HTTPException(400, '"entities" must be a list')
    if len(items) > _MAX_LEGACY_IMPORT_ENTITIES:
        raise HTTPException(400, f"Too many entities in one batch — limit is {_MAX_LEGACY_IMPORT_ENTITIES}")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(400, f"Item #{i + 1}: not an object")
        name = str(item.get("name") or "").strip()
        kind = item.get("kind")
        if not name or kind not in legacy_kinds:
            raise HTTPException(400, f'Item #{i + 1}: needs "kind" (one of {", ".join(legacy_kinds)}) and a non-empty "name"')

    created = 0
    for item in items:
        item = dict(item)
        item["name"] = str(item["name"]).strip()
        item["world_id"] = world_id
        # Defensive coercion: these columns are all String/Text — a caller
        # sending e.g. tags as a list would otherwise die unhandled at
        # commit time (a SQLite bind-parameter adaptation error, caught
        # nowhere). Both existing scripts already send strings here; this
        # only protects against a future/different caller.
        for col in ("subtype", "folder", "tags", "image_url", "summary", "body"):
            v = item.get(col)
            if v is not None and not isinstance(v, str):
                item[col] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        existing = db.query(Entity).filter(
            Entity.name == item["name"],
            Entity.kind == item["kind"],
            Entity.world_id == world_id,
        ).first()
        if not existing:
            e = Entity(**{k: v for k, v in item.items() if k in ENTITY_COLS})
            db.add(e)
            created += 1
        else:
            if item.get("image_url"):
                existing.image_url = item["image_url"]
            if "body" in item:
                existing.body = item["body"] or None
            if "summary" in item:
                existing.summary = item["summary"] or None
            if "folder" in item:
                existing.folder = item["folder"] or None
            if item.get("subtype") and not existing.subtype:
                existing.subtype = item["subtype"]
    db.commit()
    return {"created": created}

@app.post("/api/upload-image")
async def api_upload_image(file: UploadFile = File(...), db: Session = Depends(get_db)):
    uploaded = save_upload(file, db=db)
    if not uploaded:
        raise HTTPException(400, "Unsupported file type")
    return {"url": uploaded}

@app.post("/api/import/images")
async def api_import_images(
    request: Request,
    files: List[UploadFile] = File(...),
    entity_ids: List[str] = Form(...),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    """Bulk portrait/art import: each file is paired by position with the
    entity_ids entry at the same index (the client does filename-to-entity
    matching itself, using the entity list already embedded in /import) —
    entity_ids[i] == "" means "skip this file". Every entity_id is
    re-validated against the active world server-side rather than trusted
    from the client, the same as any other entity mutation."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    if len(files) != len(entity_ids):
        raise HTTPException(400, "files and entity_ids must be the same length")
    if len(files) > BULK_IMAGE_MAX_FILES:
        raise HTTPException(400, f"Too many files in one batch (max {BULK_IMAGE_MAX_FILES})")

    results = []
    updated = 0
    for file, raw_id in zip(files, entity_ids):
        name = file.filename or "(unnamed)"
        raw_id = (raw_id or "").strip()
        if not raw_id:
            results.append({"filename": name, "status": "skipped"})
            continue
        if not raw_id.isdigit():
            results.append({"filename": name, "status": "error", "error": "Invalid entity id"})
            continue
        entity = db.query(Entity).filter(Entity.id == int(raw_id), Entity.world_id == world.id).first()
        if not entity:
            results.append({"filename": name, "status": "error", "error": "Entity not found in this world"})
            continue
        uploaded = save_upload(file, db=db)
        if not uploaded:
            results.append({"filename": name, "status": "error", "error": "Unsupported file type"})
            continue
        entity.image_url = uploaded
        updated += 1
        results.append({"filename": name, "status": "ok", "entity_id": entity.id, "entity_name": entity.name})
    db.commit()
    return {"updated": updated, "results": results}

@app.post("/api/worlds")
def api_create_world(payload: dict, db: Session = Depends(get_db)):
    slug = payload["name"].lower().replace(" ", "-").replace("&", "and")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    existing = db.query(World).filter(World.slug == slug).first()
    if existing:
        return {"id": existing.id, "slug": existing.slug, "created": False}
    w = World(name=payload["name"], slug=slug,
              description=payload.get("description"), accent=_sanitize_accent(payload.get("accent"), fallback="#b44fff"))
    db.add(w)
    db.commit()
    db.refresh(w)
    return {"id": w.id, "slug": w.slug, "created": True}

_mcp_asgi_app = mcp_server.mcp.streamable_http_app()
_mcp_start_lock = asyncio.Lock()
_mcp_started = False
_mcp_ready = asyncio.Event()


async def _run_mcp_session_manager_forever():
    """Owns FastMCP's background task group for the life of the event loop.
    Entering session_manager.run() has to happen inside a task that itself
    stays alive for as long as later /mcp requests need that task group to
    still be usable — anyio ties a task group's cancel scope to whichever
    task was running when it was entered, and that scope stops working once
    that task finishes, independent of whether __aexit__ was ever called.
    A short-lived caller (an individual request handler, or an
    @app.on_event("startup") hook, which Starlette awaits and discards) both
    return almost immediately, so entering there produces a task group that
    already looks "torn down" to the very next request. Blocking forever
    after entering is what keeps the owning task — and so the task group —
    alive; asyncio.create_task's caller (_mcp_entrypoint, below) doesn't
    await this, it only awaits _mcp_ready to confirm entry succeeded before
    letting a request through."""
    async with mcp_server.mcp.session_manager.run():
        _mcp_ready.set()
        await asyncio.Event().wait()


async def _mcp_entrypoint(scope, receive, send):
    """Lazily starts the above on the first real /mcp request rather than at
    app startup. Two things force lazy-on-first-use instead of an eager
    app.on_event("startup") hook: StreamableHTTPSessionManager.run() raises
    if called more than once on the same instance even after a clean exit,
    and mcp_server.mcp is a module-level singleton — a real server only
    boots once so an eager hook would be fine there, but the test suite
    creates and tears down this app's ASGI lifespan once per test (see
    tests/conftest.py's `client` fixture), which would hit that "already
    started" error on the second test to touch it. Starting it on first use
    instead means only tests that actually exercise /mcp ever trigger it."""
    global _mcp_started
    if not _mcp_started:
        async with _mcp_start_lock:
            if not _mcp_started:
                asyncio.create_task(_run_mcp_session_manager_forever())
                _mcp_started = True
    await _mcp_ready.wait()
    await _mcp_asgi_app(scope, receive, send)


async def _mcp_auth_wrapper(scope, receive, send):
    """Bearer-token auth for /mcp, standing in for auth_gate (see the "if
    path == /mcp" branch there for why this can't just live in auth_gate
    itself). Deliberately plain ASGI — no BaseHTTPMiddleware, no Request
    convenience wrapper beyond header parsing — so nothing here creates a
    task group or buffers the body, both of which would reintroduce the same
    cancel-scope conflict this function exists to avoid."""
    if scope["type"] != "http":
        return await _mcp_entrypoint(scope, receive, send)

    headers = Headers(scope=scope)
    auth_header = headers.get("authorization", "")
    raw_token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
    if not raw_token:
        return await JSONResponse({"detail": "Bearer token required"}, status_code=401)(scope, receive, send)

    db = SessionLocal()
    try:
        token_row = db.query(ApiToken).filter(
            ApiToken.token_hash == _auth.hash_api_token(raw_token)
        ).first()
        if not token_row:
            return await JSONResponse({"detail": "Invalid or revoked token"}, status_code=401)(scope, receive, send)
        user = db.query(User).filter(User.id == token_row.user_id).first()
        if not user:
            return await JSONResponse({"detail": "Invalid or revoked token"}, status_code=401)(scope, receive, send)
        # Detach before commit: expire_on_commit would otherwise mark every
        # already-loaded attribute on `user` (id, is_gm, ...) as stale, and
        # since this session closes right after, any later access anywhere
        # downstream (MCP tool handlers reading request.state.user) would
        # hit DetachedInstanceError trying to refresh from a closed session.
        db.expunge(user)
        from datetime import datetime as _dt
        token_row.last_used_at = _dt.utcnow()
        db.commit()
    finally:
        db.close()

    scope.setdefault("state", {})["user"] = user
    await _mcp_entrypoint(scope, receive, send)


_fastapi_app = app  # the real FastAPI instance, kept under this name so every
                    # @app.get/@app.post/app.include_router/app.mount call
                    # above (already executed by the time we get here) keeps
                    # working exactly as written


async def app(scope, receive, send):
    """The actual ASGI entrypoint this module exports as `app` (what
    `uvicorn app.main:app` and TestClient(app) both run) — a thin dispatcher
    in front of the real FastAPI app, routing /mcp around its entire
    @app.middleware("http") stack (auth_gate, SessionMiddleware,
    TrustedHostMiddleware) instead of through it.

    This has to happen at this outermost level, not via app.mount(): a
    Mount()-ed sub-app is still just another route *inside* that middleware
    stack — BaseHTTPMiddleware (what @app.middleware("http") installs)
    unconditionally wraps every request reaching the router in its own
    task-group/memory-stream bridge before any route or dispatch-function
    body ever runs, and that conflicts with the streamable-http transport's
    own task-group-based streaming underneath it. Only actually bypassing
    the middleware stack itself — not just choosing to no-op inside it —
    avoids the conflict (confirmed by reproducing the cancel-scope crash
    both ways). Every non-"/mcp" path, and non-http scope types (lifespan,
    websocket), still go straight through the unmodified FastAPI app below.
    """
    if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
        await _mcp_auth_wrapper(scope, receive, send)
    else:
        await _fastapi_app(scope, receive, send)
