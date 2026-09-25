# FLASH_EXECUTION_PLAN.md — step-by-step work plan for a fast executor model

Written by GLM-5.3 (2026-09-25) as a **handoff plan**: every step below is
self-contained, names exact files, and has a verification command. Execute
steps top to bottom; every numbered step is independently committable.

## Ground rules (read first)

- Repo: `F:\GLM\nd-world` (Windows dev box; deploy target is Linux Docker).
- Test suite: `.venv311/Scripts/python.exe -m pytest -q` (full run ≈ 5 min).
  **Never run two pytest invocations at once** (shared DB_PATH).
- Scope tests while iterating (e.g. `... -m pytest tests/test_npc_talk.py -q`),
  then run the FULL suite before each commit. Expected baseline:
  `3870+ passed, ~16 skipped, 0 failed` (skips are Windows/POSIX guards).
- Read `AGENTS.md` first. Key facts: routers are GM-only by default via the
  `auth_gate` middleware allowlists in `app/main.py` (`_is_player_safe`,
  `_is_assistant_safe`); a NEW TABLE needs no migration code, a new COLUMN
  on an existing table heals automatically only for tables in
  `app/database.py`'s `_heal_table_from_model` list; every new HTTP route
  needs a row in `docs/API_REFERENCE.md` (a test enforces this).
- One commit per numbered step, message style: imperative one-liners like
  the existing `git log --oneline`. Push after each green suite: the
  GitHub "tests" + "Build & Publish" workflows must both pass.
- Don't reformat, rename, or "improve" anything outside a step's stated
  files. If a step turns out to be already done or impossible, skip it and
  note that in the commit-free skip report instead of improvising.
- All `file:line` references were verified at commit `746c46d`; if a line
  drifted, grep the quoted function name.

---

## Part A — NPC Talk follow-ups (feature shipped in 746c46d; these polish it)

### A1. README feature bullet

`README.md` has a bulleted feature list near the top (lines ~20-45) and an
"AI Setup" section (~line 900). Add ONE bullet to the top list, after the
AI Chat bullet: mention **Talk to NPCs** — persistent in-character
conversations with any entity, per-user threads, GM lore/thinking toggles,
players get it under the same per-world "players can use Ask AI" opt-in.
Verify: none (docs). Commit: `Docs: NPC Talk in README feature list`.

### A2. NPC Talk test gaps

Extend `tests/test_npc_talk.py` (read its existing helpers `_npc`,
`_patch_ai`, `_opt_in`, `_pin` first — reuse them):

1. `test_empty_message_is_400` — GM POST `/api/npc-talk/{id}/stream` with
   `{"message": "   "}` → 400.
2. `test_assistant_tier_needs_world_opt_in` — flip `seed.player_a` to
   assistant (copy `_make_assistant` from `tests/test_gm_content_leaks.py`),
   without the world flag → 403; with `players_can_ask_ai` → 200.
3. `test_model_turn_cap` — monkeypatch `_patch_ai`'s capture; pre-seed a
   `ChatSession(surface="npc", entity_id, user_id=gm.id, world_id,
   messages_json=...)` with 100 stored turns via SessionLocal, send one
   message, assert `len(cap["msgs"]) <= 40` (the `_MAX_MODEL_TURNS` window)
   while `/history` still returns 102 rows.
4. Nav-dedupe regression — in `tests/test_npc_talk.py` or the nav test
   file: `resolve_nav_menus(world_a, True, True, fake_request(is_gm=True))`
   with `world_a.players_can_ask_ai = True` must contain EXACTLY ONE
   `/npc-talk` item across menus+ungrouped; a player request with the flag
   on sees exactly one; flag off sees none.

Verify: `-m pytest tests/test_npc_talk.py -q` then full suite.

### A3. Live UI smoke (manual, no code)

Boot the app on a scratch DB (see `tests/test_npc_talk.py` for the env
vars: `DB_PATH`, `GM_EMAIL`, `GM_PASSWORD`; `ND_DIAG_DIR` optional) with
`.venv311/Scripts/python.exe -m uvicorn app.main:app --port 8765`, log in,
open `/npc-talk`, confirm: picker renders with portraits, a message
streams a reply (needs a working Ollama — SKIP the send test if none),
Restart and ⬇ .md work, and the nav shows "Talk to NPCs" once for the GM.
No commit; just report findings.

---

## Part B — audit round 2 remainder (from docs/AUDIT_PLAN_NEXT.md; full
reasoning per item lives there — this part is the execution translation)

### B1. MCP read tools must honor the section permission matrix  (P1)

Files: `app/mcp_server.py`, `app/deps.py`, `tests/test_mcp.py`.
Steps:
1. In the MCP auth wrapper (`_mcp_auth_wrapper` — find it by grep), after
   resolving the user, also resolve the active world and set
   `request.state.is_assistant` (currently unset; assistants must read as
   the player tier).
2. For each read tool — `list_facts`, `list_quests`, `get_rules`,
   `list_sessions`, `get_session`, `search_entities`, `get_entity` — add
   the matching `deps.world_can_view_section(request, world, sid)` check
   (sid: `facts`, `quests`, `rules`, `session_log`/`sessions` — use
   whichever sid the web routes use, grep them), raising the same error
   the tool layer uses for permission failures. `get_entity` additionally
   checks the per-kind id `kind_{entity.kind}` exactly like
   `app/main.py`'s entity detail route does.
3. Tests in `tests/test_mcp.py`: a player-tier token with facts=none gets
   an error from `list_facts`; quests read → `list_quests` works; a hidden
   kind blocks `get_entity`.
Verify: `-m pytest tests/test_mcp.py -q` + full suite.

### B2. Enforce assistant edit-tier dial-downs on Tier B writes  (P1)

Files: `app/routers/facts.py`, `gallery.py`, `sessions.py`,
`boards_generate.py`/`app/main.py` (board routes), `maps.py`,
`importer.py`, `bulk-edit` routes in `app/main.py`, `pages.py`,
`audio.py`, `video.py`, `audio_jobs.py` (job-create POSTs).
Steps: in every POST/DELETE handler reached through `_is_assistant_safe`
that mutates content, add right after the existing world resolution:
`if not world_can_edit_section(request, world, "<section_id>"): raise HTTPException(403)`
using the same section id the corresponding GET guards with. The quests
router (`app/routers/quests.py` quest_create) is the exemplar pattern.
DO NOT touch player own-row rules (`world_can_edit_row` already handles
those — e.g. a player's own quest stays editable).
Tests: extend `tests/test_player_section_access.py` dial-down cases
(there is an existing quests case ~line 403; mirror it for facts and
gallery POSTs at minimum).
Verify: `-m pytest tests/test_player_section_access.py tests/test_gm_assistant.py -q` + full suite.

### B3. Close request-scoped DB pool holds across AI awaits  (P1)

Files: `app/routers/facts.py` (folk-tale ~:353, parse ~:317),
`app/routers/sessions.py` (sync summarize routes ~:1394, ~:1446),
`app/routers/video.py` (transcribe ~:649).
Pattern to apply (exemplar: `app/routers/sessions.py:1214-1258` live-append
bookend): do ALL DB reads first, extract plain values, `await` the AI call
with NO session held, then open a short-lived `s = SessionLocal()` …
`s.commit(); s.close()` (try/finally) for the write. Do NOT change behavior
— same queries, same writes, just reordered around the await.
Tests: existing files must stay green (`tests/test_facts*.py`,
`tests/test_sessions*.py`, `tests/test_video.py`); add one regression per
fixed route asserting the result still lands (the existing suites mostly
cover this — add only if a route has zero coverage).
Verify: full suite.

### B4. Restore-upload decompression caps  (P2)

File: `app/main.py` (`admin_backup_restore_stage`, ~line 4576).
Steps: after opening the ZipFile, iterate `zf.infolist()`; reject (400)
if `sum(i.file_size for i in infos) > 8 GiB` or `len(infos) > 20000`,
message naming the limits. Keep the rest identical.
Test: `tests/test_backup_restore.py` — build an in-memory zip whose info
headers claim huge `file_size` (set `ZipInfo.file_size` manually) → 400.
Verify: `-m pytest tests/test_backup_restore.py -q`.

### B5. Restore staging races  (P2)

File: `app/main.py` (stage + cancel routes, ~4601-4630).
Steps: module-level `_RESTORE_LOCK = threading.Lock()`; wrap the
`rmtree/mkdir/extract/move` critical section of BOTH routes in
`with _RESTORE_LOCK:`. Import `threading` at module top if absent.
Test: `tests/test_backup_restore.py` — two threads calling the staging
route (via TestClient in threads) both complete without 500.
Verify: same file.

### B6. Snapshot-name TOCTOU  (P2)

File: `app/backups.py` (~:51-58) + `app/routers/backups.py`.
Steps: wrap the name-pick + `VACUUM INTO` in a module lock; on
`sqlite3.OperationalError` containing "exists", retry once with a `-2`
suffix. Test: pre-create the expected snapshot filename, run
`run_backup_once`, assert success and a distinct filename.
Verify: `-m pytest tests/test_backups*.py -q` (find exact filename).

### B7. Deeper staged-DB schema check  (P2)

File: `app/main.py` staging validation (~4592).
Steps: after the existing `SELECT COUNT(*) FROM worlds`, also compare
`PRAGMA user_version` of the staged DB against the live one; mismatch →
400 with a clear "backup predates the current schema" message. If both
are 0 (this app doesn't set user_version), instead check that the
`entities` table has the `aliases` column (a recent, load-bearing column)
via `PRAGMA table_info(entities)`.
Test: stage a zip whose world.db is a valid empty SQLite file with only a
`worlds` table → 400.
Verify: `tests/test_backup_restore.py`.

### B8. GM clears a member's lost 2FA  (P2)

Files: `app/main.py` (next to `reset-password` route ~1680),
`app/templates/world_edit.html` (members table), `tests/test_password_reset.py`.
Steps: new GM-only POST `/worlds/{world_id}/members/{user_id}/clear-2fa`
— verify membership (same check reset-password uses), delete the user's
`TrustedDevice` rows, set `totp_enabled=False` (+ secret column if the
model has one — check `app/models.py` User), commit, redirect back.
Add the API_REFERENCE row. Button in the members table next to
reset-password with a confirm(). Tests: player 403; happy path clears and
next login skips `/login/2fa`.
Verify: `-m pytest tests/test_password_reset.py -q` + docs test.

### B9. Concurrent vault-sync duplication  (P2)

File: `app/vault_sync.py` (~:239).
Steps: module dict `_SYNC_LOCKS: dict[int, threading.Lock]`; the sync
entry point takes the world's lock (create-if-missing) and returns a
"sync already running" result instead of interleaving.
Test: two threads syncing the same world — second returns the busy
result, chunks not duplicated.
Verify: `-m pytest tests/test_knowledge*.py -q` (find exact filename).

### B10. Autolink name-map caching  (P2)

File: `app/main.py` `_autolink_name_map` (~:4839).
Steps: memoize per `(world_id, is_gm, max_updated_at, exclude_id)` with a
small module-level dict + lock, invalidated by the max-updated_at key
(falls out naturally when entities change). Keep the visibility filter
OUTSIDE the cache (per-request shares differ — cache only the raw
name→id map per (world_id, exclude_id, max_updated_at) and apply
`_filter_visible_entities` per request on the referenced ids). If that
restructuring is too invasive for one step, cache only the GM-tier map
and leave player/share renders uncached — say so in the commit.
Tests: `tests/test_autolink_entities.py` must stay green; add a
map-identity assert (two calls, one query) only if straightforward.
Verify: `-m pytest tests/test_autolink_entities.py -q` + full suite.

### B11. Alias hygiene on save  (P2)

Files: `app/main.py` entity save routes (grep `aliases` form reads,
~:5370 and ~:5469).
Steps: normalize on save — split on comma, strip, drop empties and
duplicates, cap each at 64 chars, cap the list at 20 entries, rejoin.
Tests: save with " a , a ,, " + a 200-char alias → stored as "a" and the
alias truncated/skipped per the caps.
Verify: `-m pytest tests/test_entities*.py -q` (find files by grep).

### B12. `/api/facts/parse` input cap  (P2)

File: `app/routers/facts.py` (~:298) — reject `text` over 200_000 chars
with 400 "too long to parse in one request". Test: 200_001-char body →
400. Verify: `tests/test_facts.py`.

### B13. Session summary/transcript stripping  (P2)

Files: `app/routers/sessions.py` — `summary.md` download route (~:351)
and transcript download route (~:380): for non-GM, run
`strip_gm_only(...)` on the served text. The summary textarea on
`sessions/detail.html` (~:45) is the GM's edit surface — leave it, but
wrap the read-only render below it (~:53 region, check what renders the
saved summary for players) with `|md_for(request)` if it uses `|md`.
Tests: player with sessions read downloads a summary containing
`[gmonly]x[/gmonly]` → served text has no `x`.
Verify: `-m pytest tests/test_sessions*.py -q`.

### B14. Doc rot + judgment notes  (P2)

1. `app/main.py` `_is_player_safe` comment (~:568) claims Combat/Boards
   have no player_section entries — both now do; update the comment.
2. Add a comment at the `/characters` allowlist entry (~:347) stating
   own-character POSTs deliberately stay reachable at characters=none
   (own data, like sheets).
No tests. Verify: full suite (comment-only).

---

## Definition of done for this plan

Every step committed separately, full suite green after each, both GitHub
workflows green on `main` after each push, and a final summary commit (or
PR description) listing which steps were skipped and why.
