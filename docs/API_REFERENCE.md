# API Reference

Every HTTP route exposed by nd-world (`app/main.py` + `app/routers/*.py`), grouped by
feature area, plus the MCP server's tools (`app/mcp_server.py`). Generated from a full
audit of the route table — **453 HTTP routes** and **8 MCP tools** as of this writing,
and regression-enforced by `tests/test_api_reference_docs.py` (a new route fails CI
until it gets a row here).

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
| **GM / Assistant** | GMs always; plus a **GM-Assistant** — a non-GM whose `WorldMembership.role` in the active world is `"assistant"` (set by the GM from the world's Members table) — may call it. Governed by a second allowlist, `_is_assistant_safe()` (`app/main.py`), covering world **content** (entities/notes, sessions, calendar, tables, boards, maps/schematics, pages, gallery, audio/video, imports, and the AI content-drafters) while administration (Settings, `/worlds/*`, invites/members, backups, export, AI model/system management) stays GM-only. Assistants see exactly what players see — every visibility filter stays keyed on `is_gm`. New routes are GM-only for assistants too unless deliberately added to `_is_assistant_safe()`; regression-tested by `tests/test_gm_assistant.py`. |

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
- [Dice](#dice)
- [Parties](#parties)
- [Calendar](#calendar)
- [Maps](#maps)
- [Schematics](#schematics)
- [Investigation Boards](#investigation-boards)
- [King in Yellow & Dreamlands](#king-in-yellow--dreamlands)
- [Handouts](#handouts)
- [Audio Library](#audio-library)
- [Video Library](#video-library)
- [Gallery (Images)](#gallery-images)
- [Pages](#pages)
- [Random Tables](#random-tables)
- [Rules](#rules)
- [Search](#search)
- [Import / Export](#import--export)
- [AI — Chat & World-Building](#ai--chat--world-building)
- [AI — Image Generation](#ai--image-generation)
- [Background Jobs](#background-jobs)
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
| POST | `/account/trusted-devices/{device_id}/revoke` | Player | Revokes one of the caller's own trusted devices, so two-step auth is re-required there on the next login. |

## Worlds

`app/main.py` — world creation/management, invites, membership, per-world private notes.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/worlds` | GM | List/manage all of the GM's worlds. |
| POST | `/worlds/new` | GM | Creates a new `World`. |
| POST | `/worlds/{world_id}/delete` | GM | Deletes a world and every row/file it owns (entities, characters, maps, schematics, uploads, etc.) — irreversible. |
| GET | `/worlds/switch/{slug}` | Player | Sets the `active_world` cookie to this world and redirects to `?next=` (or `/`) — the world-switcher dropdown's target. |
| GET | `/worlds/{world_id}/edit` | GM | World settings form: name, accent color, visual theme, hero banner placement (off/home page/every page), party-visibility toggle, invites, members list. |
| POST | `/worlds/{world_id}/edit` | GM | Saves world settings. |
| POST | `/worlds/{world_id}/theme/import` | GM | Imports a visual theme (palette/font overrides) from a JSON file — see `docs/world-theme-gothic-moonlight.json` for an example and `World.theme_json` in `app/models.py` for the recognized shape. Unrecognized/invalid fields are dropped rather than failing the whole import; a `"accent"` key in the file is applied to `World.accent` instead of being stored in the theme itself. |
| POST | `/worlds/{world_id}/theme/clear` | GM | Removes the imported theme, reverting to the app's default look (still keeps the plain accent color). |
| POST | `/worlds/{world_id}/invites/new` | GM | Creates a new `InviteCode` (optionally time- or use-limited). |
| POST | `/worlds/{world_id}/invites/{invite_id}/revoke` | GM | Revokes an unused invite link. |
| POST | `/worlds/{world_id}/members/{user_id}/remove` | GM | Removes a player's `WorldMembership` from this world. |
| POST | `/worlds/{world_id}/members/{user_id}/role` | GM | Sets a member's `WorldMembership.role` — `player` (default) or `assistant` (GM-Assistant: player visibility, may create/edit world content via `_is_assistant_safe`; see the Auth model above). Unknown roles get a 400. |
| GET | `/worlds/{world_id}/notes/{user_id}` | Player | A private GM↔player note thread — visible to the GM and that one player only. |
| POST | `/worlds/{world_id}/notes/{user_id}/new` | GM | Posts a new private note to a player. |
| POST | `/worlds/{world_id}/notes/{user_id}/{note_id}/delete` | GM | Deletes a private note. |
| POST | `/folders/rename` | GM / Assistant | Renames (or clears, moving entities to Unfiled) a folder across all entities of a kind. |
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
| POST | `/api/worlds/{world_id}/home/pinned-tile` | GM | Pins an entity/character as a highlighted tile on this world's home page. |
| POST | `/api/worlds/{world_id}/home/pinned-tile/remove` | GM | Removes a pinned home-page tile. |
| POST | `/api/worlds/{world_id}/home/hide-kind` | GM | Hides (or re-shows) one entity-kind stat tile on this world's home page. |

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
| GET | `/new` | GM / Assistant | New-entity form (kind chosen via query param). |
| POST | `/new` | GM / Assistant | Creates an entity. |
| GET | `/entity/{entity_id}/edit` | GM / Assistant | Edit form. |
| POST | `/entity/{entity_id}/edit` | GM / Assistant | Saves entity edits (including per-player visibility overrides). |
| POST | `/entity/{entity_id}/delete` | GM / Assistant | Deletes an entity. |
| POST | `/entity/{entity_id}/duplicate` | GM / Assistant | Clones an entity (with a "copy" suffix on the name) as a starting point for variants. |
| POST | `/kind/{kind}/bulk-delete` | GM / Assistant | Deletes many entities of one kind at once (checkbox multi-select UI). |
| POST | `/api/entities/bulk-visibility` | GM | Sets `visible_to_players` (and, for "specific players" mode, the per-player access list) across a selected batch of entities in one call — Settings > Visibility tab. |
| POST | `/api/entities/bulk-folder` | GM / Assistant | Moves a selected batch of entities into a folder in one call (the list pages' bulk-action bar). |
| GET | `/api/entities/picker` | Player | Lightweight `{id, name, kind, folder}` listing for entity-picker UIs, filtered to what the caller may see. |
| POST | `/entity/{entity_id}/link/{target_id}` | GM / Assistant | Creates a bidirectional relationship link between two entities. |
| POST | `/entity/{entity_id}/unlink/{target_id}` | GM / Assistant | Removes a link. |
| POST | `/entity/{entity_id}/notes/new` | GM / Assistant | Adds a note to an entity — independently hideable from the entity's own visibility. |
| POST | `/entity/{entity_id}/notes/import` | GM / Assistant | Imports a note from an uploaded file — `.md`/`.txt`/`.pdf` become plain text, `.html`/`.htm` convert to markdown (or stay sanitized HTML with "preserve original formatting"), images become an embedded note. |
| POST | `/entity/{entity_id}/notes/{note_id}/toggle` | GM / Assistant | Toggles a note's `visible_to_players` flag. |
| POST | `/entity/{entity_id}/notes/{note_id}/delete` | GM / Assistant | Deletes a note. |
| POST | `/api/upload-image` | GM / Assistant | Uploads an image and returns its URL — backs the rich-text formatting toolbar's image button on GM-only fields (entity body/notes). |
| POST | `/api/entity/{entity_id}/image` | GM / Assistant | Sets an entity's portrait directly from a URL (Image Studio's "Set as portrait"/"Attach" flow) without the full edit form. |

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
| POST | `/characters/{pc_id}/retire-to-npc` | GM | Converts a retired player character into a `character`-kind Entity, preserving its lore. |

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
| GET | `/sessions` | GM / Assistant | Session list for the active world. |
| GET | `/sessions/new` | GM / Assistant | New session form. |
| POST | `/sessions/new` | GM / Assistant | Creates a `GameSession`. |
| GET | `/sessions/{session_id}` | GM / Assistant | Session detail: prep checklist, XP/loot log, recap (with the AI toolbar). |
| POST | `/sessions/{session_id}/edit` | GM / Assistant | Saves session edits, including the raw `summary` recap text. |
| POST | `/sessions/{session_id}/recap-publish` | GM / Assistant | Saves the session's PLAYER-facing recap (`player_summary`) and sets its published flag — the Session Log serves a published recap verbatim to players; unpublished/empty falls back to the facts-recap pipeline. |
| POST | `/sessions/{session_id}/delete` | GM / Assistant | Deletes a session. |
| POST | `/api/sessions/{session_id}/prep/toggle` | GM / Assistant | Toggles a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/add` | GM / Assistant | Adds a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/{idx}/delete` | GM / Assistant | Removes a prep checklist item. |
| POST | `/api/sessions/{session_id}/prep/generate` | GM / Assistant | AI: drafts a prep checklist from the prior session's recap/facts, the world's open quests, and the assigned party — returned for review, not written until confirmed items are POSTed to `prep/add`. |
| POST | `/api/sessions/{session_id}/xp` | GM / Assistant | Records XP awarded this session. |
| POST | `/api/sessions/{session_id}/loot/transfer` | GM / Assistant | Transfers loot from the session log to a party/character. |
| POST | `/api/sessions/ai/expand-notes` | GM / Assistant | AI: expands terse GM notes (from the Summary textarea) into a written narrative recap via the local Ollama model. |
| POST | `/api/sessions/ai/condense-recap` | GM / Assistant | AI: condenses/tightens an existing recap. |
| POST | `/api/sessions/ai/condense-job` | GM / Assistant | Durable background-job variant of the condense pass above. |
| POST | `/api/sessions/ai/summarize-transcript` | GM / Assistant | AI: summarizes an already-transcribed transcript text into a recap. |
| POST | `/api/sessions/ai/summarize-from-audio` | GM / Assistant | AI: transcribes an uploaded/mic-recorded session audio clip via Whisper (biased by the world's name glossary) and summarizes it into a recap — session-independent, usable on the New Session form. |
| POST | `/api/sessions/ai/summarize-from-audio/chunk`, `/api/sessions/ai/summarize-from-audio/complete` | GM / Assistant | Chunked variant of the above for a recording over the direct-upload size threshold. |
| POST | `/api/sessions/ai/audio-jobs`, `/api/sessions/ai/audio-jobs/chunk`, `/api/sessions/ai/audio-jobs/complete` | GM / Assistant | Transcribe (and, for `session_recap`, summarize) a session recording as a durable background job instead of blocking the upload request. |
| GET | `/api/sessions/ai/audio-jobs/{job_id}`, `/api/sessions/ai/audio-jobs` | GM / Assistant | Poll one session audio job, or list them. |
| POST | `/api/sessions/ai/audio-jobs/from-clip` | GM / Assistant | Starts a session audio job from an existing Audio Library clip (no re-upload). |
| GET | `/sessions/{session_id}/summary.md` | GM / Assistant | Downloads the session's summary/recap as a Markdown file. |
| GET | `/sessions/{session_id}/transcript.md` | GM / Assistant | Downloads the session's transcript as a Markdown file. |
| POST | `/api/sessions/{session_id}/live-transcript/append` | GM / Assistant | Transcribes one chunk of a live session recording via Whisper and appends it to the session's running live transcript. When the client sends `save_audio`/`recording_id`/`segment_index` ("Save raw audio" checkbox), also keeps the raw segment on disk for re-transcription/download. |
| POST | `/api/sessions/{session_id}/live-transcript/clear` | GM / Assistant | Clears the accumulated live transcript. |
| GET | `/api/sessions/{session_id}/live-audio` | GM / Assistant | Lists the raw audio segments saved by a live recording (paths, count, total bytes). |
| GET | `/api/sessions/{session_id}/live-audio/download` | GM / Assistant | Downloads the whole saved raw recording as one file (ffmpeg concat of the segments in order). 400 if nothing was saved or ffmpeg fails. |
| POST | `/api/sessions/{session_id}/ai/summarize-live-transcript` | GM / Assistant | AI: summarizes the accumulated live transcript into a recap. |
| POST | `/api/sessions/{session_id}/ai/summarize-live-transcript-job` | GM / Assistant | Durable background-job variant of the live-transcript summary above. |
| POST | `/api/sessions/{session_id}/ai/summarize-from-facts` | GM / Assistant | AI: weaves this session's logged `Fact` rows (all of them, secret or not) into a narrative recap. |
| GET | `/session-log` | Player | Player-facing list of sessions (title/date/number only — never the GM's raw summary). |
| GET | `/session-log/{session_id}` | Player | Player-facing session page. Serves the session's published player recap verbatim when one is published; otherwise fetches the AI facts-recap client-side (create-or-poll). 404s if the session's world isn't one the caller belongs to. |
| POST | `/api/session-log/{session_id}/recap` | Player | AI: returns this session's cached recap, or starts a background generation job for it (poll — re-POST — until the response stops saying `{"pending": true}`). Synthesized from only the `Fact` rows marked `visible_to_players` for this session — the GM's raw `summary` is never sent to this endpoint's caller or to the model on their behalf. GMs calling it get every fact, secret or not. Accepts optional JSON options (`model`, `think`, `use_rag`, `rag_entity_limit`, `rag_notes_limit`) — a different configuration is a different artifact and regenerates. RAG is GM-only on this surface (forced off for players; the retrieval isn't visibility-filtered). |

## Facts & Chronicler

`app/routers/facts.py`, `app/routers/chronicler.py` — a discrete, visibility-flagged log of "what happened" (as opposed to the free-text session recap), and an AI chat assistant that answers questions from it.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/facts` | GM | Facts list + quick-add form + the recap→facts AI parser panel. |
| POST | `/facts/new` | GM | Creates a `Fact` (content + `visible_to_players` flag + optional linked session). |
| POST | `/facts/{fact_id}/edit` | GM | Saves fact edits. |
| POST | `/facts/{fact_id}/delete` | GM | Deletes a fact. |
| POST | `/api/facts/parse` | GM | AI: turns a rough recap paste into draft facts (content + suggested visibility) via the local model — returned for review, **not written to the DB**. Blocking; prefer `parse-job` for long input. |
| POST | `/api/facts/parse-job` | GM | AI: the same parse as `/api/facts/parse` but as a durable background job — body `{text, game_session_id?, model?}`, returns `{job_id}` immediately; poll `/api/audio-jobs/{id}` (`result_json` holds the finished draft array). |
| GET | `/api/facts/last-parse` | GM | The latest finished facts-parse job's draft for the active world — `{job_id, created_at, facts}`; 404 when there's none yet. Powers the Facts page's "Restore last parse". |
| POST | `/api/facts/bulk` | GM | Bulk-inserts facts — the recap-review UI's "Confirm & Save" action. Duplicate content (world-scoped, case/punctuation-blind) is skipped, not re-inserted — response is `{created, skipped_duplicates}`. |
| POST | `/api/facts/from-job/{job_id}` | GM | Confirms (or dismisses) the Facts a finished `session_recap` job auto-drafted from its transcript (`AudioJob.pending_facts_json`) — body `{facts}` may be an edited/trimmed subset (or `[]` to dismiss without saving); uses the job's own session automatically. Same dedup rule as `/api/facts/bulk`; response is `{created, skipped_duplicates}`. |
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

## Dice

`app/routers/dice.py` — the shared table dice roller: system-agnostic dice notation (`2d6+3`, `d20`, `4d8+2d6+1`) with a world-scoped roll log every member (players included) can read and write.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/dice` | Player | Dice roller page: notation form, quick-pick buttons, and the world's latest 50 rolls. |
| POST | `/dice` | Player | Rolls from the page form and redirects back (POST-redirect-GET; invalid notation round-trips as `?error=`). |
| POST | `/api/dice/roll` | Player | Rolls a notation string and appends the result to the world's roll log; returns the stored roll (per-die breakdown included). |
| GET | `/api/dice/history` | Player | The world's latest 50 rolls as JSON (newest first). |

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
| GET | `/calendar` | GM / Assistant | Calendar view with logged events. |
| GET | `/calendar/config` | GM / Assistant | Calendar configuration form (month names/lengths, starting date). |
| POST | `/calendar/config` | GM / Assistant | Saves calendar configuration. |
| POST | `/api/calendar/events` | GM / Assistant | Adds an event on a given in-world date. |
| POST | `/api/calendar/events/{event_id}/delete` | GM / Assistant | Deletes an event. |
| POST | `/api/calendar/days/{day}/icons` | GM / Assistant | Attaches weather/condition icons to a calendar day. |
| POST | `/api/calendar/icons/{icon_id}/delete` | GM / Assistant | Removes a day icon. |
| POST | `/api/calendar/advance` | GM / Assistant | Advances the in-world "current date" by N days. |

## Maps

`app/main.py` — image-based maps with markers and region overlays.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/maps` | Player | Map list for the active world. |
| GET | `/maps/new` | GM / Assistant | New map form. |
| POST | `/maps/new` | GM / Assistant | Creates a map. |
| POST | `/maps/{slug}/rename` | GM / Assistant | Renames a map. |
| POST | `/maps/{slug}/delete` | GM / Assistant | Deletes a map. |
| POST | `/maps/{slug}/upload` | GM / Assistant | Uploads/replaces the map's background image. |
| GET | `/maps/{slug}` | Player | Map viewer with markers/regions. |
| POST | `/api/maps/{slug}/overlay` | GM / Assistant | Saves marker/region overlay data. |

## Schematics

`app/main.py` — an SVG-based canvas editor for battle-map/dungeon layouts, with a live token-based player view.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/maps/schematic/new` | GM / Assistant | New schematic form. |
| POST | `/maps/schematic/new` | GM / Assistant | Creates a schematic. |
| GET | `/maps/schematic/{slug}` | GM / Assistant | GM editor canvas. |
| POST | `/maps/schematic/{slug}/link-combat` | GM / Assistant | Links the schematic to a `CombatSession` for token sync. |
| POST | `/maps/schematic/{slug}/unlink-combat` | GM / Assistant | Removes the combat link. |
| POST | `/maps/schematic/{slug}/pull-combat` | GM / Assistant | One-way sync Combat → Map: creates/refreshes a token for every combatant in the linked combat. |
| POST | `/maps/schematic/{slug}/push-combat` | GM / Assistant | One-way sync Map → Combat: writes token HP/max HP/conditions back onto the linked combat's combatants. |
| GET | `/maps/schematic/{slug}/view` | Player | Read/interact-only player view (hides GM-only elements). |
| GET | `/maps/schematic/{slug}/view.json` | Player | Player-view state as JSON (polled by the view page for live updates). |
| POST | `/api/maps/schematic/{slug}/move-token` | Player | Moves a token — a non-GM player may only move the one token linked to their own `PlayerCharacter`. |
| POST | `/api/maps/schematic/{slug}/pickup-item` | Player | A player picks up an entire item-token stack into their own character's inventory. |
| POST | `/api/maps/schematic/{slug}/buy-item` | Player | A player buys one stock row from a merchant token, deducting currency and reducing stock (row-level locked against concurrent buys). |
| POST | `/maps/schematic/{slug}/grid` | GM / Assistant | Saves the grid overlay type/config (none/square/hex). |
| POST | `/maps/schematic/{slug}/elements` | GM / Assistant | Saves the full canvas element list (shapes, tokens, labels). |
| POST | `/maps/schematic/{slug}/upload` | GM / Assistant | Uploads a background image. |
| POST | `/maps/schematic/{slug}/embed-image` | GM / Assistant | Embeds a picked image file directly into an element (data-URI, no separate upload round trip) — backs the editor's 🖼 Embed Image tool. |
| POST | `/maps/schematic/{slug}/rename` | GM / Assistant | Renames a schematic. |
| POST | `/maps/schematic/{slug}/delete` | GM / Assistant | Deletes a schematic. |

## Investigation Boards

`app/main.py` + `app/routers/boards_generate.py` — node-and-edge relationship graphs for factions, story threads, or reference maps.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/boards` | GM / Assistant | Board list. |
| GET | `/boards/new` | GM / Assistant | New board form. |
| POST | `/boards/new` | GM / Assistant | Creates a blank board. |
| GET | `/boards/{slug}` | GM / Assistant | Board canvas (nodes/edges editor). |
| POST | `/boards/{slug}/save` | GM / Assistant | Saves the node/edge layout. |
| POST | `/boards/{slug}/delete` | GM / Assistant | Deletes a board. |
| GET | `/boards/{slug}/export` | GM / Assistant | Exports the board as JSON. |
| POST | `/boards/generate-orgs` | GM / Assistant | Auto-generates a faction/organization relationship board from `organization`-kind entities and their links (radial cluster layout, plus keyword-classified allies/enemies/controls/rivals edges inferred from entity text). |
| GET | `/api/orgs/graph` | GM / Assistant | The same faction-graph data as JSON without saving a board — for external use. |
| POST | `/boards/generate-dreamlands` | GM / Assistant | Generates a fixed, bundled 50-location geographic atlas of the Dreamlands (reference content, not derived from world data). |

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

## Audio Library

`app/routers/audio.py` — a per-world tree of GM-uploaded audio clips (ambiance, sound effects, NPC voice lines, recorded handouts), organized into nested albums (`AudioAlbum`/`AudioClip` in `app/models.py`). Unlike Images (GM-only end to end), the library itself is player-safe: a player sees a read-only view filtered to `visible_to_players=True`, same default-visible convention as an `Entity`. Upload/edit/delete/album management are GM-or-Assistant (enforced in each handler; a GM-Assistant — see the Auth model — may manage clips like a GM, but always sees only what a player sees).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/audio` | Player | Top-level clip/album list for the active world. |
| GET | `/audio/albums/{album_id}` | Player | One album's clips and sub-albums, with a breadcrumb. |
| POST | `/audio/albums/new` | GM / Assistant | Creates an album (optionally as a child, via `parent_id`). |
| POST | `/audio/albums/{album_id}/rename` | GM / Assistant | Renames an album. |
| POST | `/audio/albums/{album_id}/delete` | GM / Assistant | Deletes an album, cascading to its sub-albums and their clips (and files). |
| POST | `/audio/upload` | GM / Assistant | Uploads one audio clip (single request, up to `MAX_AUDIO_UPLOAD_BYTES`). |
| POST | `/audio/upload/chunk`, `/audio/upload/complete` | GM / Assistant | Client-split large-upload pair (see `app/uploads.py`) for a clip too big for a single request behind a reverse proxy's body-size cap. |
| POST | `/audio/{clip_id}/edit` | GM / Assistant | Updates a clip's name/description/visibility/album. |
| POST | `/audio/{clip_id}/transcribe` | GM / Assistant | Generates an AI transcript + WebVTT subtitle track via Whisper (`app.ai.transcribe_audio_with_subtitles`), honoring the world's glossary/language/denoise settings. Synchronous; overwrites any existing transcript/subtitles. |
| POST | `/audio/{clip_id}/delete` | GM / Assistant | Deletes a clip and its file. |
| GET | `/api/audio/clips` | Player | JSON clip listing for the soundboard/picker UIs, visibility-filtered. |

## Video Library

`app/routers/video.py` — the video counterpart to the Audio Library above (a recorded cutscene, a handout clip, an NPC video message), same album-tree/visibility/chunked-upload shape (`VideoAlbum`/`VideoClip` in `app/models.py`). Two real differences from Audio: `VideoClip.poster_url` is a best-effort ffmpeg-generated thumbnail frame (nullable — a missing poster just means the `<video>` element falls back to its own native preview frame, never a failed upload), and an opt-in per-world AV1 conversion pass on upload for space savings (`World.video_convert_enabled`/`video_convert_max_height`/`video_convert_bitrate_kbps`) — same graceful-degradation contract as the poster: an unavailable AV1 encoder just keeps the original file, never a failed upload. Upload size defaults to 2 GB (`MAX_VIDEO_UPLOAD_BYTES`, separate from audio's own limit).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/video` | Player | Top-level clip/album list for the active world; the space-saving conversion settings panel appears here (GM only) but not inside an album. |
| GET | `/video/albums/{album_id}` | Player | One album's clips and sub-albums, with a breadcrumb. |
| POST | `/video/settings` | GM | Saves the world's AV1 conversion preferences (on/off, max height, target bitrate) — applies to future uploads only. |
| POST | `/video/albums/new` | GM / Assistant | Creates an album (optionally as a child, via `parent_id`). |
| POST | `/video/albums/{album_id}/rename` | GM / Assistant | Renames an album. |
| POST | `/video/albums/{album_id}/delete` | GM / Assistant | Deletes an album, cascading to its sub-albums and their clips (and files). |
| POST | `/video/upload` | GM / Assistant | Uploads one video clip (single request, up to `MAX_VIDEO_UPLOAD_BYTES`); converts to AV1 if the world has opted in, then generates a poster frame best-effort. |
| POST | `/video/upload/chunk`, `/video/upload/complete` | GM / Assistant | Client-split large-upload pair, same as the Audio Library's. |
| POST | `/video/{clip_id}/edit` | GM / Assistant | Updates a clip's name/description/visibility/album. |
| POST | `/video/{clip_id}/transcribe` | GM / Assistant | Generates an AI transcript + WebVTT subtitle track via Whisper — same as the Audio Library's, and works directly on the video file (ffmpeg decodes its audio track). |
| POST | `/video/{clip_id}/delete` | GM / Assistant | Deletes a clip and its file (and poster, if one was generated). |

## Gallery (Images)

`app/routers/gallery.py` — a per-world image library organized into albums (`ImageAlbum`/`Image` in `app/models.py`), GM end to end and now open to a GM-Assistant as well (unlike the Audio/Video libraries it has no player-facing read tier): the gallery is the asset workspace feeding portraits, maps, handouts, and the Spotlight broadcast.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/images` | GM / Assistant | Image gallery for the active world (album grid + loose images). |
| GET | `/images/albums/{album_id}` | GM / Assistant | One album's images. |
| POST | `/images/albums/new` | GM / Assistant | Creates an album. |
| POST | `/images/albums/{album_id}/rename` | GM / Assistant | Renames an album. |
| POST | `/images/albums/{album_id}/delete` | GM / Assistant | Deletes an album. |
| POST | `/images/albums/{album_id}/upload` | GM / Assistant | Uploads images directly into an album. |
| POST | `/images/albums/upload/chunk` | GM / Assistant | Receives one part of a large (>100 MB) album image split client-side; reassembled by `/images/albums/upload/complete`. |
| POST | `/images/albums/upload/complete` | GM / Assistant | Reassembles a chunked album image upload (converted + thumbnailed) and attaches it to the album; returns `{"url": ...}`. |
| POST | `/images/albums/{album_id}/add` | GM / Assistant | Adds existing uploaded images to an album. |
| POST | `/images/albums/{album_id}/remove` | GM / Assistant | Removes an image from an album. |
| POST | `/images/albums/{album_id}/move` | GM / Assistant | Moves an image to a different album. |
| POST | `/images/delete` | GM / Assistant | Deletes an uploaded image. |
| POST | `/images/spotlight` | GM / Assistant | Broadcasts an image to every open tab in the world (lightbox popup — players included). |
| POST | `/images/spotlight/clear` | GM / Assistant | Stops the current spotlight broadcast. |
| GET | `/api/gallery/browse` | GM / Assistant | JSON album/image listing for picker UIs. |

## Pages

`app/routers/pages.py` — GM-authored static document pages (house rules summaries, player guides, custom handouts) organized into albums, rendered from their source file. Player-readable once published.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/pages` | Player | Pages list for the active world. |
| GET | `/pages/{doc_id}` | Player | Renders one page. |
| GET | `/pages/{doc_id}/download` | Player | Downloads a page's raw .html file (`Content-Disposition: attachment`) — same visibility gate as viewing it; a player sees this only for a page already `visible_to_players`. |
| POST | `/pages/{doc_id}/edit` | GM / Assistant | Saves page edits (name/description/visibility/album). |
| POST | `/pages/{doc_id}/delete` | GM / Assistant | Deletes a page and its file. |
| POST | `/pages/upload` | GM / Assistant | Uploads a page source file (single request). |
| POST | `/pages/upload/chunk`, `/pages/upload/complete` | GM / Assistant | Client-split large-upload pair for a page file over the direct threshold. |
| POST | `/pages/albums/new` | GM / Assistant | Creates a pages album. |
| POST | `/pages/albums/{album_id}/rename` | GM / Assistant | Renames a pages album. |
| POST | `/pages/albums/{album_id}/delete` | GM / Assistant | Deletes a pages album. |
| GET | `/pages/albums/{album_id}` | Player | One pages album's contents, with a breadcrumb. |

`app/routers/character_sheets.py` — a player's personal, fillable copy of a page the GM has marked as a character sheet template (a checkbox on the Pages upload/edit form). Access is GM + the owning player only everywhere below — not GM-Assistant, not another player.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/pages/sheets` | Player | "My Character Sheets" — the caller's own sheets; GM sees every sheet in the world, grouped by owner. |
| POST | `/pages/sheets/new` | Player | Creates a fillable sheet from a template (`template_id`, optional `player_character_id`/`name`); redirects to its editor. |
| GET | `/pages/sheets/{sheet_id}` | Owner / GM | The fill-in editor page (sandboxed iframe + Saving indicator + rename/PC-link/download/delete controls). |
| GET | `/pages/sheets/{sheet_id}/render` | Owner / GM | The template's HTML with the auto-save bridge script + this sheet's saved data injected, served with the same sandboxed CSP/X-Frame-Options headers as an uploaded page. |
| POST | `/pages/sheets/{sheet_id}/save` | Owner / GM | Saves the sheet's field data (JSON body `{"data": {...}}`) — called by the auto-save bridge on every debounced change. |
| POST | `/pages/sheets/{sheet_id}/edit` | Owner / GM | Renames the sheet and/or changes its linked `PlayerCharacter`. |
| POST | `/pages/sheets/{sheet_id}/delete` | Owner / GM | Deletes the sheet (the template file itself is untouched). |
| GET | `/pages/sheets/{sheet_id}/download` | Owner / GM | Downloads the filled sheet as a standalone `.html` file (`Content-Disposition: attachment`). |

## Random Tables

`app/routers/tables.py` — GM-authored roll tables (loot, encounters, NPC names, etc.).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/tables` | GM / Assistant | Table list. |
| GET | `/tables/new` | GM / Assistant | New table form. |
| POST | `/tables/new` | GM / Assistant | Creates a table. |
| GET | `/tables/{table_id}/edit` | GM / Assistant | Edit form. |
| POST | `/tables/{table_id}/edit` | GM / Assistant | Saves table edits. |
| POST | `/tables/{table_id}/delete` | GM / Assistant | Deletes a table. |
| POST | `/api/tables/{table_id}/roll` | GM / Assistant | Rolls on a table and returns the result. |
| GET | `/tables/export` | GM / Assistant | Exports all tables as JSON. |
| POST | `/tables/import` | GM / Assistant | Imports tables from JSON. |

## Rules

`app/main.py` — the per-world core-rules document (falls back to bundled N&D rules). Rendered by `app/rules_render.py`: `:::` directive blocks (`tip`/`note`/`warning`/`danger`/`lore`/`collapse`/`gm` — themed callouts, a click-to-open section, and a GM-only block that is removed server-side for players), `statblock` fenced blocks (name + label/value rows + copy button), and an optional per-world `rules_json` overlay (section icons/titles/visibility + tabs; never included in downloads).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/rules` | Player | Rendered rules page with an auto-generated table of contents, section filtering via the search box, and optional tabs from the world's `rules_json` overlay. |
| GET | `/rules/download.md` | Player* | Downloads the rules as a `.md` file (pure Markdown source — no overlay, directives left as written). GM always; a player only once the world's `players_can_download_rules` toggle is on (`world_edit.html`) — 403 otherwise. |
| GET | `/worlds/{world_id}/rules/edit` | GM | Rules edit form (raw Markdown + `rules_json` overlay textarea, with a directives/overlay cheatsheet; shows why a stored overlay is being ignored, if it is). |
| POST | `/worlds/{world_id}/rules/edit` | GM | Saves rules Markdown plus the optional `rules_json` overlay (blank clears it; invalid JSON or wrong shape → 400 with the parse error). |
| POST | `/worlds/{world_id}/rules/import` | GM | Imports rules from a JSON file shaped `{"rules_md": "...markdown..."}`. |

## Search

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/search` | Player | Full-text search across entity names, tags, summaries, and body text, filtered to what the caller may see. |
| GET | `/api/spotlight` | Player | Current spotlight image + version counter — polled by every open tab so a GM's broadcast pops up for players too. |

## Import / Export

`app/main.py`, `app/routers/importer.py`, `app/routers/export.py` — bulk content import, and split/structured export for external tooling.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/import` | GM / Assistant | Import page: JSON bulk import, bulk image matching, bulk AVIF/WebP re-encode. |
| POST | `/api/import` | GM / Assistant | Legacy single-shot bulk JSON import endpoint. |
| POST | `/api/import/images` | GM / Assistant | Bulk portrait/art import — files matched by filename to existing entities. |
| POST | `/api/import/detect` | GM / Assistant | Inspects an uploaded JSON payload and classifies each item's likely kind/shape before import. |
| POST | `/api/import/execute` | GM / Assistant | Executes a reviewed/confirmed bulk import. |
| POST | `/api/import/convert-images` | GM / Assistant | Retroactively re-encodes every image already in the active world (entity/character art, schematic/board images, gallery albums, maps) to a target format (AVIF/WebP/PNG/JPG). Preserves each image's readable filename (see `app/uploads.py`'s `unique_upload_filename`) — only the extension changes — and rewrites every reference to that image consistently, even one shared across an entity and a gallery album. |
| GET | `/api/worlds/{world_id}/content-pack` | Player | A world's homebrew races/professions/feats/items as JSON, for the NeonDragonsApp Android app's content sync. |
| GET | `/export` | GM | **Export & Backup hub page** — one place linking Full Backup, World Book, World JSON (single/split), and Import, each with a one-line explanation of when to use it. Replaces the old scattered nav/world-card export buttons. |
| GET | `/export/book.zip` | GM | Downloads a zip containing a readable HTML "book" export of the active world's lore (entities, boards, maps, rules) — linked from the hub above. |
| GET | `/worlds/{world_id}/export/split` | GM | Split-export landing page — links to per-kind/per-resource JSON files below. |
| GET | `/worlds/{world_id}/export/rules.json` | GM | This world's rules as JSON. |
| GET | `/worlds/{world_id}/export/player-characters.json` | GM | All player characters in this world as JSON. |
| GET | `/worlds/{world_id}/export/entities/{kind}.json` | GM | All entities of one kind as JSON. |
| GET | `/worlds/{world_id}/export/templates/{template_id}.json` | GM | One Sheet Template as JSON. |
| GET | `/export/foundry.json` | GM | Foundry VTT import pack for the whole active world. |
| GET | `/export/rules-and-notes.md` | GM | Downloads the world's rules plus entity notes as one Markdown file. |

## AI — Chat & World-Building

`app/main.py` + `app/routers/ai.py` (prefix `/api/ai`) — the GM-only general chat/world-building assistant, model management, and generation helpers. Not to be confused with the player-facing [Chronicler](#facts--chronicler), which is a separate, visibility-filtered endpoint.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/ai` | GM | AI chat page (Chat / Image Gen / Models / Whisper / Starred tabs). |
| GET | `/api/ai/world-context` | GM / Assistant | Keyword-search RAG context (relevant entities) for the current chat, unfiltered by player visibility. |
| POST | `/api/ai/world-context-smart` | GM / Assistant | Same, backed by an FTS5 full-text index over entity name/summary/body/tags (falls back to plain `LIKE` if FTS5 is unavailable) — also returns `entities` (what was actually retrieved, for the RAG transparency panel/pinning). |
| POST | `/api/ai/save-note` | GM / Assistant | Saves an AI chat response as a note on an entity. |
| POST | `/api/ai/generate/entity-smart` | GM / Assistant | Generates a full draft entity (name/summary/body) from a prompt, with world context. |
| POST | `/api/ai/entity-from-text` | GM / Assistant | Turns a pasted/dictated passage into a draft entity. |
| POST | `/api/ai/chat` | GM | Non-streaming chat completion. |
| POST | `/api/ai/stream` | GM* | Streaming chat completion (SSE) — GM always; a player may if the active world's `players_can_ask_ai` is on. Accepts a per-request `options` (temperature/top_p/etc, clamped) and a `surface` for per-surface default-model fallback; the SSE stream leads with a `note` event if the requested model had to be fuzzy-matched to an available one. |
| GET | `/api/ai/models` | GM | Lists available/known Ollama models with loaded/builtin flags. |
| GET | `/api/ai/resident` | GM | Models actually resident in memory (VRAM/RAM), for the Models tab's residency cockpit. |
| GET | `/api/ai/hardware` | GM | Best-effort CPU/RAM/GPU detection plus a coarse per-model settings recommendation (per-request options and server env vars) for each installed Ollama model, for the Settings → System "Detected hardware" panel. |
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
| POST | `/api/ai/generate/entity` | GM / Assistant | Generates an expanded description for a named entity. |
| POST | `/api/ai/generate/npc` | GM / Assistant | Generates an NPC backstory/personality. |
| POST | `/api/ai/generate/location` | GM / Assistant | Generates a location description. |
| POST | `/api/ai/generate/quest` | GM / Assistant | Generates a quest hook. |
| POST | `/api/ai/status` | GM | Ollama connectivity/model status. |
| GET | `/api/ai/test-chat` | GM | Single-turn non-streaming smoke test — surfaces the exact Ollama error for a given model id, plus a `note` if it had to be fuzzy-matched. |
| GET | `/api/ai/ping` | GM | SSE smoke test that streams 5 dummy tokens without touching Ollama, to isolate transport issues from model issues. |
| GET | `/api/ai/context-info` | GM | Token/context-window usage estimate for the current chat session (the context-usage indicator). |
| POST | `/api/ai/chat/compact` | GM* | Compacts a chat session's history into a shorter summary — player-reachable sibling of `/api/ai/stream` (same `players_can_ask_ai` gate). |
| GET | `/api/ai/ollama/hf-search` | GM | Searches Hugging Face for GGUF-tagged model repos (Models tab's HF search). |
| GET | `/api/ai/ollama/hf-files` | GM | Lists a Hugging Face repo's GGUF files for direct import. |
| POST | `/api/ai/ollama/upload/direct` | GM | Points Ollama at a remote GGUF URL to pull itself (no nd-world relay). |
| POST | `/api/ai/ollama/upload/chunk`, `/api/ai/ollama/upload/complete` | GM | Client-split upload pair for pushing a local `.gguf` into Ollama (up to `MAX_MODEL_UPLOAD_BYTES`). |
| GET | `/api/ai/ollama/upload/status/{import_id}` | GM | Progress of an in-flight .gguf upload/pull. |
| GET/POST | `/api/ai/whisper/denoise` | GM | Lists the bundled audio-denoise profiles / enqueues a denoise job for an audio attachment or session recording. |

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
| GET | `/api/ai/imagegen/backends` | GM | Which image-generation backends (SwarmUI/ComfyUI) are configured and reachable. |
| GET | `/api/ai/imagegen/updates` | GM | Backend version/update availability (the VRAM/status card's update strip). |
| POST | `/api/ai/imagegen/restart` | GM | Restarts the SwarmUI backend service. |
| POST | `/api/ai/imagegen/update` | GM | Updates the SwarmUI backend in place. |
| POST | `/api/ai/imagegen/free-memory` | GM | Asks the backend to unload models and free VRAM. |

## Background Jobs

`app/routers/audio_jobs.py` — a unified view over every durable `AudioJob`
regardless of which surface started it (Session Recap, an AI Chat/Ask AI
voice-memo attachment, or the Whisper Test tab), backing the standalone
**Background Jobs** page where a GM (or a GM-Assistant) can see everything
in flight across the whole world in one place, separate from the smaller inline panels embedded
on each originating page. See [Updating without losing in-flight
jobs](DEPLOYMENT.md#updating-without-losing-in-flight-jobs) for what
`status: "interrupted"` and the resume flow below mean.

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/background-jobs` | GM / Assistant | The standalone Background Jobs page. |
| GET | `/api/audio-jobs` | GM / Assistant | Every job for the active world, any purpose, most recent first. Optional filters: `purpose`, `status` (an exact status, or `running` = any in-progress phase), `game_session_id`. |
| GET | `/api/audio-jobs/{job_id}` | GM / Assistant | Poll one job. |
| POST | `/api/audio-jobs/{job_id}/cancel` | GM / Assistant | Cancels an in-progress job. |
| GET | `/api/audio-jobs/{job_id}/transcript.md` | GM / Assistant | Downloads a finished job's transcript as a Markdown file. |
| GET | `/api/audio-jobs/{job_id}/recap.md` | GM / Assistant | Downloads a finished job's AI recap as a Markdown file. |
| DELETE | `/api/audio-jobs/{job_id}` | GM / Assistant | Deletes a finished job (400 if still in progress — cancel first). |
| POST | `/api/audio-jobs/{job_id}/resummarize` | GM / Assistant | Re-runs just the summarization step against the job's already-saved transcript, optionally with a different model/instructions — no re-upload or re-transcription needed. Always a fresh pass, not a continuation of an interrupted attempt (see the next row for that). |
| POST | `/api/audio-jobs/{job_id}/resume` | GM / Assistant | Continue a job interrupted by a server restart (`status: "interrupted"`) from its saved checkpoint — a true resume, picking up from the exact chunk it left off on. Always resets the auto-resume attempt counter, since a manual click is a deliberate decision, not another automatic retry. 400 if the job isn't in the `"interrupted"` state, or if there's nothing left to resume from (audio gone, no transcript). |

## Settings

`app/main.py` — instance-wide configuration (single `AppSettings` row, not per-world).

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/settings` | GM | Settings page (Options / System / Visibility tabs). |
| POST | `/settings` | GM | Saves per-GM display preferences. |
| POST | `/settings/system` | GM | Saves instance-wide settings: image format/quality, Ollama URL/model override, per-request Ollama generation tuning (temperature/top_p/.../use_mmap, a VRAM override for hardware detection), Ollama server-level tuning (flash attention, KV cache type, and the rest — written to a shared-volume env file for the "ollama" service to pick up on its next restart; see docs/DEPLOYMENT.md), SwarmUI/Android-emulator/Editor external URLs, hover-preview timing, and the Dreamlands/King in Yellow enable toggles. |
| POST | `/settings/system/model-override` | GM | Saves a per-model Ollama override (default options + "supports thinking" flag) for one installed model. |
| POST | `/settings/system/model-override/delete` | GM | Deletes a per-model override. |

## Admin & Uploads

| Method | Path | Access | Description |
|---|---|---|---|
| GET | `/admin/backup.zip` | GM | Streams a full-fidelity backup zip: a consistent `VACUUM INTO` snapshot of `world.db`, all uploads, all map JSON, and a row-count manifest. Safe to run against a live database. |
| GET | `/api/backups` | GM | Lists scheduled DB snapshots (`ND_BACKUP_DIR`) — 400 if scheduled backups aren't configured. |
| POST | `/api/backups/run` | GM | Takes a `VACUUM INTO` snapshot right now and prunes to `ND_BACKUP_KEEP` — same primitive the optional scheduler thread runs on `ND_BACKUP_INTERVAL_SECONDS`. |
| POST | `/worlds/{world_id}/nav-menus/edit` | GM | Saves this world's customized top-nav menu grouping (Settings > Navigation). |
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
