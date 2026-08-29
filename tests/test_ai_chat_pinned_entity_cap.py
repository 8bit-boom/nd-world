"""Regression test for docs/DYNAMIC_THINKING_AND_PIPELINE_PLAN.md Part 2
item 3.5: _pinnedEntitiesContext (static/js/ai-chat-core.js) previously
injected each pinned entity's FULL body into every single chat send —
unlike the keyword-searched RAG context (naturally size-bounded), a pinned
entity stays pinned across every turn, so a few long write-ups could dwarf
the retrieved context and (after item 3.4's interactive context sizing)
inflate the auto-sized num_ctx every turn. JS-source assertion test, reading
the static file directly — matches this repo's established convention for
template-JS regression coverage."""
from pathlib import Path

_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "ai-chat-core.js").read_text()


def test_per_entity_body_cap_constant_exists():
    assert "const PINNED_ENTITY_BODY_CHAR_CAP = 4000;" in _JS


def test_combined_total_cap_constant_exists():
    assert "const PINNED_ENTITIES_TOTAL_CHAR_CAP = 12000;" in _JS


def test_per_entity_body_is_sliced_with_a_visible_truncation_notice():
    body = _JS.split("async function _pinnedEntitiesContext()", 1)[1]
    assert "body.slice(0, PINNED_ENTITY_BODY_CHAR_CAP)" in body
    assert "truncated — open the entity for the rest" in body


def test_combined_budget_drops_with_a_visible_notice_keeping_insertion_order():
    body = _JS.split("async function _pinnedEntitiesContext()", 1)[1]
    assert "usedChars + part.length > PINNED_ENTITIES_TOTAL_CHAR_CAP" in body
    assert "omitted — over the context budget" in body
    # Iterates `parts` (already in _pinnedEntities' insertion/Map order) in
    # a single forward pass — no sort/reverse that would reorder them.
    assert "for (const part of parts)" in body
