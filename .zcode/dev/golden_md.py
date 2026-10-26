def test_session_md_downloads_are_pythons(client, seed):
    """GET /sessions/<id>/summary.md and /transcript.md from the gateway: the text with [gmonly] stripped for a player,
    the sanitized file name, read-gated by the row's own world's sessions section; the no-content 404s in Python's
    words; another world's session and a closed section the same 404; a title beyond Python's ASCII file names stays
    Python's."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import GameSession, World, WorldMembership
    base = str(client.base_url)
    w = seed.world_a.id
    db = SessionLocal()
    try:
        db.add_all([
            GameSession(world_id=w, title="Docks", session_num=1,
                        summary="The meet went [gmonly]sideways fast[/gmonly] well.",
                        live_transcript="Vosk: You came alone? [gmonly]The crate is armed.[/gmonly]"),
            GameSession(world_id=w, title="Empty", session_num=2, summary="", live_transcript=""),
            GameSession(world_id=w, title="Ünicode — Title!", session_num=3, summary="Something."),
            GameSession(world_id=seed.world_b.id, title="Over There", session_num=1, summary="Other world."),
        ])
        db.commit()
        ids = [s.id for s in db.query(GameSession).order_by(GameSession.id).all()] + [999999]
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    pb = _signed_in(seed.player_b.email, PLAYER_PASSWORD, seed.world_b.slug)
    served = 0
    for who, c in (("gm", d), ("player", pl), ("stranger", pb)):
        for i in ids:
            for url in (f"/sessions/{i}/summary.md", f"/sessions/{i}/transcript.md"):
                a, b = c.get(url), _via(c, base).get(url)
                assert a.status_code == b.status_code, (who, url, a.status_code, b.status_code, a.text[:100], b.text[:100])
                if a.status_code == 200:
                    if b.headers.get("x-nd-served-by") == "gateway":
                        assert a.content == b.content, (who, url, a.text[:150], b.text[:150])
                        assert a.headers.get("content-type") == b.headers.get("content-type")
                        assert a.headers.get("content-disposition") == b.headers.get("content-disposition"), (url, a.headers.get("content-disposition"))
                        served += 1
                else:
                    assert a.json() == b.json(), (who, url, a.text[:100], b.text[:100])
    assert served >= 6, served
    # the GM's file keeps [gmonly]; the player's strips it
    g = d.get(f"/sessions/{ids[0]}/summary.md")
    assert "sideways fast" in g.text and g.headers["content-disposition"] == 'attachment; filename="Docks-summary.md"', g.text
    p = pl.get(f"/sessions/{ids[0]}/summary.md")
    assert "sideways fast" not in p.text and "went  well" in p.text, p.text
    pt = pl.get(f"/sessions/{ids[0]}/transcript.md")
    assert "The crate is armed." not in pt.text, pt.text
    # no summary / transcript: Python's own 404 words
    a = d.get(f"/sessions/{ids[1]}/summary.md")
    assert a.status_code == 404 and a.json()["detail"] == "This session has no summary yet", a.text
    a = d.get(f"/sessions/{ids[1]}/transcript.md")
    assert a.status_code == 404 and a.json()["detail"] == "This session has no live transcript yet", a.text
    # another world's session: the same 404 as a missing one (the stranger belongs there, the player doesn't)
    assert pl.get(f"/sessions/{ids[3]}/summary.md").status_code == 404
    assert pb.get(f"/sessions/{ids[3]}/summary.md").status_code == 200
    # a non-ASCII title keeps Python's file name (the gateway declines)
    r = _via(d, base).get(f"/sessions/{ids[2]}/summary.md")
    assert r.status_code == 200 and r.headers.get("x-nd-served-by") != "gateway"
