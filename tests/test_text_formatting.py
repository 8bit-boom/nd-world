"""Tests for the [color=]/[mark]/[u] inline-styling syntax added to
app.rendering.render_md, and its use on the private-notes route (the one
place that switched from plain-escaped text to full markdown rendering).
"""
from app.rendering import render_md

from .conftest import GM_PASSWORD, login


def test_color_tag_renders_span_with_validated_hex():
    assert render_md("[color=#ff0000]Danger![/color]") == '<p><span style="color:#ff0000">Danger!</span></p>\n'


def test_color_tag_accepts_allowlisted_named_color():
    html = render_md("[color=red]warning[/color]")
    assert '<span style="color:red">warning</span>' in html


def test_color_tag_rejects_unrecognized_value():
    # Not a hex code or an allowlisted name — falls back to unstyled text
    # rather than emitting an unvalidated style attribute.
    html = render_md("[color=javascript:alert(1)]x[/color]")
    assert "style=" not in html
    assert html == "<p>x</p>\n"


def test_color_tag_cannot_break_out_of_style_attribute():
    html = render_md('[color=red" onmouseover="alert(1)]x[/color]')
    assert html == '<p>[color=red" onmouseover="alert(1)]x[/color]</p>\n'
    assert "<span" not in html


def test_mark_tag_renders_default_and_colored_highlight():
    assert "<mark>hi</mark>" in render_md("[mark]hi[/mark]")
    html = render_md("[mark=cyan]hi[/mark]")
    assert '<mark style="background-color:cyan">hi</mark>' in html


def test_underline_tag_renders():
    assert "<u>text</u>" in render_md("[u]text[/u]")


def test_inline_styles_nest_with_markdown_bold():
    html = render_md("[color=#00ffcc]colored **and bold**[/color]")
    assert "<strong>and bold</strong>" in html
    assert html.startswith('<p><span style="color:#00ffcc">colored <strong>')


def test_raw_html_still_escaped_alongside_color_tag():
    html = render_md('<script>alert(1)</script> [color=red]x[/color]')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '<span style="color:red">x</span>' in html


def test_bracket_tags_inside_u_are_still_html_escaped():
    html = render_md("[u]<img src=x onerror=alert(1)>[/u]")
    assert "<img" not in html
    assert "&lt;img" in html


def test_empty_and_none_input_unchanged():
    assert render_md("") == ""
    assert render_md(None) == ""


def test_private_note_content_renders_markdown_and_color(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}/new",
        data={"title": "Hook", "content": "**Important**: [color=red]watch the docks[/color]."},
        follow_redirects=False,
    )
    assert r.status_code == 303

    view = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert view.status_code == 200
    assert "<strong>Important</strong>" in view.text
    assert '<span style="color:red">watch the docks</span>' in view.text


def test_private_note_content_escapes_raw_script(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}/new",
        data={"title": "", "content": "<script>alert(1)</script>"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    view = client.get(f"/worlds/{seed.world_a.id}/notes/{seed.player_a.id}")
    assert "<script>alert(1)</script>" not in view.text
    assert "&lt;script&gt;" in view.text
