# API Reference

Every HTTP route exposed by nd-world (`app/main.py` + `app/routers/*.py`), grouped by
feature area, plus the MCP server's tools (`app/mcp_server.py`). Generated from a full
audit of the route table — **366 HTTP routes** and **8 MCP tools** as of this writing.

This is a reference for developers and AI agents working on the codebase, not
end-user documentation — see the [README](../README.md) for how to use the app
itself, and [AGENTS.md](../AGENTS.md) / [AI_ENTITY_GUIDE.md](AI_ENTITY_GUIDE.md) /
[AI_SCHEMATIC_GUIDE.md](AI_SCHEMATIC_GUIDE.md) for content-authoring conventions.

## Auth model

There is no per-route auth code sprinkled through the routers — a single
`@app.middleware("http")` function, `auth_gate` (`app/main.py`), is the entire
authorization boundary:

| Access level | Meaning |
|---|---|
| **Public** | No login required — reachable before authentication (`/login`, `/join/{code}`, `/health`, static/upload assets). |
| **Player** | Any logged-in account (GM or player) may call it. Reachable only because the route is explicitly listed in `_is_player_safe()` (`app/main.py`) — a table-driven allowlist, regression-tested by `tests/test_player_safe.py`. |
| **GM** | Requires `User.is_gm == True`. This is the **default** — any route not explicitly allowlisted in `_is_player_safe()` is GM-only automatically, including every brand-new route added without touching that function. Non-GM requests get a 403 (JSON for `/api/*`, an HTML page otherwise). |

A handful of routes carry their own extra checks inside the handler on top of this
(e.g. a player can only move *their own* schematic token, or edit *their own*
character) — those are called out inline below where relevant.

Most pages are also **world-scoped**: the active `World` is resolved from the
`active_world` cookie or a `?w=<slug>` query parameter (`app/deps.py`'s
`get_world_ctx`), and a GM only ever sees their own worlds while a player only sees
worlds they've been invited into (`WorldMembership`).

## Table of contents

- [Health & Static](#health--static)
- [Auth](#auth)
- [Account](#account)
- [Worlds](#worlds)
- [Home Page](#home-page)
- [Custom Kinds (Categories)](#custom-kinds-categories)
- [Entities](#entities)
- [Entity Field Templates](#entity-field-templates)
- [Characters](#characters)
- [Character Sheet Templates](#character-sheet-templates)
- [Races & Professions](#races--professions)
- [Quests](#quests)
- [Sessions & Session Log](#sessions--session-log)
- [Facts & Chronicler](#facts--chronicler)
- [Combat](#combat)
- [Parties](#parties)
- [Calendar](#calendar)
- [Maps](#maps)
- [Schematics](#schematics)
- [Investigation Boards](#investigation-boards)
- [King in Yellow & Dreamlands](#king-in-yellow--dreamlands)
- [Handouts](#handouts)
- [Random Tables](#random-tables)
- [Rules](#rules)
- [Search](#search)
- [Import / Export](#import--export)
- [AI — Chat & World-Building](#ai--chat--world-building)
- [AI — Image Generation](#ai--image-generation)
- [Settings](#settings)
- [Admin & Uploads](#admin--uploads)
- [MCP Server](#mcp-server)

---

## Health & Static

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/health` | Public | Liveness probe — used by Docker/Compose healthchecks. |
| GET | `/uploads/{filepath}` | Player | Serves an uploaded file (portraits, note/entity images, map images) from the `/data/uploads` volume. |

## Auth

`app/routers/auth.py`

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/login` | Public | Login form. |
| POST | `/login` | Public | Authenticates by email/password. If the account has two-step auth enabled (`User.totp_enabled`), stashes a pending marker in the session and redirects to `/login/2fa` instead of finishing login; otherwise sets the session cookie and redirects to `?next=`. |
| GET | `/login/2fa` | Public | Second-step form (code from an authenticator app or a backup code) — only reachable with a pending login from `/login`, `/api/login`, or `/join/{code}`; redirects to `/login` if nothing is pending. |
| POST | `/login/2fa` | Public | Verifies the TOTP/backup code for the pending login; on success sets the session cookie and redirects to the original `?next=` (or completes a pending invite redemption). Rate-limited per account (8 attempts / 5 min) via a process-local dict, same pattern as the password login lockout below. |
| POST | `/api/login` | Public | JSON login for non-browser clients (the NeonDragonsApp Android app) — same credential check, returns a JSON body instead of a redirect. Body may include `totp_code` (string). If the account has two-step auth enabled and `totp_code` is missing/incorrect, responds `401` with `{"ok": false, "requires_2fa": true, "detail": "..."}` instead of logging in — the client re-sends the same request with `totp_code` filled in (a TOTP code or a backup code). Same per-account rate limit as `/login/2fa`. |
| GET | `/logout` | Player | Clears the session and redirects to `/login`. |
| GET | `/join/{code}` | Public | Invite-redemption form for a `InviteCode` — lets a new player create an account (or log an existing one in) and joins them to the issuing world. |
| POST | `/join/{code}` | Public | Submits the join form: creates the account, redeems the invite, creates the `WorldMembership`. In "log an existing account in" mode, same two-step gate as `/login` — redirects to `/login/2fa` first if the account has it enabled, and the invite is redeemed only after the code is verified. |

## Account

`app/routers/account.py` — any logged-in user's own profile settings.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/account` | Player | Profile page: display name, password change, two-step auth, MCP access tokens. |
| POST | `/account/name` | Player | Updates the caller's own display name. |
| POST | `/account/password` | Player | Changes the caller's own password; bumps `User.session_version`, invalidating every other logged-in session for that account. |
| GET | `/account/2fa/setup` | Player | Starts two-step auth setup: generates a TOTP secret (stashed in the session, not saved yet) and renders a QR code (`app/totp.py`, rendered server-side as an inline data URI — no third-party call) plus the manual entry key. No-ops (redirects to `/account`) if already enabled. |
| POST | `/account/2fa/setup` | Player | Confirms setup with a code from the authenticator app; on success saves `User.totp_secret`/`totp_enabled=True` and 8 single-use backup codes (shown once, stored as sha256 hashes). |
| POST | `/account/2fa/disable` | Player | Turns off two-step auth for the caller's own account (requires re-entering the current password; rate-limited like `/account/password`). Clears the secret and backup codes. |
| POST | `/account/2fa/backup-codes/regenerate` | Player | Replaces the caller's backup codes with a fresh set of 8 (requires the current password); the old codes stop working immediately. |
| POST | `/account/tokens/new` | Player | Issues a new MCP bearer token (`ApiToken`) for the caller — the raw token is shown once at creation and only its sha256 hash is stored. |
| POST | `/account/tokens/{token_id}/revoke` | Player | Revokes (deletes) one of the caller's own MCP tokens. |

## Worlds

`app/main.py` — world creation/management, invites, membership, per-world private notes.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/worlds` | GM | List/manage all of the GM's worlds. |
| POST | `/worlds/new` | GM | Creates a new `World`. |
| POST | `/worlds/{world_id}/delete` | GM | Deletes a world and every row/file it owns (entities, characters, maps, schematics, uploads, etc.) — irreversible. |
| GET | `/worlds/switch/{slug}` | Player | Sets the `active_world` cookie to this world and redirects to `?next=` (or `/`) — the world-switcher dropdown's target. |
| GET | `/worlds/{world_id}/edit` | GM | World settings form: name, accent color, party-visibility toggle, invites, members list. |
| POST | `/worlds/{world_id}/edit` | GM | Saves world settings. |
| POST | `/worlds/{world_id}/invites/new` | GM | Creates a new `InviteCode` (optionally time- or use-limited). |
| POST | `/worlds/{world_id}/invites/{invite_id}/revoke` | GM | Revokes an unused invite link. |
| POST | `/worlds/{world_id}/members/{user_id}/remove` | GM | Removes a player's `WorldMembership` from this world. |
| GET | `/worlds/{world_id}/notes/{user_id}` | Player | A private GM↔player note thread — visible to the GM and that one player only. |
| POST | `/worlds/{world_id}/notes/{user_id}/new` | GM | Posts a new private note to a player. |
| POST | `/worlds/{world_id}/notes/{user_id}/{note_id}/delete` | GM | Deletes a private note. |
| POST | `/folders/rename` | GM | Renames (or clears, moving entities to Unfiled) a folder across all entities of a kind. |
| GET | `/worlds/{world_id}/export` | GM | Downloads a single-world JSON export (entities + embedded images). |
| POST | `/worlds/{world_id}/import` | GM | Imports a previously-exported world JSON, merging into this world. |
| POST | `/api/worlds` | GM | JSON API: creates a world (used by the NeonDragonsApp companion flow). |

## Home Page

`app/main.py` + `app/routers/home_content.py` — the customizable per-world landing page (stat tiles + GM-editable Quick Link sections).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/` | Player | World home page: entity-kind stat tiles with live counts, plus the GM's custom Quick Link sections. |
| GET | `/worlds/{world_id}/home/edit` | GM | Edit form for the home page's Quick Link sections. |
| POST | `/worlds/{world_id}/home/edit` | GM | Saves the full edited/reordered section list. |
| POST | `/api/worlds/{world_id}/home/quick-link` | GM | Appends one link to a section without going through the full edit form (used by "drag a nav tab onto the home page"). |

## Custom Kinds (Categories)

`app/routers/kinds_admin.py` — lets a GM define brand-new entity categories per world (e.g. "Vehicles") that behave like the 8 built-in kinds (nav tab, home stat tile, entity form option).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/worlds/{world_id}/kinds/edit` | GM | Management page: add/rename/reorder/delete custom kinds. |
| POST | `/worlds/{world_id}/kinds/edit` | GM | Saves the full edited/reordered custom-kind list (labels, icons, subtype suggestions). |
| POST | `/worlds/{world_id}/kinds/new` | GM | Adds one new custom kind (slugifies the label into a permanent `custom_*` id). |
| POST | `/worlds/{world_id}/kinds/{kind_id}/delete` | GM | Deletes a custom kind — blocked (400) while any entity still uses it. |

## Entities

`app/main.py` — the core lore content model: Characters, Locations, Organizations, Creatures, Events, Items, Feats, Notes, plus any GM-defined custom kind.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/kind/{kind}` | Player | Paginated/searchable list of entities of one kind, filtered to what the caller may see. |
| GET | `/kind/{kind}/download.zip` | Player* | Downloads every visible entity of a kind as one `.md` file per entity, zipped. GM always; a player only once the world's `players_can_download_entities` toggle is on. |
| GET | `/kind/{kind}/download-selected.zip` | Player* | Same, but only the checkbox-selected entities (bulk-action bar), via `?id=` query params. |
| GET | `/entity/{entity_id}` | Player | Entity detail page: body, notes, links to other entities, image gallery. |
| GET | `/entity/{entity_id}/download.md` | Player* | Downloads the entity (body + visible notes) as a `.md` file. GM always; a player only once `players_can_download_entities` is on — independent of whether the entity itself is visible to them (`_entity_view_gate` still 404s a hidden/inaccessible entity first). |
| GET | `/api/entity/{entity_id}/preview` | Player | Hover-preview popup content (name/summary/image) for the base-template's link-hover feature. |
| GET | `/api/hover-preview/config` | Player | Instance-wide hover-preview timing/size settings (Settings > Options). |
| GET | `/new` | GM | New-entity form (kind chosen via query param). |
| POST | `/new` | GM | Creates an entity. |
| GET | `/entity/{entity_id}/edit` | GM | Edit form. |
| POST | `/entity/{entity_id}/edit` | GM | Saves entity edits (including per-player visibility overrides). |
| POST | `/entity/{entity_id}/delete` | GM | Deletes an entity. |
| POST | `/kind/{kind}/bulk-delete` | GM | Deletes many entities of one kind at once (checkbox multi-select UI). |
| POST | `/api/entities/bulk-visibility` | GM | Sets `visible_to_players` (and, for "specific players" mode, the per-player access list) across a selected batch of entities in one call — Settings > Visibility tab. |
| POST | `/entity/{entity_id}/link/{target_id}` | GM | Creates a bidirectional relationship link between two entities. |
| POST | `/entity/{entity_id}/unlink/{target_id}` | GM | Removes a link. |
| POST | `/entity/{entity_id}/notes/new` | GM | Adds a note to an entity — independently hideable from the entity's own visibility. |
| POST | `/entity/{entity_id}/notes/{note_id}/toggle` | GM | Toggles a note's `visible_to_players` flag. |
| POST | `/entity/{entity_id}/notes/{note_id}/delete` | GM | Deletes a note. |
| POST | `/api/upload-image` | GM | Uploads an image and returns its URL — backs the rich-text formatting toolbar's image button on GM-only fields (entity body/notes). |

## Entity Field Templates

`app/main.py` — structured custom fields/stat blocks layered on top of an entity's free-text body.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/entity-templates` | GM | List of Field Templates. |
| GET | `/entity-templates/new` | GM | New-template form. |
| POST | `/entity-templates/new` | GM | Creates a Field Template (fields: text/number/textarea/dropdown/repeatable list). |
| GET | `/entity-templates/{tpl_id}/edit` | GM | Edit form. |
| POST | `/entity-templates/{tpl_id}/edit` | GM | Saves template edits. |
| POST | `/entity-templates/{tpl_id}/delete` | GM | Deletes a template. |

## Characters

`app/routers/characters.py` — Player Character sheets, the N&D creation wizard, and the sync API used by the NeonDragonsApp Android app.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/characters` | Player | List of characters in the active world (a player sees the roster read-only if the GM enabled party visibility; always sees their own). |
| GET | `/characters/new` | Player | The guided Race → Profession → Stats → Feats → Equipment creation wizard. |
| GET | `/api/characters/catalog` | Player | Race/profession/feat/equipment catalog JSON that powers the wizard frontend. |
| POST | `/api/characters/upload-image` | Player | Image upload for the shared rich-text toolbar on player-writable character fields (reuses the portrait-upload pipeline). |
| POST | `/characters/new` | Player | Submits the finished wizard, creating a `PlayerCharacter`. |
| GET | `/characters/{pc_id}` | Player | Character sheet — stats, HP/Shock/PP/MP tracking, equipment, feats. |
| POST | `/characters/{pc_id}/owner` | GM | Links (or unlinks) a `PlayerCharacter` to a connected player's account. |
| GET | `/characters/{pc_id}/edit` | Player | Edit form (own character, or any if GM). |
| POST | `/characters/{pc_id}/edit` | Player | Saves character edits. |
| POST | `/characters/{pc_id}/delete` | Player | Deletes a character. |
| GET | `/characters/{pc_id}/export.ndc` | Player | Downloads a `.ndc` file — the interchange format also used by NeonDragonsApp and NeonDragonsEditor. |
| GET | `/characters/{pc_id}/export.foundry.json` | Player | Downloads a Foundry VTT–compatible actor JSON. |
| POST | `/api/characters/{pc_id}/hp-async` | Player | Live HP update (character sheet's +/- controls, no page reload). |
| POST | `/api/characters/{pc_id}/shock` | Player | Live Shock update. |
| POST | `/api/characters/{pc_id}/pp` | Player | Live Power Points update. |
| POST | `/api/characters/{pc_id}/mp` | Player | Live Mana Points update. |
| POST | `/api/characters/{pc_id}/xp` | Player | Adjusts XP (and any level-derived stats). |
| POST | `/api/characters/{pc_id}/equipment` | Player | Updates the equipment list. |
| POST | `/api/characters/{pc_id}/feats` | Player | Updates the selected feats list. |
| GET | `/api/characters/{pc_id}/sync` | Player | Full character JSON for the NeonDragonsApp sync flow (pull). |
| PUT | `/api/characters/{pc_id}/sync` | Player | Overwrites the character from a NeonDragonsApp sync payload (push). |
| POST | `/api/worlds/{world_id}/characters/sync` | Player | Creates a new character from a NeonDragonsApp sync payload (upsert-by-name). |
| GET | `/api/me` | Player | The caller's own user info (id, display name, is_gm, active world) — used by the Android app and frontend JS. |
| POST | `/api/characters/roll` | Player | Server-side dice roller (stat/skill checks) used by the character sheet. |

## Character Sheet Templates

`app/routers/characters.py` — GM-authored alternate/extended character systems ("Custom Character Systems" in the README), e.g. the built-in Asterion system.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/characters/templates` | GM | List of Sheet Templates. |
| GET | `/characters/templates/new` | GM | New-template form. |
| POST | `/characters/templates/new` | GM | Creates a template — either extends the N&D sheet with extra sections, or fully replaces it with a custom field set. |
| GET | `/characters/templates/{tpl_id}/edit` | GM | Edit form. |
| POST | `/characters/templates/{tpl_id}/edit` | GM | Saves template edits. |
| POST | `/characters/templates/{tpl_id}/delete` | GM | Deletes a template. |
| GET | `/api/characters/templates` | Player | JSON list of templates, for the creation wizard's "create with this template" flow. |

## Races & Professions

`app/routers/races.py`, `app/routers/professions.py` — per-world homebrew race/profession catalog management, layered on the bundled N&D catalog.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/races` | Player | List of races available in the active world (built-in + homebrew). |
| GET | `/races/new` | GM | New homebrew race form. |
| POST | `/races/new` | GM | Creates a homebrew race. |
| POST | `/races/add-builtin` | GM | Adds one built-in race into this world's catalog. |
| POST | `/races/add-all-builtin` | GM | Adds every built-in race at once. |
| POST | `/races/{race_id}/delete` | GM | Removes a homebrew race. |
| GET | `/professions` | Player | List of professions available in the active world. |
| GET | `/professions/new` | GM | New homebrew profession form. |
| POST | `/professions/new` | GM | Creates a homebrew profession. |
| POST | `/professions/add-builtin` | GM | Adds one built-in profession. |
| POST | `/professions/add-all-builtin` | GM | Adds every built-in profession at once. |
| POST | `/professions/{profession_id}/delete` | GM | Removes a homebrew profession. |

## Quests

`app/routers/quests.py`

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/quests` | GM | Quest list for the active world. |
| GET | `/quests/new` | GM | New quest form. |
| POST | `/quests/new` | GM | Creates a quest. |
| GET | `/quests/{quest_id}` | GM | Quest detail. |
| POST | `/quests/{quest_id}/edit` | GM | Saves quest edits. |
| POST | `/api/quests/{quest_id}/status` | GM | Updates quest status (active/completed/failed). |
| POST | `/quests/{quest_id}/delete` | GM | Deletes a quest. |

## Sessions & Session Log

`app/routers/sessions.py` — GM session prep/recap tracking, its AI-assist toolbar, and the player-facing filtered session log.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/sessions` | GM | Session list for the active world. |
| GET | `/sessions/new` | GM | New session form. |
| POST | `/sessions/new` | GM | Creates a `GameSession`. |
| GET | `/sessions/{session_id}` | GM | Session detail: prep checklist, XP/loot log, recap (with the AI toolbar). |
| POST | `/sessions/{session_id}/edit` | GM | Saves session edits, including the raw `summary` recap text. |
| POST | `/sessions/{session_id}/delete` | GM | Deletes a session. |
| POST | `/api/sessions/{session_id}/prep/toggle` | GM | Toggles a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/add` | GM | Adds a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/{idx}/delete` | GM | Removes a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/generate` | GM | AI: drafts a prep checklist from the prior session's recap/facts, the world's open quests, and the assigned party — returned for review, not written until confirmed items are POSTed to `prep/add`. |
| POST | `/api/sessions/{session_id}/xp` | GM | Records XP awarded this session. |
| POST | `/api/sessions/{session_id}/loot/transfer` | GM | Transfers loot from the session log to a party/character. |
| POST | `/api/sessions/ai/expand-notes` | GM | AI: expands terse GM notes (from the Summary textarea) into a written narrative recap via the local Ollama model. |
| POST | `/api/sessions/ai/condense-recap` | GM | AI: condenses/tightens an existing recap. |
| POST | `/api/sessions/ai/summarize-from-audio` | GM | AI: transcribes an uploaded/mic-recorded session audio clip via Whisper (biased by the world's name glossary) and summarizes it into a recap — session-independent, usable on the New Session form. |
| POST | `/api/sessions/ai/summarize-from-audio/chunk`, `/api/sessions/ai/summarize-from-audio/complete` | GM | Chunked variant of the above for a recording over the direct-upload size threshold. |
| POST | `/api/sessions/ai/audio-jobs`, `/api/sessions/ai/audio-jobs/chunk`, `/api/sessions/ai/audio-jobs/complete` | GM | Transcribe (and, for `session_recap`, summarize) a session recording as a durable background job instead of blocking the upload request. |
| GET | `/api/sessions/ai/audio-jobs/{job_id}`, `/api/sessions/ai/audio-jobs` | GM | Poll one session audio job, or list them. |
| POST | `/api/sessions/{session_id}/live-transcript/append` | GM | Transcribes one ~1-minute chunk of a live session recording via Whisper and appends it to the session's running live transcript. |
| POST | `/api/sessions/{session_id}/live-transcript/clear` | GM | Clears the accumulated live transcript. |
| POST | `/api/sessions/{session_id}/ai/summarize-live-transcript` | GM | AI: summarizes the accumulated live transcript into a recap. |
| POST | `/api/sessions/{session_id}/ai/summarize-from-facts` | GM | AI: weaves this session's logged `Fact` rows (all of them, secret or not) into a narrative recap. |
| GET | `/session-log` | Player | Player-facing list of sessions (title/date/number only — never the GM's raw summary). |
| GET | `/session-log/{session_id}` | Player | Player-facing session page; fetches its AI recap client-side. 404s if the session's world isn't one the caller belongs to. |
| POST | `/api/session-log/{session_id}/recap` | Player | AI: synthesizes a recap from only the `Fact` rows marked `visible_to_players` for this session — the GM's raw `summary` is never sent to this endpoint's caller or to the model on their behalf. GMs calling it get every fact, secret or not. |

## Facts & Chronicler

`app/routers/facts.py`, `app/routers/chronicler.py` — a discrete, visibility-flagged log of "what happened" (as opposed to the free-text session recap), and an AI chat assistant that answers questions from it.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/facts` | GM | Facts list + quick-add form + the recap→facts AI parser panel. |
| POST | `/facts/new` | GM | Creates a `Fact` (content + `visible_to_players` flag + optional linked session). |
| POST | `/facts/{fact_id}/edit` | GM | Saves fact edits. |
| POST | `/facts/{fact_id}/delete` | GM | Deletes a fact. |
| POST | `/api/facts/parse` | GM | AI: turns a rough recap paste into draft facts (content + suggested visibility) via the local model — returned for review, **not written to the DB**. |
| POST | `/api/facts/bulk` | GM | Bulk-inserts facts — the recap-review UI's "Confirm & Save" action. |
| GET | `/chronicler` | Player | Chat page for the Chronicler assistant. |
| POST | `/api/chronicler/ask` | Player | Answers a question using only facts/entities visible to the caller's role — the filtering happens server-side before anything reaches the model, not as a prompt instruction. |

## Combat

`app/routers/combat.py`

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/combat` | GM | Combat encounter list. |
| POST | `/combat/new` | GM | Creates a `CombatSession`. |
| GET | `/combat/{combat_id}` | GM | Initiative tracker UI. |
| GET | `/api/combat/{combat_id}/state` | GM | Current combat state JSON (combatants, initiative order, round). |
| POST | `/combat/{combat_id}/state` | GM | Saves combat state (HP, conditions, turn order). |
| POST | `/combat/{combat_id}/delete` | GM | Deletes a combat session. |
| POST | `/combat/{combat_id}/link-session` | GM | Associates this combat with a `GameSession`. |
| POST | `/combat/{combat_id}/unlink-session` | GM | Removes that association. |
| POST | `/api/combat/{combat_id}/sync-characters` | GM | Pulls current HP/stats from linked `PlayerCharacter` rows into the combatant list. |

## Parties

`app/routers/parties.py`

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/parties` | GM | Party list. |
| POST | `/parties/new` | GM | Creates a party (a named group of characters). |
| GET | `/parties/{party_id}` | GM | Party detail: members, shared loot, location. |
| POST | `/parties/{party_id}/edit` | GM | Saves party edits (membership, name). |
| POST | `/parties/{party_id}/delete` | GM | Deletes a party. |
| POST | `/api/parties/{party_id}/loot` | GM | Updates shared party loot/currency. |
| POST | `/api/parties/{party_id}/location` | GM | Sets the party's current in-world location. |
| POST | `/api/parties/{party_id}/launch-combat` | GM | Creates a `CombatSession` pre-populated with this party's characters. |

## Calendar

`app/routers/calendar.py` — an in-world calendar (custom epoch/months) for tracking campaign time.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/calendar` | GM | Calendar view with logged events. |
| GET | `/calendar/config` | GM | Calendar configuration form (month names/lengths, starting date). |
| POST | `/calendar/config` | GM | Saves calendar configuration. |
| POST | `/api/calendar/events` | GM | Adds an event on a given in-world date. |
| POST | `/api/calendar/events/{event_id}/delete` | GM | Deletes an event. |
| POST | `/api/calendar/advance` | GM | Advances the in-world "current date" by N days. |

## Maps

`app/main.py` — image-based maps with markers and region overlays.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/maps` | Player | Map list for the active world. |
| GET | `/maps/new` | GM | New map form. |
| POST | `/maps/new` | GM | Creates a map. |
| POST | `/maps/{slug}/rename` | GM | Renames a map. |
| POST | `/maps/{slug}/delete` | GM | Deletes a map. |
| POST | `/maps/{slug}/upload` | GM | Uploads/replaces the map's background image. |
| GET | `/maps/{slug}` | Player | Map viewer with markers/regions. |
| POST | `/api/maps/{slug}/overlay` | GM | Saves marker/region overlay data. |

## Schematics

`app/main.py` — an SVG-based canvas editor for battle-map/dungeon layouts, with a live token-based player view.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/maps/schematic/new` | GM | New schematic form. |
| POST | `/maps/schematic/new` | GM | Creates a schematic. |
| GET | `/maps/schematic/{slug}` | GM | GM editor canvas. |
| POST | `/maps/schematic/{slug}/link-combat` | GM | Links the schematic to a `CombatSession` for token sync. |
| POST | `/maps/schematic/{slug}/unlink-combat` | GM | Removes the combat link. |
| POST | `/maps/schematic/{slug}/pull-combat` | GM | One-way sync Combat → Map: creates/refreshes a token for every combatant in the linked combat. |
| POST | `/maps/schematic/{slug}/push-combat` | GM | One-way sync Map → Combat: writes token HP/max HP/conditions back onto the linked combat's combatants. |
| GET | `/maps/schematic/{slug}/view` | Player | Read/interact-only player view (hides GM-only elements). |
| GET | `/maps/schematic/{slug}/view.json` | Player | Player-view state as JSON (polled by the view page for live updates). |
| POST | `/api/maps/schematic/{slug}/move-token` | Player | Moves a token — a non-GM player may only move the one token linked to their own `PlayerCharacter`. |
| POST | `/api/maps/schematic/{slug}/pickup-item` | Player | A player picks up an entire item-token stack into their own character's inventory. |
| POST | `/api/maps/schematic/{slug}/buy-item` | Player | A player buys one stock row from a merchant token, deducting currency and reducing stock (row-level locked against concurrent buys). |
| POST | `/maps/schematic/{slug}/grid` | GM | Saves the grid overlay type/config (none/square/hex). |
| POST | `/maps/schematic/{slug}/elements` | GM | Saves the full canvas element list (shapes, tokens, labels). |
| POST | `/maps/schematic/{slug}/upload` | GM | Uploads a background image. |
| POST | `/maps/schematic/{slug}/embed-image` | GM | Embeds a picked image file directly into an element (data-URI, no separate upload round trip) — backs the editor's 🖼 Embed Image tool. |
| POST | `/maps/schematic/{slug}/rename` | GM | Renames a schematic. |
| POST | `/maps/schematic/{slug}/delete` | GM | Deletes a schematic. |

## Investigation Boards

`app/main.py` + `app/routers/boards_generate.py` — node-and-edge relationship graphs for factions, story threads, or reference maps.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/boards` | GM | Board list. |
| GET | `/boards/new` | GM | New board form. |
| POST | `/boards/new` | GM | Creates a blank board. |
| GET | `/boards/{slug}` | GM | Board canvas (nodes/edges editor). |
| POST | `/boards/{slug}/save` | GM | Saves the node/edge layout. |
| POST | `/boards/{slug}/delete` | GM | Deletes a board. |
| GET | `/boards/{slug}/export` | GM | Exports the board as JSON. |
| POST | `/boards/generate-orgs` | GM | Auto-generates a faction/organization relationship board from `organization`-kind entities and their links (radial cluster layout, plus keyword-classified allies/enemies/controls/rivals edges inferred from entity text). |
| GET | `/api/orgs/graph` | GM | The same faction-graph data as JSON without saving a board — for external use. |
| POST | `/boards/generate-dreamlands` | GM | Generates a fixed, bundled 50-location geographic atlas of the Dreamlands (reference content, not derived from world data). |

## King in Yellow & Dreamlands

`app/routers/lore_extras.py` — two bundled GM-facing lore extras: a static Dreamlands write-up, and an AI-assisted King in Yellow play generator with a saved-play library used as style RAG. Both are **off by default** (`AppSettings.dreamlands_enabled` / `.king_in_yellow_enabled`) — a GM turns them on from Settings > System > Optional extras, which also un-hides their nav links under 🎯 Tools. Hitting either page URL directly while disabled renders a small "enable this in Settings" page instead of a 404, so an old bookmark/link doesn't just break.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/dreamlands` | GM | Dreamlands atlas write-up (links to the generated board above). Shows a disabled-state page unless `dreamlands_enabled`. |
| GET | `/king-in-yellow` | GM | King in Yellow page: generator + saved-play library. Shows a disabled-state page unless `king_in_yellow_enabled`. |
| GET | `/api/kiy/inspirations` | GM | Fetches public-domain King in Yellow research/reference material from several sources in parallel (each independently fault-tolerant) to seed generation. |
| GET | `/api/kiy/plays` | GM | Lists saved generated plays. |
| POST | `/api/kiy/plays` | GM | Saves a generated (or edited) play to the library. |
| DELETE | `/api/kiy/plays/{play_id}` | GM | Deletes a saved play. |
| POST | `/api/kiy/generate` | GM | Streams a newly generated two-act King in Yellow play (SSE), using fetched inspirations plus 1-2 random saved plays as style reference. |
| POST | `/api/kiy/build-model` | GM | Fine-tunes a local Ollama model from the entire saved-play library (requires a real local Ollama daemon — unavailable, e.g., on Android). |

## Handouts

`app/routers/handouts.py` — printable/shareable single-entity handout pages.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/handout/{entity_id}` | GM | Single printable handout for one entity. |
| GET | `/handouts` | GM | Gallery of handout-eligible entities. |
| POST | `/handouts/print` | GM | Generates a combined multi-entity printable page. |

## Random Tables

`app/routers/tables.py` — GM-authored roll tables (loot, encounters, NPC names, etc.).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/tables` | GM | Table list. |
| GET | `/tables/new` | GM | New table form. |
| POST | `/tables/new` | GM | Creates a table. |
| GET | `/tables/{table_id}/edit` | GM | Edit form. |
| POST | `/tables/{table_id}/edit` | GM | Saves table edits. |
| POST | `/tables/{table_id}/delete` | GM | Deletes a table. |
| POST | `/api/tables/{table_id}/roll` | GM | Rolls on a table and returns the result. |
| GET | `/tables/export` | GM | Exports all tables as JSON. |
| POST | `/tables/import` | GM | Imports tables from JSON. |

## Rules

`app/main.py` — the per-world core-rules document (falls back to bundled N&D rules).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/rules` | Player | Rendered rules page with an auto-generated table of contents. |
| GET | `/rules/download.md` | Player* | Downloads the rules as a `.md` file. GM always; a player only once the world's `players_can_download_rules` toggle is on (`world_edit.html`) — 403 otherwise. |
| GET | `/worlds/{world_id}/rules/edit` | GM | Rules edit form (raw Markdown). |
| POST | `/worlds/{world_id}/rules/edit` | GM | Saves rules Markdown. |
| POST | `/worlds/{world_id}/rules/import` | GM | Imports rules from a JSON file shaped `{"rules_md": "...markdown..."}`. |

## Search

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/search` | Player | Full-text search across entity names, tags, summaries, and body text, filtered to what the caller may see. |

## Import / Export

`app/main.py`, `app/routers/importer.py`, `app/routers/export.py` — bulk content import, and split/structured export for external tooling.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/import` | GM | Import page: JSON bulk import, bulk image matching, bulk AVIF/WebP re-encode. |
| POST | `/api/import` | GM | Legacy single-shot bulk JSON import endpoint. |
| POST | `/api/import/images` | GM | Bulk portrait/art import — files matched by filename to existing entities. |
| POST | `/api/import/detect` | GM | Inspects an uploaded JSON payload and classifies each item's likely kind/shape before import. |
| POST | `/api/import/execute` | GM | Executes a reviewed/confirmed bulk import. |
| POST | `/api/import/convert-images` | GM | Retroactively re-encodes every image already in the active world (entity/character art, schematic/board images, gallery albums, maps) to a target format (AVIF/WebP/PNG/JPG). Preserves each image's readable filename (see `app/uploads.py`'s `unique_upload_filename`) — only the extension changes — and rewrites every reference to that image consistently, even one shared across an entity and a gallery album. |
| GET | `/api/worlds/{world_id}/content-pack` | Player | A world's homebrew races/professions/feats/items as JSON, for the NeonDragonsApp Android app's content sync. |
| GET | `/export` | GM | **Export & Backup hub page** — one place linking Full Backup, World Book, World JSON (single/split), and Import, each with a one-line explanation of when to use it. Replaces the old scattered nav/world-card export buttons. |
| GET | `/export/book.zip` | GM | Downloads a zip containing a readable HTML "book" export of the active world's lore (entities, boards, maps, rules) — linked from the hub above. |
| GET | `/worlds/{world_id}/export/split` | GM | Split-export landing page — links to per-kind/per-resource JSON files below. |
| GET | `/worlds/{world_id}/export/rules.json` | GM | This world's rules as JSON. |
| GET | `/worlds/{world_id}/export/player-characters.json` | GM | All player characters in this world as JSON. |
| GET | `/worlds/{world_id}/export/entities/{kind}.json` | GM | All entities of one kind as JSON. |
| GET | `/worlds/{world_id}/export/templates/{template_id}.json` | GM | One Sheet Template as JSON. |

## AI — Chat & World-Building

`app/main.py` + `app/routers/ai.py` (prefix `/api/ai`) — the GM-only general chat/world-building assistant, model management, and generation helpers. Not to be confused with the player-facing [Chronicler](#facts--chronicler), which is a separate, visibility-filtered endpoint.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/ai` | GM | AI chat page (Chat / Image Gen / Models / Whisper / Starred tabs). |
| GET | `/api/ai/world-context` | GM | Keyword-search RAG context (relevant entities) for the current chat, unfiltered by player visibility. |
| POST | `/api/ai/world-context-smart` | GM | Same, backed by an FTS5 full-text index over entity name/summary/body/tags (falls back to plain `LIKE` if FTS5 is unavailable) — also returns `entities` (what was actually retrieved, for the RAG transparency panel/pinning). |
| POST | `/api/ai/save-note` | GM | Saves an AI chat response as a note on an entity. |
| POST | `/api/ai/generate/entity-smart` | GM | Generates a full draft entity (name/summary/body) from a prompt, with world context. |
| POST | `/api/ai/entity-from-text` | GM | Turns a pasted/dictated passage into a draft entity. |
| POST | `/api/ai/chat` | GM | Non-streaming chat completion. |
| POST | `/api/ai/stream` | GM* | Streaming chat completion (SSE) — GM always; a player may if the active world's `players_can_ask_ai` is on. Accepts a per-request `options` (temperature/top_p/etc, clamped) and a `surface` for per-surface default-model fallback; the SSE stream leads with a `note` event if the requested model had to be fuzzy-matched to an available one. |
| GET | `/api/ai/models` | GM | Lists available/known Ollama models with loaded/builtin flags. |
| GET | `/api/ai/resident` | GM | Models actually resident in memory (VRAM/RAM), for the Models tab's residency cockpit. |
| POST | `/api/ai/unload` | GM | Unloads a model from memory. |
| GET | `/api/ai/defaults` | GM | Per-surface (`chat`/`ask_ai`/`image`) default model ids. |
| POST | `/api/ai/defaults` | GM | Sets a surface's default model. |
| GET | `/api/ai/presets` | GM | Lists chat presets (saved `{label, model, system_extra, options}` bundles). |
| POST | `/api/ai/presets` | GM | Saves/updates a chat preset (upsert by label). |
| DELETE | `/api/ai/presets/{label}` | GM | Deletes a chat preset. |
| GET | `/api/ai/prompt-presets` | GM | Lists the prompt library for a scope (`chat` Quick Prompts or `image` Image Studio presets) — lazy-seeds the 8 default Quick Prompts on a world's first fetch. |
| POST | `/api/ai/prompt-presets` | GM | Adds a prompt library entry. |
| DELETE | `/api/ai/prompt-presets/{preset_id}` | GM | Deletes a prompt library entry. |
| POST | `/api/ai/benchmark` | GM | Runs a short fixed prompt against a model and reports Ollama's own tok/s throughput (generation + prompt-eval) and load time. |
| GET | `/api/ai/debug` | GM | Raw Ollama connection debug info. |
| POST | `/api/ai/models/add` | GM | Adds a model to the picker (built-in unhide, or a custom model id). |
| POST | `/api/ai/models/remove` | GM | Hides a built-in model or removes a custom one; can also delete it from Ollama. |
| POST | `/api/ai/models/reset` | GM | Restores all built-in models (clears the hidden list). |
| POST | `/api/ai/pull` | GM | Streams progress while pulling a model into Ollama (SSE). |
| GET/POST/DELETE | `/api/ai/sessions`, `/api/ai/sessions/{id}` | GM | AI Chat History — list, save, load, and delete a persisted chat conversation (`ChatSession`). |
| POST | `/api/ai/attachments/upload` | GM* | Attaches an image/audio/document to a chat message — audio is transcribed via Whisper and documents text-extracted inline. |
| POST | `/api/ai/attachments/upload/chunk`, `/api/ai/attachments/upload/complete` | GM* | Chunked variant of the above for an attachment over the direct-upload size threshold. |
| POST | `/api/ai/attachments/audio-jobs`, `/api/ai/attachments/audio-jobs/chunk`, `/api/ai/attachments/audio-jobs/complete` | GM* | Transcribe a chat voice attachment as a durable background job instead of blocking the upload request. |
| GET | `/api/ai/attachments/audio-jobs/{job_id}`, `/api/ai/attachments/audio-jobs` | GM* | Poll one attachment transcription job, or list them. |
| GET | `/api/ai/whisper/model-status` | GM | Whether the Whisper model is downloaded, and which known models exist. |
| POST | `/api/ai/whisper/pull` | GM | Streams progress while downloading a Whisper model (SSE). |
| POST | `/api/ai/whisper/activate` | GM | Switches the active Whisper model: writes a marker file the "whisper" Compose service reads on its next (re)start, and — if `hot_swap` (default true) and the file passes a basic format sanity check — also asks the running server to switch immediately via its own `/load` endpoint, no restart needed. Falls back to `restart_required: true` if the hot-swap can't happen. |
| GET | `/api/ai/whisper/glossary` | GM | The active world's Whisper name glossary (campaign vocabulary hinted to every session-recording transcription). |
| POST | `/api/ai/whisper/glossary` | GM | Saves the world's Whisper glossary. |
| GET | `/api/ai/whisper/language` | GM | The active world's pinned Whisper spoken-language code (e.g. `"ru"`), or `""` for auto-detect — applied to every session-recording transcription. |
| POST | `/api/ai/whisper/language` | GM | Saves the world's pinned Whisper language. |
| GET | `/api/ai/recap-instructions` | GM | The active world's extra steering for the recap-writing step (e.g. "write in Spanish") — applied to every session-recording recap, not transcription itself. |
| POST | `/api/ai/recap-instructions` | GM | Saves the world's recap instructions. |
| POST/GET | `/api/ai/chat/jobs` | GM | Runs a chat completion as a durable background job instead of live-streaming — same request shape as `/api/ai/stream` minus streaming; POST creates, GET lists recent jobs for the active world. |
| GET | `/api/ai/chat/jobs/{job_id}` | GM | Poll one chat job. |
| POST | `/api/ai/chat/jobs/{job_id}/cancel` | GM | Cancels an in-progress chat job. |
| DELETE | `/api/ai/chat/jobs/{job_id}` | GM | Deletes a finished chat job (400 if still in progress — cancel first). |
| POST | `/api/ai/generate/entity` | GM | Generates an expanded description for a named entity. |
| POST | `/api/ai/generate/npc` | GM | Generates an NPC backstory/personality. |
| POST | `/api/ai/generate/location` | GM | Generates a location description. |
| POST | `/api/ai/generate/quest` | GM | Generates a quest hook. |
| POST | `/api/ai/status` | GM | Ollama connectivity/model status. |
| GET | `/api/ai/test-chat` | GM | Single-turn non-streaming smoke test — surfaces the exact Ollama error for a given model id, plus a `note` if it had to be fuzzy-matched. |
| GET | `/api/ai/ping` | GM | SSE smoke test that streams 5 dummy tokens without touching Ollama, to isolate transport issues from model issues. |

\* GM always; a player may if the active world's `players_can_ask_ai` is on (same axis `/api/ai/stream` uses) — these routes back the per-entity "Ask AI" / "Talk as this NPC" panel, not the GM-only `/ai` World Chat page.

## AI — Image Generation

`app/routers/ai.py` (prefix `/api/ai/imagegen`) — SwarmUI/ComfyUI-backed image generation, tag autocomplete, and a starred-image gallery.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/api/ai/imagegen/status` | GM | Backend (SwarmUI/ComfyUI) connectivity status. |
| POST | `/api/ai/imagegen/models/download` | GM | Streams a checkpoint/VAE/text-encoder/etc. from a direct URL into SwarmUI's own Models folder (SSE progress) — only works when nd-world and SwarmUI share the model volume (the bundled "swarmui" Compose service does by default). The final `"status": "done"` event includes `model_list_refreshed` (bool) — whether nd-world was able to make SwarmUI rescan its Models folder so the file shows up in `/api/ai/imagegen/models` etc. immediately; if `false`, SwarmUI needs a restart to notice it. |
| GET | `/api/ai/imagegen/models/downloaded` | GM | Lists files nd-world can see under the shared SwarmUI models directory, plus suggested subfolder names. |
| DELETE | `/api/ai/imagegen/models/downloaded` | GM | Deletes a previously-downloaded file (`?subfolder=&filename=`). Response includes `model_list_refreshed` (bool), same meaning as the download route. |
| GET | `/api/ai/imagegen/models` | GM | Available checkpoint models. |
| GET | `/api/ai/imagegen/loras` | GM | Available LoRAs. |
| GET | `/api/ai/imagegen/samplers-schedulers` | GM | Available samplers/schedulers. |
| GET | `/api/ai/imagegen/upscalers` | GM | Available upscale models. |
| GET | `/api/ai/imagegen/refiners` | GM | Available refiner models. |
| GET | `/api/ai/imagegen/ipadapter-models` | GM | Available IP-Adapter models. |
| GET | `/api/ai/imagegen/progress` | GM | Current generation job progress. |
| GET | `/api/ai/imagegen/tags/sources` | GM | Lists built-in + custom tag-autocomplete sources (Danbooru, e621, etc.) and which is downloaded/active. |
| GET | `/api/ai/imagegen/tags/status` | GM | Whether a tag source is currently loaded, and its tag count. |
| POST | `/api/ai/imagegen/tags/fetch` | GM | Downloads a tag source's CSV (built-in or a custom URL), optionally mirroring it into SwarmUI's autocomplete directory. |
| POST | `/api/ai/imagegen/tags/activate` | GM | Switches the active tag source. |
| POST | `/api/ai/imagegen/tags/delete` | GM | Deletes a downloaded tag source. |
| GET | `/api/ai/imagegen/tags` | GM | Prefix/exact tag search against the active source, for the prompt box's autocomplete. |
| POST | `/api/ai/imagegen/star` | GM | Saves a generated image (URL + full generation params) to the starred gallery. |
| POST | `/api/ai/imagegen/unstar` | GM | Removes an image from the starred gallery. |
| GET | `/api/ai/imagegen/starred` | GM | Lists starred images. |
| POST | `/api/ai/imagegen/generate` | GM | Generates image(s) — the full parameter surface (sampler/scheduler/CFG/seed/LoRA/VAE/CLIP-skip/upscale/img2img/ControlNet/hires-fix/refiner/FreeU/DynThresh/IP-Adapter/batch). |
| POST/GET | `/api/ai/imagegen/jobs` | GM | Runs an image generation as a durable background job instead of blocking the request — same parameter surface as `/generate`; POST creates, GET lists recent jobs for the active world. |
| GET | `/api/ai/imagegen/jobs/{job_id}` | GM | Poll one image generation job. |
| POST | `/api/ai/imagegen/jobs/{job_id}/cancel` | GM | Cancels an in-progress image generation job. |
| DELETE | `/api/ai/imagegen/jobs/{job_id}` | GM | Deletes a finished image generation job (400 if still in progress — cancel first). |

## Settings

`app/main.py` — instance-wide configuration (single `AppSettings` row, not per-world).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/settings` | GM | Settings page (Options / System / Visibility tabs). |
| POST | `/settings` | GM | Saves per-GM display preferences. |
| POST | `/settings/system` | GM | Saves instance-wide settings: image format/quality, Ollama URL/model override, SwarmUI/Android-emulator/Editor external URLs, hover-preview timing, and the Dreamlands/King in Yellow enable toggles. |

## Admin & Uploads

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/admin/backup.zip` | GM | Streams a full-fidelity backup zip: a consistent `VACUUM INTO` snapshot of `world.db`, all uploads, all map JSON, and a row-count manifest. Safe to run against a live database. |
| GET | `/imagestudio` | GM | Embedded SwarmUI iframe (`SWARMUI_EXTERNAL_URL`). |
| GET | `/androidapp` | Player | Embedded noVNC viewer for the optional Android-emulator Compose profile. |
| GET | `/editor` | GM | Embedded noVNC viewer for the optional NeonDragonsEditor Compose profile. |

---

## MCP Server

`app/mcp_server.py` — a [Model Context Protocol](https://modelcontextprotocol.io) server mounted at `POST /mcp`, so a phone or desktop Claude conversation (or any MCP client) can read/write world data directly. Authenticated by an `ApiToken` bearer token (issued from [`/account`](#account)) instead of the session cookie — `/mcp` is deliberately routed around the whole `auth_gate` middleware stack at the ASGI level (see the comment above `auth_gate` in `app/main.py`) because `BaseHTTPMiddleware` is fundamentally incompatible with the transport's own task-group-based streaming.

Every tool resolves the calling user from the bearer token and applies the exact same GM/player rules as the equivalent HTTP endpoint — a player's token can read but never write GM-only data, and `create_fact`/`update_fact`/`delete_fact` reject a non-GM token outright.

| Tool | Access | Description |
|---|---|---|
| `list_worlds()` | Player | Worlds the token's user can access. |
| `create_fact(world_id, content, visible_to_players, game_session_id?)` | GM | Creates a `Fact`. |
| `list_facts(world_id, game_session_id?)` | Player | Lists facts, filtered to `visible_to_players` for a non-GM token. |
| `update_fact(fact_id, content?, visible_to_players?)` | GM | Edits a fact. |
| `delete_fact(fact_id)` | GM | Deletes a fact. |
| `search_entities(world_id, query, kind?)` | Player | Keyword search over entities, filtered by visibility. |
| `list_quests(world_id, status?)` | Player | Lists quests, filtered by `Quest.visible_to_players`. |
| `ask_chronicler(world_id, question)` | Player | Same filtered RAG chat as `POST /api/chronicler/ask`, callable from a phone conversation. |
