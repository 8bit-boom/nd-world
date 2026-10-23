# The gateway's AI routes (phase 5)

Behind `ai` in `ND_RUST_ROUTES` (`docs/RUST_GATEWAY.md`). Same rule as every other gateway route: it answers only what it is sure of,
and everything else goes to Python unchanged.

| Route | What the gateway does | Stays Python's |
|---|---|---|
| `POST /api/chronicler/ask` | The Chronicler: the system prompt `build_chronicler_system_prompt` builds (facts - a player's without GM-only ones -, the entities the question finds through FTS5 with the same column weights, the visibility filter and secret-only matches dropped, their excerpts picked by `best_matching_excerpt`, their notes, the GM's AI instructions), the same request to an Unsloth Studio backend (`/v1/models` to resolve the model, `/v1/chat/completions` streamed, Settings' generation options translated as `app/llm_client.py` does), and the same SSE answer: `note`, `thinking` (the backend's reasoning and a literal `<think>` block), `token`, `error` (Python's texts for a backend error, an error event mid-stream and an empty answer), `[DONE]`, a keep-alive comment every 12 s. A GM's own model choice (resolved against `/v1/models`) and Thinking choice (`thinking_predict` added) are honoured as Python does; a player's are ignored. A player's 3 s cooldown (429) and the 30 s answer cache are kept by the gateway. | The legacy Ollama backend (no Studio key), an `https://` backend address, a note stored as HTML, a failure of the FTS5 index (Python's ILIKE fallback) |

Code: `rust/nd-gateway/src/ai.rs` (the route, the backend client, the stream), `rust/nd-gateway/src/retrieval.rs` (`app/retrieval.py`'s entity
search, excerpts and context lines; `app/ai_instructions.py`).

Tests: `tests/test_gateway_ai.py` runs a fake Studio both Python and the gateway talk to. It checks that the requests reaching it (system
prompt, model, options, key) and the SSE answers are the same, for a GM and a player, from the cache too, and for backend errors, an
error mid-stream and empty answers.

Known differences: when the backend cannot be reached at all, the `error` text names the failure in the gateway's words, not httpx's
(`[AI unavailable: ConnectionError: ...]` either way). The cache and the cooldown live in the process that answers. With `ai` on, that
is the gateway for every request it serves, so a question answered by Python a moment earlier is not in the gateway's cache.
