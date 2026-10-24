# Phase 6: switching Python off - the work list

(Handoff summary with what is left: `docs/RUST_PORT_HANDOFF.md`.)

Python can leave the image only when the gateway answers every request itself. To know how far that is, the gateway can log who
answers each request (`ND_FORWARD_LOG=<file>`: one `GW|PY METHOD path` line per request; `scripts/gateway_suite.sh` turns it on per
shard as `tests/_data/shard<N>/forwarded.log`). The whole suite run through the gateway (Python 3.13, every route switch on) gives the
census below. Ids are folded to `{}`.

| Route patterns seen in the suite | Count | Requests still answered by Python |
|---|---|---|
| Answered by the gateway every time | 133 | 0 |
| Gateway for some cases, Python for the rest (edge cases, refusals, coercions) | 376 | 5,388 |
| Python every time | 555 | 5,877 |

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
