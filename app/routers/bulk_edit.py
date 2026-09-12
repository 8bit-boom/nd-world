"""GM/Assistant-only "AI-assisted find & replace" across world content — the
answer to "change this race's name everywhere" without hand-editing every
entity/note/character that mentions it.

Deliberately NOT a free-form agentic editor: the model's only job (see
app.ai.parse_find_replace_instruction) is turning a plain-language
instruction into a literal {find, replace} pair. Locating matches and
performing the substitution is a plain, deterministic string operation this
module does itself — the model never sees or rewrites the actual content, so
even a bad/hallucinated parse can only produce a wrong (or empty) search
term, never corrupted prose. Nothing is written until the GM reviews an
exact before/after preview and explicitly selects which matches to apply,
mirroring the draft-then-confirm convention already used by
/api/ai/entity-from-text and the vision import routes.
"""
import re
from typing import List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import ai as _ai
from ..database import get_db
from ..deps import get_world_ctx, require_can_edit
from ..models import Entity, EntityNote, PlayerCharacter
from ..templating import templates

router = APIRouter(tags=["bulk-edit"])


@router.get("/tools/bulk-edit", response_class=HTMLResponse)
def bulk_edit_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    require_can_edit(request)
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("bulk_edit.html", {"request": request, "world": world, "worlds": worlds})


# Bounds both the SQL candidate fetch and the number of targets one /apply
# call may touch — a GM reviewing a preview of thousands of rows isn't
# reviewing anything, and an unbounded query on a large world is a real cost
# for a page that has no reason to run often.
MAX_CANDIDATES = 200

# Entity/EntityNote/PlayerCharacter text fields this tool searches and can
# rewrite — free-form prose fields only. Structured *_json columns (stats,
# equipment, custom_fields) are out of scope: a substring replace inside a
# JSON-encoded blob could corrupt its structure, and those are edited
# through their own dedicated UIs anyway.
_ENTITY_FIELDS = ("name", "summary", "body")
_PC_FIELDS = ("race", "char_class", "backstory", "notes")


def _apply_replace(value: str, find: str, replace: str, case_insensitive: bool):
    """Returns (new_value, occurrence_count). Never trusts the model with
    the actual substitution — this is a plain str.replace/re.sub, always
    computed here (in preview AND in apply) from the caller-supplied
    literal find/replace pair, so what a GM sees in the preview is
    byte-for-byte what /apply would write."""
    if not value or not find:
        return value, 0
    if case_insensitive:
        pattern = re.compile(re.escape(find), re.IGNORECASE)
        count = len(pattern.findall(value))
        return (pattern.sub(lambda _m: replace, value) if count else value), count
    count = value.count(find)
    return (value.replace(find, replace) if count else value), count


def _find_index(value: str, needle: str, case_insensitive: bool) -> int:
    if case_insensitive:
        return value.lower().find(needle.lower())
    return value.find(needle)


_EXCERPT_RADIUS = 100


def _excerpt(value: str, around: int) -> str:
    value = value or ""
    if len(value) <= _EXCERPT_RADIUS * 2:
        return value
    if around < 0:
        return value[: _EXCERPT_RADIUS * 2].rstrip() + "…"
    start = max(0, around - _EXCERPT_RADIUS)
    end = min(len(value), around + _EXCERPT_RADIUS)
    return ("…" if start > 0 else "") + value[start:end] + ("…" if end < len(value) else "")


def _candidate(target_type: str, target_id: int, target_name: str, field: str,
               before: str, after: str, count: int, find: str, replace: str, case_insensitive: bool) -> dict:
    before_idx = _find_index(before, find, case_insensitive)
    after_idx = _find_index(after, replace, case_insensitive) if replace else before_idx
    return {
        "target_type": target_type, "target_id": target_id, "target_name": target_name or "",
        "field": field, "occurrences": count,
        "before_excerpt": _excerpt(before, before_idx),
        "after_excerpt": _excerpt(after, after_idx if after_idx >= 0 else before_idx),
    }


class ParseBody(BaseModel):
    instruction: str


@router.post("/api/bulk-edit/parse")
async def bulk_edit_parse(
    body: ParseBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "No instruction provided")
    try:
        return await _ai.parse_find_replace_instruction(instruction)
    except ValueError as exc:
        raise HTTPException(502, str(exc))


class PreviewBody(BaseModel):
    find: str
    replace: str = ""
    case_insensitive: bool = False


@router.post("/api/bulk-edit/preview")
def bulk_edit_preview(
    body: PreviewBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    find = body.find.strip()
    if not find:
        raise HTTPException(400, "No search text provided")

    pattern = f"%{find}%"
    candidates = []

    entities = db.query(Entity).filter(
        Entity.world_id == world.id,
        or_(*(getattr(Entity, f).ilike(pattern) for f in _ENTITY_FIELDS)),
    ).limit(MAX_CANDIDATES).all()
    for e in entities:
        for field in _ENTITY_FIELDS:
            value = getattr(e, field) or ""
            new_value, count = _apply_replace(value, find, body.replace, body.case_insensitive)
            if count:
                candidates.append(_candidate("entity", e.id, e.name, field, value, new_value, count, find, body.replace, body.case_insensitive))

    notes = db.query(EntityNote, Entity.name).join(Entity, Entity.id == EntityNote.entity_id).filter(
        Entity.world_id == world.id, EntityNote.content.ilike(pattern),
    ).limit(MAX_CANDIDATES).all()
    for n, ename in notes:
        value = n.content or ""
        new_value, count = _apply_replace(value, find, body.replace, body.case_insensitive)
        if count:
            candidates.append(_candidate("entity_note", n.id, ename, "content", value, new_value, count, find, body.replace, body.case_insensitive))

    pcs = db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world.id,
        or_(*(getattr(PlayerCharacter, f).ilike(pattern) for f in _PC_FIELDS)),
    ).limit(MAX_CANDIDATES).all()
    for pc in pcs:
        for field in _PC_FIELDS:
            value = getattr(pc, field) or ""
            new_value, count = _apply_replace(value, find, body.replace, body.case_insensitive)
            if count:
                candidates.append(_candidate("player_character", pc.id, pc.name, field, value, new_value, count, find, body.replace, body.case_insensitive))

    candidates = candidates[:MAX_CANDIDATES]
    return {
        "candidates": candidates,
        "total_occurrences": sum(c["occurrences"] for c in candidates),
        "truncated": len(entities) >= MAX_CANDIDATES or len(notes) >= MAX_CANDIDATES or len(pcs) >= MAX_CANDIDATES,
    }


class ApplyTarget(BaseModel):
    target_type: str
    target_id: int
    field: str


class ApplyBody(BaseModel):
    find: str
    replace: str = ""
    case_insensitive: bool = False
    targets: List[ApplyTarget]


def _row_world_id(row) -> Optional[int]:
    wid = getattr(row, "world_id", None)
    if wid is not None:
        return wid
    entity = getattr(row, "entity", None)
    return entity.world_id if entity else None


_TARGET_MODEL_FIELDS = {
    "entity": (Entity, _ENTITY_FIELDS),
    "entity_note": (EntityNote, ("content",)),
    "player_character": (PlayerCharacter, _PC_FIELDS),
}


@router.post("/api/bulk-edit/apply")
def bulk_edit_apply(
    body: ApplyBody, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    require_can_edit(request)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    find = body.find.strip()
    if not find:
        raise HTTPException(400, "No search text provided")
    if not body.targets:
        raise HTTPException(400, "No targets selected")
    if len(body.targets) > MAX_CANDIDATES:
        raise HTTPException(400, f"Too many targets — limit is {MAX_CANDIDATES}")

    updated = 0
    for t in body.targets:
        spec = _TARGET_MODEL_FIELDS.get(t.target_type)
        if not spec or t.field not in spec[1]:
            continue  # unrecognized type/field — skip rather than fail the whole batch
        model = spec[0]
        row = db.query(model).filter(model.id == t.target_id).first()
        if not row or _row_world_id(row) != world.id:
            continue  # gone, or belongs to another world — never cross a world boundary
        current = getattr(row, t.field) or ""
        # Re-derived here rather than trusting any client-supplied "after"
        # text — the preview is just a display of what this same function
        # would do; the actual write always recomputes it fresh.
        new_value, count = _apply_replace(current, find, body.replace, body.case_insensitive)
        if count:
            setattr(row, t.field, new_value)
            updated += 1
    db.commit()
    return {"updated": updated}
