import os
import json as _json
import httpx
from collections.abc import AsyncGenerator

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

_SYSTEM = (
    "You are a creative fantasy world-building assistant. "
    "Write vivid, immersive lore. Be concise but evocative. "
    "Keep it under 200 words."
)


async def generate_chat(messages: list[dict], system: str = "") -> str:
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": full, "stream": False},
            )
            r.raise_for_status()
            msg = r.json()["message"]
            return msg.get("content") or msg.get("thinking", "[empty response]")
    except Exception as exc:
        return f"[AI unavailable: {type(exc).__name__}: {exc}]"


async def stream_chat(messages: list[dict], system: str = "") -> AsyncGenerator[str, None]:
    full = [{"role": "system", "content": system}] if system else []
    full.extend(messages)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/chat",
                json={"model": OLLAMA_MODEL, "messages": full, "stream": True},
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
