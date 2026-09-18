"""Per-world, per-section read-only access for players — World.
player_section_access_json (app/models.py), deps.world_player_sections/
world_can_view_section/world_row_visible (app/deps.py), the nav_menus.py
"player_section" override, and the "Player World Access" panel on the
World edit page. Covers Maps (already open before this feature, now also
toggleable), Calendar, Quests, Parties, and Random Tables — each
independently opt-in per world, defaulting to today's behavior (Maps on,
everything else off). Combat Tracker and Investigation Boards deliberately
have no toggle at all (see their own comments in app/deps.py/app/main.py)
and stay fully GM-only, unaffected by this feature.
"""
import json

import app.database as database_module
from app.database import SessionLocal
from app.models import Party, Quest, RandomTable, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_sections(world_id, sections):
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        w.player_section_access_json = json.dumps(sections)
        db.commit()
    finally:
        db.close()


def _add_quest(world_id, **kw):
    db = SessionLocal()
    try:
        q = Quest(world_id=world_id, title=kw.get("title", "Q"), status=kw.get("status", "active"),
                   category=kw.get("category", "main"), summary=kw.get("summary", ""), body=kw.get("body", ""))
        db.add(q); db.commit(); db.refresh(q)
        return q.id
    finally:
        db.close()


def _add_party(world_id, **kw):
    db = SessionLocal()
    try:
        p = Party(world_id=world_id, name=kw.get("name", "P"), notes=kw.get("notes", ""))
        db.add(p); db.commit(); db.refresh(p)
        return p.id
    finally:
        db.close()


def _add_table(world_id, **kw):
    db = SessionLocal()
    try:
        t = RandomTable(world_id=world_id, name=kw.get("name", "T"), slug=kw.get("slug", "t"),
                         entries_json=json.dumps(kw.get("entries", [{"label": "A", "weight": 1}])))
        db.add(t); db.commit(); db.refresh(t)
        return t.id
    finally:
        db.close()


def _make_assistant(world_id, user_id):
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == world_id, WorldMembership.user_id == user_id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


# ── World.player_section_access_json / deps helpers ─────────────────────────

def test_default_world_grants_only_maps():
    from app.deps import world_player_sections
    assert world_player_sections(World()) == {"maps"}
    assert world_player_sections(None) == {"maps"}


def test_malformed_json_falls_back_to_maps_only():
    from app.deps import world_player_sections
    w = World(player_section_access_json="not json")
    assert world_player_sections(w) == {"maps"}
    w2 = World(player_section_access_json='"a string, not a list"')
    assert world_player_sections(w2) == {"maps"}


def test_unknown_section_ids_are_dropped():
    from app.deps import world_player_sections
    w = World(player_section_access_json=json.dumps(["maps", "not-a-real-section"]))
    assert world_player_sections(w) == {"maps"}


# ── Migration heal ───────────────────────────────────────────────────────────

def test_heals_pre_player_section_access_worlds_schema(tmp_path, monkeypatch):
    """A worlds table predating player_section_access_json (the "Player
    World Access" toggles) must heal onto the '["maps"]' default — Maps
    was already open to every player unconditionally before this column
    existed, so this default preserves that exact behavior."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "pre_player_section_access.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocalScratch = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
            "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
            "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
            "home_sections_json TEXT, custom_kinds_json TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json) "
            "VALUES (1, 'Pre-existing World', 'pre-existing-world', '[]', '[]')"
        ))

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocalScratch)
    database_module.init_db()

    with engine.begin() as conn:
        world_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()}
    assert "player_section_access_json" in world_cols

    db = SessionLocalScratch()
    try:
        w = db.get(World, 1)
        assert w.player_section_access_json == '["maps"]'
    finally:
        db.close()

    engine.dispose()


# ── Nav visibility ───────────────────────────────────────────────────────────

def test_gm_always_sees_toggleable_sections_in_nav_regardless_of_toggle(client, seed):
    _set_sections(seed.world_a.id, [])  # nothing opened to players
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/calendar"' in r.text
    assert 'data-ql-ref="/quests"' in r.text
    assert 'data-ql-ref="/parties"' in r.text
    assert 'data-ql-ref="/tables"' in r.text
    assert 'data-ql-ref="/maps"' in r.text


def test_player_sees_maps_by_default_but_not_other_sections(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/maps"' in r.text
    assert 'data-ql-ref="/calendar"' not in r.text
    assert 'data-ql-ref="/quests"' not in r.text
    assert 'data-ql-ref="/parties"' not in r.text
    assert 'data-ql-ref="/tables"' not in r.text


def test_player_gains_nav_entries_once_opted_in(client, seed):
    _set_sections(seed.world_a.id, ["maps", "calendar", "quests", "parties", "tables"])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/calendar"' in r.text
    assert 'data-ql-ref="/quests"' in r.text
    assert 'data-ql-ref="/parties"' in r.text
    assert 'data-ql-ref="/tables"' in r.text


def test_player_loses_maps_nav_entry_when_gm_turns_it_off(client, seed):
    _set_sections(seed.world_a.id, [])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/maps"' not in r.text
    # GM keeps seeing it regardless.
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/")
    assert 'data-ql-ref="/maps"' in r.text


# ── Route-level gating: off by default ──────────────────────────────────────

def test_player_403s_on_calendar_quests_parties_tables_by_default(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar", "/calendar/agenda", "/quests", "/parties", "/tables"):
        r = client.get(path)
        assert r.status_code == 403, path


def test_player_403s_on_maps_when_gm_turns_it_off(client, seed):
    _set_sections(seed.world_a.id, [])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/maps").status_code == 403


def test_gm_always_200s_on_every_section_regardless_of_toggle(client, seed):
    _set_sections(seed.world_a.id, [])
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar", "/calendar/agenda", "/quests", "/parties", "/tables", "/maps"):
        r = client.get(path)
        assert r.status_code == 200, path


# ── Route-level gating: opted in ────────────────────────────────────────────

def test_player_200s_once_opted_in(client, seed):
    _set_sections(seed.world_a.id, ["maps", "calendar", "quests", "parties", "tables"])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    q_id = _add_quest(seed.world_a.id)
    p_id = _add_party(seed.world_a.id)
    for path in ("/calendar", "/calendar/agenda", "/quests", f"/quests/{q_id}",
                 "/parties", f"/parties/{p_id}", "/tables", "/maps"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_assistant_gets_calendar_and_tables_regardless_of_toggle(client, seed):
    """Pre-existing behavior, unaffected by this feature: an assistant
    already has full CRUD on Calendar/Random Tables via _is_assistant_safe,
    independent of the player-facing toggle."""
    _set_sections(seed.world_a.id, [])
    _make_assistant(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/calendar").status_code == 200
    assert client.get("/tables").status_code == 200


def test_assistant_gets_read_only_quests_and_parties_even_when_toggle_off(client, seed):
    """A deliberate, accepted side effect (see world_can_view_section's
    docstring): an assistant sits at a strictly more-trusted tier than a
    plain player everywhere else, so they also get read access to Quests/
    Parties once the route is reachable at all — but their WRITE routes
    stay blocked, since _is_assistant_safe was never extended to cover
    quests/parties."""
    _set_sections(seed.world_a.id, [])
    _make_assistant(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    q_id = _add_quest(seed.world_a.id)
    assert client.get("/quests").status_code == 200
    assert client.get(f"/quests/{q_id}").status_code == 200
    assert client.post("/quests/new", data={"title": "x"}).status_code == 403
    assert client.post(f"/quests/{q_id}/edit", data={"title": "y"}).status_code == 403


# ── Write routes stay GM/Assistant-only even when opted in ──────────────────

def test_opted_in_player_still_cannot_write(client, seed):
    _set_sections(seed.world_a.id, ["maps", "calendar", "quests", "parties", "tables"])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    q_id = _add_quest(seed.world_a.id)
    p_id = _add_party(seed.world_a.id)
    t_id = _add_table(seed.world_a.id)

    assert client.get("/calendar/config").status_code == 403
    assert client.post("/api/calendar/events", json={"day": 1, "title": "x"}).status_code == 403
    assert client.post("/api/calendar/advance", json={"days": 1}).status_code == 403

    assert client.get("/quests/new").status_code == 403
    assert client.post("/quests/new", data={"title": "x"}).status_code == 403
    assert client.post(f"/quests/{q_id}/edit", data={"title": "y"}).status_code == 403
    assert client.post(f"/quests/{q_id}/delete").status_code == 403

    assert client.post("/parties/new", data={"name": "x"}).status_code == 403
    assert client.post(f"/parties/{p_id}/edit", data={"name": "y"}).status_code == 403
    assert client.post(f"/parties/{p_id}/delete").status_code == 403
    assert client.post(f"/api/parties/{p_id}/loot", json={"action": "add", "name": "x"}).status_code == 403

    assert client.get("/tables/new").status_code == 403
    assert client.post("/tables/new", data={"name": "x"}).status_code == 403
    assert client.post(f"/tables/{t_id}/edit", data={"name": "y"}).status_code == 403
    assert client.post(f"/tables/{t_id}/delete").status_code == 403


# ── Random Tables: rolling is the one action opened to players ─────────────

def test_opted_in_player_can_roll_a_table(client, seed):
    _set_sections(seed.world_a.id, ["tables"])
    t_id = _add_table(seed.world_a.id, entries=[{"label": "Sword", "weight": 1}])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/tables/{t_id}/roll")
    assert r.status_code == 200
    assert r.json()["result"] == "Sword"


def test_player_cannot_roll_a_table_without_the_toggle(client, seed):
    t_id = _add_table(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/tables/{t_id}/roll").status_code == 404


def test_anyone_can_roll_a_builtin_table_once_tables_is_opted_in(client, seed):
    """world_id=None tables are global/shared — no per-world toggle to check
    beyond the section itself being opened."""
    db = SessionLocal()
    try:
        t = RandomTable(world_id=None, name="Builtin", slug="builtin-loot", is_builtin=True,
                         entries_json=json.dumps([{"label": "Coin", "weight": 1}]))
        db.add(t); db.commit(); db.refresh(t)
        t_id = t.id
    finally:
        db.close()
    _set_sections(seed.world_a.id, ["tables"])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/tables/{t_id}/roll")
    assert r.status_code == 200
    assert r.json()["result"] == "Coin"


# ── Cross-world scoping (world_row_visible) ─────────────────────────────────
# quest_detail/party_detail/table_roll all fetch by raw id with no query-level
# world filter of their own — before this feature only a GM (who can access
# every world) could ever reach them at all, so this was latent. Opening
# them to players means a player must not be able to view another world's
# quest/party, or roll another world's table, by guessing its id.

def test_player_cannot_view_a_quest_belonging_to_a_world_they_cant_access(client, seed):
    _set_sections(seed.world_a.id, ["quests"])
    _set_sections(seed.world_b.id, ["quests"])
    other_q_id = _add_quest(seed.world_b.id, title="Not yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)  # member of world_a only
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/quests/{other_q_id}").status_code == 404


def test_player_cannot_view_a_party_belonging_to_a_world_they_cant_access(client, seed):
    _set_sections(seed.world_a.id, ["parties"])
    _set_sections(seed.world_b.id, ["parties"])
    other_p_id = _add_party(seed.world_b.id, name="Not yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/parties/{other_p_id}").status_code == 404


def test_player_cannot_roll_a_table_belonging_to_a_world_they_cant_access(client, seed):
    _set_sections(seed.world_a.id, ["tables"])
    _set_sections(seed.world_b.id, ["tables"])
    other_t_id = _add_table(seed.world_b.id, slug="not-yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/tables/{other_t_id}/roll").status_code == 404


def test_gm_can_still_view_quest_from_any_world(client, seed):
    other_q_id = _add_quest(seed.world_b.id, title="GM sees everything")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/quests/{other_q_id}").status_code == 200


# ── Templates hide GM-only controls for a read-only player ──────────────────

def test_quest_detail_hides_edit_controls_for_opted_in_player(client, seed):
    _set_sections(seed.world_a.id, ["quests"])
    q_id = _add_quest(seed.world_a.id, title="Hooded Stranger", body="Find the *stranger*.")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/quests/{q_id}")
    assert r.status_code == 200
    assert 'id="quest-form"' not in r.text
    assert "Delete this quest" not in r.text
    assert "Hooded Stranger" in r.text

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/quests/{q_id}")
    assert 'id="quest-form"' in r.text


def test_party_detail_hides_edit_controls_for_opted_in_player(client, seed):
    _set_sections(seed.world_a.id, ["parties"])
    p_id = _add_party(seed.world_a.id, name="The Hollow Blades")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{p_id}")
    assert r.status_code == 200
    assert "Launch Combat" not in r.text
    assert "Delete Party" not in r.text
    assert "The Hollow Blades" in r.text

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/parties/{p_id}")
    assert "Launch Combat" in r.text


def test_calendar_hides_gm_controls_for_opted_in_player(client, seed):
    _set_sections(seed.world_a.id, ["calendar"])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/calendar")
    assert r.status_code == 200
    assert "Advance Days" not in r.text
    assert "Set as Today" not in r.text
    assert "⚙ Configure" not in r.text

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get("/calendar")
    assert "Advance Days" in r.text


def test_tables_list_hides_gm_controls_but_keeps_roll_for_opted_in_player(client, seed):
    _set_sections(seed.world_a.id, ["tables"])
    _add_table(seed.world_a.id, name="Loot", slug="loot-a")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/tables")
    assert r.status_code == 200
    assert "+ New Table" not in r.text
    assert "Edit →" not in r.text
    assert "🎲 Roll" in r.text


# ── World edit page: the "Player World Access" panel ────────────────────────

def test_world_edit_page_renders_section_checkboxes(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/worlds/{seed.world_a.id}/edit")
    assert r.status_code == 200
    for sid in ("maps", "calendar", "quests", "parties", "tables"):
        assert f'name="player_sections" value="{sid}"' in r.text
    # Combat/Boards deliberately have no toggle at all.
    assert 'value="combat"' not in r.text
    assert 'value="boards"' not in r.text


def test_world_edit_saves_selected_sections(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
        "player_sections": ["calendar", "tables"],
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.player_section_access_json) == ["calendar", "tables"]
    finally:
        db.close()


def test_world_edit_drops_unknown_section_values(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
        "player_sections": ["maps", "not-a-real-section", "<script>"],
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.player_section_access_json) == ["maps"]
    finally:
        db.close()


def test_world_edit_clears_all_sections_when_none_checked(client, seed):
    _set_sections(seed.world_a.id, ["maps", "calendar"])
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(f"/worlds/{seed.world_a.id}/edit", data={
        "name": seed.world_a.name,
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SessionLocal()
    try:
        w = db.get(World, seed.world_a.id)
        assert json.loads(w.player_section_access_json) == []
    finally:
        db.close()
