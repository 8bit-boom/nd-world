"""Layouts a list page can be shown in ("view layouts"): the ONE registry of which sections have which modes, sort keys and group keys.

A leaf module (no router imports). The page macro `view_bar(section)` (templates/_views.html) renders the switcher from `spec(section)`, and
static/js/nd-views.js - the engine that applies a layout, a size, a sort and a grouping to a collection of items - receives the same spec as JSON,
so the buttons, the validation of a stored / URL-given choice and the server can never disagree. See docs/VIEW_LAYOUTS_PLAN.md.

A choice is {mode, size (1..3 = S / M / L), sort ("key" or "-key" for descending), group ("" or a key), filters: [keys]}. It is stored per browser
(localStorage "nd_view:<section>") and may be given in the URL (?view= &size= &sort= &group=); `clean` is the tolerant reader both use.
"""

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
    "play": ("🎲", "Play"),
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
    # the character page: its tabs (today's layout, tidied) or Play (the phone at the table: no edit chrome, big targets, swipe between sheet pages).
    # `filters` holds the PINNED tabs - the ones in the phone's bottom bar (empty = the default four); at most `max_filters`.
    "modes": ["tabs", "play"], "default": "tabs", "size_modes": ["none"], "sorts": ["name"], "default_sort": "name", "groups": [],
    "filters": list(HUB_TABS), "max_filters": 4}

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
        "filters": [{"id": k, "label": FILTER_LABELS[k]} for k in s["filters"]],
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
    group = raw.get("group") if raw.get("group") in s["groups"] else ""
    filters = [f for f in (raw.get("filters") if isinstance(raw.get("filters"), list) else []) if f in s["filters"]]
    filters = list(dict.fromkeys(filters))[: s.get("max_filters", len(filters))]      # no repeats; a section may cap how many
    return {"mode": mode, "size": size, "sort": sort, "group": group, "filters": filters}
