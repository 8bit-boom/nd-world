"""The single shared Jinja2Templates instance, used by main.py and every router.

Previously each router built its own Jinja2Templates() to avoid importing from
main.py (which would be circular, since main.py imports the routers). Starlette
constructs a fresh jinja2.Environment per Jinja2Templates instance, so that left
10 separate environments — only main.py's and characters.py's ever registered
the `kinds`/`subtypes`/`kind_icons` globals that base.html's nav loops over, so
the other eight routers silently rendered zero lore-kind nav links. Registering
everything exactly once here fixes that app-wide and is the reason this module
(like app/rendering.py and app/deps.py) deliberately doesn't import main.
"""
import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .constants import KIND_ICONS, KINDS, SUBTYPES
from .rendering import body_summary, entry_text, parse_stats, render_md, strip_md

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals.update(kinds=KINDS, subtypes=SUBTYPES, kind_icons=KIND_ICONS)
templates.env.filters["md"] = render_md
templates.env.filters["strip_md"] = strip_md
templates.env.filters["body_summary"] = body_summary
templates.env.filters["parse_stats"] = parse_stats
templates.env.filters["entry_text"] = entry_text
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
