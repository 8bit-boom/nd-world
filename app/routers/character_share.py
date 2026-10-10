"""A read-only link to ONE character, for a friend, a co-player or a forum post: anyone holding the token opens `/share/<token>`
without an account. The owner (or the GM) makes, shows and revokes it from the character page.

What the page shows is a deliberate short list - name, race / profession, level, stats and vitals, conditions, feats, gear - and
never the journal, private notes, GM notes, loot, quests, the owner's account or portrait (uploads sit behind login). The token
is the only secret: it is never logged, a revoked or unknown token gives the same 404, and the page is no-store / noindex."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import sheet_systems
from ..database import get_db
from ..models import CharacterShare, PlayerCharacter, SheetTemplate
from ..pc_stats import pc_maxima, stat_values
from ..templating import templates

router = APIRouter()

_STAT_LABELS = (("str", "Strength"), ("dex", "Dexterity"), ("bod", "Body"), ("per", "Perception"),
                ("wil", "Willpower"), ("int", "Intellect"), ("cha", "Charisma"), ("itu", "Intuition"))


def _manageable(request: Request, db: Session, pc_id: int) -> PlayerCharacter:
    """The character, if the caller owns it or is a GM - otherwise the 404 for "no such character"."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401)
    pc = db.get(PlayerCharacter, pc_id)
    if not pc or not (user.is_gm or pc.owner_user_id == user.id):
        raise HTTPException(404)
    return pc


def _active(db: Session, pc_id: int):
    return (db.query(CharacterShare).filter(CharacterShare.character_id == pc_id, CharacterShare.revoked.isnot(True))
            .order_by(CharacterShare.id.desc()).first())


def _state(request: Request, share) -> dict:
    if not share:
        return {"active": False, "url": ""}
    return {"active": True, "url": str(request.base_url).rstrip("/") + "/share/" + share.token}


@router.get("/api/characters/{pc_id}/share")
def share_state(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = _manageable(request, db, pc_id)
    return _state(request, _active(db, pc.id))


@router.post("/api/characters/{pc_id}/share")
def share_make(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Make a new link; any earlier one stops working, so "new link" is also how a leaked one is replaced."""
    from .. import char_extras
    pc = _manageable(request, db, pc_id)
    db.query(CharacterShare).filter(CharacterShare.character_id == pc.id).update({"revoked": True}, synchronize_session=False)
    share = char_extras.new_share(db, pc)
    db.commit()
    return _state(request, share)


@router.delete("/api/characters/{pc_id}/share")
def share_revoke(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = _manageable(request, db, pc_id)
    db.query(CharacterShare).filter(CharacterShare.character_id == pc.id).update({"revoked": True}, synchronize_session=False)
    db.commit()
    return _state(request, None)


def _json_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def public_view(db: Session, pc: PlayerCharacter) -> dict:
    """The few things a share link shows, as plain data."""
    m = pc_maxima(pc)
    out = {"name": pc.name, "line": " · ".join(x for x in (pc.race, pc.char_class) if x), "level": pc.level or 1,
           "native": m["native"], "stats": [], "vitals": [], "groups": [], "conditions": [], "feats": [], "gear": []}
    tpl = db.get(SheetTemplate, pc.sheet_template_id) if pc.sheet_template_id else None
    if m["native"]:
        vals = stat_values(pc)
        out["stats"] = [{"label": label, "value": vals.get(sid, 0)} for sid, label in _STAT_LABELS if sid in vals]
        out["vitals"] = [{"label": "HP", "text": f"{pc.current_hp or 0} / {m['hp']}"},
                         {"label": "Shock", "text": f"{pc.shock_current or 0} / {m['shock']}"}]
    elif tpl is not None:
        out["line"] = out["line"] or sheet_systems.system_label(tpl)
        cf = sheet_systems.parse_custom_fields(pc.custom_fields_json)
        for f in sheet_systems.template_fields(tpl):
            if isinstance(f, dict) and f.get("type") == "resource":
                text = sheet_systems.field_value_text(f, cf)
                if text:
                    out["vitals"].append({"label": f.get("label") or f.get("id"), "text": text})
        for title, fields in sheet_systems.roster_field_groups(tpl):
            rows = [{"label": f.get("label") or f.get("id"), "text": sheet_systems.field_value_text(f, cf)} for f in fields]
            rows = [r for r in rows if r["text"]]
            if rows:
                out["groups"].append({"title": title, "rows": rows})
    out["conditions"] = [str(c)[:40] for c in _json_list(pc.conditions_json)[:12] if isinstance(c, str)]
    for f in _json_list(pc.feats_json)[:80]:
        if isinstance(f, dict) and f.get("name"):
            out["feats"].append({"name": str(f["name"])[:80], "rank": str(f.get("rank") or "")[:20]})
    for e in _json_list(pc.equipment_json)[:120]:
        if isinstance(e, dict) and e.get("name"):
            out["gear"].append({"name": str(e["name"])[:80], "qty": e.get("qty") or 1, "equipped": bool(e.get("equipped"))})
    return out


@router.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str, request: Request, db: Session = Depends(get_db)):
    share = db.query(CharacterShare).filter(CharacterShare.token == token[:64], CharacterShare.revoked.isnot(True)).first()
    pc = db.get(PlayerCharacter, share.character_id) if share else None
    if not pc:
        raise HTTPException(404)
    resp = templates.TemplateResponse("characters/share.html", {"request": request, "v": public_view(db, pc)})
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp
