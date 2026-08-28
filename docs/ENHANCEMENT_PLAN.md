# nd-world Enhancement Plan

Produced by a full-codebase audit (test suite state at the time: 1719 passed, 2 skipped).
Organized by the areas requested — AI, UI, convenience, speed, accessibility, entity
management, security, bug fixes — each item names concrete files and a rough size
(small/medium/large). See "Suggested execution order" at the end for the actual
implementation sequence; sections above it are grouped by topic, not priority.

Status legend used as waves are executed: ✅ done · 🚧 in progress · ⬜ not started.

---

## 1. AI — management, QoL, integration, speed, resources

**1.1 RAG retrieves entities but only feeds the model one line per entity — include body excerpts. (Medium; highest-value AI change)**
`_format_context_from_entities()` (`app/main.py:2463`) emits only `[kind] name (subtype): summary`. Retrieval (FTS5 over name/summary/**body**/tags) can *find* an entity by its body text, but the model then never sees that body — so "RAG" answers are built from ≤512-char summaries. Change: for the top-N retrieved entities (say the first 5), append a truncated `Entity.body` excerpt under a per-entity char budget (e.g. 1,200 chars each, total budget ~8k chars), keeping the one-liner form for the rest. Apply the same in `_build_rag_context()` (`app/audio_jobs.py:409`) and `build_chronicler_system_prompt()` (`app/routers/chronicler.py:59`). Tradeoff: bigger prompts — make the excerpt count/length two more fields on the existing RAG limit controls, defaulting conservatively.

**1.2 Chronicler retrieval is ILIKE-only; unify retrieval into one module. (Medium)**
`visible_entities()` in `app/routers/chronicler.py:35` duplicates the old ILIKE matcher because routers can't import from main.py. Extract `_find_relevant_entities`/`_find_relevant_entities_fts`/`_find_relevant_entities_ilike`/`_format_context_from_entities` (`app/main.py:2405-2472`) into a new `app/retrieval.py`, add an optional `user` param applying the `visible_to_players`/`entity_player_access` filter, and use it from main.py, chronicler.py, and audio_jobs.py.

**1.3 Background jobs have no concurrency limit — serialize per backend. (Medium)**
Two session recaps queued together interleave Whisper chunks and Ollama calls, doubling wall time and thrashing VRAM. Add module-level `asyncio.Semaphore`s: one for Whisper calls, one for job-scope Ollama work shared by `audio_jobs`/`chat_jobs`. Acquire *inside* the task so shutdown/checkpoint logic still sees pending jobs correctly.

**1.4 Unified Background Jobs page. (Medium)**
`background_jobs.html:42` hardcodes `BG_LIST_URL = '/api/audio-jobs'` — chat jobs and image jobs are only visible inside ai_chat.html's own panels. Render all three job types on the Background Jobs page.

**1.5 "recap" model surface. (Small)**
`DEFAULT_SURFACES = ("chat", "ask_ai", "image")` (`app/ai.py:193`) has no "recap" entry, so session recap/condense jobs fall back to the instance default unless re-picked every time. Add a fourth surface + selector.

**1.6 Handle the model-substitution `note` SSE event — see Bugs #1.**

**1.7 Cache `/api/ai/status`. (Small)** ~15s in-process cache; fold `/api/hover-preview/config` and `/api/spotlight` bootstrap into server-rendered context to drop fetches per page load.

**1.8 Attachment/document truncation is silent. (Small)** `_MAX_ATTACHMENT_TEXT_CHARS = 12000` truncates with no indication — surface `truncated: true` + original length.

**1.9 Player-facing LLM endpoints lack gating and caching (also Security #1). (Small)** `/api/chronicler/ask` and `POST /api/session-log/{id}/recap` invoke Ollama with no cooldown and no caching.

**1.10 Config sprawl — one map, fewer places. (Small doc; Medium if migrating storage)** AI config lives in four places (AppSettings, `/data/ai_models.json`, per-world World columns, PromptPreset). Add cross-links; verify `/admin/backup.zip` includes `ai_models.json`.

**1.11 "Recent notes" retrieval actually orders by name. (Small)** Order by `updated_at.desc()` instead.

---

## 2. UI

**2.1 Split `ai_chat.html` (5,597 lines). (Large, mechanical)** Extract shared JS (`nd-markdown.js`, per-tab JS files) and Jinja partials per tab. No behavior change.

**2.2 Players see GM-only controls on entity detail. (Small)** Wrap Edit/Delete in an `is_gm` check; gate Download link on `players_can_download_entities`; hide Ask AI panel when `players_can_ask_ai` is off.

**2.3 Replace the "Add connection" full-world dump with the picker. (Medium — also Speed #2)** `detail()` loads every entity into the template; use the existing entity-picker + paged search API instead.

**2.4 (folded into 1.4.)**

**2.5 Session list cards carry no content. (Small)** Add a summary excerpt + "has transcript"/"has recap" badges.

**2.6 Inline-style sprawl. (Medium, incremental)** Promote repeated `style="..."` patterns to CSS classes, opportunistically.

**2.7 Stale docs describing UI that changed. (Small)** README's entity-type count, SVG-upload claim (contradicts a real security decision — SVG is rejected), stat-name defaults; AGENTS.md's "no API token auth" claim (ApiToken/MCP bearer tokens exist).

---

## 3. Convenience / QoL

**3.1 Extend the bulk-action bar.** Add Set-visibility and Move-to-folder (visibility endpoint already exists).
**3.2 "Save & add another" on the entity form.** Second submit button redirecting back to `/new?kind=...`.
**3.3 Duplicate entity.** `POST /entity/{id}/duplicate`.
**3.4 Recap → entity linking assist. (Medium)** Deterministic exact-name-match scan of a saved summary against world entities, offered as one-click "check all mentioned" in the featured picker.
**3.5 Audio Library as a soundboard.** Loop toggle + pop-out mini-player.
**3.6 Live recording wake-lock.** `navigator.wakeLock.request('screen')` while recording.
**3.7 Player session-log recap caching (see 1.9) + paginate `session_log_list` for players.**

---

## 4. Speed

**4.1 `/kind/{kind}` folder views parse every entity body per request. (Medium)** LRU keyed by `(entity.id, entity.updated_at)` around `parse_stats`; paginate leaf-folder tables.
**4.2 Entity detail `all_entities` dump — see 2.3.** Also switch `q=` search to FTS with ILIKE fallback; cap `/search` results.
**4.3 Immutable caching for `/uploads`. (Small, real win)** `Cache-Control: public, max-age=31536000, immutable` for content-unique filenames; exclude `maps/` (stable names, replaced in place).
**4.4 Spotlight polling → in-memory cache.** Every open tab polls every 4s; cache invalidated by the two spotlight routes (single-process app).
**4.5 `_kinds_context_processor` cost.** Cache `AppSettings` in-process, invalidated on save.
**4.6 Gallery N+1 and unpaginated grid.** `selectinload(Entity.notes)`; batch rendering client-side past a few hundred images.
**4.7 Composite index.** `(world_id, kind)` on entities.
**4.8 Migration-healing maintenance burden. (Large; do deliberately)** ~580 hand-maintained lines in `database.py`; replace with a generic healer driven by `Base.metadata` diffing `PRAGMA table_info`. High payoff, needs careful test scaffolding against fixture DBs from several historical schemas.

---

## 5. Accessibility

- Dropdown menus: `aria-haspopup`/`aria-expanded`, close on Escape, focus return.
- Status-by-color-alone (`#ai-dot`): add visually-hidden text.
- Icon-only buttons (📎/🎤/🔒/⌕): add `aria-label` sweep.
- Lightbox: `role="dialog" aria-modal="true"`, focus management.
- Hover previews are mouse-only: mirror with `focusin`/`focusout`.
- Streaming chat: `aria-live="polite"` on the status line only (not per-token).
- Contrast audit: `.rel-sub`/`--text-dim` micro-labels are borderline/failing WCAG AA in places.
- Skip-to-content link before `<nav>`.

---

## 6. Entity creation & management, per type

Lore entities (NPC, location, org, creature, event, item, feat, note, race, profession, custom kinds): well-built (folders/tags/templates/visibility/AI-gen/backlinks/hover previews), but missing duplicate (3.3), save-and-new (3.2), bulk visibility/folder ops (3.1), a scalable connection picker (2.3), and automatic mention detection (a "Mentions" section via FTS query of the entity's name against other bodies would surface implicit links with no schema change). Also: no per-kind default field template.

Player characters: best-covered entity in the app (wizard, full sheet + custom templates, ownership, `.ndc`/Foundry export, mobile sync). Gap: no "retire PC → NPC entity" conversion.

Races/professions: adequate as-is.

Maps: not linked to location entities structurally; cheapest bridge is a `map_slug` custom-field convention + a "View map" button when set.

Schematics/handouts: rich (SVG canvas editor, combat linking, token movement, item pickup). No structural gaps found; flag `schematic-render.js` for an XSS-escaping review pass whenever it's next touched.

Audio/Video: solid (albums/nesting, chunked upload, AV1 conversion, poster gen). Missing bulk move/visibility across clips; video rows/files orphaned on world delete (Bug #2).

Sessions: the deepest workflow in the app (prep, loot/XP, live recording, transcribe+summarize with checkpoint/resume, condense, facts extraction, RAG pinning, player log). Gaps are all in sections 1/2/3 above (list density, recap caching, featured-entity assist, wake lock).

Notes: three distinct systems (`EntityNote`, `PrivateNote`, `Entity(kind=note)`) — coherent in code, confusing in UI since "note" means three things. Rename the `note` kind's label to "Lore Notes" + a one-line explainer; no schema change.

Rules: fine as-is.

Quests/parties/combat/calendar/tables/boards/facts: sensible CRUD; only small items (entity-picker reuse for quest links; no UI to reassign a Fact's `game_session_id`).

---

## 7. Security

Overall unusually careful for a self-hosted app (path containment on uploads, nh3 allowlisting, timing-equalized login, lockouts, TOTP + trusted devices, per-world membership gates, MCP GM gating). Remaining items:

**7.1 Ungated player LLM invocation + no rate limiting.** `/api/chronicler/ask`, `POST /api/session-log/{id}/recap` — see 1.9.
**7.2 Attachment job transcripts leak across users. (Small)** `GET /api/ai/attachments/audio-jobs` filters only by world+purpose, not by uploader — with `players_can_ask_ai` on, any player can read another user's voice-memo transcripts. Filter non-GM callers to their own `created_by_user_id`.
**7.3 Compose files publish unauthenticated AI services on 0.0.0.0.** Ollama/whisper/SwarmUI ports are LAN-exposed with no auth by default in `docker-compose.yml`/`truenas-compose.yml`; nd-world only needs them over the compose network. Default to `127.0.0.1:` bindings.
**7.4 State-changing GETs.** `GET /logout`, `GET /worlds/switch/{slug}` — convert logout to POST.
**7.5 Disk-DoS headroom for players. (Minor)** Player-uploaded AI attachments (up to 1GB audio) are never garbage-collected. Age-based sweep, or at minimum a disk-usage figure on Settings.
**7.6 `/uploads` is world-unscoped for any logged-in user.** Unguessable URLs are the real control (adequate for this threat model) — document it explicitly; optional: scope `ai_attachments/*` to GM+uploader.
**7.7 Doc drift with security implications** — same items as 2.7.

---

## 8. Bug fixes (verified in code)

1. **SSE `note` event renders as "undefined".** `/api/ai/stream` emits `{"note": ...}` on model substitution; both stream consumers (`ai_chat.html`, `entities/detail.html`) unconditionally append it as if it were a token. Guard `if (obj.note) {...continue}` / `if (obj.token === undefined) continue`.
2. **`world_delete` orphans video, facts, chat sessions, presets, and jobs.** The cleanup loop predates `VideoClip`/`VideoAlbum` (rows *and* files), `Fact`, `ChatSession`, `PromptPreset`, `AudioJob`, `ImageJob`, `ChatJob`.
3. **500s for a player with no world memberships.** `/search` and `/kind/{kind}` dereference `get_active_world()`'s possible `None` unguarded; `home()` already guards this case correctly.
4. **Unretrieved MCP task exception at shutdown.** Cosmetic (noisy in CI, harmless in prod) — the forever-task is never cancelled/observed at shutdown.
5. **Cross-world entity links.** `link()` doesn't check `src.world_id == tgt.world_id`.
6. **Doc drift** — same items as 2.7/7.7.

---

## Suggested execution order

**Wave 1 — quick, safe, high-value:**
Bugs #1/#3/#5 · Security 7.1/7.2 · Speed 4.3/4.4/1.7 · UI 2.2/2.5 · Doc pass (2.7+7.3)

**Wave 2 — correctness + resource management:**
Bug #2 (+ metadata-driven cleanup test) · AI 1.3 (job concurrency) · AI 1.4/1.5 · QoL 3.1/3.2/3.3

**Wave 3 — RAG quality (biggest AI payoff):**
AI 1.2 (`app/retrieval.py` extraction) · AI 1.1 (body excerpts) · 1.11 · 1.9 caching · Speed 4.2 (FTS everywhere)

**Wave 4 — structural refactors (dedicated PRs, no behavior change):**
UI 2.1 (split ai_chat.html) · UI 2.3 + Speed 4.1 (entity picker + parse_stats caching) · Speed 4.5/4.6/4.7

**Wave 5 — long-horizon:**
Speed 4.8 (metadata-driven migration healer) · Accessibility sweep · Remaining QoL (3.4/3.5/3.6, PC→NPC conversion)
