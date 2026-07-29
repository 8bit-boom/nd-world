"""Shared FastAPI route helpers used across main.py and every router.

Kept separate from main.py so routers (imported BY main.py) can use these
without a circular import — see app/templating.py for the same rationale.
"""
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from . import auth
from .models import World


def get_world_ctx(request: Request, db: Session, active_world: Optional[str]):
    """The active world plus the world-switcher list, filtered to what this
    viewer may access — GMs see every world, players only the ones they're a
    member of. World existence/names must not leak to non-members by ID
    enumeration, so this (not a raw `db.query(World).all()`) is what every
    handler that needs "the current world" should call.
    """
    user = getattr(request.state, "user", None)
    accessible = auth.accessible_world_ids(db, user)
    q = db.query(World)
    if accessible is not None:
        q = q.filter(World.id.in_(accessible)) if accessible else q.filter(World.id.in_([]))
    worlds = q.order_by(World.id).all()
    world = next((w for w in worlds if w.slug == active_world), None) or (worlds[0] if worlds else None)
    return world, worlds
