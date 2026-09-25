# AUDIT_PLAN_NEXT.md — fresh code audit, late September 2026 (round 2)

The previous plan in this file (23 items) is **fully implemented** — verified
item-by-item against the tree and deleted in its entirety. This document is
the follow-up: a fresh six-area audit (password reset & auth, the section
permission matrix, streaming exports/backup-restore, hybrid RAG + knowledge
graph, rendering/visibility for `[gmonly]`+autolinking, and facts/background
jobs) over the features that shipped after that first audit.

**Fixed same-day** (committed together with this file — don't re-derive):
search/hover-preview `[gmonly]` leaks, the `md_for(request)` filter for
quest/party/fact/PC-note renders, `Quest`/`Fact` `visible_to_players` on the
web routes, `/api/facts/from-job` world scoping, GM-only `use_rag` opt-in on
assistant-reachable job routes, corrupt chat/image job JSON bricking boot,
restore staging order + WAL-safe aside copy + incomplete-stage cleanup,
`/rules/download.md` gating+stripping, non-GM `surface` clamping on
`/api/ai/stream`, alias-map determinism, vault dotdir/node_modules pruning,
`GM_PASSWORD_RESET`-without-`GM_EMAIL` warning, and the diagnostics
cooldown fresh-boot sentinel bug (CI failure).

Everything below is **left to do**, in priority order. Line numbers are as
of this file's commit; grep the quoted function name if they drift.

---

## Wave 1 — remaining P1s

### 1. Enforce the section permission matrix on MCP read tools

`app/mcp_server.py` — `list_facts` (:137), `list_quests` (:223), `get_rules`
(:456), `list_sessions`/`get_session` (:475, :500), `get_entity`/
`search_entities` (:293, :203) never call `world_can_view_section`. Facts/
quests/sessions default to player "none", so a player API token reads exactly
what the web UI 403s — contradicting the module docstring's "identical
GM/player boundary as the web UI". `get_entity` also skips the per-kind
`kind_{kind}` gate the web detail page enforces. **Fix:** resolve the
world's section levels inside `_mcp_auth_wrapper` (note it never sets
`request.state.is_assistant`, so assistant tokens conservatively read as
player) and check `facts`/`quests`/`sessions`/`session_log`/`rules`/
`kind_{kind}` in those tools. Add cases to `tests/test_mcp.py`.

### 2. Enforce assistant edit-tier dial-downs on Tier B write routes

A GM can set a section's assistant level to "read"/"none" in Settings, and
the GETs honor it — but these POSTs are allowlisted by `_is_assistant_safe`
(`app/main.py`'s assistant block) with **no handler gate**, so an assistant
dialed down can still write: facts (`app/routers/facts.py` fact_create/
edit/delete + `/api/facts/bulk|parse|folk-tale|parse-job`), gallery, session
writes, boards (`board_new_post`/`board_save`), maps/import/bulk-edit,
pages/audio/video POSTs, background-job creates. Only quests/calendar/
tables/parties honor `world_can_edit_section` today. **Fix:** add the same
`world_can_edit_section(request, world, "<section>")` check to those
handlers. Extend `tests/test_player_section_access.py`'s dial-down cases
(the quests one at :403-412 is the pattern).

### 3. Close the request-scoped pool holds across AI calls (newest paths)

The known bug class (see `docs/LIVE_RECORDING_AUDIT.md` item 12, fixed only
for live-append/live-download): these handlers hold their `Depends(get_db)`
checked-out connection across a minutes-long Ollama/Whisper await —
`app/routers/facts.py` folk-tale (~:353→await), parse (~:317→await),
`app/routers/sessions.py` sync summarize routes, `app/routers/video.py`
transcribe. With the 5+10 pool this is 15 concurrent requests from
loop-freeze territory. **Fix:** the bookend pattern (`sessions.py:1214-1258`
is the in-repo exemplar): read what's needed, release, `await`, re-open a
short-lived `SessionLocal()` to write.

---

## Wave 2 — P2s, data integrity and hardening

### 4. Restore-upload decompression caps (`app/main.py` admin_backup_restore_stage)
`await file.read()` buffers the zip; every `zf.read(name)` inflates an entry
wholly in memory. Cap entry count + total uncompressed size via
`zf.infolist()` file_sizes before reading. GM-only, so P2 — but a GM's
honest multi-GB backup is enough to OOM the container.

### 5. Restore staging races (`app/main.py` staging routes)
Two concurrent restores, or restore racing cancel, interleave
rmtree/mkdir/extract. Module-level `threading.Lock`, or extract to a temp
dir and atomically rename into `RESTORE_STAGING_DIR`.

### 6. Snapshot-name TOCTOU (`app/backups.py:51-58`)
Scheduler thread and manual `POST /api/backups/run` can pass
`candidate.exists()` for the same second; the second `VACUUM INTO` fails
("file already exists") → 500. Lock around `run_backup_once` or retry with
a new suffix on `sqlite3.OperationalError`.

### 7. Deeper staged-DB schema check (`app/main.py` staging validation)
Only `SELECT COUNT(*) FROM worlds` runs; a much-older-schema backup stages
fine then crash-loops boot in `_migrate` until the aside copy is restored
by hand. Check `PRAGMA user_version` / a required-column set before staging.

### 8. GM recovery for a member's lost 2FA device (`app/routers/account.py`)
A player who lost authenticator + backup codes is unrecoverable without raw
SQLite surgery: password reset doesn't help (login stalls at `/login/2fa`),
and the only TOTP-disable route is self-service behind that gate
(`account_2fa_disable`). Add a GM-only "clear two-step auth for member"
action next to reset-password on the Members table (also clearing
TrustedDevice rows). Tests: `tests/test_password_reset.py` (which also
wants: 2FA stays enforced after a member reset; reset cannot target the GM).

### 9. Concurrent vault-sync duplication (`app/vault_sync.py` ~:239)
Two overlapping `POST /api/knowledge/sync` runs interleave delete→insert→
commit; no unique index on `(world_id, source_path, heading)`. Per-world
lock or unique constraint + upsert.

### 10. Autolink name-map cost (`app/main.py` `_autolink_name_map`)
Every entity-detail/private-notes render queries all world entities and
compiles one alternation regex of every name+alias+variant — O(text×
alternatives) per chunk on top. Cache keyed on
`(world_id, viewer-signature, max(Entity.updated_at))`.

### 11. Alias hygiene on save (`app/main.py` entity save routes)
No length/count caps on `Entity.aliases` (SQLite ignores `String(512)`);
normalize/dedupe on save, cap at a sane count (~20) and length (~64).

### 12. `/api/facts/parse` input cap (`app/routers/facts.py` ~:298)
No bound on `text`; a huge paste becomes thousands of multi-minute chunk
calls inside one HTTP request. Reject over ~200k chars with a clear message.

### 13. Session summary/transcript stripping for non-GMs (`app/routers/sessions.py`)
The summary textarea + `summary.md`/transcript downloads serve
`[gmonly]`-tagged text raw to players with sessions-read (rendered surfaces
are fixed; these are the remaining two).

### 14. Doc rot + judgment calls
- `app/main.py` `_is_player_safe` comment (~:568) claims Combat/Boards have
  no player_section entries — both now do.
- Decide & document: characters="none" still allows own-character POSTs
  (`_can_manage_character`) — deliberate (own data, like sheets), but say so
  at the allowlist.
- Decide & document (or implement): video deliberately out of broadcast
  scope — already documented in `video.py`'s docstring; nothing to do.

---

## Not findings — verified correct (round 2)

- Password reset: temp password `secrets.token_urlsafe(12)`, PBKDF2 600k,
  shown once, never in URLs/logs; no enumeration surface; login throttled
  8/300s per (ip,email); `session_version` bump + trusted-device wipe on
  every reset path; GM_PASSWORD_RESET keyed on GM_EMAIL+is_gm.
- Streaming builders: all six use no-DB/eager-loaded/fresh-SessionLocal
  patterns; producer threads daemon + STALL_TIMEOUT-bounded; zip-slip
  containment via resolve()+is_relative_to; boot apply before first
  connection with atomic os.replace.
- RAG core: FTS MATCH built from `\w+` tokens quote-escaped with ILIKE
  fallback; `smart_world_context` threads player visibility + strips
  `[gmonly]`; vault/graph blocks hard-gated off for non-GMs; AiInstruction
  `applies_to_players` defaults secure; knowledge routes GM-only with
  cross-world id validation.
- Autolink: redaction-before-autolink ordering at all three sites;
  visibility via `_filter_visible_entities` (hidden entities, per-player
  shares, cross-world); operates on escaped text skipping a/code/pre — no
  XSS path found.
- Jobs: boot order sweep→orphan-sweep→resume; no double-start; stragglers
  swept at both ends; image orphan cleanup complete; semaphore-held spans
  use short-lived `_set` sessions throughout `audio_jobs.py`.
- `/admin/diagnostics/*`, worlds/importer routes, MCP mutating tools
  (`_require_gm` at call time): all correctly gated.
