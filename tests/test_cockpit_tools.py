"""GM tools inside the cockpit: the party window's quick HP / Shock controls, a window's own zoom, pop-out, the lock and the combat
status in the status bar.

The party controls call the same routes the sheet's own steppers use (the N&D HP track, Shock, or a custom system's vital track);
what they send and what comes back is tested here, the page behaviour (instant change, queued clicks, clamping, a lock that stops
drags) was driven in Chromium against the running app."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Party, PlayerCharacter, SheetTemplate

from .conftest import GM_PASSWORD, login

ROOT = Path(__file__).resolve().parent.parent
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _login_gm(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)


# ── per-window zoom: what the server keeps ──────────────────────────────────────────────────────────────────────

def _panel(type_, data, i=1):
    return {"id": f"p{i}", "type": type_, "ref": "", "title": "x", "x": 8, "y": 8, "w": 400, "h": 300, "z": 1, "data": data}


@pytest.mark.parametrize("sent,kept", [(125, 125), (50, 50), (200, 200), (5, 50), (999, 200), (149.9, 149), ("150", 150),
                                       (100, None), ("x", None), (None, None), ([1], None), ({"a": 1}, None), (True, 50)])
def test_a_windows_zoom_is_clamped_to_50_200_and_only_stored_when_it_is_not_100(client, seed, sent, kept):
    _login_gm(client, seed)
    panels = [_panel("dice", {"zoom": sent}), _panel("notes", {"text": "hi", "zoom": sent}, 2)]
    assert client.post("/api/cockpit/workspace", json={"current": {"panels": panels}, "presets": {"A": {"panels": panels}}}).status_code == 200
    ws = client.get("/api/cockpit/workspace").json()["workspace"]
    for plist in (ws["current"]["panels"], ws["presets"]["A"]["panels"]):
        assert [p["data"].get("zoom") for p in plist] == [kept, kept]
    assert ws["current"]["panels"][1]["data"]["text"] == "hi", "the notes text is kept beside the zoom"


def test_a_json_infinity_for_the_zoom_is_not_a_server_error(client, seed):
    _login_gm(client, seed)
    body = '{"current": {"panels": [{"id": "p1", "type": "dice", "data": {"zoom": Infinity}}]}, "presets": {}}'
    r = client.post("/api/cockpit/workspace", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert "zoom" not in client.get("/api/cockpit/workspace").json()["workspace"]["current"]["panels"][0]["data"]


@needs_node
def test_zoom_steps_through_the_ladder_and_a_window_scaled_by_it_still_fills_its_frame():
    script = r"""
    global.window = global; require(process.env.LAYOUT); const L = global.ndCockpitLayout; const out = {};
    out.ladder = L.ZOOMS;
    out.up = [100, 110, 125, 150, 175, 200, 200].map((z, i, a) => L.zoomStep(z, 1));
    out.down = [100, 90, 80, 75, 67, 50, 50].map(z => L.zoomStep(z, -1));
    out.odd = [133, 49, 260, 1].map(z => [L.zoomStep(z, 1), L.zoomStep(z, -1)]);           // an unknown value goes to the next step that way
    out.box = [50, 100, 125, 150, 200, 10, 999].map(z => L.zoomBox(z));
    out.fill = [50, 67, 75, 125, 150, 200].map(z => { const b = L.zoomBox(z); return Math.abs(b.scale * b.size - 100) < 1e-9; });
    console.log(JSON.stringify(out));
    """
    env = {"PATH": os.environ["PATH"], "LAYOUT": str(ROOT / "static/js/cockpit-layout.js")}
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ladder"] == [50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200]
    assert out["up"] == [110, 125, 150, 175, 200, 200, 200]
    assert out["down"] == [90, 80, 75, 67, 50, 50, 50]
    assert out["odd"] == [[150, 125], [50, 50], [200, 200], [50, 50]]
    assert [b["scale"] for b in out["box"]] == [0.5, 1, 1.25, 1.5, 2, 0.5, 2], "out-of-range values are clamped"
    assert out["box"][0]["size"] == 200 and out["box"][4]["size"] == 50
    assert all(out["fill"]), "scale x inverse size is always 100%: the page fills the window exactly"


# ── the party window's quick controls ───────────────────────────────────────────────────────────────────────────

def _tpl_id(slug):
    db = SessionLocal()
    try:
        return db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first().id
    finally:
        db.close()


def _party_with(seed, *pcs):
    db = SessionLocal()
    try:
        members = []
        for pc in pcs:
            db.add(pc)
            db.flush()
            members.append(pc.id)
        party = Party(world_id=seed.world_a.id, name="Vanguard", member_pc_ids_json=json.dumps(members))
        db.add(party)
        db.commit()
        return party.id, members
    finally:
        db.close()


def _vitals(client, party_id):
    r = client.get(f"/api/parties/{party_id}/vitals")
    assert r.status_code == 200, r.text
    return {m["name"]: m for m in r.json()["members"]}


def test_the_party_vitals_carry_what_the_controls_need_for_both_kinds_of_character(client, seed):
    _login_gm(client, seed)
    native = PlayerCharacter(world_id=seed.world_a.id, name="Aria", current_hp=12, max_hp=20, shock_current=2, shock_max=6)
    hunter = PlayerCharacter(world_id=seed.world_a.id, name="Mirabel", current_hp=0, max_hp=0, sheet_template_id=_tpl_id("hunt-in-the-moonlight"),
                             custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 5, "stamina_max": 5}))
    party_id, _ = _party_with(seed, native, hunter)
    v = _vitals(client, party_id)
    a, m = v["Aria"], v["Mirabel"]
    assert (a["native"], a["hp"], a["max_hp"], a["shock"], a["shock_max"]) == (True, 12, 20, 2, 6)
    assert a["hp_id"] is None, "an N&D character is stepped through the HP route, not a template track"
    assert m["native"] is False and m["shock"] is None and m["shock_max"] is None, "Shock is an N&D track"
    assert m["hp_id"] and m["hp"] == 3 and m["max_hp"] == 5, "a custom system's vital track is what the HP buttons step"


def test_the_buttons_use_the_routes_the_sheets_own_steppers_use_and_the_server_clamps(client, seed):
    _login_gm(client, seed)
    native = PlayerCharacter(world_id=seed.world_a.id, name="Aria", current_hp=12, max_hp=20, shock_current=2, shock_max=6)
    hunter = PlayerCharacter(world_id=seed.world_a.id, name="Mirabel", current_hp=0, max_hp=0, sheet_template_id=_tpl_id("hunt-in-the-moonlight"),
                             custom_fields_json=json.dumps({"health_current": 3, "health_max": 5, "stamina_current": 5, "stamina_max": 5}))
    party_id, (aid, mid) = _party_with(seed, native, hunter)
    post = lambda url, body: client.post(url, json=body)
    assert post(f"/api/characters/{aid}/hp-async", {"action": "delta", "value": -5}).json()["current_hp"] == 7
    assert post(f"/api/characters/{aid}/hp-async", {"action": "delta", "value": -99}).json()["current_hp"] == 0, "never below 0"
    assert post(f"/api/characters/{aid}/hp-async", {"action": "delta", "value": 99}).json()["current_hp"] == 20, "never above the maximum"
    assert post(f"/api/characters/{aid}/shock", {"action": "delta", "value": 1}).json()["shock_current"] == 3
    assert post(f"/api/characters/{aid}/shock", {"action": "delta", "value": -99}).json()["shock_current"] == 0
    hp_id = _vitals(client, party_id)["Mirabel"]["hp_id"]
    assert post(f"/api/characters/{mid}/resource", {"field_id": hp_id, "action": "delta", "value": -2}).json()["current"] == 1
    assert post(f"/api/characters/{mid}/resource", {"field_id": hp_id, "action": "delta", "value": 99}).json() == {"field_id": hp_id, "current": 5, "max": 5}
    # and the party window's next refetch shows the truth
    v = _vitals(client, party_id)
    assert (v["Aria"]["hp"], v["Aria"]["shock"], v["Mirabel"]["hp"]) == (20, 0, 5)


def test_a_player_cannot_use_the_controls_on_someone_elses_character(client, seed):
    from .conftest import PLAYER_PASSWORD
    aria = PlayerCharacter(world_id=seed.world_a.id, name="Aria", current_hp=12, max_hp=20)
    _party_with(seed, aria)
    db = SessionLocal()
    try:
        aid = db.query(PlayerCharacter).filter(PlayerCharacter.name == "Aria").first().id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/characters/{aid}/hp-async", json={"action": "delta", "value": -5}).status_code == 403
    assert client.post(f"/api/characters/{aid}/shock", json={"action": "delta", "value": 1}).status_code == 403


# ── the page ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_party_window_has_gm_only_controls_that_queue_and_show_the_change_at_once():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "if (p.type === 'party' && !PLAYER) live.addEventListener('click', function (e) { memberClick(e, p); });" in js
    assert "(PLAYER ? '' : memberControls(m))" in js, "a player's party window has no controls"
    ctl = js.split("function memberControls(m) {", 1)[1].split("const memberQueue", 1)[0]
    for n in ("-5", "-1", "1", "5"):
        assert f"[-5, -1, 1, 5]" in ctl
    # a custom character without a vital track, or a native one without a maximum, gets no buttons
    assert "(m.native === false && !m.hp_id) || (m.native !== false && !m.max_hp)" in ctl
    assert "m.native !== false && m.shock_max" in ctl
    click = js.split("function memberClick(e, p) {", 1)[1].split("// Show the change at once", 1)[0]
    assert "/hp-async" in click and "/shock" in click and "/resource" in click and "field_id: ctl.dataset.hpId" in click
    assert "memberQueue.get(id) || Promise.resolve()" in click, "clicks are queued per character: five fast -1s are five changes in order"
    assert "nudgeHpText(card, b.dataset.act, d)" in click and click.index("nudgeHpText") < click.index("fetch(url"), "shown before the answer"
    assert "loadLive(p)" in click, "the truth replaces the optimistic number once the last change has landed"
    assert "toast('Could not change that" in click


def test_the_cockpit_page_styles_the_controls_the_zoom_badge_the_lock_and_the_status_chips(client, seed):
    _login_gm(client, seed)
    html = client.get("/cockpit").text
    for needle in (".ck-memctl", ".ck-shock", ".ck-zoom-badge", "body.ck-locked .ck-rz", 'data-role="close"', "#ck-stat-combat", "#ck-lock-chip",
                   'id="ck-lock-row"', 'id="ck-stat-combat"', 'id="ck-lock-chip"'):
        assert needle in html, needle


def test_a_locked_cockpit_stops_drags_resizes_closes_and_dock_to_edge_but_nothing_else():
    js = (ROOT / "static/js/cockpit.js").read_text()
    assert "const LOCK_KEY = 'nd_cockpit_lock';" in js
    drag = js.split("function wireDrag(handle, win, p) {", 1)[1].split("function wireResize", 1)[0]
    resize = js.split("function wireResize(handle, win, p, dir) {", 1)[1].split("// ── the active window", 1)[0]
    assert "|| locked()) return;" in drag and "|| locked()) return;" in resize
    ctx = js.split("function openCtx(p, win, x, y) {", 1)[1].split("document.addEventListener('mousedown'", 1)[0]
    assert ctx.count("if (!locked()) {") == 2, "dock-to-edge and Close are left out of the window menu while locked"
    chip = js.split("function openChipCtx(p, x, y) {", 1)[1].split("function openCtx", 1)[0]
    assert "if (!locked())" in chip
    assert "x.hidden = locked();" in js, "the Windows list's ✕"
    # per browser, not part of the layout
    assert "setPref(LOCK_KEY, locked() ? '' : '1')" in js and "applyLock();" in js


def test_pop_out_uses_the_windows_current_address_without_the_embed_flag():
    js = (ROOT / "static/js/cockpit.js").read_text()
    path = js.split("function popOutPath(p) {", 1)[1].split("function popOut(p)", 1)[0]
    assert "u.searchParams.delete('embed')" in path, "ndFloat adds its own"
    assert "loc.href !== 'about:blank'" in path and "framePath(p)" in path
    assert "'/parties/' + encodeURIComponent(p.ref)" in path and "'/quests'" in path
    pop = js.split("function popOut(p) {", 1)[1].split("// ── lock the windows", 1)[0]
    assert "ndFloat(path," in pop and "name: 'nd-cockpit-' + CK_WORLD + '-' + p.id" in pop, "a second click focuses the same pop-out"
    assert "blocked the new window" in pop


@needs_node
def test_every_window_type_keeps_the_page_address_it_always_had():
    js = (ROOT / "static/js/cockpit.js").read_text()
    body = js.split("function framePath(p) {", 1)[1].split("function fillBody(p, body) {", 1)[0]
    # the validPagePath function the page type uses
    vp = [ln for ln in js.splitlines() if ln.strip().startswith("function validPagePath(ref)")][0]
    script = vp + "\nfunction framePath(p) {" + body + "\nconst out = {};\n" + r"""
    const t = (type, ref) => framePath({type: type, ref: ref});
    out.map = t('map', 'silt docks'); out.wmap = t('wmap', 'x'); out.dice = t('dice'); out.entity = t('entity', '42'); out.ai = t('ai');
    out.gallery = t('gallery'); out.audio = t('audio'); out.video = t('video'); out.studio = t('imagestudio'); out.calendar = t('calendar');
    out.combat = t('combat', '7'); out.combat_none = t('combat', ''); out.page = t('page', '/tables'); out.page_bad = t('page', '//evil.example');
    out.none = ['party', 'quests', 'notes', 'timer', 'find', 'ai_chat', 'tables', 'ecard', 'pc'].map(x => t(x, '1'));
    console.log(JSON.stringify(out));"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["map"] == "/maps/schematic/silt%20docks/view" and out["wmap"] == "/maps/x" and out["dice"] == "/dice"
    assert out["entity"] == "/entity/42" and out["ai"] == "/ai" and out["gallery"] == "/images" and out["audio"] == "/audio"
    assert out["video"] == "/video" and out["studio"] == "/imagestudio" and out["calendar"] == "/calendar"
    assert out["combat"] == "/combat/7" and out["combat_none"] is None, "an unpicked combat window is the picker, not a page"
    assert out["page"] == "/tables" and out["page_bad"] is None
    assert out["none"] == [None] * 9, "the windows the cockpit draws itself have no page address"


def test_the_combat_status_follows_the_picked_combat_through_the_live_bus_and_is_gm_only():
    js = (ROOT / "static/js/cockpit.js").read_text()
    fn = js.split("async function refreshCombatStatus() {", 1)[1].split("const combatChip", 1)[0]
    assert "PLAYER ? null" in fn and "q.type === 'combat' && q.ref" in fn
    assert "'/api/combat/' + encodeURIComponent(cp.ref) + '/state'" in fn
    assert "d.active_id" in fn and "d.round_num" in fn      # the turn is found by id: the list is in the order added, not turn order
    # it refreshes with the other live panels, when a combat window is added / picked / closed, and on a slow timer
    live = js.split("function scheduleLiveRefresh() {", 1)[1].split("function saveNow", 1)[0]
    assert "refreshCombatStatus();" in live
    assert "combatRef !== lastCombatRef" in js and "setInterval(function () { if (!document.hidden && Date.now() - combatStatusAt > 18000)" in js
    assert js.index("let lastCombatRef = null;") < js.index("function updateStatus()"), "declared before anything can call it"


def test_the_live_panels_load_on_a_phone_too_not_just_in_the_desktop_workspace():
    """The party / quests / card / combat-picker loaders looked only inside #ck-viewport - the desktop workspace - while the phone shell
    keeps its panels in #ck-mcontent, so on a phone those tabs stayed on "Loading…" for ever."""
    js = (ROOT / "static/js/cockpit.js").read_text()
    host = js.split("function panelHost(p, inner) {", 1)[1].split("\n  }\n", 1)[0]
    assert "viewport.querySelector('.ck-win[data-pid=" in host and "document.querySelector('.ck-mpanel[data-pid=" in host
    for fn in ("async function loadLive(p) {", "async function loadCard(p) {", "async function loadCombatPicker(p) {"):
        body = js.split(fn, 1)[1].split("\n  }\n", 1)[0]
        assert "panelHost(p, " in body, fn
        assert "viewport.querySelector('.ck-win[data-pid=\"' + p.id + '\"] .ck-live')" not in body, fn
