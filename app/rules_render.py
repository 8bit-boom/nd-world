"""Rules-page markdown "superpowers" — ::: directive blocks (callouts,
collapses, GM-only sections), ```statblock fences, and the per-world
rules.json overlay (section icons/titles/visibility + tabs).

Lives in its own module for the same reason app/rendering.py was split out of
main.py: routers are imported BY main.py, so anything main.py imports must not
import main.py back. The overlay helpers live here too because they operate on
the section structure this module's splitter produces (the h2/h3 id slugs that
main._rules_toc generates).

Security model, inherited from render_md's design (never weaken these):
- Block INNER text is rendered through the exact same markdown2
  safe_mode="escape" pipeline as the rest of the page, so raw HTML typed
  inside a ::: block is inert escaped text, same as anywhere else.
- The :::gm block is REMOVED server-side for non-GM viewers — it is never
  rendered as CSS-hidden HTML that a player could read in the page source.
- Overlay-provided strings (icons, titles, tab labels) are HTML-escaped when
  injected into section HTML, which is rendered through the template's
  `|safe` filter and therefore bypasses Jinja auto-escaping.

Pipeline (why the phases exist): directives and statblocks are extracted from
the SOURCE before markdown2 runs (markdown2 would otherwise mangle the :::
open/close lines into paragraphs), replaced by plain-text sentinel paragraphs
markdown2 leaves untouched, and restored into the rendered HTML afterwards.
main.py's rules_page runs the phases separately (extract → render skeleton →
_rules_toc → restore) so heading ids are assigned BEFORE blocks come back — a
"## Heading" typed inside a :::collapse block then carries no id and can't
trick the section splitter into cutting the block's <div>/<details> open.
render_rules_markdown() is the one-shot equivalent (extract → render →
restore) for callers that don't need section splitting.
"""
import html
import json
import logging
import re

from .rendering import render_md

_log = logging.getLogger("nd.rules_render")

# Opening line of a ::: directive: the type word plus an optional free-text
# title after it (":::tip Read this first"). Only [ \t] between type and
# title — \s would eat the newline that splitlines() already removed.
_DIRECTIVE_OPEN_RE = re.compile(r'^:::(\w+)(?:[ \t]+(.*?))?[ \t]*$')
_DIRECTIVE_CLOSE_RE = re.compile(r'^:::[ \t]*$')
# Fenced block with the info string exactly "statblock" (leading indent of up
# to 3 spaces allowed, matching markdown2's own fence tolerance).
_STATBLOCK_OPEN_RE = re.compile(r'^ {0,3}```statblock[ \t]*$')

# Supported directive types → (icon, default title, css modifier).
_CALLOUT_TYPES = {
    "tip": ("💡", "Tip", "tip"),
    "note": ("📝", "Note", "note"),
    "warning": ("⚠️", "Warning", "warning"),
    "danger": ("☠️", "Danger", "danger"),
    "lore": ("📖", "Lore", "lore"),
    "gm": ("🗝", "GM Only", "gm"),
}

# Sentinel paragraphs swap in for extracted blocks. Plain word-chars only so
# markdown2 (safe_mode escape, strike/fenced/tables extras) passes them
# through as an untouchable <p>; the numeric suffix makes them unique per
# block within one render.
_SENTINEL_TMPL = "NDRULESBLOCK{}NDRULES"
_SENTINEL_LINE_RE = re.compile(r'^NDRULESBLOCK\d+NDRULES$')

# Ordinary fenced code: 3+ backticks to open (with an info string) or close.
_FENCE_CLOSE_RE = re.compile(r'^ {0,3}`{3,}[ \t]*$')
_FENCE_OPEN_RE = re.compile(r'^ {0,3}`{3,}[^`]*[ \t]*$')


def extract_blocks(md: str):
    """Scan rules markdown for ::: directive blocks and ```statblock fences.

    -> (skeleton, blocks): the skeleton is the same document with every
    block's SOURCE replaced by a sentinel paragraph (blank-line padded so it
    can't glue onto neighbouring paragraphs), and blocks is a list of
    {kind, type, title, text, sentinel} dicts in document order. An unclosed
    block simply runs to end-of-file (the last ::: is optional — a GM who
    forgets it still gets their block, not a broken page). Ordinary ``` code
    fences are tracked as state so a ::: typed inside one stays literal.
    """
    lines = md.splitlines()
    skeleton_lines = []
    blocks = []
    i = 0
    in_plain_fence = False
    while i < len(lines):
        line = lines[i]
        if in_plain_fence:
            # Inside an ordinary code fence everything is literal until the
            # closing ``` — directives/fences in here are somebody's code
            # sample, not page structure.
            skeleton_lines.append(line)
            if _FENCE_CLOSE_RE.match(line):
                in_plain_fence = False
            i += 1
            continue
        m = _DIRECTIVE_OPEN_RE.match(line)
        stat = _STATBLOCK_OPEN_RE.match(line)
        if m:
            i += 1
            inner = []
            while i < len(lines) and not _DIRECTIVE_CLOSE_RE.match(lines[i]):
                inner.append(lines[i])
                i += 1
            i += 1  # step past the closing ::: (or past EOF, harmlessly)
            blocks.append({
                "kind": "callout",
                "type": m.group(1).lower(),
                "title": (m.group(2) or "").strip(),
                "text": "\n".join(inner),
                "sentinel": _SENTINEL_TMPL.format(len(blocks)),
            })
            skeleton_lines.append(blocks[-1]["sentinel"])
        elif stat:
            i += 1
            inner = []
            while i < len(lines) and not _FENCE_CLOSE_RE.match(lines[i]):
                inner.append(lines[i])
                i += 1
            i += 1
            blocks.append({
                "kind": "statblock",
                "type": "statblock",
                "title": "",
                "text": "\n".join(inner),
                "sentinel": _SENTINEL_TMPL.format(len(blocks)),
            })
            skeleton_lines.append(blocks[-1]["sentinel"])
        else:
            skeleton_lines.append(line)
            if _FENCE_OPEN_RE.match(line):
                in_plain_fence = True
            i += 1
    # Blank lines around each sentinel: the block it replaces may have sat
    # directly against surrounding paragraphs, and an unpadded sentinel could
    # be swallowed into a neighbouring paragraph/lazy continuation and then
    # never appear as its own <p> to restore over.
    skeleton = []
    for line in skeleton_lines:
        if _SENTINEL_LINE_RE.match(line):
            if skeleton and skeleton[-1].strip():
                skeleton.append("")
            skeleton.append(line)
            skeleton.append("")
        else:
            skeleton.append(line)
    return "\n".join(skeleton), blocks


def _render_callout(block: dict, is_gm: bool) -> str:
    """One ::: block → themed callout / collapse / gm-only div (or "" when it
    must not ship)."""
    dtype = block["type"]
    if dtype == "gm" and not is_gm:
        # Hard server-side removal for non-GM viewers — the block's text is
        # never put on the page at all (not CSS-hidden, which any player
        # could read in view-source / the network tab).
        return ""
    if dtype == "collapse":
        # Native <details> element: collapsed by default, summary is the
        # whole click target. Falls through no callout theming.
        title = block["title"] or "Details"
        inner = render_md(block["text"])
        return (f'<details class="nd-collapse"><summary>{html.escape(title)}</summary>'
                f'<div class="nd-collapse-body">{inner}</div></details>')
    spec = _CALLOUT_TYPES.get(dtype)
    if spec:
        icon, default_title, css = spec
        # An explicit title after the type overrides the default LABEL, but
        # the per-type icon stays (":::tip Reroll Policy" → "💡 Reroll Policy").
        title = block["title"] or default_title
    else:
        # Unknown directive types must never be silently dropped — render as
        # a note callout with the type name in the title so the GM sees the
        # typo'd directive ON the page instead of losing the content.
        icon, css = "📝", "note"
        title = f"{dtype}: {block['title']}" if block["title"] else dtype
    inner = render_md(block["text"])
    return (
        f'<div class="nd-callout nd-callout-{css}">'
        f'<div class="nd-callout-title">{html.escape(icon)} {html.escape(title)}</div>'
        f'<div class="nd-callout-body">{inner}</div></div>'
    )


def _render_statblock(block: dict) -> str:
    """```statblock fence → name + label/value rows + copy-raw-text button.

    The first non-empty line is the block's name; following "Label: value"
    lines become rows (split on the FIRST colon — values contain colons, e.g.
    times/URLs); anything else is free text rendered as a paragraph through
    the normal markdown pipeline. The verbatim block text rides along in a
    data-raw attribute (attribute-escaped, newlines as &#10; which the HTML
    parser decodes back) for rules.html's copy-button wiring."""
    raw_lines = block["text"].replace("\r\n", "\n").replace("\r", "\n").splitlines()
    stripped = [ln.strip() for ln in raw_lines]
    while stripped and not stripped[0]:
        stripped.pop(0)
    while stripped and not stripped[-1]:
        stripped.pop()
    name = stripped[0] if stripped else "Statblock"
    body = []
    free_text = []  # consecutive non "Label: value" lines merge into one <p>
    for ln in stripped[1:]:
        if not ln:
            if free_text:
                body.append("<p>" + render_md("\n".join(free_text)) + "</p>")
                free_text = []
            continue
        label, sep, value = ln.partition(":")
        if sep and label.strip():
            if free_text:
                body.append("<p>" + render_md("\n".join(free_text)) + "</p>")
                free_text = []
            # Strip markdown bold off the label so "**AC**: 17" renders a
            # clean "AC" — markdown2 already ran on nothing here, the raw
            # ** would otherwise show literally.
            label = label.strip().strip("*").strip()
            body.append(
                f'<div class="nd-statblock-row">'
                f'<span class="nd-statblock-label">{html.escape(label)}</span>'
                f'<span class="nd-statblock-value">{html.escape(value.strip())}</span></div>'
            )
        else:
            free_text.append(ln)
    if free_text:
        body.append("<p>" + render_md("\n".join(free_text)) + "</p>")
    raw_attr = html.escape(block["text"], quote=True).replace("\n", "&#10;")
    return (
        f'<div class="nd-statblock" data-raw="{raw_attr}">'
        f'<div class="nd-statblock-name">{html.escape(name)}</div>'
        f'<div class="nd-statblock-body">{"".join(body)}</div>'
        f'<button type="button" class="nd-statblock-copy">📋 Copy</button></div>'
    )


def restore_blocks(skeleton_html: str, blocks: list, is_gm: bool) -> str:
    """Swap each sentinel paragraph back for its rendered block HTML.

    Callers that need heading ids (rules_page) run _rules_toc BETWEEN
    extract_blocks and this — see the module docstring for why the order
    matters."""
    for block in blocks:
        if block["kind"] == "statblock":
            block_html = _render_statblock(block)
        else:
            block_html = _render_callout(block, is_gm)
        sentinel = block["sentinel"]
        # markdown2 normally emits <p>SENTINEL</p>; replace that whole
        # paragraph so no empty <p></p> husk is left behind for a removed
        # :::gm block. The bare-sentinel fallback covers the (not seen in
        # practice) case where markdown2 didn't wrap it.
        wrapped = f"<p>{sentinel}</p>"
        if wrapped in skeleton_html:
            skeleton_html = skeleton_html.replace(wrapped, block_html)
        else:
            skeleton_html = skeleton_html.replace(sentinel, block_html)
    return skeleton_html


def render_rules_markdown(md: str, is_gm: bool) -> str:
    """One-shot rules renderer: directives/statblocks extracted, markdown2
    run exactly as render_md would (same safe_mode + extras + inline styles
    via render_md itself), blocks restored. Documents with no directives hit
    the plain render_md fast path, so the default core-rules page renders
    byte-identical to before this module existed."""
    if not md or not md.strip():
        return ""
    skeleton, blocks = extract_blocks(md)
    if not blocks:
        # Feed the ORIGINAL text through, not the splitlines() roundtrip in
        # skeleton — the roundtrip normalizes \r\n and drops a trailing
        # newline, which would make block-free documents render
        # (byte-)differently from plain render_md.
        return render_md(md)
    return restore_blocks(render_md(skeleton), blocks, is_gm)


# Section splitter — matches ONLY the id-tagged headings _rules_toc emits
# (<h2 id="slug">). Headings inside ::: blocks never get ids (they're
# restored after _rules_toc runs), so they can't become split points and
# chop a callout's <div> in half.
_SECTION_HEADING_RE = re.compile(r'<h([23]) id="([^"]*)">(.*?)</h[23]>', re.DOTALL)


def split_rules_sections(content_html: str) -> list:
    """Split rendered rules HTML into section chunks at the h2/h3 id split
    points. -> [{id, level, title, html}] in document order; anything before
    the first heading is a preamble section with id=None (always shown,
    never in the TOC, untouchable by the overlay)."""
    matches = list(_SECTION_HEADING_RE.finditer(content_html))
    if not matches:
        return [{"id": None, "level": 0, "title": "", "html": content_html}]
    sections = []
    preamble = content_html[:matches[0].start()]
    if preamble.strip():
        sections.append({"id": None, "level": 0, "title": "", "html": preamble})
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content_html)
        # Same tag-strip + unescape as _rules_toc's label building: this text
        # feeds both the TOC (re-escaped by Jinja) and heading rewrites.
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        sections.append({
            "id": m.group(2),
            "level": int(m.group(1)),
            "title": title,
            "html": content_html[m.start():end],
        })
    return sections


# ── rules.json overlay ────────────────────────────────────────────────────────
#
# Schema (validated by parse_rules_overlay, applied by apply_rules_overlay):
# {
#   "sections": {
#     "<section-slug>": {"icon": "🧬", "title": "Races & Optional Systems",
#                        "players_visible": true, "order": 2}
#   },
#   "tabs": [{"id": "core", "label": "Core", "sections": ["slug", "slug"]}]
# }
# Section slugs are the ones _rules_toc generates from ##/### headings.
# sections.title/icon rename the section (TOC + heading); order reorders it
# within its group in the flat (no-tabs) flow — in tabs flow the tab's own
# sections list order wins; players_visible:false hard-hides the section from
# non-GM viewers (same server-side removal as :::gm). tabs, when present,
# splits the page into a tab bar; sections listed in no tab fall into a
# trailing "More" tab. Blank/missing overlay = plain single-page flow.

def _overlay_slugify(text: str) -> str:
    # Mirrors _rules_toc's slug shape so tab ids derived from labels stay
    # URL/attribute-safe without importing from main.py.
    return re.sub(r"[^\w]+", "-", text.lower()).strip("-")


def parse_rules_overlay(raw):
    """Validate a world's stored rules_json text.

    -> (overlay, None) when usable, (None, error_message) when not — callers
    render with no overlay on error (log + continue) and the editor POST
    turns the same message into a 400 so the GM sees the parse error
    immediately instead of a silently ignored overlay."""
    if raw is None or not raw.strip():
        return None, None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "top level must be a JSON object"
    sections_in = data.get("sections")
    if sections_in is None:
        sections_in = {}
    if not isinstance(sections_in, dict):
        return None, '"sections" must be an object mapping section slugs to settings'
    sections = {}
    for slug, cfg in sections_in.items():
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            return None, f'sections["{slug}"] must be an object'
        entry = {}
        for key in ("icon", "title"):
            val = cfg.get(key)
            if val is not None:
                if not isinstance(val, str):
                    return None, f'sections["{slug}"].{key} must be a string'
                entry[key] = val
        players_visible = cfg.get("players_visible")
        if players_visible is not None:
            if not isinstance(players_visible, bool):
                return None, f'sections["{slug}"].players_visible must be true or false'
            entry["players_visible"] = players_visible
        order = cfg.get("order")
        if order is not None:
            # bool is an int subclass in Python — exclude it explicitly,
            # "order": true is a GM typo, not a sort key.
            if isinstance(order, bool) or not isinstance(order, (int, float)):
                return None, f'sections["{slug}"].order must be a number'
            entry["order"] = order
        sections[slug] = entry
    tabs_in = data.get("tabs")
    if tabs_in is None:
        tabs_in = []
    if not isinstance(tabs_in, list):
        return None, '"tabs" must be a list of {"id", "label", "sections"} objects'
    tabs = []
    seen_ids = set()
    for idx, tab in enumerate(tabs_in):
        if not isinstance(tab, dict):
            return None, f"tabs[{idx}] must be an object"
        label = tab.get("label")
        if not isinstance(label, str) or not label.strip():
            return None, f"tabs[{idx}].label must be a non-empty string"
        tab_id = tab.get("id")
        if not isinstance(tab_id, str) or not tab_id.strip():
            tab_id = _overlay_slugify(label) or f"tab-{idx + 1}"
        # Duplicate ids would make the tab bar's localStorage restore and
        # pane toggle ambiguous — dedupe with a numeric suffix.
        base, n = tab_id, 2
        while tab_id in seen_ids:
            tab_id = f"{base}-{n}"
            n += 1
        seen_ids.add(tab_id)
        tab_sections = tab.get("sections")
        if tab_sections is None:
            tab_sections = []
        if not isinstance(tab_sections, list) or not all(
            isinstance(s, str) for s in tab_sections
        ):
            return None, f"tabs[{idx}].sections must be a list of section slugs"
        tabs.append({"id": tab_id.strip(), "label": label.strip(), "sections": tab_sections})
    return {"sections": sections, "tabs": tabs}, None


_RETITLE_RE = re.compile(r'(<h[23] id="[^"]*">)(.*?)(</h[23]>)', re.DOTALL)


def _retitle_heading(section_html: str, plain_label: str) -> str:
    # Section HTML goes through |safe, so the overlay-provided label must be
    # escaped HERE (Jinja won't do it) — icon/title are GM-authored but this
    # also keeps a stray "<" from breaking the heading markup.
    return _RETITLE_RE.sub(lambda m: m.group(1) + html.escape(plain_label) + m.group(3),
                           section_html, count=1)


def apply_rules_overlay(sections: list, overlay, is_gm: bool):
    """Apply a parsed overlay to split sections.

    -> (final_sections, tabs_or_none). final_sections is the flat,
    visible-only list (TOC is built from it); tabs is a resolved pane list
    [{id, label, sections}] when the overlay defines tabs, else None. The
    preamble section (id=None) is never filtered/renamed and, in tabs flow,
    is prepended to the first pane so intro text stays above everything."""
    if not overlay:
        return sections, None
    cfg = overlay.get("sections", {})
    kept = []
    for section in sections:
        conf = cfg.get(section["id"]) if section["id"] else None
        if conf:
            if conf.get("players_visible") is False and not is_gm:
                # Same hard server-side hide as :::gm — the section's HTML
                # never reaches a player's page.
                continue
            section = dict(section)
            icon = conf.get("icon") or ""
            title = conf.get("title")
            if icon or title:
                label = f"{icon} {title}" if icon and title else (title or (f"{icon} {section['title']}".strip()))
                section["title"] = label
                section["html"] = _retitle_heading(section["html"], label)
            section["_order"] = conf.get("order")
        kept.append(section)
    # Flat-flow ordering: sections with an explicit order come first sorted
    # by it; unordered ones keep their document order AFTER them (stable
    # partition, exactly the documented "missing order = natural order after
    # ordered ones"). The preamble (id=None) is pinned at the top — it has no
    # slug and must not be dragged around by ordered sections. In tabs flow
    # the tab's sections list order wins — see the schema comment above.
    body = [s for s in kept if s["id"]]
    ordered = sorted((s for s in body if s.get("_order") is not None),
                     key=lambda s: s["_order"])
    natural = [s for s in body if s.get("_order") is None]
    preambles = [s for s in kept if not s["id"]]
    flat = preambles + ordered + natural
    tabs_def = overlay.get("tabs") or []
    if not tabs_def:
        return flat, None
    by_id = {s["id"]: s for s in kept if s["id"]}
    listed = set()
    panes = []
    for tab in tabs_def:
        # First tab to list a slug owns it — a slug repeated across tabs
        # renders once, not twice.
        ids = [sid for sid in tab["sections"] if sid in by_id and sid not in listed]
        listed.update(ids)
        panes.append({"id": tab["id"], "label": tab["label"],
                      "sections": [by_id[sid] for sid in ids]})
    leftovers = [s for s in kept if s["id"] and s["id"] not in listed]
    if leftovers:
        panes.append({"id": "more", "label": "More", "sections": leftovers})
    # A tab whose sections are all player-hidden (or whose slugs don't exist)
    # would render as an empty clickable tab — drop those BEFORE the preamble
    # is attached, or a fully-hidden tab would survive as an intro-only pane.
    panes = [p for p in panes if p["sections"]]
    if not panes or (len(panes) == 1 and panes[0]["id"] == "more"):
        # Nothing survived the visibility filter, or the only survivor is the
        # auto-"More" catch-all (a one-tab bar that says "More" would be
        # pure noise) — fall back to the flat single-page flow.
        return flat, None
    preambles = [s for s in kept if not s["id"]]
    if preambles:
        panes[0]["sections"] = preambles + panes[0]["sections"]
    return flat, panes
