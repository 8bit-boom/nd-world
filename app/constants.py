KINDS = ["character", "location", "organization", "creature", "event", "item", "feat", "note", "race", "profession"]

SUBTYPES = {
    "character": ["NPC", "PC", "villain", "ally", "neutral"],
    "location": ["district", "city", "country", "void station", "moon", "ruin", "corp facility"],
    "organization": ["megacorp", "syndicate", "government", "cult", "secret society", "gang", "AI entity", "family"],
    "creature": ["mutant", "animal", "abomination", "corp-enhanced", "ice creature", "undead"],
    "event": ["corporate war", "outbreak", "disaster", "political", "yellow corruption", "discovery"],
    "item": ["weapon", "armor", "augment", "bio-augmentation", "drone", "husk", "vehicle", "oddity", "metal", "item"],
    "feat": ["common feat", "origin feat", "profession feat", "profession ability", "psy power", "race feat"],
    "note": ["lore", "session note", "rumor", "prophecy", "theory", "tale"],
    # Tier, matching the bundled race catalog under app/races/<tier>/*.md.
    "race": ["standard", "advanced", "exceptional"],
    # Tier, matching the bundled profession catalog under app/professions/<tier>/*.md.
    "profession": ["standard", "advanced", "exceptional"],
}

KIND_ICONS = {
    "character": "👤", "location": "🗺", "organization": "🏢",
    "creature": "☠", "event": "⚡", "item": "⚙", "feat": "✦", "note": "📄",
    "race": "🧬", "profession": "🎭",
}

XP_THRESHOLDS = [
    0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
    85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
]

# Neon & Dragons default stat/currency templates
# 8 stats split Physical / Mental — total 20 points at creation
ND_DEFAULT_STATS = [
    # Physical
    {"id": "str", "label": "Strength",   "abbr": "STR", "value": 3},
    {"id": "dex", "label": "Dexterity",  "abbr": "DEX", "value": 3},
    {"id": "bod", "label": "Body",       "abbr": "BOD", "value": 3},
    {"id": "per", "label": "Perception", "abbr": "PER", "value": 3},
    # Mental
    {"id": "wil", "label": "Willpower",  "abbr": "WIL", "value": 2},
    {"id": "int", "label": "Intellect",  "abbr": "INT", "value": 2},
    {"id": "cha", "label": "Charisma",   "abbr": "CHA", "value": 2},
    {"id": "itu", "label": "Intuition",  "abbr": "ITU", "value": 2},
]

ND_DEFAULT_CURRENCY = [
    {"label": "Creds",  "abbr": "CR", "value": 0},
    {"label": "Tokens", "abbr": "TK", "value": 0},
]
