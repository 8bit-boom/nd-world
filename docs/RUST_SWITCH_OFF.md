# Phase 6: switching Python off - the work list

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
- **Not yet**: `GET /account/2fa/setup` draws its QR code with Python's `qrcode` package; the gateway's image will be a different
  (equally scannable) PNG, so that page needs its own parity rule (decode the QR, compare the otpauth URI) when it moves.

### What has to wait for the switch itself

Python keeps some saved settings in memory and reloads them only when its own handler saves them: the resource sizing
(`workers.apply`), the LLM URL / model / key / context overrides (`ai.set_llm_override`) and the Ollama tuning. A save written
by the gateway would leave Python running on the old values, so `POST /settings`, `/settings/system*` and `/settings/resources` stay
Python's until Python is switched off, and then move in one step.

Python-only libraries with no exact Rust twin (reportlab for PDF export, the PDF / HTML sheet readers behind the AI drafters) may stay
in a small sidecar, as the plan allows, if a port is not good enough.
