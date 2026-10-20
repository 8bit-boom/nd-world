"""Layouts a list page can be shown in ("view layouts"): the ONE registry of which sections have which modes, sort keys and group keys.

A leaf module (no router imports). The page macro `view_bar(section)` (templates/_views.html) renders the switcher from `spec(section)`, and
static/js/nd-views.js - the engine that applies a layout, a size, a sort and a grouping to a collection of items - receives the same spec as JSON,
so the buttons, the validation of a stored / URL-given choice and the server can never disagree. See docs/VIEW_LAYOUTS_PLAN.md.

A choice is {mode, size (1..3 = S / M / L), sort ("key" or "-key" for descending), group ("" or a key), filters: [keys]}. It is stored per browser
(localStorage "nd_view:<section>") and may be given in the URL (?view= &size= &sort= &group=); `clean` is the tolerant reader both use.
"""

import re

MODE_LABELS = {
    "grid": ("⊞", "Grid"),
    "masonry": ("▥", "Masonry"),
    "list": ("☰", "List"),
    "compact": ("≡", "Compact"),
    "filmstrip": ("🎞", "Filmstrip"),
    "rows": ("☰", "Rows"),
    "playlist": ("▶", "Playlist"),
    "pads": ("🎛", "Soundboard"),
    "posters": ("🎬", "Posters"),
    "wall": ("🖼", "Portrait wall"),
    "split": ("◫", "Split"),
    "timeline": ("📜", "Timeline"),
    "cards": ("🗒", "Cards"),
    "outline": ("≣", "Outline"),
    "board": ("▦", "Board"),
    "web": ("🕸", "Web"),
    "tabs": ("🗂", "Tabs"),
    "site": ("⚙", "Site"),
    "classic": ("☰", "Long page"),
    "cols": ("◧", "Two columns"),
    "reader": ("📖", "Reader"),
    "night": ("🌙", "Session night"),
    "focus": ("◎", "Focus"),
    "widgets": ("🧩", "My widgets"),
    "tray": ("🎲", "3D tray"),
    "quick": ("⚡", "Quick buttons"),
    "log": ("📜", "History first"),
    "turn": ("🎯", "Turn view"),
    "big": ("🖥", "Big screen"),
    "roll": ("🎲", "Roll board"),
    "free": ("◫", "Free windows"),
    "rail": ("▥", "Stage + rail"),
    "play": ("🎲", "Play"),
    "dashboard": ("▤", "Dashboard"),
}

SORT_LABELS = {
    "name": "Name",
    "uses": "Used in",
    "date": "Date",
    "size": "File size",
    "num": "Session number",
}

GROUP_LABELS = {
    "usage": "Used / unused",
    "source": "First place used",
    "visibility": "Visible / hidden to players",
    "subtype": "Subtype",
    "tag": "First tag",
    "month": "Month",
    "status": "Status",
    "category": "Category",
    "party": "Party",
    "owner": "Player",
    "system": "Sheet type",
    "dir": "Direction",
    "recap": "Recap",
    "type": "Type",
    "kind": "Kind",
}

# the character page's tabs: a "filter" of the hub section is a PINNED tab (the ones the phone's bottom bar shows)
HUB_TABS = {
    "sheet": "Sheet", "cockpit": "Cockpit", "journey": "Journey", "quests": "Quests", "notes": "Notes", "world": "World", "schedule": "Schedule",
    "sessions": "Sessions", "keepsakes": "Kept", "handouts": "Handouts", "companions": "Allies", "log": "Log", "places": "Pages",
}

FILTER_LABELS = {
    **HUB_TABS,
    "unused": "Unused only",
    "hidden": "Hidden from players only",
}

SECTIONS = {
    # All Images (/images): the gallery of every picture the world uses or keeps in an album
    "images": {"modes": ["grid", "masonry", "list", "filmstrip"], "default": "grid",
               "sorts": ["name", "uses", "date", "size"], "default_sort": "name",
               "groups": ["usage", "source"], "filters": ["unused"]},
    # an album's pictures: no "used in" data on its tiles
    "album": {"modes": ["grid", "masonry", "list", "filmstrip"], "default": "grid",
              "sorts": ["name"], "default_sort": "name", "groups": [], "filters": []},
    # the Audio library (/audio and an audio album): rows (today's layout), compact rows, a playlist with one shared player, a soundboard of pads
    "audio": {"modes": ["rows", "compact", "playlist", "pads"], "default": "rows", "size_modes": ["pads"],
              "sorts": ["name", "date", "size"], "default_sort": "name", "groups": ["visibility"], "filters": ["hidden"]},
    # the Video library (/video and a video album): rows (today's layout), compact rows, a grid of posters
    "video": {"modes": ["rows", "compact", "posters"], "default": "rows", "size_modes": ["posters"],
              "sorts": ["name", "date", "size"], "default_sort": "name", "groups": ["visibility"], "filters": ["hidden"]},
    # an entity list's cards (/kind/<kind>): cards (today's), compact rows, a portrait wall, a split view (the list beside a live preview) and a board (a column per subtype)
    "entities": {"modes": ["grid", "compact", "wall", "split", "board"], "default": "grid", "size_modes": ["grid", "wall"],
                 "sorts": ["name", "date"], "default_sort": "name", "groups": ["subtype", "tag"], "filters": []},
    # a player's private notes page: the timeline (today's), sticky-note cards, a collapsed outline, and a reader (the list beside one full note)
    "notes": {"modes": ["timeline", "cards", "outline", "split"], "default": "timeline", "size_modes": ["cards"],
              "labels": {"split": "Reader"}, "sorts": ["date", "name"], "default_sort": "-date", "groups": ["month"], "filters": []},
    # the quest list: a board (a column per status - drag a quest to change it), rows grouped by status, or cards
    "quests": {"modes": ["board", "compact", "grid"], "default": "board", "size_modes": ["grid"], "labels": {"compact": "List", "grid": "Cards"},
               "sorts": ["name", "date"], "default_sort": "name", "groups": ["status", "category"], "filters": []},
    # the session list: cards (today's) or compact rows, by session number / title, grouped by party
    "sessions": {"modes": ["grid", "compact"], "default": "grid", "size_modes": ["grid"], "labels": {"compact": "List", "grid": "Cards"},
                 "sorts": ["num", "name"], "default_sort": "-num", "groups": ["party"], "filters": []},
    # the character roster: cards (today's), compact rows, a portrait wall - grouped by party, player or sheet type
    "characters": {"modes": ["grid", "compact", "wall"], "default": "grid", "size_modes": ["grid", "wall"], "labels": {"compact": "Roster", "wall": "Portrait wall"},
                   "sorts": ["name", "num"], "default_sort": "name", "sort_labels": {"num": "Level"}, "groups": ["party", "owner", "system"], "filters": []},
}

SECTIONS["connections"] = {
    # an entity page's CONNECTIONS: cards (today's; the ones it links to first, then the ones that link to it) or a web - the entity in the middle, the others around it
    "modes": ["grid", "web"], "default": "grid", "size_modes": ["none"], "labels": {"grid": "Cards"},
    "sorts": ["name"], "default_sort": "name", "groups": ["dir", "kind"], "filters": []}
SECTIONS["entity_notes"] = {
    # an entity page's NOTES: the list (today's), sticky cards, a collapsed outline, a reader - the same layouts as the private notes page
    "modes": ["timeline", "cards", "outline", "split"], "default": "timeline", "size_modes": ["cards"],
    "labels": {"timeline": "List", "split": "Reader"}, "sorts": ["date", "name"], "default_sort": "date", "groups": ["month", "visibility"], "filters": []}

SECTIONS["board_notes"] = {
    # an investigation board's nodes as notes to READ: the list, sticky cards, an outline, a reader - sorted by board order or title, grouped by type / status
    "modes": ["timeline", "cards", "outline", "split"], "default": "timeline", "size_modes": ["cards"],
    "labels": {"timeline": "List", "split": "Reader"}, "sorts": ["date", "name"], "default_sort": "date", "sort_labels": {"date": "Board order"},
    "groups": ["type", "status"], "filters": []}
SECTIONS["session_log"] = {
    # the players' Session Log: today's list, cards, compact rows - newest first by number, grouped by what the recap offers
    "modes": ["timeline", "grid", "compact"], "default": "timeline", "size_modes": ["grid"], "labels": {"timeline": "List", "grid": "Cards"},
    "sorts": ["num", "name"], "default_sort": "-num", "groups": ["recap"], "filters": []}

SECTIONS["hub"] = {
    # the character page: its tabs (today's layout, tidied), Play (the phone at the table: no edit chrome, big targets, swipe between sheet pages),
    # Dashboard (on a wide screen the pinned tabs sit in a rail beside the sheet) or Split (the sheet and ONE pinned tab side by side).
    # `filters` holds the PINNED tabs - the ones in the phone's bottom bar and in the wide screen's rail (empty = the default four); at most `max_filters`.
    "modes": ["tabs", "play", "dashboard", "split"], "default": "tabs", "size_modes": ["none"], "sorts": ["name"], "default_sort": "name", "groups": [],
    "filters": list(HUB_TABS), "max_filters": 4}

SECTIONS["cockpit"] = {
    # the cockpit's layout STYLE: free windows (today's), a stage with a rail of windows beside it, tabs (one window at a time), a tiled grid
    "modes": ["free", "rail", "tabs", "grid"], "default": "free", "size_modes": ["none"], "labels": {"grid": "Tiled grid"},
    "sorts": ["name"], "default_sort": "name", "groups": [], "filters": []}

# The SITE shell: not a list but a handful of independent settings, so this section carries `options` (each: its allowed values, the first
# being the default) and `pins` (the destinations kept in reach - the phone's bottom bar, the top of the side rail).
SITE_OPTIONS = {
    "nav": ["top", "rail", "compact", "bottom"],           # top bar (today) | side rail | icon-only top bar | phone bottom bar
    "width": ["centered", "wide", "reading"],
    "density": ["comfortable", "compact", "touch"],
    "theme": ["", "dark", "dim", "light", "contrast"],      # "" = whatever this browser chose
    "scale": ["", "90", "100", "110", "125", "150", "175", "200"],
    "motion": ["", "full", "reduced"],                      # "" = the browser / system setting
}
SITE_MAX_PINS = 6
SECTIONS["site"] = {"modes": ["site"], "default": "site", "size_modes": ["none"], "sorts": ["name"], "default_sort": "name", "groups": [], "filters": [],
                    "options": SITE_OPTIONS, "max_pins": SITE_MAX_PINS}

# What the person sees under "Your layouts" on the Account page (every section that is saved per account).
SECTION_TITLES = {
    "site": "The site (navigation, width, density)", "images": "All images", "album": "Image album", "audio": "Audio library", "video": "Video library",
    "entities": "Entity lists", "notes": "My private notes", "entity_notes": "Notes on an entity", "connections": "An entity's connections",
    "quests": "Quests", "sessions": "Sessions (GM)", "session_log": "Session Log", "characters": "Characters", "board_notes": "A board's notes",
    "hub": "My character page", "cockpit": "Cockpit layout style", "entity_page": "An entity's page", "home": "The home page", "dice": "Dice", "combat": "Combat tracker", "tables": "Random tables",
}

SECTIONS["entity_page"] = {
    # an entity's own page: the long page (today's), tabs (Overview / Details / Notes / Connections / Media), two columns (text | a sticky side) or a reader
    "modes": ["classic", "tabs", "cols", "reader"], "default": "classic", "size_modes": ["none"], "sorts": ["name"], "default_sort": "name", "groups": [], "filters": []}

SECTIONS["dice"] = {
    # the dice page: the 3D tray (today's), one-tap quick buttons with saved formulas (no animation), or the shared roll log first
    "modes": ["tray", "quick", "log"], "default": "tray", "size_modes": ["none"], "sorts": ["name"], "default_sort": "name", "groups": [], "filters": []}
SECTIONS["combat"] = {
    # the combat tracker: standard rows (today's), compact rows, cards, a turn view (who acts now and next, big buttons), a big-screen view
    "modes": ["rows", "compact", "cards", "turn", "big"], "default": "rows", "size_modes": ["none"], "labels": {"rows": "Standard"},
    "sorts": ["name"], "default_sort": "name", "groups": [], "filters": []}
SECTIONS["tables"] = {
    # random tables: cards (today's), compact rows, a roll board (every table a big roll button, the results stack in a log beside it)
    "modes": ["grid", "compact", "roll"], "default": "grid", "size_modes": ["grid"], "labels": {"grid": "Cards", "compact": "List"},
    "sorts": ["name"], "default_sort": "name", "groups": ["category"], "default_group": "category", "filters": []}

SECTIONS["home"] = {
    # the world's home page: classic (today's), Session night (what the table needs at the start of play), Focus (links and recent only) or My widgets
    # (reorder and hide the blocks - `order` and `hidden` are lists of block ids)
    "modes": ["classic", "night", "focus", "widgets"], "default": "classic", "size_modes": ["none"], "labels": {"classic": "Classic"},
    "sorts": ["name"], "default_sort": "name", "groups": [], "filters": [], "lists": {"order": 16, "hidden": 16}}

SIZES = (1, 2, 3)
DEFAULT_SIZE = 2


def spec(section: str) -> dict:
    """The section's spec as the page and the engine use it: ids plus the labels to show. Unknown section -> {}."""
    s = SECTIONS.get(section)
    if not s:
        return {}
    return {
        "section": section,
        "default": s["default"], "default_sort": s["default_sort"], "default_size": DEFAULT_SIZE,
        # the layouts the size slider means something in (the picture layouts all; a list of rows has no size)
        "size_modes": s.get("size_modes") or [m for m in s["modes"] if m != "list"],
        "modes": [{"id": m, "icon": MODE_LABELS[m][0], "label": s.get("labels", {}).get(m, MODE_LABELS[m][1])} for m in s["modes"]],
        "sorts": [{"id": k, "label": s.get("sort_labels", {}).get(k, SORT_LABELS[k])} for k in s["sorts"]],
        "groups": [{"id": k, "label": GROUP_LABELS[k]} for k in s["groups"]],
        "default_group": s.get("default_group", ""),
        "filters": [{"id": k, "label": FILTER_LABELS[k]} for k in s["filters"]],
        **({"options": s["options"]} if s.get("options") else {}),
    }


def clean(section: str, raw) -> dict:
    """A stored or URL-given choice as a usable one: anything unknown falls back to the section's default, field by field (never all at once)."""
    s = SECTIONS.get(section)
    if not s:
        return {}
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in s["modes"] else s["default"]
    try:
        size = int(raw.get("size"))
    except (TypeError, ValueError):
        size = DEFAULT_SIZE
    size = size if size in SIZES else DEFAULT_SIZE
    sort = str(raw.get("sort") or "")
    desc = sort.startswith("-")
    key = sort[1:] if desc else sort
    sort = ("-" if desc else "") + key if key in s["sorts"] else s["default_sort"]
    g = raw.get("group")
    # a section may open grouped (`default_group`); an explicit "" is the person choosing "no grouping" and is kept
    group = g if g in s["groups"] else ("" if g == "" else s.get("default_group", ""))
    filters = [f for f in (raw.get("filters") if isinstance(raw.get("filters"), list) else []) if f in s["filters"]]
    filters = list(dict.fromkeys(filters))[: s.get("max_filters", len(filters))]      # no repeats; a section may cap how many
    out = {"mode": mode, "size": size, "sort": sort, "group": group, "filters": filters}
    for name, cap in (s.get("lists") or {}).items():        # id lists a section keeps (e.g. the home page's block order and hidden blocks)
        given = raw.get(name) if isinstance(raw.get(name), list) else []
        out[name] = list(dict.fromkeys(x for x in given if isinstance(x, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", x)))[:cap]
    if s.get("options"):
        given = raw.get("options") if isinstance(raw.get("options"), dict) else {}
        out["options"] = {k: (str(given.get(k)) if str(given.get(k)) in allowed else allowed[0]) for k, allowed in s["options"].items()}
        pins = raw.get("pins") if isinstance(raw.get("pins"), list) else []
        out["pins"] = list(dict.fromkeys(p for p in pins if isinstance(p, str) and re.fullmatch(r"[a-z0-9_-]{1,40}", p)))[: s.get("max_pins", 6)]
    return out
