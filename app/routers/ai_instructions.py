"""CRUD for AiInstruction (see app/models.py and app/ai_instructions.py)
— GM-authored markdown files of standing behavior instructions for every
AI-answering surface. GM-only (like every route not listed in
app.main._is_player_safe/_is_assistant_safe) — a GM manages these from
the world settings page (world_edit.html), which is also where every
route here redirects back to (plain form POSTs, matching entities/
detail.html's EntityNote add/toggle/delete forms — no JS fetch needed
for a same-page list-with-buttons UI)."""
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from typing import Optional

from ..database import get_db
from ..deps import get_world_ctx
from ..models import AiInstruction
from ..uploads import read_upload_bounded

router = APIRouter()

# Deliberately markdown/plain-text only, matching what was actually asked
# for ("md files") — unlike EntityNote's importer (which also accepts
# .pdf/.html/images for a note that's meant to be READ), a document whose
# whole job is to be pasted into a system prompt gains nothing from PDF/
# HTML conversion machinery, and staying narrow here avoids surprising
# markup surviving into the prompt unnoticed.
_ALLOWED_EXTS = {".md", ".markdown", ".txt"}
MAX_AI_INSTRUCTION_BYTES = 200 * 1024  # a system-prompt document, not a document library


@router.post("/worlds/{world_id}/ai-instructions/import")
def import_ai_instruction(
    world_id: int, request: Request, file: UploadFile = File(...), title: str = Form(""),
    db=Depends(get_db), active_world: Optional[str] = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world or world.id != world_id:
        raise HTTPException(404)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type {ext!r} — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")
    raw = read_upload_bounded(file, max_bytes=MAX_AI_INSTRUCTION_BYTES)
    content = raw.decode("utf-8", errors="replace").strip()
    if content:
        doc_title = title.strip() or Path(file.filename or "").stem or "Untitled"
        db.add(AiInstruction(world_id=world.id, title=doc_title, content=content, enabled=True))
        db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)


@router.post("/worlds/{world_id}/ai-instructions/{instruction_id}/toggle")
def toggle_ai_instruction(world_id: int, instruction_id: int, db=Depends(get_db)):
    instr = db.get(AiInstruction, instruction_id)
    if instr and instr.world_id == world_id:
        instr.enabled = not instr.enabled
        db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)


@router.post("/worlds/{world_id}/ai-instructions/{instruction_id}/delete")
def delete_ai_instruction(world_id: int, instruction_id: int, db=Depends(get_db)):
    instr = db.get(AiInstruction, instruction_id)
    if instr and instr.world_id == world_id:
        db.delete(instr)
        db.commit()
    return RedirectResponse(f"/worlds/{world_id}/edit", status_code=303)
