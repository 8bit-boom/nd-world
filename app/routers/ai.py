import json as _json
import csv as _csv
import logging
import os as _os
import ollama as _ollama
import urllib.request as _urllib
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse as _SR
from pydantic import BaseModel
from typing import List
from pathlib import Path as _Path
from .. import ai as _ai

router = APIRouter(prefix="/api/ai", tags=["ai"])
_log = logging.getLogger("nd.ai.router")


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
    requested = body.model
    _log.info("stream requested model=%r msgs=%d", requested, len(body.messages))

    async def _gen():
        model = await _ai.resolve_model(requested)
        async for token in _ai.stream_chat(msgs, body.system, model):
            yield f"data: {_json.dumps({'token': token})}\n\n"
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@router.get("/models")
async def ai_models():
    loaded = await _ai._list_loaded()
    loaded_lower = [ll.lower() for ll in loaded]

    def _is_loaded(model_id: str) -> bool:
        ml = model_id.lower()
        return any(ml == ll or ml in ll or ll in ml for ll in loaded_lower)

    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    result = [
        {**m, "loaded": _is_loaded(m["id"]), "builtin": m["id"] in builtin_ids}
        for m in _ai.all_models()
    ]
    # Auto-surface any Ollama model not already in the list
    listed_lower = {m["id"].lower() for m in result}
    for lid in loaded:
        ll = lid.lower()
        if not any(ll == r or ll in r or r in ll for r in listed_lower):
            result.append({"id": lid, "label": lid, "loaded": True, "builtin": False})
    return {"models": result, "default": _ai.OLLAMA_MODEL, "available": loaded}


@router.get("/debug")
async def ai_debug():
    return await _ai.debug_info()


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
    _log.info("model add: %s", model_id)
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
    _log.info("model remove: %s delete_from_ollama=%s", body.model_id, body.delete_from_ollama)
    builtin_ids = {m["id"] for m in _ai.KNOWN_MODELS}
    if body.model_id in builtin_ids:
        _ai.hide_builtin(body.model_id)
    else:
        custom = _ai.load_custom_models()
        custom = [m for m in custom if m["id"] != body.model_id]
        _ai.save_custom_models(custom)
    if body.delete_from_ollama:
        try:
            await _ai._client().delete(body.model_id)
        except Exception:
            pass
    return {"ok": True}


@router.post("/models/reset")
async def ai_models_reset():
    """Restore all built-in models (clears hidden list)."""
    _ai.reset_hidden()
    _log.info("model list reset to defaults")
    return {"ok": True}


class PullBody(BaseModel):
    model_id: str


@router.post("/pull")
async def ai_pull(body: PullBody):
    _log.info("pull model=%r", body.model_id)

    async def _gen():
        try:
            async for progress in await _ai._client().pull(body.model_id, stream=True):
                yield f"data: {_json.dumps(progress.model_dump())}\n\n"
        except _ollama.ResponseError as exc:
            yield f"data: {_json.dumps({'error': f'Ollama {exc.status_code}: {exc.error}'})}\n\n"
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


# ── Image generation routes ───────────────────────────────────────────────────

@router.get("/imagegen/status")
async def api_imagegen_status():
    return await _ai.imagegen_status()


@router.get("/imagegen/models")
async def api_imagegen_models():
    return {"models": await _ai.imagegen_models()}


@router.get("/imagegen/loras")
async def api_imagegen_loras():
    return {"loras": await _ai.imagegen_loras()}


@router.get("/imagegen/samplers-schedulers")
async def api_imagegen_samplers_schedulers():
    return await _ai.imagegen_samplers_schedulers()


@router.get("/imagegen/upscalers")
async def api_imagegen_upscalers():
    return {"upscalers": await _ai.imagegen_upscalers()}


# ── Tag autocomplete ───────────────────────────────────────────────────────────

_TAG_CSV_URL = "https://github.com/BetaDoggo/danbooru-tag-list/releases/download/Model-Tags/ChenkinNoob-XL-V0.3_underscore.csv"
_TAG_CAT_COLORS = {0: "#aaa", 1: "#c0a060", 3: "#a060c0", 4: "#60a0c0", 5: "#60c080"}
# in-memory cache: list of [tag, category, count]
_tags_cache: list[list] = []


def _tag_file() -> _Path:
    data_dir = _Path(_os.environ.get("DB_PATH", "/data/world.db")).parent
    return data_dir / "tags" / "danbooru_tags.csv"


def _swarmui_ac_dir() -> _Path | None:
    """Directory SwarmUI reads for autocomplete CSVs (shared volume mount)."""
    d = _os.environ.get("SWARMUI_AC_DIR", "/data/swarmui-ac")
    return _Path(d) if d else None


def _load_tags_from_disk() -> list[list]:
    global _tags_cache
    if _tags_cache:
        return _tags_cache
    tf = _tag_file()
    if not tf.exists():
        return []
    rows: list[list] = []
    with open(tf, newline="", encoding="utf-8") as f:
        for row in _csv.reader(f):
            if len(row) >= 3:
                try:
                    rows.append([row[0], int(row[1]), int(row[2])])
                except ValueError:
                    pass
    _tags_cache = rows
    return rows


@router.get("/imagegen/tags/status")
async def api_tags_status():
    tf = _tag_file()
    if tf.exists():
        count = len(_load_tags_from_disk())
        return {"loaded": True, "count": count, "file": tf.name}
    return {"loaded": False, "count": 0, "file": ""}


@router.post("/imagegen/tags/fetch")
async def api_tags_fetch():
    global _tags_cache
    import asyncio
    tf = _tag_file()
    tf.parent.mkdir(parents=True, exist_ok=True)

    def _download():
        opener = _urllib.build_opener()
        opener.addheaders = [("User-Agent", "nd-world/1.0")]
        with opener.open(_TAG_CSV_URL, timeout=60) as resp:
            data = resp.read()
        tf.write_bytes(data)
        # Also copy to SwarmUI Data/Autocompletions if the shared volume is mounted
        ac_dir = _swarmui_ac_dir()
        if ac_dir and ac_dir.exists():
            try:
                ac_dir.mkdir(parents=True, exist_ok=True)
                (ac_dir / "danbooru.csv").write_bytes(data)
                _log.info("copied tag CSV to SwarmUI Autocompletions at %s", ac_dir)
            except Exception as copy_exc:
                _log.warning("could not copy tags to SwarmUI AC dir: %s", copy_exc)
        return len(data)

    try:
        size = await asyncio.get_event_loop().run_in_executor(None, _download)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    _tags_cache = []          # invalidate cache so it reloads
    count = len(_load_tags_from_disk())
    # Report whether SwarmUI copy succeeded
    ac_dir = _swarmui_ac_dir()
    swarmui_ok = bool(ac_dir and (ac_dir / "danbooru.csv").exists())
    return {"ok": True, "bytes": size, "count": count, "swarmui_ac": swarmui_ok}


@router.get("/imagegen/tags")
async def api_tags_search(q: str = "", limit: int = 25):
    if len(q) < 1:
        return {"tags": []}
    tags = _load_tags_from_disk()
    q_norm = q.lower().replace(" ", "_")
    exact: list[list] = []
    prefix: list[list] = []
    for t in tags:
        name = t[0]
        if name == q_norm:
            exact.append(t)
        elif name.startswith(q_norm):
            prefix.append(t)
        if len(exact) + len(prefix) >= limit * 3:
            break
    results = (exact + prefix)[:limit]
    return {
        "tags": [
            {"tag": t[0], "category": t[1], "count": t[2],
             "color": _TAG_CAT_COLORS.get(t[1], "#aaa")}
            for t in results
        ]
    }


class ImagegenBody(BaseModel):
    prompt: str = ""
    negative: str = ""
    model: str = ""
    width: int = 512
    height: int = 512
    steps: int = 20
    cfg: float = 7.0
    seed: int = -1
    sampler: str = "euler"
    scheduler: str = "normal"
    batch_size: int = 1
    loras: str = ""
    lora_weights: str = ""
    vae: str = ""
    clip_skip: int = -1
    init_image: str = ""
    init_strength: float = 0.6
    upscale_model: str = ""
    upscale_factor: float = 1.0


@router.post("/imagegen/generate")
async def api_imagegen_generate(body: ImagegenBody):
    _uploads = _Path(_os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
    try:
        urls = await _ai.imagegen_generate(
            prompt=body.prompt, negative=body.negative, model=body.model,
            width=body.width, height=body.height, steps=body.steps,
            cfg=body.cfg, seed=body.seed, uploads_dir=_uploads,
            sampler=body.sampler, scheduler=body.scheduler,
            batch_size=body.batch_size, loras=body.loras,
            lora_weights=body.lora_weights, vae=body.vae,
            clip_skip=body.clip_skip, init_image=body.init_image,
            init_strength=body.init_strength,
            upscale_model=body.upscale_model, upscale_factor=body.upscale_factor,
        )
        return {"url": urls[0] if urls else "", "urls": urls}
    except Exception as exc:
        _log.error("imagegen_generate failed: %s", exc)
        return {"url": "", "urls": [], "error": str(exc)}


@router.get("/test-chat")
async def ai_test_chat(model: str = ""):
    """Non-streaming single-turn test. Shows exact Ollama error for a given model ID."""
    resolved = await _ai.resolve_model(model)
    result = await _ai.generate_chat(
        [{"role": "user", "content": "Say only the word OK."}],
        system="",
        model=resolved,
    )
    return {"requested": model, "resolved": resolved, "result": result}


@router.get("/ping")
async def ai_ping():
    """SSE smoke test — streams 5 dummy tokens without touching Ollama."""
    import asyncio

    async def _gen():
        for word in ["SSE", " ", "ping", " ", "OK"]:
            yield f"data: {_json.dumps({'token': word})}\n\n"
            await asyncio.sleep(0.05)
        yield "data: [DONE]\n\n"

    return _SR(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
