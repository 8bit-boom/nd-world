import os
import json as _json
import httpx
from pathlib import Path
from collections.abc import AsyncGenerator

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

_DATA_DIR = Path(os.getenv("DB_PATH", "/data/world.db")).parent
_CUSTOM_MODELS_FILE = _DATA_DIR / "ai_models.json"

KNOWN_MODELS = [
    {"id": "gemma4:26b", "label": "Gemma 4 26B (default)"},
    {
        "id": "hf.co/noctrex/gemma-4-26B-A4B-it-MXFP4_MOE-GGUF:gemma-4-26B-A4B-it-MXFP4_MOE.gguf",
        "label": "Gemma 4 26B MXFP4",
    },
    {
        "id": "hf.co/noctrex/Qwen3.6-35B-A3B-MXFP4_MOE-GGUF:Qwen3.6-35B-A3B-MXFP4_MOE.gguf",
        "label": "Qwen 3.6 35B MXFP4",
    },
    {
        "id": "hf.co/unsloth/GLM-4.6V-Flash-GGUF:GLM-4.6V-Flash-UD-Q4_K_XL.gguf",
        "label": "GLM 4.6V Flash",
    },
    {
        "id": "hf.co/mistralai/Ministral-3-14B-Reasoning-2512-GGUF:Ministral-3-14B-Reasoning-2512-Q4_K_M.gguf",
        "label": "Ministral 3B Reasoning",
    },
    {
        "id": "hf.co/unsloth/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-GGUF:NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-UD-IQ4_NL_XL.gguf",
        "label": "Nemotron 30B Reasoning",
    },
]

def load_custom_models() -> list[dict]:
    try:
        return _json.loads(_CUSTOM_MODELS_FILE.read_text())
    except Exception:
        return []


def save_custom_models(models: list[dict]) -> None:
    _CUSTOM_MODELS_FILE.write_text(_json.dumps(models, indent=2))


def all_models() -> list[dict]:
    seen = {m["id"] for m in KNOWN_MODELS}
    return KNOWN_MODELS + [m for m in load_custom_models() if m["id"] not in seen]


_SYSTEM = (
    "You are a creative fantasy world-building assistant. "
    "Write vivid, immersive lore. Be concise but evocative. "
    "Keep it under 200 words."
)


async def generate_chat(messages: list[dict], system: str = "", model: str = "") -> str:
    m = model or OLLAMA_MODEL
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": m, "messages": full, "stream": False},
            )
            r.raise_for_status()
            msg = r.json()["message"]
            return msg.get("content") or msg.get("thinking", "[empty response]")
    except Exception as exc:
        return f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def stream_chat(messages: list[dict], system: str = "", model: str = "") -> AsyncGenerator[str, None]:
    m = model or OLLAMA_MODEL
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/chat",
                json={"model": m, "messages": full, "stream": True},
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    chunk = _json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if chunk.get("done"):
                        break
    except Exception as exc:
        yield f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def generate(prompt: str, system: str = _SYSTEM) -> str:
    return await generate_chat([{"role": "user", "content": prompt}], system)


async def status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            return {"status": "ok", "model": OLLAMA_MODEL, "loaded_models": models}
    except Exception:
        return {"status": "unavailable", "model": OLLAMA_MODEL}
