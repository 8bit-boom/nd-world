"""GM-manageable top-nav dropdown menus: which of the app's GM-only
utility/feature pages (Boards, Quests, AI Chat, etc.) are grouped into which
top-nav dropdown, versus rendered as a flat top-level tab. See
app/routers/nav_menus_admin.py for the save endpoint (edited from the
Navigation tab on /settings) and app/templating.py's context processor for
how this becomes base.html's nav_menus/nav_ungrouped_items every request.

Deliberately scoped to only the pages that were already GM-only and already
lived inside the old hardcoded Tools/AI Tools dropdowns — Maps, Race/
Profession Catalog, Chronicler, Session Log, Rules, Player Characters, and
Android App are separate always-flat nav tabs (some of them player-visible)
and are not part of this manageable catalog.
"""
import json
import re

MAX_NAV_MENUS = 12
_MAX_MENU_LABEL_LEN = 40
_MAX_MENU_ICON_LEN = 8

# id -> {label, icon, href, exact, condition}. "exact" mirrors the
# home-grown active-class rule each of these links used before this was
# data-driven: single-page routes (no /id sub-paths) matched the current
# path exactly, list-style pages matched by prefix. "condition" is an
# optional key into the settings dict resolve_nav_menus is given — an item
# with a condition that's False is dropped entirely (not even shown
# ungrouped), matching the old {% if dreamlands_enabled %} template guards.
NAV_CATALOG = [
    {"id": "boards", "label": "Boards", "icon": "📌", "href": "/boards"},
    {"id": "tables", "label": "Random Tables", "icon": "🎲", "href": "/tables"},
    {"id": "combat", "label": "Combat Tracker", "icon": "⚔", "href": "/combat"},
    {"id": "parties", "label": "Parties", "icon": "🛡", "href": "/parties"},
    {"id": "quests", "label": "Quests", "icon": "📜", "href": "/quests"},
    {"id": "sessions", "label": "Sessions", "icon": "📓", "href": "/sessions"},
    {"id": "facts", "label": "Facts", "icon": "🗒", "href": "/facts"},
    {"id": "calendar", "label": "Calendar", "icon": "🗓", "href": "/calendar"},
    {"id": "images", "label": "Images", "icon": "🖼", "href": "/images"},
    {"id": "import", "label": "Import", "icon": "📥", "href": "/import"},
    {"id": "export", "label": "Export & Backup", "icon": "📦", "href": "/export", "exact": True},
    {"id": "dreamlands", "label": "Dreamlands", "icon": "🌙", "href": "/dreamlands",
     "exact": True, "condition": "dreamlands_enabled"},
    {"id": "king-in-yellow", "label": "King in Yellow", "icon": "🎭", "href": "/king-in-yellow",
     "exact": True, "condition": "king_in_yellow_enabled"},
    {"id": "ai", "label": "AI Chat", "icon": "🤖", "href": "/ai", "exact": True},
    {"id": "imagestudio", "label": "Image Studio", "icon": "🎨", "href": "/imagestudio", "exact": True},
    {"id": "editor", "label": "Content Editor", "icon": "🛠", "href": "/editor", "exact": True},
]
NAV_CATALOG_BY_ID = {item["id"]: item for item in NAV_CATALOG}

# Shipped so every world keeps today's exact grouping (Tools / AI Tools)
# until a GM explicitly customizes it from Settings -> Navigation — see
# load_nav_menus: a world whose nav_menus_json is still NULL (never saved)
# falls back to this, while an explicitly-saved empty list ([], meaning "no
# menus, everything flat") is respected as real.
DEFAULT_NAV_MENUS = [
    {"id": "menu_tools", "label": "Tools", "icon": "🎯",
     "item_ids": ["boards", "tables", "combat", "parties", "quests", "sessions",
                  "facts", "calendar", "images", "import", "export",
                  "dreamlands", "king-in-yellow"]},
    {"id": "menu_ai_tools", "label": "AI Tools", "icon": "🤖",
     "item_ids": ["ai", "imagestudio", "editor"]},
]


def _slugify_menu_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return (slug or "menu")[:24]


def load_nav_menus(world) -> list:
    """world.nav_menus_json -> a list of {id,label,icon,item_ids}. Never
    raises. NULL/unset -> DEFAULT_NAV_MENUS (a deep-ish copy, so callers
    can't mutate the module-level constant); anything else gets parsed
    as-is (already sanitized at save time by _sanitize_nav_menus)."""
    raw = getattr(world, "nav_menus_json", None) if world is not None else None
    if not raw:
        return [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]
    return data if isinstance(data, list) else [dict(m, item_ids=list(m["item_ids"])) for m in DEFAULT_NAV_MENUS]


def sanitize_nav_menus(raw_json) -> list:
    """Re-validates a posted nav-menu list: caps count, drops empty labels,
    de-duplicates/generates ids, and keeps only real catalog item ids —
    each assigned to at most one menu (first claim wins if the payload
    somehow lists the same item twice, which the Settings UI's per-item
    single-select can't itself produce)."""
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
                if isinstance(iid, str) and iid in NAV_CATALOG_BY_ID and iid not in seen_items:
                    item_ids.append(iid)
                    seen_items.add(iid)
        out.append({"id": mid, "label": label, "icon": icon, "item_ids": item_ids})
    return out


def resolve_nav_menus(world, dreamlands_enabled: bool, king_in_yellow_enabled: bool):
    """-> (menus, ungrouped_items) for base.html:
    menus = [{id,label,icon,links:[<catalog item dict>,...]}, ...] (empty
    menus dropped); ungrouped_items = every catalog item claimed by no menu,
    in catalog order. Both filtered by each item's own "condition" flag.

    The resolved key is "links", not "items" — Jinja's `menu.items` would
    silently resolve to the dict's own builtin .items() *method* instead of
    a "items" dict key (attribute lookup is tried before __getitem__), which
    is exactly what happened here during live verification before this
    rename: base.html's `{% for item in menu.items %}` blew up with
    "'builtin_function_or_method' object is not iterable"."""
    def _cond_ok(item):
        cond = item.get("condition")
        if cond == "dreamlands_enabled":
            return dreamlands_enabled
        if cond == "king_in_yellow_enabled":
            return king_in_yellow_enabled
        return True

    claimed = set()
    menus = []
    for m in load_nav_menus(world):
        if not isinstance(m, dict):
            continue
        links = []
        for iid in m.get("item_ids") or []:
            item = NAV_CATALOG_BY_ID.get(iid)
            if not item or not _cond_ok(item):
                continue
            links.append(item)
            claimed.add(iid)
        if links:
            menus.append({"id": m.get("id"), "label": m.get("label"), "icon": m.get("icon"), "links": links})

    ungrouped = [item for item in NAV_CATALOG if item["id"] not in claimed and _cond_ok(item)]
    return menus, ungrouped
