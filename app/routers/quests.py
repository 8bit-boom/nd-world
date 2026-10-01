import json
import re
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from typing import Optional

from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sqlalchemy import or_

from .. import ai as _ai
from .. import live
from .. import retrieval as _retrieval
from ..database import SessionLocal, get_db
from ..deps import get_world_ctx, is_gm, world_can_edit_row, world_can_edit_section, world_can_view_section, world_row_visible
from ..models import Entity, Fact, GameSession, Party, Quest, World
from ..templating import templates

router = APIRouter()

STATUSES = ["active", "complete", "failed", "secret"]
CATEGORIES = ["main", "side", "personal"]


def _current_user_id(request: Request):
    user = getattr(request.state, "user", None)
    return user.id if user else None


@router.get("/api/quests/board")
async def quests_board(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Active top-level quests as JSON for the GM Cockpit's quests panel —
    the same slice the old fixed cockpit rendered server-side. GM-only
    (not in _is_player_safe; the handler re-checks)."""
    if not is_gm(request):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    quests = (
        db.query(Quest)
        .filter(Quest.world_id == world.id, Quest.status == "active", Quest.parent_id.is_(None))
        .order_by(Quest.category, Quest.title)
        .limit(12)
        .all()
    )
    party_names = {p.id: p.name for p in db.query(Party).filter(Party.world_id == world.id).all()}
    return {"quests": [{
        "id": q.id, "title": q.title, "category": q.category or "main",
        "summary": q.summary or "",
        "party_id": q.assigned_party_id,
        "party": party_names.get(q.assigned_party_id),
    } for q in quests]}


@router.get("/quests", response_class=HTMLResponse)
def quests_list(request: Request, q: str = "", category: str = "", party: str = "",
                db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not world_can_view_section(request, world, "quests"):
        raise HTTPException(403)
    query = db.query(Quest).filter(Quest.world_id == world.id)
    if not is_gm(request):
        # visible_to_players=False is the GM's "hide this quest from the
        # table entirely" flag — the MCP list tool and the world-summary
        # pipeline already honor it; the web list must too, or a player
        # granted quests-read sees hidden titles/summaries/bodies.
        query = query.filter(Quest.visible_to_players.isnot(False))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(or_(Quest.title.ilike(like), Quest.summary.ilike(like), Quest.body.ilike(like)))
    if category:
        query = query.filter(Quest.category == category)
    if party.isdigit():
        query = query.filter(Quest.assigned_party_id == int(party))
    quests = query.order_by(Quest.title).all()

    # Sub-quest progress (done/total per parent quest) — computed from the
    # already-fetched rows, no extra queries.
    subs_done: dict = {}
    subs_total: dict = {}
    for quest in quests:
        if quest.parent_id:
            subs_total[quest.parent_id] = subs_total.get(quest.parent_id, 0) + 1
            if (quest.status or "") == "complete":
                subs_done[quest.parent_id] = subs_done.get(quest.parent_id, 0) + 1

    grouped: dict = {s: [] for s in STATUSES}
    for quest in quests:
        grouped.setdefault(quest.status or "active", []).append(quest)

    # Parties for the filter dropdown; recent sessions for the AI sync panel.
    parties = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all() if world else []
    sessions = (
        db.query(GameSession).filter(GameSession.world_id == world.id)
        .order_by(GameSession.session_num.desc()).limit(50).all()
    ) if world else []

    return templates.TemplateResponse("quests/list.html", {
        "request": request, "world": world, "worlds": worlds, "grouped": grouped, "statuses": STATUSES,
        "can_create": world_can_edit_section(request, world, "quests"),
        "q": term, "category": category, "party": party, "parties": parties,
        "subs_done": subs_done, "subs_total": subs_total,
        "sessions": sessions,
        "ai_ready": _ai.effective_llm_api_key(),
    })


@router.get("/quests/new", response_class=HTMLResponse)
def quest_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world or not world_can_edit_section(request, world, "quests"):
        raise HTTPException(403)
    parties = db.query(Party).filter(Party.world_id == world.id).order_by(Party.name).all()
    quests = db.query(Quest).filter(Quest.world_id == world.id).order_by(Quest.title).all()
    entities = db.query(Entity).filter(Entity.world_id == world.id).order_by(Entity.name).all()
    return templates.TemplateResponse("quests/detail.html", {
        "request": request, "world": world, "worlds": worlds, "quest": None,
        "parties": parties, "quests": quests, "entities": entities,
        "linked_entities": [], "statuses": STATUSES, "categories": CATEGORIES,
        "can_edit_this": True,
    })


@router.post("/quests/new")
async def quest_create(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    if not world or not world_can_edit_section(request, world, "quests"):
        raise HTTPException(403)
    form = await request.form()
    # A plain player creating under their own "quests: edit" grant owns the
    # row (see Quest.created_by_user_id) and may only ever edit/delete this
    # one going forward — a GM or assistant creating one leaves it
    # GM-authored (None), same as every quest before this column existed,
    # since both already have unrestricted edit rights on every quest here.
    user = getattr(request.state, "user", None)
    is_gm_or_assistant = bool(user and (user.is_gm or getattr(request.state, "is_assistant", False)))
    q = Quest(
        world_id=world.id,
        title=str(form.get("title", "")).strip() or "Untitled Quest",
        status=str(form.get("status", "active")).strip() or "active",
        category=str(form.get("category", "main")).strip() or "main",
        summary=str(form.get("summary", "")).strip(),
        body=str(form.get("body", "")),
        parent_id=int(form["parent_id"]) if form.get("parent_id") else None,
        assigned_party_id=int(form["assigned_party_id"]) if form.get("assigned_party_id") else None,
        created_by_user_id=None if is_gm_or_assistant else _current_user_id(request),
    )
    raw_links = str(form.get("linked_entities_json", "[]") or "[]")
    try:
        json.loads(raw_links)
    except Exception:
        raw_links = "[]"
    q.linked_entities_json = raw_links
    db.add(q)
    db.commit()
    db.refresh(q)
    return RedirectResponse(f"/quests/{q.id}", status_code=303)


@router.get("/quests/{quest_id}", response_class=HTMLResponse)
def quest_detail(quest_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest or not world_row_visible(request, db, quest.world_id, "quests"):
        raise HTTPException(404)
    # Same hidden-quest rule as the list route: a non-GM gets a 404, not the
    # body of a quest the GM explicitly hid (guessable sequential ids).
    if not quest.visible_to_players and not is_gm(request):
        raise HTTPException(404)
    quest_world = world if (world and world.id == quest.world_id) else db.get(World, quest.world_id)
    parties = db.query(Party).filter(Party.world_id == quest.world_id).order_by(Party.name).all()
    quests = db.query(Quest).filter(Quest.world_id == quest.world_id, Quest.id != quest.id).order_by(Quest.title).all()
    entities = db.query(Entity).filter(Entity.world_id == quest.world_id).order_by(Entity.name).all()
    linked = json.loads(quest.linked_entities_json or "[]")
    entity_map = {e.id: e for e in db.query(Entity).filter(Entity.id.in_([l["entity_id"] for l in linked])).all()} if linked else {}
    linked_entities = [
        {"entity": {"id": entity_map[l["entity_id"]].id, "name": entity_map[l["entity_id"]].name}, "role": l.get("role", "")}
        for l in linked if entity_map.get(l["entity_id"])
    ]
    return templates.TemplateResponse("quests/detail.html", {
        "request": request, "world": world, "worlds": worlds, "quest": quest,
        "parties": parties, "quests": quests, "entities": entities,
        "linked_entities": linked_entities, "statuses": STATUSES, "categories": CATEGORIES,
        "can_edit_this": world_can_edit_row(request, quest_world, "quests", quest.created_by_user_id),
    })


@router.post("/quests/{quest_id}/edit")
async def quest_edit(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
    form = await request.form()
    quest.title = str(form.get("title", quest.title)).strip() or quest.title
    quest.status = str(form.get("status", "active")).strip() or "active"
    quest.category = str(form.get("category", "main")).strip() or "main"
    quest.summary = str(form.get("summary", "")).strip()
    quest.body = str(form.get("body", ""))
    parent_id = form.get("parent_id")
    quest.parent_id = int(parent_id) if parent_id and int(parent_id) != quest.id else None
    assigned = form.get("assigned_party_id")
    quest.assigned_party_id = int(assigned) if assigned else None
    raw_links = str(form.get("linked_entities_json", "[]") or "[]")
    try:
        json.loads(raw_links)
    except Exception:
        raw_links = "[]"
    quest.linked_entities_json = raw_links
    db.commit()
    return RedirectResponse(f"/quests/{quest_id}?saved=1", status_code=303)


@router.post("/api/quests/{quest_id}/status")
async def quest_status(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
    body = await request.json()
    status = str(body.get("status", "")).strip()
    if status:
        quest.status = status
        db.commit()
        live.touch(quest.world_id)
    return {"status": quest.status}


@router.post("/quests/{quest_id}/delete")
def quest_delete(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.query(Quest).filter(Quest.id == quest_id).first()
    if not quest:
        raise HTTPException(404)
    world = db.get(World, quest.world_id)
    if not world_can_edit_row(request, world, "quests", quest.created_by_user_id):
        raise HTTPException(403)
    db.query(Quest).filter(Quest.parent_id == quest_id).update({"parent_id": None})
    db.delete(quest)
    db.commit()
    return RedirectResponse("/quests", status_code=303)



# ── AI: quest suggestions from a session log ─────────────────────────────────
# A GM runs a session, then asks the AI to diff the session log against the
# current quest board: which quests advanced/completed/failed, and what new
# plot threads emerged. Suggestions are RETURNED for GM review — nothing is
# written until POST /api/quests/apply confirms them (same AI-drafts-
# GM-confirms pattern as the entity/relation suggestion flows).

def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _session_quest_material(db: Session, gs: GameSession, char_budget: int = 6000) -> str:
    """The session's story material for the AI: the GM summary (or the
    live transcript's tail when there is no summary), plus the session's
    Facts. Trimmed to `char_budget` so a long recording doesn't blow the
    context on its own."""
    parts = []
    summary = (gs.summary or "").strip()
    if summary:
        parts.append("Session summary:\n" + summary[:char_budget // 2])
    transcript = (gs.live_transcript or "").strip()
    if transcript and not summary:
        parts.append("Session transcript (tail):\n" + transcript[-char_budget:])
    facts = db.query(Fact).filter(Fact.game_session_id == gs.id).order_by(Fact.created_at).all()
    if facts:
        parts.append("Logged facts:\n" + "\n".join(f"- {f.content}" for f in facts[-30:]))
    return "\n\n".join(parts).strip()


@router.post("/api/quests/apply")
async def quests_apply_suggestions(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Apply reviewed AI suggestions: create new quests and apply quest
    updates. GM-only. Body: {new_quests: [...], quest_updates: [...], owner_user_id?} —
    the same shapes /api/quests/suggest returned."""
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    if not is_gm(request):
        raise HTTPException(403)
    body = await request.json()
    user = getattr(request.state, "user", None)

    created = 0
    world_party_ids = {pid for (pid,) in db.query(Party.id).filter(Party.world_id == world.id).all()}
    for n in (body.get("new_quests") or [])[:12]:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        # A party can be attached only if it lives in THIS world; a missing,
        # non-integer or foreign id is ignored (the quest is still created).
        party_id = n.get("assigned_party_id")
        if isinstance(party_id, bool) or not isinstance(party_id, int) or party_id not in world_party_ids:
            party_id = None
        db.add(Quest(
            world_id=world.id, title=title[:256], assigned_party_id=party_id,
            status=n.get("status") if n.get("status") in STATUSES else "active",
            category=n.get("category") if n.get("category") in CATEGORIES else "side",
            summary=str(n.get("summary") or "").strip()[:512],
            body=str(n.get("body") or ""),
            visible_to_players=bool(n.get("visible_to_players", True)),
            created_by_user_id=user.id if user else None,
        ))
        created += 1

    updated = 0
    quest_ids = [u.get("quest_id") for u in (body.get("quest_updates") or [])
                 if isinstance(u, dict) and isinstance(u.get("quest_id"), int)]
    quest_map = {}
    if quest_ids:
        for q in db.query(Quest).filter(Quest.id.in_(quest_ids), Quest.world_id == world.id).all():
            quest_map[q.id] = q
    for u in (body.get("quest_updates") or [])[:24]:
        if not isinstance(u, dict):
            continue
        quest = quest_map.get(u.get("quest_id"))
        if not quest:
            continue
        if u.get("status") in STATUSES:
            quest.status = u["status"]
        if str(u.get("note") or "").strip():
            note = str(u["note"]).strip()[:512]
            quest.summary = note
        updated += 1
    db.commit()
    live.touch(world.id)
    return {"created": created, "updated": updated}


# Quest AI sync runs as an in-process background job — reasoning + RAG +
# generation exceeds Cloudflare Tunnel's ~100 s no-byte timeout (HTTP 524),
# the same failure that moved AI Build to this pattern. POST starts the job
# and returns an id; GET polls. Results are in-process: a GM actively
# waiting on one panel, and a restart just means re-clicking Generate.
_QUEST_SUGGEST_JOBS: dict = {}
_QUEST_SUGGEST_SEQ: list = [0]


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


@router.post("/api/quests/suggest/start")
async def quests_suggest_start(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Start the AI quest-sync for this world — returns {"job_id"} for the
    poll route. GM-only (the prompt includes GM-only lore via RAG and full
    session summaries — never a player surface)."""
    if not is_gm(request):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No active world")
    body = await request.json()
    session_id = body.get("session_id")
    text = str(body.get("text") or "").strip()
    if not session_id and not text:
        raise HTTPException(400, "Provide session_id or text")
    if not _ai.effective_llm_api_key():
        raise HTTPException(400, "No Unsloth backend configured — set UNSLOTH_API_KEY (Settings → System).")

    job_id = _QUEST_SUGGEST_SEQ[0] + 1
    _QUEST_SUGGEST_SEQ[0] = job_id
    _QUEST_SUGGEST_JOBS[job_id] = {"status": "running", "started": time.time(),
                                   "suggestions": None, "error": ""}
    done = [j for j, v in _QUEST_SUGGEST_JOBS.items() if v["status"] != "running"]
    while len(done) > 12:
        _QUEST_SUGGEST_JOBS.pop(done.pop(0), None)

    import asyncio as _asyncio
    _user = getattr(request.state, "user", None)
    _asyncio.get_running_loop().create_task(_quests_suggest_task(
        job_id, world.id,
        session_id=int(session_id) if session_id else None,
        text=text, model=str(body.get("model") or "").strip(),
        use_rag=bool(body.get("use_rag", True)),
        think=bool(body.get("think", True)),
    ))
    return {"job_id": job_id, "status": "running"}


@router.get("/api/quests/suggest/{job_id}")
async def quests_suggest_poll(job_id: int):
    """Poll a quest-sync job: running (with elapsed seconds), done (with the
    draft suggestions), or error (with the reason)."""
    job = _QUEST_SUGGEST_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown quest-sync job")
    if job["status"] == "running":
        return {"status": "running", "elapsed": round(time.time() - job["started"])}
    if job["status"] == "error":
        return {"status": "error", "error": job["error"]}
    return {"status": "done", "suggestions": job["suggestions"]}


async def _quests_suggest_task(job_id: int, world_id: int,
                               session_id: Optional[int], text: str, model: str,
                               use_rag: bool, think: bool = True):
    db = SessionLocal()
    try:
        world = db.get(World, world_id)
        gs = None
        material = ""
        if session_id:
            gs = db.query(GameSession).filter(
                GameSession.id == session_id, GameSession.world_id == world_id).first()
            if gs:
                material = _session_quest_material(db, gs)
        if not material and text:
            material = text[:8000]
        if not material and gs:
            material = f"Session #{gs.session_num}: {gs.title or ''}"

        quests = db.query(Quest).filter(Quest.world_id == world_id).order_by(Quest.title).all()
        quest_ids_in_world = {q.id for q in quests}
        board = "\n".join(
            f"- id={q.id} [{q.status or 'active'}/{q.category or 'main'}] {q.title}"
            + (f" — {q.summary[:120]}" if q.summary else "")
            for q in quests
        ) or "(no quests yet)"

        world_ctx = ""
        if use_rag:
            try:
                # _retrieval is the module-level import (app.retrieval) — a
                # local `from . import retrieval` here once shadowed it with
                # app.routers.retrieval (nonexistent) and the blanket except
                # silently turned quest-sync RAG off.
                rag, _n, _notes = _retrieval.smart_world_context(
                    db, world_id, material[:1500], entity_limit=8, notes_limit=2,
                )
                if rag:
                    world_ctx = "\n\n=== World lore (ground the suggestions in this) ===\n" + rag[:4000]
            except Exception:
                world_ctx = ""
    finally:
        db.close()

    system = (
        "You are a campaign co-GM. Compare the session material against the CURRENT QUEST "
        "BOARD and report what changed. Return STRICT JSON only:\n"
        '{"new_quests": [{"title": str, "summary": str, "category": "main"|"side"|"personal"}], '
        '"quest_updates": [{"quest_id": int, "status": "active"|"complete"|"failed", "note": str}]}\n'
        "Rules: only propose quest_updates whose quest_id appears verbatim in the current "
        "quest board; only mark complete/failed when the session says it plainly; keep new "
        "quest titles short and in-world; propose a new quest only for a genuine open plot "
        "thread the session introduced (not a one-off scene beat); it is fine to return "
        "empty arrays if nothing changed. No comments, no markdown fences."
    )
    user_text = (
        "=== CURRENT QUEST BOARD ===\n" + board
        + "\n\n=== SESSION MATERIAL ===\n" + material
        + world_ctx
    )

    raw = await _ai.generate_chat(
        [{"role": "user", "content": user_text}],
        system=system, model=model, think=think,
        format={"type": "object",
                "properties": {
                    "new_quests": {"type": "array"},
                    "quest_updates": {"type": "array"},
                },
                "required": ["new_quests", "quest_updates"]},
    )

    try:
        parsed = _extract_json(raw)
    except ValueError:
        _QUEST_SUGGEST_JOBS[job_id].update(
            status="error", error="The model's reply wasn't valid JSON — try again.")
        return

    new_quests = parsed.get("new_quests") or []
    quest_updates = parsed.get("quest_updates") or []
    quest_updates = [
        u for u in quest_updates
        if isinstance(u, dict) and isinstance(u.get("quest_id"), int)
        and u["quest_id"] in quest_ids_in_world
    ]
    new_quests = [
        n for n in new_quests
        if isinstance(n, dict) and str(n.get("title") or "").strip()
    ]
    _QUEST_SUGGEST_JOBS[job_id].update(
        status="done",
        suggestions={
            "new_quests": [
                {"title": str(n["title"]).strip()[:256],
                 "summary": str(n.get("summary") or "").strip()[:512],
                 "category": n.get("category") if n.get("category") in CATEGORIES else "side"}
                for n in new_quests[:12]
            ],
            "quest_updates": [
                {"quest_id": u["quest_id"],
                 "status": u.get("status") if u.get("status") in STATUSES else "active",
                 "note": str(u.get("note") or "").strip()[:512]}
                for u in quest_updates[:24]
            ],
        },
    )
