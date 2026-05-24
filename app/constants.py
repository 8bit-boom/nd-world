KINDS = ["character", "location", "organization", "creature", "event", "item", "feat", "note"]

SUBTYPES = {
    "character": ["NPC", "PC", "villain", "ally", "neutral"],
    "location": ["district", "city", "country", "void station", "moon", "ruin", "corp facility"],
    "organization": ["megacorp", "syndicate", "government", "cult", "secret society", "gang", "AI entity", "family"],
    "creature": ["mutant", "animal", "abomination", "corp-enhanced", "ice creature", "undead"],
    "event": ["corporate war", "outbreak", "disaster", "political", "yellow corruption", "discovery"],
    "item": ["weapon", "armor", "augment", "bio-augmentation", "drone", "husk", "vehicle", "oddity", "metal", "item"],
    "feat": ["common feat", "origin feat", "profession feat", "profession ability", "psy power", "race feat"],
    "note": ["lore", "session note", "rumor", "prophecy", "theory"],
}

KIND_ICONS = {
    "character": "👤", "location": "🗺", "organization": "🏢",
    "creature": "☠", "event": "⚡", "item": "⚙", "feat": "✦", "note": "📄",
}

XP_THRESHOLDS = [
    0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
    85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
]

# Neon & Dragons default stat/skill/currency templates
ND_DEFAULT_STATS = [
    {"id": "pow", "label": "Power",      "abbr": "POW", "value": 10},
    {"id": "agi", "label": "Agility",    "abbr": "AGI", "value": 10},
    {"id": "for", "label": "Fortitude",  "abbr": "FOR", "value": 10},
    {"id": "int", "label": "Intellect",  "abbr": "INT", "value": 10},
    {"id": "per", "label": "Perception", "abbr": "PER", "value": 10},
    {"id": "soc", "label": "Social",     "abbr": "SOC", "value": 10},
]

ND_DEFAULT_SKILLS = [
    {"id": "melee",         "label": "Melee",         "stat_id": "pow", "value": 0},
    {"id": "athletics",     "label": "Athletics",     "stat_id": "pow", "value": 0},
    {"id": "ranged",        "label": "Ranged",        "stat_id": "agi", "value": 0},
    {"id": "stealth",       "label": "Stealth",       "stat_id": "agi", "value": 0},
    {"id": "acrobatics",    "label": "Acrobatics",    "stat_id": "agi", "value": 0},
    {"id": "endurance",     "label": "Endurance",     "stat_id": "for", "value": 0},
    {"id": "hacking",       "label": "Hacking",       "stat_id": "int", "value": 0},
    {"id": "technology",    "label": "Technology",    "stat_id": "int", "value": 0},
    {"id": "lore",          "label": "Lore",          "stat_id": "int", "value": 0},
    {"id": "investigation", "label": "Investigation", "stat_id": "int", "value": 0},
    {"id": "awareness",     "label": "Awareness",     "stat_id": "per", "value": 0},
    {"id": "medicine",      "label": "Medicine",      "stat_id": "per", "value": 0},
    {"id": "streetwise",    "label": "Streetwise",    "stat_id": "soc", "value": 0},
    {"id": "deception",     "label": "Deception",     "stat_id": "soc", "value": 0},
    {"id": "persuasion",    "label": "Persuasion",    "stat_id": "soc", "value": 0},
    {"id": "intimidation",  "label": "Intimidation",  "stat_id": "soc", "value": 0},
]

ND_DEFAULT_CURRENCY = [
    {"label": "Creds",  "abbr": "CR", "value": 0},
    {"label": "Tokens", "abbr": "TK", "value": 0},
]
