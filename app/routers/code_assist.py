"""Code Assist — a GM-only, PREVIEW-ONLY panel where a coding-capable
Ollama model drafts a change to one of nd-world's OWN source files.

Deliberately preview-only: the drafted result is never written to disk,
and nothing here restarts or rebuilds anything — a GM reviews the diff and
applies it themselves (by hand, or by handing it to whatever actually
manages this deployment, e.g. a git-based coding session). A fully
autonomous version — the app patching and redeploying itself with no
human in the loop — is a real production-safety question (a bad
AI-authored patch could corrupt the app, leak secrets, or open a security
hole with nothing to catch it) and is intentionally out of scope until
there's a rollback/backup story in place; see app/ai_assist.py's
OP_CODE_EDIT for the op this calls.

Reading is sandboxed to exactly the two directories the Dockerfile copies
into the running container (`COPY app/ ./app/`, `COPY static/ ./static/`,
both under WORKDIR /app) — resolved from this module's own __file__ so the
same code is correct both in this repo checkout and inside the deployed
image, without hardcoding /app. Every candidate path is realpath-resolved
BEFORE the containment check, which also closes the symlink-escape case (a
symlink under an allowed root pointing outside it resolves to its real,
outside target first). Extensions are allowlisted to plain text/source
types nd-world is actually built from.

Not in main._is_player_safe or _is_assistant_safe, so the auth_gate
middleware already denies this to anyone but a GM — deliberately narrower
than the content-editing AI-assist ops (which a GM-Assistant may run):
this touches the application's own source, not campaign content.
"""
import difflib
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import ai_assist as _ai_assist
from ..audio_jobs import create_assist_job
from ..database import get_db
from ..deps import get_world_ctx
from ..models import AudioJob
from ..templating import templates

router = APIRouter()

_log = logging.getLogger("nd.code_assist")

_INSTALL_ROOT = Path(__file__).resolve().parent.parent.parent
_ALLOWED_ROOTS = [(_INSTALL_ROOT / "app").resolve(), (_INSTALL_ROOT / "static").resolve()]
_ALLOWED_EXTS = {".py", ".html", ".js", ".css", ".md", ".txt"}
# Generous for any real source file in this codebase (the largest, main.py,
# is well under this); a bound so a mistaken/huge path can't be read into
# memory unbounded.
_MAX_FILE_BYTES = 300_000


def _resolve_safe_path(rel: str) -> Path:
    """A file under one of _ALLOWED_ROOTS, or 400/404. `rel` is relative to
    _INSTALL_ROOT (e.g. "app/routers/races.py" — the same shape
    _list_source_files() emits), NOT to an individual allowed root, so it
    is resolved against _INSTALL_ROOT and then checked for containment —
    resolving it against an allowed root directly would double up that
    root's own name (".../app/app/routers/races.py"). Realpath-resolves
    BEFORE the containment check (closes the symlink-escape case: a
    symlink under an allowed root pointing outside it resolves to its
    real, outside target first), and checks the extension allowlist
    before ever touching the filesystem."""
    rel = (rel or "").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise HTTPException(400, "Invalid file path")
    if Path(rel).suffix.lower() not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file type — allowed: {', '.join(sorted(_ALLOWED_EXTS))}")
    candidate = (_INSTALL_ROOT / rel).resolve()
    if not any(candidate.is_relative_to(root) for root in _ALLOWED_ROOTS):
        raise HTTPException(404, "File not found")
    if not candidate.is_file():
        raise HTTPException(404, "File not found")
    return candidate


def _list_source_files() -> list[str]:
    """Every allowlisted-extension file under the two readable roots, as
    paths relative to the install root (e.g. "app/routers/races.py") —
    backs the file picker's datalist."""
    out = []
    for root in _ALLOWED_ROOTS:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in _ALLOWED_EXTS:
                out.append(str(p.relative_to(_INSTALL_ROOT)))
    return out


@router.get("/tools/code-assist", response_class=HTMLResponse)
def code_assist_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("code_assist.html", {
        "request": request, "world": world, "worlds": worlds,
        "files": _list_source_files(),
    })


@router.post("/tools/code-assist/generate")
async def code_assist_generate(
    request: Request,
    file: str = Form(...),
    instruction: str = Form(""),
    model: str = Form(""),
    think: bool = Form(False),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        # AudioJob.world_id is a required FK — every job needs an anchor
        # world, even though this op's actual work has nothing to do with
        # any campaign. See this module's own docstring for why that's an
        # acceptable trade rather than a schema change.
        raise HTTPException(400, "No active world")
    if not instruction.strip():
        raise HTTPException(400, "Describe the change to make")
    path = _resolve_safe_path(file)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise HTTPException(400, "Could not read that file as text")
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        raise HTTPException(400, f"File too large to preview here (over {_MAX_FILE_BYTES // 1000} KB)")
    relpath = str(path.relative_to(_INSTALL_ROOT))
    user = getattr(request.state, "user", None)
    job_id = create_assist_job(
        world.id, op=_ai_assist.OP_CODE_EDIT, surface="code_assist",
        content=content, meta=_ai_assist.compose_meta({"File": relpath}),
        instruction=instruction, model=model, think=think,
        created_by_user_id=user.id if user else None,
    )
    return {"job_id": job_id}


@router.get("/tools/code-assist/generate/{job_id}")
def code_assist_status(
    job_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None),
):
    """Poll a code-assist job. Mirrors GET /api/ai/assist-job/{id}'s own
    world-scoping (job.world_id must match the caller's active world), but
    is its own route rather than reusing that one because the "done"
    payload needs a diff computed from data that route doesn't have: this
    reads job.transcript back as the ORIGINAL file content (exactly what
    was sent to the model, not a fresh disk read that could race a
    concurrent edit) and diffs it against the model's revised text."""
    world, _ = get_world_ctx(request, db, active_world)
    job = db.get(AudioJob, job_id)
    if not job or job.purpose != "ai_assist" or (world and job.world_id != world.id):
        raise HTTPException(404)
    try:
        params = json.loads(job.assist_params_json or "{}")
    except ValueError:
        params = {}
    if params.get("op") != _ai_assist.OP_CODE_EDIT:
        raise HTTPException(404)
    if job.status in ("pending", "processing", "summarizing"):
        return {"status": "pending"}
    if job.status != "done":
        return {"status": "error", "error": job.error or "Generation failed"}
    try:
        result = json.loads(job.result_json or "{}")
    except ValueError:
        result = {}
    revised = str(result.get("text") or "")
    original = job.transcript or ""
    meta = params.get("meta") or ""
    # meta is compose_meta()'s "File: <relpath>" line — pull the path back
    # out rather than storing it a second time.
    file_label = meta.split(":", 1)[1].strip() if ":" in meta else meta
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), revised.splitlines(keepends=True),
        fromfile=file_label, tofile=f"{file_label} (AI suggested)",
    ))
    return {
        "status": "done", "file": file_label, "original": original,
        "revised": revised, "diff": diff, "model": result.get("model") or job.model or "",
    }
