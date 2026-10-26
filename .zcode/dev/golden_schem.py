def test_schematic_new_and_ai_new_pages_are_pythons(client, seed):
    """GET /maps/schematic/new (the empty canvas form, player-safe) and GET /characters/ai-new (the AI creator page: the
    world's custom systems, the selected template id, the player's display name; a player at their limit redirected) from
    the gateway; the POST /maps/schematic/new write: the slug uniquified, the canvas bounds checked, the row as
    SQLAlchemy writes it, the redirect - run on two identical worlds and compared; Python's refusals (an empty name, bad
    bounds, a missing required field) reached unchanged."""
    from .conftest import PLAYER_PASSWORD
    from app.database import SessionLocal
    from app.models import SheetTemplate, World
    base = str(client.base_url)
    db = SessionLocal()
    try:
        tpl = SheetTemplate(world_id=seed.world_a.id, name="Mekton", slug="mekton-schem", sheet_mode="custom", fields_json="[]")
        db.add(tpl)
        db.commit()
    finally:
        db.close()
    d = _signed_in(seed.gm.email, GM_PASSWORD, seed.world_a.slug)
    pl = _signed_in(seed.player_a.email, PLAYER_PASSWORD, seed.world_a.slug)
    served = 0
    for who, c in (("gm", d), ("player", pl)):
        served += _same_page(c, _via(c, base), "/maps/schematic/new", who)
        for url in ("/characters/ai-new", "/characters/ai-new?template_id=5", "/characters/ai-new?template_id=0"):
            served += _same_page(c, _via(c, base), url, who)
    assert served >= 5, served
    # the AI page really lists the world's custom systems and the display name
    page = d.get("/characters/ai-new").text
    assert "Mekton" in page, page[:400]
    # a template id FastAPI would refuse: the same 422 from either side, gateway-declined
    a, b = d.get("/characters/ai-new?template_id=x"), _via(d, base).get("/characters/ai-new?template_id=x")
    assert a.status_code == b.status_code == 422, (a.status_code, b.status_code)
    # the POST: the same form on two identical worlds, the rows compared
    from tests.test_gateway_writes import _row_by
    form = {"name": "Rooftop Chase", "description": "The big map", "canvas_width": "3000", "canvas_height": "2000", "canvas_bg": "dark"}
    out = []
    for side in (d, _via(d, base)):
        r = side.post("/maps/schematic/new", data=form)
        slug = r.headers.get("location", "").rsplit("/", 1)[-1]
        row = _row_by("schematics", "slug", slug) or {}
        out.append((r.status_code, r.headers.get("location") is not None, row.get("name"), row.get("world_id"),
                    row.get("canvas_width"), row.get("canvas_height"), row.get("canvas_bg"), row.get("description"),
                    row.get("is_html"), row.get("elements_json"), r.headers.get("x-nd-served-by")))
    assert out[0][:2] == out[1][:2] and out[0][1], out
    for k in range(2, 10):
        assert out[0][k] == out[1][k], (k, out)
    assert out[1][9] == "gateway"
    assert out[0][2] == "Rooftop Chase" and out[0][6] == "dark", out
    # a second create with the same name: the slug uniquified (-2), by both sides
    out2 = []
    for side in (d, _via(d, base)):
        r = side.post("/maps/schematic/new", data=form)
        out2.append(r.headers.get("location", "").rsplit("/", 1)[-1])
    assert out2[0] != out2[1] and "rooftop-chase" in out2[0] and "rooftop-chase" in out2[1], out2
    # refusals: an empty name (400), bounds out of range (400), a missing required field (422) - Python's, forwarded
    for data, code in (({"name": "###"}, 400), ({"name": "OK", "canvas_width": "50"}, 400), ({"description": "no name"}, 422)):
        a, b = d.post("/maps/schematic/new", data=data), _via(d, base).post("/maps/schematic/new", data=data)
        assert a.status_code == b.status_code == code, (data, a.status_code, b.status_code)
        assert b.headers.get("x-nd-served-by") != "gateway" or code != 422
