# The gateway's MCP server (phase 5)

Behind `mcp` in `ND_RUST_ROUTES` (`docs/RUST_GATEWAY.md`). `POST /mcp` is answered by the gateway the way `app/mcp_server.py` (FastMCP,
streamable HTTP, stateless) answers it, byte for byte. Same rule as every other gateway route: what it is not sure of goes to Python
unchanged.

The plan named the official Rust SDK (`rmcp`). It is not used: an MCP client sees the server's own answers (the tool list, its schemas,
the error texts), and those must stay exactly what the Python SDK sends. The gateway speaks the small part of the protocol the stateless
server uses itself (`rust/nd-gateway/src/mcp.rs`, about 1,500 lines) and takes the tool list from the SDK.

## The transport

| Message | What the gateway does |
|---|---|
| Any `POST /mcp` | Checks what the SDK checks before reading the message: `Accept` naming both `application/json` and `text/event-stream`, a JSON `Content-Type`, an `mcp-protocol-version` header it knows (when there is one). Then the bearer token: the sha256 of the token in `api_tokens`, and the token's `last_used_at` stamped. The answer is one SSE `message` event (`event: message\r\ndata: {...}\r\n\r\n`, `cache-control: no-cache, no-transform`, `x-accel-buffering: no`). |
| `initialize` | The SDK's answer: the protocol version the client asked for when the server supports it (else the latest), the capabilities, `serverInfo` (`nd-world`, the `mcp` package version) and the server's instructions. |
| `notifications/initialized` | 202, no body. |
| `ping` | `{}`. |
| `tools/list` | The SDK's own serialization of the tool list, generated into `app_data.json` (`mcp.tools_list_json`) by `scripts/gen_rust_data.py`, so the schemas cannot drift. |
| `tools/call` | Every tool of `app/mcp_server.py` (below): its arguments taken as the tool's pydantic model would take them, the same checks in the same order, and the result as FastMCP shapes it - a list as one text block per item plus `structuredContent` `{"result": [...]}`, a dict as one block, a string as one block plus `{"result": ...}`, a failure as `Error executing tool <name>: <the exception's text>` with `isError`. |

Python's: a missing, malformed or unknown token (its 401s), a GET, a batch, a message with fields the SDK's models do not have, an unknown
method, `tools/list` with a cursor, an unknown tool, and an argument the SDK would coerce or refuse with a pydantic message (`"7"` or `true`
for an integer, a string holding JSON for a parameter not typed `str`, `null` for a plain string).

## The tools

| Tools | Notes | Stays Python's |
|---|---|---|
| `list_worlds`, `list_facts`, `list_quests`, `search_entities`, `get_entity`, `get_rules`, `list_sessions`, `get_session`, `list_tables`, `roll_table` | The GM / player boundary of the web UI: a world the token's user may reach, the Settings → Navigation section levels at the player tier (an MCP request never carries `is_assistant`), GM-only rows dropped for a player, `:::gm` blocks taken out of the rules, a session's recap only once published, and only the audio clips players may hear. `list_tables` counts entries with `json.loads`' own failures (CPython 3.13's messages); `roll_table` rolls weighted, a weight of 0 counting as 1. | A stored list Python reads its own way (a dict of table entries, a weight given as text), a failure of the FTS5 index |
| `create_fact`, `update_fact`, `delete_fact`, `create_entity`, `create_note`, `update_entity`, `delete_entity`, `create_quest`, `update_quest` | GM tokens only; the rows written as the ORM writes them (column defaults, `onupdate` stamps, and nothing at all when no value really changes); a fact delete stamps the world's recap watermark; an entity delete takes its relations and links with it. | Deleting an entity that has notes: the ORM tries to unlink them and fails with an IntegrityError naming a timestamp, which only Python can spell |
| `list_characters`, `get_character`, `list_parties`, `get_party`, `adjust_character_resource`, `set_character_conditions`, `award_character_xp`, `rest_party` | System-aware like the party page: each character's own vital, resources and conditions (`member_vitals`), its sheet as the markdown export (`[gmonly]` stripped for a player), XP awarded to a custom system's own XP fields, a Rest by each member's own rules (and the once-per-Rest feats given back). | A member list holding numeric text, a bool or a float (SQLite would match it to an id), a stored number Python keeps as a decimal |
| `list_sheet_templates`, `get_sheet_template`, `create_sheet_template`, `update_sheet_template` | GM tokens only; the summary's integrations from the effective system spec, the rules digest of a built-in system, and a GM's proposal cleaned by `template_draft` as the in-app drafter does. | A spec value Python measures its own way (`rest` given as a list) |
| `ask_chronicler` | The Chronicler's prompt for the token's user, and one non-streamed answer from the backend. | The legacy Ollama backend |

## Tests

`tests/test_gateway_mcp.py` sends the same JSON-RPC message to Python and to the gateway and compares status, content type and body: the
protocol messages and every tool, for a GM token and a player token; the write tools on two identical sets of rows (one per side) with
the rows compared afterwards; the cases left to Python answered the same way; and the MCP SDK's own client working end to end through the
gateway. The in-process suite (`tests/test_mcp.py`) still checks Python's side.
