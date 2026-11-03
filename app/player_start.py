"""The players' start page (the home page for a plain player; app/templates/index_player.html).

One dict, `start`, holds everything that page needs beyond the usual home context: what this player may open (the section matrix's PLAYER column — a GM
previewing with ?as=player sees the same), the pages the GM marked as the setting introduction and as character-sheet templates, the player's own sheets
and characters, and which optional AI tools the GM switched on. rust/nd-gateway/src/home.rs builds the same dict; the two are compared page by page."""
from sqlalchemy.orm import Session

from .deps import _default_section_levels, effective_kinds, world_section_access
from .models import CharacterSheet, PageDoc, PlayerCharacter

# the sections the start page links to (a card shows only when the player's level for it is read or edit)
SECTIONS = ("rules", "characters", "races", "professions", "pages", "dice", "quests", "sessions", "session_log", "parties", "combat", "maps",
            "calendar", "boards", "audio", "video", "chronicler")


def _character_limit(world) -> int:
    try:
        return max(1, int(getattr(world, "max_characters_per_player", None) or 1))
    except (TypeError, ValueError):
        return 1


def player_level(world, section_id: str) -> str:
    """The PLAYER column of the world's section matrix for `section_id` (what a real player gets; also what a GM sees when previewing)."""
    return world_section_access(world).get(section_id, _default_section_levels(section_id))["player"]


def build(db: Session, world, user, counts: dict, preview: bool) -> dict:
    can = {s: player_level(world, s) in ("read", "edit") for s in SECTIONS}
    docs = db.query(PageDoc).filter(PageDoc.world_id == world.id, PageDoc.visible_to_players.is_(True)).order_by(PageDoc.name, PageDoc.id).all()
    intro = [{"id": d.id, "name": d.name, "description": d.description or ""} for d in docs if d.is_setting_intro] if can["pages"] else []
    templates_ = [{"id": d.id, "name": d.name, "description": d.description or ""} for d in docs if d.is_character_sheet_template]
    my_sheets, my_chars = [], []
    if user is not None and not preview:
        for s in db.query(CharacterSheet).filter(CharacterSheet.world_id == world.id, CharacterSheet.owner_user_id == user.id).order_by(CharacterSheet.updated_at.desc()).all():
            my_sheets.append({"id": s.id, "name": s.name or ""})
        for c in db.query(PlayerCharacter).filter(PlayerCharacter.world_id == world.id, PlayerCharacter.owner_user_id == user.id).order_by(PlayerCharacter.id).all():
            my_chars.append({"id": c.id, "name": c.name, "race": c.race or "", "char_class": c.char_class or "", "level": c.level})
    kinds, _ = effective_kinds(world)
    explore = [k for k in kinds if counts.get(k) and player_level(world, f"kind_{k}") in ("read", "edit")]
    # the numbered steps, in order: only the steps this player has get a number
    n, nums = 0, {}
    for key, shown in (("intro", bool(intro)), ("rules", can["rules"]), ("character", can["characters"] or can["races"] or can["professions"]),
                       ("sheet", bool(templates_ or my_sheets))):
        if shown:
            n += 1
            nums[key] = n
    return {
        "can": can, "intro_pages": intro, "sheet_templates": templates_, "my_sheets": my_sheets, "my_characters": my_chars,
        "can_create_character": bool(can["characters"] and len(my_chars) < _character_limit(world)),
        "ai_chat": bool(world.players_can_use_ai_chat), "npc_talk": bool(world.players_can_ask_ai), "image_gen": bool(getattr(world, "players_can_use_image_gen", False)),
        "explore_kinds": explore, "nums": nums, "preview": preview,
    }
