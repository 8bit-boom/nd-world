"""Regression test for docs/MOBILE_UI_UX_AUDIT.md's iOS-zoom note ("Inline
font-size < 16px on many inputs ... defeats the global iOS anti-zoom fix")
as it applies to the entity detail page's Ask AI panel (entities/detail.html).

The global mobile rule (static/style.css: `textarea { font-size: 16px }`)
exists specifically to stop iOS Safari auto-zooming the page on focus, but
`.ai-panel .ep-bar textarea`'s own `font-size:.85rem` rule is more specific
and declared later in the document (an inline <style> block after style.css's
<link>), so it silently won and re-broke the anti-zoom fix for this one
textarea. The same crushed-onto-one-row layout bug fixed for the main AI
Chat input bar (.ai-input-bar, see test_ai_chat_mobile_drawer.py's sibling
fix) also applies here — attach/mic/bgjob/textarea/send, plus a separate
mode-switch row whose button labels include the entity's own (unbounded-
length) name."""
from app.database import SessionLocal
from app.models import Entity

from .conftest import GM_PASSWORD, login


def _make_entity(world_id, name="Ask AI Panel Test Entity"):
    db = SessionLocal()
    try:
        e = Entity(world_id=world_id, kind="character", name=name)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def test_ep_bar_textarea_forces_16px_on_mobile_overriding_the_inline_rule(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200

    media_block, rest = r.text.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)
    ep_bar_textarea_block = media_block.split(".ai-panel .ep-bar textarea {", 1)[1].split("}", 1)[0]
    assert "font-size:16px" in ep_bar_textarea_block

    # The base (non-mobile) rule, declared further down the page (outside
    # the media query — see `rest`), is still the smaller size: the mobile
    # override exists specifically because that base rule's higher
    # specificity beats the global anti-zoom selector.
    base_block = rest.split(".ai-panel .ep-bar textarea {", 1)[1].split("}", 1)[0]
    assert "font-size:.85rem" in base_block


def test_ep_bar_and_mode_row_wrap_on_mobile_instead_of_crushing(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    entity_id = _make_entity(seed.world_a.id)
    r = client.get(f"/entity/{entity_id}")
    assert r.status_code == 200

    media_block = r.text.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    ep_bar_block = media_block.split(".ai-panel .ep-bar {", 1)[1].split("}", 1)[0]
    assert "flex-wrap:wrap" in ep_bar_block

    textarea_block = media_block.split(".ai-panel .ep-bar textarea {", 1)[1].split("}", 1)[0]
    assert "flex:1 1 100%" in textarea_block
    assert "order:-1" in textarea_block

    mode_row_block = media_block.split(".ep-mode-row {", 1)[1].split("}", 1)[0]
    assert "flex-wrap:wrap" in mode_row_block
