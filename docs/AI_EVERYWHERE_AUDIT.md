# AI-Everywhere Audit — where AI lives in nd-world, and the plan that made it live everywhere

Audit date: 2026-09-05 · Implemented in `feat/ai-everywhere`

This document is the audit the "implement AI into everything" request
started from: a complete inventory of which surfaces had AI, which had
none, the shared architecture chosen to close the gap, and the roadmap
for the surfaces still pending. It doubles as the maintainer's guide to
adding AI to the next surface (it is now a ~20-line drop-in, see §3).

---

## 1. The audit: surfaces × AI, before this work

### Surfaces WITH AI (kept, some upgraded here)

| Surface | What it had |
|---|---|
| Entity detail | "Ask AI / Talk as" chat panel (thinking, attachments, voice, background jobs) |
| Entity form | one "Generate with AI" body-drafter (entity-smart, RAG-fed) |
| Sessions (GM) | recap expand/condense/summarize-from-facts, audio→transcribe→recap jobs, live transcript summarize, publish-to-players, prep checklist, model/think/RAG pickers |
| Session Log (player) | published recap or facts-recap generation with pickers |
| Facts | recap→facts parser (model/think/RAG, background job, draft review) |
| AI chat page | chat + RAG transparency/pinning + presets + compact + image gen + model management + Whisper |
| Image Studio / imagegen | SwarmUI/ComfyUI generation with jobs |
| Chronicler | player-facing RAG oracle (visibility-filtered, cached) |
| King in Yellow | streamed play generation, library RAG |
| Background Jobs | monitor over every AI job |
| MCP server | 8 tools: facts CRUD, entity search, quest list, ask_chronicler |

### Surfaces WITHOUT AI (the gap)

Entities list · entity templates · **player characters / sheets** ·
races · professions · **maps / overlays / gallery** · **schematics** ·
**rules** · **random tables** · **quests** · combat tracker · parties ·
calendar · investigation boards · importer · dashboard · worlds/private
notes · dice · audio/video libraries · handouts · search.

## 2. The architecture chosen

One **shared assist engine + one shared panel**, not N bespoke features:

- **`app/ai_assist.py`** — a leaf module with an op registry:
  free-text ops (`expand`, `improve`, `summarize`, `analyze`,
  `translate`, `custom`, `rules_rewrite`, internal `world_summary`) that
  ride `generate_chat` with the recap family's full defensive stack
  (thinking headroom, num_predict caps, degeneration cleanup, sentinel
  detection), and structured ops (`suggest`, `table_entries`) that use
  Ollama's JSON-schema `format` exactly like the facts parser.
- **`POST /api/ai/assist`** (interactive, input-capped at 60k chars so
  it can never become a Cloudflare-524 trap) and **`POST/GET
  /api/ai/assist-job/{id}`** (durable `AudioJob` purpose `ai_assist` for
  big content — whole rules documents above all). GM + GM-Assistant tier.
- **`templates/_ai_assist_panel.html` + `static/js/ai-assist.js`** — one
  collapsible panel (op select, instruction box, model/think/RAG
  controls, result with Replace/Insert/Copy or per-field Apply buttons)
  that every editor embeds with a small per-page config.
- A new **"assist" default-model surface** on the AI page's Models tab,
  so quick editorial ops can pin a fast model while recaps keep the big
  one.
- **MCP as the agent path**: an external AI assistant (phone app, IDE
  agent, another LLM with a tool loop) can now audit/analyze/edit/create
  everything through 20 tools — entity full CRUD, notes, rules (with
  :::gm stripped for player tokens), sessions (publish-boundary aware),
  tables + rolls, quests CRUD, plus the original facts/chronicler set.

Guardrails carried over from the recap work by construction: failure
sentinels can never be pasted into an editor field (interactive route
502s them; job engine error-rows them), every op runs under the job
semaphore on the job path, nothing structured is applied without a
per-field Apply click, and assistant/player tiers follow the existing
allowlists.

## 3. What shipped where (Phase 1, this cycle)

| Surface | Ops | Path |
|---|---|---|
| Entity form (all kinds) | improve/expand/summarize/suggest/translate/custom + legacy generate | interactive |
| Entity detail | analyze (audit) + custom vs RAG lore | interactive |
| Entity notes & private notes | improve/expand/translate/custom | interactive |
| Races / professions forms | improve/expand/summarize/suggest/translate/custom | interactive |
| Quests form | expand (hook→details)/improve/summarize/translate/custom | interactive |
| Random tables | **table_entries** (theme → weighted rows, appended after review) | interactive |
| Rules editor | rules_rewrite/improve/summarize/custom | **job** (documents are big) |
| Party notes | improve/expand/translate/custom | interactive |
| Home welcome message | improve/expand/summarize/translate/custom | interactive |
| Dashboard | **world summary** card (job-cached until Regenerate) | job |
| MCP | 12 new tools (§2) | — |

## 4. Adding AI to the next surface (the recipe)

1. `{% import '_ai_assist_panel.html' as _aa %}` + `{{ _aa.ai_assist_panel('pid') }}`
   near the content field.
2. `<script src="/static/js/ai-assist.js"></script>` + one `ndAiAssist('pid', {...})`
   call: `ops`, `surface`, `contentSelector` (or `getContent` for
   non-field content), `metaSelectors`, optional `job: true` and
   `onData` for structured results.
3. That's it — the backend is surface-agnostic. Add tests only if you
   add a new **op** (engine-level), not a new surface.

Deliberately **not** wired, with reasons:

- **Calendar events** — title-only rows; no text body to assist on
  (would become meaningful if events gain descriptions).
- **Combat tracker** — live-initiative data, not prose; an "encounter
  recap" belongs to Sessions, which already has it.
- **Dice** — pure RNG; narrating rolls is a chat job, not a dice job.
- **Players** — assist is a content-editing tier (GM/assistant); players
  keep the Ask-AI panel and Chronicler.

## 5. Phase-2 roadmap (documented, not yet built)

| Surface | Idea | Building blocks that exist |
|---|---|---|
| Schematics | "describe the scene" → AI drafts SVG elements JSON; player-view aware | `docs/AI_SCHEMATIC_GUIDE.md` schema + importer + `parse_entity_from_text`-style schema calls |
| Maps | suggest overlay markers from entity mentions in session recaps | overlay JSON importer + RAG |
| Gallery | AI alt-text/tagging for uploaded images | image attachments already reach vision models via Ask-AI |
| Importer | paste arbitrary text → AI maps it to an entity_bulk import JSON | general importer + `parse_entity_from_text` |
| Search | semantic/meaning search beside FTS5 | retrieval.py is the single choke point for both |
| Investigation boards | "suggest connections" between board nodes | boards_generate.py's org-graph already walks entity_links |
| Player characters | backstory/notes assist; sheet-field suggestions | characters router + assist panel |
| Rules (deeper) | section-scoped rewrite + "ask the rules" chat with rules-RAG (rules_md is not yet a retrieval source — entities/notes/facts are) | rules_render section splitter |
| Entity templates | AI-draft field templates from an example statblock | template form + assist panel |

Each Phase-2 line should reuse §3's recipe — the point of the shared
engine is that none of them needs new backend architecture.

## 6. GPU note

The same cycle added full NVIDIA GPU (incl. **Tesla V100**) support
docs and wiring for the Ollama/Whisper side of the AI stack — see
[GPU_SETUP.md](GPU_SETUP.md). Summary: V100 is Volta (compute 7.0),
still supported by current Ollama with flash attention + q8_0 KV cache
working; the two traps are the driver 580 branch being the last for
Volta and CUDA 13 dropping Volta entirely — hence the image-pinning
guidance.

---

Related: [AI_ENTITY_GUIDE.md](AI_ENTITY_GUIDE.md) ·
[API_REFERENCE.md](API_REFERENCE.md) (§MCP Server, §AI) ·
[IMPROVEMENT_PLAN.md](../IMPROVEMENT_PLAN.md) (A1–A6, V1–V3)
