# AGENTS.md — guide for AI agents/assistants working with this repository

nd-world is a self-hosted FastAPI + SQLAlchemy (SQLite) + Jinja2 GM toolkit
for the **Neon & Dragons** tabletop RPG: worldbuilding entities, character
sheets, maps/schematics, investigation boards, AI chat/image-gen, and
campaign-management tools (random tables, combat tracker, parties, quests,
sessions, calendar). See [README.md](README.md) for the full feature list,
deployment instructions, and project structure — that's the primary
human-facing doc; this file is oriented at agents that will either create
content in a running instance or modify the application code.

## If you're asked to create or import content into a running instance

"Content" means entities (NPCs, locations, items, ...), entity/sheet
templates (stat blocks, custom fields), player characters, random tables,
schematic elements, or map overlays. Read these first — they document the
real, *verified* field contracts, not just a happy-path guess:

- [docs/AI_ENTITY_GUIDE.md](docs/AI_ENTITY_GUIDE.md) — entities, entity
  templates, player characters, sheet templates, and the general importer's
  auto-detected kinds relevant to each.
- [docs/AI_SCHEMATIC_GUIDE.md](docs/AI_SCHEMATIC_GUIDE.md) — the SVG-canvas
  schematic editor's element schema, plus map overlays.

The **general importer** (`GET /import` in the UI; `POST /api/import/detect`
then `POST /api/import/execute` as an API) is the recommended entry point
for bulk or AI-authored content — paste/POST raw JSON and it auto-detects
what it's meant for (entity, player character, random table, schematic
elements, field template, world rules, map overlay). It also supports
importing several different kinds of content in a single call via
`{"imports": [...]}` (each entry auto-detected, or an explicit
`{"kind","data","params"}` envelope for kinds that need extra params).
Always call `/api/import/detect` on a sample before assuming you know what
`params` a given kind needs — don't guess internal ids (template ids,
schematic slugs); the importer resolves templates by name/slug instead.

Every content-creation route requires an authenticated **GM** session
cookie — log in via `POST /login` (form-encoded `email`/`password`) first.
These routes are cookie/session-based only, exactly like a real browser
session — there's no API key/token auth for them. Content routes are
GM-only by default; a logged-in player account will get 403s from all of
the above. (The separate `/mcp` server does support personal-access-token
bearer auth — see `ApiToken` in `app/models.py` — but that's a distinct
surface from the content-creation REST routes described here, and MCP
tools that require GM access still check `is_gm` at call time.)

## If you're asked to modify the application code itself

- Router-per-feature pattern: `app/routers/*.py`, each registered in
  `app/main.py`. Routers can't import from `main.py` (main.py imports the
  routers — that would be circular), but one router importing a helper
  from *another* router module is fine and already done in several places
  (e.g. `app/routers/importer.py` imports from `app/routers/characters.py`
  and `app/routers/tables.py`).
- `app/database.py`'s `_migrate()` / `_heal_table()` handles SQLite schema
  drift (SQLite can't `ALTER TABLE` away a `NOT NULL` the way most other
  databases can). A brand new table needs no migration code —
  `Base.metadata.create_all()` covers it for free — but a new **column** on
  an *existing* table does.
- Auth is GM-only by default: `_is_player_safe()` in `app/main.py` is an
  allowlist — a new route stays GM-only unless deliberately added there.
  GM-Assistants (a `WorldMembership.role == "assistant"` in the active world,
  set from the world's Members table) additionally get the routes in
  `_is_assistant_safe()` right below it — content creation/editing (entities,
  sessions, calendar, tables, boards, maps, pages, gallery, audio/video,
  imports) — while administration (Settings, `/worlds/*`, invites/members,
  backups, export, AI model/system management) stays GM-only for them too,
  and new routes default GM-only for them as well. Assistants always see what
  players see: visibility filters stay keyed on `is_gm`.
  A third tier, World **Owner** (`WorldMembership.role == "owner"`), gets
  everything an Assistant gets *plus* the `/worlds/*` administration surface
  — but only for the one world they own, via `_is_owner_safe()` right below
  `_is_assistant_safe()`, matched against their own `world_id` so it can
  never reach a different world. Instance-wide Settings, every other world,
  and granting `"owner"` itself stay GM-only regardless — only a real GM can
  create an Owner (`member_set_role` 403s an Owner who tries to mint a
  co-owner). It's how a GM hands an assistant a fully independent world of
  their own without making them a global GM.
  Watch out for the blanket prefixes in `_is_player_safe`: `/characters` and
  `/api/characters/` admit every logged-in player for *any* method and path
  beneath them, so a new route there is player-reachable by default and must
  enforce its own access (see `app/routers/character_hub.py`: `_owned_pc` 404s
  anyone but the character's owner — Notes and every write — while `_hub_pc`
  additionally lets a global GM read the shared Quests/World/Schedule tabs,
  evaluated from the owning player's point of view, never the GM's).
- Character/party rules that several routers share live in leaf modules with
  no router imports, so any router can use them without an import cycle:
  `app/pc_stats.py` (`pc_maxima` — THE rule for HP/Shock/PP/MP ceilings, where a
  stored max of 0 means "stat-derived"; never read `max_hp`/`shock_max` raw for
  a ceiling) and `app/party_refs.py` (party membership parsing,
  `parties_for_pc`, `detach_pc` — call it wherever a character is deleted so
  no party roster, loot claim or calendar event dangles — and `load_loot`, the
  stable-`lid` loot normaliser), and `app/sheet_systems.py` (read-time system
  metadata for custom-sheet templates: which field is the HP-like `vital`, which
  fields `binds` to name/player, XP fields, status-condition presets and per-system
  Rest ops — all derived per field attribute or from `BUILTIN_SYSTEMS` by slug, so
  existing databases need no migration; party vitals/roster, combat, XP awards and
  Rest go through it, never through raw `custom_fields_json` guesses). Built-in
  sheet templates are re-synced by `_upgrade_builtin_sheet_fields` in
  `app/database.py`: an untouched old row is replaced, a GM-customised one is kept,
  and field ids are never removed. **A character is native N&D only if its template
  is not a `custom`-mode system** (`pc_maxima(pc)["native"]` — leftover N&D stats on a
  Hunt in the Moonlight sheet never make it native), so any new surface that lists,
  compares, exports or prompts about characters must branch on that, not on whether
  `stats_json` is filled. The shared pieces: `app/pc_digest.py` (one system-aware line
  per character for AI prompts — chat RAG `retrieval.characters_context`, party insights,
  session prep, audio hints all use it), `sheet_systems.system_rules_markdown` (a rules
  digest per built-in system in `app/game_data/systems/<slug>.md`, which AI reviews and the
  AI character creator ground in instead of "standard N&D"), `parties._roster_table`, and
  the MCP character/party tools in `app/mcp_server.py` (every test that drives the MCP
  client must live in `tests/test_mcp.py`'s module — it imports `tests/mcp_character_cases.py`
  — because the MCP session manager is bound to one event loop). The Sheet tab is split into pages (a strip under
  the header, `static/js/sheet-pages.js`; blocks carry `data-sheet-page`):
  `sheet_systems.sheet_pages()` groups a custom template's sections — a built-in
  system lists its own groups in `BUILTIN_SYSTEMS[slug]["pages"]`, anything it doesn't
  name lands on a trailing "More" page (a section is never hidden), other templates
  with 4+ sections get a page per section. Editing a character is a *partial* update
  (`_apply_form(..., partial=True)`): only keys the form sent are written.
- **A GM's own sheet template is a full system, same as a built-in**: `SheetTemplate.system_json` (conditions,
  Rest ops, pages, roster groups, optional hp/xp/binds) + `rules_md` are read through `sheet_systems.system_spec(tpl)`
  — the built-in table overlaid with the template's own — and are always stored through `clean_system_spec`
  (references to missing fields dropped). `app/template_draft.py` is the validator for anything an AI proposes
  (`clean_template_draft`: safe unique ids with system references remapped, known types, one HP track…) and the
  rulebook chunker; it backs the "Draft with AI" page (`app/routers/template_ai.py`) and the MCP tools
  `create_sheet_template` / `update_sheet_template`, so never store a model-written template without it.
- **The character page is the player's home - nothing in it should send them away.** The hub include (`characters/_player_hub.html`)
  has a sticky live strip (`hub/now`: vitals with ± buttons - N&D through `hp-async` / `shock`, a custom system through
  `/api/characters/{id}/resource`, whose steppers also update the custom sheet's own `[data-cf-id=<id>_current]` input so its next Save
  cannot write the old number back - plus the combat turn, which never names a GM-run combatant) and a **Pages** launcher
  (`hub/places`: the PLAYER role's visible sections). Every in-app link inside the hub opens in `static/js/pc-viewer.js` (an
  `?embed=1` iframe over the character; a FRESH iframe per open with `src` set before insertion, because changing the `src` of a frame
  already in the page adds session-history entries and breaks Back); use `ndPcViewer.open(href, title)` for new links, never
  `target=_blank`. On a phone (<=720px) the tab strip is a bottom bar, secondary tabs (`pch-tab--minor`) live under More - keep new
  tabs short-labelled. `docs/PLAYER_CHARACTER_IDEAS.md` has the backlog.
- **Fan control is safety-relevant code** (`app/fan_control.py`, a leaf; System Monitor > Fans; `docs/GPU_SETUP.md` "Fan control"): a passive GPU
  is cooled by a motherboard header, so a wrong write can stop its air. Keep these invariants: an output is identified by
  `chip@device/pwmN`, never by `hwmonN` (the kernel renumbers it) and never by a path from a request; a speed is never under `MIN_PERCENT`;
  a follow curve must pass `clean_curve` (reaches `TOP_PERCENT` before the panic temperature); an unreadable GPU temperature means 100 %;
  fans speed up at once and slow down by `RAMP_DOWN`; turning following off or dropping a fan puts it back as the BIOS had it
  (`give_back` restores the remembered mode + duty - it never writes the chip's own auto mode, which is not the BIOS curve on boards like
  the X570 Aorus Master); an output `check_fan` found inverted never follows; `shutdown_safe` only ever raises. The sysfs root is a
  parameter (`FAN_HWMON_ROOT`), and the container should get only the fan chip's folder, not a writable `/sys`. Tests use a fake hwmon tree
  (`tests/test_fan_control.py`).
- **Copy / paste for media is one shared script** (`static/js/nd-clipboard.js`, loaded by `base.html` on every page; pure helpers tested under Node in
  `tests/test_nd_clipboard.py`): **Ctrl+V** hands pasted files to the visible `<input type=file>` whose `accept` takes them (sets `.files`, fires
  `change`, focuses the page's upload button) - so a new upload field needs NO code of its own; opt a field out with `data-nd-paste="off"`. It never acts
  while typing in a text field, and a page with its own paste handler (the session media panel) keeps priority by calling `preventDefault()` - the shared
  one defers a tick and checks. **Ctrl+C** with nothing selected copies the media under the pointer: a picture as a picture (plus its address in the same
  clipboard item), audio / video / `/uploads/...` links as an address; mark a custom copy target with `data-nd-copy-url` (and `data-nd-copy-name`).
  The session media panel prefers a pasted ADDRESS over pasted picture data (it attaches the existing item instead of uploading a copy), and
  `POST /api/sessions/{id}/media/attach` takes `url` for audio / video as well as images. Never add a per-page paste listener for a plain upload field.
- **Small image boxes use the thumbnail**: `{{ url|thumb }}` for `src` and `data-full="{{ url }}"` for the original
  (lightbox, "send to screen"); a 160px portrait that downloads a 1 MB PNG is the slow-page bug.
- **The Player Cockpit lives inside Player Characters** (no nav item of its own — `nav_menus.py` has none; saved menus that still name `player_cockpit` just drop it): the hub's owner-only 🎛 Cockpit tab lazily loads
  `/player-cockpit?pc=ID&embed=1` in an iframe, the list has a Cockpit button, and `?pc=` is honoured only for the
  viewer's OWN character — a GM may focus any character of the world and gets that player's view (`cockpit._viewer_pcs`
  decides whose characters/parties the cockpit is for; `CK_MY_PCS` / `CK_FOCUS_PC` / `CK_VIEW_AS` in cockpit.html,
  `focusMyCharacter`, `LS_KEY` and `BOARD_URL` in cockpit.js — a GM's view-as layout is stored under its own key so it never
  overwrites their GM Cockpit). Hub tabs are listed in `_player_hub.html` (`TABS` + `loaders`).
  **Every cockpit window is a whole page in an iframe** (~15-35 MB and ~15 requests each, measured), so frames go through
  `static/js/cockpit-frames.js` (`ndCreateFrameLoader`): loaded only once showing, 4 at a time (2 on a phone), and given back
  (`about:blank`) after a minute hidden by the layout (collapsed window, inactive phone tab, the hub's hidden Cockpit tab) -
  never set an iframe's `src` directly or use `frame.src = frame.src`, use `frames.mount` / `frames.reload`, and `frames.forget`
  when a window is removed. `CK_MAX_PANELS` mirrors `cockpit.MAX_PANELS` (the server rejects a bigger layout whole), and
  `saveNow` reports a rejected save. `tests/test_cockpit_scaling.py` runs the loader under Node.
- **Cockpit window geometry is screen-relative** (`static/js/cockpit-layout.js`, pure functions tested across monitor sizes in
  `tests/test_cockpit_layout.py`): a layout is saved with the workspace size it was arranged for (`vp`) and opened on another
  screen it is *scaled* (`scalePanels`), pulled back inside (`fitPanels`) when it has no `vp`, and re-scaled live when the
  window resizes or goes fullscreen (cockpit.js `scheduleAdapt`, no iframe reload, nothing saved until the user changes
  something). The default layout and 📌 Auto-arrange are `tilePanels` (a map/combat *stage* plus tiles, columns chosen from
  the width); a window added later is `sizeFor`/`cascade`. Never hard-code window pixels in cockpit.js again.
  **Below ~1700px of window width the cockpit is a different layout, not a smaller one** (a Vivobook is 1280-1536 wide; scaled from a
  4K arrangement every window was a 240px sliver): `screenClass` (by WIDTH only, with hysteresis, so F11 never flips it) picks
  `'laptop'` or `'wide'`; the laptop shows 1-3 windows tiled big and keeps the rest `parked` in a dock of chips (`showLaptop` swaps a
  window in for the one brought in last, `parkLaptop`, `retileLaptop`, `makeMain`, `deriveLaptop` for a first visit). The windows are
  SHARED between the classes but each class has its OWN geometry: a saved panel's x/y/w/h/z/collapsed are the big screen's and are
  never written by the laptop, the laptop's travel as `lap` (+ `lvp`, its reference screen); working panels hold the current class's
  geometry in those same fields and stash the other in `_wide` / `_lap`, so only `toWorking` / `toSaved` know there are two - save
  through `savedLayout()`, never `panels` directly, and never put layout state (`parked`, `swapped`, `_wide`, `_lap`) anywhere else.
  A parked window is `visibility:hidden` with a real (clamped) box, NOT `display:none`: its page loads with the rest and is never given back,
  so a window in the dock is as live as one on screen (a 0x0 frame would not load, and a map initialised at 0x0 fits wrong).
  The class, the slot count and the site-bar toggle are per-browser (`nd_cockpit_mode|slots|site` in `localStorage`), not world data.
  A change of class only restyles in place (no re-render: pages stay loaded, chats keep their text). `tests/test_cockpit_laptop.py`
  drives the maths under Node across laptop sizes and the server keeps/validates `lap` / `lvp`.
- **Cockpit windows behave like a window manager** (`cockpit.js` drag/resize block + the pure `resizeBox` / `snapBox` in
  `cockpit-layout.js`, tested in `tests/test_cockpit_window_manager.py`): eight resize handles per window (`.ck-rz-*` sit just
  OUTSIDE the frame so they never cover the scrollbar of the page inside; the visible corner grip is the south-east one), moves
  and resizes snap to the workspace's edges and to other windows' (guide lines while a snap holds, `SNAP_PX`), **Ctrl held =
  free placement** (no snapping, no 8px grid). The window you last pressed on is `.ck-active` and raised - a press INSIDE a
  window's page never reaches the cockpit, so `mkIframe` listens in on the same-origin page on every `load`
  (`relayFrameEvents`; the same hook is where keys are relayed) and a focus move into a frame does it through the window's
  `blur`. Every iframe is titled with its window's title (`titleFrames`); icon-only controls inside panels get their tooltip as
  `aria-label` (`labelControls`). Site UI scale (`nd_ui_scale`) goes 90-200 % for a 4K screen.
- **The cockpit workspace scrolls** (`#ck-viewport` is `overflow-y: auto` and `position: relative`; every window is `position:
  absolute`, so the workspace is as tall as its lowest window - there is no separate canvas). So a window can be made taller than the
  screen and more kept open than fit. Pure maths in `cockpit-layout.js` (`autoScrollStep`, `revealScroll`, `scrollPages`, `pageStep`,
  `wheelPixels`, `cascade(..., top)`; `tests/test_cockpit_scroll.py`), wiring in the "scrolling the workspace" block of `cockpit.js`:
  (1) a page in a window takes the wheel for itself, so **Alt + wheel** scrolls the workspace instead - one `wheel` listener
  (`workspaceWheel`) on the cockpit's window AND, via `relayFrameEvents` -> `watchWheel`, on every page in a window. It must be NON-passive
  (it cancels the page's scroll), and a blocking wheel listener makes every scroll over its page wait for the main thread, which the cockpit
  and ALL its windows share - so `syncWheel` adds it only while Alt is down (keydown / keyup on every host; a cheap passive listener
  catches an Alt pressed where the page could not see it and a plain wheel while still armed) - never leave one on. The per-browser option
  `nd_cockpit_wheel` (🧩 Layouts → "Wheel scrolls the workspace") turns it round over a PAGE: the plain wheel scrolls the workspace, Alt +
  wheel gives it back to the page, and the pages stay armed; the cockpit's own document (dock chips, menus, a notes window's text) never loses
  its plain wheel. Plain wheel over a title bar or gap scrolls the workspace natively, the neon scrollbar is styled (`#ck-viewport` scrollbar rules), **Alt + ↑ / ↓** (+ Shift: to the ends - table
  entries `scrollup` / `scrolldown` / `scrolltop` / `scrollend`, not while typing) and the **▲ 1/2 ▼** in the status bar do it too;
  (2) dragging or stretching a window near the top / bottom edge scrolls by itself (`autoScroller`, by TIME not by frame, with an 8px
  dead zone so a wobbly click does not start it) - and because a window's bottom edge cannot pass the pointer, which cannot pass the
  screen, a workspace that FITS has nothing to scroll into, so once a drag scrolls down an invisible `#ck-spacer` gives it half a screen
  below the window until the button is released (created lazily: an ordinary drag never makes the scrollbar flicker); y stops at
  `MAX_Y` (8000), where the server stops keeping it; (3) `revealWin` brings a window into sight when it is shown from the dock / the
  Windows list / Alt+1..9, added or reopened (the least scroll that shows it; one taller than the screen: its title bar), new windows
  open where you are looking (`cascade(..., scrollTop)`), half-dock fills the half in sight, and a new arrangement (scene, saved
  layout, auto-arrange, slot change) scrolls to the top; (4) a maximised window is `top: 0` of the CONTENT, so a MutationObserver
  (`syncMaxLock`) scrolls to the top and locks the workspace (`.ck-maxed`) until it is given back. **Do NOT reserve the scrollbar's
  place** (`scrollbar-gutter: stable`): layouts arranged to fill the screen would lose ~15px at the right edge, hiding half the
  scrollbar of the page inside their right-hand windows.
- **Roomy windows** (🧩 Layouts, per browser `nd_cockpit_roomy`): `tilePanels` never makes a tile shorter than `pref.minH(vp)` - cockpit.js hangs
  `minH` on `prefHeight`, the height function EVERY tiler already receives (auto-arrange, scenes, the laptop's tiles), so one option covers all
  of them without threading a parameter; `roomyHeight` = 60% of the screen, 300-560 px. Many windows stay big and the workspace scrolls.
- **Text selection menu** (`text-format-toolbar.js`): ONE menu (`ndFmtOpenMenu`) for the editor textarea (`data-fmt-gmonly` fields) and, for a GM, the
  rendered entity page. Right-click on a selection: hide from players (`[gmonly]`), spoiler, B/I/U/S, highlight, 8 colours, copy, search the
  world, new entity from the text; inside a GM-only part or spoiler it offers to take it off. Letting go of a selection shows a small bubble
  (hide / spoiler / more). Editor: `ndFmtWrapSelection` on the textarea. Read view (`ndFmtSetupReadMode`, elements with `data-markup-field`
  = body | summary | note, `data-markup-entity` on the article, GM only): the selection + ~80 chars of context is POSTed to
  `/api/entity/{id}/markup`, `app/markup_locator.py` finds it in the markdown (mask markup, collapse whitespace, search, score by
  surrounding text, map back, one wrapper per line, widen over half-covered emphasis) and the page reloads. It REFUSES (422 + reason)
  when the text is ambiguous, not in the source, cuts across formatting, or the note is rich text - a wrong guess would hide or leak the
  wrong text. In the read view `[gmonly]` inside a paragraph renders as an inline `gm-only-text gm-inline` span, a whole-block one as a box.
  More actions on the same menu: **Undo** (the read-view POST returns `undo:{previous,expected}`, kept in sessionStorage across the reload;
  `action:"restore"` puts `previous` back only while the field is still `expected`, so it never clobbers a later edit), **Roll** (a
  selection like `2d6+3` -> `/api/dice/roll`), **Show on the second screen** (`/api/display/show`), **Rewrite with AI** (`/api/ai/rewrite`
  is a PREVIEW; accepting does `action:"replace"` in the read view / `setRangeText` in the editor; one line only), **Send to a player**
  (`/api/entity/{id}/send-to-player`: text is taken from the SOURCE, parts inside `[gmonly]` are dropped by position AND by
  `strip_gm_only`, never from what the browser says it selected), **Add as another name** (`/api/entity/{id}/alias`). Right-click with
  nothing selected: the block under the pointer (paragraph / list item / quote: hide or spoiler; heading: the whole section) and, on an
  entity link inside an entity page, open in a tab / floating window / second screen / copy. Wrapping over a part that is already
  `[gmonly]`/`[spoiler]` removes the inner tags (nested tags would pair up wrongly when rendered).
  Also: **Make a quest from this** (`/quests/new`), **Save as a fact** (`/facts/new`, GM-only), a **read-aloud box** (`[read]…[/read]` ->
  `<span class="read-aloud">`, everyone sees it; toolbar 📖 and menu item), and in the editor menu a row of block formats (heading / quote /
  bullet toggled per line, link, clear formatting). `/entity/{id}?as=player` previews the page's content as a player gets it (GM only;
  body, summary, notes and the AI panel's context are stripped of `[gmonly]`; the read-view menu hooks are off). Not built yet: per-player
  secrets and timed reveals (they need the same strip-everywhere treatment as `[gmonly]`), add-to-combat.
  **Per-player secrets and timed reveals** (`app/rendering.py`): `[for=ID,ID]…[/for]` (visible only to those users, by user id) and
  `[reveal=YYYY-MM-DD[THH:MM]]…[/reveal]` (UTC; hidden until then, plain text after). Both are access control and are resolved INSIDE
  `strip_gm_only(text, viewer_id=None, now=None)`, so EVERY existing call site that strips for a non-GM already hides them (fail-closed):
  a `[for]` part shows only where the caller passes `viewer_id` (entity page, notes, download.md, hover preview, search snippets; the GM's
  `?as=player&player=ID` preview); AI/RAG context, the second screen and every other consumer drop it for everyone, including its own
  player. Reveals need no viewer: once due they are plain text everywhere. Malformed tags hide, never show: an opening tag that never
  closes hides the rest of the text, a bad date stays hidden; nested blocks resolve innermost-first so an outer restriction still wins.
  The GM sees both rendered as labelled parts (`.for-only-text[data-for]`, `.reveal-later-text[data-reveal]`; the page script fills in
  player names / local time). From the menu: "Secret for one player…" (`action:"for:ID"`, the id must be a player of the world) and
  "Reveal to players later…" (`action:"reveal:ISO"`, picked in local time, stored UTC); these tags refuse to nest when marked from the
  read view. A due reveal appears on the player's next page load (nothing is pushed). Search cannot be used to probe secrets: `retrieval.drop_secret_only_matches` drops, for a non-GM viewer, any entity (search,
  RAG lookups, ILIKE fallback) whose only match is text that viewer cannot see, and note search checks the stripped text.
  An entity page's breadcrumb (`#entity-breadcrumb`) links back through its folder and every subfolder.
- **Pictures in text start small and can be stretched** (`text-format-toolbar.js`): an image uploaded / pasted / dropped through the toolbar is
  written `![alt](url "size:30")` (`NDFMT_DEFAULT_IMAGE_PCT`; audio/video keep their kind marker; pictures already saved without a marker stay full
  width). On an entity's read view a GM hovering a picture gets a corner grip (`_ndFmtSetupImageStretch`); dragging it resizes live in steps of 5 %
  and on release POSTs `action:"imgsize:PCT"` (`index` = the picture's place among the field's pictures, `snippet` = its address) to
  `/api/entity/ID/markup`, handled by `markup_locator.resize_image` - which refuses (422) when the n-th picture in the source is not that address or
  has its own caption, and 100 removes the marker. The response carries the usual `undo`. Clicking a picture still opens the lightbox with the ORIGINAL
  file (the size is only CSS width).
- **Rename an entity from the right-click menu** (`static/js/entity-menu.js`, loaded by base.html for editors only): right-click an entity link
  (`/entity/ID`) on ANY page - open in tab / floating window / second screen / copy link / ✏ Rename… - or the title of an entity page
  (`.detail-meta > h1`, `article.detail[data-entity-id]`). Shift+right-click still gives the browser's menu. On an entity page's text the richer
  menu in text-format-toolbar.js owns links (its `_ndFmtLinkItems` also calls `window.ndRenameEntity`, which fetches the real name from
  `/api/entity/ID/preview` before opening the dialog). Saves via `POST /api/entity/ID/rename` (allowed for assistants in `_is_assistant_safe`);
  Undo is another rename; names of links to it on the page follow. Text elsewhere that mentions the old name is left as written and just stops
  auto-linking.
- **Combat tracker** (`app/routers/combat.py`, `combat/detail.html` GM page, `combat/player.html`, `app/combat_rules.py`, `combat_state.py`,
  `combat_turns.py`, `static/js/combat-core.js`). Rule-system aware: an encounter has a `ruleset` (nd, worm, hunt, asterion, nd_asterion,
  worm_asterion, generic - written against the four rule books and the two Asterion-engine conversions) and `combat_rules.RULESETS` is the ONE
  table of how each runs (initiative vs fluid turns, action economy pips, HP/Shock/shield labels and damage absorbers, what 0 HP means, conditions
  with stacks / ticks / action penalties, armor caps, counter presets); the page gets it as `RULES_JSON`. **Initiative** systems (N&D, Worm,
  generic): numeric initiative, `active_idx` is a POSITION in initiative order (then `tiebreak`, then added order) - `combat-core.js` `keepActive`
  re-finds the active combatant by id after every change, and `combat_turns.order` sorts identically (tests/test_combat_core.py runs both).
  **Fluid** systems (Hunt, Asterion, the conversions): `turn_json` `{active_id, acted, picker}`; the actor ends the turn and CHOOSES who goes next
  (the GM page's chooser, or a player through `/hub/pass-turn`); the last actor of a round opens the next. Every consumer of "whose turn" (maps, hub,
  cockpit chip) goes through `combat_turns.active_combatant_id`. **Saving**: `normalize_state` cleans the body; every writer bumps `rev`
  (`combat_turns.touch`); a save carrying a stale `rev` gets 409 + the current state and the page three-way merges (`merge3`, per combatant and
  field) instead of overwriting; the page shows Saved / Saving / Not saved (retrying) and warns before leaving unsaved. **Undo/redo** is
  client-side (your own recent edits; cleared when others' changes arrive); the persisted event log is `log_json`. **Players** never get the GM
  page or data: `combat_state.player_view` (enemies as "Enemy A" unless `name_known`, a word for their health, no armor/conditions/notes, `visible:false`
  combatants omitted) feeds both the player page and `/api/combat/ID/state`. Per combatant extras: `temp_hp`, `res` (Spark Shield, Ichor, Stamina,
  PP/MP - read from the sheet, written back by Sync), `used` (action pips), `saves` (N&D death saves / wake-up rolls), `wounds` / `press_on` (Hunt and
  the conversions), `counters` (Legendary Action ...), `notes` (GM only). Conditions stack (a name listed twice) and tick through a confirm panel
  - nothing is applied silently. Asterion: no death saves, Shattered can be executed; Armor never above 3 (4 vs legendary).
  Also: **Turns** can be overridden per encounter (`turn_json.mode` "initiative" | "fluid" - the Asterion-engine books allow a fixed order);
  **initiative** is d10 + `init_bonus` (Speed: Dex + Intuition from a sheet, or a creature's speed field), rolled per row or for every
  enemy without one (Shift re-rolls all); ties are ordered by hand (`tiebreak`, ▲▼); a combatant that cannot act (dead, or down - the
  ruleset's `skip_down`: enemies / all / none) is skipped by `nextTurn`/`advance` and never holds a fluid round up (`combat_rules.is_out`
  and `combat-core.js isOut` agree, tested on the same cases); a condition typed "Burn 3" lasts 3 rounds (`cond_dur`, ticked down
  each new round); Phase Break (boss: shield refreshes, Phase counter up).
  **Lifecycle**: "Run again" (`_copy_encounter`: NPCs full HP, nothing spent, PCs keep their HP), templates (`is_template` rows: the NPCs only; Run adds
  the party; hidden from lists, cockpit, maps and players), "End encounter" (`finished_at` + a GM-only `combat` fact written from the stored state
  and log), "Pull from sheets" (`/pc-state`; the other half of Sync) and the second screen (`display_combat.html`, `/api/display/combat/ID/state`
  - always `_player_state`, so a GM-signed-in TV window never carries enemy names or numbers). AI Tactics sends the ruleset, whose turn it is,
  conditions with stacks, armor, resources and who is out.
- **Session media can be named, and the character hub has a Sessions tab**: `POST /api/sessions/ID/media/{image|video|audio}/REF/rename` sets a
  picture's caption or a clip's own name (the ✏ in the session page's media panel) - the Session Log (`session_media.viewer_media`) already shows
  those names to players. The hub's 📓 Sessions tab (`GET /api/characters/ID/hub/sessions`) lists the world's Session Log through the PLAYER view
  (`viewer_media(..., is_gm=False)` even for a GM looking in, nothing hidden leaks into names) and opens each session in the pc-viewer.
- **Keepsakes** (`CharacterKeepsake`, the hub's 💾 Kept tab, `character_hub.py` "Keepsakes" block): a picture / video / song / text that was shown or sent to
  the players, kept on ONE character under the player's own name and a 400-character description - by the owner, or by the GM for them. The client
  only says WHICH thing (`source`: handout, a note the GM sent that player, a Session Log item); `_resolve_keepsake_source` takes the content from the
  server's copy (notes stripped of `[gmonly]`, Session media through the player view), so nothing a player could not see can be kept. Only a GM may add
  a raw `/uploads/` address or text. Capped at `KEEPSAKE_MAX`, removed with the character (`char_extras.delete_for_character`). The dialog is one sheet
  for keeping and editing: a bottom sheet on a phone, 16px inputs (no zoom), 44px buttons.
  **A player's own files** (`own=True`, `POST hub/keepsakes/upload`, owner only) are a separate section: limits per CHARACTER and kind come from the
  world (`World.personal_images_max/audio/video`, `app/personal_files.py`: 30 / 10 / 5 by default, blank = default, 0 = off; set in Edit World) and are
  checked before the file is written; deleting one deletes its file. Kept pictures (not own uploads) count as "used" in `discover_world_images`, so
  the gallery cannot delete them; a player's own uploads stay out of the world's gallery.
- **View layouts** (docs/VIEW_LAYOUTS_PLAN.md): a list page offers several layouts through ONE engine - `app/view_prefs.py` (registry + `clean`), `_views.html`
  (`view_bar(section, target, request)`), `static/js/nd-views.js` (controls, apply, remember, account save; pure decisions tested under Node), CSS in style.css
  keyed on `.nd-collection[data-view][data-size]`. Items are `.nd-item` with `data-name/uses/date/size/source`; a page that adds items later calls
  `container.ndViews.refresh()`. A choice is URL > account (`user_view_prefs`, `/api/view-prefs`) > this browser (`nd_view:<section>`) > default, and a hidden
  item is always unchecked so bulk actions never act on what is not shown. Never add a per-page grid / toggle again - register the section.
  Audio has two layouts with behaviour of their own in `static/js/audio-views.js` (soundboard pads, playlist bar; every clip still uses its own
  `<audio id=audio-el-ID>`, so Loop / Pop out / subtitles are untouched); they listen for the engine's `nd-view-change` event on the collection. The entity list's card grid (`/kind/<kind>`, `view=grid`) uses the same
  engine (`entities` section; cards are `.entity-card-wrap.nd-item` with `data-g-subtype` / `data-g-tag` - any `data-g-<key>` is a usable group key): compact,
  portrait wall, and a split view whose preview (`static/js/entity-views.js`) is `GET /api/entity/ID/preview`, so it shows exactly what that viewer may see.
  The table view stays server-rendered (`?view=table`, its columns come from the field template). The private-notes page uses it too (`notes` section,
  `static/js/notes-views.js`: outline toggles in place, the reader copies the server-rendered note; on a phone the reader is the outline).
  Boards are the shared `static/js/nd-board.js` (`ndBoard.attach(collection,{key,columns,editable,idOf,move})`): entity subtype board and the quest status board;
  an entity page's CONNECTIONS (`connections` section, `#conn-items`: related + backlinks are one collection of `.nd-item`s with `data-dir`; the web is `static/js/connections-web.js`, SVG built from those cards, hidden on a phone) and NOTES (`entity_notes`, the private notes' `.pn-note` hooks on `.en-note`, same `notes-views.js`) use it too;
  the home page (`home` section: Classic / Session night / Focus / My widgets; `static/js/home-layouts.js`; blocks are tagged `data-hb`, re-ordered with CSS `order` and hidden, never rebuilt; the layout registry gained generic id `lists` - here `order` and `hidden` - next to `options`; Session night reads `/api/schedule/state`, `/api/cockpit/player-board` (GM: `/api/quests/board`, `/api/combat/recent`), so it shows only what that viewer may see);
  reading pages (phase 5) on one generic helper `static/js/nd-pagelayout.js` (`data-pl` / `data-pl-modes` / `data-pl-default` / `data-pl-saved`, `[data-pl-bar]`, events `nd-pl-ready` / `nd-pl-change`; macros `pl_bar` / `pl_attrs` in `_views.html`): `rules` (Scroll / Book / Wide; `static/js/rules-book.js` pages the existing sections, ← → / swipe), `search_results` (Grouped / Flat / Cards), `facts` (By session / Timeline / Table), `recap` (Full text / Sections; `static/js/recap-sections.js` folds the recap at its headings, text moved not rewritten);
  phase 6 on the same helper: `maps` (Grid / List / Large), `map_viewer` (Map only / Map + list: `#map-side` lists the pins already on the Leaflet layers, click flies there), `boards` (Cards / List), `parties` (Cards / Table), `party_page` (Long page / Dashboard: the page's cards tile in a CSS grid), `calendar_view` (Month / Week: `static/js/calendar-week.js` shows one row of the month grid with ◀ ▶), `ai_chat` (Chat + side panel / Chat only: the side panel becomes the ☰ drawer at every width);
  phase 7: `catalog` (races / professions: Cards / Table / Compact), `handouts`, `worlds`, `templates` (today's list stays the default), `settings_page` (Tabs / One page + search: `static/js/settings-onepage.js`, the panes all show, the tab row scrolls, a search hides non-matching `.form-group`s); presets `view_prefs.PRESETS` (Table night, Prep, Player phone, Reading) via `POST /api/view-prefs/preset/{id}` (merged onto the person's own choices; site theme / scale / pins kept) and buttons on Account ▸ Layout & appearance; `World.layout_preset` (world edit form) is applied once by `routers/auth.py` `_start_with_world_layout` to a player who joins the world with no saved layouts;
  last layouts: `rules` Search first (nothing shows until you type; a Contents click shows the rules), `recap` Cards, `board_view` (Canvas / Outline / Timeline: `static/js/board-layouts.js` reads the page's own `nodes` / `edges`; timeline = events by a date found in the title or text, undated ones by canvas x), `calendar_agenda` Eras timeline (`static/js/agenda-eras.js` adds a heading row per year), `combat_list` (Cards / Table / By status), `jobs_list` (Cards / Compact / Table), `session_gm` (Long page / Tabs / Run mode: blocks tagged `data-sp`, `static/js/session-gm-layouts.js`), `map_viewer` Mini-map;
  the table-night tools: dice (`dice`: 3D tray / Quick buttons with saved formulas in this browser / History first - `static/js/dice-layouts.js`, every roll still goes through the page's own form), random tables (`tables`: one `#table-cards` collection that OPENS grouped by category through the registry's `default_group`; the Roll board logs results in `#tbl-log`) and the combat tracker (`combat`: Standard / Compact = the tracker's own compact setting / Cards / Turn view / Big screen - `static/js/combat-layouts.js` only re-arranges, loaded after the tracker's script);
  an entity's own page (`entity_page` section: Long page / Tabs / Two columns / Reader; `static/js/entity-page.js` shows, hides or MOVES the blocks tagged `data-ep="overview|details|notes|links|media|ai"` - never rebuilds them - and puts them back in their first order on every change; two columns need >= 900 px; the last tab is remembered per kind in this browser);
  the SITE shell (`site` section: `options` nav top / rail / compact / bottom, width, density, theme incl. high contrast, scale, motion + `pins`; `static/js/nd-site.js`, CSS block "Site shell" in style.css; edited on the Account page `_site_layout_panel.html`, every control applies at once). The server renders the person's choice into `<html data-nav data-width data-density data-motion data-acct-theme data-acct-scale data-pins>` from `app/site_layout.py`, which keeps it in the SESSION cookie (written at login and on save, refreshed from the DB every 15 min) so no page render costs a query. Anything that measures the top bar must use `window.ndNavOffset()` (a side rail takes width, not height); nav links carry `data-nav-id` and `.nav-ic` / `.nav-lb` spans;
  the cockpit's LAYOUT STYLE (`cockpit` section: free / rail / tabs / grid; `arrangeStyle` in cockpit-layout.js is the pure planner, cockpit.js `reflow()` applies it after every change and blocks drag / resize while a managed style is on; saved per person like the other layouts) and the GM cockpit's scenes are one click away (`#ck-scenebar` chips + `#ck-welcome-scenes` tiles, `Alt+Shift+1-4`; GM only);
  the character page's own layout (`hub` section: Tabs / Play / Dashboard / Split, `static/js/hub-layout.js`; its `filters` are the tabs PINNED to the phone's bottom bar, max 4, Sheet always one; every player chooses their own and it is saved to their account via `/api/view-prefs/hub`; Play = `body.pch-play`: no edit chrome, bigger controls, swipe between sheet pages; Dashboard / Split on a screen >= 1100 px move the pinned tabs' own `.pch-panel`s into `#pch-rail` beside the sheet - they stay inside `#pc-hub` so their loaders keep working - and are plain Tabs on a phone) uses the same store;
  a board's notes (`/boards/<slug>/notes`, `board_notes` section, `#bn-notes`, same `notes-views.js`; plain escaped text, same access as the board) and the players' Session Log list (`session_log`, `#sl-list`) too;
  quests (`quest-views.js`), sessions (`sessions` section, group by party) and characters (`characters` section, roster / wall) are registered sections too.
- **Quick rename of media** (`static/js/media-rename.js`, loaded by base.html): a ✏ button with `data-nd-rename data-kind="audio|video|image"
  data-id|data-url data-name` opens a dialog and saves through `/api/media-rename/apply` (the AI renamer's route, so it is logged and undoable
  - the notice offers Undo), then rewrites every `[data-rename-label="kind:ref"]` / `[data-rename-img]` on the page. Clips: their own `name`
  (never empty). Images: a `MediaTitle` label beside the URL - the file is never renamed - and an empty name resets it to the automatic one.
  Buttons are on the audio and video libraries, the album image tiles and the All Images tiles (including the "Load more" ones), editors only.
- **Cockpit keyboard** (`static/js/cockpit-keys.js` - pure, tested in `tests/test_cockpit_keys.py`): ONE table (`SHORTCUTS`) is both the
  handler (`shortcutFor`) and the F1 help (`helpRows`), so they cannot drift apart. Keys match by `e.code`, never by character
  (Russian / Hebrew layouts), use Alt + letter/digit (never Ctrl+Alt: that is AltGr and types characters), skip the browser's own
  keys, and wait while typing on a Mac. A window's page never delivers its keys to the cockpit, so `relayFrameEvents` (called from
  each iframe's `load`) listens in on it and feeds the same `onKey`. **Ctrl+K belongs to `command-palette.js`**: a page inside a
  frame forwards it to the nearest ancestor palette that has providers (`hostPalette`), because a palette confined to a window is
  useless; the cockpit plugs in through `window.ndPalette` (`addProvider`, `setEntityOpener`, `setPageOpener`) - registered on
  `DOMContentLoaded` because the palette script is `defer`red. The palette sits at z-index 100020, above every cockpit layer.
- **A "page" window is any page of the app** (`type: 'page'`, `ref` = a path on this site): opened from the quick switcher (a menu
  entry's Enter) or the Add dialog's Page picker. The address is data that goes into an iframe `src`, so it is checked twice with
  the same rule (`_PAGE_PATH_RE` in `app/routers/cockpit.py`, `validPagePath` in `cockpit.js`; `tests/test_cockpit_page_windows.py`
  asserts they agree): one leading slash, no scheme / `//host`, no whitespace or backslash, 120 characters. Python's `$` lets a
  trailing newline through - use `\Z` for any such check.
- **The dock is on both layouts, and a scene is a view over the windows** (`showWide` / `parkWide` / `applyScene` in `cockpit-layout.js`,
  `tests/test_cockpit_scenes.py`): `parked` is saved at the TOP level for the big screen (absent = showing; the laptop's own flag stays
  `lap.parked`), and `toWorking` / `toSaved` carry both - so a trip through the laptop never changes who waits in the big screen's dock.
  `applyScene(panels, ids, {klass, vp, slots, pref, stage})` shows exactly `ids` (the first is the stage / main), tiles them, parks
  everything else and NEVER closes or rebuilds a window (`cockpit.js` `sceneWindow` reuses the one used last of each type or adds a
  missing one through `addPanel(..., {scene: true})`, which skips its own placement and save; `applyScene` saves once). The scenes
  (`SCENES`) are GM-only; an entity window is never invented. Header controls for presets live in the 🧩 Layouts menu
  (`#ck-layouts-pop`, which keeps the ids `ck-pin-btn` / `ck-save-preset` / `ck-reset`); presets are applied / deleted / saved through
  `applyPreset` / `deletePreset` / `saveLayoutAs` (never read a `<select>`), and `CK_MAX_PRESETS` mirrors the server's cap.
- **GM tools in cockpit windows** (`tests/test_cockpit_tools.py`): the party window's HP / Shock buttons are GM-only and go through the
  sheet's own routes (`hp-async`, `shock`, `/resource` with the template's vital track); `memberClick` shows the change at once
  (`nudgeHpText`), QUEUES per character (`memberQueue` - never fire-and-forget: five fast -1s must be five changes) and refetches
  (`loadLive`) when the last has landed. `_member_vitals` carries `shock` / `shock_max` (None on a custom sheet). A window's **zoom** is
  `data.zoom` (50-200, absent = 100; `_sanitize_panel_data` clamps it; `zoomStep` / `zoomBox` are pure in `cockpit-layout.js`): the first
  child of `.ck-body` is scaled from the top-left and given the inverse size - apply it AFTER the window is assembled (`buildWin` looks
  inside it). Never assign `p.data = {...}`: merge (`Object.assign({}, p.data, ...)`) or the zoom / timer is lost. `framePath(p)` is the
  ONE mapping from a window to its page (building the iframe and Pop out both use it). The **lock** is per browser (`nd_cockpit_lock`)
  and checked in `wireDrag` / `wireResize` and the menus, not a saved-layout field. The combat status chip follows the first combat
  window with a `ref` and refreshes on the live bus plus a slow timer.
- **The timer window is wall-clock** (`static/js/cockpit-timer.js` - the state machine, pure, `now` passed in, tested in
  `tests/test_cockpit_timer.py`; `buildTimer` in `cockpit.js` only draws it): a countdown stores its END TIME and a stopwatch its START
  TIME (epoch ms) in the window's `data.timer`, so a throttled background tab (hidden-tab timers run once a second, then once a minute),
  a sleeping laptop, a reload and an Undo-close all show the true time - never count ticks. The server keeps `data.timer` only on a
  `timer` panel, clamped (`_sanitize_timer`); a saved layout is stripped of timers (`stripTimers`), because a running one is a moment, not
  an arrangement. An alarm is visible as well as audible (`setAlarmUi`: pulsing window + dock chip, tab title, toast) - sound alone is
  lost when the tab is muted or the window parked. `_clamp_int` also catches `OverflowError` (a JSON `Infinity`). The window is a CSS size
  container (`.ck-timer`, `container-type: size`): the time grows with it and a short window drops the presets, then the controls
  (`@container` rules measure the CONTENT box - and a container cannot restyle itself, only its children); the time is a button too.
- **Pages in windows are compact** (`body.nd-embed` block in `static/style.css`; inline-styled elements need `!important` to be
  overridden, as the dice page's title row, tray and cards do): smaller headings and padding so the content gets the room.
- **Images in cockpit / floated windows open full screen in the MAIN window** (`static/js/nd-lightbox-bridge.js`, tested under
  Node in `tests/test_lightbox_bridge.py`): an embedded page's `openLightbox()` posts the image up to the window above (same
  origin, image addresses only, passed up again through nested frames), because an iframe's lightbox is confined to its
  window and the GM's 📺 *Send to screen* / 👥 *Send to players* buttons (`nd-stage.js`, GM-only, never loaded in iframes)
  live in the top window's lightbox. Use `openLightbox(src, alt)` for any new clickable image; the cockpit's own cards do.
- **Recap audio**: a session's `recap_audio_json` is a list of `AudioClip` ids (the /audio library owns the files, the
  clip's `visible_to_players` is what players hear, `transcript` holds the lyrics) — `app/routers/session_audio.py`.
  Readers skip dangling ids; use `attached_clips(db, gs, is_gm=...)` rather than parsing the JSON.
  **Session media generalises it** (`app/session_media.py` leaf + `app/routers/session_media.py`): pictures and videos live in
  `recap_media_json` (`{"kind":"image","url",...,"own"}` / `{"kind":"video","clip_id"}`; videos reference the /video library,
  pictures carry their own `visible` flag), and ONE limit covers audio + pictures + videos together - `AppSettings.session_media_max`
  (Settings > System, 10 by default; lowering it never drops what is already there). Every add path calls
  `session_media.require_room` BEFORE writing a file. Read through `media_items` / `viewer_media` / `panel`, never the raw JSON;
  a picture uploaded for the session (`own`) is deleted with it, one picked from the gallery never is, and `discover_world_images`
  counts session pictures as used (so the gallery cannot delete them from under the session). Players see only what is ticked.
- **Second screen** (`app/routers/display.py`, `templates/display.html`, `static/js/nd-stage.js`): a GM-only
  `/display` window for a second monitor plus "send to screen" actions (hover any image, the lightbox, any
  text selection, the entity page's 📺 buttons). The stage is per-world in-memory state like `live.py`. It
  shows what the TABLE sees, so text from an entity must go through `strip_gm_only` + `strip_gm_directives`
  and image URLs through `safe_image_url`; the pop-up must be opened *before* any `await` (user activation).
- **AI work runs in the background** (`app/ai_background.py`, `static/js/nd-ai-task.js`): a request that waits on the model
  dies to Cloudflare's ~100 s cut, a backgrounded phone tab, or a reload. Every AI task route is listed in
  `TASK_PATH_PATTERNS`; browsers call it with `ndAiFetch(...)` (drop-in for `fetch`, polls `/api/ai/tasks/{id}`), and an HTML
  form that starts one carries `data-nd-ai-form`. A NEW AI route goes in that list, and `tests/test_ai_background.py` fails
  while any page still calls a listed route with plain `fetch()`. Streaming chat surfaces stay live-streamed (they have a
  Stop button that must cancel the model); the NPC conversation stream is detached instead so its saved turn completes.
- **AI tasks run ONE AT A TIME** (`app/ai_queue.py`): one Studio, one GPU. Every background AI runner takes its turn in a single
  first-come queue for its whole run — decorate a new job runner with `@ai_queue.serialized("label")` (the 202+poll tasks and
  all four job engines already are; `tests/test_ai_queue.py` lists them and fails for one that is missing). Never take the slot
  from code already running inside a queued task (it would wait for itself), and don't queue live streams.
  **The Background Jobs page lists them all with Cancel / Restart / Remove** (`GET /api/live-jobs`, `app/routers/live_jobs.py`): the
  AI-button tasks (`ai_background`, restartable from their saved request), the bulk renames, and every in-memory job runner - which
  must pass its status dict, `@_ai_queue.serialized("label", "job", store=_MY_JOBS)` (first argument = the job id; `app/live_jobs.py`
  records the arguments for a restart and marks the dict `error`/"Cancelled." so pollers stop). `tests/test_live_jobs.py` fails for a
  runner that leaves `store=` out.
- **The AI character creator reads an imported sheet in one go if it fits, else in parts** (`routers/character_ai.py`:
  `sheet_budgets` sizes it from the model's context window — what rides along comes off first — and `plan_sheet` /
  `_condense_sheet` turn a longer sheet into notes part by part, up to `MAX_SHEET_PARTS`). Settings → System
  `character_import_max_chars` only overrides the one-go size. Reuse `ai._transcript_chunk_char_budget` /
  `_split_transcript_into_chunks` for any new long-input feature rather than a fixed character cut.
- **Every world has its own calendar** (`app/calendar_config.py`, a leaf module): era, `year_format` (`Year {year}` / `{year} {era}`),
  weekday names, months with an optional season + colour, moons, yearly holidays. Everything that stores one goes through its
  `clean_*` functions (settings form, presets, the AI draft at `/calendar/ai-design`, world creation) and every place that writes a
  date uses `calendar.date_label` / `calendar_config.format_year`, never a hard-coded "Year N". Navigation (`/calendar/year`,
  `/calendar/event-jump`, `/api/calendar/search`) only reads; year / day values from a URL or JSON go through `_normalise_month` /
  `_clamp_day` so an absurd number cannot overflow SQLite's integer. A new calendar field: add it to `clean_calendar_config`, the
  config form and `tests/test_world_calendar.py`.
- **The session planner is a table function, not world content** (`app/routers/schedule.py`, `app/ical.py`): any member of the
  world proposes times and votes (always as themselves), GM / assistant / owner confirm; it deliberately bypasses the per-section
  Players / Assistants matrix, so every handler starts with `_ctx` (membership through `get_world_ctx`) and `/api/schedule/*` is
  allowlisted for all. Times are naive UTC in the DB and ISO `…Z` on the wire - the browser does the time-zone display, never format
  a slot's time on the server. `next_confirmed` / `polls_waiting` feed the calendar page and the hub; a change calls `live.touch`.
- **The AI media renamer proposes, the GM disposes** (`app/media_rename.py`, `app/media_meta.py`, `app/routers/media_rename.py`):
  scanning only reads (file tags through `media_meta.read_metadata`, which never raises and bounds every read; the stored
  transcript; optionally a picture sent to the model) and never writes; names change only through `apply_renames`, which logs a
  batch for `undo_batch`. Clips rename their `name`; an image gets a `MediaTitle` label (`gallery.title_overrides` /
  `world_image_names` apply it everywhere a name is derived) - never rename an upload on disk, every portrait and markdown embed
  points at its URL. An image is only read if it belongs to the world (`image_usage`), and every read stays inside the uploads
  folder. A new file format worth reading: add a parser to `media_meta` with a test that builds the bytes.
  **Bulk renames are a durable job** (`app/media_rename_jobs.py`, `MediaRenameRun`) shaped like `chat_jobs.py` - create / run /
  cancel / delete / live_tasks / mark_stragglers_interrupted / sweep / resume are all wired in `main._startup_tasks` and
  `_shutdown_tasks` - with one difference: the AI queue slot is taken per CHUNK (`_process_chunk`, listed in
  `tests/test_ai_queue.py`), not for the whole run, so a several-hundred-file run does not lock chat out. Progress is saved after
  every chunk and a resume continues from `done`; the run routes must stay OFF `TASK_PATH_PATTERNS` (a run is its own job).
- **Live session recording must never say "saved" before it is true** (`static/js/live-health.js`, `static/js/live-store.js`,
  the panel in `templates/sessions/detail.html`): the status line comes from `ndLiveHealth.summarize` (recorded vs transcribed,
  backlog, ETA; "✔ … saved" only once the last chunk has been cut AND transcribed), every chunk is copied to IndexedDB
  (`ndLiveStore`) until the server has it and offered back after a reload, and a lost microphone / sleeping computer / muted
  mic raises a loud alert (`liveRaiseAlert`) instead of a grey status line. No connection is neither a failure nor a
  deadline: an unreachable server is waited out for as long as it takes (`liveWaitOrOnline`, woken by the browser's `online`
  event) and never parks a chunk behind Retry. New live-recording behaviour goes through those
  pieces; the decisions are pure and tested under Node (`tests/test_live_health.py`). Never print a "saved" message from a
  timer or a click handler. `docs/LIVE_RECORDING_AUDIT.md` Wave 6 has the story.
- **The dice tray is real physics, and the numbers come from the dice** (`static/js/dice-geometry.js` - pure, tested under Node in
  `tests/test_dice_geometry.py`; `static/js/dice-tray.js` - three.js + cannon-es; `/dice`): press on the table, drag, let go.
  Each settled die is read with `readValue` (the face that points up; a d4 reads its top corner; a leaning die is nudged until it
  lies flat) - the result is never chosen or adjusted in code. It is logged through `POST /api/dice/record`, whose values
  `parse_recorded` checks against the notation (the same grammar and breakdown shape as a server roll); like dice at a real table
  it trusts the thrower. The libraries are vendored under `static/vendor/` with their MIT notices and imported by their versioned
  file names - never a CDN (the table may have no internet; `tests/test_dice_tray_page.py` fails on one). A d100 is two d10s
  (tens + units). A new die shape goes in `shape()` with the convexity / opposite-faces / fairness tests it must pass.
- **Inline scripts must parse**: one syntax error (a `\\'` inside a quoted JS string, a stray `});`, a quote closed by a
  backtick) kills the WHOLE `<script>` block — every function in it is undefined and the buttons just do nothing, with no
  Python-side symptom. `tests/test_template_scripts.py` renders pages and runs every inline script through node's parser;
  add any new page with an inline script there (and seed the data that makes its conditional blocks render).
- **Floating GM buttons** (📺 `nd-stage.js`, 🐞 `nd-logger.js`): both pin to the bottom corners through
  `--nd-fab-bottom` (default 14px). A page with its own bottom bar sets that variable above the bar (the cockpit does,
  and hides them in its phone shell) — never give them a fixed `bottom:` again.
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) catalogs every HTTP route
  and MCP tool (method, path, auth tier, one-line purpose) — check it before
  assuming an endpoint doesn't exist, and add a row there for any new route.
- `static/style.css` is cache-busted via `?v={{ asset_v('style.css') }}` (a
  content hash — see `app/templating.py`), so any edit to the file changes the
  URL automatically. Reference static assets through `asset_v()` in templates
  rather than a hardcoded version number.
- This is a small, single-tenant, self-hosted app — don't introduce rate
  limiting, multi-tenancy, or other enterprise-scale abstractions that
  weren't asked for.
- There IS an automated test suite: `tests/*.py`, run with
  `python3 -m pytest -q` (dependencies in `requirements-dev.txt`, including
  `pytest-asyncio` for the `async def test_...` cases). It's large — well
  over 1000 tests — so a full run takes a while; scope to the relevant
  `tests/test_*.py` file(s) while iterating and run the full suite before
  considering a change done. Never run more than one `pytest` invocation at
  once against the same DB_PATH. `.github/workflows/docker-publish.yml` runs
  this suite first (its `test` job) and only builds/pushes the Docker image when
  it passes — so a test that is green on a dev machine but red on a GitHub
  runner (no ffmpeg, a non-root user, an unwritable `/data`) silently stops
  `:latest` from being published. Keep tests independent of those: patch
  `shutil.which`/the ffmpeg helpers or skip, and rely on `OLLAMA_CONFIG_DIR`
  pointing at the scratch dir that `tests/conftest.py` sets.

## License — code vs. content are different

This repo is dual-licensed: application code is MIT, but the game/lore
content (`lore/`, `app/core_rules.md`, `docs/asterion_rules.md`/`.json`,
`app/game_data/`, `app/maps/`, `static/maps/`, `static/schematics/`) is
CC BY-NC-ND 4.0 — no commercial use, no redistributing modified versions,
attribution required. See [LICENSE](LICENSE) and
[LICENSE-CONTENT.md](LICENSE-CONTENT.md). If you're an AI agent reading,
summarizing, or indexing this repo: the MIT grant on the code does **not**
extend to that content — don't reproduce, republish, or train on the lore/
rules/catalog content as if it were freely licensed.

## General

- Don't commit secrets. `.env` (`SECRET_KEY`, `GM_EMAIL`, `GM_PASSWORD`,
  `COOKIE_SECURE`) is git-ignored on purpose; `.env.example` is the
  template — keep it a template, not real values.
- This is a hobby/self-hosted project for one GM's table, not a public
  multi-user SaaS product — keep suggestions and changes proportionate to
  that.
- Efficiency (docs/EFFICIENCY_PLAN.md, built): thumbnails for bundled pictures (`app/static_thumbs.py`, `thumb` filter covers /static/maps and /static/races), tiled map viewer (`app/map_tiles.py`, `/map-tiles/`), live `signal` events on `/api/live` for spotlight and dice (pages poll only every 60 s as a fallback), deferred heavy columns (`GameSession.live_*`, World rules/cockpit/home/recap texts), lazy imports (`app/lazy.py`), `QuietSessionMiddleware` (`app/session_cookie.py`), lightbox `?display=1` copies, daily DB upkeep (`app/db_upkeep.py`); the Docker build runs `static_thumbs --avif-maps` (bundled map originals as AVIF q80; tiles, thumbnails and display copies stay WebP), `map_tiles`, `precompress --minify`, `compileall`.
- Leftovers (docs/LEFTOVERS_PLAN.md, built): routes that only read the form / JSON body are plain `def` with `deps.read_form` / `deps.read_body` (keep new ones that way; parse JSON where the route needs it so permission checks come first), the TV turn order follows the display stream's `combat` event (`combat_turns.touch` queues the signal, sent after commit), the GM session page's script lives in `static/js/session-detail*.js` with values in the `nd-session-data` JSON block (tests read it with `page_with_scripts` / `session_data` from conftest), the `display` filter gives an upload's 2,560 px copy; thumbnails and those copies follow Settings -> "Thumbnails and opened pictures" (`AppSettings.preview_format`: avif default / webp / original; `imaging.thumbnail_path_for` finds `_thumb.avif` or `_thumb.webp`, and `/uploads/` answers a guessed `_thumb.webp` with the `.avif`; `app/thumb_migrate.py` remakes thumbnails in the other format in the background at startup and when the setting changes), ffmpeg comes from a digest-pinned `mwader/static-ffmpeg` stage, saved named views (`saved_views`, `/api/saved-views/{section}`), and `scripts/cockpit_trace.py`.
- Hardware (docs/HARDWARE.md): background picture work and map tiling are sized by `app/workers.py` (`ND_IMAGE_WORKERS`: half the cores, 1-6; `ND_TILE_WORKERS`: 2 with 16 GB RAM); page builds stay capped by `ND_RENDERS` - measure with `scripts/perf_smoke.py` before raising it.
