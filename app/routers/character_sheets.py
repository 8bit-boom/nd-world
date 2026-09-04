"""Player-fillable character sheets — a personal copy of a PageDoc the GM
has marked is_character_sheet_template (see app/routers/pages.py), one per
(player, template) at most... actually not even that: a player may create
several, e.g. one per PlayerCharacter they own. See CharacterSheet in
app/models.py for the data shape and its own docstring for the security
reasoning behind the postMessage bridge (static/js/character-sheet-bridge.js)
this whole feature is built on.

Access is GM + the owning player only, everywhere — not GM-Assistant, not
other players (even with World.players_see_party on). This deliberately
mirrors app/routers/characters.py's own PlayerCharacter permission model
(_can_manage_character: `user.is_gm or pc.owner_user_id == user.id`, no
assistant tier at all) rather than Pages' own looser GM-or-Assistant
`can_edit_content` gate — a filled sheet is personal player data, not Pages
content an assistant manages. Every route below 404s (never 403s) for a
caller who isn't the owner and isn't GM, matching pages_viewer's own
hidden-content convention (no acknowledging the sheet exists to someone
with no business seeing it)."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_world_ctx
from ..models import CharacterSheet, PageDoc, PlayerCharacter, User
from ..templating import templates
from .pages import _UPLOADS_DIR, _doc_or_404

router = APIRouter()

_log = logging.getLogger(__name__)

_MAX_NAME = 256
# Generous for any plausible form-field dump (a sheet's worth of text
# fields), while still bounding what a broken/malicious sheet script could
# push per save — same "best-effort cap, not a precise budget" spirit as
# pages.py's own _MAX_PAGE_BYTES.
_MAX_SHEET_DATA_BYTES = 256 * 1024


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


def _sheet_or_404(db: Session, world_id: int, sheet_id: int, request: Request) -> CharacterSheet:
    """World-scoped AND access-scoped in one place — every route below
    calls this first, so "wrong world" and "not mine" both collapse to the
    same 404 a caller can't distinguish from "doesn't exist"."""
    sheet = db.get(CharacterSheet, sheet_id)
    if not sheet or sheet.world_id != world_id:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not user or (not user.is_gm and sheet.owner_user_id != user.id):
        raise HTTPException(404)
    return sheet


def _owned_pc_or_400(db: Session, world_id: int, owner_user_id: int, pc_id) -> Optional[PlayerCharacter]:
    """Resolves an optional player_character_id form value to a PC row
    owned by `owner_user_id` (the SHEET's owner, not necessarily the
    caller — a GM editing on a player's behalf must still only be able to
    link that player's own characters, never someone else's). Returns None
    for a blank/absent value (unlink); raises 400 for anything else that
    doesn't resolve to one of that owner's characters."""
    pc_id = (pc_id or "").strip()
    if not pc_id:
        return None
    if not pc_id.isdigit():
        raise HTTPException(400, "Invalid character")
    pc = db.query(PlayerCharacter).filter(
        PlayerCharacter.id == int(pc_id), PlayerCharacter.world_id == world_id,
        PlayerCharacter.owner_user_id == owner_user_id,
    ).first()
    if not pc:
        raise HTTPException(400, "That character isn't owned by this sheet's player")
    return pc


def _read_template_html(doc: PageDoc) -> str:
    """The template's raw file, off disk — same path-traversal-checked
    resolution as pages.py's _delete_doc_file/pages_download. Never the
    modified/injected copy; that's built fresh per response by
    _render_sheet_html so the file on disk (and every OTHER sheet built
    from it) stays untouched."""
    root = _UPLOADS_DIR.resolve()
    if not doc.file_url or not doc.file_url.startswith("/uploads/"):
        raise HTTPException(404)
    try:
        path = (root / doc.file_url[len("/uploads/"):]).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(404)
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404)
    return path.read_text(encoding="utf-8", errors="replace")


def _render_sheet_html(sheet: CharacterSheet, doc: PageDoc) -> str:
    """The template's HTML with the bridge script + this sheet's current
    saved data injected right before </body> (or appended at the end, for
    a template that somehow has none). See static/js/character-sheet-
    bridge.js's own docstring for what runs on the other end of this.

    `</` inside the embedded JSON is escaped to `<\\/` — a player's own
    saved text (a character name, notes) can legitimately contain the
    literal substring "</script>", which would otherwise close the
    injected <script> tag early and corrupt the rest of the document."""
    try:
        data = json.loads(sheet.data_json or "{}")
    except ValueError:
        data = {}
    safe_data_json = json.dumps(data).replace("</", "<\\/")
    injected = (
        f'<script>window.__ND_SHEET_ID__={json.dumps(sheet.id)};'
        f'window.__ND_SHEET_DATA__={safe_data_json};</script>'
        f'<script src="/static/js/character-sheet-bridge.js"></script>'
    )
    html = _read_template_html(doc)
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        return html + injected
    return html[:idx] + injected + html[idx:]


def _sandboxed_headers() -> dict:
    """Identical to what main.py's serve_upload already applies to a raw
    .html upload — this response is html the SAME template's own file
    contributed, just with our injected script added, so it carries the
    exact same isolation guarantees (no allow-same-origin anywhere in the
    chain)."""
    return {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox allow-scripts allow-popups",
        "X-Frame-Options": "SAMEORIGIN",
    }


@router.get("/pages/sheets", response_class=HTMLResponse)
def character_sheets_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    is_gm = _is_gm(request)
    q = db.query(CharacterSheet).filter(CharacterSheet.world_id == world.id)
    if not is_gm:
        q = q.filter(CharacterSheet.owner_user_id == user.id if user else False)
    sheets = q.order_by(CharacterSheet.updated_at.desc()).all()

    owners = []
    if is_gm:
        # Grouped by owner for GM oversight — a flat list of every
        # player's sheets read together isn't useful without knowing
        # whose is whose.
        by_owner = {}
        for s in sheets:
            by_owner.setdefault(s.owner_user_id, []).append(s)
        users = {u.id: u for u in db.query(User).filter(User.id.in_(by_owner.keys())).all()} if by_owner else {}
        owners = sorted(
            ({"user": users.get(uid), "sheets": ss} for uid, ss in by_owner.items()),
            key=lambda o: (o["user"].display_name or o["user"].email) if o["user"] else "",
        )

    return templates.TemplateResponse("character_sheets_list.html", {
        "request": request, "world": world, "worlds": worlds,
        "sheets": sheets, "owners": owners, "is_gm": is_gm,
    })


@router.post("/pages/sheets/new")
async def character_sheets_new(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403)
    form = await request.form()
    template_id = str(form.get("template_id", "")).strip()
    if not template_id.isdigit():
        raise HTTPException(400, "Missing template")
    doc = _doc_or_404(db, world.id, int(template_id))
    if not doc.visible_to_players and not _is_gm(request):
        raise HTTPException(404)
    if not doc.is_character_sheet_template:
        raise HTTPException(400, "This page isn't a character sheet template")
    pc = _owned_pc_or_400(db, world.id, user.id, form.get("player_character_id"))
    sheet = CharacterSheet(
        world_id=world.id, template_id=doc.id, owner_user_id=user.id,
        player_character_id=pc.id if pc else None,
        name=(pc.name if pc else doc.name)[:_MAX_NAME],
    )
    db.add(sheet)
    db.commit()
    db.refresh(sheet)
    return RedirectResponse(f"/pages/sheets/{sheet.id}", status_code=303)


@router.get("/pages/sheets/{sheet_id}", response_class=HTMLResponse)
def character_sheet_edit(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    doc = db.get(PageDoc, sheet.template_id)
    # The sheet's own OWNER's characters — see _owned_pc_or_400's own
    # comment for why this is keyed on the sheet's owner, not the caller
    # (a GM editing on a player's behalf still only links that player's
    # characters).
    own_pcs = db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world.id, PlayerCharacter.owner_user_id == sheet.owner_user_id,
    ).order_by(PlayerCharacter.name).all()
    linked_pc = db.get(PlayerCharacter, sheet.player_character_id) if sheet.player_character_id else None
    return templates.TemplateResponse("character_sheet_edit.html", {
        "request": request, "world": world, "worlds": worlds,
        "sheet": sheet, "template": doc, "own_pcs": own_pcs, "linked_pc": linked_pc,
        "is_gm": _is_gm(request),
    })


@router.get("/pages/sheets/{sheet_id}/render")
def character_sheet_render(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    doc = db.get(PageDoc, sheet.template_id)
    if not doc:
        raise HTTPException(404)
    html = _render_sheet_html(sheet, doc)
    return Response(content=html, media_type="text/html; charset=utf-8", headers=_sandboxed_headers())


@router.post("/pages/sheets/{sheet_id}/save")
async def character_sheet_save(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    body = await request.json()
    data = body.get("data")
    if not isinstance(data, dict):
        raise HTTPException(400, '"data" must be an object')
    encoded = json.dumps(data)
    if len(encoded.encode("utf-8")) > _MAX_SHEET_DATA_BYTES:
        raise HTTPException(413, "Sheet data too large")
    sheet.data_json = encoded
    db.commit()
    return {"ok": True}


@router.post("/pages/sheets/{sheet_id}/edit")
async def character_sheet_update(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:_MAX_NAME]
    if name:
        sheet.name = name
    pc = _owned_pc_or_400(db, world.id, sheet.owner_user_id, form.get("player_character_id"))
    sheet.player_character_id = pc.id if pc else None
    db.commit()
    return RedirectResponse(f"/pages/sheets/{sheet.id}", status_code=303)


@router.post("/pages/sheets/{sheet_id}/delete")
def character_sheet_delete(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    db.delete(sheet)
    db.commit()
    return RedirectResponse("/pages/sheets", status_code=303)


@router.get("/pages/sheets/{sheet_id}/download")
def character_sheet_download(sheet_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    sheet = _sheet_or_404(db, world.id, sheet_id, request)
    doc = db.get(PageDoc, sheet.template_id)
    if not doc:
        raise HTTPException(404)
    html = _render_sheet_html(sheet, doc)
    fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (sheet.name or "character-sheet")) or "character-sheet"
    headers = _sandboxed_headers()
    headers["Content-Disposition"] = f'attachment; filename="{fname}.html"'
    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)
