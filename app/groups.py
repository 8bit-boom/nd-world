"""Player groups: separate parties of players inside one world.

The GM makes groups ("Tuesday table", "The new party") and puts each player in one (WorldMembership.group_id). An entity can then be limited to some groups
(`visibility_mode = groups`): Entity.group_ids_json holds the group ids, visible_to_players is False, and entity_player_access holds the players of those groups -
so every place that already honours "visible to specific players" (lists, search, the entity page, links, the AI's lookups, MCP, export ...) applies the
limit with no change of its own. This module keeps that access in step whenever a group's members change or a group goes away.
rust/nd-gateway/src/groups.rs does the same for the gateway; the two are compared in tests/test_player_groups.py."""
import json
from typing import Iterable, List

from sqlalchemy.orm import Session

from .models import Entity, PlayerGroup, WorldMembership, entity_player_access

MAX_GROUPS_PER_WORLD = 50
MAX_NAME = 120


def world_groups(db: Session, world_id: int) -> List[PlayerGroup]:
    return db.query(PlayerGroup).filter(PlayerGroup.world_id == world_id).order_by(PlayerGroup.name, PlayerGroup.id).all()


def parse_ids(raw) -> List[int]:
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [i for i in v if isinstance(i, int) and not isinstance(i, bool)] if isinstance(v, list) else []


def valid_group_ids(db: Session, world_id: int, ids: Iterable[int]) -> List[int]:
    """`ids` that are groups of this world, once each, in id order."""
    have = {g.id for g in db.query(PlayerGroup.id).filter(PlayerGroup.world_id == world_id).all()}
    return sorted({int(i) for i in ids if int(i) in have})


def member_ids(db: Session, world_id: int, group_ids: Iterable[int]) -> List[int]:
    """The users who play in any of these groups."""
    gids = list(group_ids)
    if not gids:
        return []
    rows = db.query(WorldMembership.user_id).filter(WorldMembership.world_id == world_id, WorldMembership.group_id.in_(gids)).all()
    return sorted({r[0] for r in rows})


def apply_to_entity(db: Session, entity: Entity, group_ids: Iterable[int]) -> None:
    """Limit `entity` to these groups (already validated); no groups = GM only. Does not commit."""
    gids = sorted({int(i) for i in group_ids})
    entity.group_ids_json = json.dumps(gids) if gids else ""
    entity.visible_to_players = False
    db.execute(entity_player_access.delete().where(entity_player_access.c.entity_id == entity.id))
    for uid in member_ids(db, entity.world_id, gids):
        db.execute(entity_player_access.insert().values(entity_id=entity.id, user_id=uid))


def resync(db: Session, world_id: int) -> int:
    """After a member changed group or a group was deleted: bring every group-limited entity of the world back in step. Commits."""
    n = 0
    for e in db.query(Entity).filter(Entity.world_id == world_id, Entity.group_ids_json.isnot(None), Entity.group_ids_json != "").all():
        apply_to_entity(db, e, valid_group_ids(db, world_id, parse_ids(e.group_ids_json)))
        n += 1
    db.commit()
    return n
