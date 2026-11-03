"""Player groups (app/groups.py): the GM's page to make groups and put each player in one. GM-only, like every route not listed in the
player/assistant allow-lists of app.main; plain form POSTs that redirect back to /groups."""
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import groups as _groups
from ..database import get_db
from ..deps import get_world_ctx, is_gm
from ..models import Entity, PlayerGroup, User, WorldMembership
from ..templating import templates

router = APIRouter()


def _gm_world(request: Request, db: Session, active_world):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(404)
    if not is_gm(request):
        raise HTTPException(403)
    return world, worlds


@router.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = _gm_world(request, db, active_world)
    rows = (db.query(WorldMembership, User).join(User, User.id == WorldMembership.user_id)
            .filter(WorldMembership.world_id == world.id).order_by(User.display_name, WorldMembership.id).all())
    groups = _groups.world_groups(db, world.id)
    limited = {}
    for e in db.query(Entity).filter(Entity.world_id == world.id, Entity.group_ids_json.isnot(None), Entity.group_ids_json != "").all():
        for gid in _groups.parse_ids(e.group_ids_json):
            limited[gid] = limited.get(gid, 0) + 1
    return templates.TemplateResponse("groups.html", {
        "request": request, "world": world, "worlds": worlds, "groups": groups, "members": [(m, u) for m, u in rows],
        "limited": limited, "max_groups": _groups.MAX_GROUPS_PER_WORLD,
        "content": [(t, label, _counts.get(t, {})) for t, label in _groups.TABLE_LABELS.items()
                    for _counts in [_groups.content_counts(db, world.id)]],
    })


@router.post("/groups/switch")
def groups_switch(request: Request, group_id: str = Form(""), next: str = Form("/"), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """The top bar's "Playing: <group>" chooser (a GM or GM-Assistant): which group's table the pages show and new content joins; blank = every group."""
    from .. import scope as _scope
    user = getattr(request.state, "user", None)
    if not user or not (user.is_gm or getattr(request.state, "is_assistant", False)):
        raise HTTPException(403)
    world, _ = get_world_ctx(request, db, active_world)
    dest = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(dest, status_code=303)
    gid = group_id.strip()
    if gid.isdigit() and world and db.get(PlayerGroup, int(gid)) and db.get(PlayerGroup, int(gid)).world_id == world.id:
        resp.set_cookie(_scope.ACTIVE_GROUP_COOKIE, gid, max_age=31536000, path="/", samesite="lax")
    else:
        resp.delete_cookie(_scope.ACTIVE_GROUP_COOKIE, path="/")
    return resp


@router.post("/groups/new")
def groups_new(request: Request, name: str = Form(""), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    name = name.strip()[:_groups.MAX_NAME]
    if not name:
        raise HTTPException(400, "Give the group a name.")
    if db.query(PlayerGroup).filter(PlayerGroup.world_id == world.id).count() >= _groups.MAX_GROUPS_PER_WORLD:
        raise HTTPException(400, f"A world can have at most {_groups.MAX_GROUPS_PER_WORLD} groups.")
    db.add(PlayerGroup(world_id=world.id, name=name))
    db.commit()
    return RedirectResponse("/groups", status_code=303)


def _group_or_404(db: Session, world_id: int, gid: int) -> PlayerGroup:
    g = db.get(PlayerGroup, gid)
    if not g or g.world_id != world_id:
        raise HTTPException(404)
    return g


@router.post("/groups/{gid}/rename")
def groups_rename(gid: int, request: Request, name: str = Form(""), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    g = _group_or_404(db, world.id, gid)
    name = name.strip()[:_groups.MAX_NAME]
    if not name:
        raise HTTPException(400, "Give the group a name.")
    g.name = name
    db.commit()
    return RedirectResponse("/groups", status_code=303)


def _target(db: Session, world_id: int, raw: str):
    """A "move to" choice: "shared" -> None, "hidden" (or blank) -> 0, a group id of this world -> that id."""
    raw = (raw or "").strip()
    if raw == "shared":
        return None
    if raw.isdigit() and int(raw) != 0:
        _group_or_404(db, world_id, int(raw))
        return int(raw)
    return _groups.HIDDEN


@router.post("/groups/{gid}/delete")
def groups_delete(gid: int, request: Request, move_to: str = Form("hidden"), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    """Deleting a group re-files its table content (sessions, quests, characters ...) where the GM says: kept hidden from every group (the default), shared with all,
    or given to another group - never silently shown to everyone."""
    world, _ = _gm_world(request, db, active_world)
    g = _group_or_404(db, world.id, gid)
    target = _target(db, world.id, move_to)
    if target == gid:
        raise HTTPException(400, "Choose another place for this group's content.")
    _groups.move_content(db, world.id, gid, target)
    for m in db.query(WorldMembership).filter(WorldMembership.world_id == world.id, WorldMembership.group_id == gid).all():
        m.group_id = None
    db.delete(g)
    db.commit()
    _groups.resync(db, world.id)      # an entity that was limited to this group (only) is now GM-only, never open to everyone
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/move-content")
def groups_move_content(request: Request, source: str = Form(...), target: str = Form(...), table: str = Form("all"), db: Session = Depends(get_db),
                        active_world: str = Cookie(None)):
    """Re-file table content in bulk (the groups page): everything of `table` (or all) that is `source` (shared / hidden / a group) goes to `target`."""
    world, _ = _gm_world(request, db, active_world)
    if table != "all" and table not in _groups.TABLE_LABELS:
        raise HTTPException(400, "Unknown kind of content.")
    src = None if source == "shared" else (_groups.HIDDEN if source == "hidden" else (int(source) if source.isdigit() else -1))
    if isinstance(src, int) and src not in (_groups.HIDDEN, -1):
        _group_or_404(db, world.id, src)
    if src == -1:
        raise HTTPException(400, "Unknown source.")
    tgt = _target(db, world.id, target)
    if target.strip() == "":
        raise HTTPException(400, "Choose where the content goes.")
    if src == tgt:
        raise HTTPException(400, "Source and target are the same.")
    _groups.move_content(db, world.id, src, tgt, table)
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/members/{user_id}")
def groups_assign(user_id: int, request: Request, group_id: str = Form(""), db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = _gm_world(request, db, active_world)
    m = db.query(WorldMembership).filter(WorldMembership.world_id == world.id, WorldMembership.user_id == user_id).first()
    if not m:
        raise HTTPException(404)
    gid = None
    if group_id.strip():
        if not group_id.strip().isdigit():
            raise HTTPException(400, "Unknown group.")
        gid = int(group_id.strip())
        _group_or_404(db, world.id, gid)
    m.group_id = gid
    _groups.follow_member(db, world.id, user_id, gid)
    db.commit()
    _groups.resync(db, world.id)
    return RedirectResponse("/groups", status_code=303)
