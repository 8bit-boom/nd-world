"""Two bundled lore pages ported from NeonDragonsWorld: Dreamlands (a static
atlas write-up linking to the generated Dreamlands board) and The King in
Yellow (an AI-assisted play generator with public-domain research and a
saved-play library used as RAG context for future generations).

Both are GM-facing extras layered on existing machinery (Entity notes, the
`ai` module's model resolution/streaming, InvestBoard via routers/boards_generate.py)
rather than new subsystems.
"""
import asyncio
import json
import random
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ai as _ai_module
from ..database import get_db, get_app_settings
from ..deps import get_world_ctx
from ..templating import templates

router = APIRouter()

_KIY_PLAYS_FILE = Path(__import__("os").environ.get("DB_PATH", "/data/world.db")).parent / "kiy_plays.json"


@router.get("/dreamlands", response_class=HTMLResponse)
def dreamlands_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not get_app_settings(db).dreamlands_enabled:
        return templates.TemplateResponse("feature_disabled.html", {
            "request": request, "world": world, "worlds": worlds,
            "feature_name": "Dreamlands", "feature_icon": "🌙",
        })
    return templates.TemplateResponse("dreamlands.html", {"request": request, "world": world, "worlds": worlds})


@router.get("/king-in-yellow", response_class=HTMLResponse)
def king_in_yellow_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not get_app_settings(db).king_in_yellow_enabled:
        return templates.TemplateResponse("feature_disabled.html", {
            "request": request, "world": world, "worlds": worlds,
            "feature_name": "King in Yellow", "feature_icon": "🎭",
        })
    return templates.TemplateResponse("king_in_yellow.html", {"request": request, "world": world, "worlds": worlds})


# ── Research: pull public-domain / reference material to seed generation ──────

async def _fetch_kiy_inspirations() -> list[dict]:
    """Fetch King in Yellow inspirations from multiple public sources in parallel.
    Every call is independently try/excepted and the whole client has a 9s
    timeout, so a slow or unreachable source just drops out of the results
    rather than failing the request."""
    headers = {"User-Agent": "NeonDragons-WorldBuilder/1.0 (educational)"}

    async def _wiki(client: httpx.AsyncClient, title: str, slug: str):
        try:
            r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")
            if r.status_code == 200:
                extract = r.json().get("extract", "")[:600]
                if extract:
                    return {"source": "Wikipedia", "title": title, "extract": extract}
        except Exception:
            pass
        return None

    async def _ddg(client: httpx.AsyncClient, title: str, q: str):
        try:
            r = await client.get(f"https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1")
            if r.status_code == 200:
                data = r.json()
                text = data.get("AbstractText") or data.get("Answer") or data.get("Definition") or ""
                if not text:
                    for t in data.get("RelatedTopics", []):
                        if isinstance(t, dict) and t.get("Text"):
                            text = t["Text"]
                            break
                if len(text) >= 30:
                    return {"source": "DuckDuckGo", "title": title, "extract": text[:500]}
        except Exception:
            pass
        return None

    async def _gutenberg(client: httpx.AsyncClient) -> list[dict]:
        results = []
        try:
            r = await client.get("https://www.gutenberg.org/cache/epub/8492/pg8492.txt", timeout=12.0)
            if r.status_code != 200:
                return results
            full = r.text
            body_start = full.find("The Repairer of Reputations")
            body_end = full.rfind("End of the Project Gutenberg")
            body = full[body_start:body_end] if body_start > 0 and body_end > body_start else full

            all_paras = [p.strip() for p in body.split("\n\n") if 60 < len(p.strip()) < 1200]
            if all_paras:
                chosen = random.sample(all_paras, min(2, len(all_paras)))
                results.append({
                    "source": "Chambers (1895)", "title": "The King in Yellow — passage",
                    "extract": " [...] ".join(chosen)[:700],
                })

            for story in ["The Yellow Sign", "In the Court of the Dragon", "The Mask",
                          "The Repairer of Reputations", "The Demoiselle d'Ys"]:
                pos = body.find(story)
                if pos < 0:
                    continue
                segment = body[pos:pos + 10000]
                paras = [p.strip() for p in segment.split("\n\n") if 60 < len(p.strip()) < 1200]
                if paras:
                    results.append({
                        "source": "Chambers (1895)", "title": story,
                        "extract": random.choice(paras[:15])[:600],
                    })
                    if len(results) >= 4:
                        break
        except Exception:
            pass
        return results

    async def _open_library(client: httpx.AsyncClient):
        try:
            r = await client.get("https://openlibrary.org/works/OL15143745W.json")
            if r.status_code == 200:
                desc = r.json().get("description", "")
                if isinstance(desc, dict):
                    desc = desc.get("value", "")
                if desc and len(desc) >= 30:
                    return {"source": "Open Library", "title": "The King in Yellow", "extract": desc[:500]}
        except Exception:
            pass
        return None

    async def _archive_org(client: httpx.AsyncClient, query: str, label: str):
        try:
            r = await client.get(
                f"https://archive.org/advancedsearch.php?q={query}"
                "&fl[]=title&fl[]=description&rows=5&output=json&mediatype=texts"
            )
            if r.status_code == 200:
                docs = r.json().get("response", {}).get("docs", [])
                for doc in docs:
                    raw = doc.get("description", "")
                    text = (raw[0] if isinstance(raw, list) else raw) or ""
                    if len(text) >= 30:
                        return {"source": "Archive.org", "title": label, "extract": text[:400]}
        except Exception:
            pass
        return None

    async with httpx.AsyncClient(timeout=9.0, headers=headers, follow_redirects=True) as client:
        scalar_tasks = [
            _wiki(client, "The King in Yellow", "The_King_in_Yellow"),
            _wiki(client, "Hastur", "Hastur"),
            _wiki(client, "Carcosa", "Carcosa"),
            _wiki(client, "Robert W. Chambers", "Robert_W._Chambers"),
            _wiki(client, "The Yellow Sign", "Yellow_Sign"),
            _wiki(client, "Ambrose Bierce", "Ambrose_Bierce"),
            _wiki(client, "Weird fiction", "Weird_fiction"),
            _wiki(client, "Cosmic horror", "Cosmic_horror"),
            _wiki(client, "Lake of Hali", "Lake_of_Hali"),
            _ddg(client, "King in Yellow", "King+in+Yellow"),
            _ddg(client, "Hastur", "Hastur"),
            _ddg(client, "Carcosa", "Carcosa"),
            _ddg(client, "Cosmic horror fiction", "Cosmic+horror+fiction"),
            _open_library(client),
            _archive_org(client, "subject%3A%22King+in+Yellow%22", "King in Yellow — Archive.org"),
            _archive_org(client, "subject%3A%22Hastur%22", "Hastur — Archive.org"),
        ]
        scalar_results, gutenberg_results = await asyncio.gather(
            asyncio.gather(*scalar_tasks), _gutenberg(client),
        )

    return [r for r in scalar_results if r] + gutenberg_results


@router.get("/api/kiy/inspirations")
async def kiy_inspirations():
    return {"inspirations": await _fetch_kiy_inspirations()}


# ── Saved-play library (flat JSON next to the world DB, used as RAG context) ──

def _load_kiy_plays() -> list[dict]:
    try:
        return json.loads(_KIY_PLAYS_FILE.read_text())
    except Exception:
        return []


def _save_kiy_plays(plays: list[dict]) -> None:
    _KIY_PLAYS_FILE.write_text(json.dumps(plays, indent=2))


class _KiyPlayBody(BaseModel):
    text: str
    title: Optional[str] = None


@router.get("/api/kiy/plays")
async def kiy_list_plays():
    plays = _load_kiy_plays()
    return {"plays": [{"id": p["id"], "title": p["title"], "ts": p.get("ts", 0)} for p in plays], "count": len(plays)}


@router.post("/api/kiy/plays")
async def kiy_save_play(body: _KiyPlayBody):
    plays = _load_kiy_plays()
    play_id = str(uuid.uuid4())[:8]
    title = body.title or f"The King in Yellow — {date.today().isoformat()}"
    plays.append({"id": play_id, "title": title, "text": body.text, "ts": int(time.time())})
    _save_kiy_plays(plays)
    return {"id": play_id, "title": title, "count": len(plays)}


@router.delete("/api/kiy/plays/{play_id}")
async def kiy_delete_play(play_id: str):
    plays = [p for p in _load_kiy_plays() if p["id"] != play_id]
    _save_kiy_plays(plays)
    return {"count": len(plays)}


# ── Generation ──────────────────────────────────────────────────────────────

class _KiyGenerateReq(BaseModel):
    model: Optional[str] = None


@router.post("/api/kiy/generate")
async def kiy_generate(req: _KiyGenerateReq = _KiyGenerateReq()):
    inspirations = await _fetch_kiy_inspirations()
    inspiration_block = "\n".join(
        f"[{i['source']} — {i['title']}]: {i['extract']}" for i in inspirations
    ) or "(no external inspirations retrieved — draw from imagination alone)"

    rag_block = ""
    library = _load_kiy_plays()
    if library:
        samples = random.sample(library, min(2, len(library)))
        rag_parts = [f'--- excerpt from "{p["title"]}" ---\n{p["text"][:2500].strip()}\n---' for p in samples]
        rag_block = (
            "\n\nRetrieved style references from the play library "
            "(study the voice, rhythm, and structure — do NOT copy plot or characters):\n"
            + "\n\n".join(rag_parts)
        )

    system = (
        "You are the anonymous author of THE KING IN YELLOW — a forbidden two-act play "
        "set in the mythical city of CARCOSA on the shores of the Lake of Hali beneath twin suns. "
        "Write the complete play now in proper theatrical script format. "
        "Every performance must be genuinely unique: different character names, different opening scene, "
        "different central mystery. The King in Yellow wears many masks.\n\n"
        "Canonical elements (use freely): Cassilda, Camilla, The Stranger (who may be Hastur), "
        "The Pallid Mask, Uoht, Thale, Aldones, the Yellow Sign, Carcosa, Lake of Hali, "
        "the Hyades, twin suns, black stars, the Phantom of Truth.\n\n"
        "Themes: identity erosion, forbidden knowledge, the horror of beauty, "
        "madness as revelation, the thin line between art and destruction.\n\n"
        "STRUCTURE:\n"
        "ACT I — Appears to be a melancholy aristocratic drama. Foreshadows what is to come. "
        "Ends with a character beginning to read Act II aloud.\n"
        "ACT II — Reality unravels. Cosmic horror is revealed. Characters lose identity. "
        "The Yellow Sign manifests. There is no happy ending.\n\n"
        "STRICT FORMAT — each element on its own line:\n"
        "  ACT I\n  Scene 1\n  [Stage direction describing the setting]\n  CHARACTER NAME. Dialogue here.\n\n"
        f"Inspirations gathered from across the veil (weave these themes in subtly, do not quote):\n{inspiration_block}"
        f"{rag_block}"
    )

    async def _gen():
        model, _note = await _ai_module.resolve_model(req.model or "")
        async for token in _ai_module.stream_chat(
            [{"role": "user", "content": "Write the play. Begin with ACT I."}], system=system, model=model,
        ):
            yield f"data: {json.dumps({'token': token})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@router.post("/api/kiy/build-model")
async def kiy_build_model():
    """Fine-tune a local Ollama model from the saved play library. Requires a
    real local Ollama daemon (Client.create builds the model server-side) —
    unavailable wherever ollama is stubbed out (e.g. Android), where this
    degrades to a normal SSE error event rather than a crash."""
    plays = _load_kiy_plays()
    if not plays:
        raise HTTPException(400, "No training plays saved yet")

    base_model, _note = await _ai_module.resolve_model("")
    system = (
        "You are the anonymous author of THE KING IN YELLOW — a forbidden two-act play "
        "set in the mythical city of CARCOSA. Write unique, atmospheric plays every time. "
        "Follow strict theatrical script format with ACT headings, Scene headings, "
        "stage directions in [brackets], and CHARACTER NAME. Dialogue lines."
    )
    lines = [f"FROM {base_model}", f"SYSTEM {json.dumps(system)}"]
    for play in plays[-6:]:
        excerpt = play["text"][:6000]
        lines.append(f"MESSAGE user {json.dumps('Write a complete King in Yellow play in two acts.')}")
        lines.append(f"MESSAGE assistant {json.dumps(excerpt)}")
    modelfile = "\n".join(lines)

    async def _gen():
        try:
            client = _ai_module._client()
            async for chunk in await client.create(model="kiy-author:latest", modelfile=modelfile, stream=True):
                yield f"data: {json.dumps({'status': chunk.get('status', '')})}\n\n"
            yield f"data: {json.dumps({'done': True, 'model': 'kiy-author:latest'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                              headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
