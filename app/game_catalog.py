"""Neon & Dragons game-content catalog (races/professions/feats/equipment).

Bundled from the sibling UoY-Neon-Dragons rules repo's generated JSON assets
(NeonDragonsApp/app/src/main/assets/data/*.json) so the Player Character
creation wizard can offer real, ID-matched picks instead of free text.

To refresh after the rules repo's content changes, re-copy the *.json files
from that repo's assets/data/ directory into app/game_data/ here.
"""

import json
import re
from pathlib import Path

DATA_DIR = Path(__file__).parent / "game_data"


def _load(name):
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


RACES = _load("races")
PROFESSIONS = _load("professions")
FEATS = _load("feats")

EQUIPMENT_CATEGORIES = {
    "weapons": _load("weapons"),
    "armor": _load("armor"),
    "augments": _load("augments"),
    "bio_augments": _load("bio_augments"),
    "drones": _load("drones"),
    "vehicles": _load("vehicles"),
    "consumables": _load("consumables"),
    "programs": _load("programs"),
    "gadgets": _load("gadgets"),
    "special_equipment": _load("special_equipment"),
    "bases": _load("bases"),
    "husks": _load("husks"),
}

FEATS_BY_ID = {f["id"]: f for f in FEATS}
RACES_BY_ID = {r["id"]: r for r in RACES}
PROFESSIONS_BY_ID = {p["id"]: p for p in PROFESSIONS}

EQUIPMENT_BY_ID = {}
EQUIPMENT_CATEGORY_OF = {}
for _cat, _items in EQUIPMENT_CATEGORIES.items():
    for _it in _items:
        EQUIPMENT_BY_ID[_it["id"]] = _it
        EQUIPMENT_CATEGORY_OF[_it["id"]] = _cat

# Ritual feats auto-granted to Child of the Black Goat characters (ported from
# NeonDragonsEditor ui/character/character_creator.py RITUAL_FEAT_IDS).
RITUAL_FEAT_IDS = [
    "cradle_of_the_world_wound", "echoes_in_ash", "harvest_of_bone_and_growth",
    "head_of_spoken_truth", "mothers_reach_beyond_skies", "oath_of_bark_and_fang",
    "precognition_dream", "return_to_milk", "ritual_of_hollow_crown",
    "spawn_of_mycelium_dream", "spine_of_the_beast", "tattoo_of_all_mother",
    "the_buried_tongue", "traverse_land", "veil_of_the_verdant_god", "weather_change",
]

# Race feats granted via the Consumed by Yellow race's corruption mechanic rather
# than picked at creation — excluded from the Race Feat / Free Feat pickers.
POWER_OF_YELLOW_FEAT_IDS = {"yellow_ledger_of_debts", "yellow_rehearsal_of_futures", "yellow_static"}

# (label, physical points, mental points) — Core Rules §3 stat allocation options.
STAT_SPLITS = [
    ("15 Physical / 5 Mental", 15, 5),
    ("10 Physical / 10 Mental", 10, 10),
    ("5 Physical / 15 Mental", 5, 15),
]

CREATION_EQUIPMENT_RARITIES = {"", "simple", "standard"}
CREATION_BUDGET = 5000
PSYONIC_INITIAL_POWER_COUNT = 4

_STAT_NAME_TO_ABBR = {
    "strength": "str", "dexterity": "dex", "body": "bod", "perception": "per",
    "willpower": "wil", "intellect": "int", "charisma": "cha", "intuition": "itu",
}


def normalize(s: str) -> str:
    """Lowercase and strip separators for loose id comparison."""
    return (s or "").lower().replace("_", "").replace(" ", "").replace("/", "")


def matches_association(full_id: str, associated: str) -> bool:
    """Whether a race/profession id satisfies a feat's associatedRace/associatedProfession.

    Ported from NeonDragonsEditor ui/character/id_utils.py::matches_association so
    feat eligibility here matches the Editor and Android app exactly (e.g. a feat
    associated with "crimson_elf" is available to race id "crimson_elvesamalgama").
    """
    if not associated:
        return False
    norm_full = normalize(full_id)
    norm_assoc = normalize(associated)
    if norm_assoc in norm_full:
        return True
    norm_full_orc = norm_full.replace("orks", "orc").replace("ork", "orc")
    norm_assoc_orc = norm_assoc.replace("orks", "orc").replace("ork", "orc")
    if norm_assoc_orc in norm_full_orc:
        return True
    if "ork" not in norm_full and "drow" not in norm_full:
        norm_full_elf = norm_full.replace("elves", "elf")
        norm_assoc_elf = norm_assoc.replace("elves", "elf")
        if norm_assoc_elf in norm_full_elf:
            return True
    return False


def parse_race_bonuses(race: dict) -> dict:
    """Parse "+N <Stat>" / "-N <Stat>" bonuses out of a race's free-text description.

    Race JSON has no structured stat-bonus field (specialAttributes is empty for
    every race) — bonuses are embedded in prose, e.g. "+1 Willpower." — so this
    mirrors NeonDragonsEditor's _parse_race_bonuses() regex approach.
    """
    if not race:
        return {}
    text = (race.get("description") or "") + "\n" + "\n".join((race.get("sections") or {}).values())
    bonuses = {}
    for full_name, abbr in _STAT_NAME_TO_ABBR.items():
        m = re.search(rf'([+-])\s*(\d+)\s+{full_name}', text, re.IGNORECASE)
        if m:
            val = int(m.group(2))
            if m.group(1) == "-":
                val = -val
            bonuses[abbr] = val
    return bonuses


def parse_equipment_attrs(item: dict) -> dict:
    """Extract Rarity/Cost from an equipment entry's markdown "Attributes" bullet list."""
    text = (item.get("description") or "") + " " + " ".join((item.get("sections") or {}).values())
    attrs = {}
    m = re.search(r'Rarity\**:?\**\s*([A-Za-z]+)', text)
    if m:
        attrs["rarity"] = m.group(1).lower()
    m = re.search(r'Cost\**:?\**\s*(\d+)', text)
    if m:
        attrs["cost"] = int(m.group(1))
    return attrs


def catalog_payload() -> dict:
    """Full catalog JSON served to the character-creation wizard frontend."""
    equipment = []
    for cat, items in EQUIPMENT_CATEGORIES.items():
        for it in items:
            attrs = parse_equipment_attrs(it)
            equipment.append({
                "id": it["id"], "name": it["name"], "category": cat,
                "rarity": attrs.get("rarity", ""), "cost": attrs.get("cost", 0),
                "description": it.get("description") or "",
            })
    return {
        "races": [
            {
                "id": r["id"], "name": r["name"], "tier": r.get("tier", "Standard"),
                "description": r.get("description", ""),
                "bonuses": parse_race_bonuses(r),
            }
            for r in RACES
        ],
        "professions": [
            {"id": p["id"], "name": p["name"], "description": p.get("description", "")}
            for p in PROFESSIONS
        ],
        "feats": [
            {
                "id": f["id"], "name": f["name"], "category": f.get("category", ""),
                "rank": f.get("rank", ""), "associatedRace": f.get("associatedRace", ""),
                "associatedProfession": f.get("associatedProfession", ""),
                "description": f.get("description", ""),
            }
            for f in FEATS
        ],
        "equipment": equipment,
        "ritualFeatIds": RITUAL_FEAT_IDS,
        "powerOfYellowFeatIds": sorted(POWER_OF_YELLOW_FEAT_IDS),
        "statSplits": [{"label": l, "phys": p, "ment": m} for l, p, m in STAT_SPLITS],
        "creationEquipmentRarities": sorted(CREATION_EQUIPMENT_RARITIES),
        "creationBudget": CREATION_BUDGET,
        "psyonicInitialPowerCount": PSYONIC_INITIAL_POWER_COUNT,
    }
