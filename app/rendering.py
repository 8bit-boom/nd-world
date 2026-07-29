"""Markdown/text rendering helpers used by main.py and shared across every
router's Jinja filters (via app/templating.py).

Split out of main.py rather than imported from it: routers are imported BY
main.py, so a router importing from main.py would be circular. That circular
import was the root cause of every router building its own copy-pasted
Jinja2Templates instance instead of sharing one (see app/templating.py).
"""
import re

import markdown2


def render_md(text):
    # safe_mode="escape": raw HTML typed into markdown-authored fields (character
    # notes/backstory, entity/rules bodies) is escaped to inert text instead of
    # rendered — otherwise a <script> tag in user-authored content would execute
    # in the browser of anyone who views it (stored XSS). Normal markdown syntax
    # (links, emphasis, tables, ...) is unaffected.
    return markdown2.markdown(text, extras=["fenced-code-blocks", "tables", "strike"], safe_mode="escape") if text else ""


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
