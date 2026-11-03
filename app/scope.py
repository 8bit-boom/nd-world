"""Player groups, the table layer: ONE place decides which rows of a group's table content a request may see.

A world has a shared setting (lore, rules, races, professions ...) and, on top, each player group's own table: its sessions, quests, facts, parties, characters, calendar events,
combats, boards, handouts, schematics and dice rolls. Each of those rows has `group_id`: NULL = shared with every group, a PlayerGroup id = that group's table, 0 = kept hidden
from every group. A player sees the shared rows and the rows of their own group (no group: shared only). A GM or GM-Assistant sees everything, or - after choosing "Playing: <group>"
in the top bar - the shared rows and that group's. New rows take the viewer's group (the GM's chosen one).

The limit is applied to every ORM query through one `do_orm_execute` hook (`with_loader_criteria`), so a page, an API route, an export or a feature written next year is separated
without knowing about groups. rust/nd-gateway/src/grp.rs does the same for the gateway with per-request temporary views.
"""
import contextvars
from typing import Optional

from sqlalchemy import event, or_
from sqlalchemy.orm import Session, with_loader_criteria

from .models import (CalendarEvent, CombatSession, DiceRoll, Fact, GameSession, InvestBoard, Party, PlayerCharacter, PlayerGroup, Quest, Schematic,
                     WorldHandout, WorldMembership)

SCOPED = (GameSession, Quest, Fact, Party, PlayerCharacter, CalendarEvent, CombatSession, InvestBoard, WorldHandout, Schematic, DiceRoll)
SCOPED_TABLES = tuple(m.__tablename__ for m in SCOPED)
ACTIVE_GROUP_COOKIE = "active_group"
# a GM's "Playing: <group>" never narrows these: they must see every group's rows (export, backups, the groups page itself ...)
GM_UNSCOPED_PREFIXES = ("/groups", "/export", "/api/export", "/api/backups", "/import", "/admin", "/worlds", "/settings", "/tools", "/api/worlds")


class Scope:
    """What the current request may see. `filtered`: apply the limit; `gid`: the group whose rows (plus shared ones) are visible; `world_id`: the world `gid` belongs to."""
    __slots__ = ("filtered", "gid", "world_id")

    def __init__(self, filtered: bool, gid: Optional[int], world_id: Optional[int]):
        self.filtered, self.gid, self.world_id = filtered, gid, world_id


_scope: contextvars.ContextVar = contextvars.ContextVar("nd_group_scope", default=None)


def set_scope(scope: Optional[Scope]):
    return _scope.set(scope)


def reset_scope(token) -> None:
    _scope.reset(token)


def current() -> Optional[Scope]:
    return _scope.get()


def compute(db: Session, user, is_assistant: bool, world_id: Optional[int], cookie_value: Optional[str], path: str) -> Optional[Scope]:
    """The scope of a logged-in request: `world_id` = the user's active world (None: nothing to scope)."""
    if user is None or world_id is None:
        return None
    if not user.is_gm and not is_assistant:                                       # a player: their own group (or none: shared rows only)
        m = db.query(WorldMembership.group_id).filter(WorldMembership.world_id == world_id, WorldMembership.user_id == user.id).first()
        return Scope(True, m[0] if m else None, world_id)
    # GM / assistant: unfiltered unless they chose a group of this world to play
    gid = int(cookie_value) if cookie_value and cookie_value.isdigit() else None
    if gid is None or path.startswith(GM_UNSCOPED_PREFIXES):
        return Scope(False, None, world_id) if gid is None else Scope(False, gid, world_id)
    g = db.get(PlayerGroup, gid)
    if g is None or g.world_id != world_id:
        return Scope(False, None, world_id)
    return Scope(True, gid, world_id)


def map_visible(data) -> bool:
    """Maps are JSON files (no table): a map has an optional `group_id` key with the same meaning as the table rows' (missing / null = shared)."""
    sc = _scope.get()
    if sc is None or not sc.filtered or not isinstance(data, dict):
        return True
    g = data.get("group_id")
    return g is None or (sc.gid is not None and g == sc.gid)


def new_map_group() -> Optional[int]:
    """The group a map made now joins (the creator's group), or None for a shared map."""
    sc = _scope.get()
    return sc.gid if sc is not None else None


def _criteria(gid: Optional[int]):
    def make(cls):
        return cls.group_id.is_(None) if gid is None else or_(cls.group_id.is_(None), cls.group_id == gid)
    return make


@event.listens_for(Session, "do_orm_execute")
def _limit_selects(state) -> None:
    sc = _scope.get()
    if sc is None or not sc.filtered or not state.is_select:
        return
    stmt = state.statement
    for model in SCOPED:
        stmt = stmt.options(with_loader_criteria(model, _criteria(sc.gid), include_aliases=True))
    state.statement = stmt


def _new_row_takes_the_group(mapper, connection, target) -> None:
    sc = _scope.get()
    if sc is None or sc.gid is None or getattr(target, "group_id", None) is not None:
        return
    if sc.world_id is not None and getattr(target, "world_id", None) not in (None, sc.world_id):
        return
    target.group_id = sc.gid


for _m in SCOPED:
    event.listen(_m, "before_insert", _new_row_takes_the_group)
