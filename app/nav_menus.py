"""GM-manageable top-nav dropdown menus: which of the app's nav tabs are
grouped into which top-nav dropdown, versus rendered as a flat top-level
tab. Covers both the GM-only utility/feature pages (Boards, Quests, AI
Chat, ...) and the always-present tabs every user sees (the per-world
entity kinds, Maps, Race/Profession Catalog, Chronicler, Session Log,
Rules, Player Characters, Android App) — each catalog item carries its own
`gm_only` flag so a menu can mix both and each viewer only ever sees the
items their role already had access to. See app/routers/nav_menus_admin.py
for the save endpoint (edited from the Navigation tab on /settings) and
app/templating.py's context processor for how this becomes base.html's
nav_menus/nav_ungrouped_items every request.
"""
import json
import re

from . import deps

MAX_NAV_MENUS = 12
_MAX_MENU_LABEL_LEN = 40
_MAX_MENU_ICON_LEN = 8

# id -> {label, icon, href, exact, gm_only, condition, ql_type, ql_ref}.
# "exact" mirrors the home-grown active-class rule each of these links used
# before this was data-driven: single-page routes (no /id sub-paths) matched
# the current path exactly, list-style pages matched by prefix. "gm_only"
# controls both whether a player ever sees the item (grouped or flat) and
# whether a menu containing only gm_only items disappears entirely for a
# player. "condition" is an optional key into the settings dict
# resolve_nav_menus is given — an item with a condition that's False is
# dropped entirely (not even shown ungrouped), matching the old
# {% if dreamlands_enabled %} template guards. "ql_type"/"ql_ref" override
# the Quick-Links drag payload for items that aren't a plain url (currently
# only the dynamic per-kind items built by build_catalog, which drag onto
# the home page as target_type="kind" rather than target_type="url").
STATIC_CATALOG = [
    {"id": "maps", "label": "Maps", "icon": "🗺", "href": "/maps", "exact": True, "gm_only": False},
    {"id": "races", "label": "Race Catalog", "icon": "🧬", "href": "/races", "gm_only": False},
    {"id": "professions", "label": "Profession Catalog", "icon": "🎭", "href": "/professions", "gm_only": False},

    {"id": "boards", "label": "Boards", "icon": "📌", "href": "/boards", "gm_only": True},
    {"id": "tables", "label": "Random Tables", "icon": "🎲", "href": "/tables", "gm_only": True},
    {"id": "combat", "label": "Combat Tracker", "icon": "⚔", "href": "/combat", "gm_only": True},
    {"id": "parties", "label": "Parties", "icon": "🛡", "href": "/parties", "gm_only": True},
    {"id": "quests", "label": "Quests", "icon": "📜", "href": "/quests", "gm_only": True},
    {"id": "sessions", "label": "Sessions", "icon": "📓", "href": "/sessions", "gm_only": True},
    {"id": "facts", "label": "Facts", "icon": "🗒", "href": "/facts", "gm_only": True},
    {"id": "calendar", "label": "Calendar", "icon": "🗓", "href": "/calendar", "gm_only": True},
    {"id": "images", "label": "Images", "icon": "🖼", "href": "/images", "gm_only": True},
    {"id": "import", "label": "Import", "icon": "📥", "href": "/import", "gm_only": True},
    {"id": "background_jobs", "label": "Background Jobs", "icon": "⏳", "href": "/background-jobs", "exact": True, "gm_only": True},
    {"id": "export", "label": "Export & Backup", "icon": "📦", "href": "/export", "exact": True, "gm_only": True},
    {"id": "dreamlands", "label": "Dreamlands", "icon": "🌙", "href": "/dreamlands",
     "exact": True, "condition": "dreamlands_enabled", "gm_only": True},
    {"id": "king-in-yellow", "label": "King in Yellow", "icon": "🎭", "href": "/king-in-yellow",
     "exact": True, "condition": "king_in_yellow_enabled", "gm_only": True},
    {"id": "ai", "label": "AI Chat", "icon": "🤖", "href": "/ai", "exact": True, "gm_only": True},
    {"id": "imagestudio", "label": "Image Studio", "icon": "🎨", "href": "/imagestudio", "exact": True, "gm_only": True},
    {"id": "editor", "label": "Content Editor", "icon": "🛠", "href": "/editor", "exact": True, "gm_only": True},

    {"id": "chronicler", "label": "Chronicler", "icon": "📜", "href": "/chronicler", "exact": True, "gm_only": False},
    {"id": "dice", "label": "Dice", "icon": "🎲", "href": "/dice", "exact": True, "gm_only": False},
    {"id": "session_log", "label": "Session Log", "icon": "📓", "href": "/session-log", "gm_only": False},
    {"id": "rules", "label": "Rules", "icon": "📖", "href": "/rules", "exact": True, "gm_only": False},
    {"id": "characters", "label": "Player Characters", "icon": "🎲", "href": "/characters", "gm_only": False},
    {"id": "audio", "label": "Audio", "icon": "🎵", "href": "/audio", "exact": True, "gm_only": False},
    {"id": "video", "label": "Video", "icon": "🎬", "href": "/video", "exact": True, "gm_only": False},
    {"id": "pages", "label": "Pages", "icon": "📄", "href": "/pages", "exact": True, "gm_only": False},
    {"id": "androidapp", "label": "Android App", "icon": "📱", "href": "/androidapp", "exact": True, "gm_only": False},
]
# Every static item gets the same ql_type/ql_ref defaults so templates and
# sanitize_nav_menus never have to special-case a missing key.
for _item in STATIC_CATALOG:
    _item.setdefault("exact", False)
    _item.setdefault("condition", None)
    _item.setdefault("ql_type", None)
    _item.setdefault("ql_ref", None)

# Shipped so every world keeps today's exact grouping (Tools / AI Tools)
# until a GM explicitly customizes it from Settings -> Navigation — see
# load_nav_menus: a world whose nav_menus_json is still NULL (never saved)
# falls back to this, while an explicitly-saved empty list ([], meaning "no
# menus, everything flat") is respected as real. Every catalog item not
# claimed here (every kind tab, Maps/Races/Professions, Chronicler/Session
# Log/Rules/Player Characters/Android App) renders as a flat ungrouped tab
# by default, same as it always has.
DEFAULT_NAV_MENUS = [
    {"id": "menu_tools", "label": "Tools", "icon": "🎯",
     "item_ids": ["boards", "tables", "combat", "parties", "quests", "sessions",
                  "facts", "calendar", "images", "import", "export", "background_jobs",
                  "dreamlands", "king-in-yellow"]},
    {"id": "menu_ai_tools", "label": "AI Tools", "icon": "🤖",
     "item_ids": ["ai", "imagestudio", "editor"]},
]


def build_catalog(world) -> list:
    """The full set of manageable nav items for `world`: STATIC_CATALOG
    plus one dynamic entry per entity kind (built-in or GM-defined custom —
    see deps.effective_kinds), so a custom kind is manageable here the
    moment it's created with zero extra wiring. Kind items are prepended so
    the default (never-customized) flat ordering still puts the kind tabs
    first, matching the nav bar's original layout."""
    kinds, icons = deps.effective_kinds(world)
    custom_labels = {c["id"]: c["label"] for c in deps.load_custom_kinds(world)}
    kind_items = [
        {
            "id": f"kind_{k}", "label": custom_labels.get(k, k.capitalize()), "icon": icons.get(k, "🏷"),
            "href": f"/kind/{k}", "exact": True, "condition": None, "gm_only": False,
            "ql_type": "kind", "ql_ref": k,
        }
        for k in kinds
    ]
    return kind_items + STATIC_CATALOG


def _slugify_menu_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return (slug or "menu")[:24]


def load_nav_menus(world) -> list:
    """world.nav_menus_json -> a list of {id,label,icon,item_ids}. Never
    raises. NULL/unset -> DEFAULT_NAV_MENUS (a deep-ish copy, so callers
    can't mutate the module-level constant); anything else gets parsed
    as-is (already sanitized at save time by sanitize_nav_menus)."""
    raw = getattr(world, "nav_menus_json", None) if world is not None else None
    if not raw:
        return [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]
    return data if isinstance(data, list) else [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]


def sanitize_nav_menus(raw_json, world=None) -> list:
    """Re-validates a posted nav-menu list: caps count, drops empty labels,
    de-duplicates/generates ids, and keeps only real catalog item ids for
    this world — each assigned to at most one menu (first claim wins if the
    payload somehow lists the same item twice, which the Settings UI's
    per-item single-select can't itself produce)."""
    catalog_by_id = {item["id"]: item for item in build_catalog(world)}
    try:
        data = json.loads(raw_json or "[]")
    except (TypeError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    out = []
    seen_ids = set()
    seen_items = set()
    for m in data[:MAX_NAV_MENUS]:
        if not isinstance(m, dict):
            continue
        label = str(m.get("label", "")).strip()[:_MAX_MENU_LABEL_LEN]
        if not label:
            continue
        icon = str(m.get("icon", "")).strip()[:_MAX_MENU_ICON_LEN] or "🗂"
        mid = m.get("id")
        if not isinstance(mid, str) or not mid or mid in seen_ids:
            base_id = f"menu_{_slugify_menu_label(label)}"
            mid = base_id
            n = 2
            while mid in seen_ids:
                mid = f"{base_id}_{n}"
                n += 1
        seen_ids.add(mid)
        item_ids = []
        raw_items = m.get("item_ids")
        if isinstance(raw_items, list):
            for iid in raw_items:
                if isinstance(iid, str) and iid in catalog_by_id and iid not in seen_items:
                    item_ids.append(iid)
                    seen_items.add(iid)
        out.append({"id": mid, "label": label, "icon": icon, "item_ids": item_ids})
    return out


def resolve_nav_menus(world, dreamlands_enabled: bool, king_in_yellow_enabled: bool, is_gm: bool):
    """-> (menus, ungrouped_items) for base.html:
    menus = [{id,label,icon,links:[<catalog item dict>,...]}, ...] (empty
    menus dropped, after per-viewer filtering); ungrouped_items = every
    catalog item claimed by no menu, in catalog order. Every item is
    filtered by its own "condition" flag and, for a non-GM viewer, its
    "gm_only" flag — a menu that mixes GM-only and player-visible items
    still renders for a player with only the player-visible ones inside.

    The resolved key is "links", not "items" — Jinja's `menu.items` would
    silently resolve to the dict's own builtin .items() *method* instead of
    a "items" dict key (attribute lookup is tried before __getitem__), which
    is exactly what happened here during live verification before this
    rename: base.html's `{% for item in menu.items %}` blew up with
    "'builtin_function_or_method' object is not iterable"."""
    catalog = build_catalog(world)
    catalog_by_id = {item["id"]: item for item in catalog}

    def _visible(item):
        cond = item.get("condition")
        if cond == "dreamlands_enabled" and not dreamlands_enabled:
            return False
        if cond == "king_in_yellow_enabled" and not king_in_yellow_enabled:
            return False
        if item.get("gm_only") and not is_gm:
            return False
        return True

    claimed = set()
    menus = []
    for m in load_nav_menus(world):
        if not isinstance(m, dict):
            continue
        links = []
        for iid in m.get("item_ids") or []:
            item = catalog_by_id.get(iid)
            claimed.add(iid)  # claimed regardless of visibility, so it never leaks into "ungrouped" for anyone
            if not item or not _visible(item):
                continue
            links.append(item)
        if links:
            menus.append({"id": m.get("id"), "label": m.get("label"), "icon": m.get("icon"), "links": links})

    ungrouped = [item for item in catalog if item["id"] not in claimed and _visible(item)]
    return menus, ungrouped
