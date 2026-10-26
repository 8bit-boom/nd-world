def test_export_rules_and_notes_is_pythons(client, seed):
    """GET /export/rules-and-notes.md from the gateway: the rules (anchors stripped), the world's note entities (full
    text, hidden included, subtype tags) and every entity note grouped under its entity - byte for byte, the trailing
    whitespace collapsed as Python's rstrip does; a player refused as Python refuses; no world is Python's 404."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import Entity, EntityNote, World
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.get(World, w).rules_md = "# House Rules\n\nRoll d10.\n\n<a name=\"x\"></a>## Keep\n\nBe kind.\n\n\n\n"
        db.add_all([
            Entity(world_id=w, kind="note", name="Dossier", subtype="Rumor", body="  The mayor lies.  ", summary="s"),
            Entity(world_id=w, kind="note", name="Ledger", body="", summary="  the numbers  "),
            Entity(world_id=w, kind="note", name="Hidden note", body="Secret.", visible_to_players=False),
            Entity(world_id=w, kind="item", name="Blade"),
            Entity(world_id=seed.world_b.id, kind="note", name="Elsewhere"),
        ])
        db.flush()
        db.add_all([
            EntityNote(entity_id=4, content="Sharp.", visible_to_players=True),      # Blade's note
            EntityNote(entity_id=4, content="  Kept oiled.  ", visible_to_players=False),
        ])
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    v = _via(d, base)
    a, b = d.get("/export/rules-and-notes.md"), v.get("/export/rules-and-notes.md")
    assert a.status_code == b.status_code == 200, (a.status_code, b.status_code, a.text[:200])
    assert b.headers.get("x-nd-served-by") == "gateway"
    assert a.content == b.content, (a.text[:300], b.text[:300])
    assert a.headers.get("content-type") == b.headers.get("content-type")
    assert a.headers["content-disposition"] == b.headers["content-disposition"] == 'attachment; filename="world-a-rules-and-notes.md"'
    t = a.text
    assert t.startswith("# World A — Rules & Notes\n\n# House Rules\n\nRoll d10.\n\n## Keep\n\nBe kind.\n"), t[:120]
    assert "anchor" not in t, t[:200]
    assert "## Notes\n\n### Dossier [Rumor]\n\nThe mayor lies.\n" in t, t
    assert "### Ledger\n\nthe numbers\n" in t and "### Hidden note\n\nSecret.\n" in t, t
    assert "## Entity notes\n\n### Blade (Item)\n\nSharp.\n\n  Kept oiled.  \n" in t, t
    assert t.endswith("Kept oiled.  \n"), t[-60:]
    # a player refused exactly as Python refuses
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    a2, b2 = pl.get("/export/rules-and-notes.md"), _via(pl, base).get("/export/rules-and-notes.md")
    assert (a2.status_code, a2.text) == (b2.status_code, b2.text) and a2.status_code == 403, a2.status_code
