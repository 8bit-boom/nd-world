"""GM-only: have the local model draft a whole character-sheet template from a rulebook.

The GM pastes or uploads rules (a PDF/MD/TXT/JSON file, or the world's own rules text). A long book is
read in pieces — one short "what does a sheet need?" note per piece — and the notes are then turned into
ONE template draft: fields, the system hooks (HP track, XP, name/player binds, conditions, Rest, pages,
roster comparison) and a rules digest. The model's JSON goes through `template_draft.clean_template_draft`
so what reaches the review screen is guaranteed to load and work; nothing is saved until the GM presses
Create, which posts to the existing POST /characters/templates/new (the same route the hand-made editor
uses, so the result is exactly as integrated as a template built by hand).

Same background start + poll shape as the character creator: reading a book with thinking on outlives
Cloudflare's ~100 s no-byte timeout."""
import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import ai as _ai
from .. import retrieval as _retrieval
from ..database import SessionLocal, get_db
from ..deps import get_world_ctx
from ..template_draft import (
    DRAFT_SYSTEM, NOTES_SYSTEM, clean_template_draft, draft_prompt, notes_prompt, pick_chunks, split_rules_text,
)
from ..templating import templates
from .character_ai import _extract_json

router = APIRouter()

_TPL_AI_JOBS: dict = {}
_TPL_AI_SEQ: list = [0]
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_SOURCE_CHARS = 900_000     # past this a book is cut (and the GM told)
_MAX_PDF_PAGES = 400
_CHUNK_CHARS = 6000
_MAX_PARTS = 14                 # parts the model actually reads; the most sheet-relevant ones
_DIRECT_CHARS = 14000           # short enough to hand to the drafting call whole
_NOTES_CAP = 26000
_TEXT_EXTS = {".md", ".markdown", ".txt"}

_DRAFT_FORMAT = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "description": {"type": "string"},
        "fields": {"type": "array"}, "system": {"type": "object"}, "rules_md": {"type": "string"},
    },
    "required": ["name", "fields"],
}


def _read_pdf(data: bytes) -> str:
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:_MAX_PDF_PAGES]:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages).strip()


async def read_rulebook(data: bytes, ext: str) -> str:
    """File bytes -> rulebook text. Raises ValueError with something the GM can act on."""
    ext = ext.lower()
    if ext in _TEXT_EXTS:
        return data.decode("utf-8", errors="replace")
    if ext == ".json":
        text = data.decode("utf-8", errors="replace")
        try:
            return json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            return text
    if ext == ".pdf":
        try:
            text = await asyncio.to_thread(_read_pdf, data)
        except Exception as exc:
            raise ValueError(f"Reading this PDF failed ({type(exc).__name__}) — export it as text or paste the rules.")
        if not text:
            raise ValueError("This PDF has no extractable text (it may be a scan) — paste the rules instead.")
        return text
    if ext in (".doc", ".docx", ".odt"):
        raise ValueError("Word documents aren't readable here — export the rules to PDF or plain text, or paste them.")
    raise ValueError(f"Unsupported file type {ext!r} — use PDF, MD, TXT or JSON, or paste the text.")


def _is_gm(request: Request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_gm)


async def _draft_task(job_id: int, source: str, system_name: str, wishes: str, think: bool):
    job = _TPL_AI_JOBS[job_id]
    try:
        chunks = split_rules_text(source, _CHUNK_CHARS)
        total = len(chunks)
        picked = pick_chunks(chunks, _MAX_PARTS)
        job.update(parts_total=total, parts_read=len(picked))
        if len(source) <= _DIRECT_CHARS:
            notes = [source.strip()]
        else:
            notes = []
            for i, chunk in enumerate(picked, 1):
                job["progress"] = f"Reading the rules, part {i} of {len(picked)}…"
                raw = await _ai.generate_chat(
                    [{"role": "user", "content": notes_prompt(chunk, i, len(picked))}],
                    system=NOTES_SYSTEM, model="", think=False)
                raw = (raw or "").strip()
                if raw and raw.upper().rstrip(". ") != "NOTHING":
                    notes.append(raw)
            if not notes:
                raise ValueError("Nothing in this text describes what a character sheet tracks — "
                                 "paste the character-creation and resource/rest chapters.")
        joined = "\n\n".join(notes)
        if len(joined) > _NOTES_CAP:
            joined = joined[:_NOTES_CAP]
        job["progress"] = "Designing the sheet…"
        raw = await _ai.generate_chat(
            [{"role": "user", "content": draft_prompt([joined], system_name, wishes)}],
            system=DRAFT_SYSTEM, model="", think=think, format=_DRAFT_FORMAT)
        try:
            data = _extract_json(raw)
        except ValueError:
            raise ValueError("The model's reply wasn't a template — try again, or paste a shorter excerpt.")
        if system_name and isinstance(data, dict):
            data["name"] = system_name
        draft, warnings = clean_template_draft(data)
        if draft is None:
            raise ValueError("The model couldn't produce a usable sheet" + (": " + "; ".join(warnings[:3]) if warnings else "."))
        if not draft["rules_md"]:
            draft["rules_md"] = f"# {draft['name']} — rules notes\n\n{joined}"[:20000]
            warnings.append("the model wrote no rules digest; kept its reading notes instead")
        if job.get("truncated"):
            warnings.append(f"the text was longer than {_MAX_SOURCE_CHARS // 1000}k characters; only the start was read")
        job.update(status="done", draft=draft, warnings=warnings)
    except Exception as exc:
        job.update(status="error", error=str(exc) or exc.__class__.__name__)


@router.post("/api/sheet-templates/ai/start")
async def template_ai_start(request: Request,
                            rules_text: str = Form(""),
                            system_name: str = Form(""),
                            wishes: str = Form(""),
                            think: bool = Form(True),
                            use_world_rules: bool = Form(False),
                            file: UploadFile = File(None),
                            db: Session = Depends(get_db),
                            active_world: str = Cookie(None)):
    """Start drafting a sheet template from pasted rules, an uploaded rulebook and/or the world's own
    rules text. GM only."""
    if not _is_gm(request):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    parts = []
    if (rules_text or "").strip():
        parts.append(rules_text.strip())
    if file and file.filename:
        data = await file.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(400, "File too large (over 25 MB)")
        if data:
            try:
                parts.append(await read_rulebook(data, Path(file.filename).suffix or ""))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
    if use_world_rules and world:
        try:
            own = (_retrieval.world_rules_markdown(world) or "").strip()
        except Exception:
            own = ""
        if own:
            parts.append(own)
    source = "\n\n".join(p for p in parts if p).strip()
    if not source:
        raise HTTPException(400, "Paste the rules, upload a rulebook (PDF / MD / TXT / JSON), or tick 'use this world's rules'.")
    truncated = len(source) > _MAX_SOURCE_CHARS
    source = source[:_MAX_SOURCE_CHARS]
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No AI backend configured — set UNSLOTH_API_KEY (Settings → System).")

    job_id = _TPL_AI_SEQ[0] + 1
    _TPL_AI_SEQ[0] = job_id
    _TPL_AI_JOBS[job_id] = {"status": "running", "started": time.time(), "user_id": request.state.user.id,
                            "draft": None, "warnings": [], "error": "", "progress": "Starting…",
                            "truncated": truncated, "parts_total": 0, "parts_read": 0}
    done = [j for j, v in _TPL_AI_JOBS.items() if v["status"] != "running"]
    while len(done) > 8:
        _TPL_AI_JOBS.pop(done.pop(0), None)
    asyncio.get_running_loop().create_task(
        _draft_task(job_id, source, " ".join((system_name or "").split())[:80], (wishes or "").strip(), think))
    return {"job_id": job_id, "status": "running", "characters": len(source)}


@router.get("/api/sheet-templates/ai/{job_id}")
async def template_ai_poll(job_id: int, request: Request):
    """Poll a template-draft job. GM only."""
    if not _is_gm(request):
        raise HTTPException(403)
    job = _TPL_AI_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown template job")
    if job["status"] == "running":
        return {"status": "running", "elapsed": round(time.time() - job["started"]), "progress": job["progress"]}
    if job["status"] == "error":
        return {"status": "error", "error": job["error"]}
    return {"status": "done", "draft": job["draft"], "warnings": job["warnings"],
            "parts_read": job["parts_read"], "parts_total": job["parts_total"]}


@router.get("/characters/templates/ai-new", response_class=HTMLResponse)
def template_ai_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    if not _is_gm(request):
        raise HTTPException(403)
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    has_world_rules = bool((getattr(world, "rules_md", None) or "").strip())
    return templates.TemplateResponse("characters/template_ai.html", {
        "request": request, "world": world, "worlds": worlds, "has_world_rules": has_world_rules,
    })
