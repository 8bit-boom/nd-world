"""AI Assist — the single shared engine behind every surface's ✨ AI panel.

Every editor surface (entity forms, notes, quests, random tables, rules,
parties, calendar, the dashboard's world summary) offers the same small
set of operations — expand a draft, polish what's written, summarize,
audit for consistency, suggest metadata, translate, or run a free-form GM
instruction. Rather than N surfaces × M ops of bespoke implementations,
each surface collects its fields, calls run_assist(op, ...), and renders
whatever comes back.

Split by output contract, mirroring app.ai's own two families:

- Free-text ops (expand/improve/summarize/analyze/translate/custom/
  rules_rewrite/world_summary) go through ai.generate_chat, so they NEVER
  raise on an Ollama-side failure — they return generate_chat's failure
  sentinel string instead, and the interactive route maps that to HTTP
  502 so a sentinel can never be silently pasted into an editor field.
- Structured ops (suggest/table_entries) use Ollama's JSON-schema
  `format` (the same pattern as ai.parse_facts_from_recap) and raise
  ValueError on any failure — the contract ai.parse_entity_from_text
  already established for schema-constrained calls.

The recap family's degeneration defenses apply to the free-text path
too: _clean_degenerate_recap strips leaked chat-template tokens and
collapses repetition artifacts, and _recap_num_predict_default_if_
unbounded caps an otherwise-unbounded generation so a looping model
can't spin forever inside an "improve" call. Thinking is opt-in per run
(the panel's 🧠 checkbox, default off) — these are short editorial ops,
not session recaps.

RAG world_context, when a caller passes it, is prepended via
ai._with_world_context — the same "canonical names win over the model's
own transliterations" framing every other RAG consumer uses.

No database access in this module — callers build context (fields,
world lore) and persist results. That keeps it testable with the same
fake-client monkeypatch pattern the rest of the AI layer uses.
"""
import json as _json
import logging

from . import ai as _ai_module

_log = logging.getLogger("nd.ai.assist")

# Free-text ops — result is displayable/insertable prose (or, for analyze,
# a findings list that is display-only by convention).
OP_EXPAND = "expand"
OP_IMPROVE = "improve"
OP_SUMMARIZE = "summarize"
OP_ANALYZE = "analyze"
OP_TRANSLATE = "translate"
OP_CUSTOM = "custom"
OP_RULES_REWRITE = "rules_rewrite"
# Internal-only op (the dashboard's world-summary widget; the job engine
# assembles the world state and hands it in as `content`). Accepted by
# run_assist but deliberately not offered by any editor panel.
OP_WORLD_SUMMARY = "world_summary"
FREE_TEXT_OPS = {
    OP_EXPAND, OP_IMPROVE, OP_SUMMARIZE, OP_ANALYZE, OP_TRANSLATE,
    OP_CUSTOM, OP_RULES_REWRITE, OP_WORLD_SUMMARY,
}

# Structured ops — result is JSON the surface applies field-by-field.
OP_SUGGEST = "suggest"
OP_TABLE_ENTRIES = "table_entries"
STRUCTURED_OPS = {OP_SUGGEST, OP_TABLE_ENTRIES}

ALL_OPS = FREE_TEXT_OPS | STRUCTURED_OPS

# The interactive route's input ceiling — a body bigger than this against
# a slow local model would turn POST /api/ai/assist into the same
# reverse-proxy ~100s-timeout trap (Cloudflare 524) that motivated
# background jobs for every other long AI op. The job route has no HTTP
# timeout to respect, so it accepts any size and is what the rules editor
# (whole-document rewrites) and other big-content surfaces use.
MAX_INTERACTIVE_CHARS = 60000

_BASE_SYSTEM = (
    "You are an editorial assistant inside a tabletop RPG world-building tool. "
    "You help the GM draft, polish, and audit campaign content (entities, notes, "
    "quests, rules, random tables). Write your answer in the same language as the "
    "input content. Respond with the requested output only — no preamble, no "
    "commentary about what you changed."
)

_EXPAND_SYSTEM = _BASE_SYSTEM + (
    " Expand the GM's rough draft into a rich, complete write-up in Markdown. "
    "Preserve every detail and name already present; add texture, structure and "
    "paragraph flow, but do not invent plot twists, relationships or outcomes "
    "that contradict the draft or the supplied world lore. Keep headings and "
    "lists where the draft already uses them."
)

_IMPROVE_SYSTEM = _BASE_SYSTEM + (
    " Rewrite the text: fix grammar and spelling, tighten wording, improve "
    "structure and flow. Preserve the meaning, tone, voice and every fact/name "
    "exactly — this is an edit, not a rewrite of the story. Keep the existing "
    "Markdown formatting. Output only the improved text."
)

_SUMMARIZE_SYSTEM = _BASE_SYSTEM + (
    " Write a concise summary of the text: one or two sentences, at most about "
    "40 words, capturing what a GM needs at a glance. Output only the summary."
)

_ANALYZE_SYSTEM = _BASE_SYSTEM + (
    " Audit the provided content as a critical editor. Report concrete findings "
    "as a Markdown bullet list, each bullet one specific observation: internal "
    "inconsistencies or contradictions (saying what contradicts what), gaps a GM "
    "would need filled before running it, connections to other entities implied "
    "by the supplied world lore but not stated, and tone/style problems. If the "
    "content is consistent and complete, say so briefly instead of inventing "
    "problems. Do not rewrite the content — findings only."
)

_TRANSLATE_SYSTEM = _BASE_SYSTEM + (
    " Translate the text into the target language. Preserve meaning, tone and "
    "Markdown formatting. Keep proper nouns unchanged unless the supplied world "
    "lore establishes a canonical translated name — use the lore's exact form "
    "when it does. Output only the translation."
)

_CUSTOM_SYSTEM = _BASE_SYSTEM + (
    " Follow the GM's instruction applied to the supplied content. If the "
    "instruction asks for edited text, output only the edited text; if it asks "
    "a question about the content, answer it directly."
)

_RULES_REWRITE_SYSTEM = _BASE_SYSTEM + (
    " You are editing the campaign's rules document — a large Markdown file. "
    "Apply the GM's instruction while preserving the document's heading "
    "structure, tables and formatting exactly where they are correct; change "
    "only what the instruction asks for. Output the complete edited text."
)

_WORLD_SUMMARY_SYSTEM = _BASE_SYSTEM + (
    " Summarize the current state of the campaign from the supplied world "
    "state digest: a few short paragraphs covering where the story stands, "
    "the important people and places, and the open threads a GM should keep "
    "in mind. Do not invent facts beyond the digest. Output only the summary."
)

_SUGGEST_SYSTEM = _BASE_SYSTEM + (
    " Suggest metadata for the given content: a one-sentence summary, a short "
    "comma-separated tag list (specific, reusable tags — places, factions, "
    "themes), a concise subtype label, and a folder path suggestion. If the "
    "metadata lists existing folders or tags, prefer fitting into them over "
    "inventing new ones. Respond with JSON only."
)

_TABLE_ENTRIES_SYSTEM = _BASE_SYSTEM + (
    " Generate rows for the described random table: varied, concrete, "
    "immediately usable results in the table's own language and tone, each a "
    "single phrase or sentence. Weights are 1 (rare) to 10 (common); leave most "
    "near the middle unless the theme clearly calls for a skew. Respond with "
    "JSON only."
)

_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "string"},
        "subtype": {"type": "string"},
        "folder": {"type": "string"},
    },
    "required": [],
}

_TABLE_ENTRIES_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "weight": {"type": "integer"},
                },
                "required": ["text"],
            },
        }
    },
    "required": ["entries"],
}


def compose_meta(fields: dict) -> str:
    """One 'Key: value' line per non-empty field — the compact context block
    every assist prompt carries ahead of the content itself (Kind, Name,
    current Summary, Tags, ...). Callers pass only what their surface has;
    empty/blank values are dropped rather than shipped as empty lines."""
    lines = []
    for key, value in (fields or {}).items():
        text = str(value or "").strip()
        if text:
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def _compose_user(meta: str, content: str, instruction: str = "") -> str:
    parts = []
    if instruction.strip():
        parts.append(f"GM instruction: {instruction.strip()}")
    if meta.strip():
        parts.append(meta.strip())
    if content.strip():
        parts.append(f"Content:\n{content.strip()}")
    return "\n\n".join(parts)


async def _free_text_call(system: str, user: str, model: str, think: bool) -> str:
    """One generate_chat call with the recap family's full defensive stack:
    thinking headroom when (and only when) the widening actually fired, the
    unbounded-generation cap, context sizing for oversized inputs, and a
    degeneration-artifact cleanup on the way out. Returns generate_chat's
    result verbatim on failure (its sentinel string) — never raises, same
    contract as every other generate_chat consumer."""
    m = model or _ai_module.effective_ollama_model()
    thinking_opts = _ai_module._thinking_num_predict_override(think)
    opts = dict(thinking_opts)
    _ai_module._recap_num_predict_default_if_unbounded(opts)
    reserve = _ai_module._CONTEXT_FIT_RESERVED_TOKENS + (
        _ai_module._THINKING_HEADROOM_TOKENS if thinking_opts else 0
    )
    opts.update(_ai_module._ctx_override_if_needed(system + user, reserve))
    result = await _ai_module.generate_chat(
        [{"role": "user", "content": user}], system=system, model=m,
        options=opts or None, think=think,
    )
    result, _truncated = _ai_module._clean_degenerate_recap(result)
    return result


async def _structured_call(system: str, user: str, schema: dict, model: str) -> dict:
    """One schema-constrained chat call — same shape as ai.parse_entity_
    from_text: ValueError on any Ollama/parse failure (the route maps it
    to a clean 4xx/5xx), parsed dict on success. Deliberately think=False:
    the JSON constraint binds the final content, and these are short
    metadata calls where reasoning buys nothing (the same reasoning the
    facts parser's default think=False rests on)."""
    m = model or _ai_module.effective_ollama_model()
    try:
        resp = await _ai_module._client().chat(
            model=m,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=schema,
            **(await _ai_module._chat_kwargs(model=m)),
        )
    except Exception as exc:
        raise ValueError(f"AI unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _json.loads(resp.message.content or "")
        if not isinstance(parsed, dict):
            raise ValueError("not a JSON object")
        return parsed
    except Exception as exc:
        raise ValueError("The model returned malformed JSON — try again or switch models.") from exc


def _require_content(op: str, content: str) -> None:
    if not (content or "").strip():
        raise ValueError(f"Nothing to work on — the '{op}' operation needs some content first.")


async def run_assist(
    op: str,
    *,
    content: str = "",
    meta: str = "",
    instruction: str = "",
    model: str = "",
    think: bool = False,
    world_context: str = "",
    lang: str = "",
) -> dict:
    """Run one assist operation. Returns:

        {"op": op, "mode": "text",  "text": str, "model": m}
        {"op": op, "mode": "data",  "data": dict, "model": m}

    Free-text ops never raise on an Ollama-side failure — `text` carries
    generate_chat's sentinel string and the CALLER checks
    ai.is_failure_sentinel (the interactive route 502s it; the job engine
    error-rows it, so a sentinel can never be cached as a done result).
    Invalid op / missing required input raises ValueError — caller maps
    to HTTP 400 / a job error row.

    `content` is the text the op targets (entity body, note text, rules
    markdown, world-state digest); `meta` is compose_meta()'s context
    block (Kind/Name/Tags/...); `instruction` is the GM's free-form
    steering — required for the custom op, doubles as the theme for
    table_entries and is ignored where it makes no sense; `lang` is the
    translate target (falling back to instruction, then English);
    `world_context` is optional RAG lore, framed by ai._with_world_context.
    """
    if op not in ALL_OPS:
        raise ValueError(f"Unknown AI assist operation: {op!r}")
    if op == OP_CUSTOM and not instruction.strip():
        raise ValueError("The custom operation needs an instruction — say what the AI should do.")

    m = model or _ai_module.effective_ollama_model()
    user = _compose_user(meta, content, instruction)

    if op in STRUCTURED_OPS:
        if op == OP_SUGGEST:
            if not user.strip():
                raise ValueError("Nothing to work on — add a name or some content first.")
            parsed = await _structured_call(
                _SUGGEST_SYSTEM, user or "(empty draft)", _SUGGEST_SCHEMA, m,
            )
            data = {k: str(parsed.get(k) or "").strip() for k in ("summary", "tags", "subtype", "folder")}
        else:  # OP_TABLE_ENTRIES
            if not user.strip():
                raise ValueError("Describe the table's theme (or fill in its name/description) first.")
            parsed = await _structured_call(
                _TABLE_ENTRIES_SYSTEM, user, _TABLE_ENTRIES_SCHEMA, m,
            )
            entries = []
            for row in (parsed.get("entries") or []):
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                try:
                    weight = max(1, min(10, int(row.get("weight") or 1)))
                except (TypeError, ValueError):
                    weight = 1
                entries.append({"text": text, "weight": weight})
            if not entries:
                raise ValueError("The model returned no usable table entries — try a more specific theme.")
            data = {"entries": entries}
        return {"op": op, "mode": "data", "data": data, "model": m}

    system_by_op = {
        OP_EXPAND: _EXPAND_SYSTEM,
        OP_IMPROVE: _IMPROVE_SYSTEM,
        OP_SUMMARIZE: _SUMMARIZE_SYSTEM,
        OP_ANALYZE: _ANALYZE_SYSTEM,
        OP_TRANSLATE: _TRANSLATE_SYSTEM,
        OP_CUSTOM: _CUSTOM_SYSTEM,
        OP_RULES_REWRITE: _RULES_REWRITE_SYSTEM,
        OP_WORLD_SUMMARY: _WORLD_SUMMARY_SYSTEM,
    }
    system = system_by_op[op]
    if op == OP_TRANSLATE:
        target = (lang or instruction or "English").strip()
        system = f"{system}\nTarget language: {target}."
        # For translate the instruction box holds the language, not a task —
        # don't ship it twice ("GM instruction: Spanish" reads as noise).
        user = _compose_user(meta, content)
    if op in (OP_EXPAND, OP_IMPROVE, OP_SUMMARIZE, OP_TRANSLATE, OP_RULES_REWRITE, OP_WORLD_SUMMARY):
        _require_content(op, content)
    elif op in (OP_ANALYZE,) and not user.strip():
        raise ValueError("Nothing to analyze — add a name or some content first.")
    system = _ai_module._with_world_context(system, world_context)
    text = await _free_text_call(system, user or "(empty draft)", m, think)
    return {"op": op, "mode": "text", "text": text, "model": m}
