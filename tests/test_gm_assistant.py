"""The GM-Assistant role (WorldMembership.role == "assistant"): the second
tier of the auth_gate allowlist. _is_player_safe is the entire authorization
boundary for players; _is_assistant_safe (app/main.py) is the equivalent for
assistants — a trusted table helper who may create/edit/delete world CONTENT
while world ADMINISTRATION stays GM-only. Table-driven like
test_player_safe.py so an accidental broadening (e.g. a careless prefix match
letting /worlds/{id}/delete through) is a one-line failure here instead of a
live incident.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.deps import can_edit_content
from app.main import _is_assistant_safe
from app.models import WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── The allowlist itself ───────────────────────────────────────────────────────

# (method, path, expected_assistant_safe)
CASES = [
    # Administration stays GM-only — the assistant allowlist must never
    # widen these, they have no in-handler auth of their own.
    ("GET", "/worlds", False),
    ("POST", "/worlds/new", False),
    ("POST", "/worlds/1/delete", False),
    ("GET", "/worlds/1/edit", False),
    ("POST", "/worlds/1/edit", False),
    ("POST", "/worlds/1/import", False),
    ("POST", "/worlds/1/invites/new", False),
    ("POST", "/worlds/1/members/2/remove", False),
    ("POST", "/worlds/1/members/2/role", False),  # role management itself
    ("GET", "/settings", False),
    ("POST", "/settings/system", False),
    ("POST", "/settings/system/model-override", False),
    ("GET", "/export", False),
    ("GET", "/export/book.zip", False),
    ("GET", "/admin/backup.zip", False),
    ("GET", "/api/backups", False),
    ("GET", "/ai", False),  # the GM World Chat page
    ("POST", "/api/ai/chat", False),
    ("GET", "/api/ai/models", False),
    ("POST", "/api/ai/defaults", False),
    ("GET", "/api/ai/whisper/model-status", False),
    ("GET", "/api/ai/imagegen/status", False),
    ("POST", "/api/ai/imagegen/generate", False),
    ("POST", "/video/settings", False),  # world upload-policy preferences
    ("POST", "/api/entities/bulk-visibility", False),  # Settings > Visibility
    ("GET", "/facts", False),  # not in the plan's content list
    ("POST", "/facts/new", False),
    ("GET", "/quests", False),
    ("GET", "/parties", False),
    ("GET", "/combat", False),
    ("GET", "/races/new", False),
    ("POST", "/professions/new", False),
    ("GET", "/handouts", False),
    ("GET", "/entity-templates", False),  # structural templates, not content
    ("GET", "/characters/templates", False),
    ("POST", "/characters/1/owner", False),
    ("POST", "/characters/1/retire-to-npc", False),
    ("GET", "/worlds/1/home/edit", False),
    ("POST", "/api/worlds/1/home/quick-link", False),
    # Content — exactly what _is_assistant_safe exists to allow.
    ("GET", "/new", True),
    ("POST", "/new", True),
    ("GET", "/entity/5/edit", True),
    ("POST", "/entity/5/edit", True),
    ("POST", "/entity/5/delete", True),
    ("POST", "/entity/5/duplicate", True),
    ("POST", "/entity/5/link/6", True),
    ("POST", "/entity/5/notes/new", True),
    ("POST", "/entity/5/notes/7/delete", True),
    ("POST", "/api/upload-image", True),
    ("POST", "/api/entity/5/image", True),
    ("POST", "/kind/character/bulk-delete", True),
    ("POST", "/api/entities/bulk-folder", True),
    ("POST", "/folders/rename", True),
    ("GET", "/sessions", True),
    ("POST", "/sessions/new", True),
    ("POST", "/sessions/5/edit", True),
    ("POST", "/api/sessions/5/prep/add", True),
    ("POST", "/api/sessions/ai/summarize-from-audio", True),
    ("GET", "/api/sessions/ai/audio-jobs", True),
    ("GET", "/calendar", True),
    ("POST", "/api/calendar/events", True),
    ("POST", "/api/calendar/advance", True),
    ("GET", "/tables", True),
    ("POST", "/tables/new", True),
    ("POST", "/api/tables/5/roll", True),
    ("GET", "/tables/export", True),
    ("POST", "/tables/import", True),
    ("GET", "/boards", True),
    ("POST", "/boards/new", True),
    ("POST", "/boards/some-board/save", True),
    ("POST", "/boards/generate-orgs", True),
    ("GET", "/maps", False),  # already player-safe — _is_assistant_safe is only
    # consulted after the player-safe check fails, so it never has to answer
    # for player-tier routes (an assistant reaches /maps via that tier anyway)
    ("GET", "/maps/new", True),
    ("POST", "/maps/new", True),
    ("POST", "/maps/some-map/rename", True),
    ("POST", "/maps/some-map/delete", True),
    ("POST", "/maps/some-map/upload", True),
    ("POST", "/api/maps/some-map/overlay", True),
    ("GET", "/maps/schematic/new", True),
    ("POST", "/maps/schematic/new", True),
    ("GET", "/maps/schematic/some-slug", True),  # the GM editor canvas
    ("POST", "/maps/schematic/some-slug/elements", True),
    ("POST", "/maps/schematic/some-slug/embed-image", True),
    ("GET", "/images", True),
    ("GET", "/images/albums/1", True),
    ("POST", "/images/albums/new", True),
    ("POST", "/images/albums/1/upload", True),
    ("POST", "/images/spotlight", True),
    ("GET", "/api/gallery/browse", True),
    ("POST", "/pages/upload", True),
    ("POST", "/pages/5/edit", True),
    ("POST", "/pages/albums/new", True),
    ("POST", "/audio/upload", True),
    ("POST", "/audio/upload/chunk", True),
    ("POST", "/audio/albums/new", True),
    ("POST", "/audio/1/edit", True),
    ("GET", "/api/audio/clips", True),
    ("POST", "/video/upload", True),
    ("POST", "/video/1/edit", True),
    ("POST", "/video/albums/new", True),
    ("GET", "/background-jobs", True),
    ("GET", "/api/audio-jobs", True),
    ("POST", "/api/audio-jobs/5/cancel", True),
    ("POST", "/api/audio-jobs/5/resummarize", True),
    ("GET", "/import", True),
    ("POST", "/api/import", True),
    ("POST", "/api/import/execute", True),
    ("POST", "/api/import/convert-images", True),
    ("POST", "/api/ai/generate/entity-smart", True),
    ("POST", "/api/ai/generate/npc", True),
    ("POST", "/api/ai/entity-from-text", True),
    ("POST", "/api/ai/save-note", True),
    ("GET", "/api/ai/world-context", True),
    ("POST", "/api/ai/world-context-smart", True),
]


@pytest.mark.parametrize("method,path,expected", CASES)
def test_is_assistant_safe(method, path, expected):
    assert _is_assistant_safe(method, path) is expected, f"{method} {path} expected assistant_safe={expected}"


def test_assistant_allowlist_defaults_to_gm_only():
    """New routes are assistant-GM-only too — the allowlist must be extended
    deliberately, exactly like _is_player_safe."""
    assert _is_assistant_safe("POST", "/some/brand/new/route") is False
    assert _is_assistant_safe("GET", "/some/brand/new/route") is False


# ── Role default & migration ──────────────────────────────────────────────────

def test_membership_defaults_to_player_role(seed):
    """The standard seed creates memberships without naming a role — the
    column default must fill in "player"."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert m is not None
        assert m.role == "player"
    finally:
        db.close()


def test_membership_role_db_column_default(client, seed):
    """Bypass the ORM entirely: a raw INSERT without a role column reads back
    "player" — proving the SQLite column itself carries DEFAULT 'player'
    (what _migrate()'s ALTER TABLE gives every pre-existing install)."""
    db = SessionLocal()
    try:
        db.execute(
            text("INSERT INTO world_memberships (world_id, user_id) VALUES (:w, :u)"),
            {"w": seed.world_a.id, "u": seed.player_b.id},
        )
        db.commit()
        role = db.execute(
            text("SELECT role FROM world_memberships WHERE world_id = :w AND user_id = :u"),
            {"w": seed.world_a.id, "u": seed.player_b.id},
        ).scalar()
        assert role == "player"
    finally:
        db.close()


def test_join_flow_creates_player_role(client, seed):
    """Invite redemption joins someone as a plain player (belt-and-braces
    role="player" at the creation site in routers/auth.py)."""
    from app.models import InviteCode, User

    login(client, "gm@test.local", GM_PASSWORD)
    r = client.post("/worlds/%d/invites/new" % seed.world_a.id, data={}, follow_redirects=False)
    assert r.status_code == 303
    from app.database import SessionLocal as SL
    db = SL()
    try:
        code = db.query(InviteCode).order_by(InviteCode.id.desc()).first().code
    finally:
        db.close()

    # A brand-new account redeems the invite.
    client.get("/logout")
    r = client.post(f"/join/{code}", data={
        "mode": "signup", "email": "joined@test.local",
        "password": "joined-password-1", "display_name": "Joined",
    }, follow_redirects=False)
    assert r.status_code == 303

    db = SL()
    try:
        u = db.query(User).filter(User.email == "joined@test.local").first()
        assert u is not None
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == u.id
        ).first()
        assert m is not None
        assert m.role == "player"
    finally:
        db.close()


# ── Middleware matrix ─────────────────────────────────────────────────────────

def _make_assistant(seed, player):
    """Flip `player`'s membership in their world to role="assistant"."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()


def _switch_world(c, slug):
    """Pin the active_world cookie explicitly. The client fixture's DB also
    contains the bootstrap "neon-dragons" world (app.database._seed, id 1),
    and an absent cookie makes get_active_world fall back to the LOWEST-ID
    accessible world — for the GM that's the bootstrap world, not world_a —
    so every content-creation call in these tests pins its world first."""
    r = c.get(f"/worlds/switch/{slug}?next=/", follow_redirects=False)
    assert r.status_code == 303


def test_player_blocked_from_content_and_admin(client, seed):
    login(client, "player-a@test.local", PLAYER_PASSWORD)
    r = client.post("/new", data={"kind": "character", "name": "Sneaky"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/settings").status_code == 403
    # no entity was created
    from app.models import Entity
    db = SessionLocal()
    try:
        assert db.query(Entity).filter(Entity.name == "Sneaky").first() is None
    finally:
        db.close()


def test_assistant_can_create_content_but_not_admin(client, seed):
    _make_assistant(seed, seed.player_a)
    login(client, "player-a@test.local", PLAYER_PASSWORD)
    _switch_world(client, seed.world_a.slug)

    # Content create succeeds and really writes a row.
    r = client.post("/new", data={"kind": "character", "name": "Assistant Made"}, follow_redirects=False)
    assert r.status_code == 303
    from app.models import Entity
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.name == "Assistant Made").first()
        assert ent is not None
        assert ent.world_id == seed.world_a.id
        ent_id = ent.id
    finally:
        db.close()

    # ...but administration still 403s.
    assert client.get("/settings").status_code == 403
    assert client.get(f"/worlds/{seed.world_a.id}/edit").status_code == 403
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/role",
        data={"role": "player"}, follow_redirects=False,
    )
    assert r.status_code == 403
    assert client.get("/export").status_code == 403
    assert client.get("/ai").status_code == 403


def test_assistant_gets_edit_ui_on_player_visible_pages(client, seed):
    """can_edit renders content-creation controls on player-visible pages: the
    same entity detail page shows the GM's Edit link and note form to an
    assistant, while a plain player's render has neither."""
    login(client, "gm@test.local", GM_PASSWORD)
    _switch_world(client, seed.world_a.slug)
    r = client.post("/new", data={"kind": "character", "name": "Visible NPC"}, follow_redirects=False)
    assert r.status_code == 303
    from app.models import Entity
    db = SessionLocal()
    try:
        ent_id = db.query(Entity).filter(Entity.name == "Visible NPC").first().id
    finally:
        db.close()
    client.get("/logout")

    # Plain player first — no edit controls on the same page.
    login(client, "player-a@test.local", PLAYER_PASSWORD)
    page = client.get(f"/entity/{ent_id}")
    assert page.status_code == 200
    assert "/entity/%d/edit" % ent_id not in page.text
    assert "Add a note about this entity" not in page.text
    client.get("/logout")

    # Same page, same world, now as an assistant.
    _make_assistant(seed, seed.player_a)
    login(client, "player-a@test.local", PLAYER_PASSWORD)
    page = client.get(f"/entity/{ent_id}")
    assert page.status_code == 200
    assert "/entity/%d/edit" % ent_id in page.text
    assert "Add a note about this entity" in page.text
    # ...and the "+ New" nav button shows too (but not the Settings link).
    assert ">+ New</a>" in page.text
    assert ">⚙ Settings</a>" not in page.text


def test_assistant_visibility_stays_player_tier(client, seed):
    """An assistant may edit content but still SEES what a player sees —
    a hidden entity is a 404 for them, not readable like for the GM."""
    _make_assistant(seed, seed.player_a)
    login(client, "gm@test.local", GM_PASSWORD)
    _switch_world(client, seed.world_a.slug)
    r = client.post("/new", data={
        "kind": "character", "name": "Hidden NPC", "visibility_mode": "nobody",
    }, follow_redirects=False)
    assert r.status_code == 303
    from app.models import Entity
    db = SessionLocal()
    try:
        ent_id = db.query(Entity).filter(Entity.name == "Hidden NPC").first().id
    finally:
        db.close()
    client.get("/logout")

    login(client, "player-a@test.local", PLAYER_PASSWORD)
    assert client.get(f"/entity/{ent_id}").status_code == 404


def test_gm_still_allowed_everywhere(client, seed):
    login(client, "gm@test.local", GM_PASSWORD)
    _switch_world(client, seed.world_a.slug)
    r = client.post("/new", data={"kind": "character", "name": "GM Made"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/settings").status_code == 200
    assert client.get(f"/worlds/{seed.world_a.id}/edit").status_code == 200


# ── The role-management route ─────────────────────────────────────────────────

def test_role_route_promotes_demotes_and_validates(client, seed):
    login(client, "gm@test.local", GM_PASSWORD)

    r = client.post(
        f"/worlds/{seed.world_b.id}/members/{seed.player_b.id}/role",
        data={"role": "assistant"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id, WorldMembership.user_id == seed.player_b.id
        ).first()
        assert m.role == "assistant"
    finally:
        db.close()

    # The promotion takes effect immediately: player_b can now create content.
    client.get("/logout")
    login(client, "player-b@test.local", PLAYER_PASSWORD)
    _switch_world(client, seed.world_b.slug)
    r = client.post("/new", data={"kind": "character", "name": "B Made"}, follow_redirects=False)
    assert r.status_code == 303
    client.get("/logout")

    login(client, "gm@test.local", GM_PASSWORD)
    # Unknown role -> 400, membership untouched.
    r = client.post(
        f"/worlds/{seed.world_b.id}/members/{seed.player_b.id}/role",
        data={"role": "bogus"}, follow_redirects=False,
    )
    assert r.status_code == 400
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id, WorldMembership.user_id == seed.player_b.id
        ).first()
        assert m.role == "assistant"
    finally:
        db.close()

    # Demote back to player.
    r = client.post(
        f"/worlds/{seed.world_b.id}/members/{seed.player_b.id}/role",
        data={"role": "player"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_b.id, WorldMembership.user_id == seed.player_b.id
        ).first()
        assert m.role == "player"
    finally:
        db.close()
    client.get("/logout")

    # ...and the demotion takes effect immediately too.
    login(client, "player-b@test.local", PLAYER_PASSWORD)
    r = client.post("/new", data={"kind": "character", "name": "B Blocked"}, follow_redirects=False)
    assert r.status_code == 403


def test_role_route_404_for_non_member(client, seed):
    """player_b is not a member of world_a — there's no membership row to
    re-role, so the route 404s instead of silently creating one."""
    login(client, "gm@test.local", GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_b.id}/role",
        data={"role": "assistant"}, follow_redirects=False,
    )
    assert r.status_code == 404


# ── The shared can_edit helper ────────────────────────────────────────────────

def test_can_edit_content_helper():
    """Unit-test the Jinja-global logic against stub request.state objects —
    the same answer deps.can_edit_content gives routers and templates."""
    def req(is_gm, is_assistant=False, has_flag=True):
        state = SimpleNamespace(user=SimpleNamespace(is_gm=is_gm))
        if has_flag:
            state.is_assistant = is_assistant
        return SimpleNamespace(state=state)

    assert can_edit_content(req(is_gm=True)) is True
    assert can_edit_content(req(is_gm=False, is_assistant=True)) is True
    assert can_edit_content(req(is_gm=False, is_assistant=False)) is False
    # A GM with the flag absent/False still edits (is_gm short-circuits).
    assert can_edit_content(req(is_gm=True, is_assistant=False)) is True
    # Anonymous / flag never set (e.g. a template rendered outside a gated
    # request): False, never an AttributeError.
    assert can_edit_content(SimpleNamespace(state=SimpleNamespace(user=None))) is False
    assert can_edit_content(req(is_gm=False, has_flag=False)) is False
