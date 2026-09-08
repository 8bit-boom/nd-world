# Mobile UI/UX Audit

Audit date: 2026-09-08 · scope: every nd-world surface, phone-first (390×844) with tablet notes

**Verdict:** nd-world's mobile foundation is genuinely good — a real ≤768px
global block (hamburger nav, stacked layouts, 16px inputs), an excellent AI
Chat page, a model touch citizen in the schematic editor's event handling, and
an iOS-tolerant recording pipeline by design. The gaps are concentrated and
specific: **investigation boards are effectively unusable on touch**, the
**schematic editor's chrome crowds out its canvas on phones**, **iOS recording
has a filename/format mismatch**, and a handful of admin pages (settings tabs,
world edit) have clipped or broken controls. Roughly two-thirds of the app's
pages already pass a 390px smoke test with zero horizontal overflow.

---

## 1. Method

1. **Static sweep** — every template, `static/style.css`, `static/css/ai-chat.css`,
   and all shared JS, for viewport/meta, media queries, fixed widths, hover-only
   affordances, pointer-event handling, recording formats, and file-input
   behavior (file:line evidence throughout §3–§6).
2. **Live crawl** — the app run in Docker (fresh DB, seeded content), driven at
   **390×844, mobile/touch emulation**: 33 routes crawled measuring horizontal
   overflow (`scrollWidth` vs viewport), per-page JS errors, and screenshots.
   Routes covered: dashboard, entities (list/detail/edit/new), AI chat + Image
   tab, facts, sessions (list/detail/new), rules (view/edit), quests (list/
   form), tables (list/form), calendar, boards (+ editor), combat, settings,
   world edit, home edit, maps, schematic editor + player view, background
   jobs, import, gallery, audio library, characters, chronicler.

   *Limits:* Chrome device emulation, not real iOS hardware — Safari-specific
   behaviors (recording formats, keyboard/beforeunload quirks) are verified by
   code-reading, not on-device. The world under test was near-empty; pages were
   audited for layout/interaction, not content density.

**Live-crawl headline result: zero routes had document-level horizontal
overflow at 390px.** The problems below are therefore about *interaction*,
*clipping inside containers* (the app sets `overflow-x: clip` on mobile, so
clipped content cannot even be scrolled to), and *fit quality* — not page-wide
breakage.

## 2. What already works well (keep these patterns)

- **Global ≤768px block** (`static/style.css:1306–1466`): topbar grid +
  hamburger with click-toggled dropdowns (touch-safe, `aria-expanded`),
  stacked list/detail layouts, dashboard/card restacks, 16px inputs (blocks
  iOS focus-zoom), touch-target bumps.
- **AI Chat is the best mobile page in the app**: sidebar hides behind mobile
  model/RAG bars (`static/css/ai-chat.css:305–356, 605–639`), sticky input
  bar, tab chips wrap — the whole page was verified visually at 390px.
- **Schematic's event layer is exemplary**: Pointer Events end-to-end with
  `pointercancel` (iOS-commented), two-finger pan, `touch-action:none`,
  coarse-pointer handle sizing, collapsible "⋯ More" toolbar
  (`app/templates/schematic.html:427–453, 580, 776–795, 1147–1194`).
- **Recording pipeline design is iOS-tolerant**: default (not forced)
  MediaRecorder mimeType, benign getUserMedia constraints, no AudioWorklet,
  one shared mic stream per recording (explicitly for mobile Safari), retry
  queue for failed chunks (`base.html:345–408`, `sessions/detail.html:1142+`).
- All AI streaming uses fetch + reader (no EventSource) — correct for iOS.
- Uploads nearly always pair a dropzone with a real file input
  (`static/js/file-dropzone.js:37–41`); `Blob.slice` chunked upload is
  iOS-safe.
- Sidebar-TOC trio (entity detail, rules, private notes), characters pages,
  chronicler, imagestudio, gallery, background jobs, import, login, search,
  dice — all pass the 390px crawl cleanly (several with their own MQs).

## 3. P1 — blockers for a phone/tablet GM

### 3.1 Investigation boards are touch-unusable
`board.html` has **zero pointer/touch handlers** — node drag, canvas pan, and
group drag are all mouse-event chains (`board.html:652, 678, 691–704,
945–1003`), the canvas is `overflow:hidden` with no scroll fallback (:227),
and **editing an existing node is dblclick-or-right-click only** (:653–654) —
neither fires on iOS. The page's own hint line (live-verified) reads
*"click=select · Shift+click=multi · drag=move · dblclick=edit ·
Space+drag=pan"* — every instruction is mouse/keyboard-only. Node dialogs
(`min-width:340px`), a 180px minimap overlay, and 160px fixed-width nodes
compound it. **Fix direction:** port the schematic's Pointer Events pattern
(drag + two-finger pan), add a tap-to-open editor (select → ✎ button), make
the minimap toggleable.

### 3.2 Schematic editor: chrome eats the canvas on phones
Live-verified at 390px: six wrapped toolbar rows consume the top ~45% of the
screen, the 178px `#sch-panel` (:456) and 160px minimap (:487) never collapse,
leaving the canvas a small strip; the status hint says "Space=pan ·
Shift+click=multi-select" — keyboard instructions on a touch device. Three
affordance gaps: **polygon finish is dblclick/Escape-only** (:1237, :2229 — no
Finish button), the element context menu (flip/z-order/duplicate variants) is
**right-click-only** (:688; the features exist in bar2, the gesture doesn't
exist on iOS), and inline `padding:2px 6px` (:34) defeats the coarse-pointer
bump. **Fix direction:** phone MQ that collapses the panel into a drawer,
a "✓ Finish" button while drawing, a long-press/select-and-✎ menu, touch-
worded hints.

### 3.3 iOS recording: misnamed files + screen-lock stall
`ndMicFilename` maps mimeType → `.ogg`/`.wav`/`.webm` only (`base.html:404–408`)
— Safari's `audio/mp4` (AAC) recordings are saved as **`.webm` with MP4
bytes**. The server allowlists would accept `.m4a` (`routers/ai.py:43`,
`sessions.py:36`) but that extension is dead code because the client never
emits it; suffix-keyed logic exists server-side (`ai.py:316`), so the mismatch
is one ffmpeg sniff away from breaking. Separately, live-recording chunk
rotation is `setTimeout`-based (`sessions/detail.html:1282`) — if an iPhone
screen locks (Wake Lock is iOS 16.4+ only, :1293–1304), timers suspend and the
current segment stalls until foreground with no flush. **Fix direction:** add
the `audio/mp4 → .m4a` filename branch; on `visibilitychange/pagehide`, flush
the in-flight segment immediately; consider a foreground notification to the
GM ("screen locked — recording paused").

### 3.4 Dashboard pinning is drag-from-nav only
HTML5 drag-and-drop never fires on touch: pinning tiles/quick-links by
dragging nav items (`base.html:78, 194–203`; `index.html:330–350, 404–422`)
is desktop-only. The only fallback is a different page (`/home/edit`), not in
place. **Fix direction:** "＋ Pin" tap affordance (long-press nav item → menu,
or a pin button in the nav dropdown), reusing the home-edit save endpoint.

## 4. P2 — degraded but usable

| # | Finding | Evidence | Fix direction |
|---|---|---|---|
| 4.1 | **Settings tab bar clips the 4th tab**: bar content 407px vs 366px available, `overflow: visible`, document `overflow-x: clip` — "Navigation" is cut and only its left sliver is tappable | live-verified (`.settings-tab-bar`, settings.html:551 no-wrap) | `overflow-x:auto` + scroll snap on the bar, or wrap |
| 4.2 | **World edit breaks on mobile**: checkbox stacks orphaned from their labels (live-verified), bottom action row clipped at the right edge, and two borderless tables (:97, :156) have no overflow wrapper | screenshot + static | stack label+checkbox pairs, wrap tables in `overflow-x:auto`, wrap the action row |
| 4.3 | **Session detail's title/#/date row squeezes** — the date input clips ("YYYY-" visible) at 390px (the page's `2fr 1fr 1fr` grid, detail.html:22) | screenshot | collapse to 1 column ≤640px; use `type="date"` while there (no date picker exists anywhere in the app) |
| 4.4 | **Entity hover preview has no touch trigger** — mouseover/focus only, 5s default delay (`base.html:678–791`) | static | long-press or tap-and-hold trigger, or a "ⓘ" chip on links when `pointer:coarse` |
| 4.5 | **Entities list hides stat columns ≤768** instead of scrolling — feat descriptions vanish (`style.css:1406–1408`) | static | `overflow-x:auto` on the table wrapper instead of hiding |
| 4.6 | **Combat tracker touch targets ~18px** — HP/shock/condition buttons `.stat-btn padding:.15rem .4rem`, init inputs 48px (`combat/detail.html:69–81`) | static | `pointer:coarse` bump like schematic's |
| 4.7 | **Calendar cells ~48px** with event chips at `.68rem`; grid event details are `title`-tooltip only (the tap-to-open day panel is the real save) | live screenshot + month.html:75–124 | keep, but raise cell height slightly; add a visible "n events" dot count |
| 4.8 | **Sticky-bottom chat input vs iOS keyboard** — no `visualViewport` handling anywhere; the keyboard can cover the compose bar | static (ai-chat.css:313/613) | `visualViewport` resize → adjust bar bottom |
| 4.9 | **Unsaved-changes guards use `beforeunload`** (boards, schematic, live transcript) — iOS Safari skips it unreliably; the codebase already knows to use `pagehide` elsewhere (`base.html:595`) | static | switch guards to `pagehide`/`visibilitychange` |
| 4.10 | **Map region move-drag fights Leaflet touch panning** (Leaflet-synthesized mouse events, `map_viewer.html:363–383`) | static | select-then-drag-handle pattern, or drag only when a "move" mode toggle is on |
| 4.11 | **Handouts print flow: `window.open('', '_blank')` + `document.write` after `await fetch`** (`handouts_gallery.html:58–61`) — classic iOS popup-blocker casualty | static | build the DOM in-page, then `window.open` synchronously and write |

## 5. P3 — polish

- **No PWA basics**: no `manifest.json`, `theme-color`, or `apple-touch-icon`
  (adding them makes the add-to-home-screen experience real for a self-hosted
  tool people open on phones daily).
- Format-toolbar targets ≈22px, swatches 18px (`style.css:1562–1578`) — bump
  under `pointer:coarse` like schematic does.
- Inline `font-size` < 16px on many inputs (schematic props, sessions inputs,
  facts, library selects) defeats the global iOS anti-zoom fix
  (`style.css:1440–1445`) — sweep inline styles on inputs.
- No `inputmode` anywhere except 2FA fields; `type="date"` used nowhere
  (session date is free-text `YYYY-MM-DD`).
- `quests/detail.html` 3-col grids (:27, :107) and `home_edit.html` 6-col
  quick-link rows (:116) have no collapse MQ (top of home_edit renders fine;
  the link rows are the risk).
- Schematic merchant dialog `min-width:420px` (:344) overflows 390px; board
  node dialog 340px just fits; `schematic_view.html` (player-facing) has no
  mobile handling beyond its pan handler.
- Dashboard tile remove buttons are hover-only and ~20px
  (`style.css:309–316`) — invisible on touch (mitigated: /home/edit manages
  tiles with real buttons).
- Handout page keeps 2cm screen padding (print-only MQ, `handout.html:29`).
- Double-tap zoom is live and competes with the dblclick-dependent UIs (board
  edit, polygon finish); `touch-action: manipulation` on those canvases would
  remove the 300ms ambiguity once the touch alternatives (3.1/3.2) exist.
- Command palette is Ctrl/Cmd-K only (`static/js/command-palette.js:120`) —
  no mobile entry point.
- `map_viewer` edit-mode toolbar adds 7 small `.tb-btn`s that only wrap.

## 6. Notes from the live run

- 33 routes crawled at 390×844: **no document-level horizontal overflow
  anywhere**; per-page JS errors: none on any natural load. One transient
  `SyntaxError: Unexpected token '}'` was observed twice during the first
  synthetic crawl only; three targeted hunts (per-page listeners + parsing
  every inline `<script>` on /ai) could not reproduce it and no functional
  gap was found — recorded as a harness artifact, worth a one-line watch
  item, not a finding.
- The floating AI-status beetle bottom-right slightly overlaps content at
  390px on long pages (cosmetic).

## 7. Recommended fix order

1. **Quick wins (CSS-mostly, one branch):** 4.1 settings tab scroll, 4.2
   world-edit stacking + table wrappers, 4.3 session form-row collapse +
   `type="date"`, 4.5 stat-table scroll instead of hide, 4.6 combat touch
   bump, schematic merchant dialog width, PWA meta trio (§5.1).
2. **Touch interactions:** board Pointer Events + tap-to-edit (3.1),
   schematic Finish button + panel drawer + touch hints (3.2), dashboard
   pin fallback (3.4).
3. **iOS correctness:** recording `.m4a` branch + segment flush on
   visibility change (3.3), `pagehide` guard swap (4.9), handout popup (4.11),
   visualViewport for the chat bar (4.8).
4. **Then:** hover-preview touch trigger (4.4), map region drag mode (4.10),
   remaining P3 polish.

Items slot under IMPROVEMENT_PLAN's existing U5–U8 (mobile polish) lines —
this document is the concrete backlog for them.
