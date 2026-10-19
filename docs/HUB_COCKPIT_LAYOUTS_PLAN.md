# Layouts for the character page (the "hub") and the GM Cockpit - audit and plan

Status: a plan, nothing built yet. Written after reading the code (`_player_hub.html`, `sheet.html`, `cockpit.html`, `cockpit.js`,
`cockpit-layout.js`) and looking at both pages in Chromium at 1600x900, 1280x700 and 390x800 (a fresh world with three characters, so the
cockpit was empty). Not tested on a real iPhone.

## 1. What exists today

**Character page (`/characters/<id>`)**
- A sticky *status strip* (vitals, turn, next game night), a row of action chips (Alerts / Share / Roll / Rest), then **13 tabs**: Sheet, Cockpit,
  Journey, Quests, Notes, World, Schedule, Sessions, Kept, Handouts, Allies, Log, Pages.
- On a phone the tabs become a bottom bar: Sheet, Journey, Quests, Notes + More (everything else is under More).
- The Sheet tab has its own page strip (Overview / Feats & gear / Story / Dice / All) and, above it, a toolbar (Edit, five exports, Retire, Delete).
- The Cockpit tab is the Player Cockpit in an iframe.

**GM Cockpit (`/cockpit`, `/player-cockpit`)**
- Free floating windows (drag, resize, snap, colour, zoom, lock, pop out), a dock of chips, a *laptop layout* (1-3 windows at once, rest in the dock),
  a *phone/tablet shell* (one panel at a time, bottom tab bar), four *scenes* (Combat, Exploration, Roleplay, Prep), up to 12 saved layouts,
  Ctrl+K / Alt+keys, "roomy windows". The layout follows the GM across devices.

## 2. Findings (what is weak)

Character page
1. **The top of the page is a stack of chrome.** Desktop: strip -> chips -> 13 tabs -> "Analyze with AI" -> Edit/Export/Retire/Delete toolbar -> hero
   -> page tabs, and only then the first stat. Phone: the first stat is about 700 px down.
2. **The tab strip scrolls sideways with no hint** (on 1600 px "Allies" is already cut off), the order is fixed, and a phone shows only 4 of 13 tabs.
3. **One column, capped at 1100 px.** On a wide screen half the page is empty, and seeing the sheet *next to* quests / party / notes means opening the
   Cockpit tab (a separate world inside an iframe).
4. **Play and edit are mixed.** Edit / Export / Retire / Delete sit above the numbers a player looks at all night.
5. The owner, a GM looking in, and a phone all get the same arrangement; nothing is remembered per person (the list pages now remember theirs).

GM Cockpit
1. **First open is empty.** One-panel tiles are offered, but the four scenes (the thing most GMs want first) are hidden in the 🧩 menu.
2. **The header is icon-only at laptop width** (☰ 💻 🗂 🧩 🔍 ＋ ⛶ and two ✕-looking buttons at the end); you can't tell which scene you are in.
3. **Only one arrangement style: free floating windows.** Fine on a big monitor; on a laptop it needs the dock; there is no "stage + side rail"
   or "tabs" style, which is what most tables actually use (map in the middle, party and dice on the side).
4. **Phone/tablet:** one panel at a time, tab titles cut at 12 characters, no quick actions (next turn, HP ±) without opening the combat panel.
5. Scenes are recipes you apply by hand; nothing offers the Combat scene when a fight starts.
6. Three overlapping dashboards: hub tabs, the Player Cockpit (iframe) and the GM Cockpit.

## 3. Proposed layouts

All of them reuse what exists: the view-layout engine (account-saved choice, URL > account > browser > default), the cockpit's pure tiler
(`cockpit-layout.js`, tested under Node), and the scenes.

### A. Character page - a "Layout" switcher (new `hub` section in `app/view_prefs.py`)

| Layout | For | What it is |
|---|---|---|
| **Tabs** (default, today's, tidied) | everyone | Same tabs. The strip gets scroll arrows + edge fade; the action chips fold into the strip; Edit/Export/Retire/Delete move into one ⋯ menu; "Analyze with AI" moves into it too. |
| **Play** | phone at the table | Sticky vitals; big Roll / Rest / HP± buttons; the sheet as swipeable cards (Abilities, Attacks, Gear, Story); the bottom bar is *your* four pinned tabs; the rest under More. No edit chrome. |
| **Dashboard** | laptop / desktop >= 1100 px | Sheet on the left; a right rail of collapsible cards (Quests, Party, Notes, Dice, Log) that you pick and order - no iframe, same data as the tabs. |
| **Split** | wide screens >= 1400 px | Sheet and any one other tab side by side (like the notes "Reader"); j / k or a dropdown picks the right-hand tab. |
| **Focus** | reading / printing | One sheet page at a time, nothing else (today's page strip, full width). |

Features across them: pin which tabs are in the phone bottom bar; hide tabs you never use (Schedule, Pages, ...); a GM looking in gets the same
layout read-only; the choice is saved to the account (`/api/view-prefs/hub`) like the list layouts.

### B. GM Cockpit

| Layout | What it is |
|---|---|
| **Free windows** (today) | Unchanged. |
| **Stage + rail** (new, suggested default for laptops) | One big stage (map / combat / entity) and a narrow rail of stacked windows (party, dice, notes, quests); drag a window between stage and rail; resizable divider. Built on the existing stage/tile planner. |
| **Tabs** (new) | One big area, windows as browser-style tabs; the dock chips become the tab row. Best for a 13" laptop and for tablets. |
| **Tiled grid** (new) | No overlaps: windows snap into a grid and drag to swap places (a tmux-style split), so nothing can hide behind another window. |
| **Table (tablet / phone)** | Landscape tablet: stage + rail. Phone: panel pager plus a quick-actions bar (next turn, party HP ±1, roll d20) and the combat panel jumps first when a fight is running. |

Features: **scene bar** in the header (⚔ 🧭 🎭 📖 as chips, current one lit, keys 1-4) and the scenes offered on first open; **remember the arrangement
per scene** (so Combat reopens the way you left it); an opt-in "suggest the Combat scene when a combat starts"; header labels collapse into one
"More" menu at laptop width and the doubled ✕ is fixed; the Player Cockpit gets the same layouts read-only.

### C. Cross-cutting
- One shared **layout switcher** control for the hub and the cockpit (same look as the list pages' bar).
- Pure planners (stage + rail, tabs, grid) under Node tests, like `tests/test_cockpit_layout.py`; Chromium checks at 1600 / 1280 / 390 px as for
  the list layouts; keyboard and `aria-` roles for every new control; nothing player-visible is added that the tabs would not already show.

## 4. Suggested order

| Phase | Scope | Size |
|---|---|---|
| 1 | Hub: declutter the header (⋯ menu, chips into the strip), tab strip arrows/fade, pin tabs for the phone bar, **Play** layout | 1 session |
| 2 | Hub: **Dashboard** and **Split** layouts, account-saved `hub` section | 1 session |
| 3 | Cockpit: scene bar + scenes on first open, header cleanup, per-scene arrangement | 1 session |
| 4 | Cockpit: **Stage + rail** and **Tabs** layouts | 1-2 sessions |
| 5 | Cockpit: **Tiled grid**, tablet / phone Table mode, combat auto-suggest | 1-2 sessions |

Recommended first slice: phase 1 + phase 3 together - the most visible fixes (tidy top of the page, Play layout, scene bar) with the least new machinery.

## 5. Questions that change the plan
1. Where do you play: GM on a laptop, players on phones? (decides whether Dashboard or Play comes first)
2. Is a floating-window cockpit what you use, or would a fixed stage + rail suit you better?
3. Should a player be able to choose their own layout, or should the GM set one for the whole table?
