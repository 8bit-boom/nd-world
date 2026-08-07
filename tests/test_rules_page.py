"""/rules renders core_rules.md and per-world custom rules_md as HTML.
Regression coverage for a bug where legacy <a name="..."></a> anchors
(from a Word/Docs export) got HTML-escaped by render_md()'s XSS guard and
then double-escaped again in the sidebar TOC, showing literal "&lt;a
name=..." text instead of a clean heading — see app.main._world_rules_markdown.
"""
from app.database import SessionLocal
from app.models import World

from .conftest import GM_PASSWORD, login


def test_core_rules_anchor_tags_not_shown_as_text(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "<a name=" not in r.text.lower().replace("&lt;a name=", "")
    assert "&lt;a name=" not in r.text
    assert "&amp;lt;a name=" not in r.text


def test_custom_world_rules_anchor_tags_not_shown_as_text(client, seed):
    """Same bug, but via a world's own custom rules_md instead of the
    bundled core_rules.md fallback — both paths go through
    _world_rules_markdown()."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = '## <a name="test-heading"></a>1. Test Heading\n\nBody text.'
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert "&lt;a name=" not in r.text
    assert "&amp;lt;a name=" not in r.text
    assert "Test Heading" in r.text
    assert "Body text." in r.text


def test_rules_toc_labels_are_clean(client, seed):
    """The sidebar TOC label for a heading must be plain text, not leak
    HTML-entity artifacts from the anchor-stripping/escaping pipeline."""
    login(client, seed.gm.email, GM_PASSWORD)
    db = SessionLocal()
    try:
        w = db.query(World).filter(World.id == seed.world_a.id).first()
        w.rules_md = '## <a name="core-stats"></a>1. Core Stats\n\nBody text.'
        db.commit()
    finally:
        db.close()

    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/rules")
    assert r.status_code == 200
    assert '>1. Core Stats<' in r.text
    assert "&amp;lt;" not in r.text
