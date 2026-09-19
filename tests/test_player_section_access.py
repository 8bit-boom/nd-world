"""Per-world, per-role (player/assistant) None/Read/Edit access for
World.section_access_json (app/models.py) — deps.world_section_access/
world_section_level/world_can_view_section/world_can_edit_section/
world_can_edit_row/sanitize_section_access/section_permission_ids
(app/deps.py), the nav_menus.py "player_section" override, and the
per-section Players/Assistants selects on Settings -> Navigation.

This matrix now covers every nav catalog section, not just five: the
original Maps/Calendar/Quests/Parties/Tables ("Tier A" — real per-row
ownership, a player's own "edit" reaches only rows they created), every
GM-tool/content section from Boards through Pages ("Tier B" — an assistant
keeps its existing blanket can_edit_content access by default via
_ASSISTANT_EDIT_DEFAULT_SECTIONS, a player never gets past "read" via
_NO_PLAYER_EDIT_SECTIONS), the GM-admin/reference pages from Combat
Tracker through Code Assist ("Tier C" — _NO_ASSISTANT_EDIT_SECTIONS floors
BOTH roles to at most "read"; actual editing there, e.g. rules_md or
backups, stays exactly as GM-only as it always was, untouched by this
matrix), and one dynamic "kind_<kind>" id per entity kind (built-in or
GM-custom, from section_permission_ids/effective_kinds) gating that kind's
/kind/{kind} list page and /entity/{id} detail page — additively, on top
of each entity's own visible_to_players flag, not instead of it.

Every section's default level is chosen to exactly preserve whatever that
section's behavior was before it joined this matrix (see
deps._default_section_levels) — folding a section in changes nothing for
an existing world until a GM explicitly touches a Settings > Navigation
select.

Covers Maps (players: None/Read only — nothing lets a player create/modify
map content), Quests and Random Tables (own-row create/edit/delete for a
player with "edit"; full CRUD for an assistant with "edit"), Calendar
(assistants: full management + events; a player's own "edit" only reaches
events they created — not config/advance/icons), and Parties (no owner
column — a player's "edit" only reaches notes/loot on a party they're
already a member of, never create/delete/membership).

Successor to the previous boolean, players-only, read-only
player_section_access_json model (see git history for those tests)."""
import json

import app.database as database_module
from app.database import SessionLocal
from app.models import CalendarEvent, Party, PlayerCharacter, Quest, RandomTable, World, WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


def _set_access(world_id, **section_levels):
    """_set_access(world_id, calendar={"player": "edit"}, quests={"assistant": "none"})
    merges the given per-section level dicts into World.section_access_json,
    leaving every other section (and role not mentioned) at its default."""
    from app.deps import world_section_access
    db = SessionLocal()
    try:
        w = db.get(World, world_id)
        current = world_section_access(w)
        for sid, levels in section_levels.items():
            current[sid].update(levels)
        w.section_access_json = json.dumps(current)
        db.commit()
    finally:
        db.close()


def _add_quest(world_id, **kw):
    db = SessionLocal()
    try:
        q = Quest(
            world_id=world_id, title=kw.get("title", "Q"), status=kw.get("status", "active"),
            category=kw.get("category", "main"), summary=kw.get("summary", ""), body=kw.get("body", ""),
            created_by_user_id=kw.get("created_by_user_id"),
        )
        db.add(q); db.commit(); db.refresh(q)
        return q.id
    finally:
        db.close()


def _add_party(world_id, **kw):
    db = SessionLocal()
    try:
        p = Party(
            world_id=world_id, name=kw.get("name", "P"), notes=kw.get("notes", ""),
            member_pc_ids_json=json.dumps(kw.get("member_pc_ids", [])),
        )
        db.add(p); db.commit(); db.refresh(p)
        return p.id
    finally:
        db.close()


def _add_table(world_id, **kw):
    db = SessionLocal()
    try:
        t = RandomTable(
            world_id=world_id, name=kw.get("name", "T"), slug=kw.get("slug", "t"),
            entries_json=json.dumps(kw.get("entries", [{"label": "A", "weight": 1}])),
            created_by_user_id=kw.get("created_by_user_id"),
        )
        db.add(t); db.commit(); db.refresh(t)
        return t.id
    finally:
        db.close()


def _add_pc(world_id, owner_user_id, name="Hero"):
    db = SessionLocal()
    try:
        pc = PlayerCharacter(world_id=world_id, owner_user_id=owner_user_id, name=name)
        db.add(pc); db.commit(); db.refresh(pc)
        return pc.id
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


# ── deps helpers ─────────────────────────────────────────────────────────────

def test_default_world_section_access():
    from app.deps import world_section_access
    access = world_section_access(World())
    assert access["maps"] == {"player": "read", "assistant": "edit"}
    for sid in ("calendar", "quests", "parties", "tables"):
        assert access[sid] == {"player": "none", "assistant": "edit"}
    assert world_section_access(None) == access


def test_malformed_json_falls_back_to_defaults():
    from app.deps import world_section_access
    w = World(section_access_json="not json")
    assert world_section_access(w)["maps"]["player"] == "read"
    w2 = World(section_access_json='"a string, not a dict"')
    assert world_section_access(w2)["maps"]["player"] == "read"


def test_unknown_section_and_invalid_level_fall_back_independently():
    from app.deps import world_section_access
    w = World(section_access_json=json.dumps({
        "maps": {"player": "bogus", "assistant": "edit"},
        "not-a-real-section": {"player": "edit"},
        "calendar": {"player": "read"},  # assistant missing -> its own default
    }))
    access = world_section_access(w)
    assert access["maps"] == {"player": "read", "assistant": "edit"}  # bogus floored to default
    assert "not-a-real-section" not in access
    assert access["calendar"] == {"player": "read", "assistant": "edit"}


def test_maps_player_edit_is_floored_to_read():
    from app.deps import world_section_access
    w = World(section_access_json=json.dumps({"maps": {"player": "edit", "assistant": "edit"}}))
    assert world_section_access(w)["maps"]["player"] == "read"


def test_sanitize_section_access_denies_invalid_values_and_floors_maps():
    from app.deps import sanitize_section_access
    out = json.loads(sanitize_section_access(json.dumps({
        "quests": {"player": "edit", "assistant": "bogus"},
        "maps": {"player": "edit", "assistant": "edit"},
    })))
    assert out["quests"] == {"player": "edit", "assistant": "none"}
    assert out["maps"] == {"player": "read", "assistant": "edit"}
    assert out["calendar"] == {"player": "none", "assistant": "none"}  # missing entirely -> deny


def test_sanitize_section_access_handles_garbage_input():
    from app.deps import sanitize_section_access
    out = json.loads(sanitize_section_access("not json at all"))
    for sid in ("maps", "calendar", "quests", "parties", "tables"):
        assert out[sid] == {"player": "none", "assistant": "none"}


# ── Migration heal ────────────────────────────────────────────────────────────

_SCRATCH_WORLDS_TABLE_SQL = (
    "CREATE TABLE worlds (id INTEGER PRIMARY KEY, name VARCHAR(256) NOT NULL, "
    "slug VARCHAR(64) UNIQUE NOT NULL, description VARCHAR(512), accent VARCHAR(16), "
    "players_see_party BOOLEAN, rules_md TEXT, home_welcome_md TEXT, "
    "home_sections_json TEXT, custom_kinds_json TEXT, created_at DATETIME{extra})"
)


def test_heals_fresh_worlds_schema_with_default_section_access(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "fresh_section_access.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocalScratch = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with engine.begin() as conn:
        conn.execute(text(_SCRATCH_WORLDS_TABLE_SQL.format(extra="")))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json) "
            "VALUES (1, 'Fresh World', 'fresh-world', '[]', '[]')"
        ))
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocalScratch)
    database_module.init_db()

    with engine.begin() as conn:
        world_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()}
    assert "section_access_json" in world_cols
    assert "player_section_access_json" not in world_cols  # never added to a fresh install

    db = SessionLocalScratch()
    try:
        from app.deps import world_section_access
        access = world_section_access(db.get(World, 1))
        assert access["maps"] == {"player": "read", "assistant": "edit"}
        assert access["quests"] == {"player": "none", "assistant": "edit"}
    finally:
        db.close()
    engine.dispose()


def test_migrates_existing_player_section_access_json_data(tmp_path, monkeypatch):
    """An install that already had the OLD column (a GM had opted players
    into some sections under the previous read-only-only model) must not
    lose that customization on upgrade — see _migrate()'s one-time
    transform in app/database.py."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "old_player_section_access.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocalScratch = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with engine.begin() as conn:
        conn.execute(text(_SCRATCH_WORLDS_TABLE_SQL.format(
            extra=", player_section_access_json TEXT DEFAULT '[\"maps\"]'"
        )))
        conn.execute(text(
            "INSERT INTO worlds (id, name, slug, home_sections_json, custom_kinds_json, player_section_access_json) "
            "VALUES (1, 'Customized World', 'customized-world', '[]', '[]', '[\"maps\", \"calendar\"]')"
        ))
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", SessionLocalScratch)
    database_module.init_db()

    db = SessionLocalScratch()
    try:
        from app.deps import world_section_access
        access = world_section_access(db.get(World, 1))
        assert access["maps"]["player"] == "read"
        assert access["calendar"]["player"] == "read"
        assert access["quests"]["player"] == "none"  # wasn't in the old list
        # Assistants keep their pre-upgrade blanket access on every section,
        # including the two (quests/parties) that never had a toggle before.
        assert access["quests"]["assistant"] == "edit"
        assert access["parties"]["assistant"] == "edit"
    finally:
        db.close()
    engine.dispose()


# ── Nav visibility ────────────────────────────────────────────────────────────

def test_gm_always_sees_toggleable_sections_in_nav_regardless_of_access(client, seed):
    _set_access(
        seed.world_a.id, maps={"assistant": "none"}, calendar={"assistant": "none"},
        quests={"assistant": "none"}, parties={"assistant": "none"}, tables={"assistant": "none"},
    )
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    for ref in ("/calendar", "/quests", "/parties", "/tables", "/maps"):
        assert f'data-ql-ref="{ref}"' in r.text


def test_player_sees_maps_by_default_but_not_other_sections(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    assert 'data-ql-ref="/maps"' in r.text
    for ref in ("/calendar", "/quests", "/parties", "/tables"):
        assert f'data-ql-ref="{ref}"' not in r.text


def test_player_gains_nav_entries_once_granted_read(client, seed):
    _set_access(
        seed.world_a.id, calendar={"player": "read"}, quests={"player": "read"},
        parties={"player": "read"}, tables={"player": "read"},
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/")
    for ref in ("/calendar", "/quests", "/parties", "/tables"):
        assert f'data-ql-ref="{ref}"' in r.text


def test_player_loses_maps_nav_entry_when_gm_sets_it_to_none(client, seed):
    _set_access(seed.world_a.id, maps={"player": "none"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert 'data-ql-ref="/maps"' not in client.get("/").text
    login(client, seed.gm.email, GM_PASSWORD)
    assert 'data-ql-ref="/maps"' in client.get("/").text


# ── Route-level gating: off by default, GM unaffected ────────────────────────

def test_player_403s_by_default_except_maps(client, seed):
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar", "/calendar/agenda", "/quests", "/parties", "/tables"):
        assert client.get(path).status_code == 403, path
    assert client.get("/maps").status_code == 200


def test_player_403s_on_maps_when_gm_sets_it_to_none(client, seed):
    _set_access(seed.world_a.id, maps={"player": "none"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/maps").status_code == 403


def test_gm_always_200s_regardless_of_access(client, seed):
    _set_access(seed.world_a.id, maps={"assistant": "none"})
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar", "/calendar/agenda", "/quests", "/parties", "/tables", "/maps"):
        assert client.get(path).status_code == 200, path


def test_player_200s_once_granted_read(client, seed):
    _set_access(
        seed.world_a.id, calendar={"player": "read"}, quests={"player": "read"},
        parties={"player": "read"}, tables={"player": "read"},
    )
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    q_id = _add_quest(seed.world_a.id)
    p_id = _add_party(seed.world_a.id)
    for path in ("/calendar", "/calendar/agenda", "/quests", f"/quests/{q_id}",
                 "/parties", f"/parties/{p_id}", "/tables", "/maps"):
        assert client.get(path).status_code == 200, path


# ── Assistant default access (edit everywhere) and per-section restriction ──

def test_assistant_gets_edit_access_by_default(client, seed):
    """Fresh-world default: assistant='edit' on every section — preserving
    pre-matrix behavior for calendar/tables (already full-CRUD via
    _is_assistant_safe) while genuinely NEWLY granting quests/parties,
    which had zero assistant access at all before this matrix existed."""
    _make_assistant(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    for path in ("/calendar", "/tables", "/quests", "/parties"):
        assert client.get(path).status_code == 200, path
    assert client.post("/quests/new", data={"title": "Assistant Quest"}, follow_redirects=False).status_code == 303
    assert client.post("/parties/new", data={"name": "Assistant Party"}, follow_redirects=False).status_code == 303


def test_assistant_edit_can_be_restricted_to_read_per_section(client, seed):
    _set_access(seed.world_a.id, quests={"assistant": "read"})
    _make_assistant(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/quests").status_code == 200
    assert client.post("/quests/new", data={"title": "x"}).status_code == 403


def test_assistant_can_be_locked_out_of_a_section_entirely(client, seed):
    _set_access(seed.world_a.id, quests={"assistant": "none"})
    _make_assistant(seed.world_a.id, seed.player_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/quests").status_code == 403


# ── Player "edit" level: own-row CRUD for Quests/Random Tables ───────────────

def test_player_with_edit_can_create_and_edit_own_quest(client, seed):
    _set_access(seed.world_a.id, quests={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/quests/new", data={"title": "My Personal Quest"}, follow_redirects=False)
    assert r.status_code == 303
    quest_id = int(r.headers["location"].rstrip("/").split("/")[-1])

    db = SessionLocal()
    try:
        assert db.get(Quest, quest_id).created_by_user_id == seed.player_a.id
    finally:
        db.close()

    assert client.post(f"/quests/{quest_id}/edit", data={"title": "Updated Title"}, follow_redirects=False).status_code == 303
    r = client.get(f"/quests/{quest_id}")
    assert "Updated Title" in r.text
    assert 'id="quest-form"' in r.text  # editor visible on their own quest

    assert client.post(f"/quests/{quest_id}/delete", follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        assert db.get(Quest, quest_id) is None
    finally:
        db.close()


def test_player_with_edit_cannot_touch_gm_authored_or_other_players_quest(client, seed):
    _set_access(seed.world_a.id, quests={"player": "edit"})
    gm_quest_id = _add_quest(seed.world_a.id, title="GM's plot")  # created_by_user_id=None
    other_quest_id = _add_quest(seed.world_a.id, title="Someone else's", created_by_user_id=seed.player_b.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/quests/{gm_quest_id}/edit", data={"title": "x"}).status_code == 403
    assert client.post(f"/quests/{gm_quest_id}/delete").status_code == 403
    assert client.post(f"/quests/{other_quest_id}/edit", data={"title": "x"}).status_code == 403
    r = client.get(f"/quests/{gm_quest_id}")
    assert r.status_code == 200
    assert 'id="quest-form"' not in r.text  # still just read-only for a quest they don't own


def test_player_without_quests_edit_cannot_create_one(client, seed):
    _set_access(seed.world_a.id, quests={"player": "read"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/quests/new").status_code == 403
    assert client.post("/quests/new", data={"title": "x"}).status_code == 403


def test_player_with_edit_can_create_edit_roll_and_delete_own_table(client, seed):
    _set_access(seed.world_a.id, tables={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/tables/new", data={
        "name": "My Table", "entries_json": json.dumps([{"label": "Sword", "weight": 1}]),
    }, follow_redirects=False)
    assert r.status_code == 303
    table_id = int(r.headers["location"].rstrip("/").split("/")[-2])  # .../{id}/edit

    db = SessionLocal()
    try:
        assert db.get(RandomTable, table_id).created_by_user_id == seed.player_a.id
    finally:
        db.close()

    assert client.get(f"/tables/{table_id}/edit").status_code == 200
    r = client.post(f"/tables/{table_id}/edit", data={
        "name": "Renamed", "entries_json": json.dumps([{"label": "Shield", "weight": 1}]),
    }, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/api/tables/{table_id}/roll")
    assert r.status_code == 200
    assert r.json()["result"] == "Shield"

    assert client.post(f"/tables/{table_id}/delete", follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        assert db.get(RandomTable, table_id) is None
    finally:
        db.close()


def test_player_with_edit_cannot_touch_gm_or_other_players_table(client, seed):
    _set_access(seed.world_a.id, tables={"player": "edit"})
    gm_table_id = _add_table(seed.world_a.id, name="GM Table", slug="gm-table")
    other_table_id = _add_table(seed.world_a.id, name="Other", slug="other-table", created_by_user_id=seed.player_b.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/tables/{gm_table_id}/edit").status_code == 404
    assert client.post(f"/tables/{gm_table_id}/edit", data={"name": "x"}).status_code == 403
    assert client.post(f"/tables/{gm_table_id}/delete").status_code == 403
    assert client.get(f"/tables/{other_table_id}/edit").status_code == 404
    # Reading/rolling isn't gated by ownership — only edit/delete are.
    assert client.post(f"/api/tables/{gm_table_id}/roll").status_code == 200


# ── Player "edit" level: own-row create for Calendar events ──────────────────

def test_player_with_edit_can_add_and_delete_own_calendar_event(client, seed):
    _set_access(seed.world_a.id, calendar={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post("/api/calendar/events", json={"day": 5, "title": "My birthday"})
    assert r.status_code == 200
    event_id = r.json()["id"]
    db = SessionLocal()
    try:
        assert db.get(CalendarEvent, event_id).created_by_user_id == seed.player_a.id
    finally:
        db.close()
    assert client.post(f"/api/calendar/events/{event_id}/delete").status_code == 200


def test_player_with_edit_cannot_delete_gm_or_other_players_event(client, seed):
    _set_access(seed.world_a.id, calendar={"player": "edit"})
    db = SessionLocal()
    try:
        ev = CalendarEvent(world_id=seed.world_a.id, day=1, title="GM's event")
        ev2 = CalendarEvent(world_id=seed.world_a.id, day=2, title="Other's event", created_by_user_id=seed.player_b.id)
        db.add_all([ev, ev2]); db.commit(); db.refresh(ev); db.refresh(ev2)
        gm_event_id, other_event_id = ev.id, ev2.id
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/calendar/events/{gm_event_id}/delete").status_code == 403
    assert client.post(f"/api/calendar/events/{other_event_id}/delete").status_code == 403


def test_player_without_calendar_edit_cannot_add_events(client, seed):
    _set_access(seed.world_a.id, calendar={"player": "read"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/api/calendar/events", json={"day": 1, "title": "x"}).status_code == 403


def test_player_with_calendar_edit_still_cannot_manage_or_add_icons(client, seed):
    """Config/advance/set-date/icons are structural — full-level only, even
    for a player granted "edit" on calendar events."""
    _set_access(seed.world_a.id, calendar={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get("/calendar/config").status_code == 403
    assert client.post("/api/calendar/advance", json={"days": 1}).status_code == 403


# ── Player "edit" level: Parties (member-based, not owner-based) ────────────

def test_party_member_player_can_edit_notes_and_loot_but_not_membership(client, seed):
    _set_access(seed.world_a.id, parties={"player": "edit"})
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id)
    party_id = _add_party(seed.world_a.id, name="The Fellowship", member_pc_ids=[pc_id])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)

    r = client.post(f"/parties/{party_id}/edit", data={"name": "Renamed!", "notes": "We are heroes"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        p = db.get(Party, party_id)
        assert p.name == "The Fellowship"  # name change ignored at member level
        assert p.notes == "We are heroes"  # notes did save
    finally:
        db.close()

    r = client.post(f"/api/parties/{party_id}/loot", json={"action": "add", "name": "Sword", "qty": 1})
    assert r.status_code == 200
    assert r.json()["loot"][0]["name"] == "Sword"

    assert client.post(f"/parties/{party_id}/delete").status_code == 403
    assert client.post(f"/api/parties/{party_id}/launch-combat").status_code == 403
    assert client.post(f"/api/parties/{party_id}/location", json={"kind": "map", "slug": "x", "lat": 1, "lng": 1}).status_code == 403


def test_non_member_player_with_edit_level_cannot_touch_a_party_they_are_not_in(client, seed):
    _set_access(seed.world_a.id, parties={"player": "edit"})
    party_id = _add_party(seed.world_a.id, name="Not Yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/parties/{party_id}/edit", data={"notes": "x"}).status_code == 403
    assert client.post(f"/api/parties/{party_id}/loot", json={"action": "add", "name": "x"}).status_code == 403


def test_player_cannot_create_a_new_party_even_with_edit_level(client, seed):
    _set_access(seed.world_a.id, parties={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post("/parties/new", data={"name": "x"}).status_code == 403


# ── Random Tables: rolling stays open at "read" too ─────────────────────────

def test_player_with_read_can_roll_a_table(client, seed):
    _set_access(seed.world_a.id, tables={"player": "read"})
    t_id = _add_table(seed.world_a.id, entries=[{"label": "Sword", "weight": 1}])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/tables/{t_id}/roll")
    assert r.status_code == 200
    assert r.json()["result"] == "Sword"


def test_player_cannot_roll_a_table_without_access(client, seed):
    t_id = _add_table(seed.world_a.id)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/tables/{t_id}/roll").status_code == 404


def test_anyone_can_roll_a_builtin_table_once_tables_is_readable(client, seed):
    """world_id=None tables are global/shared — no per-world matrix to
    consult beyond the section itself being at least readable."""
    db = SessionLocal()
    try:
        t = RandomTable(world_id=None, name="Builtin", slug="builtin-loot", is_builtin=True,
                         entries_json=json.dumps([{"label": "Coin", "weight": 1}]))
        db.add(t); db.commit(); db.refresh(t)
        t_id = t.id
    finally:
        db.close()
    _set_access(seed.world_a.id, tables={"player": "read"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.post(f"/api/tables/{t_id}/roll")
    assert r.status_code == 200
    assert r.json()["result"] == "Coin"


# ── Cross-world scoping (world_row_visible / world_can_edit_row) ────────────

def test_player_cannot_view_a_quest_belonging_to_a_world_they_cant_access(client, seed):
    _set_access(seed.world_a.id, quests={"player": "read"})
    _set_access(seed.world_b.id, quests={"player": "read"})
    other_q_id = _add_quest(seed.world_b.id, title="Not yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)  # member of world_a only
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/quests/{other_q_id}").status_code == 404


def test_player_cannot_view_a_party_belonging_to_a_world_they_cant_access(client, seed):
    _set_access(seed.world_a.id, parties={"player": "read"})
    _set_access(seed.world_b.id, parties={"player": "read"})
    other_p_id = _add_party(seed.world_b.id, name="Not yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/parties/{other_p_id}").status_code == 404


def test_player_cannot_roll_a_table_belonging_to_a_world_they_cant_access(client, seed):
    _set_access(seed.world_a.id, tables={"player": "read"})
    _set_access(seed.world_b.id, tables={"player": "read"})
    other_t_id = _add_table(seed.world_b.id, slug="not-yours")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.post(f"/api/tables/{other_t_id}/roll").status_code == 404


def test_gm_can_still_view_quest_from_any_world(client, seed):
    other_q_id = _add_quest(seed.world_b.id, title="GM sees everything")
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert client.get(f"/quests/{other_q_id}").status_code == 200


# ── Templates hide GM-only controls for a read-only/member-level player ─────

def test_quest_detail_hides_edit_controls_for_read_only_player(client, seed):
    _set_access(seed.world_a.id, quests={"player": "read"})
    q_id = _add_quest(seed.world_a.id, title="Hooded Stranger", body="Find the *stranger*.")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/quests/{q_id}")
    assert r.status_code == 200
    assert 'id="quest-form"' not in r.text
    assert "Hooded Stranger" in r.text

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/quests/{q_id}")
    assert 'id="quest-form"' in r.text


def test_party_detail_shows_member_level_notes_but_not_full_controls(client, seed):
    _set_access(seed.world_a.id, parties={"player": "edit"})
    pc_id = _add_pc(seed.world_a.id, seed.player_a.id)
    p_id = _add_party(seed.world_a.id, name="The Hollow Blades", member_pc_ids=[pc_id])
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{p_id}")
    assert r.status_code == 200
    assert "Launch Combat" not in r.text
    assert "Delete Party" not in r.text
    assert "The Hollow Blades" in r.text
    assert 'name="notes"' in r.text  # notes ARE editable at member level

    login(client, seed.gm.email, GM_PASSWORD)
    r = client.get(f"/parties/{p_id}")
    assert "Launch Combat" in r.text


def test_party_detail_is_fully_read_only_for_a_non_member_player(client, seed):
    _set_access(seed.world_a.id, parties={"player": "read"})
    p_id = _add_party(seed.world_a.id, name="The Hollow Blades")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get(f"/parties/{p_id}")
    assert r.status_code == 200
    assert 'name="notes"' not in r.text


def test_calendar_hides_gm_controls_for_read_only_player(client, seed):
    _set_access(seed.world_a.id, calendar={"player": "read"})
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


def test_tables_list_hides_gm_controls_but_keeps_roll_for_read_only_player(client, seed):
    _set_access(seed.world_a.id, tables={"player": "read"})
    _add_table(seed.world_a.id, name="Loot", slug="loot-a")
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/tables")
    assert r.status_code == 200
    assert "+ New Table" not in r.text
    assert "Edit →" not in r.text
    assert "🎲 Roll" in r.text


def test_tables_list_shows_new_table_link_for_edit_level_player(client, seed):
    _set_access(seed.world_a.id, tables={"player": "edit"})
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    assert "+ New Table" in client.get("/tables").text


# ── Settings > Navigation: per-section Players/Assistants save ──────────────

def test_navigation_settings_page_renders_section_selects(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    r = client.get("/settings?tab=navigation")
    assert r.status_code == 200
    assert "section-access-json-input" in r.text
    assert "updateSectionAccess" in r.text


def test_navigation_settings_save_persists_section_access(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    payload = json.dumps({
        "maps": {"player": "read", "assistant": "edit"},
        "calendar": {"player": "edit", "assistant": "read"},
        "quests": {"player": "none", "assistant": "none"},
        "parties": {"player": "none", "assistant": "edit"},
        "tables": {"player": "read", "assistant": "edit"},
    })
    r = client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={
        "nav_menus_json": "[]",
        "section_access_json": payload,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.deps import world_section_access
    db = SessionLocal()
    try:
        access = world_section_access(db.get(World, seed.world_a.id))
    finally:
        db.close()
    assert access["calendar"] == {"player": "edit", "assistant": "read"}
    assert access["quests"] == {"player": "none", "assistant": "none"}


def test_navigation_settings_save_denies_invalid_levels_and_floors_maps(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    client.cookies.set("active_world", seed.world_a.slug)
    payload = json.dumps({
        "maps": {"player": "edit", "assistant": "edit"},  # player edit not allowed on maps
        "quests": {"player": "bogus-level", "assistant": "edit"},
    })
    r = client.post(f"/worlds/{seed.world_a.id}/nav-menus/edit", data={
        "nav_menus_json": "[]",
        "section_access_json": payload,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.deps import world_section_access
    db = SessionLocal()
    try:
        access = world_section_access(db.get(World, seed.world_a.id))
    finally:
        db.close()
    assert access["maps"]["player"] == "read"  # floored, not denied outright
    assert access["quests"]["player"] == "none"  # invalid value -> denied
