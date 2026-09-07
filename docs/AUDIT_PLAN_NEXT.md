# AUDIT_PLAN_NEXT.md — fresh code audit, September 2026

A read-only audit pass over the features that shipped after the earlier
`IMPROVEMENT_PLAN.md` / `docs/ENHANCEMENT_PLAN.md` / `docs/AI_EVERYWHERE_AUDIT.md`
work (all of which is already implemented — this document does not revisit it).
Focus areas: Pages + Character Sheets, the entity TOC/search, Chronicler SSE,
Calendar, Race/Profession catalogs, World Summary audience scoping, Code Assist,
and the Audio "Play for players" broadcast.

**How to use this file.** Each numbered item is independently implementable and
fully specified — pick them off top to bottom. Nothing here requires reading the
other items first. Every finding cites the file and line it is about (line
numbers are as of commit `4889fff`; if drift is suspected, grep for the quoted
function name instead).

Waves are rough priority, not hard dependencies:

| Wave | Theme | Items |
|------|-------|-------|
| 1 | Correctness + security bugs | 1–8 |
| 2 | Data integrity + performance | 9–14 |
| 3 | Code quality + test coverage | 15–19 |
| 4 | UX / product polish | 20–23 |

---

## Wave 1 — Correctness and security

### 1. Scope `/api/audio-jobs` by caller tier so GM-only job payloads stop leaking to GM-Assistants

`GET /api/audio-jobs` and `GET /api/audio-jobs/{job_id}` (`app/routers/audio_jobs.py:157`
and `:194`) are gated only by `_require_can_edit` (`:47`), which admits a
GM-Assistant — and `app/main.py:559-564` deliberately puts `/background-jobs`,
`/api/audio-jobs` and `/api/audio-jobs/*` in `_is_assistant_safe`. The list query
(`:177-188`) filters on `world_id` and optional `purpose`/`status`/`game_session_id`
only, and `_job_to_dict` (`app/routers/audio_jobs.py:57-133`) serializes
`transcript`, `recap` and `result_json` in full for every row. Two GM-only surfaces
leak straight through it:

- **World Summary.** `app/routers/ai.py:625-638`'s `_world_summary_audience_filter`
  exists specifically so an assistant is never served the GM's secrets-included
  digest (`AudioJob.audience == "gm"` or `""`). `GET /api/audio-jobs?purpose=world_summary`
  bypasses that filter entirely and returns `job.recap` verbatim; so does
  `GET /api/audio-jobs/{id}/recap.md` (`:232`).
- **Code Assist.** `app/routers/code_assist.py` is deliberately absent from both
  allowlists ("this touches the application's own source, not campaign content",
  module docstring lines 25-28). But `create_assist_job` (`app/audio_jobs.py:477-517`)
  stores the complete source file in `AudioJob.transcript` and the model's revised
  file in `result_json`, under `purpose="ai_assist"` with
  `filename="AI assist · code_assist"` — all of which `_job_to_dict` hands to an
  assistant, plus `GET /api/audio-jobs/{id}/transcript.md` (`:215`).

**Why it matters:** this silently defeats a security boundary the codebase went
out of its way to build. It is the highest-value fix in this document.

**Fix:** add a tier filter in `audio_jobs.py` applied by all four routes (list,
detail, `transcript.md`, `recap.md`). Compute `is_gm = bool(user and user.is_gm)`
once; for a non-GM caller, exclude `AudioJob.purpose == "world_summary"` rows whose
`audience` is not `"players"` (reuse `ai._world_summary_audience_filter` — importing
across routers is an established pattern, see AGENTS.md), and exclude
`purpose == "ai_assist"` rows whose `assist_params_json` carries
`op == "code_edit"`. Prefer filtering in SQL for `world_summary` (indexed column)
and post-filtering the small `ai_assist` set in Python. Add the matching rows to
`tests/test_gm_assistant.py`.

### 2. Clear the "Now Playing" broadcast when a clip's player visibility is revoked

`POST /audio/{clip_id}/play-for-players` (`app/routers/audio.py:569-594`) correctly
refuses to broadcast a clip that isn't `visible_to_players` (`:585-586`), and
`audio_delete` (`:541-559`) correctly calls `_clear_now_playing` when the
broadcasting clip is deleted. But `audio_edit` (`:479-499`) sets
`clip.visible_to_players = bool(visible_to_players)` at `:494` with no such check —
so a GM who broadcasts a clip and then un-ticks "visible to players" leaves
`World.now_playing_url` pointing at it. `GET /api/spotlight` (`app/main.py:4283-4321`)
keeps serving `audio_url` to every player, and `base.html`'s widget keeps playing
it. The GM has explicitly acted to hide the clip and the app keeps playing it at
the table.

**Why it matters:** it inverts the one guard the broadcast feature has, and it is
the exact "GM hides something and it stays visible" failure the
`visible_to_players` model exists to prevent.

**Fix:** in `audio_edit`, after assigning `clip.visible_to_players`, add
`if not clip.visible_to_players and world.now_playing_url == clip.file_url: _clear_now_playing(world)`
and clear `app.main._spotlight_cache` the same deferred-import way
`play-for-players`/`now-playing/stop` already do (`:592-593`, `:607-608`). Consider
also clearing when the clip's `file_url` changes. Add a test to
`tests/test_audio_broadcast.py` (it currently has no case for this — see item 18).

### 3. Apply player visibility filtering to `/races` and `/professions`

`races_page` (`app/routers/races.py:119-140`) queries
`db.query(Entity).filter(Entity.world_id == world.id, Entity.kind == "race")` at
`:127-129` with **no visibility filter at all**, and `professions_page`
(`app/routers/professions.py:122-142`, query at `:129-131`) does exactly the same.
Both `/races` and `/professions` are player-safe GETs (`app/main.py:411`). The
template makes it concrete: `app/templates/races.html:203-210` inlines
`body_html: {{ (e.body | md if e.body else '') | tojson }}` into a JS
`ENTITY_RACES` object for **every** race entity in the world, so a player who
opens `/races` and reads the page source gets the full rendered body of races the
GM marked `visible_to_players = False`. Every other entity list route routes its
query through `app/main.py:840-852`'s `_filter_visible_entities`.

**Why it matters:** a straightforward GM-only-content-in-a-player-safe-response
leak on two pages players are explicitly invited to browse.

**Fix:** `_filter_visible_entities` lives in `main.py`, which imports these
routers, so it cannot be imported back. Either (a) move `_filter_visible_entities`
into `app/deps.py` and have `main.py` re-export it (cleanest — several other
routers would benefit), or (b) inline the same 6-line filter in both routers. Then
wrap both queries. Add coverage to `tests/test_races.py` and
`tests/test_professions.py`: a hidden race must not appear in `world_by_tier`, nor
its body in the response text, for a player session.

### 4. World-scope the calendar event/icon delete routes and drop the hardcoded `world_id = 1` fallback

`calendar_event_delete` (`app/routers/calendar.py:356-363`) looks up
`db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()` with **no
`world_id` check** and deletes it. `calendar_day_icon_delete` (`:419-427`) is
identical, and additionally calls `_delete_icon_file(icon)` — deleting the file
off disk. Neither route even takes `active_world`. A GM-Assistant scoped to world
A can therefore delete any calendar event or day icon in world B by id. Every
other router in the codebase uses a `_x_or_404(db, world.id, id)` helper for
exactly this.

Relatedly, six handlers in this file use `world_id = world.id if world else 1`
(`:195`, `:286`, `:298`, `:338`, `:369`, `:390`) — a hardcoded write into world 1
whenever `get_world_ctx` returns nothing. Also, `calendar_view:202-203` does
`int(request.query_params.get("year", cur_year))` and the same for `month` with no
guard, so `/calendar?year=x` is an unhandled `ValueError` → HTTP 500;
`calendar_config_save:303` has the same problem on `current_day`.

**Why it matters:** a cross-world destructive write reachable by a lower-privilege
role, plus two trivially reachable 500s.

**Fix:** add `_event_or_404(db, world_id, event_id)` and
`_icon_or_404(db, world_id, icon_id)` helpers mirroring
`app/routers/audio.py:118-129`, give both delete routes the
`request`/`active_world` params and use the helpers. Replace `if world else 1`
with `if not world: raise HTTPException(404)`. Wrap the `year`/`month`/`current_day`
parses in a small `_safe_int(value, default)` helper. Add a cross-world delete
case to `tests/test_calendar.py` and `tests/test_cross_world.py`.

### 5. De-duplicate heading slugs in `_rules_toc` so the entity TOC anchors work

`_rules_toc` (`app/main.py:727-756`) builds each heading id as
`slug = re.sub(r'[^\w]+', '-', text.lower()).strip('-') or 'sec'` (`:752`) with no
uniqueness pass. Two headings with the same text produce the same `id`, so the
second TOC link scrolls to the first heading. This was tolerable when `_rules_toc`
only ran over the Rules document; the entity detail route now calls it with
`levels="123"` (`app/main.py:4355-4358`) precisely for long GM-authored documents
with `# Part I` chapters and repeated `## Overview` / `## Hooks` subsections
underneath — the exact shape that collides. Worse, `split_rules_sections`
(`app/rules_render.py:292`) splits on those same ids and
`app/templates/entities/detail.html:79` emits them as `data-section-id`, so the
rules-overlay targeting documented in `docs/RULES_OVERLAY.md` also mis-targets the
first of any duplicate pair.

**Why it matters:** the sidebar TOC is the entire point of the feature, and it is
broken on exactly the documents the feature was built for.

**Fix:** keep a `seen: dict[str, int]` inside `_rules_toc`'s closure; on a
collision emit `f"{slug}-{n}"` (incrementing) and use that in both the emitted
`<h{lvl} id=...>` and the `toc` entry, so anchors and section ids stay in
agreement. Because existing rules overlays may reference old ids, only the *second
and later* occurrence should get a suffix — the first keeps the bare slug, which
preserves every currently-working overlay reference. Add a case to
`tests/test_entity_detail_toc.py` and `tests/test_rules_render.py`.

### 6. Stop deleting a sheet template out from under filled player character sheets

`CharacterSheet.template_id` is a non-null FK to `page_docs.id`
(`app/models.py:893`), but `pages_delete` (`app/routers/pages.py:450-461`) and
`pages_album_delete` (`:296-317`) delete a `PageDoc` — and `_delete_doc_file`
(`:135-144`) removes its file from disk — with no check for referencing
`CharacterSheet` rows. SQLite FK enforcement is off (no `PRAGMA foreign_keys=ON`
anywhere in `app/database.py`; only `journal_mode`/`busy_timeout`/`synchronous`
are set at `:420-426`), so the delete succeeds silently. Afterwards
`character_sheet_render` (`app/routers/character_sheets.py:226-236`) 404s on
`if not doc`, and so does `character_sheet_download` (`:284-297`). The player's
filled-in data still exists in `CharacterSheet.data_json` but there is no route in
the app that can render or export it — `character_sheet_edit`'s template says
"Based on **a deleted template**" and shows a dead iframe.

**Why it matters:** silent, unrecoverable loss of player-authored data caused by a
routine GM action (tidying up the Pages library), with no warning at delete time.

**Fix:** in `pages_delete` and `pages_album_delete`, count referencing
`CharacterSheet` rows before deleting. If any exist, either refuse with a clear
`HTTPException(400, "N player character sheets are built from this template — delete those first")`,
or (preferable UX) keep the `PageDoc` row and its file and only allow deletion once
the sheets are gone. Surface the count in `app/templates/pages_library.html`
next to the `🧬 sheet template` tag (`:112`) so the GM can see it before clicking.
Add a test to `tests/test_character_sheets.py`.

### 7. Cap how many character sheets a player can create

`POST /pages/sheets/new` (`app/routers/character_sheets.py:175-201`) is
player-writable (`app/main.py:384-390`) and creates a `CharacterSheet` row on
every call with no count check of any kind — the module has `_MAX_NAME` and
`_MAX_SHEET_DATA_BYTES` (`:37-42`) but no `_MAX_SHEETS_*`. Every other
content-creating route in the app has a per-world ceiling:
`_MAX_DOCS_PER_WORLD = 200` (`app/routers/pages.py:43`),
`_MAX_CLIPS_PER_WORLD = 300` (`app/routers/audio.py:37`),
`_MAX_ALBUMS_PER_WORLD = 100`. Combined with the 256 KB
`_MAX_SHEET_DATA_BYTES` per save, a scripted (or just stuck-retry) client can grow
the SQLite file without bound. The "＋ New sheet from this" button in
`app/templates/pages_library.html:119-123` is rendered for players with no
confirm step, so accidental duplicates are likely even without malice.

**Why it matters:** it is the only player-writable create route in the app with no
ceiling, on a single-file SQLite database with no quota story.

**Fix:** add `_MAX_SHEETS_PER_PLAYER = 50` (per owner, per world) and
`_MAX_SHEETS_PER_WORLD = 500`, checked in `character_sheets_new` before
`db.add(sheet)`, raising `HTTPException(400, ...)` with a message naming the limit
— exactly the shape `pages_upload` uses at `app/routers/pages.py:339-340`. Add a
test to `tests/test_character_sheets.py`.

### 8. Detect a truncated Code Assist response instead of rendering it as a deletion diff

`OP_CODE_EDIT` asks the model for the **complete revised file**
(`app/ai_assist.py:180-193`), and `code_assist_status`
(`app/routers/code_assist.py:143-185`) diffs whatever comes back against
`job.transcript` with `difflib.unified_diff` (`:178-181`) and returns it with no
sanity check. `_free_text_call` (`app/ai_assist.py:265-285`) caps generation via
`_recap_num_predict_default_if_unbounded`, and `_clean_degenerate_recap` will
truncate a looping response — so a short or truncated `revised` is a normal,
expected outcome, not an exotic one. `_MAX_FILE_BYTES = 300_000`
(`app/routers/code_assist.py:56`) admits `app/main.py` (259 KB, 5349 lines) and
`app/ai.py` (245 KB), files no local coding model will faithfully re-emit in full.
The result is a clean-looking unified diff whose tail is thousands of `-` lines,
presented in the UI with no warning that the model simply stopped writing.

**Why it matters:** the panel's entire safety story is "a GM reviews the diff and
applies it themselves". A diff that silently proposes deleting half of `main.py`
undermines that, and the GM's Copy-diff button makes acting on it one click away.

**Fix:** in `code_assist_status`, before building the diff, compute a
completeness heuristic and attach it to the response — e.g.
`revised_ratio = len(revised) / max(1, len(original))`, plus a check that the
revised text's last non-blank line isn't mid-token. When
`revised_ratio < 0.9` (tunable constant), return an extra
`"truncation_warning": "The model returned only N% of the file — it likely ran out
of output budget. Try a smaller file or a model with more headroom."` and render
it prominently in `app/templates/code_assist.html` above the diff (`:45`), with
the Copy-diff button visually de-emphasized. Separately, lower `_MAX_FILE_BYTES`
to something a local model can actually round-trip (~80 KB) and make the
over-cap error message at `:130-131` say *why*. Add cases to
`tests/test_code_assist.py`.

---

## Wave 2 — Data integrity and performance

### 9. Add the newer tables to `_heal_table_from_model`'s list

`app/database.py:917-932` heals a fixed list of tables on every boot:
`combat_sessions, parties, quests, game_sessions, world_calendars, calendar_events,
image_albums, audio_jobs, image_jobs, chat_jobs, chat_sessions, prompt_presets,
page_docs, audio_clips, video_clips`. Missing: **`character_sheets`**,
`page_albums`, `audio_albums`, `video_albums`, `calendar_day_icons`,
`dice_rolls`. `Base.metadata.create_all()` covers a brand-new table on a fresh
install, but per AGENTS.md a new *column* on an existing table needs migration
code — and `character_sheets` is the newest, least-settled table in the schema
(`app/models.py:877-905`), the single most likely place to gain a column next.
Because `_heal_table_from_model` derives its column/FK/index list from the model
itself (`:192-205`), adding a table name to this tuple is free and eliminates the
drift risk permanently.

**Why it matters:** the next column added to `CharacterSheet` will work on the
developer's fresh DB and crash every existing install with
`no such column`, with no obvious cause.

**Fix:** append `"character_sheets", "page_albums", "audio_albums",
"video_albums", "calendar_day_icons", "dice_rolls"` to the tuple at
`app/database.py:925-931`. Verify against `tests/test_heal_table.py` and
`tests/test_migration.py`, and add an assertion there that every table backing a
model in `app.main._WORLD_DELETE_MODELS` appears in this list — a durable guard
against the same omission recurring.

### 10. Null out `CharacterSheet.player_character_id` when its PlayerCharacter is deleted

`character_delete` (`app/routers/characters.py:689-698`) does `db.delete(pc)` with
no cleanup, and `character_retire_to_npc` (`:737`) deletes the PC too. With FK
enforcement off (see item 6), any `CharacterSheet` rows pointing at that PC keep a
dangling `player_character_id` (`app/models.py:900`). The detail route already
knows these rows exist — `app/routers/characters.py:534` queries
`linked_sheets` for display. Downstream this degrades gracefully rather than
crashing (`character_sheets.py:218`'s `db.get` returns `None`, and the template
renders no link), so this is data hygiene rather than a live bug — but it silently
breaks the "🔗 linked to a character" indicator in
`app/templates/character_sheets_list.html:21` and `:39`, which keys off
`s.player_character_id` being truthy without resolving it.

**Why it matters:** the list page shows a link badge for a character that no
longer exists, and the dangling row would become a hard failure the day anyone
turns on `PRAGMA foreign_keys`.

**Fix:** in both `character_delete` and `character_retire_to_npc`, before
`db.delete(pc)`, run
`db.query(CharacterSheet).filter(CharacterSheet.player_character_id == pc.id).update({"player_character_id": None}, synchronize_session=False)`.
While there, change the template's badge condition to resolve the name (see item
21). Add a test to `tests/test_character_ownership.py`.

### 11. Replace the per-album COUNT loops with one GROUP BY (three routers)

`_sub_album_counts` and `_clip_counts`/`_doc_counts` each run **one query per
album**, in a Python `for` loop:

- `app/routers/audio.py:195-208`
- `app/routers/video.py:198-211`
- `app/routers/pages.py:154-167`

Every `/audio`, `/audio/albums/{id}`, `/video`, `/video/albums/{id}`, `/pages` and
`/pages/albums/{id}` render therefore issues `2 × len(albums)` extra queries — up
to 200 with `_MAX_ALBUMS_PER_WORLD = 100`. `tests/test_gallery_n_plus_1.py` shows
this codebase already treats this class of problem as worth fixing on the gallery.

**Why it matters:** it is the single largest avoidable query cost on the three
library pages, all of which are player-facing.

**Fix:** rewrite each pair as a single grouped query, e.g.
`db.query(AudioClip.album_id, func.count(AudioClip.id)).filter(AudioClip.album_id.in_(album_ids))`
(plus the `visible_to_players` filter for non-GMs) `.group_by(AudioClip.album_id)`,
then `dict(rows)` with `.get(aid, 0)` at the call site. Best done together with
item 15 (extract the helpers to a shared module and fix them once). Extend
`tests/test_gallery_n_plus_1.py`'s query-count assertion pattern to `/audio`,
`/video` and `/pages`.

### 12. Fix the calendar month view's N+1 relationship loads and unbounded picker lists

`calendar_view` (`app/routers/calendar.py:192-280`) builds `events_by_day` at
`:219-227` by touching `e.entity.name`, `e.session.session_num`/`.title`,
`e.character.name` and `e.party.name` — four lazily-loaded relationships per
event, so a month with 40 events issues up to 160 extra SELECTs. Separately,
`:257-270` loads **every** `Entity`, `GameSession`, `PlayerCharacter` and `Party`
in the world into four lists purely to populate the "link this event to…"
dropdowns, on every month page view, with no limit. On a mature campaign the
entity list alone can be thousands of rows serialized into the template context.
Also, `CalendarEvent.day` (`app/models.py:1426`) has no index even though the view
range-queries it every render — its sibling `CalendarDayIcon.day`
(`app/models.py:1452`) does have one, so this is an inconsistency, not a design
choice.

**Why it matters:** the calendar is a page a GM leaves open during play; it is
currently the slowest non-AI page in the app on a large world.

**Fix:** (a) add
`.options(joinedload(CalendarEvent.entity), joinedload(CalendarEvent.session), joinedload(CalendarEvent.character), joinedload(CalendarEvent.party))`
to the query at `:215-217`; (b) replace the four eager picker lists with the
existing `/api/entities/picker` endpoint (already player-safe, `app/main.py:337`)
loaded on demand when the event dialog opens, or at minimum add
`.limit(500)` with a "type to search" note; (c) add `index=True` to
`CalendarEvent.day` and let `_heal_table_from_model` create it (`calendar_events`
is already in the heal list at `app/database.py:926`).

### 13. Pause the two 4-second pollers when the tab is hidden

`app/templates/base.html:541` runs `setInterval(pollSpotlight, 4000)` on **every
page**, for every logged-in user, unconditionally — including in a backgrounded
tab left open for hours. `app/templates/schematic_view.html:269` runs
`setInterval(poll, 4000)` on the battle-map view, fetching
`/maps/schematic/{slug}/view.json` (a full elements + party-pins + combat-state
payload) on the same cadence. Neither checks `document.visibilityState`. A GM with
five tabs open overnight is issuing ~4,500 requests/hour against a single-worker
SQLite app for state that changed zero times. The spotlight side is partially
mitigated by `_SPOTLIGHT_CACHE_TTL = 2.0` (`app/main.py:4279`), which halves the
DB work but not the request volume or the auth-gate membership query that
`auth_gate` runs per non-GM request (`app/main.py:675`).

**Why it matters:** it is pure waste on a self-hosted box (often a TrueNAS jail
sharing a spinning disk), and it grows linearly with open tabs.

**Fix:** wrap both pollers so a hidden tab skips its tick —
`if (document.hidden) return;` at the top of `pollSpotlight` (`app/templates/base.html:520`) / `poll` — and add a
`document.addEventListener('visibilitychange', ...)` that fires one immediate poll
on becoming visible, so returning to a tab re-syncs instantly rather than up to 4 s
later. Consider extracting a tiny shared `ndPoll(fn, ms)` helper into
`static/js/` since both call sites want identical behavior. No server change
needed.

### 14. Cache the built-in race/profession markdown catalogs

`_load_builtin_races` (`app/routers/races.py:70-116`) reads all 17 files under
`app/races/**/*.md` off disk, runs two regex passes and a full `render_md` on each,
on **every** request. `_load_builtin_professions`
(`app/routers/professions.py:71-118`) does the same for `app/professions/`. It is
called by `races_page` (`:122`), `races_add_builtin` (`:195` — rendering all 17 to
find one), and `races_add_all_builtin` (`:226`). These are player-safe GET pages.
The source files are bundled, read-only, and change only when the image is rebuilt.

**Why it matters:** `render_md` × 17 per page view is meaningful markdown work on
a player-facing page for data that is byte-identical every time.

**Fix:** memoize both loaders with `functools.lru_cache(maxsize=1)` — the return
value is a list of plain dicts and is never mutated by callers (verify: `races.py:132-134`
only filters, `:203-207` only reads). If a hot-reload story is wanted for
development, key the cache on the directory's max `st_mtime`. Note that
`races_add_builtin` should not need the full catalog at all; a narrower
`_find_builtin_race(slug, tier)` that reads one file would be better still.

---

## Wave 3 — Code quality and test coverage

### 15. Extract the duplicated album-tree helpers into a shared module

Four routers carry near-identical copies of the same helpers:

| Helper | audio.py | video.py | pages.py | gallery.py |
|--------|----------|----------|----------|------------|
| `_is_gm` | `:100` | `:96` | `:69` | — |
| `_require_can_edit` | `:105` | `:106` | `:74` | — |
| `_breadcrumb` | `:132` | `:133` | `:101` | `:137` |
| `_descendant_albums` | `:151` | `:152` | `:120` | `:156` |
| `_sub_album_counts` | `:195` | `:198` | `:154` | — |
| clip/doc counts | `:199` | `:202` | `:158` | — |

Each of `audio.py:43`, `pages.py:49`, `video.py` and `gallery.py` also re-derives
`_UPLOADS_DIR` from `DB_PATH` with an identical comment explaining the circular
import that forces the duplication. The bodies are literally the same except for
the model class — `_breadcrumb`'s 50-hop cap comment is copied verbatim four times.

**Why it matters:** item 11's N+1 fix has to be made three times as things stand,
and the four copies have already begun to drift (`gallery.py` never grew the count
helpers; `audio.py` names its counter `_clip_counts` while `pages.py` says
`_doc_counts`).

**Fix:** add `app/media_albums.py` (no router imports, so no circularity) exposing
generic `breadcrumb(db, AlbumModel, album)`, `descendant_albums(db, AlbumModel, root_id)`,
`sub_album_counts(db, AlbumModel, album_ids)` and
`child_counts(db, ItemModel, album_ids, visible_only)`, plus a single
`UPLOADS_DIR`. Move `_is_gm`/`_require_can_edit` into `app/deps.py` next to the
existing `can_edit_content` (`app/deps.py:36-49`). Then delete the four copies.
Behavior-preserving; the existing `tests/test_audio.py`, `tests/test_video.py`,
`tests/test_pages.py` and `tests/test_gallery.py` are the regression net.

### 16. Decide and document whether Race/Profession catalog management is assistant-safe

`POST /races/new`, `/races/add-builtin`, `/races/add-all-builtin`,
`/races/{id}/delete` (and the profession equivalents) create and delete
`Entity(kind="race"|"profession")` rows — i.e. plain world content. But
`_is_assistant_safe` (`app/main.py:456-608`) has no `/races` or `/professions`
branch, so they are GM-only, while the *generic* path to the same data
(`POST /new` with `kind=race`, `POST /entity/{id}/delete`) **is** assistant-safe
(`app/main.py:463-466`). A GM-Assistant can create a race one way and not the
other, for no stated reason. The policy comment at `:441-455` lists the intended
content surfaces and race/profession catalogs fit it squarely.

**Why it matters:** an undocumented inconsistency in the permission model is
exactly the kind of thing that makes the next person guess wrong; and the
allowlist's value depends on every entry being deliberate.

**Fix:** either add
`if path in ("/races/new", "/races/add-builtin", "/races/add-all-builtin") or re.match(r"^/races/\d+/delete$", path)`
(plus professions) to `_is_assistant_safe` with a comment, **or** add a comment at
`:455` explicitly stating that the bundled catalogs are GM-curated and deliberately
excluded. Whichever is chosen, add the paths to the parametrized matrix in
`tests/test_gm_assistant.py:20-163` with the expected value, so the decision is
pinned by a test.

### 17. Resolve the Video / Audio broadcast asymmetry

`app/routers/audio.py:569-609` gives Audio a "Play for players" broadcast backed
by `World.now_playing_*` (`app/models.py:271-274`) and the shared
`GET /api/spotlight` poller. `app/routers/video.py` — which otherwise mirrors
`audio.py` structurally, function for function — has no analogue (grep for
`play-for-players|now_playing|spotlight` in it returns nothing but an unrelated
comment at `:57`). Images have Spotlight; Audio has Now Playing; Video has
nothing. This is very plausibly intentional (a full-screen video push is a
different UX problem from a background track, and `base.html`'s floating widget is
audio-shaped), but nothing in the code or docs says so.

**Why it matters:** the next person to read `video.py` next to `audio.py` will
either build it speculatively or file the same question again.

**Fix:** low-cost option — add a short paragraph to `app/routers/video.py`'s module
docstring stating that video is deliberately out of scope for the broadcast
mechanism and why, and a matching line in the Video section of
`docs/API_REFERENCE.md`. Higher-cost option — implement it, reusing
`World.now_playing_*` with a `now_playing_kind` discriminator so the existing
poller/cache/banner all work unchanged. Recommend the docstring; the audio widget
does not generalize to video without a real UI design.

### 18. Close the specific test gaps around the newest features

Coverage is broadly good (153 test files vs 33 routers). The gaps are narrow and
specific, all on Wave 1 items:

- `tests/test_audio_broadcast.py` has 15 cases (`:48`–`:206`) including
  `test_play_for_players_rejects_hidden_clip` and
  `test_deleting_the_broadcasting_clip_clears_now_playing` — but **none** for
  editing a broadcasting clip to hidden (item 2).
- `tests/test_gm_assistant.py`'s matrix (`:20-163`) covers reachability but there
  is no test that an assistant hitting `GET /api/audio-jobs` cannot read a
  `world_summary` recap or a `code_assist` transcript (item 1).
- `tests/test_calendar.py` has no cross-world delete case, and
  `tests/test_cross_world.py` (`:38-97`) covers only characters and entities
  (item 4).
- `tests/test_entity_detail_toc.py` has no duplicate-heading case (item 5).
- `/pages/sheets` only resolves ahead of `/pages/{doc_id}` because
  `character_sheets_router` is registered at `app/main.py:155`, one line before
  `pages_router` at `:156`. Nothing asserts that ordering; swapping the two lines
  would make every sheet URL 422 with no test failure until a human clicked.

**Fix:** add one test per bullet. The router-ordering one can be a single
assertion in `tests/test_pages.py`: `client.get("/pages/sheets").status_code != 422`,
with a comment naming the ordering dependency.

### 19. Tighten `code_assist_status`'s world scoping and the file-picker walk

Two smaller issues in `app/routers/code_assist.py`:

- `:156` reads `if not job or job.purpose != "ai_assist" or (world and job.world_id != world.id)`.
  When `world` is `None` the world check is skipped entirely, contradicting the
  docstring at `:147-153` ("job.world_id must match the caller's active world").
  GM-only, so impact is nil today, but it is a false comment.
- `_list_source_files` (`:83-94`) does `root.rglob("*")` over both `app/` and
  `static/` on every `GET /tools/code-assist`, then emits one `<option>` per match
  into a `<datalist>` (`app/templates/code_assist.html:16-18`). The walk is
  unbounded by design — it is bounded only by whatever happens to be on disk under
  those two roots.

**Why it matters:** neither is exploitable, but both are the kind of small drift
that turns into a real bug later (e.g. if `static/` ever gains a generated-asset
subdirectory).

**Fix:** change `:156` to `if not world or job.world_id != world.id: raise HTTPException(404)`
after the existing checks. Add an explicit result cap and a skip-list to
`_list_source_files` (`__pycache__`, dotdirs, and a `[:2000]` slice) and memoize it
with `lru_cache(maxsize=1)` keyed on nothing — the source tree is immutable in a
container.

---

## Wave 4 — UX and product polish

### 20. Hide GM-only controls on the player-facing `/races` and `/professions` pages

`app/templates/races.html` renders `+ New Race` (`:111`), a per-race delete form
(`:152`, and again in JS at `:308`), and the built-in "Add" buttons
(`:322`) with **no** `can_edit(request)` or `is_gm` guard anywhere in the file.
`app/templates/professions.html` is the same (`:106`, `:145`). Both pages are
player-safe (`app/main.py:411`), so a player sees a full GM toolbar whose every
button 403s. Compare `app/templates/pages_library.html`, which wraps all of its
management UI in `{% if can_edit(request) %}` (`:17`, `:25`, `:42`, `:125`).

**Fix:** wrap the create/delete/add controls in both templates in
`{% if can_edit(request) %}`. Note this is cosmetic only once item 3 lands — the
server-side gate is the auth_gate, which already denies the POSTs. Add a render
assertion to `tests/test_races.py` / `tests/test_professions.py` mirroring
`tests/test_audio_broadcast.py:197`'s `test_player_never_sees_..._button` pattern.

### 21. Make the Character Sheets list actually informative

`app/templates/character_sheets_list.html` shows each sheet as a name plus, if
`s.player_character_id` is set, the literal text "🔗 linked to a character"
(`:21` and `:39`) — never the character's name, because `character_sheets_list`
(`app/routers/character_sheets.py:143-172`) never resolves the PC. The GM view
groups by owner (`:155-167`) but shows no template name either, so a GM looking at
"Kira" and "Kira (2)" cannot tell which template each came from. There is also no
delete affordance on the list — a player must open a sheet to remove it — and
no "Duplicate", which is the obvious action when a player wants a second sheet
from the same template.

**Fix:** in `character_sheets_list`, batch-load the referenced `PlayerCharacter`
and `PageDoc` rows (`.filter(PlayerCharacter.id.in_(ids))`, same for templates —
one query each, not per row) and pass name maps into the template. Render
`🔗 {{ pc_names[s.player_character_id] }}` and a dim
`from {{ template_names[s.template_id] }}` line. Add a delete form per row reusing
`POST /pages/sheets/{id}/delete` with the same `confirm()` the edit page uses
(`app/templates/character_sheet_edit.html:36`).

### 22. Give the Now Playing widget a volume control and remember its state

`app/templates/base.html:453-457` renders the persistent widget with exactly two
controls: "▶ Tap to play" (autoplay fallback) and "✕" (dismiss). There is no
volume slider and no mute — a GM broadcasting ambiance has no way to let an
individual player turn it down, and `ndNowPlayingDismiss` (`:592-596`) is
all-or-nothing and resets on the next page load, since `seenAudioVersion` is
re-initialized to `null` per page (`:497`). So a player who dismisses the
track gets it back, at full volume, from the top, on their very next navigation.
The dismiss button's own tooltip ("Hide (doesn't stop it for others)") promises
something the implementation doesn't deliver across navigations.

**Fix:** add an `<input type="range">` bound to `audioEl.volume`, persisted in
`localStorage` under `nd_now_playing_volume` and applied in `ndNowPlayingShow`
(`:559-579`). Persist the dismissal too — store the dismissed `audio_version` in
`sessionStorage` and have `pollSpotlight` (`:520-541`) skip `ndNowPlayingShow` when
`data.audio_version` matches it, so a dismissal survives navigation but a *new*
broadcast still gets through. Also consider preserving playback position across
navigation (store `audioEl.currentTime` on `pagehide`), which is the difference
between "ambiance" and "the same 8 seconds over and over".

### 23. Explain what to do with a Code Assist diff

`app/templates/code_assist.html:8-9` tells the GM "Review the diff, then apply it
yourself (paste it into…)" and gives them a 📋 Copy diff button (`:42`). For the
non-technical single user this app is built for, that is the end of the road —
there is no guidance on *where* to paste it, no unified-diff line numbers rendered
alongside (`diffLineHtml` at `:70-76` colors lines but adds no gutter), no way to
see the revised file as a whole rather than as a diff, and no download button for
`d.revised` (which the API already returns —
`app/routers/code_assist.py:182-185`). The panel also silently forgets everything
on reload: the job id isn't in the URL and there is no history, so a GM who
navigates away mid-generation loses the result even though the `AudioJob` row
survives and is visible on `/background-jobs`.

**Fix:** (a) add a "⬇ Download revised file" button writing `lastRevised` (already
captured, `:59`) to a Blob with the file's own basename; (b) add a toggle between
"Diff" and "Full revised file" views; (c) put the job id in the URL hash on submit
and re-attach on load, so a reload resumes polling instead of starting over; (d)
replace the "paste it into…" sentence with a concrete two-line recipe naming the
repo path and `git apply`. None of this changes the preview-only guarantee.

---

## Not findings — verified correct

Recorded so the next audit doesn't re-derive them:

- **Character-sheet postMessage bridge.** `app/templates/character_sheet_edit.html:86-97`
  verifies `event.source === frame.contentWindow` (correct — `event.origin` is the
  literal `"null"` for a sandboxed frame), re-checks `msg.sheetId`, and the server
  takes the sheet id from the URL, not the message. `sandbox="allow-scripts allow-popups"`
  with no `allow-same-origin` is applied at both the iframe (`:48`) and the response
  headers (`app/routers/character_sheets.py:130-140`), matching `serve_upload`
  (`app/main.py:983-985`). The `</` → `<\/` escaping at `character_sheets.py:116`
  is correct and necessary.
- **Code Assist path handling.** `_resolve_safe_path`
  (`app/routers/code_assist.py:59-80`) rejects `..` components, checks the
  extension allowlist before touching the filesystem, and `.resolve()`s **before**
  the containment check, which closes the symlink-escape case. `_INSTALL_ROOT`
  resolves to `/app` inside the container and the repo root in a checkout, so the
  two allowed roots are correct in both.
- **World delete cascade.** `_WORLD_DELETE_MODELS` (`app/main.py:1119-1125`)
  covers every model carrying a `world_id`, `CharacterSheet` included, and
  `world_delete` (`:1128-1200`) separately unlinks files for `AudioClip`,
  `VideoClip`, `PageDoc`, `CalendarDayIcon`, schematics and maps. The
  `now_playing_*` / `spotlight_*` columns live on the `World` row itself and go
  with it.
- **World Summary audience scoping.** `_world_summary_audience_filter`
  (`app/routers/ai.py:625-638`) is correct and correctly applied by the GET, POST
  and DELETE routes; `audience` is server-computed, never client-supplied
  (`:612-614`). Its only hole is item 1's `/api/audio-jobs` bypass.
- **Chronicler SSE.** `chronicler_ask` (`app/routers/chronicler.py:100-187`)
  correctly ignores client-supplied `model`/`think` for non-GMs (`:126-127`),
  checks the cache before the cooldown (`:128-135`), keys the cache per-user so
  per-player entity visibility can't cross-contaminate (`:18-30`), and enforces
  visibility inside `visible_facts` (`:54-64`) rather than in the prompt text.
- **`serve_upload`.** `app/main.py:950-1001` resolves both sides before the
  containment check, forces legacy `.svg` to download, and applies the sandbox CSP
  to `.html`/`.htm`.
- **`docs/API_REFERENCE.md`.** All newer routes are documented — `play-for-players`
  (`:507`), `now-playing/stop` (`:508`), the eight `/pages/sheets/*` routes
  (`:573-580`), `/api/spotlight` (`:616`), and the three `/tools/code-assist`
  routes (`:721-723`).
