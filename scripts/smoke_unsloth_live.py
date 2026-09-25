"""Live smoke test: app.ai's new Unsloth shim against the running Phase 0
spike container (nd-unsloth-phase0, port 8000)."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["UNSLOTH_URL"] = "http://127.0.0.1:8000"
os.environ["UNSLOTH_API_KEY"] = "sk-unsloth-d3ef7c6c2f4f7345a78ff86149137d1f"
os.environ["UNSLOTH_MODEL"] = "unsloth/gemma-4-26B-A4B-it-GGUF"
os.environ["OLLAMA_URL"] = ""  # ensure no legacy fallback

import app.ai as ai


async def main():
    print("backend:", ai.llm_backend_name(), "| model:", ai.effective_llm_model())
    assert ai.llm_backend_name() == "unsloth"

    # 1. Non-streamed chat, thinking explicitly off — the "list ordinary weapons" path.
    r = await ai.generate_chat(
        [{"role": "user", "content": "Reply with exactly: PONG"}],
    )
    print("non-stream:", repr(r[:120]))
    assert r.strip().startswith("PONG"), r
    assert not ai.is_failure_sentinel(r)

    # 2. Streaming chat with thinking on (reasoning must arrive as thinking).
    pieces, thinking = [], 0
    async for item in ai.stream_chat(
        [{"role": "user", "content": "What is 17+25? Think briefly, then answer with just the number."}],
        think=True, emit_thinking=True,
    ):
        if item["type"] == "content":
            pieces.append(item["text"])
        else:
            thinking += len(item["text"])
    answer = "".join(pieces)
    print("stream answer:", repr(answer[:120]), "| thinking chars:", thinking)
    assert "42" in answer, answer
    assert thinking > 0, "expected reasoning_content under emit_thinking"

    # 3. Status/list through the shim.
    s = await ai.status()
    print("status:", s)
    assert s["status"] == "ok"

    print("ALL LIVE SMOKE TESTS PASSED")


asyncio.run(main())
