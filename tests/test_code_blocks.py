"""Fenced code blocks in rendered markdown (entity notes, rules pages, AI answers).

A GM imported a note whose "Krea prompts" are one-line paragraphs of prose in ``` fences, plus an ASCII map in another
fence. Both ran off the right edge of the page: the notes' `<pre>` had no styling at all (only the `.prose` wrapper had
any), so each line got its own inline-code background and nothing scrolled or wrapped. Now:
  - every code block is a boxed block that scrolls sideways instead of overflowing its container;
  - a block that is prose (long lines of ordinary words — an image prompt, a paragraph of read-aloud text) wraps, and
    one that is aligned text (an ASCII map, a table, code) keeps its columns and scrolls."""
import re
from pathlib import Path

from app.rendering import render_md, wraps_as_prose

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "style.css").read_text()

PROMPT = (
    "A wide cinematic dark fantasy establishing shot of Reed-Hollow, a gloomy, claustrophobic marsh village built over "
    "muddy peat bogs in the Halu Swamps, hyper-detailed dark fantasy realism inspired by Bloodborne and grim gothic horror."
)
DIAGRAM = (
    "                  [Reed-Hollow]\n"
    "          (Quest 1: The Soporific Press)\n"
    "                  /                             /                  [Gallow-Stilt] ------------ [Black-Sump]\n"
    "(Quest 2: The Silent Ward)    (Quest 3: The Midnight Sluice)\n"
    "                 \\              /\n"
    "               [The Submerged Grotto]\n"
)


def _pres(html):
    return re.findall(r"<pre[^>]*>", html)


def test_a_paragraph_of_prose_wraps():
    assert wraps_as_prose(PROMPT)
    assert _pres(render_md(f"```text\n{PROMPT}\n```")) == ['<pre class="md-wrap">']


def test_aligned_text_keeps_its_columns():
    # the map's long line is made of words too, but they are spread out by runs of spaces: wrapping would wreck it
    assert not wraps_as_prose(DIAGRAM)
    assert _pres(render_md(f"```\n{DIAGRAM}```")) == ["<pre>"]


def test_short_blocks_and_single_tokens_are_left_alone():
    assert not wraps_as_prose("print('hello')")
    assert not wraps_as_prose("docker compose up -d")
    assert not wraps_as_prose("")
    # one very long token (a URL, a base64 blob) has no word breaks to wrap at: it scrolls
    assert not wraps_as_prose("https://example.com/" + "a" * 200)
    # an indented line of prose is still prose: leading indentation is not alignment
    assert wraps_as_prose("    " + PROMPT)


def test_a_block_with_one_prose_line_among_code_wraps_only_if_no_line_is_aligned():
    mixed = "def f():\n    return 1\n" + PROMPT
    assert wraps_as_prose(mixed)


def test_the_block_content_and_its_escaping_are_untouched():
    html = render_md("```text\n" + PROMPT + " <script>alert(1)</script> & more words in this sentence to be long\n```")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; more words" in html


def test_inline_code_and_ordinary_paragraphs_are_not_touched():
    html = render_md("Run `docker compose up -d` and read " + PROMPT)
    assert "md-wrap" not in html


def test_the_note_from_the_report_renders_prompts_wrapped_and_the_map_not():
    src = "## Map\n\n```\n" + DIAGRAM + "```\n\n## Prompts\n\n```text\n" + PROMPT + "\n```\n\n```text\n" + PROMPT + "\n```\n"
    assert _pres(render_md(src)) == ["<pre>", '<pre class="md-wrap">', '<pre class="md-wrap">']


def test_code_blocks_are_styled_wherever_markdown_is_rendered():
    # `.prose pre` was the only rule: notes (.rendered-md) and AI answers (.ai-bubble) got the browser default
    block = re.search(r"(?m)^pre\s*\{([^}]*)\}", CSS)
    assert block, "a base `pre` rule is missing"
    assert "max-width: 100%" in block.group(1) and "overflow-x: auto" in block.group(1)
    boxed = re.search(r"(?s)(\.rendered-md pre[^{]*)\{([^}]*)\}", CSS)
    assert boxed and ".prose pre" in boxed.group(1) and ".ai-bubble pre" in boxed.group(1)
    assert "background: var(--bg3)" in boxed.group(2) and "padding" in boxed.group(2)
    # and the code inside is not a second, inline-code box on every line
    flat = re.search(r"(?s)(\.rendered-md pre code[^{]*)\{([^}]*)\}", CSS)
    assert flat and ".prose pre code" in flat.group(1) and "background: transparent" in flat.group(2)
    wrap = re.search(r"(?s)pre\.md-wrap[^{]*\{([^}]*)\}", CSS)
    assert wrap and "white-space: pre-wrap" in wrap.group(1) and "overflow-wrap: anywhere" in wrap.group(1)
