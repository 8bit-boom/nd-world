"""Markdown/text rendering helpers used by main.py and shared across every
router's Jinja filters (via app/templating.py).

Split out of main.py rather than imported from it: routers are imported BY
main.py, so a router importing from main.py would be circular. That circular
import was the root cause of every router building its own copy-pasted
Jinja2Templates instance instead of sharing one (see app/templating.py).
"""
import re
from collections import OrderedDict

import html2text
import markdown2
import nh3


_COLOR_NAMES = {
    "red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink",
    "white", "black", "gray", "grey", "magenta", "lime", "teal", "gold",
    "silver", "brown", "crimson", "violet", "indigo", "salmon", "coral",
}
_HEX_COLOR_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}){1,2}$')

# [color=...] / [mark] / [u] give authors color and underline/highlight beyond
# what markdown2's extras cover, without allowing raw HTML: they're matched
# and swapped for real tags *after* markdown2 has already run in safe_mode
# (so any literal "<"/">" a user typed is inert text by this point), and the
# color value itself is allowlisted so it can't break out of the style="..."
# attribute or inject arbitrary CSS.
_COLOR_TAG_RE = re.compile(r'\[color=([^\]]{1,20})\](.*?)\[/color\]', re.DOTALL)
_MARK_TAG_RE = re.compile(r'\[mark(?:=([^\]]{1,20}))?\](.*?)\[/mark\]', re.DOTALL)
_U_TAG_RE = re.compile(r'\[u\](.*?)\[/u\]', re.DOTALL)


def _safe_color(raw: str) -> str | None:
    raw = raw.strip()
    if _HEX_COLOR_RE.match(raw):
        return raw
    if raw.lower() in _COLOR_NAMES:
        return raw.lower()
    return None


def _apply_inline_styles(html: str) -> str:
    def color_sub(m):
        color = _safe_color(m.group(1))
        return f'<span style="color:{color}">{m.group(2)}</span>' if color else m.group(2)

    def mark_sub(m):
        color = _safe_color(m.group(1)) if m.group(1) else None
        style = f' style="background-color:{color}"' if color else ""
        return f'<mark{style}>{m.group(2)}</mark>'

    html = _COLOR_TAG_RE.sub(color_sub, html)
    html = _MARK_TAG_RE.sub(mark_sub, html)
    html = _U_TAG_RE.sub(r'<u>\1</u>', html)
    return html


def render_md(text):
    if not text:
        return ""
    # safe_mode="escape": raw HTML typed into markdown-authored fields (character
    # notes/backstory, entity/rules bodies) is escaped to inert text instead of
    # rendered — otherwise a <script> tag in user-authored content would execute
    # in the browser of anyone who views it (stored XSS). Normal markdown syntax
    # (links, emphasis, tables, ...) is unaffected.
    html = markdown2.markdown(text, extras=["fenced-code-blocks", "tables", "strike"], safe_mode="escape")
    return _apply_inline_styles(html)


def html_to_markdown(raw_html: str) -> str:
    """Convert an uploaded .html/.htm file's contents into markdown suitable
    for EntityNote.content — used by /entity/{id}/notes/import (main.py).

    html2text drops <script>/<style> elements and any other markup entirely
    rather than converting their contents to text, so those never reach the
    stored note. Images are dropped too (ignore_images) — an imported note
    embedding a remote <img src="https://..."> would otherwise turn into a
    live markdown image reference that auto-loads whenever anyone views the
    note, a tracking-pixel risk for content pulled from an untrusted source.
    A dangerous URL scheme on a link (javascript:, data:, ...) survives
    html2text as literal markdown link syntax, but render_md's own
    safe_mode="escape" markdown2 pass already neutralizes those to href="#"
    at render time — the same protection every other markdown-authored
    field in this app already relies on, so no extra sanitizing is needed
    here specifically for that case. Verified against a real payload
    (script/style/onclick/javascript:/data: URLs) during development."""
    if not raw_html or not raw_html.strip():
        return ""
    converter = html2text.HTML2Text()
    converter.body_width = 0  # don't hard-wrap paragraphs at 78 cols
    converter.ignore_images = True
    return converter.handle(raw_html).strip()


# Allowlist for sanitize_note_html below — deliberately no <img> (an
# imported <img src="https://..."> would become a live remote reference
# that auto-loads and phones home to that URL whenever anyone views the
# note, a tracking-pixel risk for content pulled from an untrusted source —
# same reasoning html_to_markdown's ignore_images applies) and no
# script/style/iframe/object/embed/form/svg — the tags that actually carry
# executable or network-fetching behavior.
_NOTE_HTML_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "strike",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "table", "thead", "tbody", "tr", "td", "th",
    "span", "div", "hr", "sub", "sup", "a",
}
_NOTE_HTML_ATTRS = {
    "a": {"href"},
    "span": {"style"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_NOTE_HTML_STYLE_PROPS = {"color", "background-color", "font-weight", "font-style", "text-decoration"}
_NOTE_HTML_STRIP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "form"}


def sanitize_note_html(raw_html: str) -> str:
    """Allowlist-sanitize an uploaded .html/.htm file's contents for the
    "preserve original formatting" note-import mode (main.py's
    /entity/{id}/notes/import) — the result is stored as EntityNote.content
    with content_is_html=True and rendered with the `safe` filter directly,
    bypassing render_md's markdown2 pass entirely (that pass's safe_mode
    only escapes raw HTML, which is exactly the formatting this mode exists
    to keep). nh3 (Rust-backed, actively maintained — bleach, the older
    pure-Python alternative, has no further releases as of mid-2026) does
    the real safety work: strips every tag/attribute not on the allowlist,
    strips javascript:/data:/etc. hrefs (only http/https/mailto survive),
    and drops <script>/<style>/<iframe>/... together with their contents
    rather than leaving the text behind. Verified against a real payload
    (script/style/onclick/onerror/onload/javascript: href/iframe) during
    development — every dangerous piece was removed, formatting (headings,
    bold, lists, tables, allowlisted inline color) survived intact."""
    if not raw_html or not raw_html.strip():
        return ""
    return nh3.clean(
        raw_html,
        tags=_NOTE_HTML_TAGS,
        attributes=_NOTE_HTML_ATTRS,
        clean_content_tags=_NOTE_HTML_STRIP_CONTENT_TAGS,
        url_schemes={"http", "https", "mailto"},
        filter_style_properties=_NOTE_HTML_STYLE_PROPS,
        link_rel="noopener noreferrer nofollow",
    ).strip()


def strip_md(text):
    if not text:
        return ""
    text = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'^\*\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


_STAT_SKIP = {"visibility", "type", "physical stats", "other", "costs", ""}
_STAT_WANT = {
    "damage", "rarity", "armor", "cost", "special conditions", "effect",
    "requirement", "requirements", "range", "rounds", "strength", "power",
    "speed", "feats", "capacity", "augment slots", "max health", "max pp",
}
_STAT_TABLE_SKIP = {"visibility", "physical stats", "other", "costs"}


def _clean_val(v: str) -> str:
    v = re.sub(r'\\\*\\\*', '', v)
    v = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', v)
    return v.strip()


_PLAIN_KEY_RE = re.compile(r'^([A-Z][A-Za-z ()]{1,38}):\s*(.*)')


def parse_stats(body: str) -> list[dict]:
    """Return list of {key, val, special} dicts from ## Attributes or ## Entry."""
    if not body:
        return []
    lines = body.splitlines()
    in_section = False
    rows = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if re.match(r'^##\s+(Attributes|Entry|Profile)', ln, re.IGNORECASE):
            in_section = True
            continue
        if in_section and re.match(r'^##', ln):
            break
        if not in_section:
            continue
        if re.match(r'^(---|!\[|\s*$)', ln):
            continue

        key = val = None
        # Format 1 – bullet: * **Key[optional bracket]**: Value
        m = re.match(r'\*\s+\*\*([^*\[]+)(?:\[[^\]]*\])?\*\*[:\s]\s*(.*)', ln)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 2 – Kanka escaped: \*\*Key\*\* = Value
            m = re.match(r'\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.*)', ln)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 3 – inline bold: **Key:** Value  (race feats)
            m = re.match(r'\*\*([^*:]+)\*\*[:\s]\s*(.*)', ln)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
        if not key:
            # Format 4 – plain title-case: Key: Value  (flesh grafts, "Points: 12")
            m = _PLAIN_KEY_RE.match(ln.strip())
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
                # Multi-line: key-only line, value is on the next non-empty line
                if not val:
                    j = i
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and not re.match(r'^##|^---|^\*\s+\*\*|^\\\*\\\*', lines[j]):
                        # next line is plain text — use as value, advance pointer
                        val = lines[j].strip()
                        i = j + 1

        if not key or not val:
            continue
        val = _clean_val(val)
        if not val or val.startswith('{') or key.lower() in _STAT_TABLE_SKIP:
            continue
        rows.append({"key": key, "val": val, "special": key.lower() == "special conditions"})
    return rows


# /kind/{kind} folder views call parse_stats() once per visible entity on
# every request. Keyed on (entity.id, entity.updated_at) rather than the
# body text itself — that way a cache hit doesn't need to hash the (often
# multi-KB) body string, just a cheap int+datetime tuple, and the key
# naturally goes stale the moment an edit bumps updated_at, so there's
# nothing to invalidate on writes.
_PARSE_STATS_CACHE_SIZE = 512
_parse_stats_cache: "OrderedDict[tuple, list[dict]]" = OrderedDict()


def parse_stats_cached(entity_id: int, updated_at, body: str) -> list[dict]:
    key = (entity_id, updated_at)
    cached = _parse_stats_cache.get(key)
    if cached is not None:
        _parse_stats_cache.move_to_end(key)
        return cached
    rows = parse_stats(body)
    _parse_stats_cache[key] = rows
    if len(_parse_stats_cache) > _PARSE_STATS_CACHE_SIZE:
        _parse_stats_cache.popitem(last=False)
    return rows


def clear_parse_stats_cache():
    _parse_stats_cache.clear()


def _decode(text: str) -> str:
    return (text.replace('&#39;', "'").replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))


def entry_text(body: str) -> str:
    """Extract ## Entry section as clean plain text (for feat descriptions)."""
    if not body:
        return ""
    lines = body.splitlines()
    in_entry = False
    chunks = []
    for ln in lines:
        if re.match(r'^##\s+Entry', ln, re.IGNORECASE):
            in_entry = True
            continue
        if in_entry and re.match(r'^##', ln):
            break
        if not in_entry or re.match(r'^(---|!\[|\s*$)', ln):
            continue
        if re.match(r'^\|', ln):          # markdown table row — skip
            continue
        ln = re.sub(r'\\\*\\\*', '', ln)  # \*\*
        ln = re.sub(r'\\-', '-', ln)
        ln = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', ln)
        ln = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', ln)
        ln = re.sub(r'^[-*]\s+', '', ln)
        chunks.append(ln.strip())
    text = ' '.join(c for c in chunks if c)
    return _decode(re.sub(r'\s+', ' ', text).strip())


def body_summary(text):
    """Compact stat line for cards: key stats or entry text fallback."""
    if not text:
        return ""
    pairs = []
    special = None
    for ln in text.splitlines():
        if re.match(r'^\|', ln):          # skip markdown tables
            continue
        m = re.match(r'\*\s+\*\*([^*]+)\*\*[:\s]\s*(.+)', ln)
        if not m:
            m = re.match(r'\\\*\\\*([^\\]+)\\\*\\\*\s*[=:]\s*(.+)', ln)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = _decode(re.sub(r'\\\*\\\*', '', m.group(2)).strip().rstrip('\\').strip())
        if not val or val.startswith('{') or key in _STAT_SKIP:
            continue
        if key == "special conditions":
            special = val[:220]
        elif key in _STAT_WANT and len(pairs) < 4:
            pairs.append(f"{m.group(1).strip()}: {val}")
    return special or "  ·  ".join(pairs) or entry_text(text)
