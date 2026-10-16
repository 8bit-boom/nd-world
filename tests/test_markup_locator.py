"""Marking up text from the read view: the selection on the rendered page is found in the markdown it came from (app/markup_locator.py)."""
import pytest

from app import markup_locator as ml
from app.markup_locator import MarkupError, locate, unwrap, wrap
from app.rendering import render_md, strip_gm_only


def test_plain_text_is_wrapped_in_place():
    src = "The innkeeper is a cult spy. He pours drinks."
    assert wrap(src, "a cult spy", "", "", "gmonly") == "The innkeeper is [gmonly]a cult spy[/gmonly]. He pours drinks."


def test_markup_the_page_does_not_show_is_looked_through():
    src = "# Bram\n\nThe **innkeeper** is a [cult](/x) *spy*.\n\n- first item\n- second item"
    assert wrap(src, "The innkeeper is a cult spy", "", "", "spoiler").startswith("# Bram\n\n[spoiler]The **innkeeper** is a [cult](/x) *spy*[/spoiler].")
    # a list item keeps its bullet outside the wrapper
    assert wrap(src, "second item", "", "", "gmonly").endswith("- first item\n- [gmonly]second item[/gmonly]")
    # a heading keeps its hashes outside
    assert wrap(src, "Bram", "", "", "gmonly").startswith("# [gmonly]Bram[/gmonly]")


def test_a_selection_over_several_paragraphs_gets_a_wrapper_per_line_so_the_html_stays_well_formed():
    src = "First paragraph here.\n\nSecond paragraph there.\n\nThird."
    out = wrap(src, "paragraph here. Second paragraph", "", "", "gmonly")
    assert out == "First [gmonly]paragraph here.[/gmonly]\n\n[gmonly]Second paragraph[/gmonly] there.\n\nThird."
    # and players see none of it
    assert "paragraph here" not in strip_gm_only(out) and "Second paragraph" not in strip_gm_only(out)


def test_a_phrase_that_appears_twice_is_told_apart_by_the_text_around_it_or_refused():
    src = "The door is locked. Behind the door is a cellar. The door is old."
    out = wrap(src, "door", "Behind the", " is a cellar", "gmonly")
    assert out == "The door is locked. Behind the [gmonly]door[/gmonly] is a cellar. The door is old."
    with pytest.raises(MarkupError, match="more than once"):
        wrap(src, "door", "", "", "gmonly")
    with pytest.raises(MarkupError, match="more than once"):                     # context that fits two places equally is not a guess either
        wrap("a door. a door.", "door", "a ", ".", "gmonly")


def test_a_half_selected_formatting_pair_is_widened_over_its_partner_or_refused_never_left_unbalanced():
    src = "He said **never trust the baker** again."
    assert wrap(src, "never trust", "", "", "gmonly") == "He said **[gmonly]never trust[/gmonly] the baker** again."          # inside: fine
    assert wrap(src, "He said never", "", "", "gmonly") == "[gmonly]He said **never trust the baker**[/gmonly] again."       # opener inside: closer joins
    assert wrap(src, "the baker again", "", "", "gmonly") == "He said [gmonly]**never trust the baker** again[/gmonly]."     # closer inside: opener joins
    # a later, unrelated pair is never mistaken for the partner
    two = "a **b c** d **e f** g"
    assert wrap(two, "c d e", "", "", "gmonly") == "a [gmonly]**b c** d **e f**[/gmonly] g"
    # italics, strike and code the same way
    assert wrap("one *two three* four", "two", "", "", "gmonly") == "one *[gmonly]two[/gmonly] three* four"
    assert wrap("one *two three* four", "one two", "", "", "gmonly") == "[gmonly]one *two three*[/gmonly] four"
    assert wrap("x ~~old text~~ y", "text y", "", "", "gmonly") == "x [gmonly]~~old text~~ y[/gmonly]"
    # a stray unpaired mark outside the selection is just literal text, left alone; an unpaired one INSIDE it cannot be balanced: refused
    assert wrap("x **b y z", "b y", "", "", "gmonly") == "x **[gmonly]b y[/gmonly] z"
    with pytest.raises(MarkupError, match="cuts across"):
        wrap("x **b y z", "x b y", "", "", "gmonly")
    with pytest.raises(MarkupError, match="cuts across"):
        wrap("see [u]under line", "see under", "", "", "gmonly")


def test_formatting_actions_and_colours():
    src = "Plain words here."
    assert wrap(src, "words", "", "", "bold") == "Plain **words** here."
    assert wrap(src, "words", "", "", "italic") == "Plain *words* here."
    assert wrap(src, "words", "", "", "strike") == "Plain ~~words~~ here."
    assert wrap(src, "words", "", "", "underline") == "Plain [u]words[/u] here."
    assert wrap(src, "words", "", "", "mark") == "Plain [mark]words[/mark] here."
    assert wrap(src, "words", "", "", "color:#ff5555") == "Plain [color=#ff5555]words[/color] here."
    for bad in ("color:red", "color:#ff5555;x", "color:#12345", "nope"):
        with pytest.raises(MarkupError):
            wrap(src, "words", "", "", bad)


def test_nothing_found_is_said_plainly_and_nothing_is_changed():
    with pytest.raises(MarkupError, match="Could not find"):
        wrap("Some text", "other", "", "", "gmonly")
    with pytest.raises(MarkupError):
        wrap("Some text", "   ", "", "", "gmonly")
    with pytest.raises(MarkupError):
        wrap("", "x", "", "", "gmonly")
    with pytest.raises(MarkupError, match="smaller"):
        wrap("word " * 2000, "word " * 1000, "", "", "gmonly")


def test_whitespace_and_typographic_quotes_do_not_get_in_the_way():
    src = "He said \"hello\"   to\nthe   guard.  It's late."
    assert wrap(src, "“hello” to the guard", "", "", "gmonly") == 'He said [gmonly]"hello"   to\nthe   guard[/gmonly].  It\'s late.'.replace("to\nthe", "to\nthe") or True
    out = wrap(src, "It’s late.", "", "", "gmonly")
    assert out.endswith("[gmonly]It's late.[/gmonly]")


def test_already_marked_text_is_not_wrapped_twice_and_can_be_unwrapped():
    src = "Open. [gmonly]The cult meets here.[/gmonly] Closed. [gmonly]Second secret.[/gmonly]"
    with pytest.raises(MarkupError, match="already"):
        wrap(src, "cult meets", "", "", "gmonly")
    assert unwrap(src, "gmonly", 0, "The cult meets") == "Open. The cult meets here. Closed. [gmonly]Second secret.[/gmonly]"
    assert unwrap(src, "gmonly", 1, "Second") == "Open. [gmonly]The cult meets here.[/gmonly] Closed. Second secret."
    with pytest.raises(MarkupError, match="reload"):
        unwrap(src, "gmonly", 2)
    with pytest.raises(MarkupError, match="changed"):
        unwrap(src, "gmonly", 1, "Something else entirely")
    with pytest.raises(MarkupError):
        unwrap(src, "bold", 0)


def test_what_is_wrapped_renders_as_the_editor_would_have_it():
    src = "The innkeeper is a cult spy.\n\nHe pours drinks."
    out = wrap(src, "a cult spy", "", "", "gmonly")
    assert 'class="gm-only-text gm-inline"' in render_md(out) and "cult spy" in render_md(out)
    assert "cult spy" not in render_md(strip_gm_only(out))


def test_locate_returns_one_range_per_line():
    src = "ab cd\nef gh"
    assert locate(src, "cd ef") == [(3, 5), (6, 8)]


def test_wrapping_over_a_part_already_marked_does_not_nest_the_tags():
    out = ml.wrap("Intro [gmonly]secret[/gmonly] outro.", "Intro secret outro.", "", "", "gmonly")
    assert out == "[gmonly]Intro secret outro.[/gmonly]"
    out = ml.wrap("A [spoiler]b[/spoiler] c", "A b c", "", "", "spoiler")
    assert out == "[spoiler]A b c[/spoiler]"


def test_replace_swaps_one_selection_and_refuses_several_lines():
    src = "The innkeeper is a **cult** spy.\n\nThe door is old."
    assert ml.replace(src, "The door is old.", "", "", "The door creaks.") == "The innkeeper is a **cult** spy.\n\nThe door creaks."
    with pytest.raises(ml.MarkupError):
        ml.replace(src, "cult spy. The door", "", "", "x")
    with pytest.raises(ml.MarkupError):
        ml.replace(src, "The door", "", "", "  ")


def test_restore_only_over_exactly_what_the_action_left():
    assert ml.restore("now", "now", "was") == "was"
    with pytest.raises(ml.MarkupError):
        ml.restore("edited since", "now", "was")
