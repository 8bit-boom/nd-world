# Phase 6: switching Python off - the work list

(Handoff summary with what is left: `docs/RUST_PORT_HANDOFF.md`.)

Python can leave the image only when the gateway answers every request itself. To know how far that is, the gateway can log who
answers each request (`ND_FORWARD_LOG=<file>`: one `GW|PY METHOD path` line per request; `scripts/gateway_suite.sh` turns it on per
shard as `tests/_data/shard<N>/forwarded.log`). The whole suite run through the gateway (Python 3.13, every route switch on) gives the
census below. Ids are folded to `{}`.

| Route patterns seen in the suite | Count | Requests still answered by Python |
|---|---|---|
| Answered by the gateway every time | 375 | 0 |
| Gateway for some cases, Python for the rest (edge cases, refusals, coercions) | 435 | 2,695 |
| Python every time | 258 | 561 |

(The logs are one full run of the suite, split into groups on one machine each with its own shard folder - `scripts/run_group.sh`.)

The lists, busiest first, are in `docs/census/python_only.txt` and `docs/census/mixed.txt` (`forwarded / gateway` counts). Regenerate
them after each batch; the phase is done when both are empty and the steps below are done.

## Order of work

1. **Authentication**: `POST /login`, `GET /logout`, two-factor (`/login/2fa`, `/account/2fa/*`), `POST /api/login`, password reset.
   Every other route depends on a session the gateway can issue itself.
2. **The busiest Python-only pages and APIs** from `python_only.txt` (session pages, parties, settings, the schedule, the cockpit
   workspace, import), in batches with parity tests as before.
3. **The mixed routes**: close each route's remaining Python cases (refusals with Python's messages, pydantic's coercions), busiest first.
4. **Startup work Python does**: migrations and the database healing in `app/database.py` (`_migrate`, `_heal_table`, seeding), the
   background loops (the jobs' sweeps, the watchdog, backups), and the 2FA / session secrets.
5. **The image**: Python removed from the Dockerfile; image size and memory measured against today's.

### Done so far

- **The System-tab save** (`POST /settings/system`, settings_system.rs `settings_system_save`): the Ollama / Unsloth
  URLs and models, the per-request generation tuning (every field's parse failure and bounds are Python's messages),
  the upload limits, the two world-feature toggles, the API-key leave / clear / replace rules, and the server-level
  Ollama env (tuning.rs's SERVER_ENV_SPEC sanitizer, verbatim, plus the deterministic env-file writer — written after
  the DB save, an unwritable dir surfacing as a 200 page warning as Python's does). Errors re-render settings.html at
  400 through the shared page_context. The model-override save / delete (`settings_overrides.rs`) are ported too —
  the options from the filled fields only (int-kind fields stored as Python ints), the entry kept only when something
  is actually set, the delete dropping the model's entry. The Studio-key save (`studio_key.rs`) is ported too — the
  paste cleaner (Bearer prefix / quotes / the two 400s), the live verify against `{llm_url}/api/hub/cached-gguf`
  (a 401 is not saved and says the saved key is unchanged; an unreachable Studio saves anyway with "couldn't
  confirm"), the clear, and `_key_explanation`.
- **The character roll route** (`POST /api/characters/{}/roll`, edits.rs `pc_roll`): both dice engines — the N&D
  Stat + d10 check with the PP / MP boost spent and logged, and the template system's success pool with the
  `override`, the Stamina spend and its tally / into-every side effects. The dice are random; the answers are
  compared by their own arithmetic in the parity test.
- **The quick-edit number parsing** (`body_int_py`, edits.rs): `int_field`'s coercion exactly - int-strings
  (underscores, whitespace, sign), float truncation, the bool / junk 400s with Python's messages; Unicode-digit
  strings and values beyond i64 still forward. Covers hp-async, shock/pp/mp, carry-limit, currency, xp, equipment
  deltas, ability-use, advance, resource.
- **The list-route coercions** (edits.rs): `_num` = float() exactly (bools are 1.0/0.0, numeric text, containers
  the default), the list index check isinstance-int (a bool counts; every failure the same "Invalid index" 400),
  `str()` of item fields ("None" for null, numbers' spelling, containers their repr), xp's delta through int_field,
  level-up's not-ready 400 (a custom system, level 20+, xp short), "Unknown action" 400s, and the currency purse's
  str()-keyed coin match with `int(_num(value))`. Declines trace their route and body under `ND_DECLINE_TRACE=1`.
- **The party stash** (`POST /api/parties/{}/loot`, edits.rs): `load_loot` is Python's lenient normaliser
  (bad JSON an empty stash, non-dicts dropped, a legacy item's `lid` derived as sha1 of "pos|name", qty
  `int(x or 1)` with the fallback, ints-only `claimed_by`), `_loot_int`'s strings through int() and its
  "must be a whole number" / "out of range" 400s, the 409 for a gone `lid`, "Invalid loot item", "Item name
  is required", "notes must be text", the member / own-character refusals and the action list 400.
- **Condition labels** (`clean_condition`, edits.rs): Python's isprintable() as a table of the
  separator / format blocks, whitespace-collapsed, cut at 40 then stripped; the case-insensitive
  de-dup lowercases the Unicode way. Only a name of unassigned codepoints (undetectable without
  Unicode tables) would diverge.
- **Calendar events** (`POST /api/calendar/events`, edits.rs): `_safe_int` (int() with the fallback),
  the linked ids through int() (a bool 1/0, int()-readable text; junk is Python's uncaught 500 —
  declined), and str() titles / notes / colors (numbers their spelling, containers their repr).
- **The second-screen stage** (`POST /api/display/show`, edits.rs `stage_item`): every refusal is
  Python's answer verbatim (the 404 messages, "That image address is not allowed", "Nothing to
  show", the kind list); a bool combat_id / entity_id is isinstance-int; the text card is rendered
  here (`render_md`, the 20,000 cap). The entity text card still forwards (its gmonly strip is
  Python's). Each side's stage is per-process, so the parity test compares item content.
- **Quest suggestions applied** (`POST /api/quests/apply`, edits.rs): `(x or [])[:12]` over a string
  iterates characters (nothing is a dict), a number / bool is Python's 500 (declined); `str(v or "")`
  fields (containers their repr); `quest_id` by isinstance-int (a bool is quest 1/0);
  `assigned_party_id` refuses bools FIRST (None) and must be a party of the world.
- **The read-view markup route** (`POST /api/entity/{}/markup`, edits.rs + markup.rs): the locators are a full
  port and Python's `MarkupError` texts are answered verbatim as 422s (`MkErr::Refuse`); only the internals the
  gateway keeps declining on (a regex backtracking differently) still forward (`MkErr::Forward`).
- **The quick-edit number parsing** (`body_int_py`, edits.rs): `int_field`'s coercion exactly - int-strings
  (underscores, whitespace, sign), float truncation, the bool / junk 400s with Python's messages; Unicode-digit
  strings and values beyond i64 still forward. Covers hp-async, shock/pp/mp, carry-limit, currency, xp, equipment
  deltas, ability-use, advance, resource.
- **The list-route coercions** (edits.rs): `_num` = float() exactly (bools are 1.0/0.0, numeric text, containers
  the default), the list index check isinstance-int (a bool counts; every failure the same "Invalid index" 400),
  `str()` of item fields ("None" for null, numbers' spelling, containers their repr), xp's delta through int_field,
  level-up's not-ready 400 (a custom system, level 20+, xp short), "Unknown action" 400s, and the currency purse's
  str()-keyed coin match with `int(_num(value))`. Declines trace their route and body under `ND_DECLINE_TRACE=1`.
- **The party stash** (`POST /api/parties/{}/loot`, edits.rs): `load_loot` is Python's lenient normaliser
  (bad JSON an empty stash, non-dicts dropped, a legacy item's `lid` derived as sha1 of "pos|name", qty
  `int(x or 1)` with the fallback, ints-only `claimed_by`), `_loot_int`'s strings through int() and its
  "must be a whole number" / "out of range" 400s, the 409 for a gone `lid`, "Invalid loot item", "Item name
  is required", "notes must be text", the member / own-character refusals and the action list 400.
- **Condition labels** (`clean_condition`, edits.rs): Python's isprintable() as a table of the
  separator / format blocks, whitespace-collapsed, cut at 40 then stripped; the case-insensitive
  de-dup lowercases the Unicode way. Only a name of unassigned codepoints (undetectable without
  Unicode tables) would diverge.
- **Calendar events** (`POST /api/calendar/events`, edits.rs): `_safe_int` (int() with the fallback),
  the linked ids through int() (a bool 1/0, int()-readable text; junk is Python's uncaught 500 —
  declined), and str() titles / notes / colors (numbers their spelling, containers their repr).
- **The second-screen stage** (`POST /api/display/show`, edits.rs `stage_item`): every refusal is
  Python's answer verbatim (the 404 messages, "That image address is not allowed", "Nothing to
  show", the kind list); a bool combat_id / entity_id is isinstance-int; the text card is rendered
  here (`render_md`, the 20,000 cap). The entity text card still forwards (its gmonly strip is
  Python's). Each side's stage is per-process, so the parity test compares item content.
- **Quest suggestions applied** (`POST /api/quests/apply`, edits.rs): `(x or [])[:12]` over a string
  iterates characters (nothing is a dict), a number / bool is Python's 500 (declined); `str(v or "")`
  fields (containers their repr); `quest_id` by isinstance-int (a bool is quest 1/0);
  `assigned_party_id` refuses bools FIRST (None) and must be a party of the world.
- **The settings saves** (`POST /settings`, `/settings/resources`, edits.rs): the display / hover form (format
  validation falls back to avif, the hover numerics clamped rather than rejected, the checkbox presence-keyed) and the
  resource sizing (blank / "auto" / a failed parse = automatic per field, the values clamped to their bounds). The
  routes' database writes are byte for byte Python's; Python's in-memory side effects (its worker pools, its
  preview-format cache, the queued thumbnail remake) are process-internal and move at the switch.
- **The database boot** (startup.rs, batch H step 4): a gateway-only container's first boot creates the database —
  the post-migration schema (dumped from a fresh Python boot into app_data.json's "schema_sql" by
  scripts/gen_rust_data.py: create_all + _migrate against a temp database, 245 CREATE statements, FTS5 shadows
  skipped) plus database.py's _seed rows (world 1, the bundled HTML schematics, the built-in sheet / entity templates
  with their field blobs from the dumped seed data, the starter tables, the GM account from GM_EMAIL / GM_PASSWORD —
  an existing non-GM row with that email left alone, as Python's seeder logs). Idempotent: a database anyone already
  made is left untouched. The Rust binary carries the dumped schema (include_str), so no Python is needed at boot.
  After the boot, the sweep marks every job still mid-flight interrupted across the five engines — each engine's own
  fields (audio_jobs carries chunk progress and a finished_at stamp; the others their own note, no finished_at
  column). Python's resume_interrupted_jobs re-runs them through its engines; the gateway's own audio runner resumes
  its audio jobs the same way. The gateway also owns two of the boot loops: `db_upkeep.start` (startup.rs `db_upkeep`:
  PRAGMA optimize an hour after boot and daily, the incremental-auto-vacuum switch at a quarter free pages, up to
  25 000 pages released daily) and `_backups.start` (startup.rs `backups`: when ND_BACKUP_DIR is configured,
  a world-<stamp>.db VACUUM INTO snapshot per ND_BACKUP_INTERVAL_SECONDS — at least 60s, wait-first — keeping
  ND_BACKUP_KEEP, pruned by name newest-first). `_diagnostics.start` stays with Python (its watchdog watches
  Python's own event loop); `_fan_control.start` is already the gateway's (fans.rs).
- **The password change** (`POST /account/password`, account.rs `password_change`): the failed-attempt lockout with the
  GATEWAY'S OWN counter (the in-memory Python counter it replaces at the switch - 8 wrong current passwords in 5 minutes
  lock the change for 5), the current-password check, the length and confirm errors rendering the account page at 400
  byte for byte, and on success the hash swap (the same PBKDF2 format), the session_version bump, every trusted device
  revoked and THIS session re-stamped (auth.rs resign) so the change doesn't log itself out. The 2FA-disable and
  backup-code routes (which share the counter) stay Python's until their TOTP paths move.
- **2FA disable and backup-code regenerate** (`POST /account/2fa/disable`, `/account/2fa/backup-codes/regenerate`,
  account.rs): the same gateway-owned lockout counter, the password verified, disable wiping the secret / codes /
  trusted devices (re-enabling later starts clean), regen replacing the hashed codes (8 fresh ones, only their sha256
  stored) and showing the plaintext once on the re-rendered page - every error page byte for byte, the row compared
  after each step. A login with 2FA on still goes through Python's half-authenticated TOTP flow.
- **Two systematic mixed-route declines closed**: a tables edit whose entries JSON fails to parse now resets the
  entries to "[]" and answers 303 as Python does (Python's json.loads ACCEPTS NaN / Infinity where serde fails - that
  payload still reaches Python so the stored text cannot diverge); and a party edit by a viewer with no edit rights at
  all ("none") answers Python's plain 403 instead of forwarding.
- **Hub companions and live-audio reads** (`GET /api/characters/<id>/hub/companions`, `GET /api/sessions/<id>/live-audio`):
  the companions bare with the kind list (the _hub_pc gate's 404s answered), the raw recording's segments with their
  total disk size (a corrupt list degrades to nothing, a mixed-type list str()'d, the record's own world's sessions
  level judged - players never reach the route: the auth gate 403s it on both sides). A GET slug route no longer lets
  a POST-only ROUTES pattern on the same path shadow it (edits.rs run()).
- **The world JSON restore** (`POST /worlds/<id>/import`, edits.rs `world_import`): a world export's entities restored -
  the embedded data-URI image written back under a fresh uploads name (the allowed-extension floor), each entity
  upserted by (name, kind) with every field set outright (absent key NULL, explicit "" empty), the template resolved
  id > slug > name, the redirect counting created and updated.
- **The importer** (`POST /api/import/detect`, `POST /api/import/execute`, importer.rs): every detection branch byte for
  byte, and the execution's world_rules / random_table / entity_single / entity_bulk / map_overlay /
  schematic_elements / field_template / player_character branches (Python's messages word for word, one transaction per
  request); the batch envelope stays Python's.
- **Authentication** (`auth` route switch): sign-in, two-step codes, trusted devices, the lockout, sign-out, the stale-session refresh
  and `auth_gate`'s refusals.
- **Settings page** (`pages` switch): `GET /settings` (every tab) and `GET /settings/visibility-rows`. The cells that describe the
  serving process are the gateway's own: resource sizing is read from the same machine, environment and saved `res_*` row; the live
  page-build limit is Python's render gate, which the gateway does not have (the page shows the starting value); the Diagnostics
  tab's watchdog thread, event-loop heartbeat and database pool are Python's and read "not started" / "inactive" / "unavailable".

- **Session planner** (`pages` switch, rust/nd-gateway/src/schedule.rs): `GET /schedule`, `GET /api/schedule/state`, every plan /
  slot / vote write, confirm (optionally creating the session log), reopen, cancel, delete and both `.ics` downloads, refusals
  included. A time `datetime.fromisoformat` reads in one of its rarer forms (week dates, compact `20310504T1830`, an hour alone) and
  a path id FastAPI would coerce (`007`) still go to Python.

- **Party page** (`pages` switch, rust/nd-gateway/src/idpages.rs `party_detail`, apis.rs `party_vitals`): `GET /parties/<id>` and
  `GET /api/parties/<id>/vitals`, refusals included. A party of a world other than the player's active one, and member lists that are
  not plain id lists, still go to Python.

- **Character list** (`GET /characters`), **the GM's AI chat page** (`GET /ai`) and **calendar search** (`GET /api/calendar/search`),
  refusals included. A calendar not created yet (Python makes the row on the first visit) still goes to Python.

- **Import page** (`GET /import`), **Session Log page** (`GET /session-log/<id>`), **a job's status** (`GET /api/audio-jobs/<id>`) and
  the **character hub's quests and known-world tabs** (`GET /api/characters/<id>/hub/quests`, `/hub/world`), refusals included.
- **Refusals on the mixed routes** (step 3): the character quick-edits (`hp-async`, `shock`, `pp`, `mp`, `xp`, `conditions`,
  `currency`, `carry-limit`, `feats`, `equipment`) and the party stash (`POST /api/parties/<id>/loot`) now answer Python's 400 / 403 /
  404 / 409 themselves, with Python's messages (edits.rs `pc_gate`, `json_body_refusal`, `list_refusal`, `loot_refusal`). Bodies
  serde reads differently from `json.loads` (NaN, lone surrogates, a BOM, out-of-range numbers, deep nesting), a GM's stash edit in
  another world still go to Python. The stash's `give` (into a member's equipment, merging a matching item) is the
  gateway's too.
- **The settings row**: Python's `get_app_settings` creates the one `app_settings` row on first use; the gateway's page context
  declined while it was missing, so the first pages of a fresh instance (and of nearly every test) went to Python. It now creates
  the row the same way (layout.rs `app_settings_row`).
- **Race and profession catalogs** (`GET /races`, `GET /professions`, catalog.rs): the world's entries by tier and the bundled
  catalog under app/races/ and app/professions/ (read once, as Python's lru_cache does).
- **Player-safe Smart Context** (`POST /api/ai/world-context-player`, retrieval.rs `smart_world_context_as`): the same retrieval as
  the GM's, through the player's own visibility at every step (search, top-up, latest notes, priority entities), [gmonly] text and
  :::gm rules blocks stripped, only the characters the player may see; no vault layer for a player (as in Python).
- **The player cockpit's live board** (`GET /api/cockpit/player-board`, apis.rs `cockpit_player_board`): the viewer's own parties with
  member vitals (a GM's `?pc=` reads the board through that character's owner), the world's player-visible active top-level quests with
  party names, the viewer's characters with vitals and XP; an unknown `?w=` falls back to the viewer's world, as Python's does.
- **A party Rest undone** (`POST /api/parties/<id>/rest/undo`, edits.rs `party_rest_undo`): the snapshot `/rest` returned put back on
  every member - PP / MP / Shock clamped as Python's `int()` reads them and, on a custom system, the custom fields its Rest touches;
  malformed entries skipped, Python's bad-body 400s answered, a bool id or a float in the stored member list left to Python.
- **The importer's execution, first branches** (`POST /api/import/execute`, importer.rs `execute_import`): `world_rules`,
  `random_table` (one or several, the "Imported Table" default, category / description as Python's `str()` spells them),
  and `entity_single` / `entity_bulk` (`_create_entity` with the template resolved id > slug > name, the custom fields
  JSON-ified, Python's ValueError messages word for word, "Entity #N:" for a bulk failure) - one transaction per request,
  Python's messages or the redirect; the remaining branches (map overlays, schematics, field templates, player
  characters, the batch envelope) still Python's.
- **The importer's detection** (`POST /api/import/detect`, importer.rs `detect_kind`): every sniff branch byte for byte -
  the batch envelope and its Counter-ordered breakdown, world rules, map overlays, schematic elements (both shapes),
  random tables (list and single), field templates (both shapes), entities and player characters (bulk and single, the
  world's custom kinds recognized, the fuzzy PC key markers), the unknown fallback. An inner text json.loads refuses (or
  reads differently) stays Python's own "invalid" answer, message included.
- **The per-kind entity zips** (`GET /kind/<kind>/download.zip`, `/download-selected.zip`, apis.rs `kind_download_zip`):
  one .md per entity - the same text the single-entity download writes (`entity_markdown`, now shared by both) - the
  name sanitized with the id appended, visibility and the download toggle enforced, the picked ids honored (a hidden id
  never smuggled in), a query id FastAPI would 422 on left to Python.
- **The rules-and-notes export** (`GET /export/rules-and-notes.md`, export.rs `rules_and_notes`): the world's rules (legacy
  anchors stripped), its note entities (full text, hidden included, subtype tags) and every entity note grouped under its
  entity - the same markdown Python writes, byte for byte, the final rstrip included; a kind or name beyond the ASCII this
  plain-text writer promises stays Python's.
- **The instance backup zip** (`GET /admin/backup.zip`, export.rs `admin_backup`): a VACUUM INTO snapshot of the database
  (taken next to the database, never inside the walked uploads tree), the manifest with the table counts, every uploaded
  file and map json under its prefix. The filename and the manifest's created_at are the build moment; the parity rule
  compares the entries (the database by its logical dump - the two VACUUM INTO snapshots can differ physically), the
  manifest with created_at normalized.
- **The world book zip** (`GET /export/book.zip`, export.rs `book_zip`): the standalone HTML book (the entities by kind
  with their bodies rendered and the leading h1 dropped, their images bundled by basename, the boards, the maps with their
  marker counts, the rules with the legacy anchors stripped), zipped with the stylesheet. The zips' own bytes differ from
  Python's (its timestamps are its build moment) - the parity rule compares the entries, name for name and byte for byte,
  with the owner-chosen markdown whitespace difference collapsed for index.html (docs/RUST_PAGES.md).
- **The hover-preview settings read** (`GET /api/hover-preview/config`, apis.rs): the instance-wide flags and delays
  exactly as Python answers them, including the null-vs-zero distinction on the hide delay.
- **The AI character creator page and a new schematic** (`GET /characters/ai-new`, pages.rs `characters_ai_new`: the world's
  custom systems, the selected template id, the player's display name, the limit redirect; `GET /maps/schematic/new`, the
  empty canvas form; `POST /maps/schematic/new`, edits.rs `schematic_new`: the name's slug uniquified, the canvas bounds
  checked, the row written, the redirect - player-safe as Python's allowlist has both).
- **Unknown map / board slugs answered** (`GET /maps/<slug>`, `/boards/<slug>`, `/boards/<slug>/notes`): a map with no
  json file (or one that does not parse) and a board with no row are the 404 Python raises, answered by the gateway itself
  instead of forwarded.
- **Session summary / transcript downloads** (`GET /sessions/<id>/summary.md`, `/transcript.md`, apis.rs): the text with
  [gmonly] stripped for a player, the sanitized file name, read-gated by the row's own world's sessions section; the
  no-content 404s in Python's words; a title beyond Python's ASCII file names stays Python's.
- **AI attachment background jobs** (edits.rs, ai.rs): `POST /api/ai/attachments/audio-jobs` (a recording stored under
  uploads/ai_attachments and a pending row, the job started right away), the chunk pair (32-hex upload ids, parts reassembled
  by .../complete), `GET .../audio-jobs/<id>` and the recent list - a GM always, a player only in a world that opened an AI
  surface, and a player never seeing anyone else's jobs; `purpose="attachment"` transcribes and is done (the runner's
  attachment branch, no summarization). A 403 in a world without either surface stays Python's (forwarded).
- **The character creation and edit forms** (pages.rs `characters_new`, idpages.rs `character_edit`): the wizard with the
  N&D default template (or the ?template_id= one - a custom mode template gets its own form page through the ported
  `tpl_fields_json` filter, now on the gateway's environment), a player at their character limit redirected as Python
  redirects; the edit form with the row's live JSON columns and the world's templates, a custom-mode character's edit a
  redirect to its page, a stranger 403.
- **The session page's featured-entity picker** (`GET /sessions/<id>/npc-candidates`, apis.rs): the world's entities by the
  picker's groups (NPCs split from subtype-PC write-ups), the real characters as "pc:<id>"; GM-only as Python's allowlist has it.
- **Hub keepsakes, places and handouts tabs** (hub.rs `keepsakes_tab` / `places_tab` / `handouts_tab`): what the character
  kept (newest first, the world's per-kind personal-file limits) with everything the viewer could still keep - shown handouts,
  the owner's private notes with [gmonly] stripped, the session log's player-visible media (`viewer_media` extracted into
  edits.rs); every page the world opens to a player with the character's pins; the shown pictures with their new flags.
- **Character reads and exports** (apis.rs): `GET /api/characters/<id>/vitals` (the live numbers with the sheet's effective
  maxima and the cleaned conditions, viewable by exactly who may view the sheet), `/sync` (the live-sync dict of a character
  the caller manages, of any world - Python checks no world here), `/advance` (the XP shop; a custom system's 400 keeps
  Python's wording) and `/ability-cards` (the feat cards with their pools); `GET /characters/<id>/export.json` (the import
  shape as `json.dumps(indent=2, ensure_ascii=False)` writes it, zero / empty fields omitted, the name defaulted) and
  `/export.md` (the sections file - a template's list fields expanded per item and resources reading their
  `{id}_current / {id}_max`, which `field_value_text` now speaks too); a name with a character beyond Python's ASCII file
  names stays Python's.
- **The site layout saved and reset** (`POST` / `DELETE /api/view-prefs/site`, edits.rs `view_pref_save` / `view_pref_reset`): the row
  written as Python's `json.dumps` writes it and the session re-signed with `{options, pins, at, uid}` in it (auth.rs `resign`), so the
  pages after it are gateway-served from the fresh layout; an unknown section is Python's 404 "Unknown section" on both verbs.
- **Studio TTS / STT preferences** (`POST /api/ai/unsloth/prefs`): ai_models.json written byte for byte as Python's
  `json.dumps(indent=2)` writes it (nd-render `py_dumps_indent`); Python reads that file afresh on every call, so the two never disagree.
- **An entity as markdown** (`GET /entity/<id>/download.md`, apis.rs `entity_download`): the same file and headers; a name beyond
  ASCII (Python's `isalnum` / `capitalize`) stays Python's.
- **Session Log list** (`GET /session-log`, pages.rs `session_log_list`): recap markers and the audio / picture / video marks by
  what the viewer may hear and see.
- **NPC talk page** (`GET /npc-talk`): the talkable entities the viewer may see, `?entity=` picking one; the 403 for a world closed
  to players' AI.
- **World JSON exports** (`GET /worlds/<id>/export`, `/export/entities/<kind>.json`, export.rs): the same bytes as
  `stream_json_array_download` writes (nd-render `py_dumps_indent_as`, `ensure_ascii=False`), images embedded; built whole rather
  than streamed.
- **Character sheets** (`/pages/sheets*`, charsheets.rs): list, page, sandboxed render and download, new / save / edit / delete,
  refusals included.
- **Character hub log and notes tabs** (`GET /api/characters/<id>/hub/log`, `/hub/notes`, hub.rs): the change log with what can still
  be undone, the GM's private notes as excerpts with the thread link, the journal with its session labels.
- **Account page** (`GET /account`, account.rs) with its layout panel (the presets in Python's key order, from
  scripts/gen_rust_data.py), and the **two-step confirm** (`POST /account/2fa/setup`): a wrong code (the setup page again, 400), the
  right one (8 backup codes stored as sha256, shown once on the account page), the redirects.
  Also `POST /account/name` (its 400 page too) and `POST /account/tokens/new` (shown once; only the sha256 kept).
- **Two-step setup** (`GET /account/2fa/setup`, auth.rs `twofa_setup`, qr.rs): the pending secret kept in the session across
  reloads, and a QR code drawn module for module as Python's qrcode package draws it - its segments (`optimal_data_chunks`, minimum
  20), the smallest version at level M, and its own mask scoring (`lost_point`, format/version areas light while scoring). The PNG
  bytes differ; the parity rule compares decoded pixels (tests/test_gateway_golden.py) and module matrices for 40 URIs, versions
  6-14 (rust/nd-gateway/tests/qr.rs).
- **Job status polls** (apis.rs): `GET /api/media-rename/runs/<id>` (the results included, the "stored results were unreadable"
  fallback either column tripping, the audio / video / images edit-level 403, the author's display name or email prefix),
  `GET /api/sessions/ai/audio-jobs/<id>` (sessions.py's own smaller `_job_to_dict`; a job of any other purpose is the same 404 a
  missing one is), and `GET /api/ai/imagegen/player/jobs/<id>` (the gate first - a GM always may, anyone else needs the world's
  image-gen toggle, a missing world 403 there - then "wrong world" and "not mine" collapsing into one 404 for a player). Also
  `POST /logout` (Python has no such route: Starlette's method-not-allowed, answered without reading the body, lib.rs).

### What has to wait for the switch itself

Python keeps some saved settings in memory and reloads them only when its own handler saves them: the resource sizing
(`workers.apply`), the LLM URL / model / key / context overrides (`ai.set_llm_override`) and the Ollama tuning. A save written
by the gateway would leave Python running on the old values, so `POST /settings`, `/settings/system*` and `/settings/resources` stay
Python's until Python is switched off, and then move in one step.

The account routes that check the current password - `POST /account/password`, `/account/2fa/disable` and
`/account/2fa/backup-codes/regenerate` - count failed attempts in Python's memory (`_failed_password_changes`, a lockout after 5). A
gateway answer would not count toward Python's lockout, so they move at the switch, with the gateway's own counter.

Python-only libraries with no exact Rust twin (reportlab for PDF export, the PDF / HTML sheet readers behind the AI drafters) may stay
in a small sidecar, as the plan allows, if a port is not good enough.
