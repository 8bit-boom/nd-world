"""Who is in which party — one place that reads and rewrites the membership.

Party membership lives in a JSON id list (Party.member_pc_ids_json), so there
is no foreign key to cascade on. Four routers used to hand-roll the
"which parties contain this character" loop, and deleting or retiring a
character cleaned none of it up: the party kept a dead id (shown as a phantom
member, counted in the roster, still handed out XP/rest), loot kept it in
`claimed_by`, and a calendar event kept pointing at it.

Leaf module (models only) so characters.py can use it without the
characters <-> parties import cycle.
"""
import json

from sqlalchemy.orm import Session

from .models import CalendarEvent, Party


def member_ids(raw) -> list:
    """Parse a JSON id list column into ints, tolerating bad data."""
    try:
        ids = json.loads(raw or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


def parties_for_pc(db: Session, world_id: int, pc_id: int) -> list:
    """Parties in `world_id` that list `pc_id` as a member, name-ordered."""
    return [p for p in db.query(Party).filter(Party.world_id == world_id).order_by(Party.name).all()
            if pc_id in member_ids(p.member_pc_ids_json)]


def detach_pc(db: Session, world_id: int, pc_id: int) -> int:
    """Remove a character from every reference that would otherwise dangle:
    party member lists, loot `claimed_by` claims (the item itself stays, back
    in the unclaimed pool — the party still owns it) and calendar events tied
    to the character (kept, just no longer attached). Caller commits.
    Returns the number of parties touched."""
    touched = 0
    for party in db.query(Party).filter(Party.world_id == world_id).all():
        changed = False
        members = member_ids(party.member_pc_ids_json)
        if pc_id in members:
            party.member_pc_ids_json = json.dumps([i for i in members if i != pc_id])
            changed = True
        try:
            loot = json.loads(party.loot_json or "[]")
        except ValueError:
            loot = []
        loot_changed = False
        for item in loot if isinstance(loot, list) else []:
            if isinstance(item, dict) and pc_id in (item.get("claimed_by") or []):
                item["claimed_by"] = [i for i in item["claimed_by"] if i != pc_id]
                loot_changed = True
        if loot_changed:
            party.loot_json = json.dumps(loot)
            changed = True
        touched += changed
    db.query(CalendarEvent).filter(CalendarEvent.character_id == pc_id).update(
        {"character_id": None}, synchronize_session=False)
    return touched
