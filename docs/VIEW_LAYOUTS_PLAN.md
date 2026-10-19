# View layouts - plan and status

Every list page (pictures, audio, video, notes, entities, ...) can be shown in more than one layout, with a size, a sort, a grouping and
filters. Decisions (from the GM): images and audio first; choices are saved to the ACCOUNT (and this browser); players get the switcher on
read-only pages too; the soundboard is a priority for table use.

## Architecture (built)
* `app/view_prefs.py` - the ONE registry: which sections have which modes / sorts / groups / filters, and `clean()` for any stored or URL-given choice.
* `templates/_views.html` - `view_bar(section, target, request)` puts the switcher above a `.nd-collection` of `.nd-item`s
  (`data-name`, `data-uses`, `data-date`, `data-size`, `data-source`).
* `static/js/nd-views.js` - builds the controls, applies layout / size / sort / group / filters, remembers the choice (precedence: URL, account,
  this browser, default), and saves it to the account (`POST /api/view-prefs/{section}`, `GET /api/view-prefs`, table `user_view_prefs`,
  every signed-in user, only their own rows). The decisions are pure and tested under Node (`tests/test_nd_views.py`). A page that adds items later calls
  `container.ndViews.refresh()`.
* `static/style.css` "View layouts" - the layouts are CSS keyed on `data-view` and `data-size`, so switching never reloads.

## Status
| Phase | Scope | State |
|---|---|---|
| 0 | Engine, registry, macro, CSS, account saving, tests | done |
| 1 | Images: grid (S/M/L), masonry, list, filmstrip; sort by name / used-in / date / size; group by used / first place used; "unused only" filter (All Images and albums) | done |
| 2 | Audio: rows, compact, playlist (shared bar, shuffle / repeat), soundboard (pads: tap, keys 1-9, volume, loop, fade, one-at-a-time, stop / fade all); Video: rows, compact, poster grid; group by visibility, "hidden only" filter | done (audio table and video theater still to come) |
| 3 | Entities: cards (today's), compact list, portrait wall, split browse (list + live preview, j / k); group by subtype / first tag; sort by name / date. Board (a column per subtype / first tag, drag a card to change its subtype, a move menu on touch, Undo); the web of relations still to come | mostly done |
| 4 | Notes: private notes page - timeline, sticky cards, outline, reader (list + full note, j / k), group by month, sort by date / title (done); session / board notes, facts, maps still to come | partly done |
| 4c | Entity page: CONNECTIONS as cards or a web (this entity in the middle, links solid, backlinks dashed; group by direction / kind; a phone keeps the cards); NOTES in the private-notes layouts (list, cards, outline, reader; sort by date / title; group by month / visibility) | done |
| 4b | Quests: board (a column per status, drag or move menu, Undo; shared `static/js/nd-board.js`), list, cards, group by status / category; Sessions: cards, list, sort by number / name, group by party; Characters: cards, roster, wall, sort by name / level, group by party / owner / system | done |
| 5 | Saved named views, world defaults the GM sets, cockpit-window layouts | planned |

## Layout catalogue
* **Images** - grid, masonry, list (thumbnail, name, used-in), filmstrip (big preview, arrows, full size). Later: compare 2-up, justified rows (needs stored dimensions).
* **Audio** - rows, playlist (one line per clip, one shared player, queue, shuffle), soundboard (big pads, one tap play / stop, hotkeys 1-9, per-pad volume + loop, fades, long-press "play for players"), table.
* **Video** - poster grid, list, theater (big player + playlist sidebar), table.
* **Notes** - timeline, sticky cards, reader (list + full note), outline; group by tag / entity / session / month.
* **Entities** - cards, table, compact list, portrait wall, split browse, board (columns by a field, drag to change it), web (relations), outline, A-Z index.
* **Other** - quests kanban, sessions timeline / calendar, facts grouped by session, character roster / stat table, maps and boards grid / list.

## Rules for adding a section
1. Add it to `view_prefs.SECTIONS` (modes, sorts, groups, filters). 2. Mark the items `.nd-item` with their `data-*`, put `view_bar(...)` above the container.
3. Keep the page's own class names (selection scripts and tests use them). 4. A hidden item is never left selected (the engine unchecks it).
5. Add the page to `tests/test_nd_views.py`.
