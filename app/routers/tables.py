import json
import random
import re

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import ai as _ai
from ..database import get_db
from ..deps import can_edit_content, get_world_ctx, world_can_edit_row, world_can_edit_section, world_can_view_section, world_row_visible
from ..models import RandomTable, World
from ..templating import templates

router = APIRouter()


def _current_user_id(request: Request):
    user = getattr(request.state, "user", None)
    return user.id if user else None


def _can_edit_table_row(request: Request, db: Session, tbl: RandomTable) -> bool:
    """world_can_edit_row for a table that belongs to a specific world;
    falls back to the old blanket can_edit_content (GM/assistant, no
    per-row ownership) for a global/builtin table (world_id is None) —
    there's no per-world matrix to consult for content shared across
    every world, and this preserves exactly what assistants could already
    do to those rows before this permission matrix existed."""
    if tbl.world_id is None:
        return can_edit_content(request)
    world = db.get(World, tbl.world_id)
    return world_can_edit_row(request, world, "tables", tbl.created_by_user_id)


def _slugify(name: str, db: Session) -> str:
    base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "table"
    slug = base_slug
    n = 2
    while db.query(RandomTable).filter(RandomTable.slug == slug).first():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


@router.get("/tables", response_class=HTMLResponse)
def tables_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "tables"):
        raise HTTPException(403)
    tables = db.query(RandomTable).filter(
        (RandomTable.world_id.is_(None)) | (RandomTable.world_id == world.id)
    ).order_by(RandomTable.category, RandomTable.name).all()
    grouped: dict = {}
    for t in tables:
        grouped.setdefault(t.category or "general", []).append(t)
    return templates.TemplateResponse("tables/list.html", {
        "request": request, "world": world, "worlds": worlds, "grouped": grouped,
        "can_create": world_can_edit_section(request, world, "tables"),
        "can_edit_table": lambda t: _can_edit_table_row(request, db, t),
    })


@router.get("/tables/new", response_class=HTMLResponse)
def table_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world_can_edit_section(request, world, "tables"):
        raise HTTPException(403)
    return templates.TemplateResponse("tables/form.html", {
        "request": request, "world": world, "worlds": worlds, "tbl": None, "entries": [],
    })


@router.post("/tables/new")
async def table_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world_can_edit_section(request, world, "tables"):
        raise HTTPException(403)
    form = await request.form()
    name = str(form.get("name", "")).strip() or "Unnamed Table"
    category = str(form.get("category", "")).strip() or "general"
    description = str(form.get("description", "")).strip()
    raw_entries = str(form.get("entries_json", "[]") or "[]")
    try:
        json.loads(raw_entries)
    except Exception:
        raw_entries = "[]"
    user = getattr(request.state, "user", None)
    is_gm_or_assistant = bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))
    tbl = RandomTable(
        world_id=world.id if world else None, name=name, slug=_slugify(name, db),
        category=category, description=description, is_builtin=False, entries_json=raw_entries,
        created_by_user_id=None if is_gm_or_assistant else _current_user_id(request),
    )
    db.add(tbl)
    db.commit()
    db.refresh(tbl)
    return RedirectResponse(f"/tables/{tbl.id}/edit", status_code=303)


@router.get("/tables/{table_id}/edit", response_class=HTMLResponse)
def table_edit_form(table_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl or not _can_edit_table_row(request, db, tbl):
        raise HTTPException(404)
    entries = json.loads(tbl.entries_json or "[]")
    return templates.TemplateResponse("tables/form.html", {
        "request": request, "world": world, "worlds": worlds, "tbl": tbl, "entries": entries,
    })


@router.post("/tables/{table_id}/edit")
async def table_update(table_id: int, request: Request, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    if not _can_edit_table_row(request, db, tbl):
        raise HTTPException(403)
    form = await request.form()
    if not tbl.is_builtin:
        tbl.name = str(form.get("name", tbl.name)).strip() or tbl.name
        tbl.category = str(form.get("category", "")).strip() or "general"
        tbl.description = str(form.get("description", "")).strip()
    raw_entries = str(form.get("entries_json", "[]") or "[]")
    try:
        json.loads(raw_entries)
    except Exception:
        raw_entries = "[]"
    tbl.entries_json = raw_entries
    db.commit()
    return RedirectResponse(f"/tables/{table_id}/edit?saved=1", status_code=303)


@router.post("/tables/{table_id}/delete")
def table_delete(table_id: int, request: Request, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl:
        raise HTTPException(404)
    if tbl.is_builtin:
        raise HTTPException(403, "Cannot delete built-in tables")
    if not _can_edit_table_row(request, db, tbl):
        raise HTTPException(403)
    db.delete(tbl)
    db.commit()
    return RedirectResponse("/tables", status_code=303)


@router.get("/api/tables/options")
def tables_options(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The world's roll tables as JSON for the GM Cockpit's quick-roll
    panel (name + entry count, no contents). GM-only via middleware (not
    in _is_player_safe), matching the cockpit's other feeds."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    rows = (
        db.query(RandomTable)
        .filter(or_(RandomTable.world_id == world.id, RandomTable.world_id.is_(None)))
        .order_by(RandomTable.name)
        .limit(60)
        .all()
    )
    out = []
    for t in rows:
        try:
            n = len(json.loads(t.entries_json or "[]"))
        except ValueError:
            n = 0
        out.append({"id": t.id, "name": t.name, "entries": n})
    return {"tables": out}


@router.post("/api/tables/{table_id}/roll")
def table_roll(table_id: int, request: Request, db: Session = Depends(get_db)):
    tbl = db.query(RandomTable).filter(RandomTable.id == table_id).first()
    if not tbl or not world_row_visible(request, db, tbl.world_id, "tables"):
        raise HTTPException(404)
    entries = json.loads(tbl.entries_json or "[]")
    if not entries:
        raise HTTPException(400, "This table has no entries")
    weights = [max(0, int(e.get("weight", 1) or 1)) for e in entries]
    total = sum(weights) or len(entries)
    if sum(weights) == 0:
        weights = [1] * len(entries)
    roll = random.randint(1, total)
    choice = random.choices(entries, weights=weights, k=1)[0]
    return {"result": choice.get("label", ""), "roll": roll, "total": total}


@router.get("/tables/export")
def tables_export(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    tables = db.query(RandomTable).filter(
        (RandomTable.world_id.is_(None)) | (RandomTable.world_id == (world.id if world else None))
    ).all()
    payload = [{
        "name": t.name, "category": t.category, "description": t.description,
        "entries": json.loads(t.entries_json or "[]"),
    } for t in tables]
    return JSONResponse(payload)


@router.post("/tables/import")
async def tables_import(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world_can_edit_section(request, world, "tables"):
        raise HTTPException(403)
    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(400, "No file uploaded")
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(payload, list):
        raise HTTPException(400, "Expected a JSON array of tables")
    user = getattr(request.state, "user", None)
    is_gm_or_assistant = bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))
    owner_id = None if is_gm_or_assistant else _current_user_id(request)
    for item in payload:
        name = str(item.get("name", "")).strip() or "Imported Table"
        db.add(RandomTable(
            world_id=world.id if world else None, name=name, slug=_slugify(name, db),
            category=str(item.get("category", "general")), description=str(item.get("description", "")),
            is_builtin=False, entries_json=json.dumps(item.get("entries", [])),
            created_by_user_id=owner_id,
        ))
    db.commit()
    return RedirectResponse("/tables", status_code=303)


# ── AI table generator ───────────────────────────────────────────────────────
# "Describe a table" → dN entries as a real RandomTable the GM lands on the
# edit page to review/tweak. Random Tables had zero AI (audit 2026-09-30);
# this is deliberately human-in-the-loop: the route creates the table and
# redirects to its editor rather than being a faceless autocommit.

@router.post("/tables/generate-ai")
async def table_generate_ai(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
    description: str = Form(""),
    count: int = Form(20),
    category: str = Form("general"),
):
    """Generate a random table from a description via the chat model and
    create it, redirecting to its edit page for review. GM/assistant
    content editors (same tier as table_create)."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_edit_section(request, world, "tables"):
        raise HTTPException(403)
    description = (description or "").strip()
    if not description:
        raise HTTPException(400, "Describe the table first")
    if len(description) > 2000:
        raise HTTPException(400, "Description too long (over 2k chars)")
    count = max(4, min(int(count or 20), 30))
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System)")

    system = (
        "You generate random tables for a tabletop RPG GM. Reply with ONLY a JSON object, "
        "no markdown fences:\n"
        '{"name": str (short table name), "description": str (one sentence), '
        '"entries": [str, ...]}\n'
        f"Rules: exactly {count} entries; each entry one line, evocative but usable at the "
        "table (a name, a sight, a complication — per the description); vary them across the "
        "full range the description implies; never name real people; no numbering in the text."
    )
    try:
        raw = await _ai.generate_chat(
            [{"role": "user", "content": description}],
            system=system, options={"num_predict": 1800}, think=False,
        )
    except Exception as exc:
        raise HTTPException(502, f"AI call failed: {exc}") from exc
    if _ai.is_failure_sentinel(raw or ""):
        raise HTTPException(502, raw)

    from .boards_generate import _extract_json_object
    data = _extract_json_object(raw)
    entries = []
    if data and isinstance(data.get("entries"), list):
        entries = [str(e).strip()[:300] for e in data["entries"] if str(e).strip()][:40]
    if len(entries) < 4:
        raise HTTPException(502, "The AI reply wasn't a usable table — try rephrasing the description.")

    name = (str(data.get("name") or "").strip() or description[:60])[:200]
    tbl = RandomTable(
        world_id=world.id, name=name, slug=_slugify(name, db),
        category=(category or "general").strip()[:64] or "general",
        description=(str(data.get("description") or "").strip() or description)[:500],
        is_builtin=False,
        entries_json=json.dumps([{"label": e, "weight": 1} for e in entries]),
    )
    db.add(tbl)
    db.commit()
    db.refresh(tbl)
    return RedirectResponse(f"/tables/{tbl.id}/edit", status_code=303)
