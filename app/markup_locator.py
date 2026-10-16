"""Marking up text from the READ view: find what a GM selected on a rendered page inside the markdown it came from.

A page shows rendered text ("The cult spy"), the field stores markdown ("The **cult** spy"). To wrap a selection in [gmonly]...[/gmonly] (or
[spoiler], bold ...) without opening the editor, the selected text - plus a little text either side of it - is located in the source:

  1. every piece of syntax that renders to nothing (``**``, ``~~``, backticks, a heading's ``#``, a list bullet, a link's ``](url)``, our own
     ``[u]`` / ``[spoiler]`` / ``[gmonly]`` tags) is masked, whitespace is collapsed - a projection of the source that reads like the page;
  2. the selection is searched for in the projection. One match: that is it. Several: the text around the selection picks the one that shares
     the most with it, and a tie is refused ("select a little more") rather than guessed - marking the wrong sentence GM-only would hide the
     wrong thing from players, and the other way round would leak;
  3. the match is mapped back to source offsets, split per line (a list item or a paragraph keeps its own wrapper, a bullet stays outside it),
     and each piece is widened over markup it only half covers (the closing ``**`` of a bold phrase the selection ended inside).

Pure functions over strings, no database, tested on their own (tests/test_markup_locator.py)."""
import re

# what is NOT shown: our tags, link / image syntax (the text of a link stays), emphasis marks, code ticks
_TAG = r"\[/?(?:u|spoiler|gmonly|mark|color)(?:=[^\]\n]{0,20})?\]"
_MASK_RE = re.compile(
    r"(?P<tag>" + _TAG + r")"
    r"|(?P<linkend>\]\([^)\n]*\))|(?P<open>!?\[)"
    r"|(?P<strong>\*\*|__|~~)|(?P<tick>`+)"
    r"|(?P<em>(?<![*\w])\*(?=\S)|(?<=\S)\*(?![*\w])|(?<![\w_])_(?=\S)|(?<=\S)_(?![\w_]))",
    re.IGNORECASE,
)
# what a line starts with that is not shown: heading hashes, bullets, numbers, quotes
_LINE_START_RE = re.compile(r"^[ \t]{0,3}(?:(?:#{1,6}|[-*+]|\d+[.)]|>)[ \t]+)+", re.MULTILINE)
_TABLE_RULE_RE = re.compile(r"^[ \t]*\|?[ \t:-]+(?:\|[ \t:-]+)+\|?[ \t]*$", re.MULTILINE)
_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", "–": "-", "—": "-"}
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))

ACTIONS = {
    "gmonly": ("[gmonly]", "[/gmonly]"),
    "spoiler": ("[spoiler]", "[/spoiler]"),
    "mark": ("[mark]", "[/mark]"),
    "underline": ("[u]", "[/u]"),
    "bold": ("**", "**"),
    "italic": ("*", "*"),
    "strike": ("~~", "~~"),
}
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
UNWRAPPABLE = {"gmonly": "gmonly", "spoiler": "spoiler", "mark": "mark"}

MAX_SELECTION = 4000


class MarkupError(ValueError):
    """The selection cannot be placed safely; the message says what to do instead."""


def _norm(text: str) -> str:
    text = text.translate(_ZERO_WIDTH)
    text = "".join(_QUOTES.get(c, c) for c in text)
    return re.sub(r"\s+", " ", text).strip()


def _project(source: str):
    """The source as the page shows it: (text, idx) - the text with markup removed and whitespace collapsed, and for each of its characters
    the offset it came from."""
    masked = [False] * len(source)
    for m in _MASK_RE.finditer(source):
        for k in range(m.start(), m.end()):
            masked[k] = True
    for rx in (_LINE_START_RE, _TABLE_RULE_RE):
        for m in rx.finditer(source):
            for k in range(m.start(), m.end()):
                masked[k] = True
    for k, ch in enumerate(source):
        if ch == "|":
            masked[k] = True
    out, idx = [], []
    pending_space = None
    for k, ch in enumerate(source):
        if masked[k]:
            continue
        ch = _QUOTES.get(ch, ch)
        if ch in "​‌‍﻿":
            continue
        if ch.isspace():
            if out and pending_space is None:
                pending_space = k
            continue
        if pending_space is not None:
            out.append(" ")
            idx.append(pending_space)
            pending_space = None
        out.append(ch)
        idx.append(k)
    return "".join(out), idx


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def locate(source: str, selected: str, before: str = "", after: str = ""):
    """Where `selected` is in `source`: a list of (start, end) source ranges, one per line it covers. Raises MarkupError when it is not
    found, or not certain."""
    if not source:
        raise MarkupError("There is no text to mark.")
    sel = _norm(selected)
    if not sel:
        raise MarkupError("Select some text first.")
    if len(sel) > MAX_SELECTION:
        raise MarkupError("That is a lot of text - select a smaller part.")
    text, idx = _project(source)
    hits, at = [], text.find(sel)
    while at != -1:
        hits.append(at)
        at = text.find(sel, at + 1)
    if not hits:
        raise MarkupError("Could not find that text in the source - it may come from a template, a link or generated text. "
                          "Select plain text inside one paragraph, or use the editor.")
    if len(hits) > 1:
        b, a = _norm(before) if before else "", _norm(after) if after else ""
        scored = []
        for h in hits:
            s = _common_suffix(text[:h].rstrip(), b.rstrip()) + _common_prefix(text[h + len(sel):].lstrip(), a.lstrip())
            scored.append((s, h))
        scored.sort(reverse=True)
        if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            raise MarkupError("That text appears more than once - select a little more around it so it is clear which one.")
        hits = [scored[0][1]]
    a = hits[0]
    b = a + len(sel)
    # one source range per line (a bullet or a heading mark stays outside the wrapper)
    groups, last_line = [], None
    for k in range(a, b):
        if text[k] == " ":
            continue
        src = idx[k]
        line = source.count("\n", 0, src)
        if groups and line == last_line:
            groups[-1][1] = src + 1
        else:
            groups.append([src, src + 1])
        last_line = line
    return [(s, e) for s, e in groups]


_PAIRED = (
    ("bold", re.compile(r"\*\*")), ("underscore", re.compile(r"__")), ("strike", re.compile(r"~~")),
    ("code", re.compile(r"`")), ("italic", re.compile(r"(?<!\*)\*(?!\*)")),
)


def _paragraph(source: str, s: int, e: int):
    lo = source.rfind("\n\n", 0, s)
    hi = source.find("\n\n", e)
    return (0 if lo < 0 else lo + 2), (len(source) if hi < 0 else hi)


def _widen(source: str, s: int, e: int):
    """A range that cuts through a pair of emphasis marks (`**bo|ld**` ending inside the bold) is widened over its partner, so the wrapper
    encloses whole formatting and the rendered HTML stays well formed. Marks pair up in reading order within a paragraph (1st with 2nd ...)."""
    for _ in range(3):
        before = (s, e)
        for name, rx in _PAIRED:
            lo, hi = _paragraph(source, s, e)
            marks = [(m.start(), m.end()) for m in rx.finditer(source, lo, hi)]
            inside = [k for k, (a, b) in enumerate(marks) if a >= s and b <= e]
            if not inside:
                continue
            first, last = inside[0], inside[-1]
            if first % 2:                                    # its partner (the opening mark) is before the selection
                s = marks[first - 1][0]
            if last % 2 == 0:                                # its partner (the closing mark) is after it
                if last + 1 >= len(marks):
                    raise MarkupError("That selection cuts across bold / italic / code - extend it over the whole formatted part, or use the editor.")
                e = marks[last + 1][1]
        if (s, e) == before:
            break
    seg = source[s:e]
    for name, rx in _PAIRED:
        lo, hi = _paragraph(source, s, e)
        marks = [m.start() for m in rx.finditer(source, lo, hi)]
        if sum(1 for a in marks if s <= a < e) % 2:
            raise MarkupError("That selection cuts across bold / italic / code - extend it over the whole formatted part, or use the editor.")
    for tag in ("u", "spoiler", "gmonly", "mark"):
        opens = len(re.findall(r"\[%s(?:=[^\]\n]{0,20})?\]" % tag, seg, re.IGNORECASE))
        closes = len(re.findall(r"\[/%s\]" % tag, seg, re.IGNORECASE))
        if opens != closes:
            raise MarkupError("That selection cuts across a [%s] part - extend it over the whole part, or use the editor." % tag)
    return s, e


def _inside(source: str, s: int, tag: str) -> bool:
    """Is offset `s` already inside a [tag]...[/tag] block?"""
    for m in re.finditer(r"\[%s\](.*?)\[/%s\]" % (tag, tag), source, re.DOTALL | re.IGNORECASE):
        if m.start() <= s < m.end():
            return True
    return False


def wrap(source: str, selected: str, before: str, after: str, action: str):
    """The source with the selection wrapped for `action` ('gmonly', 'spoiler', 'mark', 'underline', 'bold', 'italic', 'strike' or
    'color:#rrggbb')."""
    if action.startswith("color:"):
        color = action[6:]
        if not _COLOR_RE.match(color):
            raise MarkupError("That colour is not allowed.")
        open_, close = "[color=%s]" % color, "[/color]"
    elif action in ACTIONS:
        open_, close = ACTIONS[action]
    else:
        raise MarkupError("Unknown action.")
    ranges = [_widen(source, s, e) for s, e in locate(source, selected, before, after)]
    if action in ("gmonly", "spoiler") and all(_inside(source, s, action) for s, _ in ranges):
        raise MarkupError("That is already marked.")
    out = source
    for s, e in reversed(ranges):
        seg = out[s:e]
        if action in ("gmonly", "spoiler"):
            # a part already marked inside the new one is subsumed: nested tags would pair up wrongly when rendered
            seg = re.sub(r"\[/?%s\]" % action, "", seg, flags=re.IGNORECASE)
        out = out[:s] + open_ + seg + close + out[e:]
    return out


def unwrap(source: str, tag: str, n: int, snippet: str = ""):
    """The source with the n-th (0-based, in reading order) [tag]...[/tag] block unwrapped - its text stays. `snippet` (the start of what the
    page showed in it) guards against the page and the source having drifted apart."""
    if tag not in UNWRAPPABLE:
        raise MarkupError("Unknown action.")
    blocks = list(re.finditer(r"\[%s\](.*?)\[/%s\]" % (tag, tag), source, re.DOTALL | re.IGNORECASE))
    if not 0 <= n < len(blocks):
        raise MarkupError("Could not find that block any more - reload the page.")
    m = blocks[n]
    if snippet and _norm(snippet)[:30] not in _project(m.group(1))[0]:
        raise MarkupError("The text changed since this page was loaded - reload it.")
    return source[:m.start()] + m.group(1) + source[m.end():]


MAX_REPLACEMENT = 6000


def replace(source: str, selected: str, before: str, after: str, replacement: str):
    """The source with the selection's text swapped for `replacement` (an AI rewrite the GM accepted). Only a selection inside ONE line is
    replaced: a selection over several paragraphs or list items would need the new text split back over them, which cannot be done safely."""
    replacement = (replacement or "").strip()
    if not replacement:
        raise MarkupError("There is no replacement text.")
    if len(replacement) > MAX_REPLACEMENT:
        raise MarkupError("The replacement is too long.")
    ranges = locate(source, selected, before, after)
    if len(ranges) != 1:
        raise MarkupError("Select text inside one paragraph or list item to replace it - or use the editor.")
    s, e = _widen(source, *ranges[0])
    return source[:s] + replacement + source[e:]


def restore(source: str, expected_now: str, previous: str):
    """Undo: `previous` comes back only if the field is still exactly what the action left (`expected_now`) - never over a later edit."""
    if source != expected_now:
        raise MarkupError("This was edited since - open the editor to change it back.")
    return previous
