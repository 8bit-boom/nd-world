import base64
import html
import io
import json
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import auth, game_catalog, live
from ..constants import (
    KIND_ICONS, KINDS, SUBTYPES, XP_THRESHOLDS,
    ND_DEFAULT_STATS, ND_DEFAULT_CURRENCY,
)
from ..database import get_db, get_app_settings
from ..deps import get_world_ctx, world_can_view_section
from ..imaging import convert_image, make_thumbnail
from ..templating import templates
from ..uploads import MAX_UPLOAD_BYTES, copy_upload_bounded, effective_upload_bytes, unique_upload_filename, save_inline_av
from ..models import CharacterSheet, Entity, ImageJob, PlayerCharacter, SheetTemplate, User, World, WorldMembership
from ..sheet_systems import (
    clean_system_spec, enrich_fields, field_value_text, parse_custom_fields, resource_tracks, sheet_pages,
    short_label, system_conditions, system_meta, system_rules_markdown, system_spec_public, template_fields,
)
from ..pc_stats import MAX_CONDITIONS, clean_condition, clean_conditions, int_field, pc_maxima
from ..party_refs import detach_pc, member_ids as _member_ids
from .. import char_extras
from .character_hub import delete_character_journal, journey_context
from pydantic import BaseModel

router = APIRouter()

UPLOADS_DIR = Path(os.environ.get("DB_PATH", "/data/world.db")).parent / "uploads"
# Raster only — see the matching note on main.py's ALLOWED_EXTS. SVG is excluded
# because it can carry <script> and portrait upload is a player-reachable path.
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _num(v, default=0.0):
    """stats_json/equipment_json used to only ever be written by JS that
    already coerced numbers (parseFloat(...)||0), so this never needed to be
    defensive. The general JSON importer can write these fields directly
    from arbitrary author-supplied JSON now, so a stray "" or missing value
    must degrade to `default` instead of throwing and 500ing every page that
    lists characters."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pc_condition_list(pc: PlayerCharacter) -> list:
    try:
        return clean_conditions(json.loads(pc.conditions_json or "[]"))
    except ValueError:
        return []


def _derived(pc: PlayerCharacter) -> dict:
    stats     = json.loads(pc.stats_json      or "[]")
    currency  = json.loads(pc.currency_json   or "[]")
    equipment = json.loads(pc.equipment_json  or "[]")
    feats     = json.loads(pc.feats_json      or "[]")
    attacks   = json.loads(pc.attacks_json    or "[]")
    cyberware = json.loads(getattr(pc, "cyberware_json",  None) or "[]")
    conditions = json.loads(getattr(pc, "conditions_json", None) or "[]")

    lvl = min(pc.level, 20)
    xp_lo = XP_THRESHOLDS[lvl - 1]
    xp_hi = XP_THRESHOLDS[lvl] if lvl < 20 else None
    if xp_hi and xp_hi > xp_lo:
        xp_pct = min(100, int(max(0, pc.xp - xp_lo) * 100 / (xp_hi - xp_lo)))
    else:
        xp_pct = 100

    total_weight = sum(
        _num(item.get("weight"), 0) * _num(item.get("qty"), 1)
        for item in equipment if isinstance(item, dict)
    )

    # N&D derived attributes
    stat_val = {
        s["id"]: int(_num(s.get("value"), 0))
        for s in stats if isinstance(s, dict) and s.get("id")
    }
    phys = (stat_val.get("str", 0) + stat_val.get("dex", 0)
            + stat_val.get("bod", 0) + stat_val.get("per", 0))
    ment = (stat_val.get("wil", 0) + stat_val.get("int", 0)
            + stat_val.get("cha", 0) + stat_val.get("itu", 0))
    hp_max_derived    = phys + 10
    shock_max_derived = ment
    ca_derived        = stat_val.get("wil", 0) + stat_val.get("bod", 0)
    speed_derived     = stat_val.get("dex", 0) + stat_val.get("itu", 0)

    # Use stored override if non-zero, else fall back to derived value
    hp_max        = pc.max_hp if pc.max_hp > 0 else hp_max_derived
    shock_max     = (getattr(pc, "shock_max", 0) or 0) if (getattr(pc, "shock_max", 0) or 0) > 0 else shock_max_derived
    shock_current = getattr(pc, "shock_current", 0) or 0
    pp_current    = getattr(pc, "pp_current",    0) or 0
    mp_current    = getattr(pc, "mp_current",    0) or 0

    secondary = {
        "name": "Shock",
        "max": shock_max,
        "current": shock_current,
    }

    return {
        "stats": stats,
        "currency": currency,
        "secondary": secondary,
        "xp_lo": xp_lo, "xp_hi": xp_hi, "xp_pct": xp_pct,
        "equipment": equipment, "feats": feats, "attacks": attacks,
        "total_weight": total_weight,
        "cyberware": cyberware,
        # Total CA used, tolerant of entries with a missing / non-numeric ca_cost
        # (imports and AI-built characters produce them) — the template's
        # sum(attribute=...) raised and 500'd the whole sheet.
        "ca_used": int(sum(_num(c.get("ca_cost"), 0) for c in cyberware if isinstance(c, dict))),
        "conditions": conditions,
        "phys": phys, "ment": ment,
        "hp_max_derived": hp_max_derived,
        "hp_max": hp_max,
        "shock_max_derived": shock_max_derived,
        "ca_derived": ca_derived,
        "speed_derived": speed_derived,
        "shock_max": shock_max,
        "shock_current": shock_current,
        "pp_current": pp_current,
        "mp_current": mp_current,
        "minor_edge": getattr(pc, "minor_edge", "") or "",
        "major_edge": getattr(pc, "major_edge", "") or "",
    }


# Scalar fields _apply_form and the character-sync JSON API (below) both write.
# Kept as one list so the two paths can't quietly drift on what's "live" for N&D
# characters — see docs/AI_ENTITY_GUIDE.md's own field-liveness audit. race_id/
# profession_id are handled separately below: unlike the rest, an omitted value
# means "keep the current one" (they're wizard-set catalog ids, not something
# every form submission necessarily repeats), not "blank it out."
_PC_LIVE_SCALAR_FIELDS = (
    "name", "player_name", "race", "char_class",
    "level", "xp", "backstory", "notes",
)
# JSON-array columns, default "[]". skills_json/attacks_json are kept for DB
# compatibility but unused in N&D — included anyway so there's exactly one field
# list to maintain rather than two overlapping ones.
_PC_LIVE_LIST_FIELDS = (
    "stats_json", "skills_json", "currency_json", "equipment_json",
    "feats_json", "attacks_json", "cyberware_json", "conditions_json",
)
# JSON-object columns, default "{}".
_PC_LIVE_DICT_FIELDS = ("custom_fields_json", "app_extra_json")


def _apply_form(pc: PlayerCharacter, data: dict, partial: bool = False):
    """Copy a submitted form / import payload onto `pc`.

    partial=False (create, importer): every field is written, an absent key
    getting its default — the row is brand new or being fully replaced.
    partial=True (the edit route): only keys actually PRESENT in `data` are
    written. The custom-sheet form posts five fields and the native form never
    posts conditions_json / app_extra_json, so default-filling on edit reset
    level to 1, zeroed XP and HP and wiped everything the form didn't carry.
    A key that IS sent but empty still clears the field."""
    def has(k):      return (not partial) or (k in data)
    def gi(k, d=0):  return int(data.get(k) or d)
    def gs(k, d=""): return str(data.get(k) or d).strip()

    for field in _PC_LIVE_SCALAR_FIELDS:
        if has(field) and field not in ("level", "xp"):
            setattr(pc, field, gs(field))
    pc.name = pc.name or "Unnamed"
    pc.race_id        = gs("race_id", pc.race_id or "")
    pc.profession_id  = gs("profession_id", pc.profession_id or "")
    if has("level"):
        pc.level = max(1, min(20, gi("level", 1)))
    if has("xp"):
        pc.xp = max(0, gi("xp"))

    # HP — max_hp=0 means "use auto-derived value"; store 0 so sheet uses derived
    if has("max_hp"):
        pc.max_hp = max(0, gi("max_hp", 0))
    if has("current_hp") and str(data.get("current_hp") or "").strip() != "":
        pc.current_hp = gi("current_hp", pc.max_hp or 0)

    # N&D resources
    for field in ("shock_max", "shock_current", "pp_current", "mp_current"):
        if has(field):
            setattr(pc, field, max(0, gi(field)))

    # Edges
    for field in ("minor_edge", "major_edge"):
        if has(field):
            setattr(pc, field, gs(field))
    pc.minor_edge_count = max(0, gi("minor_edge_count", pc.minor_edge_count or 0))
    pc.major_edge_count = max(0, gi("major_edge_count", pc.major_edge_count or 0))

    # Sheet template. Sent-but-empty means "no template"; absent (partial) means
    # "leave it" — the custom-sheet form re-posts its own id, the GM quick-edit doesn't.
    if has("sheet_template_id"):
        tpl_id = data.get("sheet_template_id")
        pc.sheet_template_id = int(tpl_id) if tpl_id and str(tpl_id).isdigit() else None

    # JSON object fields (free-form)
    for field in _PC_LIVE_DICT_FIELDS:
        if not has(field):
            continue
        raw = data.get(field, "{}") or "{}"
        try:
            json.loads(raw)
        except Exception:
            raw = "{}"
        setattr(pc, field, raw)

    # JSON array fields
    for field in _PC_LIVE_LIST_FIELDS:
        if not has(field):
            continue
        raw = data.get(field, "[]") or "[]"
        try:
            json.loads(raw)
        except Exception:
            raw = "[]"
        setattr(pc, field, raw)

    # A brand-new character left on auto-HP (max 0) or with the HP box blank
    # starts at full health, not at 0/22.
    if not partial and str(data.get("current_hp") or "").strip() == "":
        pc.current_hp = pc_maxima(pc)["hp"]

    pc.updated_at = datetime.utcnow()


def _effective_general_upload_bytes(db: Optional[Session]) -> int:
    """Portrait/entity-art uploads' size cap for this request: the GM's saved
    AppSettings.max_upload_mb (Settings > System's "Upload limits" — applies
    to new uploads immediately, no restart) or the MAX_UPLOAD_BYTES env
    default when left blank; db=None falls back to that same env default,
    matching copy_upload_bounded's own max_bytes=None behavior. See
    effective_upload_bytes (app/uploads.py)."""
    if db is None:
        return MAX_UPLOAD_BYTES
    settings = get_app_settings(db)
    return effective_upload_bytes(getattr(settings, "max_upload_mb", None), MAX_UPLOAD_BYTES)


def _upload_portrait(file: UploadFile, db: Optional[Session] = None) -> Optional[str]:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return None
    portraits_dir = UPLOADS_DIR / "portraits"
    portraits_dir.mkdir(parents=True, exist_ok=True)
    fname = unique_upload_filename(file.filename, ext)
    dest = portraits_dir / fname
    copy_upload_bounded(file, dest, max_bytes=_effective_general_upload_bytes(db))
    if db is not None:
        settings = get_app_settings(db)
        dest = convert_image(dest, static_format=settings.static_format,
                              animated_format=settings.animated_format)
    else:
        dest = convert_image(dest)
    make_thumbnail(dest)
    return f"/uploads/portraits/{dest.name}"


# ── List ──────────────────────────────────────────────────────────────────────

def _templates_for_world(db: Session, world_id: Optional[int]):
    q = db.query(SheetTemplate).filter(
        (SheetTemplate.world_id == None) |
        (SheetTemplate.world_id == world_id)
    ).order_by(SheetTemplate.is_builtin.desc(), SheetTemplate.name)
    return q.all()


def _group_by_section(tpl_fields):
    """Groups template fields by their `section` label, preserving both field
    order and first-seen section order (Jinja's groupby filter re-sorts
    alphabetically, which would scramble an intentionally-ordered sheet)."""
    sections, lookup = [], {}
    for f in tpl_fields:
        sec = f.get("section") or "Custom"
        if sec not in lookup:
            lookup[sec] = []
            sections.append((sec, lookup[sec]))
        lookup[sec].append(f)
    return sections


def _current_user(request: Request):
    return getattr(request.state, "user", None)


def _own_characters(db: Session, world_id: int, user_id: int) -> list:
    """Every character `user_id` owns in the world, oldest first."""
    return db.query(PlayerCharacter).filter(
        PlayerCharacter.world_id == world_id, PlayerCharacter.owner_user_id == user_id
    ).order_by(PlayerCharacter.id).all()


def _own_character(db: Session, world_id: int, user_id: int) -> Optional[PlayerCharacter]:
    """The player's FIRST (oldest) character — what code that predates the per-world character limit means by "the"
    character. Use _own_characters where more than one can matter."""
    mine = _own_characters(db, world_id, user_id)
    return mine[0] if mine else None


def character_limit(world) -> int:
    """How many characters one player may own in `world` (World.max_characters_per_player; 1 = the classic rule)."""
    try:
        return max(1, int(getattr(world, "max_characters_per_player", None) or 1))
    except (TypeError, ValueError):
        return 1


def _limit_reached_message(owned: int, limit: int, *, sync: bool = False) -> str:
    """The 400 text for a player who is at the limit. The limit-of-one wording is the original, kept verbatim."""
    if limit <= 1:
        return ("You already have a character in this world — use PUT .../sync to update it." if sync
                else "You already have a character in this world.")
    return (f"You already have {owned} characters in this world — the limit is {limit}. "
            + ("Use PUT .../sync to update one, or delete one" if sync else "Delete one")
            + ", or ask your GM to raise the limit.")


def _levelup_ready(pc: PlayerCharacter) -> bool:
    """True when the PC's XP has crossed the threshold for the next level
    (XP_THRESHOLDS[level] is what level+1 starts at) — the sheet and lists
    surface a level-up prompt, and POST /level-up applies it. Mirrors
    _derived's own threshold math (lvl capped at 20; a level-20 PC is done)."""
    if pc.level >= 20:
        return False
    if not pc_maxima(pc)["native"]:
        return False  # levels/XP thresholds are N&D rules; a custom system tracks its own progression
    return (pc.xp or 0) >= XP_THRESHOLDS[min(pc.level, 19)]


def _can_manage_character(user, pc: PlayerCharacter) -> bool:
    """Full edit/delete/export access: the GM, or the player who owns this character."""
    if not user or not pc:
        return False
    return user.is_gm or pc.owner_user_id == user.id


def _can_view_character(db: Session, user, pc: PlayerCharacter, world: World) -> bool:
    if _can_manage_character(user, pc):
        return True
    # `world` here is the *character's* world, so party visibility must be gated on the
    # viewer actually belonging to it — otherwise, since players_see_party defaults to
    # True, any logged-in player could read any owned sheet in any world by walking IDs.
    if not auth.user_can_access_world(db, user, world):
        return False
    # Party visibility: other players may view (read-only) party members' sheets if the GM allows it.
    return bool(world and world.players_see_party and pc.owner_user_id is not None)


@router.get("/characters", response_class=HTMLResponse)
def characters_list(request: Request, q: str = "", sort: str = "name",
                    db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world_can_view_section(request, world, "characters"):
        raise HTTPException(403)
    user = _current_user(request)
    pcs = []
    term = (q or "").strip()
    if world:
        qbase = db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id)
        if user and not user.is_gm:
            if world.players_see_party:
                qbase = qbase.filter(or_(PlayerCharacter.owner_user_id == user.id, PlayerCharacter.owner_user_id.isnot(None)))
            else:
                qbase = qbase.filter(PlayerCharacter.owner_user_id == user.id)
        if term:
            like = f"%{term}%"
            qbase = qbase.filter(or_(
                PlayerCharacter.name.ilike(like),
                PlayerCharacter.char_class.ilike(like),
                PlayerCharacter.race.ilike(like),
                PlayerCharacter.player_name.ilike(like),
            ))
        if sort == "level":
            qbase = qbase.order_by(PlayerCharacter.level.desc(), PlayerCharacter.name)
        else:
            # "hp" (wounded first) sorts in Python below: the effective max
            # is stat-derived when max_hp is 0, which SQL can't see.
            qbase = qbase.order_by(PlayerCharacter.name)
        pcs = qbase.all()
        if sort == "hp":
            def _hp_fraction(pc):
                top = pc_maxima(pc)["hp"]
                return (pc.current_hp or 0) / top if top else float("inf")  # no known max -> bottom
            pcs.sort(key=lambda pc: (_hp_fraction(pc), pc.name or ""))
    derived = {pc.id: _derived(pc) for pc in pcs}
    sheet_templates_list = _templates_for_world(db, world.id if world else None)
    custom_tpl_ids = {t.id for t in sheet_templates_list if t.sheet_mode == "custom"}
    my_characters = _own_characters(db, world.id, user.id) if (world and user and not user.is_gm) else []
    my_character = my_characters[0] if my_characters else None
    my_ids = {c.id for c in my_characters}
    limit = character_limit(world) if world else 1
    # The "+ New Character" / "Create with AI" buttons: a GM always; a player while under the world's limit.
    can_create = bool(user and (user.is_gm or len(my_characters) < limit))

    # Party badges + the player's own-party shortcut: one pass over the
    # world's parties, membership read from the JSON lists.
    from ..models import Party
    pc_party = {}
    my_party = None
    if world:
        for party in db.query(Party).filter(Party.world_id == world.id).all():
            ids = _member_ids(party.member_pc_ids_json)
            for pcid in ids:
                pc_party.setdefault(pcid, party)
            if my_ids and my_ids.intersection(ids) and my_party is None:
                my_party = party

    # Custom-system cards show their vital tracks (Health 3/5 ...) instead of
    # N&D chrome: the template's defaults apply to anything not yet saved, the
    # same as the sheet and the party strip.
    tpl_by_id = {t.id: t for t in sheet_templates_list}
    custom_tracks = {}
    for pc in pcs:
        tpl = tpl_by_id.get(pc.sheet_template_id)
        if tpl is not None and tpl.id in custom_tpl_ids:
            custom_tracks[pc.id] = resource_tracks(
                template_fields(tpl), parse_custom_fields(pc.custom_fields_json), system_meta(tpl))[:4]

    # Owner display names — GM view only (players see their own cards).
    owner_names = {}
    if world and user and user.is_gm:
        owner_ids = {pc.owner_user_id for pc in pcs if pc.owner_user_id}
        if owner_ids:
            for u_ in db.query(User).filter(User.id.in_(owner_ids)).all():
                owner_names[u_.id] = u_.display_name or u_.email

    return templates.TemplateResponse("characters/list.html", {
        "request": request, "world": world, "worlds": worlds,
        "pcs": pcs, "derived": derived,
        "sheet_templates": sheet_templates_list,
        "custom_tpl_ids": custom_tpl_ids, "custom_tracks": custom_tracks,
        "user": user, "my_character": my_character, "my_character_ids": my_ids,
        "character_limit": limit, "my_character_count": len(my_characters), "can_create_character": can_create,
        "q": term, "sort": sort,
        "pc_party": pc_party, "owner_names": owner_names, "my_party": my_party,
        "levelup_ready": {pc.id for pc in pcs if _levelup_ready(pc)},
    })


# ── New ───────────────────────────────────────────────────────────────────────

@router.get("/characters/new", response_class=HTMLResponse)
def character_new_form(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
    template_id: Optional[int] = None,
):
    world, worlds = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    user = _current_user(request)
    if user and not user.is_gm:
        mine = _own_characters(db, world.id, user.id)
        if len(mine) >= character_limit(world):
            # At the limit. With a single character that is "go straight to your sheet" (the original behaviour);
            # with several, back to the list where they all are.
            return RedirectResponse(f"/characters/{mine[0].id}" if len(mine) == 1 else "/characters", status_code=303)
    # Pre-select template if given
    chosen_tpl = db.query(SheetTemplate).filter(SheetTemplate.id == template_id).first() if template_id else None
    if not chosen_tpl:
        # Default to N&D template
        chosen_tpl = db.query(SheetTemplate).filter(SheetTemplate.slug == "nd-default").first()
    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        tpl_fields = enrich_fields(chosen_tpl)
        return templates.TemplateResponse("characters/custom_sheet.html", {
            "request": request, "world": world, "worlds": worlds,
            "pc": None, "can_manage": True,
            "chosen_template": chosen_tpl,
            # fields that mirror a character column (Hunter Name = name) aren't asked for twice
            "sections": (new_sections := _group_by_section([f for f in tpl_fields if not f.get("binds")])),
            "sheet_pages": sheet_pages(chosen_tpl, [name for name, _ in new_sections]),
            "tpl_fields": tpl_fields,
            "custom_fields": {},
        })
    return templates.TemplateResponse("characters/wizard.html", {
        "request": request, "world": world, "worlds": worlds,
        "chosen_template": chosen_tpl,
    })


@router.get("/api/characters/catalog")
def api_characters_catalog():
    """Race/profession/feat/equipment catalog for the creation wizard frontend."""
    return game_catalog.catalog_payload()


@router.post("/api/characters/upload-image")
async def character_upload_image(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Backs the shared formatting toolbar's image button on the
    backstory/notes textareas (app/templates/characters/form.html) — a
    player-reachable equivalent of main.py's /api/upload-image, since a
    player editing their own character can't call the GM-only one. Reuses
    _upload_portrait rather than a separate code path so both land in the
    same uploads/portraits/ dir under the same size/extension rules."""
    uploaded = _upload_portrait(file, db=db)
    if not uploaded:
        raise HTTPException(400, "Unsupported file type")
    return {"url": uploaded}


def _upload_media(file: UploadFile, db: Optional[Session] = None):
    """Player-reachable sibling of main.py's save_upload_media, same
    reasoning as _upload_portrait/character_upload_image above — a player
    editing their own character can't call the GM-only /api/upload-media."""
    if not file or not file.filename:
        return None, None
    ext = Path(file.filename).suffix.lower()
    if ext in ALLOWED_EXTS:
        url = _upload_portrait(file, db=db)
        return (url, "image") if url else (None, None)
    result = save_inline_av(file, UPLOADS_DIR, subdir="")
    return result if result else (None, None)


@router.post("/api/characters/upload-media")
async def character_upload_media(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Backs the shared formatting toolbar's media button/drag-drop/paste on
    the backstory/notes textareas — see api_upload_media (app/main.py) for
    the GM-only equivalent this mirrors."""
    url, kind = _upload_media(file, db=db)
    if not url:
        raise HTTPException(400, "Unsupported file type")
    return {"url": url, "kind": kind}


@router.post("/characters/new")
async def character_create(
    request: Request,
    portrait: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    if not world:
        raise HTTPException(400, "No world selected")
    user = _current_user(request)
    owner_id = None
    if user and not user.is_gm:
        owned = len(_own_characters(db, world.id, user.id))
        if owned >= character_limit(world):
            raise HTTPException(400, _limit_reached_message(owned, character_limit(world)))
        owner_id = user.id
    form = await request.form()
    data = dict(form)
    pc = PlayerCharacter(world_id=world.id, owner_user_id=owner_id)
    _apply_form(pc, data)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait, db=db)
        if url:
            pc.portrait_url = url
    db.add(pc)
    db.commit()
    db.refresh(pc)
    return RedirectResponse(f"/characters/{pc.id}", status_code=303)


# ── Sheet Templates (must come before /{pc_id} routes) ───────────────────────

RULES_MD_CAP = 20000


def _system_from_form(form, fields_raw: str) -> tuple:
    """(system_json string, rules_md) from a template form, cleaned against the submitted fields."""
    try:
        fields = json.loads(fields_raw or "[]")
    except ValueError:
        fields = []
    try:
        raw = json.loads(str(form.get("system_json", "") or "{}"))
    except ValueError:
        raw = {}
    spec, _warnings = clean_system_spec(raw, fields if isinstance(fields, list) else [])
    return json.dumps(spec), str(form.get("rules_md", "") or "").strip()[:RULES_MD_CAP]


@router.get("/characters/templates", response_class=HTMLResponse)
def template_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    tpls = _templates_for_world(db, world.id if world else None)
    return templates.TemplateResponse("characters/templates_list.html", {
        "request": request, "world": world, "worlds": worlds, "sheet_templates": tpls,
    })


@router.get("/characters/templates/new", response_class=HTMLResponse)
def template_new_form(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    return templates.TemplateResponse("characters/template_form.html", {
        "request": request, "world": world, "worlds": worlds,
        "tpl": None, "fields": [], "system": {},
    })


@router.post("/characters/templates/new")
async def template_create(
    request: Request,
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    world, _ = get_world_ctx(request, db, active_world)
    form = await request.form()
    name = str(form.get("name", "")).strip() or "Unnamed Template"
    desc = str(form.get("description", "")).strip()
    sheet_mode = "custom" if str(form.get("sheet_mode", "nd")) == "custom" else "nd"
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    base_slug = name.lower().replace(" ", "-")[:50]
    slug = base_slug
    n = 1
    while db.query(SheetTemplate).filter(SheetTemplate.slug == slug).first():
        slug = f"{base_slug}-{n}"; n += 1
    system_json, rules_md = _system_from_form(form, raw_fields)
    tpl = SheetTemplate(
        world_id=world.id if world else None,
        name=name, slug=slug, description=desc,
        is_builtin=False, sheet_mode=sheet_mode, fields_json=raw_fields,
        system_json=system_json, rules_md=rules_md,
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return RedirectResponse(f"/characters/templates/{tpl.id}/edit", status_code=303)


@router.get("/characters/templates/{tpl_id}/edit", response_class=HTMLResponse)
def template_edit_form(tpl_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    fields = json.loads(tpl.fields_json or "[]")
    return templates.TemplateResponse("characters/template_form.html", {
        "request": request, "world": world, "worlds": worlds,
        "tpl": tpl, "fields": fields, "system": system_spec_public(tpl),
    })


@router.post("/characters/templates/{tpl_id}/edit")
async def template_update(
    tpl_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    form = await request.form()
    if not tpl.is_builtin:
        tpl.name = str(form.get("name", tpl.name)).strip() or tpl.name
        tpl.description = str(form.get("description", "")).strip()
        tpl.sheet_mode = "custom" if str(form.get("sheet_mode", "nd")) == "custom" else "nd"
    raw_fields = str(form.get("fields_json", "[]") or "[]")
    try:
        json.loads(raw_fields)
    except Exception:
        raw_fields = "[]"
    tpl.fields_json = raw_fields
    tpl.system_json, tpl.rules_md = _system_from_form(form, raw_fields)
    tpl.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/characters/templates/{tpl_id}/edit?saved=1", status_code=303)


@router.post("/characters/templates/{tpl_id}/delete")
def template_delete(tpl_id: int, db: Session = Depends(get_db)):
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    if tpl.is_builtin:
        raise HTTPException(403, "Cannot delete built-in templates")
    db.delete(tpl)
    db.commit()
    return RedirectResponse("/characters/templates", status_code=303)


@router.get("/api/characters/templates")
def api_template_list(request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, _ = get_world_ctx(request, db, active_world)
    tpls = _templates_for_world(db, world.id if world else None)
    return [
        {"id": t.id, "name": t.name, "is_builtin": t.is_builtin, "sheet_mode": t.sheet_mode,
         "fields": json.loads(t.fields_json or "[]"), "system": system_spec_public(t),
         "has_rules": bool(system_rules_markdown(t))}
        for t in tpls
    ]


@router.get("/characters/templates/{tpl_id}/export.json")
def template_export(tpl_id: int, db: Session = Depends(get_db)):
    """The template as one importable JSON document — fields, the system hooks, the rules text — which
    /api/import/execute (kind=field_template, template_kind=sheet) turns back into an identical template
    in any world."""
    tpl = db.query(SheetTemplate).filter(SheetTemplate.id == tpl_id).first()
    if not tpl:
        raise HTTPException(404)
    payload = {"name": tpl.name, "description": tpl.description or "", "sheet_mode": tpl.sheet_mode,
               "fields": json.loads(tpl.fields_json or "[]"), "system": system_spec_public(tpl),
               "rules_md": (tpl.rules_md or "")}
    return StreamingResponse(
        io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_safe_export_filename(tpl.name)}.template.json"'})


# ── Sheet ─────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}", response_class=HTMLResponse)
def character_sheet(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_view_character(db, user, pc, db.query(World).filter(World.id == pc.world_id).first()):
        raise HTTPException(403)
    can_manage = _can_manage_character(user, pc)
    chosen_tpl = db.query(SheetTemplate).filter(
        SheetTemplate.id == pc.sheet_template_id
    ).first() if pc.sheet_template_id else None
    world_members = _assignable_members(db, pc.world_id) if user and user.is_gm else []
    # Player-fillable Pages character sheets linked to this PC (see
    # app/routers/character_sheets.py) — unrelated to chosen_tpl/SheetTemplate
    # above (this app's own native structured sheet), just a "see also" panel
    # for any fillable GM-uploaded HTML sheet the owner has connected here.
    linked_sheets = db.query(CharacterSheet).filter(CharacterSheet.player_character_id == pc.id).all()
    levelup_ready = _levelup_ready(pc)
    # ── Player hub context (owner only): everything ABOUT this character a
    # player manages from this page — their party (with claimable loot and
    # their own claims), the party's XP ledger, and recent sessions the
    # party played. This is the character's HOME (management/editing);
    # the Player Cockpit (its live table dashboard) is the hub's 🎛 Cockpit tab.
    # hub_mode drives the hub tabs (Journey/Quests/Notes/World/Schedule — see
    # app/routers/character_hub.py): "owner" = the viewer OWNS this character
    # (full hub, incl. the private Notes & journal tab); "gm" = a global GM
    # looking in (read-only shared tabs from the player's point of view, no Notes
    # tab, no claiming); None = everyone else gets the plain sheet.
    if user and pc.owner_user_id == user.id:
        hub_mode = "owner"
    elif user and user.is_gm:
        hub_mode = "gm"
    else:
        hub_mode = None
    hub_enabled = hub_mode is not None
    hub_owner_name = None
    if hub_mode == "gm" and pc.owner_user_id:
        owner_user = db.get(User, pc.owner_user_id)
        hub_owner_name = (owner_user.display_name or owner_user.email) if owner_user else None
    hub_ctx = (journey_context(db, request, pc, db.get(World, pc.world_id))
               if hub_enabled else {"hub_party": None, "hub_loot": [], "hub_roster": [],
                                    "hub_xp_ledger": [], "hub_sessions": []})

    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        tpl_fields = enrich_fields(chosen_tpl)
        custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
        return templates.TemplateResponse("characters/custom_sheet.html", {
            "request": request, "world": world, "worlds": worlds,
            "pc": pc, "can_manage": can_manage,
            "conditions": _pc_condition_list(pc),
            "condition_presets": system_conditions(chosen_tpl),
            "chosen_template": chosen_tpl,
            "sections": (shown_sections := _group_by_section([f for f in tpl_fields if not f.get("binds")])),
            "sheet_pages": sheet_pages(chosen_tpl, [name for name, _ in shown_sections]),
            "tpl_fields": tpl_fields,
            "custom_fields": custom_fields,
            "world_members": world_members,
            "linked_sheets": linked_sheets,
            "levelup_ready": levelup_ready,
            "roll_cfg": _roll_cfg(pc, chosen_tpl, can_manage),
            "hub_enabled": hub_enabled,
            "hub_mode": hub_mode,
            "hub_owner_name": hub_owner_name,
            **hub_ctx,
        })

    d = _derived(pc)
    tpl_fields = enrich_fields(chosen_tpl) if chosen_tpl else []
    custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
    catalog = game_catalog.catalog_payload()
    return templates.TemplateResponse("characters/sheet.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": pc, **d,
        "chosen_template": chosen_tpl,
        "tpl_fields": tpl_fields,
        "custom_fields": custom_fields,
        "condition_presets": system_conditions(chosen_tpl),
        "can_manage": can_manage,
        "world_members": world_members,
        "equipment_catalog": catalog["equipment"],
        "feats_catalog": catalog["feats"],
        "linked_sheets": linked_sheets,
        "levelup_ready": levelup_ready,
        "carry": char_extras.carry_info(db, pc),
        "roll_cfg": _roll_cfg(pc, chosen_tpl, can_manage),
        "hub_enabled": hub_enabled,
        "hub_mode": hub_mode,
        "hub_owner_name": hub_owner_name,
        **hub_ctx,
    })


_STAT_NAMES = {"str": "Strength", "dex": "Dexterity", "bod": "Body", "per": "Perception",
               "wil": "Willpower", "int": "Intellect", "cha": "Charisma", "itu": "Intuition"}


def _roll_cfg(pc: PlayerCharacter, tpl, can_manage: bool):
    """What the character page's roll sheet needs, or None (not the owner, or nothing rollable): a native N&D sheet rolls
    Stat + d10; a custom system with `dice` rules rolls its success pool."""
    from ..sheet_systems import dice_spec, pool_dice
    if not can_manage:
        return None
    if pc_maxima(pc)["native"]:
        try:
            stats = {str(x.get("id")): int(_num(x.get("value"), 0)) for x in json.loads(pc.stats_json or "[]") if isinstance(x, dict)}
        except (ValueError, TypeError):
            stats = {}
        return {"mode": "stat", "pc_id": pc.id, "pc_name": pc.name,
                "stats": [{"id": k, "label": v, "value": stats.get(k, 0), "pool": "pp" if k in ("str", "dex", "bod", "per") else "mp"}
                          for k, v in _STAT_NAMES.items()]}
    spec = dice_spec(tpl)
    if spec is None:
        return None
    cf = parse_custom_fields(pc.custom_fields_json)
    pools = []
    for p in spec["pools"]:
        _p, dice = pool_dice(spec, p["id"], cf)
        pools.append({"id": p["id"], "label": p["label"], "dice": dice})
    sp = spec.get("spend")
    return {"mode": "pool", "pc_id": pc.id, "pc_name": pc.name, "pools": pools, "threshold": spec.get("threshold", 6),
            "explode": spec.get("explode", True),
            "spend": {"field": sp["field"], "label": sp["label"], "per": sp.get("per", 1)} if sp else None}


def _assignable_members(db: Session, world_id: int):
    """Non-GM users invited to this world — the pool a GM can assign a
    PlayerCharacter's ownership to."""
    return (
        db.query(User)
        .join(WorldMembership, WorldMembership.user_id == User.id)
        .filter(WorldMembership.world_id == world_id, User.is_gm == False)  # noqa: E712
        .order_by(User.display_name)
        .all()
    )


@router.post("/characters/{pc_id}/owner")
def character_set_owner(
    pc_id: int, request: Request,
    owner_user_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """GM-only: link (or unlink) a PlayerCharacter to a connected player's
    account. Deliberately kept separate from _apply_form/character_update —
    that route is shared with the owning player's own self-edit, which must
    never be able to reassign ownership."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not user or not user.is_gm:
        raise HTTPException(403)

    owner_user_id = owner_user_id.strip()
    if not owner_user_id:
        pc.owner_user_id = None
    else:
        if not owner_user_id.isdigit():
            raise HTTPException(400, "Invalid player")
        target = db.query(User).filter(User.id == int(owner_user_id), User.is_gm == False).first()  # noqa: E712
        if not target:
            raise HTTPException(404, "Player not found")
        if not db.query(WorldMembership).filter(
            WorldMembership.world_id == pc.world_id, WorldMembership.user_id == target.id
        ).first():
            raise HTTPException(400, "That player isn't invited to this world")
        others = [c for c in _own_characters(db, pc.world_id, target.id) if c.id != pc.id]
        limit = character_limit(db.get(World, pc.world_id))
        if len(others) >= limit:
            who = target.display_name or target.email
            if limit <= 1:
                raise HTTPException(400, f'{who} already owns "{others[0].name}" in this world — unassign that one first.')
            raise HTTPException(
                400, f"{who} already owns {len(others)} characters in this world — the limit is {limit}. "
                     "Unassign one first, or raise the limit in the world's settings.")
        pc.owner_user_id = target.id
        if not pc.player_name:
            pc.player_name = target.display_name or target.email
    db.commit()
    return RedirectResponse(f"/characters/{pc_id}", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.get("/characters/{pc_id}/edit", response_class=HTMLResponse)
def character_edit_form(pc_id: int, request: Request, db: Session = Depends(get_db), active_world: str = Cookie(None)):
    world, worlds = get_world_ctx(request, db, active_world)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    chosen_tpl = db.query(SheetTemplate).filter(
        SheetTemplate.id == pc.sheet_template_id
    ).first() if pc.sheet_template_id else None
    if chosen_tpl and chosen_tpl.sheet_mode == "custom":
        # Custom-mode sheets are always-editable in place — no separate edit page.
        return RedirectResponse(f"/characters/{pc_id}", status_code=303)
    sheet_templates_list = _templates_for_world(db, world.id if world else None)
    tpl_fields = enrich_fields(chosen_tpl) if chosen_tpl else []
    custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
    return templates.TemplateResponse("characters/form.html", {
        "request": request, "world": world, "worlds": worlds,
        "pc": pc,
        "nd_default_stats": ND_DEFAULT_STATS,
        "nd_default_currency": ND_DEFAULT_CURRENCY,
        "stats":      json.loads(pc.stats_json      or "[]"),
        "currency":   json.loads(pc.currency_json   or "[]"),
        "equipment":  json.loads(pc.equipment_json  or "[]"),
        "feats":      json.loads(pc.feats_json       or "[]"),
        "cyberware":  json.loads(getattr(pc, "cyberware_json", None) or "[]"),
        "sheet_templates": sheet_templates_list,
        "chosen_template": chosen_tpl,
        "tpl_fields": tpl_fields,
        "custom_fields": custom_fields,
    })


@router.post("/characters/{pc_id}/edit")
async def character_update(
    pc_id: int,
    request: Request,
    portrait: UploadFile = File(None),
    db: Session = Depends(get_db),
    active_world: str = Cookie(None),
):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    form = await request.form()
    data = dict(form)
    _apply_form(pc, data, partial=True)
    if portrait and portrait.filename:
        url = _upload_portrait(portrait, db=db)
        if url:
            pc.portrait_url = url
    db.commit()
    live.touch(pc.world_id)
    return RedirectResponse(f"/characters/{pc_id}", status_code=303)


class SetPortraitFromJobBody(BaseModel):
    job_id: int
    url: str


@router.post("/api/characters/{pc_id}/portrait-from-url")
def character_set_portrait_from_job(
    pc_id: int, body: SetPortraitFromJobBody, request: Request, db: Session = Depends(get_db),
):
    """Sets a character's portrait straight from one of the player's OWN
    generated images (see the "player" image-gen routes in
    app/routers/ai.py) — the /image-gen page's "Set as portrait" action,
    without round-tripping through the full character-edit form/multipart
    upload. Deliberately NOT a bare {image_url} setter like the entity
    equivalent (api_entity_set_image in main.py, a GM/Assistant-only
    route): a player calling this must prove the url actually came from a
    job THEY generated (or, for a GM, any job in the world) by naming its
    job_id and having the url match one of that job's own result_urls —
    otherwise a player could set an arbitrary external URL as a portrait
    the whole party might see."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_manage_character(user, pc):
        raise HTTPException(403)
    job = db.get(ImageJob, body.job_id)
    if not job or job.world_id != pc.world_id:
        raise HTTPException(404)
    if not (user.is_gm or job.created_by_user_id == user.id):
        raise HTTPException(404)
    try:
        result_urls = json.loads(job.result_urls_json or "[]")
    except ValueError:
        result_urls = []
    url = body.url.strip()
    if url not in result_urls:
        raise HTTPException(400, "That image isn't a result of the given job")
    pc.portrait_url = url
    db.commit()
    return {"ok": True}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.post("/characters/{pc_id}/delete")
def character_delete(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    db.query(CharacterSheet).filter(CharacterSheet.player_character_id == pc.id).update(
        {"player_character_id": None}, synchronize_session=False,
    )
    delete_character_journal(db, pc.id)
    world_id = pc.world_id
    detach_pc(db, world_id, pc.id)
    db.delete(pc)
    db.commit()
    live.touch(world_id)
    return RedirectResponse("/characters", status_code=303)


# ── Retire → NPC (plan item: PC→NPC conversion) ──────────────────────────────

def _pc_to_npc_markdown(pc: PlayerCharacter) -> str:
    """Turn a character sheet into a lore-entity write-up — everything a GM
    would actually want to keep once a PC stops being an active, mechanical
    sheet: who they were (class/race/level), how they looked, and their
    personality/backstory. Mechanical minutiae (equipment, exact HP, feats)
    is deliberately left behind — that's sheet detail, not lore."""
    parts = []
    class_line = " · ".join(x for x in [pc.char_class, pc.race, f"Level {pc.level}" if pc.level and pc_maxima(pc)["native"] else ""] if x)
    if class_line:
        parts.append(f"*{class_line}*")
    tagline = " · ".join(x for x in [pc.background, pc.alignment] if x)
    if tagline:
        parts.append(tagline)

    appearance = ", ".join(
        f"{label}: {val}" for label, val in [
            ("Age", pc.age), ("Height", pc.height), ("Weight", pc.weight_app),
            ("Eyes", pc.eyes), ("Skin", pc.skin), ("Hair", pc.hair),
        ] if val
    )
    for heading, text in [
        ("Appearance", appearance),
        ("Personality", pc.personality_traits),
        ("Ideals", pc.ideals),
        ("Bonds", pc.bonds),
        ("Flaws", pc.flaws),
        ("Backstory", pc.backstory),
        ("Notes", pc.notes),
    ]:
        if text:
            parts += ["", f"## {heading}", "", text]
    return "\n".join(parts).strip()


@router.post("/characters/{pc_id}/retire-to-npc")
def character_retire_to_npc(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """GM-only: convert a retiring/dead PlayerCharacter into a lore Entity
    (kind="character") and delete the PlayerCharacter row — one-way, same
    as Delete, just with the character's write-up preserved as an NPC
    first. GM-only rather than reusing _can_manage_character (which also
    allows the owning player): entity creation is itself a GM-only
    capability everywhere else in this app (POST /new has no
    _is_player_safe entry), and turning a PC into GM-controlled lore
    content is a campaign-level call, not a self-service one."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not user or not user.is_gm:
        raise HTTPException(403)

    entity = Entity(
        world_id=pc.world_id, kind="character", name=pc.name,
        summary=f"Formerly played by {pc.player_name}." if pc.player_name else None,
        body=_pc_to_npc_markdown(pc),
        image_url=pc.portrait_url or None,
        visible_to_players=True,
    )
    db.add(entity)
    db.query(CharacterSheet).filter(CharacterSheet.player_character_id == pc.id).update(
        {"player_character_id": None}, synchronize_session=False,
    )
    delete_character_journal(db, pc.id)
    world_id = pc.world_id
    detach_pc(db, world_id, pc.id)
    db.delete(pc)
    db.commit()
    db.refresh(entity)
    live.touch(world_id)
    return RedirectResponse(f"/entity/{entity.id}", status_code=303)


# ── Export (.ndc — importable by NeonDragonsApp & NeonDragonsEditor) ─────────
#
# Both apps read this exact camelCase field schema (Character.kt / models/character.py
# in the UoY-Neon-Dragons rules repo). We emit a bare JSON array of character objects:
# NeonDragonsApp's CharacterCodec.decode() accepts a bare array via its legacy
# (pre-envelope) path, and NeonDragonsEditor's data/character_io.py::load_all_characters()
# treats a top-level array as its native multi-character format — so one file
# imports cleanly into both, with no changes needed in either app.

def _pc_to_ndc_dict(pc: PlayerCharacter) -> dict:
    stats = json.loads(pc.stats_json or "[]")
    stat_val = {s["id"]: int(s.get("value", 0)) for s in stats}
    feats = json.loads(pc.feats_json or "[]")
    equipment = json.loads(pc.equipment_json or "[]")
    currency = json.loads(pc.currency_json or "[]")
    d = _derived(pc)

    selected_feats, custom_feats = [], {}
    for f in feats:
        fid = f.get("id")
        if fid:
            selected_feats.append(fid)
        else:
            name = f.get("name") or ""
            if name:
                custom_feats[name] = f.get("notes") or f.get("description") or ""

    buckets = {
        "weapons": [], "armor": [], "augments": [], "bio_augments": [],
        "drones": [], "vehicles": [], "bases": [], "husks": [], "inventory": [],
    }
    custom_equipment = {}
    for e in equipment:
        eid = e.get("id")
        cat = e.get("category") or game_catalog.EQUIPMENT_CATEGORY_OF.get(eid, "")
        if eid and cat in buckets:
            buckets[cat].append(eid)
        elif eid:
            buckets["inventory"].append(eid)
        else:
            name = e.get("name") or ""
            if name:
                custom_equipment[name] = e.get("notes") or ""

    credits = next((int(c.get("value", 0)) for c in currency if (c.get("abbr") or "").upper() == "CR"), 0)

    portrait_b64 = ""
    if pc.portrait_url and pc.portrait_url.startswith("/uploads/"):
        img_path = UPLOADS_DIR / Path(pc.portrait_url).name
        if img_path.exists():
            portrait_b64 = base64.b64encode(img_path.read_bytes()).decode()

    notes = {}
    if pc.backstory:
        notes["Backstory"] = pc.backstory
    if pc.notes:
        notes["Session Notes"] = pc.notes

    race_id = pc.race_id or ""
    max_ectoplasm = current_ectoplasm = 0
    if race_id == "banshee":
        max_ectoplasm = current_ectoplasm = d["phys"] + d["ment"]

    return {
        "id": 0,
        "name": pc.name or "",
        "raceId": race_id, "raceName": pc.race or "",
        "baseRaceId": "", "baseRaceName": "",
        "professionId": pc.profession_id or "", "professionName": pc.char_class or "",
        "strength": stat_val.get("str", 0), "dexterity": stat_val.get("dex", 0),
        "body": stat_val.get("bod", 0), "perception": stat_val.get("per", 0),
        "willpower": stat_val.get("wil", 0), "intellect": stat_val.get("int", 0),
        "charisma": stat_val.get("cha", 0), "intuition": stat_val.get("itu", 0),
        "strengthBonus": 0, "dexterityBonus": 0, "bodyBonus": 0, "perceptionBonus": 0,
        "willpowerBonus": 0, "intellectBonus": 0, "charismaBonus": 0, "intuitionBonus": 0,
        "maxHealth": d["hp_max"], "currentHealth": pc.current_hp or 0,
        "temporaryHP": getattr(pc, "temp_hp", 0) or 0, "healthBonus": 0,
        "maxShock": d["shock_max"], "currentShock": d["shock_current"],
        "temporaryShock": 0, "shockBonus": 0,
        "cyberAdaptivity": d["ca_derived"], "speed": d["speed_derived"],
        "physicalPoints": d["phys"], "currentPP": d["pp_current"], "ppBonus": 0,
        "mentalPoints": d["ment"], "currentMP": d["mp_current"], "mpBonus": 0,
        "speedBonus": 0, "caBonus": 0,
        "maxEctoplasm": max_ectoplasm, "currentEctoplasm": current_ectoplasm,
        "selectedFeats": selected_feats, "creationFeats": list(selected_feats),
        "customFeats": custom_feats,
        "psyPowerSelections": {}, "jackOfTradeSelections": {},
        "linguistLanguages": [], "masterLinguistLanguages": [],
        "infectedVirus": "", "jackOfAllTradesSelections": [], "statPickerSelections": {},
        "equippedWeapons": buckets["weapons"], "customWeapons": {}, "weaponFeats": {},
        "activeWeapons": list(buckets["weapons"]),
        "equippedArmor": buckets["armor"], "armorFeats": {}, "activeArmor": list(buckets["armor"]),
        "installedAugments": buckets["augments"], "customAugments": {}, "augmentFeats": {},
        "activeAugments": list(buckets["augments"]),
        "installedBioAugments": buckets["bio_augments"], "customBioAugments": {}, "bioAugmentFeats": {},
        "activeBioAugments": list(buckets["bio_augments"]),
        "ownedDrones": buckets["drones"], "droneFeats": {},
        "ownedVehicles": buckets["vehicles"], "vehicleFeats": {},
        "inventory": buckets["inventory"], "customEquipment": custom_equipment,
        "ownedBases": buckets["bases"], "customBases": {},
        "ownedHusks": buckets["husks"], "equippedHuskId": "",
        "huskCurrentHealth": 0, "huskCurrentPP": 0,
        "craftedItems": [], "credits": credits,
        "yellowSat": 0, "transcendPts": 0, "transcendencePts": 0, "heartPres": 0,
        "dragonBlood": "", "dragonbloodedStatGroup": "", "fleshGraftPts": 0, "flashGraftsPts": 0,
        "crimsonBoost1": "", "crimsonBoost2": "", "crimsonPenalty": "", "mentalCond": "", "ahCharges": 0,
        "currentXP": pc.xp or 0, "xpSpent": 0,
        "majorEdges": getattr(pc, "major_edge_count", 0) or 0,
        "minorEdges": getattr(pc, "minor_edge_count", 0) or 0,
        "portraitBase64": portrait_b64,
        "notes": notes,
        "featSpecialAttrValues": {}, "professionSpecialAttrValues": {}, "raceSpecialAttrValues": {},
    }


@router.get("/characters/{pc_id}/export.ndc")
def character_export_ndc(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    if pc_maxima(pc)["native"] is False:
        raise HTTPException(400, "The .ndc format is the Neon & Dragons app's — a character on a custom sheet "
                                 "(Hunt in the Moonlight, Asterion, …) can't be exported to it. "
                                 "Use the JSON, Markdown, PDF or Foundry export instead.")
    payload = json.dumps([_pc_to_ndc_dict(pc)], ensure_ascii=False, indent=2)
    fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (pc.name or "character")) or "character"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}.ndc"'},
    )


def _pc_to_foundry_journal(pc: PlayerCharacter, db: Session = None) -> dict:
    """A single Foundry VTT JournalEntry document (v10+ page-based schema).
    Journal Entries are system-agnostic in Foundry, so this imports cleanly
    into any world regardless of which game system it runs — there's no
    "Neon & Dragons" Foundry system to map a real Actor into. A character on a
    custom sheet (Hunt in the Moonlight, Asterion, …) gets its own sections
    instead of N&D's HP/stat table."""
    e = html.escape
    if db is not None and pc.sheet_template_id and pc.sheet_template and pc.sheet_template.sheet_mode == "custom":
        body = [f"<h1>{e(pc.name or 'Character')}</h1>"]
        for section_name, pairs in _pc_export_sections(pc, db):
            body.append(f"<h2>{e(section_name)}</h2><ul>" + "".join(
                f"<li><strong>{e(str(label))}:</strong> {e(str(value))}</li>" for label, value in pairs) + "</ul>")
        if pc.backstory:
            body.append(f"<h2>Background</h2><p>{e(pc.backstory)}</p>")
        if pc.notes:
            body.append(f"<h2>Notes</h2><p>{e(pc.notes)}</p>")
        return {
            "name": pc.name or "Character",
            "folder": None,
            "pages": [{"name": "Character Sheet", "type": "text", "text": {"format": 1, "content": "".join(body)}, "sort": 0}],
            "flags": {"nd-world": {"source": "nd-world", "character_id": pc.id, "system": pc.sheet_template.slug}},
        }
    d = _derived(pc)

    sheet_rows = "".join(
        f"<tr><td>{e(s.get('label') or s.get('id',''))}</td><td>{e(str(s.get('value','')))}</td></tr>"
        for s in d["stats"] if isinstance(s, dict)
    )
    currency_line = ", ".join(f"{c.get('value',0)} {e(c.get('abbr') or c.get('label',''))}" for c in d["currency"])
    sheet_html = (
        f"<h1>{e(pc.name or 'Character')}</h1>"
        f"<p><em>{e(pc.race or '')} {e(pc.char_class or '')}"
        f"{' — Level ' + str(pc.level) if pc.level else ''}</em></p>"
        f"<p>HP: {pc.current_hp}/{d['hp_max']}"
        f"{'  |  Shock: ' + str(d['shock_current']) + '/' + str(d['shock_max']) if d['shock_max'] else ''}</p>"
        f"<p>Currency: {currency_line or '—'}</p>"
        + (f"<table><tbody>{sheet_rows}</tbody></table>" if sheet_rows else "")
        + (f"<h2>Background</h2><p>{e(pc.backstory)}</p>" if pc.backstory else "")
        + (f"<h2>Notes</h2><p>{e(pc.notes)}</p>" if pc.notes else "")
    )

    if d["equipment"]:
        eq_rows = "".join(
            f"<tr><td>{e(it.get('name',''))}</td><td>{it.get('qty',1)}</td><td>{it.get('weight',0)}</td>"
            f"<td>{'yes' if it.get('equipped') else ''}</td><td>{e(it.get('notes',''))}</td></tr>"
            for it in d["equipment"] if isinstance(it, dict)
        )
        equipment_html = f"<table><thead><tr><th>Item</th><th>Qty</th><th>Wt</th><th>Eq</th><th>Notes</th></tr></thead><tbody>{eq_rows}</tbody></table>"
    else:
        equipment_html = "<p><em>No equipment.</em></p>"

    if d["feats"]:
        feats_html = "<ul>" + "".join(
            f"<li><strong>{e(f.get('name',''))}</strong>"
            f"{' (' + e(f.get('type') or f.get('source') or '') + ')' if (f.get('type') or f.get('source')) else ''}"
            f"{' — ' + e(f.get('notes') or f.get('description') or '') if (f.get('notes') or f.get('description')) else ''}</li>"
            for f in d["feats"] if isinstance(f, dict)
        ) + "</ul>"
    else:
        feats_html = "<p><em>No feats.</em></p>"

    return {
        "name": pc.name or "Character",
        "folder": None,
        "pages": [
            {"name": "Character Sheet", "type": "text", "text": {"format": 1, "content": sheet_html}, "sort": 0},
            {"name": "Equipment", "type": "text", "text": {"format": 1, "content": equipment_html}, "sort": 100},
            {"name": "Feats", "type": "text", "text": {"format": 1, "content": feats_html}, "sort": 200},
        ],
        "flags": {"nd-world": {"source": "nd-world", "character_id": pc.id}},
    }


@router.get("/characters/{pc_id}/export.foundry.json")
def character_export_foundry(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    payload = json.dumps([_pc_to_foundry_journal(pc, db)], ensure_ascii=False, indent=2)
    fname = "".join(c if c.isalnum() or c in " -_" else "" for c in (pc.name or "character")) or "character"
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}.foundry.json"'},
    )


# ── Generic exports: JSON (re-importable) / Markdown / PDF ───────────────────
#
# Unlike .ndc (NeonDragonsApp's own Kotlin-shaped format) and .foundry.json
# (a Foundry VTT JournalEntry), these three are plain, human-usable exports:
# .json round-trips straight back through POST /api/import/execute
# (kind=player_character — see docs/IMPORT_JSON_GUIDE.md), and .md/.pdf are
# read-only reference copies. All three work uniformly for both sheet modes
# (native N&D sheet, or an entirely custom SheetTemplate) since they're built
# from the same section list rather than assuming fixed D&D-shaped columns.

def _safe_export_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "" for c in (name or "character")) or "character"


def _pc_to_import_dict(pc: PlayerCharacter) -> dict:
    """The canonical nd-world import shape for this character — exactly what
    _apply_form/_upsert_player_character (app/routers/importer.py) accept
    back as kind="player_character". Omits zero/empty fields so the exported
    file stays readable rather than listing every unused column."""
    out = {}
    for field in _PC_LIVE_SCALAR_FIELDS:
        val = getattr(pc, field, "") or ""
        if val:
            out[field] = val
    out.setdefault("name", pc.name or "Character")
    if pc.race_id:
        out["race_id"] = pc.race_id
    if pc.profession_id:
        out["profession_id"] = pc.profession_id
    if pc.level:
        out["level"] = pc.level
    if pc.xp:
        out["xp"] = pc.xp
    if pc.max_hp:
        out["max_hp"] = pc.max_hp
    if pc.current_hp:
        out["current_hp"] = pc.current_hp
    for field in _PC_LIVE_DICT_FIELDS:
        try:
            val = json.loads(getattr(pc, field, None) or "{}")
        except Exception:
            val = {}
        if val:
            out[field] = val
    for field in _PC_LIVE_LIST_FIELDS:
        try:
            val = json.loads(getattr(pc, field, None) or "[]")
        except Exception:
            val = []
        if val:
            out[field] = val
    if pc.sheet_template_id:
        out["sheet_template_id"] = pc.sheet_template_id
    if pc.portrait_url:
        out["portrait_url"] = pc.portrait_url
    return out


def _pc_field_value_lines(field: dict, custom_fields: dict) -> list:
    """One (label, value) pair per SheetTemplate field for the export section
    list below — a "list"-type field (a repeatable group, e.g. an abilities
    table) expands to one pair per item rather than trying to squeeze a
    whole sub-table into a single value string."""
    fid = field.get("id")
    label = field.get("label") or fid or ""
    if field.get("type") == "list":
        items = custom_fields.get(fid)
        if not isinstance(items, list) or not items:
            return []
        item_fields = field.get("item_fields") or []
        out = []
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            parts = [
                f"{sf.get('label') or sf.get('id')}: {item.get(sf.get('id'), '')}"
                for sf in item_fields if item.get(sf.get("id"))
            ]
            out.append((f"{label} #{i}", "; ".join(parts)))
        return out
    if field.get("type") == "resource":
        # resources live under {id}_current / {id}_max (never under the bare id)
        text = field_value_text(field, custom_fields)
        return [(short_label(label) or label, text)] if text else []
    value = custom_fields.get(fid, field.get("default_value", ""))
    if value in (None, ""):
        return []
    return [(label, str(value))]


def _pc_export_sections(pc: PlayerCharacter, db: Session) -> list:
    """A flat (section_name, [(label, value), ...]) list summarizing this
    character for the .md/.pdf exports below — covers both the native N&D
    sheet (via _derived, same helper the sheet page itself uses) and any
    SheetTemplate fields (native "nd" mode's extra fields, or the entirety
    of a "custom" mode sheet), so it reads correctly regardless of which
    system this character actually uses."""
    chosen_tpl = db.query(SheetTemplate).filter(SheetTemplate.id == pc.sheet_template_id).first() if pc.sheet_template_id else None
    custom_fields = json.loads(getattr(pc, "custom_fields_json", None) or "{}")
    sections = []

    is_custom = bool(chosen_tpl and chosen_tpl.sheet_mode == "custom")
    basics = [
        (l, v) for l, v in (
            ("System", chosen_tpl.name if chosen_tpl else ""),
            ("Player", pc.player_name), ("Race", pc.race), ("Class", pc.char_class),
            # Level / XP columns are the N&D sheet's; a custom system keeps its own in its fields
            ("Level", str(pc.level) if pc.level and not is_custom else ""),
            ("XP", str(pc.xp) if pc.xp and not is_custom else ""),
        ) if v
    ]
    if basics:
        sections.append(("Basics", basics))
    conds = _pc_condition_list(pc)
    if conds:
        sections.append(("Conditions", [("Active", ", ".join(conds))]))

    if not is_custom:
        d = _derived(pc)
        resources = [("HP", f"{pc.current_hp}/{d['hp_max']}")]
        if d["shock_max"]:
            resources.append(("Shock", f"{d['shock_current']}/{d['shock_max']}"))
        if d["pp_current"] or d["mp_current"]:
            resources.append(("PP", str(d["pp_current"])))
            resources.append(("MP", str(d["mp_current"])))
        if pc.armor_class:
            resources.append(("Armor Class", str(pc.armor_class)))
        if pc.speed:
            resources.append(("Speed", str(pc.speed)))
        sections.append(("Resources", resources))

        stats = [
            (s.get("label") or s.get("id", ""), str(s.get("value", "")))
            for s in d["stats"] if isinstance(s, dict) and s.get("id")
        ]
        if stats:
            sections.append(("Ability Scores", stats))

        currency = [
            (c.get("label") or c.get("abbr") or "", str(c.get("value", 0)))
            for c in d["currency"] if isinstance(c, dict)
        ]
        if currency:
            sections.append(("Currency", currency))

        equipment = []
        for it in d["equipment"]:
            if not isinstance(it, dict):
                continue
            qty, notes = it.get("qty", 1), it.get("notes", "")
            equipment.append((it.get("name", ""), f"x{qty}" + (f" — {notes}" if notes else "")))
        if equipment:
            sections.append(("Equipment", equipment))

        feats = []
        for f in d["feats"]:
            if not isinstance(f, dict):
                continue
            feats.append((f.get("name", ""), f.get("notes") or f.get("description") or ""))
        if feats:
            sections.append(("Feats", feats))

    if chosen_tpl:
        tpl_fields = json.loads(chosen_tpl.fields_json or "[]")
        for section_name, fields in _group_by_section(tpl_fields):
            pairs = []
            for f in fields:
                pairs.extend(_pc_field_value_lines(f, custom_fields))
            if pairs:
                sections.append((section_name, pairs))

    personality = [
        (l, v) for l, v in (
            ("Personality Traits", pc.personality_traits), ("Ideals", pc.ideals),
            ("Bonds", pc.bonds), ("Flaws", pc.flaws),
        ) if v
    ]
    if personality:
        sections.append(("Personality", personality))

    return sections


def _pc_to_markdown(pc: PlayerCharacter, db: Session) -> str:
    lines = [f"# {pc.name or 'Character'}"]
    subtitle = " ".join(b for b in (pc.race, pc.char_class) if b)
    if pc.level and pc_maxima(pc)["native"]:  # Level is the N&D sheet's; custom systems keep progress in their fields
        subtitle = (subtitle + f" — Level {pc.level}").strip(" —")
    if subtitle:
        lines.append(f"*{subtitle}*")
    lines.append("")

    for section_name, pairs in _pc_export_sections(pc, db):
        lines.append(f"## {section_name}")
        for label, value in pairs:
            lines.append(f"- **{label}:** {value}")
        lines.append("")

    if pc.backstory:
        lines.append("## Backstory")
        lines.append(pc.backstory.strip())
        lines.append("")
    if pc.notes:
        lines.append("## Notes")
        lines.append(pc.notes.strip())
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _pc_to_pdf_bytes(pc: PlayerCharacter, db: Session) -> bytes:
    # Imported lazily — reportlab is only needed by this one export path,
    # and a local import keeps it off the module's normal startup cost.
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(html.escape(pc.name or "Character"), styles["Title"])]

    subtitle = " ".join(b for b in (pc.race, pc.char_class) if b)
    if pc.level and pc_maxima(pc)["native"]:  # Level is the N&D sheet's; custom systems keep progress in their fields
        subtitle = (subtitle + f" — Level {pc.level}").strip(" —")
    if subtitle:
        story.append(Paragraph(html.escape(subtitle), styles["Italic"]))
    story.append(Spacer(1, 12))

    for section_name, pairs in _pc_export_sections(pc, db):
        story.append(Paragraph(html.escape(section_name), styles["Heading2"]))
        for label, value in pairs:
            story.append(Paragraph(f"<b>{html.escape(label)}:</b> {html.escape(value)}", styles["Normal"]))
        story.append(Spacer(1, 8))

    for heading, text in (("Backstory", pc.backstory), ("Notes", pc.notes)):
        if not text:
            continue
        story.append(Paragraph(heading, styles["Heading2"]))
        for para in text.strip().split("\n\n"):
            story.append(Paragraph(html.escape(para).replace("\n", "<br/>"), styles["Normal"]))
        story.append(Spacer(1, 8))

    doc.build(story)
    return buf.getvalue()


@router.get("/characters/{pc_id}/export.json")
def character_export_json(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    payload = json.dumps(_pc_to_import_dict(pc), ensure_ascii=False, indent=2)
    fname = _safe_export_filename(pc.name)
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}.json"'},
    )


@router.get("/characters/{pc_id}/export.md")
def character_export_md(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    payload = _pc_to_markdown(pc, db)
    fname = _safe_export_filename(pc.name)
    return StreamingResponse(
        io.BytesIO(payload.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}.md"'},
    )


@router.get("/characters/{pc_id}/export.pdf")
def character_export_pdf(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    payload = _pc_to_pdf_bytes(pc, db)
    fname = _safe_export_filename(pc.name)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}.pdf"'},
    )


async def _json_dict(request: Request) -> dict:
    """The JSON body of a quick-edit route, or 400 — never a 500 on a missing,
    malformed or non-object body."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")
    return body


def _body_int(body: dict, key: str, default: int = 0) -> int:
    try:
        return int_field(body, key, default)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── AJAX: HP ──────────────────────────────────────────────────────────────────

def _log_num(db, pc, request, op, label, before, after) -> None:
    """Record a numeric change in the character's log (with how to undo it) - nothing when the number did not move."""
    if before != after:
        char_extras.log(db, pc, char_extras.actor_name(_current_user(request)), op, f"{label} {before} \u2192 {after}",
                        {"op": op, "value": before})


@router.post("/api/characters/{pc_id}/hp-async")
async def character_hp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    action = body.get("action", "set")
    val = _body_int(body, "value")
    _before = pc.current_hp or 0
    # Effective HP max (0 stored = auto-derived from physical stats)
    eff_max_hp = pc_maxima(pc)["hp"]
    temp_hp = getattr(pc, "temp_hp", 0) or 0
    if action == "delta":
        pc.current_hp = max(0, min(eff_max_hp + temp_hp, (pc.current_hp or 0) + val))
    elif action == "temp":
        pc.temp_hp = max(0, val)
    elif action == "max":
        pc.max_hp = max(0, val)
        new_max = pc_maxima(pc)["hp"]
        pc.current_hp = min(pc.current_hp or 0, new_max) if new_max else (pc.current_hp or 0)
    else:
        pc.current_hp = max(0, min(eff_max_hp + temp_hp, val))
    _log_num(db, pc, request, "hp", "HP", _before, pc.current_hp or 0)
    db.commit()
    live.touch(pc.world_id)
    return {
        "current_hp": pc.current_hp,
        "max_hp": pc_maxima(pc)["hp"],
        "temp_hp": getattr(pc, "temp_hp", 0) or 0,
        "death_success": getattr(pc, "death_saves_success", 0) or 0,
        "death_failure": getattr(pc, "death_saves_failure", 0) or 0,
        "secondary_current": getattr(pc, "secondary_resource_current", 0),
    }


# ── AJAX: Shock ───────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/shock")
async def character_shock_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    action = body.get("action", "set")
    val = _body_int(body, "value")
    _before = getattr(pc, 'shock_current', 0) or 0
    shock_max = pc_maxima(pc)["shock"]
    shock_current = getattr(pc, "shock_current", 0) or 0
    if action == "delta":
        shock_current = max(0, min(shock_max, shock_current + val))
    elif action == "set":
        shock_current = max(0, min(shock_max, val))
    pc.shock_current = shock_current
    _log_num(db, pc, request, "shock", "Shock", _before, pc.shock_current or 0)
    db.commit()
    live.touch(pc.world_id)
    return {"shock_current": pc.shock_current, "shock_max": shock_max}


# ── AJAX: PP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/pp")
async def character_pp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    action = body.get("action", "set")
    val = _body_int(body, "value")
    _before = getattr(pc, 'pp_current', 0) or 0
    pp_max = pc_maxima(pc)["pp"]  # PP max = sum of physical stats
    pp_current = getattr(pc, "pp_current", 0) or 0
    if action == "delta":
        pp_current = max(0, min(pp_max, pp_current + val))
    elif action == "set":
        pp_current = max(0, min(pp_max, val))
    elif action == "rest":
        pp_current = min(pp_max, pp_current + pp_max // 2)
    pc.pp_current = pp_current
    _log_num(db, pc, request, "pp", "PP", _before, pc.pp_current or 0)
    db.commit()
    live.touch(pc.world_id)
    return {"pp_current": pc.pp_current, "pp_max": pp_max}


# ── AJAX: MP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/mp")
async def character_mp_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    action = body.get("action", "set")
    val = _body_int(body, "value")
    _before = getattr(pc, 'mp_current', 0) or 0
    mp_max = pc_maxima(pc)["mp"]  # MP max = sum of mental stats
    mp_current = getattr(pc, "mp_current", 0) or 0
    if action == "delta":
        mp_current = max(0, min(mp_max, mp_current + val))
    elif action == "set":
        mp_current = max(0, min(mp_max, val))
    elif action == "rest":
        mp_current = min(mp_max, mp_current + mp_max // 2)
    pc.mp_current = mp_current
    _log_num(db, pc, request, "mp", "MP", _before, pc.mp_current or 0)
    db.commit()
    live.touch(pc.world_id)
    return {"mp_current": pc.mp_current, "mp_max": mp_max}


# ── AJAX: Conditions ──────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/conditions")
async def character_conditions_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Persist a character's conditions (Burn, Stunned, …). Body:
    {action: "add"|"remove"|"toggle", name} or {action: "set", conditions:[…]}.
    Labels are cleaned (printable, ≤40 chars), de-duplicated case-insensitively
    and capped at 12 — conditions are free text the party strip and combat
    tracker echo, so they are kept short and plain. Owner-or-GM like every
    other quick-edit route (this prefix is player-reachable, so it self-gates)."""
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    action = body.get("action", "toggle")
    try:
        current = clean_conditions(json.loads(pc.conditions_json or "[]"))
    except ValueError:
        current = []
    if action == "set":
        if not isinstance(body.get("conditions"), list):
            raise HTTPException(400, "conditions must be a list")
        new = clean_conditions(body["conditions"])
    elif action in ("add", "remove", "toggle"):
        name = clean_condition(body.get("name"))
        if not name:
            raise HTTPException(400, "name is required")
        has = any(c.lower() == name.lower() for c in current)
        if action == "remove" or (action == "toggle" and has):
            new = [c for c in current if c.lower() != name.lower()]
        elif has:
            new = current
        elif len(current) >= MAX_CONDITIONS:
            raise HTTPException(400, f"A character can have at most {MAX_CONDITIONS} conditions")
        else:
            new = current + [name]
    else:
        raise HTTPException(400, "action must be add, remove, toggle or set")
    if new != current:
        gained, lost = [c for c in new if c not in current], [c for c in current if c not in new]
        char_extras.log(db, pc, char_extras.actor_name(_current_user(request)), "condition",
                        ", ".join([f"+{c}" for c in gained] + [f"\u2212{c}" for c in lost]),
                        {"op": "conditions", "value": current})
    pc.conditions_json = json.dumps(new)
    db.commit()
    live.touch(pc.world_id)
    return {"conditions": new}


@router.post("/api/characters/{pc_id}/resource")
async def character_resource_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Quick-adjust ONE resource track (Health, Stamina, Hunger, ...) of a character on a custom sheet system - the
    custom-sheet equivalent of the N&D HP / Shock steppers, so a phone never has to open the edit form for a point of
    damage. Body: {field_id, action: "delta"|"set", value}. Clamped to 0..max (no upper bound when the track has no
    max). Owner-or-GM like every other quick-edit route (this prefix is player-reachable, so it self-gates)."""
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    tpl = db.get(SheetTemplate, pc.sheet_template_id) if getattr(pc, "sheet_template_id", None) else None
    if tpl is None or tpl.sheet_mode != "custom":
        raise HTTPException(400, "This character has no custom resource tracks")
    cf = parse_custom_fields(pc.custom_fields_json)
    tracks = resource_tracks(template_fields(tpl), cf, system_meta(tpl))
    fid = str(body.get("field_id") or "")
    track = next((t for t in tracks if t["id"] == fid), None)
    if track is None:
        raise HTTPException(404, "No such resource")
    val = _body_int(body, "value")
    cur = track["current"] if isinstance(track["current"], (int, float)) else 0
    new = max(0, int(cur + val) if body.get("action", "set") == "delta" else val)
    if track["max"]:
        new = min(new, int(track["max"]))
    if new != cur:
        char_extras.log(db, pc, char_extras.actor_name(_current_user(request)), "resource", f"{track['label']} {int(cur)} \u2192 {new}",
                        {"op": "cf", "values": {f"{fid}_current": cf.get(f"{fid}_current", cur)}})
    cf[f"{fid}_current"] = new
    pc.custom_fields_json = json.dumps(cf)
    db.commit()
    live.touch(pc.world_id)
    return {"field_id": fid, "current": new, "max": track["max"]}


# ── Live vitals (sheet live-sync source) ──────────────────────────────────────

@router.get("/api/characters/{pc_id}/vitals")
def character_vitals(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Current HP / Shock / PP / MP / XP / conditions with the SAME effective
    maxima the sheet renders — what an open sheet re-fetches when the live-sync
    bus says something in the world changed (a GM's combat sync, a Rest, the
    party XP award). Viewable by exactly the people who may view the sheet."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    world = db.query(World).filter(World.id == pc.world_id).first()
    if not _can_view_character(db, _current_user(request), pc, world):
        raise HTTPException(403)
    m = pc_maxima(pc)
    d = _derived(pc)
    return {
        "level": pc.level, "xp": pc.xp or 0, "xp_hi": d["xp_hi"], "xp_pct": d["xp_pct"],
        "hp": pc.current_hp or 0, "max_hp": m["hp"], "temp_hp": getattr(pc, "temp_hp", 0) or 0,
        "shock": pc.shock_current or 0, "shock_max": m["shock"],
        "pp": pc.pp_current or 0, "pp_max": m["pp"],
        "mp": pc.mp_current or 0, "mp_max": m["mp"],
        "conditions": _pc_condition_list(pc),
    }


# ── AJAX: XP ──────────────────────────────────────────────────────────────────

@router.post("/api/characters/{pc_id}/xp")
async def character_xp(pc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    delta = _body_int(body, "delta")
    _before = pc.xp or 0
    pc.xp = max(0, (pc.xp or 0) + delta)
    _log_num(db, pc, request, "xp", "XP", _before, pc.xp or 0)
    db.commit()
    live.touch(pc.world_id)
    lvl = min(pc.level, 20)
    xp_lo = XP_THRESHOLDS[lvl - 1]
    xp_hi = XP_THRESHOLDS[lvl] if lvl < 20 else None
    xp_pct = min(100, int(max(0, pc.xp - xp_lo) * 100 / (xp_hi - xp_lo))) if xp_hi and xp_hi > xp_lo else 100
    return {"xp": pc.xp, "xp_lo": xp_lo, "xp_hi": xp_hi, "xp_pct": xp_pct}


# ── AJAX: Equipment / Feats (inline sheet quick-add) ───────────────────────────

@router.post("/api/characters/{pc_id}/level-up")
async def character_level_up(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """One-click level application when XP has already crossed the threshold
    (see _levelup_ready). Owner-or-GM gated like every other manage route;
    refuses (400) when the XP doesn't justify a level, so a stale banner can
    never double-level a character."""
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_manage_character(user, pc):
        raise HTTPException(403)
    if not _levelup_ready(pc):
        raise HTTPException(400, f"Not enough XP to reach level {pc.level + 1} yet.")
    _before = pc.level
    pc.level += 1
    _log_num(db, pc, request, "level", "Level", _before, pc.level)
    db.commit()
    live.touch(pc.world_id)
    return {"level": pc.level, "name": pc.name}


@router.post("/api/characters/{pc_id}/equipment")
async def character_equipment_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    body = await request.json()
    equipment = json.loads(pc.equipment_json or "[]")
    _before_equipment = pc.equipment_json or "[]"
    action = body.get("action", "add")
    if action == "add":
        item = body.get("item") or {}
        name = str(item.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "Item name is required")
        entry = {
            "name": name, "qty": int(_num(item.get("qty"), 1)),
            "weight": _num(item.get("weight"), 0), "equipped": bool(item.get("equipped")),
            "notes": str(item.get("notes", "")),
        }
        for k in ("id", "category", "cost"):
            if item.get(k) is not None:
                entry[k] = item[k]
        equipment.append(entry)
    elif action == "remove":
        index = body.get("index")
        if not isinstance(index, int) or not (0 <= index < len(equipment)):
            raise HTTPException(400, "Invalid index")
        equipment.pop(index)
    elif action in ("qty", "weight"):
        # use one / pick one up (qty +-1, never below 0 - a used-up stim stays on the list as x0), or set what it weighs
        index = body.get("index")
        if not isinstance(index, int) or not (0 <= index < len(equipment)) or not isinstance(equipment[index], dict):
            raise HTTPException(400, "Invalid index")
        if action == "qty":
            equipment[index]["qty"] = max(0, min(9999, int(_num(equipment[index].get("qty"), 1)) + _body_int(body, "delta")))
        else:
            equipment[index]["weight"] = max(0, min(9999, _num(body.get("value"), 0)))
    else:
        raise HTTPException(400, "Unknown action")
    pc.equipment_json = json.dumps(equipment)
    if (pc.equipment_json != _before_equipment):
        what = {"add": "added " + str((body.get("item") or {}).get("name", "an item")), "remove": "removed an item"}.get(action, "changed a quantity / weight")
        char_extras.log(db, pc, char_extras.actor_name(_current_user(request)), "equipment", f"Equipment: {what}",
                        {"op": "equipment", "value": _before_equipment} if len(_before_equipment) <= 20000 else None)
    db.commit()
    live.touch(pc.world_id)
    total_weight = sum(_num(it.get("weight"), 0) * _num(it.get("qty"), 1) for it in equipment if isinstance(it, dict))
    return {"equipment": equipment, "total_weight": total_weight, "carry": char_extras.carry_info(db, pc)}


@router.post("/api/characters/{pc_id}/currency")
async def character_currency_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Coin purse: {abbr, action: "delta"|"set", value} on one currency of the character (matched by abbreviation or label,
    case-insensitively). Never below 0. Owner-or-GM like the other quick edits."""
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_manage_character(user, pc):
        raise HTTPException(403)
    coins = json.loads(pc.currency_json or "[]")
    key = str(body.get("abbr") or "").strip().lower()
    coin = next((c for c in coins if isinstance(c, dict) and key and key in ((c.get("abbr") or "").lower(), (c.get("label") or "").lower())), None)
    if coin is None:
        raise HTTPException(404, "No such currency on this character")
    before = int(_num(coin.get("value"), 0))
    val = _body_int(body, "value")
    after = max(0, before + val) if body.get("action", "set") == "delta" else max(0, val)
    coin["value"] = after
    pc.currency_json = json.dumps(coins)
    if after != before:
        label = coin.get("abbr") or coin.get("label") or "coins"
        char_extras.log(db, pc, char_extras.actor_name(user), "currency", f"{label} {before} \u2192 {after}",
                        {"op": "currency", "abbr": coin.get("abbr") or coin.get("label"), "value": before})
    db.commit()
    live.touch(pc.world_id)
    return {"abbr": coin.get("abbr") or coin.get("label"), "value": after}


@router.post("/api/characters/{pc_id}/carry-limit")
async def character_carry_limit_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Set how much the character can carry ({value}; 0 = back to the default of 5 x (STR + BOD))."""
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    val = max(0, min(99999, _body_int(body, "value")))
    char_extras.set_prefs(db, pc, carry_limit=val or None)
    db.commit()
    return {"carry": char_extras.carry_info(db, pc)}


@router.post("/api/characters/{pc_id}/feats")
async def character_feats_async(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    body = await request.json()
    feats = json.loads(pc.feats_json or "[]")
    action = body.get("action", "add")
    if action == "add":
        item = body.get("item") or {}
        name = str(item.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "Feat name is required")
        entry = {
            "name": name, "type": str(item.get("type", "")),
            "rank": str(item.get("rank", "")), "notes": str(item.get("notes", "")),
        }
        if item.get("id") is not None:
            entry["id"] = item["id"]
        feats.append(entry)
    elif action == "remove":
        index = body.get("index")
        if not isinstance(index, int) or not (0 <= index < len(feats)):
            raise HTTPException(400, "Invalid index")
        feats.pop(index)
    else:
        raise HTTPException(400, "Unknown action")
    pc.feats_json = json.dumps(feats)
    db.commit()
    live.touch(pc.world_id)
    return {"feats": feats}


# ── Character-sync JSON API ─────────────────────────────────────────────────────
# A clean, general character-as-JSON GET/PUT for external clients (e.g. the
# NeonDragonsApp Android client) to pull/push a full character over HTTP, on top
# of the existing session-cookie auth — no new auth mechanism, no partial-update
# semantics (each call replaces every live field), no conflict resolution (the
# caller chooses to pull or push; either one is a deliberate full overwrite).

def _pc_to_sync_dict(pc: PlayerCharacter) -> dict:
    data = {
        "id": pc.id,
        "world_id": pc.world_id,
        "updated_at": pc.updated_at.isoformat() if pc.updated_at else None,
        "portrait_url": pc.portrait_url or "",
        "sheet_template_id": pc.sheet_template_id,
    }
    for field in _PC_LIVE_SCALAR_FIELDS:
        if field in ("level", "xp"):
            continue  # integers — a falsy 0 would otherwise get coerced into "" below
        data[field] = getattr(pc, field, "") or ""
    data["level"] = pc.level or 1
    data["xp"] = pc.xp or 0
    data["race_id"] = pc.race_id or ""
    data["profession_id"] = pc.profession_id or ""
    data["max_hp"] = pc.max_hp or 0
    data["current_hp"] = pc.current_hp or 0
    for field in ("shock_max", "shock_current", "pp_current", "mp_current",
                  "minor_edge_count", "major_edge_count"):
        data[field] = getattr(pc, field, 0) or 0
    data["minor_edge"] = pc.minor_edge or ""
    data["major_edge"] = pc.major_edge or ""
    for field in _PC_LIVE_DICT_FIELDS:
        raw = getattr(pc, field, None)
        try:
            data[field] = json.loads(raw) if raw else {}
        except Exception:
            data[field] = {}
    for field in _PC_LIVE_LIST_FIELDS:
        raw = getattr(pc, field, None)
        try:
            data[field] = json.loads(raw) if raw else []
        except Exception:
            data[field] = []
    return data


def _apply_sync_json(pc: PlayerCharacter, data: dict):
    """Like _apply_form, but for a JSON body whose values are already typed
    (not form-encoded strings) — used by the sync PUT/POST routes below."""
    def gi(k, d=0):
        try:
            return int(data.get(k, d))
        except (TypeError, ValueError):
            return d
    def gs(k, d=""):
        return str(data.get(k, d) if data.get(k) is not None else d).strip()

    for field in _PC_LIVE_SCALAR_FIELDS:
        setattr(pc, field, gs(field))
    pc.name = pc.name or "Unnamed"
    pc.race_id        = gs("race_id", pc.race_id or "")
    pc.profession_id  = gs("profession_id", pc.profession_id or "")
    pc.level = max(1, min(20, gi("level", pc.level or 1)))
    pc.xp    = max(0, gi("xp", pc.xp or 0))

    pc.max_hp     = max(0, gi("max_hp", 0))
    pc.current_hp = gi("current_hp", pc.max_hp)
    pc.shock_max     = max(0, gi("shock_max"))
    pc.shock_current = max(0, gi("shock_current"))
    pc.pp_current    = max(0, gi("pp_current"))
    pc.mp_current    = max(0, gi("mp_current"))

    pc.minor_edge = gs("minor_edge")
    pc.major_edge = gs("major_edge")
    pc.minor_edge_count = max(0, gi("minor_edge_count", pc.minor_edge_count or 0))
    pc.major_edge_count = max(0, gi("major_edge_count", pc.major_edge_count or 0))

    tpl_id = data.get("sheet_template_id")
    pc.sheet_template_id = int(tpl_id) if tpl_id else None

    for field in _PC_LIVE_DICT_FIELDS:
        value = data.get(field)
        try:
            setattr(pc, field, json.dumps(value if isinstance(value, dict) else {}))
        except (TypeError, ValueError):
            setattr(pc, field, "{}")
    for field in _PC_LIVE_LIST_FIELDS:
        value = data.get(field)
        try:
            setattr(pc, field, json.dumps(value if isinstance(value, list) else []))
        except (TypeError, ValueError):
            setattr(pc, field, "[]")

    pc.updated_at = datetime.utcnow()


@router.get("/api/characters/{pc_id}/sync")
def character_sync_get(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    return _pc_to_sync_dict(pc)


@router.put("/api/characters/{pc_id}/sync")
async def character_sync_put(pc_id: int, request: Request, db: Session = Depends(get_db)):
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    if not _can_manage_character(_current_user(request), pc):
        raise HTTPException(403)
    body = await request.json()
    _apply_sync_json(pc, body)
    db.commit()
    return _pc_to_sync_dict(pc)


@router.post("/api/worlds/{world_id}/characters/sync")
async def character_sync_create(world_id: int, request: Request, db: Session = Depends(get_db)):
    user = _current_user(request)
    if not user:
        raise HTTPException(401)
    world = db.query(World).filter(World.id == world_id).first()
    if not world or not auth.user_can_access_world(db, user, world):
        raise HTTPException(404)
    if not user.is_gm:
        owned = len(_own_characters(db, world_id, user.id))
        if owned >= character_limit(world):
            raise HTTPException(400, _limit_reached_message(owned, character_limit(world), sync=True))
    body = await request.json()
    pc = PlayerCharacter(world_id=world_id, owner_user_id=user.id, name="Unnamed")
    _apply_sync_json(pc, body)
    db.add(pc)
    db.commit()
    db.refresh(pc)
    return _pc_to_sync_dict(pc)


@router.get("/api/me")
def api_me(request: Request, db: Session = Depends(get_db)):
    user = _current_user(request)
    if not user:
        raise HTTPException(401)
    if user.is_gm:
        worlds = db.query(World).order_by(World.name).all()
    else:
        worlds = (
            db.query(World)
            .join(WorldMembership, WorldMembership.world_id == World.id)
            .filter(WorldMembership.user_id == user.id)
            .order_by(World.name)
            .all()
        )
    world_list = []
    for w in worlds:
        mine = _own_characters(db, w.id, user.id)
        pc = mine[0] if mine else None
        # slug lets a caller select this world via the ?w=<slug> query param
        # (see deps.resolve_world_slug) on any world-scoped endpoint, e.g.
        # /api/import/execute — added for NeonDragonsEditor's export flow.
        # Additive only: existing clients (NeonDragonsApp's Gson-based
        # MeWorldDto) simply ignore JSON fields they don't declare.
        # character_id stays the FIRST character for clients written for one-per-player; character_ids lists them all.
        world_list.append({"id": w.id, "name": w.name, "slug": w.slug, "character_id": pc.id if pc else None,
                           "character_ids": [c.id for c in mine], "max_characters": character_limit(w)})
    return {
        "user": {"id": user.id, "email": user.email, "is_gm": user.is_gm},
        "worlds": world_list,
    }


# ── AJAX: Dice roll ───────────────────────────────────────────────────────────

# ── Tap-to-roll ───────────────────────────────────────────────────────────────
# Stat checks (N&D, Chronicles of the Worm) and success pools (Hunt in the Moonlight, Asterion): the server rolls (the player
# cannot pick a number), pays what was spent, writes the roll to the table's shared log and the character's change log.

def _roll_log(db, request, world, pc, notation, breakdown, total, label):
    """Log to the shared dice log when the player may use it; (response dict or None, shared?)."""
    from . import dice as _dice
    if not world_can_view_section(request, world, "dice"):
        return None, False
    return _dice._roll_response(_dice._save_roll(db, request, world, notation, breakdown, total, label)), True


@router.post("/api/characters/{pc_id}/roll")
async def character_roll(pc_id: int, request: Request, db: Session = Depends(get_db)):
    """Roll for a character. Body, one of:
    * {"kind": "stat", "stat": "int", "boost": 0-2, "mode": "normal"|"adv"|"dis"} - Stat + d10 + points spent from the matching
      pool (PP for physical stats, MP for mental); natural 10 = critical, natural 1 = automatic failure.
    * {"kind": "pool", "pool": id, "spend": n, "mod": -3..3, "need": 0-6} - a d10 success pool by the template's `dice` rules
      (6+ succeeds, 10 explodes); `spend` pays Stamina / Ichor for +1d10 each, `mod` is the GM's situational dice, `need` the
      successes the GM asked for (0 = none). Owner-or-GM."""
    from .. import pool_roll
    from ..sheet_systems import dice_spec, pool_dice
    body = await _json_dict(request)
    pc = db.query(PlayerCharacter).filter(PlayerCharacter.id == pc_id).first()
    if not pc:
        raise HTTPException(404)
    user = _current_user(request)
    if not _can_manage_character(user, pc):
        raise HTTPException(403)
    world = db.get(World, pc.world_id)
    actor = char_extras.actor_name(user)
    kind = body.get("kind", "stat")

    if kind == "stat":
        if not pc_maxima(pc)["native"]:
            raise HTTPException(400, "This character's system rolls a dice pool, not Stat + d10")
        stat = str(body.get("stat") or "").lower()
        pool = pool_roll.stat_pool(stat)
        if pool is None:
            raise HTTPException(400, "Unknown stat")
        stats = {str(s.get("id")): int(_num(s.get("value"), 0)) for s in json.loads(pc.stats_json or "[]") if isinstance(s, dict)}
        value = stats.get(stat, 0)
        boost = max(0, min(pool_roll.MAX_BOOST, _body_int(body, "boost")))
        mode = body.get("mode") if body.get("mode") in ("adv", "dis") else "normal"
        col = f"{pool}_current"
        have = int(getattr(pc, col, 0) or 0)
        if boost > have:
            raise HTTPException(400, f"Not enough {pool.upper()} to spend {boost}")
        res = pool_roll.roll_check(value, boost, mode)
        label_stat = {"str": "Strength", "dex": "Dexterity", "bod": "Body", "per": "Perception",
                      "wil": "Willpower", "int": "Intellect", "cha": "Charisma", "itu": "Intuition"}[stat]
        if boost:
            setattr(pc, col, have - boost)
            char_extras.log(db, pc, actor, "resource", f"{pool.upper()} {have} \u2192 {have - boost} (boosted a {label_stat} roll)",
                            {"op": pool, "value": have})
        die_term = {"term": ("2d10 keep higher" if mode == "adv" else "2d10 keep lower" if mode == "dis" else "1d10"),
                    "rolls": res["rolls"], "sum": res["die"]}
        parts = [die_term, {"term": f"+{value} {stat.upper()}", "sum": value}]
        if boost:
            parts.append({"term": f"+{boost} {pool.upper()}", "sum": boost})
        notation = ("2d10kh1" if mode == "adv" else "2d10kl1" if mode == "dis" else "1d10") + f"+{value}" + (f"+{boost}" if boost else "")
        logged, shared = _roll_log(db, request, world, pc, notation, parts, res["total"], f"{label_stat} check \u2014 {pc.name}")
        db.commit()
        live.touch(pc.world_id)
        return {"kind": "stat", "total": res["total"], "die": res["die"], "rolls": res["rolls"], "mode": mode, "crit": res["crit"],
                "fail": res["fail"], "shared": shared, "notation": notation, "roll": logged,
                "spent": {"pool": pool, "amount": boost, "current": have - boost, "max": pc_maxima(pc)[pool]} if boost else None}

    if kind != "pool":
        raise HTTPException(400, "kind must be stat or pool")
    tpl = db.get(SheetTemplate, pc.sheet_template_id) if getattr(pc, "sheet_template_id", None) else None
    spec = dice_spec(tpl)
    if spec is None:
        raise HTTPException(400, "This system has no dice-pool rules")
    cf = parse_custom_fields(pc.custom_fields_json)
    pool, base = pool_dice(spec, str(body.get("pool") or ""), cf)
    if pool is None:
        raise HTTPException(400, "Unknown pool")
    spend_spec = spec.get("spend")
    spend = max(0, min(10, _body_int(body, "spend"))) if spend_spec else 0
    mod = max(-3, min(3, _body_int(body, "mod")))
    need = max(0, min(6, _body_int(body, "need")))
    before, after = {}, {}
    if spend:
        fid = spend_spec["field"]
        tracks = {t["id"]: t for t in resource_tracks(template_fields(tpl), cf, system_meta(tpl))}
        have = int(tracks[fid]["current"]) if fid in tracks else 0
        if spend > have:
            raise HTTPException(400, f"Not enough {spend_spec['label']} to spend {spend}")
        before[f"{fid}_current"] = cf.get(f"{fid}_current", have)
        cf[f"{fid}_current"] = have - spend
        after[f"{fid}_current"] = have - spend
        tally = spend_spec.get("tally")
        if tally:
            spent_before = int(_num(cf.get(tally), 0))
            before[tally] = cf.get(tally, spent_before)
            cf[tally] = spent_before + spend
            after[tally] = cf[tally]
            into, every = spend_spec.get("into"), int(spend_spec.get("every") or 0)
            if into and every:
                fld = next((f for f in template_fields(tpl) if f.get("id") == into), {})
                top = max([int(_num(o, 0)) for o in fld.get("options") or []] or [3])
                cur = int(_num(cf.get(into), _num(fld.get("default_value"), 0)))
                want = min(top, cur if cur > (cf[tally] // every) else cf[tally] // every)
                if want != cur:
                    before[into] = cf.get(into, str(cur))
                    cf[into] = str(want)
                    after[into] = cf[into]
    dice = max(1, min(pool_roll.MAX_POOL, base + spend * int(spend_spec.get("per") or 1 if spend_spec else 0) + mod))
    res = pool_roll.roll_pool(dice, int(spec.get("threshold") or 6), bool(spec.get("explode", True)))
    ok = (res["successes"] >= need) if need else None
    if before:
        pc.custom_fields_json = json.dumps(cf)
        char_extras.log(db, pc, actor, "resource",
                        f"{spend_spec['label']} \u2212{spend} (dice pool)" , {"op": "cf", "values": before})
    parts = [{"term": f"{res['n']}d10 pool (6+ succeeds, 10 explodes)" if spec.get("explode", True) else f"{res['n']}d10 pool",
              "rolls": [d["v"] for d in res["dice"]], "sum": res["successes"]}]
    notation = f"{res['n']}d10 \u00b7 {int(spec.get('threshold') or 6)}+ \u00b7 {res['successes']} success{'es' if res['successes'] != 1 else ''}"
    logged, shared = _roll_log(db, request, world, pc, notation, parts, res["successes"], f"{pool['label']} \u2014 {pc.name}")
    db.commit()
    live.touch(pc.world_id)
    return {"kind": "pool", "dice": res["dice"], "n": res["n"], "base": base, "spend": spend, "mod": mod, "successes": res["successes"],
            "need": need, "ok": ok, "shared": shared, "roll": logged, "fields": after, "label": pool["label"],
            "spend_label": spend_spec["label"] if spend_spec else ""}


@router.post("/api/characters/roll")
async def dice_roll(request: Request):
    body = await request.json()
    expr = str(body.get("expr", "1d20")).lower().strip()
    import re
    m = re.match(r"^(\d+)d(\d+)([+-]\d+)?$", expr)
    if not m:
        return JSONResponse({"error": "Invalid dice expression"}, status_code=400)
    count = min(int(m.group(1)), 20)
    sides = int(m.group(2))
    modifier = int(m.group(3) or 0)
    if sides < 2 or sides > 1000:
        return JSONResponse({"error": "Invalid die size"}, status_code=400)
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier
    return {
        "expr": expr, "rolls": rolls, "modifier": modifier,
        "total": total,
        "crit":   count == 1 and sides == 20 and rolls[0] == 20,
        "fumble": count == 1 and sides == 20 and rolls[0] == 1,
    }
