"""Second screen: the GM sends an image / text to a display window on another monitor.

State lives per world on the server (current + recent); the /display page and the SSE stream follow
it. The screen is what the TABLE sees, so text taken from an entity never carries its [gmonly] parts,
and only safe image URLs are accepted. Everything here is GM-only."""
import asyncio
import json

import pytest

from app import live
from app.database import SessionLocal
from app.models import Entity
from app.routers import display as display_router

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture(autouse=True)
def _clean_stage():
    display_router.reset_stage()
    yield
    display_router.reset_stage()


def _gm(client, seed, world=None):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", (world or seed.world_a).slug)


def _entity(seed, **kw):
    db = SessionLocal()
    try:
        e = Entity(world_id=kw.pop("world", seed.world_a).id, kind=kw.pop("kind", "npc"), name=kw.pop("name", "Old Salt"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id
    finally:
        db.close()


def _show(client, **body):
    return client.post("/api/display/show", json=body)


# ── access ───────────────────────────────────────────────────────────────────

def test_everything_is_gm_only(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert _show(client, kind="image", url="/uploads/a.png").status_code == 403
    assert client.post("/api/display/clear").status_code == 403
    assert client.get("/api/display/state").status_code == 403
    assert client.get("/display").status_code == 403
    assert client.get("/api/display/stream").status_code == 403


def test_anonymous_is_refused(client, seed):
    for call in (lambda: client.get("/display", follow_redirects=False),
                 lambda: client.get("/api/display/state", follow_redirects=False),
                 lambda: client.get("/api/display/stream", follow_redirects=False),
                 lambda: client.post("/api/display/show", json={"kind": "image", "url": "/uploads/a.png"}),
                 lambda: client.post("/api/display/clear")):
        r = call()
        assert r.status_code in (401, 403, 303, 307), (r.request.url, r.status_code)
        assert display_router.get_state(1)["current"] is None and display_router.get_state(2)["current"] is None


# ── showing things ───────────────────────────────────────────────────────────

def test_show_an_image_sets_the_stage_and_bumps_the_live_counter(client, seed):
    _gm(client, seed)
    before = live.version(seed.world_a.id)
    r = _show(client, kind="image", url="/uploads/map.png", title="The Drowned Quarter", caption="Night, rain")
    assert r.status_code == 200, r.text
    cur = r.json()["current"]
    assert cur["kind"] == "image" and cur["url"] == "/uploads/map.png" and cur["title"] == "The Drowned Quarter"
    assert live.version(seed.world_a.id) > before
    st = client.get("/api/display/state").json()
    assert st["current"]["url"] == "/uploads/map.png" and st["seq"] == r.json()["seq"]


def test_recent_history_is_newest_first_and_deduped(client, seed):
    _gm(client, seed)
    for i in range(6):
        _show(client, kind="image", url=f"/uploads/{i}.png")
    _show(client, kind="image", url="/uploads/2.png")  # re-show one still in the window: it moves to the front
    recent = [r["url"] for r in client.get("/api/display/state").json()["recent"]]
    assert recent == ["/uploads/2.png", "/uploads/5.png", "/uploads/4.png", "/uploads/3.png", "/uploads/1.png", "/uploads/0.png"]


def test_recent_history_is_capped(client, seed):
    _gm(client, seed)
    for i in range(20):
        _show(client, kind="image", url=f"/uploads/{i}.png")
    assert len(client.get("/api/display/state").json()["recent"]) == display_router.RECENT_LIMIT


def test_the_handlers_check_gm_themselves_not_only_the_middleware():
    """Defence in depth: the allowlist in main.py is the first gate, but a route reused or re-mounted
    elsewhere must still refuse a non-GM."""
    from types import SimpleNamespace
    from fastapi import HTTPException
    for user in (None, SimpleNamespace(is_gm=False)):
        req = SimpleNamespace(state=SimpleNamespace(user=user))
        with pytest.raises(HTTPException) as exc:
            display_router._gm_world(req, None, None)
        assert exc.value.status_code == 403


def test_clear_blanks_the_screen_but_keeps_history(client, seed):
    _gm(client, seed)
    _show(client, kind="image", url="/uploads/a.png")
    r = client.post("/api/display/clear")
    assert r.status_code == 200 and r.json()["current"] is None
    st = client.get("/api/display/state").json()
    assert st["current"] is None and st["recent"][0]["url"] == "/uploads/a.png"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,<script>1</script>", "//evil.example/x.png",
                                 "file:///etc/passwd", "ftp://x/y.png", "", "uploads/relative.png", "/uploads/x\ny.png"])
def test_unsafe_image_urls_are_rejected(client, seed, bad):
    _gm(client, seed)
    assert _show(client, kind="image", url=bad).status_code == 400
    assert client.get("/api/display/state").json()["current"] is None


def test_safe_url_forms_are_accepted(client, seed):
    _gm(client, seed)
    for ok in ("/uploads/a.png", "/static/maps/x.jpg", "https://example.com/pic.webp", "http://10.0.0.5/a.png"):
        assert _show(client, kind="image", url=ok).status_code == 200, ok


def test_unknown_kind_and_junk_bodies_are_400(client, seed):
    _gm(client, seed)
    assert _show(client, kind="video", url="/uploads/a.mp4").status_code == 400
    assert client.post("/api/display/show", content=b"nope", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/display/show", json=["x"]).status_code == 400


def test_title_and_caption_are_bounded(client, seed):
    _gm(client, seed)
    cur = _show(client, kind="image", url="/uploads/a.png", title="T" * 900, caption="C" * 5000).json()["current"]
    assert len(cur["title"]) <= 200 and len(cur["caption"]) <= 600


# ── text cards ───────────────────────────────────────────────────────────────

def test_text_is_rendered_as_safe_markdown(client, seed):
    _gm(client, seed)
    cur = _show(client, kind="text", title="Read aloud", text="The **bell** tolls.\n\n<script>alert(1)</script>").json()["current"]
    assert cur["kind"] == "text" and "<strong>bell</strong>" in cur["html"] and "<script>" not in cur["html"]


def test_empty_text_is_rejected(client, seed):
    _gm(client, seed)
    assert _show(client, kind="text", text="   ").status_code == 400


# ── entities: what the table sees ────────────────────────────────────────────

def test_an_entity_with_an_image_shows_the_image_with_its_name(client, seed):
    eid = _entity(seed, name="Old Salt", image_url="/uploads/salt.png", summary="A boatman.")
    _gm(client, seed)
    cur = _show(client, kind="entity", entity_id=eid).json()["current"]
    assert cur["kind"] == "image" and cur["url"] == "/uploads/salt.png" and cur["title"] == "Old Salt"
    assert cur["source"] == {"type": "entity", "id": eid}


def test_an_entity_as_text_never_shows_gm_only_parts(client, seed):
    eid = _entity(seed, name="Old Salt", body="A boatman.\n\n[gmonly]He is the heir.[/gmonly]\n\n:::gm\nSecret hook\n:::\n\nHe drinks.")
    _gm(client, seed)
    cur = _show(client, kind="entity", entity_id=eid, mode="text").json()["current"]
    assert cur["kind"] == "text" and cur["title"] == "Old Salt"
    assert "boatman" in cur["html"] and "He drinks" in cur["html"]
    assert "heir" not in cur["html"] and "Secret hook" not in cur["html"]


def test_an_entity_without_an_image_falls_back_to_text(client, seed):
    eid = _entity(seed, name="The Ruin", summary="A flooded district.")
    _gm(client, seed)
    cur = _show(client, kind="entity", entity_id=eid).json()["current"]
    assert cur["kind"] == "text" and "flooded district" in cur["html"]


def test_an_entity_from_another_world_is_not_found(client, seed):
    eid = _entity(seed, world=seed.world_b, name="Elsewhere", image_url="/uploads/x.png")
    _gm(client, seed)
    assert _show(client, kind="entity", entity_id=eid).status_code == 404
    assert _show(client, kind="entity", entity_id=999999).status_code == 404


# ── worlds are separate stages ───────────────────────────────────────────────

def test_each_world_has_its_own_stage(client, seed):
    _gm(client, seed)
    _show(client, kind="image", url="/uploads/a.png")
    _gm(client, seed, world=seed.world_b)
    assert client.get("/api/display/state").json()["current"] is None
    _show(client, kind="image", url="/uploads/b.png")
    _gm(client, seed)
    assert client.get("/api/display/state").json()["current"]["url"] == "/uploads/a.png"


# ── the SSE stream ───────────────────────────────────────────────────────────

def test_the_stream_pushes_state_when_it_changes():
    async def go():
        gen = display_router.stream_stage(77, poll=0.01, beat=1000)
        first = await gen.__anext__()  # retry hint
        assert first.startswith("retry:")
        initial = await gen.__anext__()
        assert initial.startswith("event: state") and json.loads(initial.split("data: ", 1)[1])["current"] is None
        display_router.set_stage(77, {"kind": "image", "url": "/uploads/a.png", "title": "A"})
        nxt = await asyncio.wait_for(gen.__anext__(), 2)
        assert json.loads(nxt.split("data: ", 1)[1])["current"]["url"] == "/uploads/a.png"
        await gen.aclose()
    asyncio.run(go())


# ── the display page & the GM controls ───────────────────────────────────────

def test_display_page_renders_for_the_gm_and_is_standalone(client, seed):
    _gm(client, seed)
    r = client.get("/display")
    assert r.status_code == 200
    assert "/api/display/stream" in r.text and "/api/display/state" in r.text and "BroadcastChannel" in r.text
    assert "requestFullscreen" in r.text
    assert 'id="nd-nav"' not in r.text and "nav-kinds" not in r.text, "no site chrome on the screen the table sees"


def test_gm_pages_load_the_stage_script_and_players_do_not(client, seed):
    _gm(client, seed)
    assert "/static/js/nd-stage.js" in client.get("/").text
    client.post("/logout")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "nd-stage.js" not in client.get("/").text


def test_entity_page_has_a_show_button_for_the_gm_only(client, seed):
    eid = _entity(seed, name="Old Salt", image_url="/uploads/salt.png")
    _gm(client, seed)
    assert f"ndStage.showEntity({eid}" in client.get(f"/entity/{eid}").text
    client.post("/logout")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/entity/{eid}")
    assert "ndStage" not in r.text


# ── regression pins for the GM-side script ───────────────────────────────────

def _js():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "static" / "js" / "nd-stage.js").read_text()


def test_the_popup_opens_inside_the_click_before_any_await():
    """A pop-up opened after `await getScreenDetails()` loses the click's user activation and is
    blocked in Chrome; it must open first and be moved to the second monitor afterwards."""
    body = _js().split("function openDisplay()", 1)[1].split("// ── the floating control", 1)[0]
    assert "async" not in _js().split("function openDisplay()", 1)[0].rsplit("\n", 1)[-1]
    assert body.index("window.open(") < body.index("getScreenDetails"), "open first, then ask where monitor 2 is"
    assert "moveTo(" in body and "resizeTo(" in body


def test_floating_buttons_have_stable_ids_and_the_topbar_is_the_only_header_excluded():
    js = _js()
    for ident in ("nd-stage-fab", "nd-stage-panel", "nd-stage-imgbtn", "nd-stage-selbtn", "nd-stage-lightbtn"):
        assert ident in js, ident
    assert "header.topbar" in js and "nav, .topbar, header," not in js, \
        "an entity page's own <header class=detail-header> image must still get the button"


def test_the_display_page_pins_its_world_so_a_world_switch_elsewhere_cannot_change_it(client, seed):
    _gm(client, seed)
    html = client.get("/display").text
    assert "'?w=' + encodeURIComponent(SLUG)" in html and f'"{seed.world_a.slug}"' in html
