import json as _json
import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse as _SR
from pydantic import BaseModel
from typing import List
from .. import ai as _ai

router = APIRouter(prefix="/api/ai", tags=["ai"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatBody(BaseModel):
    messages: List[ChatMessage]
    system: str = ""
    model: str = ""


@router.post("/chat")
async def ai_chat(body: ChatBody):
    msgs = [{"role": m.role, "content": m.content} for m in body.messages]
    return {"result": await _ai.generate_chat(msgs, body.system, body.model)}


@router.post("/stream")
async def ai_stream(body: ChatBody):
    msgs = [{"role": m.role, "content": m.content} for m in body.messages]

    async def _gen():
        async for token in _ai.stream_chat(msgs, body.system, body.model):
            yield f"data: {_json.dumps({'token': token})}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/models")
async def ai_models():
    loaded = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_ai.OLLAMA_URL}/api/tags")
            loaded = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    result = [
        {**m, "loaded": any(m["id"] in l or l in m["id"] for l in loaded),
         "builtin": m["id"] in builtin_ids}
        for m in _ai.all_models()
    ]
    return {"models": result, "default": _ai.OLLAMA_MODEL}


class AddModelBody(BaseModel):
    id: str
    label: str = ""


@router.post("/models/add")
async def ai_models_add(body: AddModelBody):
    model_id = body.id.strip()
    if not model_id:
        from fastapi import HTTPException
        raise HTTPException(400, "model id required")
    label = body.label.strip() or model_id.split("/")[-1].split(":")[0]
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    if model_id in builtin_ids:
        _ai.unhide_builtin(model_id)
    else:
        custom = _ai.load_custom_models()
        if not any(m["id"] == model_id for m in custom):
            custom.append({"id": model_id, "label": label})
            _ai.save_custom_models(custom)
    return {"ok": True}


class RemoveModelBody(BaseModel):
    model_id: str
    delete_from_ollama: bool = False


@router.post("/models/remove")
async def ai_models_remove(body: RemoveModelBody):
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    if body.model_id in builtin_ids:
        _ai.hide_builtin(body.model_id)
    else:
        custom = _ai.load_custom_models()
        custom = [m for m in custom if m["id"] != body.model_id]
        _ai.save_custom_models(custom)
    if body.delete_from_ollama:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.request(
                    "DELETE", f"{_ai.OLLAMA_URL}/api/delete",
                    json={"name": body.model_id},
                )
        except Exception:
            pass
    return {"ok": True}


class PullBody(BaseModel):
    model_id: str


@router.post("/pull")
async def ai_pull(body: PullBody):
    async def _gen():
        try:
            async with httpx.AsyncClient(timeout=3600.0) as client:
                async with client.stream(
                    "POST", f"{_ai.OLLAMA_URL}/api/pull",
                    json={"name": body.model_id, "stream": True},
                ) as r:
                    async for line in r.aiter_lines():
                        if line:
                            yield f"data: {line}\n\n"
        except Exception as exc:
            yield f"data: {_json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


class EntityBody(BaseModel):
    name: str
    type: str
    description: str = ""


class NpcBody(BaseModel):
    name: str
    role: str
    notes: str = ""


class LocationBody(BaseModel):
    name: str
    region: str
    notes: str = ""


class QuestBody(BaseModel):
    title: str
    context: str = ""


@router.post("/generate/entity")
async def gen_entity(body: EntityBody):
    prompt = (
        f"Write an expanded description for this {body.type} named '{body.name}'. "
        f"Existing notes: {body.description}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/npc")
async def gen_npc(body: NpcBody):
    prompt = (
        f"Create a backstory and personality for an NPC named '{body.name}' "
        f"who is a {body.role}. Notes: {body.notes}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/location")
async def gen_location(body: LocationBody):
    prompt = (
        f"Describe the location '{body.name}' in the region '{body.region}'. "
        f"Notes: {body.notes}"
    )
    return {"result": await _ai.generate(prompt)}


@router.post("/generate/quest")
async def gen_quest(body: QuestBody):
    prompt = f"Create a quest hook for '{body.title}'. World context: {body.context}"
    return {"result": await _ai.generate(prompt)}


@router.post("/status")
async def ai_status():
    return await _ai.status()
