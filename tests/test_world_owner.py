"""The World Owner role (WorldMembership.role == "owner"): a third auth
tier above GM-Assistant. An Owner gets everything an Assistant gets (content
creation/editing, folded into _is_assistant_safe's own check) PLUS the world
ADMINISTRATION an Assistant is denied — settings, invites, members, private
GM notes, export/import, deletion — but ONLY for the one world they own,
never any other world, and never instance-wide Settings. Table-driven for
the pure allowlist like test_gm_assistant.py, plus integration tests for the
parts that need real DB state: cross-world isolation, and that granting
"owner" itself stays GM-only even to another Owner.
"""
from app.database import SessionLocal
from app.main import _is_owner_safe
from app.models import WorldMembership

from .conftest import GM_PASSWORD, PLAYER_PASSWORD, login


# ── The allowlist itself ─────────────────────────────────────────────────────

# (method, path, world_id_the_caller_owns, expected_owner_safe)
CASES = [
    # Administration an Owner DOES get — for their own world (id 1).
    ("GET", "/worlds/1/edit", 1, True),
    ("POST", "/worlds/1/edit", 1, True),
    ("POST", "/worlds/1/theme/import", 1, True),
    ("POST", "/worlds/1/theme/clear", 1, True),
    ("POST", "/worlds/1/invites/new", 1, True),
    ("POST", "/worlds/1/invites/9/revoke", 1, True),
    ("POST", "/worlds/1/members/2/remove", 1, True),
    ("POST", "/worlds/1/members/2/role", 1, True),
    ("POST", "/worlds/1/members/2/reset-password", 1, True),
    ("POST", "/worlds/1/members/2/clear-2fa", 1, True),
    ("GET", "/worlds/1/notes/2", 1, True),
    ("POST", "/worlds/1/notes/2/new", 1, True),
    ("POST", "/worlds/1/notes/2/9/delete", 1, True),
    ("GET", "/worlds/1/export", 1, True),
    ("POST", "/worlds/1/import", 1, True),
    ("GET", "/worlds/1/rules/edit", 1, True),
    ("POST", "/worlds/1/rules/edit", 1, True),
    ("POST", "/worlds/1/rules/import", 1, True),
    ("POST", "/worlds/1/rules/overlay/suggest", 1, True),
    ("POST", "/worlds/1/delete", 1, True),
    ("POST", "/worlds/1/ai-instructions/import", 1, True),
    ("POST", "/worlds/1/ai-instructions/9/toggle", 1, True),
    ("POST", "/worlds/1/ai-instructions/9/toggle-players", 1, True),
    ("POST", "/worlds/1/ai-instructions/9/delete", 1, True),
    # The exact same paths against a DIFFERENT world_id (2) must stay
    # blocked — an Owner of world 1 has no power over world 2 at all. This
    # is the core isolation guarantee: _is_owner_safe is parameterized on
    # the caller's own owned world_id, never a bare path-pattern match.
    ("GET", "/worlds/2/edit", 1, False),
    ("POST", "/worlds/2/edit", 1, False),
    ("POST", "/worlds/2/delete", 1, False),
    ("POST", "/worlds/2/invites/new", 1, False),
    ("POST", "/worlds/2/members/2/role", 1, False),
    ("GET", "/worlds/2/export", 1, False),
    # Never granted to an Owner regardless of world_id — instance-wide and
    # cross-world surfaces stay GM-only no matter what.
    ("GET", "/worlds", 1, False),
    ("POST", "/worlds/new", 1, False),
    ("GET", "/settings", 1, False),
    ("POST", "/settings/system", 1, False),
    ("GET", "/export", 1, False),
    ("GET", "/admin/backup.zip", 1, False),
]


def test_is_owner_safe_matrix():
    for method, path, owned_world_id, expected in CASES:
        got = _is_owner_safe(method, path, owned_world_id)
        assert got == expected, f"{method} {path} (owns world {owned_world_id}): expected {expected}, got {got}"


# ── Integration: real promotion + real requests ─────────────────────────────

def _make_owner(seed, player, world):
    """Flip `player`'s membership in `world` to role="owner" directly (bypasses
    the route — these tests cover the route's own behavior separately)."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == world.id, WorldMembership.user_id == player.id
        ).first()
        m.role = "owner"
        db.commit()
    finally:
        db.close()


def _switch_world(c, slug):
    r = c.get(f"/worlds/switch/{slug}?next=/", follow_redirects=False)
    assert r.status_code == 303


def test_owner_gets_full_admin_of_own_world_but_not_others(client, seed):
    _make_owner(seed, seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _switch_world(client, seed.world_a.slug)

    # Full admin of their OWN world.
    assert client.get(f"/worlds/{seed.world_a.id}/edit").status_code == 200
    r = client.post(
        f"/worlds/{seed.world_a.id}/invites/new",
        data={"expires_days": "", "max_uses": ""}, follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get(f"/worlds/{seed.world_a.id}/export")
    assert r.status_code == 200

    # Content creation too (assistant-equivalent), same as a real GM-Assistant.
    from app.models import Entity
    r = client.post("/new", data={"kind": "character", "name": "Owner Made"}, follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal()
    try:
        ent = db.query(Entity).filter(Entity.name == "Owner Made").first()
        assert ent is not None and ent.world_id == seed.world_a.id
    finally:
        db.close()

    # But NOT world_b — no power over a world they don't own, even though
    # they're a fully-empowered Owner of world_a. active_world is still
    # world_a here, so this exercises the world_id embedded in the URL
    # path, not just "is this person an owner of something."
    assert client.get(f"/worlds/{seed.world_b.id}/edit").status_code == 403
    assert client.post(f"/worlds/{seed.world_b.id}/delete", follow_redirects=False).status_code == 403

    # Nor instance-wide Settings (the shared AI backend/system config).
    assert client.get("/settings").status_code == 403
    assert client.post("/settings/system", data={}, follow_redirects=False).status_code == 403


def test_owner_cannot_mint_a_co_owner(client, seed):
    """An Owner may promote/demote player<->assistant in their own world
    (full delegated admin power) but granting "owner" itself stays GM-only
    — the one thing "full power over their own world" deliberately
    excludes, so an Owner can't unilaterally spawn co-owners with no
    oversight from the real GM."""
    _make_owner(seed, seed.player_a, seed.world_a)
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _switch_world(client, seed.world_a.slug)

    # Ordinary role management still works for them...
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/role",
        data={"role": "assistant"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert m.role == "assistant"
        m.role = "owner"  # restore for the next assertion
        db.commit()
    finally:
        db.close()

    # ...but trying to grant "owner" to someone else 403s, even though the
    # route itself is reachable (an Owner administering their own world).
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/role",
        data={"role": "owner"}, follow_redirects=False,
    )
    assert r.status_code == 403
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert m.role == "owner"  # unchanged — was already "owner" from setup, the 403 didn't touch it
    finally:
        db.close()


def test_gm_can_grant_and_revoke_ownership(client, seed):
    login(client, seed.gm.email, GM_PASSWORD)
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/role",
        data={"role": "owner"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert m.role == "owner"
    finally:
        db.close()

    # The GM can demote them straight back to player too.
    r = client.post(
        f"/worlds/{seed.world_a.id}/members/{seed.player_a.id}/role",
        data={"role": "player"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        assert m.role == "player"
    finally:
        db.close()


def test_plain_assistant_still_cannot_reach_world_admin(client, seed):
    """Regression guard: folding "owner" into is_assistant's content check
    must not accidentally widen what a plain assistant (role=="assistant")
    can reach — they still get zero admin access."""
    db = SessionLocal()
    try:
        m = db.query(WorldMembership).filter(
            WorldMembership.world_id == seed.world_a.id, WorldMembership.user_id == seed.player_a.id
        ).first()
        m.role = "assistant"
        db.commit()
    finally:
        db.close()
    login(client, seed.player_a.email, PLAYER_PASSWORD)
    _switch_world(client, seed.world_a.slug)

    assert client.get(f"/worlds/{seed.world_a.id}/edit").status_code == 403
    assert client.post(f"/worlds/{seed.world_a.id}/invites/new", data={}, follow_redirects=False).status_code == 403
    assert client.post(f"/worlds/{seed.world_a.id}/delete", follow_redirects=False).status_code == 403
