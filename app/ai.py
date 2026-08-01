import os
import json as _json
import logging
from pathlib import Path
from collections.abc import AsyncGenerator
import ollama as _ollama
import httpx as _httpx

_log = logging.getLogger("nd.ai")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

# Runtime overrides (set from AppSettings via POST /settings/system, without
# needing a restart — see main.py's _refresh_settings_overrides()). Blank means
# "use the env-var default above."
_ollama_url_override: str = ""
_ollama_model_override: str = ""


def set_ollama_override(url: str, model: str) -> None:
    global _ollama_url_override, _ollama_model_override
    _ollama_url_override = (url or "").rstrip("/")
    _ollama_model_override = model or ""


def effective_ollama_url() -> str:
    return _ollama_url_override or OLLAMA_URL


def effective_ollama_model() -> str:
    return _ollama_model_override or OLLAMA_MODEL

_DATA_DIR = Path(os.getenv("DB_PATH", "/data/world.db")).parent
_CUSTOM_MODELS_FILE = _DATA_DIR / "ai_models.json"

KNOWN_MODELS = [
    {"id": "gemma4:26b", "label": "Gemma 4 26B"},
    {
        "id": "hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf",
        "label": "Gemma 4 26B MXFP4",
    },
]


def _client() -> _ollama.AsyncClient:
    return _ollama.AsyncClient(host=effective_ollama_url())


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_data() -> dict:
    try:
        return _json.loads(_CUSTOM_MODELS_FILE.read_text())
    except Exception:
        return {"custom": [], "hidden": []}


def _save_data(data: dict) -> None:
    _CUSTOM_MODELS_FILE.write_text(_json.dumps(data, indent=2))


def load_custom_models() -> list[dict]:
    return _load_data().get("custom", [])


def load_hidden_ids() -> set:
    return set(_load_data().get("hidden", []))


def save_custom_models(models: list[dict]) -> None:
    data = _load_data()
    data["custom"] = models
    _save_data(data)


def hide_builtin(model_id: str) -> None:
    data = _load_data()
    if model_id not in data.setdefault("hidden", []):
        data["hidden"].append(model_id)
    _save_data(data)


def unhide_builtin(model_id: str) -> None:
    data = _load_data()
    data["hidden"] = [i for i in data.get("hidden", []) if i != model_id]
    _save_data(data)


def reset_hidden() -> None:
    data = _load_data()
    data["hidden"] = []
    _save_data(data)


def all_models() -> list[dict]:
    hidden = load_hidden_ids()
    custom = load_custom_models()
    seen = {m["id"] for m in KNOWN_MODELS}
    visible_builtins = [m for m in KNOWN_MODELS if m["id"] not in hidden]
    extra = [m for m in custom if m["id"] not in seen and m["id"] not in hidden]
    return visible_builtins + extra


# ── Model resolution ──────────────────────────────────────────────────────────

async def _list_loaded() -> list[str]:
    try:
        resp = await _client().list()
        return [m.model for m in resp.models]
    except Exception:
        return []


async def resolve_model(requested: str) -> str:
    target = requested or effective_ollama_model()
    available = await _list_loaded()
    if not available:
        return target
    if target in available:
        _log.info("resolve_model exact: %s", target)
        return target
    tl = target.lower()
    for a in available:
        al = a.lower()
        if tl == al or tl in al or al in tl:
            _log.info("resolve_model %r → %r", target, a)
            return a
    _log.warning("resolve_model no match for %r, using %r", target, available[0])
    return available[0]


# ── Chat functions ────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a creative fantasy world-building assistant. "
    "Write vivid, immersive lore. Be concise but evocative. "
    "Keep it under 200 words."
)


async def generate_chat(messages: list[dict], system: str = "", model: str = "") -> str:
    m = model or effective_ollama_model()
    _log.info("generate_chat model=%s msgs=%d", m, len(messages))
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        resp = await _client().chat(model=m, messages=full)
        content = resp.message.content
        return content if content else "[empty response]"
    except _ollama.ResponseError as exc:
        _log.error("generate_chat Ollama error: %s %s", exc.status_code, exc.error)
        return f"[AI error: Ollama {exc.status_code}: {exc.error}]"
    except Exception as exc:
        _log.error("generate_chat unavailable: %s: %s", type(exc).__name__, exc)
        return f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def stream_chat(messages: list[dict], system: str = "", model: str = "") -> AsyncGenerator[str, None]:
    m = model or effective_ollama_model()
    _log.info("stream_chat model=%s msgs=%d", m, len(messages))
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    try:
        async for chunk in await _client().chat(model=m, messages=full, stream=True):
            token = chunk.message.content
            if token:
                yield token
    except _ollama.ResponseError as exc:
        _log.error("stream_chat Ollama error: %s %s", exc.status_code, exc.error)
        yield f"[AI error: Ollama {exc.status_code}: {exc.error}]"
    except Exception as exc:
        _log.error("stream_chat unavailable: %s: %s", type(exc).__name__, exc)
        yield f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def generate(prompt: str, system: str = _SYSTEM) -> str:
    return await generate_chat([{"role": "user", "content": prompt}], system)


async def status() -> dict:
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        return {"status": "ok", "model": effective_ollama_model(), "loaded_models": models}
    except Exception:
        return {"status": "unavailable", "model": effective_ollama_model()}


async def debug_info() -> dict:
    try:
        resp = await _client().list()
        models = [m.model for m in resp.models]
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": True,
            "loaded_models": models,
            "default_model": effective_ollama_model(),
        }
    except Exception as exc:
        return {
            "ollama_url": effective_ollama_url(),
            "ollama_reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
            "default_model": effective_ollama_model(),
        }


# ── Image generation ──────────────────────────────────────────────────────────

_IMAGEGEN_TYPE = os.environ.get("IMAGEGEN_TYPE", "").lower()   # "swarmui" or "comfyui"
_IMAGEGEN_URL  = os.environ.get("IMAGEGEN_URL", "").rstrip("/")

# Runtime overrides (set by /admin/imagegen/install without needing a restart)
_imagegen_type_override: str = ""
_imagegen_url_override:  str = ""


def _get_type() -> str:
    return _imagegen_type_override or _IMAGEGEN_TYPE


def _get_url() -> str:
    return (_imagegen_url_override or _IMAGEGEN_URL).rstrip("/")


def set_imagegen_override(itype: str, url: str) -> None:
    global _imagegen_type_override, _imagegen_url_override
    _imagegen_type_override = itype.lower()
    _imagegen_url_override  = url.rstrip("/")

_COMFYUI_WORKFLOW = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "{model}"}},
    "2": {"class_type": "CLIPTextEncode",         "inputs": {"text": "{prompt}",   "clip": ["1", 1]}},
    "3": {"class_type": "CLIPTextEncode",         "inputs": {"text": "{negative}", "clip": ["1", 1]}},
    "4": {"class_type": "EmptyLatentImage",        "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "5": {"class_type": "KSampler",               "inputs": {"model": ["1", 0], "positive": ["2", 0],
                                                              "negative": ["3", 0], "latent_image": ["4", 0],
                                                              "seed": 0, "steps": 20, "cfg": 7,
                                                              "sampler_name": "euler", "scheduler": "normal",
                                                              "denoise": 1.0}},
    "6": {"class_type": "VAEDecode",              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "SaveImage",              "inputs": {"images": ["6", 0], "filename_prefix": "ndworld"}},
}


async def _swarmui_session(u: str, c: _httpx.AsyncClient) -> str:
    try:
        r = await c.post(f"{u}/API/GetNewSession", json={})
        return r.json().get("session_id", "ndworld")
    except Exception:
        return "ndworld"


async def imagegen_status() -> dict:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return {"ok": False, "reason": "not configured"}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            if t == "swarmui":
                r = await c.post(f"{u}/API/GetNewSession", json={})
                return {"ok": r.status_code < 400, "type": t, "url": u}
            else:
                r = await c.get(f"{u}/system_stats")
            return {"ok": r.status_code < 400, "type": t, "url": u}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


async def imagegen_loras() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "LoRA", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/LoraLoader")
                data = r.json()
                return data["LoraLoader"]["input"]["required"]["lora_name"][0]
    except Exception:
        return []


async def imagegen_samplers_schedulers() -> dict:
    t, u = _get_type(), _get_url()
    if u:
        try:
            async with _httpx.AsyncClient(timeout=8) as c:
                if t == "comfyui":
                    r = await c.get(f"{u}/object_info/KSampler")
                else:
                    # SwarmUI proxies the ComfyUI API at /comfyui/
                    r = await c.get(f"{u}/comfyui/object_info/KSampler")
                data = r.json()
                req = data["KSampler"]["input"]["required"]
                return {
                    "samplers": req["sampler_name"][0],
                    "schedulers": req["scheduler"][0],
                }
        except Exception:
            pass
    return {
        "samplers": [
            # Euler family
            "euler", "euler_ancestral", "euler_cfg_pp", "euler_ancestral_cfg_pp",
            # Heun
            "heun", "heunpp2",
            # DPM-2
            "dpm_2", "dpm_2_ancestral",
            # LMS / DPM fast/adaptive
            "lms", "dpm_fast", "dpm_adaptive",
            # DPM++ family
            "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_sde_gpu",
            "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
            "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
            # DDPM / DDIM / LCM
            "ddpm", "ddim", "lcm",
            # UniPC
            "uni_pc", "uni_pc_bh2",
            # IPNDM
            "ipndm", "ipndm_v",
            # Misc newer samplers
            "deis", "res_multistep", "res_multistep_cfg_pp",
            "sa_solver", "er_sde", "gradient_estimation", "restart",
        ],
        "schedulers": [
            "normal", "karras", "exponential", "sgm_uniform",
            "simple", "ddim_uniform", "beta",
            "linear_quadratic", "kl_optimal", "ays", "gits",
        ],
    }


async def imagegen_models() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/CheckpointLoaderSimple")
                data = r.json()
                return data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except Exception:
        return []


async def imagegen_upscalers() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "Upscale", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                r = await c.get(f"{u}/object_info/UpscaleModelLoader")
                data = r.json()
                return data["UpscaleModelLoader"]["input"]["required"]["model_name"][0]
    except Exception:
        return []


async def imagegen_ipadapter_models() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "IPAdapter", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                return []
    except Exception:
        return []


async def imagegen_refiners() -> list:
    t, u = _get_type(), _get_url()
    if not t or not u:
        return []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            if t == "swarmui":
                sid = await _swarmui_session(u, c)
                r = await c.post(f"{u}/API/ListModels",
                                 json={"session_id": sid, "path": "Refiner", "depth": 10})
                data = r.json()
                return [m["name"] for m in data.get("files", []) if m.get("name")]
            else:
                return []
    except Exception:
        return []


async def imagegen_progress() -> dict:
    t, u = _get_type(), _get_url()
    if t != "swarmui":
        return {"step": 0, "total": 0, "preview": ""}
    try:
        async with _httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{u}/API/GetCurrentStatus", json={"session_id": "ndworld"})
            data = r.json()
            return {
                "step": data.get("current_step", 0),
                "total": data.get("total_steps", 0),
                "preview": data.get("preview", ""),
            }
    except Exception:
        return {"step": 0, "total": 0, "preview": ""}


async def imagegen_generate(prompt: str, negative: str, model: str,
                            width: int, height: int, steps: int,
                            cfg: float, seed: int, uploads_dir: Path,
                            sampler: str = "euler",
                            scheduler: str = "normal",
                            batch_size: int = 1,
                            loras: str = "",
                            lora_weights: str = "",
                            vae: str = "",
                            clip_skip: int = -1,
                            init_image: str = "",
                            init_strength: float = 0.6,
                            upscale_model: str = "",
                            upscale_factor: float = 1.0,
                            controlnet_image: str = "",
                            controlnet_strength: float = 0.8,
                            controlnet_preprocessor: str = "",
                            controlnet_model: str = "",
                            hiresfix: bool = False,
                            hireswidth: int = 0,
                            hiresheight: int = 0,
                            hiresdenoisestrength: float = 0.5,
                            hiressteps: int = 0,
                            refiner_model: str = "",
                            refiner_control: float = 0.8,
                            seamless_x: bool = False,
                            seamless_y: bool = False,
                            variation_seed: int = -1,
                            variation_strength: float = 0.0,
                            freeu_enabled: bool = False,
                            freeu_b1: float = 1.3,
                            freeu_b2: float = 1.4,
                            freeu_s1: float = 0.9,
                            freeu_s2: float = 0.2,
                            dynthresh_enabled: bool = False,
                            dynthresh_mimic_scale: float = 7.0,
                            dynthresh_percentile: float = 0.999,
                            cfg_rescale: float = 0.0,
                            ipadapter_image: str = "",
                            ipadapter_strength: float = 0.6,
                            ipadapter_model: str = "") -> list[str]:
    import copy, random, asyncio, base64 as _b64, uuid as _uuid
    t, u = _get_type(), _get_url()
    ai_img_dir = Path(uploads_dir) / "ai-images"
    ai_img_dir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []

    async with _httpx.AsyncClient(timeout=600) as c:
        if t == "swarmui":
            sr = await c.post(f"{u}/API/GetNewSession", json={})
            session_id = sr.json().get("session_id", "ndworld")
            model_name = model.rsplit(".", 1)[0] if model.endswith((".safetensors", ".ckpt", ".bin")) else model
            payload: dict = {
                "session_id": session_id,
                "images": max(1, min(batch_size, 8)),
                "prompt": prompt,
                "negativeprompt": negative,
                "model": model_name,
                "width": width,
                "height": height,
                "steps": steps,
                "cfgscale": cfg,
                "seed": seed if seed >= 0 else -1,
                "sampler": sampler or "euler",
                "scheduler": scheduler or "normal",
                "donotsave": False,
            }
            if loras:
                payload["loras"] = loras
                payload["loraweights"] = lora_weights or "1"
            if vae:
                payload["vae"] = vae
            if clip_skip > 0:
                payload["clipstop"] = -clip_skip
            if init_image:
                payload["initimage"] = init_image
                payload["initimagecreativity"] = init_strength
            if upscale_model:
                payload["upscalemodel"] = upscale_model
                payload["upscalemultiplier"] = upscale_factor
            if controlnet_image:
                payload["controlnetimage"] = controlnet_image
                payload["controlnetstrength"] = controlnet_strength
                if controlnet_preprocessor:
                    payload["controlnetpreprocessor"] = controlnet_preprocessor
                if controlnet_model:
                    payload["controlnetmodel"] = controlnet_model
            if hiresfix and hireswidth > 0:
                payload["hireswidth"] = hireswidth
                payload["hiresheight"] = hiresheight
                payload["hiresdenoisestrength"] = hiresdenoisestrength
                if hiressteps > 0:
                    payload["hiressteps"] = hiressteps
            if refiner_model:
                payload["refinermodel"] = refiner_model
                payload["refinercontrolpercentage"] = refiner_control
            if seamless_x:
                payload["seamlessx"] = True
            if seamless_y:
                payload["seamlessy"] = True
            if variation_seed >= 0 and variation_strength > 0:
                payload["variationseed"] = variation_seed
                payload["variationseedstrength"] = variation_strength
            if freeu_enabled:
                payload["freeu_b1"] = freeu_b1
                payload["freeu_b2"] = freeu_b2
                payload["freeu_s1"] = freeu_s1
                payload["freeu_s2"] = freeu_s2
            if dynthresh_enabled:
                payload["dynamicthresh_enabled"] = True
                payload["dynamicthresh_mimic_scale"] = dynthresh_mimic_scale
                payload["dynamicthresh_threshold_percentile"] = dynthresh_percentile
            if cfg_rescale > 0:
                payload["cfgrescale"] = cfg_rescale
            if ipadapter_image:
                payload["ipadapterimage"] = ipadapter_image
                payload["ipadapterstrength"] = ipadapter_strength
                if ipadapter_model:
                    payload["ipadaptermodel"] = ipadapter_model

            gr = await c.post(f"{u}/API/GenerateText2Image", json=payload)
            _log.info("SwarmUI generate status=%s body=%.400s", gr.status_code, gr.text)
            data = gr.json()
            images = data.get("images") or []
            if not images:
                err = data.get("error") or data.get("errorid") or data.get("message") or str(data)
                raise ValueError(f"SwarmUI returned no image: {err}")
            for img_raw in images:
                if img_raw.startswith("data:"):
                    img_raw = img_raw.split(",", 1)[1]
                fname = str(_uuid.uuid4()) + ".png"
                (ai_img_dir / fname).write_bytes(_b64.b64decode(img_raw))
                urls.append(f"/uploads/ai-images/{fname}")

        else:  # comfyui
            wf = copy.deepcopy(_COMFYUI_WORKFLOW)
            wf["1"]["inputs"]["ckpt_name"] = model
            wf["2"]["inputs"]["text"] = prompt
            wf["3"]["inputs"]["text"] = negative
            wf["4"]["inputs"].update({"width": width, "height": height})
            wf["5"]["inputs"].update({
                "steps": steps, "cfg": cfg,
                "seed": seed if seed >= 0 else random.randint(0, 2**32),
                "sampler_name": sampler or "euler",
                "scheduler": scheduler or "normal",
            })
            if upscale_model:
                wf["8"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": upscale_model}}
                wf["9"] = {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["8", 0], "image": ["6", 0]}}
                wf["7"]["inputs"]["images"] = ["9", 0]
            pr = await c.post(f"{u}/prompt", json={"prompt": wf})
            pid = pr.json()["prompt_id"]
            for _ in range(120):
                await asyncio.sleep(1)
                hr = await c.get(f"{u}/history/{pid}")
                hist = hr.json().get(pid, {})
                if hist.get("outputs"):
                    imgs = list(hist["outputs"].values())[0].get("images", [])
                    for img_info in imgs:
                        ir = await c.get(f"{u}/view",
                                         params={"filename": img_info["filename"],
                                                 "subfolder": img_info.get("subfolder", ""),
                                                 "type": "output"})
                        fname = str(_uuid.uuid4()) + ".png"
                        (ai_img_dir / fname).write_bytes(ir.content)
                        urls.append(f"/uploads/ai-images/{fname}")
                    break

    return urls
